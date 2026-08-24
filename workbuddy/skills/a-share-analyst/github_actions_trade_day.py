#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DO / GitHub 共用的日内总控脚本。

在单个日级触发里按中国市场时间顺序执行本地 Task Scheduler 的全部阶段，
避免外部 scheduler 只能表达粗粒度起点、无法覆盖 09:31 / 09:47 / 14:49 这类精确时点。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path

from register_workbuddy_tasks import ROOT, TASK_SPECS, TaskSpec, build_task_args
from trading_calendar import CALENDAR_SOURCE, is_trading_day


MARKET_TZ = timezone(timedelta(hours=8), name="UTC+08")
WAIT_CHUNK_SECONDS = 30
DEFAULT_MAX_LAG_SECONDS = 15 * 60
AUXILIARY_PHASE_PREFIX = "workbuddy-"
AUXILIARY_PHASE_TIMEOUTS = {
    "workbuddy-refresh": 240,
    "workbuddy-buy": 180,
    "workbuddy-sell": 120,
    "workbuddy-smart-sell": 180,
    "workbuddy-status": 45,
}
AUXILIARY_NEXT_CRITICAL_GUARD_SECONDS = 60
ASYNC_PHASE_TIMEOUTS = {
    'decision': 360,
}
SCHEDULE_NON_FATAL_EXIT_CODES = {2, 10, 11}


@dataclass
class AsyncTask:
    spec: TaskSpec
    process: object
    started_monotonic: float
    timeout_seconds: int
    timed_out: bool = False


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


def is_auxiliary_spec(spec: TaskSpec) -> bool:
    return spec.phase.startswith(AUXILIARY_PHASE_PREFIX)


def auxiliary_timeout_seconds(spec: TaskSpec) -> int | None:
    return AUXILIARY_PHASE_TIMEOUTS.get(spec.phase)


def next_critical_target(specs: list[TaskSpec], current_index: int, trade_date: date) -> datetime | None:
    for next_spec in specs[current_index + 1:]:
        if not is_auxiliary_spec(next_spec):
            return target_datetime(next_spec, trade_date)
    return None


def should_skip_auxiliary_task(
    spec: TaskSpec,
    *,
    now: datetime,
    current_index: int,
    specs: list[TaskSpec],
    trade_date: date,
) -> tuple[bool, str]:
    timeout_seconds = auxiliary_timeout_seconds(spec)
    next_critical_at = next_critical_target(specs, current_index, trade_date)
    if timeout_seconds is None or next_critical_at is None:
        return False, ""
    seconds_until_next_critical = int((next_critical_at - now).total_seconds())
    required_budget = timeout_seconds + AUXILIARY_NEXT_CRITICAL_GUARD_SECONDS
    if seconds_until_next_critical <= required_budget:
        return (
            True,
            (
                f"{spec.suffix} slot={spec.time_hhmm} skipped to preserve next critical slot "
                f"{next_critical_at:%H:%M}; remaining={seconds_until_next_critical}s budget={required_budget}s"
            ),
        )
    return False, ""


def run_task(spec: TaskSpec, *, env: dict[str, str], run_prefix: str, dry_run: bool) -> int:
    args = build_task_args(spec)
    args.extend(["--run-id", f"{run_prefix}-{spec.suffix.lower()}"])
    command_text = " ".join(f'"{arg}"' if " " in arg else arg for arg in args)
    print(f"[RUN] {spec.suffix} -> {command_text}")
    if dry_run:
        return 0
    timeout_seconds = auxiliary_timeout_seconds(spec) if is_auxiliary_spec(spec) else None
    try:
        completed = subprocess.run(args, cwd=ROOT, env=env, timeout=timeout_seconds)
        print(f"[DONE] {spec.suffix} exit_code={completed.returncode}")
        return completed.returncode
    except subprocess.TimeoutExpired:
        print(f"[TIMEOUT] {spec.suffix} exceeded auxiliary timeout={timeout_seconds}s")
        return 124


def is_async_spec(spec: TaskSpec) -> bool:
    return spec.phase in ASYNC_PHASE_TIMEOUTS


