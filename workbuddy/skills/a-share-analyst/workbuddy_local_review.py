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
from trading_calendar import latest_workbuddy_source_trade_date


SUMMARY_FILE = DATA_DIR / "workbuddy_local_account_summary_latest.json"
TRACK_FILE = DATA_DIR / "workbuddy_local_track_record.csv"
ORDER_LOG_FILE = DATA_DIR / "workbuddy_local_order_log.jsonl"
OPENING_TRADABILITY_FILE = DATA_DIR / "opening_tradability_latest.json"
REVIEW_FILE = DATA_DIR / "workbuddy_local_review_latest.json"


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _expected_source_trade_date(now_dt: datetime | None = None) -> str:
    return latest_workbuddy_source_trade_date(now_dt or datetime.now()).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    return None


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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except Exception:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def _load_track_record() -> list[dict[str, Any]]:
    if not TRACK_FILE.exists():
        return []
    rows: list[dict[str, Any]] = []
    with TRACK_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            rows.append(base._normalize_record(row))
    return rows


def _build_order_summary(order_rows: list[dict[str, Any]]) -> dict[str, Any]:
    today = _today_str()
    today_rows = [row for row in order_rows if str(row.get("logged_at", "")).startswith(today)]
    buy_rows = [row for row in today_rows if str(row.get("action", "")).strip() == "buy"]
    sell_rows = [row for row in today_rows if str(row.get("action", "")).strip() == "sell"]
    return {
        "today_order_count": len(today_rows),
        "today_buy_count": len(buy_rows),
        "today_sell_count": len(sell_rows),
        "latest_orders": today_rows[-10:],
    }


def _opening_data_status() -> dict[str, Any]:
    payload = _read_json(OPENING_TRADABILITY_FILE)
    if not payload:
        return {
            "status": "missing",
            "trade_date": "",
            "record_count": 0,
            "excluded_today_count": 0,
        }
    trade_date = str(payload.get("trade_date", "")).strip()
    records = payload.get("records", [])
    excluded = [
        item for item in records
        if str((item or {}).get("executor_action", "")).strip() == "exclude_today_buy_sell"
    ] if isinstance(records, list) else []
    status = "ok" if trade_date == _today_str() else "warning"
    return {
        "status": status,
        "trade_date": trade_date,
        "record_count": len(records) if isinstance(records, list) else 0,
        "excluded_today_count": len(excluded),
    }


