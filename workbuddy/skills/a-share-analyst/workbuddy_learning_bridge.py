#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import v10_moni_trader as base
from package_paths import DATA_DIR


TRACK_FILE = DATA_DIR / "workbuddy_local_track_record.csv"
REVIEW_FILE = DATA_DIR / "workbuddy_local_review_latest.json"
SUMMARY_FILE = DATA_DIR / "workbuddy_local_account_summary_latest.json"
LEARNING_SAMPLES_FILE = DATA_DIR / "workbuddy_learning_samples.jsonl"
LEARNING_SCOREBOARD_FILE = DATA_DIR / "workbuddy_learning_scoreboard_latest.json"
LEARNING_ADVICE_FILE = DATA_DIR / "workbuddy_learning_advice_latest.json"


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    for encoding in ("utf-8", "utf-8-sig"):
        try:
            with path.open("r", encoding=encoding) as f:
                payload = json.load(f)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            continue
    return {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except Exception:
                continue
            sample_id = str((item or {}).get("sample_id", "")).strip()
            if sample_id:
                ids.add(sample_id)
    return ids


def _load_track_record() -> list[dict[str, Any]]:
    if not TRACK_FILE.exists():
        return []
    rows: list[dict[str, Any]] = []
    with TRACK_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            rows.append(base._normalize_record(row))
    return rows


def _sample_id(record: dict[str, Any]) -> str:
    code = str(record.get("code", "")).zfill(6)
    sell_date = str(record.get("sell_date", "")).strip()
    sell_order_id = str(record.get("sell_order_id", "")).strip()
    return f"{code}-{sell_date}-{sell_order_id or 'manual'}"


def _build_new_samples(
    closed_records: list[dict[str, Any]],
    *,
    existing_ids: set[str],
    review: dict[str, Any],
    summary: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in closed_records:
        sample_id = _sample_id(record)
        if sample_id in existing_ids:
            continue
        rows.append(
            {
                "sample_id": sample_id,
                "generated_at": _now_str(),
                "source": "workbuddy_local_challenger",
                "review_verdict": str(review.get("review_verdict", "")).strip(),
                "portfolio_name": str(summary.get("portfolio_name", "Workbuddy")).strip() or "Workbuddy",
                "code": str(record.get("code", "")).zfill(6),
                "name": str(record.get("name", "")).strip(),
                "buy_date": str(record.get("date", "")).strip(),
                "sell_date": str(record.get("sell_date", "")).strip(),
                "hold_days": int(float(str(record.get("hold_days", "0") or "0"))),
                "tier": str(record.get("tier", "")).strip(),
                "mode": str(record.get("mode", "")).strip(),
                "pnl": float(str(record.get("pnl", "0") or "0")),
                "pnl_pct": float(str(record.get("pnl_pct", "0") or "0")),
                "close_reason": str(record.get("close_reason", "")).strip(),
                "build_note": str(record.get("build_note", "")).strip(),
                "source_trade_date": str(summary.get("source_trade_date", "")).strip(),
            }
        )
    return rows


def _adoption_verdict(review: dict[str, Any], stats: dict[str, Any], summary: dict[str, Any]) -> tuple[str, str]:
    verdict = str(review.get("review_verdict", "")).strip()
    total_return_pct = float(((summary.get("account_snapshot") or {}).get("total_return_pct", 0.0)) or 0.0)
    win_rate = float(stats.get("win_rate_pct", 0.0))
    closed = int(stats.get("closed_count", 0))

    if verdict not in {"ok", "warning"}:
        return "blocked", "review_not_passed"
    if closed >= 12 and win_rate >= 55.0 and total_return_pct > 0:
        return "promote", "eligible_for_main_learning_review"
    if closed >= 5 and win_rate >= 50.0 and total_return_pct > 0:
        return "advisory", "eligible_for_shadow_learning_review"
    if closed > 0:
        return "observe", "waiting_for_more_clean_samples"
    return "observe", "no_closed_samples_yet"


def build_learning_bridge(*, run_id: str = "", task_name: str = "", trigger_slot: str = "") -> dict[str, Any]:
    review = _read_json(REVIEW_FILE)
    summary = _read_json(SUMMARY_FILE)
    records = _load_track_record()
    stats = base.compute_track_stats(records)
    closed_records = [row for row in records if str(row.get("status", "")).strip() == "closed"]

    existing_ids = _read_jsonl_ids(LEARNING_SAMPLES_FILE)
    if str(review.get("review_verdict", "")).strip() in {"ok", "warning"}:
        new_samples = _build_new_samples(closed_records, existing_ids=existing_ids, review=review, summary=summary)
        _append_jsonl(LEARNING_SAMPLES_FILE, new_samples)
    else:
        new_samples = []

    adoption_verdict, adoption_reason = _adoption_verdict(review, stats, summary)
    scoreboard = {
        "generated_at": _now_str(),
        "run_id": run_id,
        "task_name": task_name,
        "trigger_slot": trigger_slot,
        "source": "workbuddy_local_challenger",
        "review_verdict": str(review.get("review_verdict", "")).strip(),
        "learning_sample_ready": bool(review.get("learning_sample_ready", False)),
        "closed_trade_count": stats["closed_count"],
        "win_rate_pct": stats["win_rate_pct"],
        "avg_return_pct": stats["avg_return_pct"],
        "realized_pnl": stats["realized_pnl"],
        "new_sample_count": len(new_samples),
        "total_sample_count": len(existing_ids) + len(new_samples),
        "adoption_verdict": adoption_verdict,
        "adoption_reason": adoption_reason,
    }
    advice = {
        "generated_at": _now_str(),
        "run_id": run_id,
        "task_name": task_name,
        "trigger_slot": trigger_slot,
        "source": "workbuddy_local_challenger",
        "review_verdict": str(review.get("review_verdict", "")).strip(),
        "adoption_verdict": adoption_verdict,
        "adoption_reason": adoption_reason,
        "recommended_action": {
            "blocked": "do_not_touch_main_learning",
            "observe": "observe_only",
            "advisory": "shadow_learning_review",
            "promote": "eligible_for_main_learning_review",
        }.get(adoption_verdict, "observe_only"),
        "signals": {
            "closed_trade_count": stats["closed_count"],
            "win_rate_pct": stats["win_rate_pct"],
            "avg_return_pct": stats["avg_return_pct"],
            "realized_pnl": stats["realized_pnl"],
            "new_sample_count": len(new_samples),
        },
        "notes": [
            "本文件是 Workbuddy challenger 进入学习层前的桥接建议，不会直接修改主交易模型权重。",
            "只有当 review_verdict 通过且样本数、胜率、收益率满足门槛时，才允许进入主学习层人工或脚本复核。",
        ],
    }

    _write_json_atomic(LEARNING_SCOREBOARD_FILE, scoreboard)
    _write_json_atomic(LEARNING_ADVICE_FILE, advice)
    return {
        "scoreboard": scoreboard,
        "advice": advice,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Workbuddy challenger 学习桥接层")
    parser.add_argument("--run-id", default="", help="自动化运行ID")
    parser.add_argument("--task-name", default="", help="任务名")
    parser.add_argument("--trigger-slot", default="", help="触发时段")
    args = parser.parse_args()
    payload = build_learning_bridge(run_id=args.run_id, task_name=args.task_name, trigger_slot=args.trigger_slot)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