def is_schedule_nonfatal_exit(exit_code: int) -> bool:
    return int(exit_code) in SCHEDULE_NON_FATAL_EXIT_CODES


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f'.{path.name}.{os.getpid()}.{time.time_ns()}.tmp')
    try:
        with tmp_path.open('w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        tmp_path.replace(path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _publish_controller_decision_marker(*, env: dict[str, str], producer_run_id: str) -> dict:
    now = market_now()
    trade_date = now.date().isoformat()
    data_dir = Path(
        env.get('TLFZ_WORKBUDDY_DATA_DIR')
        or (ROOT.parent.parent / 'a-share-analyst')
    )
    pointer_path = data_dir / 'v10_decision_latest.json'
    timestamp = now.isoformat(sep=' ', timespec='microseconds')
    payload = {
        'schema_version': 1,
        'artifact_id': f'{trade_date}:decision:{producer_run_id}',
        'producer_run_id': producer_run_id,
        'phase': 'decision',
        'trade_date': trade_date,
        'complete': False,
        'state': 'starting',
        'decision_cutoff_at': f'{trade_date} 14:50:00',
        'started_at': timestamp,
        'published_at': timestamp,
        'market_timezone': 'UTC+08',
    }
    _write_json_atomic(pointer_path, payload)
    return payload


def start_async_task(spec: TaskSpec, *, env: dict[str, str], run_prefix: str, dry_run: bool):
    producer_run_id = f'{run_prefix}-{spec.suffix.lower()}'
    args = build_task_args(spec)
    args.extend(['--run-id', producer_run_id])
    command_text = ' '.join(f'"{arg}"' if ' ' in arg else arg for arg in args)
    print(f'[START-ASYNC] {spec.suffix} -> {command_text}')
    if dry_run:
        return None
    if spec.phase == 'decision':
        _publish_controller_decision_marker(env=env, producer_run_id=producer_run_id)
    popen_kwargs = {
        'cwd': ROOT,
        'env': env,
    }
    if os.name == 'nt':
        popen_kwargs['creationflags'] = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    else:
        popen_kwargs['start_new_session'] = True
    process = subprocess.Popen(args, **popen_kwargs)
    task = AsyncTask(
        spec=spec,
        process=process,
        started_monotonic=time.monotonic(),
        timeout_seconds=ASYNC_PHASE_TIMEOUTS[spec.phase],
    )
    threading.Thread(
        target=_async_task_watchdog,
        args=(task,),
        name=f'watchdog-{spec.suffix}',
        daemon=True,
    ).start()
    return task


def _async_task_watchdog(task: AsyncTask) -> None:
    try:
        task.process.wait(timeout=task.timeout_seconds)
    except subprocess.TimeoutExpired:
        task.timed_out = True
        print(f'[TIMEOUT-ASYNC] {task.spec.suffix} exceeded {task.timeout_seconds}s')
        _terminate_async_process_tree(task)


def _terminate_async_process_tree(task: AsyncTask) -> None:
    process = task.process
    if process.poll() is not None:
        return
    try:
        if os.name == 'nt':
            subprocess.run(
                ['taskkill', '/PID', str(process.pid), '/T', '/F'],
                capture_output=True,
                timeout=5,
            )
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    if os.name != 'nt':
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    elif process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def collect_finished_async_tasks(tasks: list[AsyncTask]) -> tuple[list[AsyncTask], list[tuple[str, int]]]:
    remaining_tasks = []
    completed_results = []
    now_monotonic = time.monotonic()
    for task in tasks:
        return_code = task.process.poll()
        elapsed = now_monotonic - task.started_monotonic
        if return_code is None and elapsed >= task.timeout_seconds:
            task.timed_out = True
            print(f'[TIMEOUT-ASYNC] {task.spec.suffix} exceeded {task.timeout_seconds}s')
            _terminate_async_process_tree(task)
            return_code = 124
        elif task.timed_out:
            return_code = 124
        if return_code is None:
            remaining_tasks.append(task)
            continue
        print(f'[DONE-ASYNC] {task.spec.suffix} exit_code={return_code}')
        completed_results.append((task.spec.suffix, int(return_code)))
    return remaining_tasks, completed_results


def finish_async_tasks(tasks: list[AsyncTask]) -> list[tuple[str, int]]:
    results = []
    for task in tasks:
        remaining_seconds = max(0.0, task.timeout_seconds - (time.monotonic() - task.started_monotonic))
        try:
            return_code = task.process.wait(timeout=remaining_seconds)
        except subprocess.TimeoutExpired:
            task.timed_out = True
            print(f'[TIMEOUT-ASYNC] {task.spec.suffix} exceeded {task.timeout_seconds}s')
            _terminate_async_process_tree(task)
            return_code = 124
        if task.timed_out:
            return_code = 124
        print(f'[DONE-ASYNC] {task.spec.suffix} exit_code={return_code}')
        results.append((task.spec.suffix, int(return_code)))
    return results


def _ordered_failures(failures: dict[str, int], specs: list[TaskSpec]) -> list[tuple[str, int]]:
    ordered = [(spec.suffix, failures[spec.suffix]) for spec in specs if spec.suffix in failures]
    known = {suffix for suffix, _code in ordered}
    ordered.extend(sorted((suffix, code) for suffix, code in failures.items() if suffix not in known))
    return ordered


def iter_selected_specs(start_from_slot: str) -> list[TaskSpec]:
    specs = sorted(TASK_SPECS, key=slot_key)
    if not start_from_slot:
        return specs
    threshold = parse_hhmm(start_from_slot).strftime("%H:%M")
    return [spec for spec in specs if spec.time_hhmm >= threshold]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the full A-share trade-day schedule from the shared day launcher.")
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
    run_prefix = (
        f"gha-day-{trade_date.isoformat()}-{os.environ.get('GITHUB_RUN_ID', 'local')}-"
        f"{os.getpid()}-{time.time_ns()}"
    )
    selected_specs = iter_selected_specs(args.start_from_slot)
    if not selected_specs:
        print("[SKIP] no tasks selected after start-from-slot filtering.")
        return 0

    print(f"[PLAN] trade_date={trade_date.isoformat()} start_from_slot={args.start_from_slot or 'ALL'} task_count={len(selected_specs)}")
    critical_failures: dict[str, int] = {}
    auxiliary_failures: dict[str, int] = {}
    overdue_skips: list[str] = []
    auxiliary_skips: list[str] = []
    running_async_tasks: list[AsyncTask] = []
    for index, spec in enumerate(selected_specs):
        running_async_tasks, async_results = collect_finished_async_tasks(running_async_tasks)
        for suffix, exit_code in async_results:
            if exit_code != 0 and not is_schedule_nonfatal_exit(exit_code):
                critical_failures[suffix] = exit_code
        target_at = target_datetime(spec, trade_date)
        now = market_now()
        lag_seconds = int((now - target_at).total_seconds())
        if lag_seconds > args.max_lag_seconds:
            print(f"[SKIP] {spec.suffix} slot={spec.time_hhmm} overdue_by={lag_seconds}s exceeds max_lag_seconds={args.max_lag_seconds}")
            target_list = auxiliary_skips if is_auxiliary_spec(spec) else overdue_skips
            target_list.append(f"{spec.suffix}@{spec.time_hhmm}")
            continue
        if now < target_at:
            wait_until(target_at, dry_run=args.dry_run)
        running_async_tasks, async_results = collect_finished_async_tasks(running_async_tasks)
        for suffix, exit_code in async_results:
            if exit_code != 0 and not is_schedule_nonfatal_exit(exit_code):
                critical_failures[suffix] = exit_code
        now = market_now()
        if is_auxiliary_spec(spec):
            should_skip, reason = should_skip_auxiliary_task(
                spec,
                now=now,
                current_index=index,
                specs=selected_specs,
                trade_date=trade_date,
            )
            if should_skip:
                print(f"[SKIP] {reason}")
                auxiliary_skips.append(f"{spec.suffix}@{spec.time_hhmm}")
                continue
        if is_async_spec(spec):
            async_task = start_async_task(
                spec,
                env=env,
                run_prefix=run_prefix,
                dry_run=args.dry_run,
            )
            if async_task is not None:
                running_async_tasks.append(async_task)
            continue
        exit_code = run_task(spec, env=env, run_prefix=run_prefix, dry_run=args.dry_run)
        if exit_code != 0 and not is_schedule_nonfatal_exit(exit_code):
            target_map = auxiliary_failures if is_auxiliary_spec(spec) else critical_failures
            target_map[spec.suffix] = exit_code
        elif exit_code != 0:
            print(f'[INFO] schedule semantic non-fatal: {spec.suffix} exit_code={exit_code}')

    for suffix, exit_code in finish_async_tasks(running_async_tasks):
        if exit_code != 0 and not is_schedule_nonfatal_exit(exit_code):
            critical_failures[suffix] = exit_code

    if overdue_skips and not args.dry_run:
        summary = ", ".join(overdue_skips)
        print(f"[FAIL] missed scheduled slots because the workflow started too late -> {summary}")
        return 86
    if critical_failures:
        ordered_critical = _ordered_failures(critical_failures, selected_specs)
        summary = ", ".join(f"{suffix}:{code}" for suffix, code in ordered_critical)
        print(f"[FAIL] one or more scheduled tasks failed -> {summary}")
        if auxiliary_failures:
            ordered_auxiliary = _ordered_failures(auxiliary_failures, selected_specs)
            aux_summary = ", ".join(f"{suffix}:{code}" for suffix, code in ordered_auxiliary)
            print(f"[WARN] auxiliary challenger/workbuddy tasks also failed -> {aux_summary}")
        if auxiliary_skips:
            skip_summary = ", ".join(auxiliary_skips)
            print(f"[WARN] auxiliary challenger/workbuddy tasks skipped -> {skip_summary}")
        return ordered_critical[-1][1]
    if auxiliary_failures:
        ordered_auxiliary = _ordered_failures(auxiliary_failures, selected_specs)
        summary = ", ".join(f"{suffix}:{code}" for suffix, code in ordered_auxiliary)
        print(f"[WARN] auxiliary challenger/workbuddy tasks failed but mainline stayed healthy -> {summary}")
    if auxiliary_skips:
        summary = ", ".join(auxiliary_skips)
        print(f"[WARN] auxiliary challenger/workbuddy tasks skipped to protect mainline -> {summary}")
    print("[OK] trade-day schedule completed without task failures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