def build_review(*, run_id: str = "", task_name: str = "", trigger_slot: str = "") -> dict[str, Any]:
    summary = _read_json(SUMMARY_FILE)
    track_records = _load_track_record()
    track_stats = base.compute_track_stats(track_records)
    order_rows = _read_jsonl(ORDER_LOG_FILE)
    order_summary = _build_order_summary(order_rows)
    opening_status = _opening_data_status()

    account_snapshot = summary.get("account_snapshot", {}) if isinstance(summary, dict) else {}
    performance = summary.get("performance", {}) if isinstance(summary, dict) else {}
    holdings = summary.get("holdings", []) if isinstance(summary, dict) else []

    source_status = str(summary.get("source_status", "")).strip()
    source_trade_date = str(summary.get("source_trade_date", "")).strip()
    summary_generated_at = str(summary.get("generated_at", "")).strip()
    summary_generated_dt = _parse_dt(summary_generated_at)
    expected_source_trade_date = _expected_source_trade_date(summary_generated_dt or datetime.now())
    blockers: list[str] = []
    warnings: list[str] = []
    if source_status != "ok":
        blockers.append("candidate_source_not_ok")
    if not source_trade_date:
        blockers.append("candidate_source_trade_date_missing")
    elif source_trade_date < expected_source_trade_date:
        blockers.append("candidate_source_trade_date_stale")
    if opening_status["status"] == "missing":
        blockers.append("opening_tradability_missing")
    if not SUMMARY_FILE.exists():
        blockers.append("account_summary_missing")
    if not TRACK_FILE.exists():
        blockers.append("track_record_missing")
    if not ORDER_LOG_FILE.exists():
        blockers.append("order_log_missing")
    if not summary_generated_dt:
        blockers.append("account_summary_generated_at_missing")
    elif summary_generated_dt.strftime("%Y-%m-%d") != _today_str():
        blockers.append("account_summary_stale")
    if order_summary["today_order_count"] <= 0:
        blockers.append("no_execution_evidence_today")
    elif order_summary["today_buy_count"] <= 0 and order_summary["today_sell_count"] <= 0:
        blockers.append("no_buy_sell_evidence_today")

    if blockers:
        review_verdict = "degraded"
    elif opening_status["status"] == "warning":
        review_verdict = "warning"
        warnings.append("opening_tradability_not_today")
    else:
        review_verdict = "ok"

    learning_sample_ready = bool(review_verdict in {"ok", "warning"} and track_stats["closed_count"] > 0)
    review = {
        "generated_at": _now_str(),
        "trade_date": _today_str(),
        "run_id": run_id,
        "task_name": task_name,
        "trigger_slot": trigger_slot,
        "portfolio_name": str(summary.get("portfolio_name", "Workbuddy")).strip() or "Workbuddy",
        "portfolio_type": str(summary.get("portfolio_type", "local_challenger_paper_account")).strip(),
        "review_verdict": review_verdict,
        "learning_sample_ready": learning_sample_ready,
        "source_alignment": {
            "source_file": str(summary.get("source_file", "")).strip(),
            "source_status": source_status,
            "source_trade_date": source_trade_date,
            "expected_source_trade_date": expected_source_trade_date,
        },
        "execution_health": {
            "opening_data_status": opening_status["status"],
            "opening_data_trade_date": opening_status["trade_date"],
            "opening_data_record_count": opening_status["record_count"],
            "opening_excluded_today_count": opening_status["excluded_today_count"],
            "today_order_count": order_summary["today_order_count"],
            "today_buy_count": order_summary["today_buy_count"],
            "today_sell_count": order_summary["today_sell_count"],
            "blockers": blockers,
            "warnings": warnings,
        },
        "portfolio_snapshot": {
            "initial_capital": account_snapshot.get("initial_capital", 0.0),
            "cash_balance": account_snapshot.get("cash_balance", 0.0),
            "market_value": account_snapshot.get("market_value", 0.0),
            "total_assets": account_snapshot.get("total_assets", 0.0),
            "floating_pnl": account_snapshot.get("floating_pnl", 0.0),
            "realized_pnl": account_snapshot.get("realized_pnl", 0.0),
            "total_pnl": account_snapshot.get("total_pnl", 0.0),
            "total_return_pct": account_snapshot.get("total_return_pct", 0.0),
            "holding_count": account_snapshot.get("holding_count", 0),
        },
        "trade_quality": {
            "closed_trade_count": track_stats["closed_count"],
            "holding_count": track_stats["holding_count"],
            "win_count": track_stats["win_count"],
            "closed_trade_win_rate_pct": track_stats["win_rate_pct"],
            "avg_closed_return_pct": track_stats["avg_return_pct"],
            "realized_pnl": track_stats["realized_pnl"],
            "champion_candidate_win_rate": performance.get("champion_candidate_win_rate", 0.0),
            "champion_candidate_avg_return": performance.get("champion_candidate_avg_return", 0.0),
            "champion_top50_hit_rate": performance.get("champion_top50_hit_rate", 0.0),
            "champion_front_shift_score": performance.get("champion_front_shift_score", 0.0),
        },
        "holdings_preview": holdings[:10] if isinstance(holdings, list) else [],
        "latest_orders": order_summary["latest_orders"],
        "notes": [
            "Workbuddy local challenger 先进入复核层，复核通过后才允许进入学习桥接层。",
            "学习层只吸收通过复核的 challenger 已平仓样本，不直接改主交易模型权重。",
            "若 challenger 当日未产生新的订单/账本证据，复核应降级，不允许给出假绿灯。",
        ],
        "files": {
            "summary_file": str(SUMMARY_FILE),
            "track_file": str(TRACK_FILE),
            "order_log_file": str(ORDER_LOG_FILE),
            "review_file": str(REVIEW_FILE),
        },
    }
    _write_json_atomic(REVIEW_FILE, review)
    return review


def main() -> int:
    parser = argparse.ArgumentParser(description="Workbuddy 本地 challenger 复核层桥接")
    parser.add_argument("--run-id", default="", help="自动化运行ID")
    parser.add_argument("--task-name", default="", help="任务名")
    parser.add_argument("--trigger-slot", default="", help="触发时段")
    args = parser.parse_args()
    payload = build_review(run_id=args.run_id, task_name=args.task_name, trigger_slot=args.trigger_slot)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
