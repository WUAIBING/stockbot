#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Actions self-hosted 日内总控脚本。

在单个 workflow 里按中国市场时间顺序执行本地 Task Scheduler 的全部阶段，
避免 GitHub schedule 5 分钟粒度无法覆盖 09:31 / 09:47 / 14:49 这类精确时点。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path

from register_workbuddy_tasks import ROOT, TASK_SPECS, TaskSpec, build_task_args
from trading_calendar import CALENDAR_SOURCE, is_trading_day


MARKET_TZ = timezone(timedelta(hours=8), name="UTC+08")
WAIT_CHUNK_SECONDS = 30
DEFAULT_MAX_LAG_SECONDS = 15 * 60


def market_now() -> datetime:
    return datetime.now(MARKET_TZ)


def parse_trade_date(raw: str) -> date:
    text = str(raw or "").strip()
    if not text:
        return market_now().date()
    return datetime.strptime(text[:10], "%Y-%m-%d").date()


def parse_hhmm(raw: str) -> dt_time:
    return datetime.strptime(raw.strip(), "%H:%M").time()


def slot_key(spec: TaskSpec) -> tuple[int, str]:
    hour, minute = spec.time_hhmm.split(":", 1)
    return int(hour) * 60 + int(minute), spec.suffix


def target_datetime(spec: TaskSpec, trade_date: date) -> datetime:
    slot_time = parse_hhmm(spec.time_hhmm)
    return datetime.combine(trade_date, slot_time, tzinfo=MARKET_TZ)


def ensure_runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    repo_root = ROOT.parent.parent.parent
    workbuddy_root = ROOT.parent.parent
    defaults = {
        "TLFZ_ARKCLAW_ROOT": str(repo_root),
        "TLFZ_WORKBUDDY_ROOT": str(workbuddy_root),
        "TLFZ_WORKBUDDY_SKILL_ROOT": str(ROOT),
        "TLFZ_WORKBUDDY_DATA_DIR": str(workbuddy_root / "a-share-analyst"),
        "TLFZ_WORKBUDDY_POOL_DIR": str(repo_root / "workbuddy_pool"),
    }
    for key, value in defaults.items():
        env.setdefault(key, value)
    Path(env["TLFZ_WORKBUDDY_DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(env["TLFZ_WORKBUDDY_POOL_DIR"]).mkdir(parents=True, exist_ok=True)
    return env


def wait_until(target_at: datetime, *, dry_run: bool) -> None:
    while True:
        remaining = (target_at - market_now()).total_seconds()
        if remaining <= 0:
            return
        sleep_seconds = min(WAIT_CHUNK_SECONDS, max(1, int(remaining)))
        print(f"[WAIT] until {target_at:%H:%M:%S} CST ({sleep_seconds}s)")
        if dry_run:
            return
        time.sleep(sleep_seconds)


def run_task(spec: TaskSpec, *, env: dict[str, str], run_prefix: str, dry_run: bool) -> int:
    args = build_task_args(spec)
    args.extend(["--run-id", f"{run_prefix}-{spec.suffix.lower()}"])
    command_text = " ".join(f'"{arg}"' if " " in arg else arg for arg in args)
    print(f"[RUN] {spec.suffix} -> {command_text}")
    if dry_run:
        return 0
    completed = subprocess.run(args, cwd=ROOT, env=env)
    print(f"[DONE] {spec.suffix} exit_code={completed.returncode}")
    return completed.returncode


def iter_selected_specs(start_from_slot: str) -> list[TaskSpec]:
    specs = sorted(TASK_SPECS, key=slot_key)
    if not start_from_slot:
        return specs
    threshold = parse_hhmm(start_from_slot).strftime("%H:%M")
    return [spec for spec in specs if spec.time_hhmm >= threshold]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the full A-share trade-day schedule inside GitHub Actions.")
    parser.add_argument("--trade-date", default="", help="Market trade date in YYYY-MM-DD. Defaults to current UTC+8 date.")
    parser.add_argument("--start-from-slot", default="", help="Optional HH:MM slot to skip earlier tasks.")
    parser.add_argument("--max-lag-seconds", type=int, default=DEFAULT_MAX_LAG_SECONDS, help="Run overdue tasks immediately only when lag <= this threshold.")
    parser.add_argument("--dry-run", action="store_true", help="Print the resolved schedule without executing commands.")
    args = parser.parse_args()

    trade_date = parse_trade_date(args.trade_date)
    if not is_trading_day(trade_date):
        print(f"[SKIP] {trade_date.isoformat()} is not a trading day ({CALENDAR_SOURCE}).")
        return 0

    env = ensure_runtime_env()
    run_prefix = f"gha-day-{trade_date.isoformat()}-{os.environ.get('GITHUB_RUN_ID', 'local')}"
    selected_specs = iter_selected_specs(args.start_from_slot)
    if not selected_specs:
        print("[SKIP] no tasks selected after start-from-slot filtering.")
        return 0

    print(f"[PLAN] trade_date={trade_date.isoformat()} start_from_slot={args.start_from_slot or 'ALL'} task_count={len(selected_specs)}")
    failures: list[tuple[str, int]] = []
    overdue_skips: list[str] = []
    for spec in selected_specs:
        target_at = target_datetime(spec, trade_date)
        now = market_now()
        lag_seconds = int((now - target_at).total_seconds())
        if lag_seconds > args.max_lag_seconds:
            print(f"[SKIP] {spec.suffix} slot={spec.time_hhmm} overdue_by={lag_seconds}s exceeds max_lag_seconds={args.max_lag_seconds}")
            overdue_skips.append(f"{spec.suffix}@{spec.time_hhmm}")
            continue
        if now < target_at:
            wait_until(target_at, dry_run=args.dry_run)
        exit_code = run_task(spec, env=env, run_prefix=run_prefix, dry_run=args.dry_run)
        if exit_code != 0:
            failures.append((spec.suffix, exit_code))

    if overdue_skips and not args.dry_run:
        summary = ", ".join(overdue_skips)
        print(f"[FAIL] missed scheduled slots because the workflow started too late -> {summary}")
        return 86
    if failures:
        summary = ", ".join(f"{suffix}:{code}" for suffix, code in failures)
        print(f"[FAIL] one or more scheduled tasks failed -> {summary}")
        return failures[-1][1]
    print("[OK] trade-day schedule completed without task failures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
