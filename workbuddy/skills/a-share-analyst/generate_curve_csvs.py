#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate foundational curve CSVs for the curve observatory.

Outputs:
  - curve_nav_daily.csv
  - curve_realized_pnl_daily.csv
  - curve_trade_success_daily.csv
  - curve_benchmark_daily.csv
  - curve_learning_readiness_daily.csv
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from package_paths import DATA_DIR
from trading_calendar import calendar_context

try:
    from pytdx.hq import TdxHq_API
except Exception:  # pragma: no cover - optional runtime dependency
    TdxHq_API = None


CURVE_DATA_DIR = DATA_DIR / "curve_observatory" / "data"
NAV_HISTORY_FILE = DATA_DIR / "v10_nav_history.csv"
TRACK_RECORD_FILE = DATA_DIR / "v10_track_record.csv"
TRADE_API_LOG_FILE = DATA_DIR / "v10_trade_api_log.jsonl"
MODEL_STATE_FILE = DATA_DIR / "v10_evolving_model_state.json"
MODEL_CHANGELOG_FILE = DATA_DIR / "v10_evolving_model_changelog.jsonl"
LEARNING_READINESS_START_DATE = "2026-06-18"

TDX_HOSTS: List[Tuple[str, int]] = [
    ("218.75.126.9", 7709),
    ("60.191.117.167", 7709),
    ("112.74.214.43", 7727),
    ("221.231.141.60", 7709),
]

BENCHMARKS = {
    "csi1000": {"market": 1, "code": "000852", "name": "中证1000"},
    "chinext": {"market": 0, "code": "399006", "name": "创业板指"},
}


def _fnum(value, default: float = 0.0) -> float:
    try:
        text = str(value).strip()
        return float(text) if text else default
    except (TypeError, ValueError):
        return default


def _inum(value, default: int = 0) -> int:
    try:
        text = str(value).strip()
        return int(float(text)) if text else default
    except (TypeError, ValueError):
        return default


def _parse_datetime(date_str: str, time_str: str = "") -> Optional[datetime]:
    raw = f"{str(date_str).strip()} {str(time_str).strip()}".strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _ensure_output_dir() -> None:
    CURVE_DATA_DIR.mkdir(parents=True, exist_ok=True)


def _read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return [dict(row) for row in reader]


def _write_csv(path: Path, fieldnames: List[str], rows: Iterable[Dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _date_in_scope(date_key: str, as_of_date: str = "") -> bool:
    text = str(date_key).strip()
    if not text:
        return False
    return not as_of_date or text <= as_of_date


def build_nav_daily(as_of_date: str = "") -> List[Dict[str, object]]:
    grouped: Dict[str, Dict[str, object]] = {}
    for row in _read_csv_rows(NAV_HISTORY_FILE):
        date_key = str(row.get("date", "")).strip()
        dt = _parse_datetime(row.get("date", ""), row.get("time", ""))
        if not _date_in_scope(date_key, as_of_date) or dt is None:
            continue
        payload = {
            "date": date_key,
            "recorded_at": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "tag": str(row.get("tag", "")).strip(),
            "total_assets": round(_fnum(row.get("total_assets")), 2),
            "avail_balance": round(_fnum(row.get("avail_balance")), 2),
            "total_pos_value": round(_fnum(row.get("total_pos_value")), 2),
            "position_count": _inum(row.get("position_count")),
            "holding_records": _inum(row.get("holding_records")),
            "closed_records": _inum(row.get("closed_records")),
            "realized_pnl": round(_fnum(row.get("realized_pnl")), 2),
            "floating_pnl": round(_fnum(row.get("floating_pnl")), 2),
            "win_rate_pct": round(_fnum(row.get("win_rate_pct")), 4),
            "avg_return_pct": round(_fnum(row.get("avg_return_pct")), 4),
            "is_valid_snapshot": 1 if _fnum(row.get("total_assets")) > 0 else 0,
        }
        prev = grouped.get(date_key)
        if prev is None:
            grouped[date_key] = payload
            continue
        prev_dt = _parse_datetime(prev.get("date", ""), prev.get("recorded_at", "").split(" ", 1)[-1])
        prev_valid = int(prev.get("is_valid_snapshot", 0))
        cur_valid = int(payload["is_valid_snapshot"])
        if cur_valid > prev_valid or (
            cur_valid == prev_valid and prev_dt is not None and dt >= prev_dt
        ):
            grouped[date_key] = payload
    return [grouped[key] for key in sorted(grouped)]


def build_realized_pnl_daily(nav_daily: List[Dict[str, object]], as_of_date: str = "") -> List[Dict[str, object]]:
    grouped: Dict[str, Dict[str, object]] = defaultdict(
        lambda: {
            "closed_trade_count": 0,
            "win_trade_count": 0,
            "loss_trade_count": 0,
            "daily_closed_pnl": 0.0,
            "daily_closed_return_pct_sum": 0.0,
        }
    )
    for row in _read_csv_rows(TRACK_RECORD_FILE):
        if str(row.get("status", "")).strip().lower() != "closed":
            continue
        date_key = str(row.get("sell_date", "")).strip()
        if not _date_in_scope(date_key, as_of_date):
            continue
        pnl = _fnum(row.get("pnl"))
        pnl_pct = _fnum(row.get("pnl_pct"))
        bucket = grouped[date_key]
        bucket["closed_trade_count"] += 1
        bucket["daily_closed_pnl"] += pnl
        bucket["daily_closed_return_pct_sum"] += pnl_pct
        if pnl > 0:
            bucket["win_trade_count"] += 1
        elif pnl < 0:
            bucket["loss_trade_count"] += 1

    nav_map = {str(row["date"]): row for row in nav_daily}
    rows: List[Dict[str, object]] = []
    cumulative = 0.0
    for date_key in sorted(grouped):
        bucket = grouped[date_key]
        trade_count = int(bucket["closed_trade_count"])
        daily_closed_pnl = round(_fnum(bucket["daily_closed_pnl"]), 2)
        cumulative = round(cumulative + daily_closed_pnl, 2)
        avg_closed_return_pct = round(
            bucket["daily_closed_return_pct_sum"] / trade_count, 4
        ) if trade_count else 0.0
        nav_snapshot = nav_map.get(date_key, {})
        rows.append(
            {
                "date": date_key,
                "closed_trade_count": trade_count,
                "win_trade_count": int(bucket["win_trade_count"]),
                "loss_trade_count": int(bucket["loss_trade_count"]),
                "daily_closed_pnl": daily_closed_pnl,
                "cumulative_closed_pnl": cumulative,
                "avg_closed_return_pct": avg_closed_return_pct,
                "nav_realized_pnl_snapshot": round(_fnum(nav_snapshot.get("realized_pnl")), 2),
                "nav_snapshot_tag": str(nav_snapshot.get("tag", "")),
                "nav_snapshot_recorded_at": str(nav_snapshot.get("recorded_at", "")),
            }
        )
    return rows


def build_trade_success_daily(as_of_date: str = "") -> List[Dict[str, object]]:
    grouped: Dict[str, Dict[str, object]] = defaultdict(lambda: defaultdict(int))
    if not TRADE_API_LOG_FILE.exists():
        return []
    with TRADE_API_LOG_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if not text:
                continue
            try:
                entry = json.loads(text)
            except json.JSONDecodeError:
                continue
            logged_at = str(entry.get("logged_at", "")).strip()
            date_key = logged_at.split(" ", 1)[0] if logged_at else ""
            if not _date_in_scope(date_key, as_of_date):
                continue
            action = str(entry.get("action", "")).strip().lower()
            ok = bool(entry.get("ok"))
            result_code = str(entry.get("result_code", "")).strip()
            bucket = grouped[date_key]
            bucket["total_requests"] += 1
            bucket[f"{action}_requests"] += 1
            if ok:
                bucket["success_count"] += 1
                bucket[f"{action}_success_count"] += 1
            else:
                bucket["failure_count"] += 1
                bucket[f"{action}_failure_count"] += 1
            if result_code == "112":
                bucket["rate_limit_112_count"] += 1
            if result_code == "501":
                bucket["insufficient_501_count"] += 1

    rows: List[Dict[str, object]] = []
    for date_key in sorted(grouped):
        bucket = grouped[date_key]
        total = int(bucket["total_requests"])
        buy_requests = int(bucket["buy_requests"])
        sell_requests = int(bucket["sell_requests"])
        success_count = int(bucket["success_count"])
        buy_success = int(bucket["buy_success_count"])
        sell_success = int(bucket["sell_success_count"])
        rows.append(
            {
                "date": date_key,
                "total_requests": total,
                "success_count": success_count,
                "failure_count": int(bucket["failure_count"]),
                "success_rate_pct": round((success_count / total * 100.0) if total else 0.0, 2),
                "buy_requests": buy_requests,
                "buy_success_count": buy_success,
                "buy_failure_count": int(bucket["buy_failure_count"]),
                "buy_success_rate_pct": round((buy_success / buy_requests * 100.0) if buy_requests else 0.0, 2),
                "sell_requests": sell_requests,
                "sell_success_count": sell_success,
                "sell_failure_count": int(bucket["sell_failure_count"]),
                "sell_success_rate_pct": round((sell_success / sell_requests * 100.0) if sell_requests else 0.0, 2),
                "rate_limit_112_count": int(bucket["rate_limit_112_count"]),
                "insufficient_501_count": int(bucket["insufficient_501_count"]),
            }
        )
    return rows


def _reason_label(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    reason = str(item.get("reason", "")).strip()
    count = _inum(item.get("count"), 0)
    if not reason:
        return ""
    return f"{reason} ({count})" if count > 0 else reason


def _learning_status_from_entry(event_type: str, update_reason: str) -> str:
    if event_type == "apply_update":
        return "updated_with_guardrails"
    if update_reason == "cooldown_active":
        return "guardrail_cooldown"
    if update_reason == "insufficient_eligible_samples":
        return "guardrail_waiting_clean_samples"
    return "guardrail_waiting_samples"


def _build_learning_row(
    *,
    date_key: str,
    recorded_at: str,
    source_event_type: str,
    recent_closed_trades: int,
    gross_matched_trades: int,
    eligible_matched_trades: int,
    blocked_trades: int,
    eligible_rate_pct: float,
    top_reason_1: str,
    top_reason_2: str,
    learning_status: str,
    learning_notes: str,
    update_reason: str,
) -> Dict[str, object]:
    gross_match_rate_pct = round(
        gross_matched_trades / max(recent_closed_trades, 1) * 100.0, 2
    ) if recent_closed_trades else 0.0
    clean_after_match_rate_pct = round(
        eligible_matched_trades / max(gross_matched_trades, 1) * 100.0, 2
    ) if gross_matched_trades else 0.0
    return {
        "date": date_key,
        "recorded_at": recorded_at,
        "source_event_type": source_event_type,
        "recent_closed_trades": recent_closed_trades,
        "gross_matched_trades": gross_matched_trades,
        "eligible_matched_trades": eligible_matched_trades,
        "blocked_trades": blocked_trades,
        "gross_match_rate_pct": gross_match_rate_pct,
        "eligible_rate_pct": round(eligible_rate_pct, 2),
        "clean_after_match_rate_pct": clean_after_match_rate_pct,
        "top_block_reason_1": top_reason_1,
        "top_block_reason_2": top_reason_2,
        "learning_status": learning_status,
        "learning_notes": learning_notes,
        "update_reason": update_reason,
    }


def build_learning_readiness_daily(as_of_date: str = "") -> List[Dict[str, object]]:
    grouped: Dict[str, Dict[str, object]] = {}
    if MODEL_CHANGELOG_FILE.exists():
        with MODEL_CHANGELOG_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                text = line.strip()
                if not text:
                    continue
                try:
                    entry = json.loads(text)
                except json.JSONDecodeError:
                    continue
                recorded_at = str(entry.get("recorded_at", "")).strip()
                date_key = recorded_at.split(" ", 1)[0] if recorded_at else ""
                if not _date_in_scope(date_key, as_of_date) or date_key < LEARNING_READINESS_START_DATE:
                    continue
                payload = entry.get("payload", {}) if isinstance(entry.get("payload"), dict) else {}
                sample_filter = payload.get("sample_filter", {}) if isinstance(payload.get("sample_filter"), dict) else {}
                if not sample_filter and not payload.get("gross_matched_trades") and not payload.get("eligible_matched_trades"):
                    continue
                top_reasons = sample_filter.get("reason_counts_top", []) if isinstance(sample_filter.get("reason_counts_top"), list) else []
                row = _build_learning_row(
                    date_key=date_key,
                    recorded_at=recorded_at,
                    source_event_type=str(entry.get("event_type", "")).strip(),
                    recent_closed_trades=_inum(payload.get("recent_closed_trades"), 0),
                    gross_matched_trades=_inum(
                        payload.get("gross_matched_trades", sample_filter.get("gross_matched_trades")),
                        0,
                    ),
                    eligible_matched_trades=_inum(
                        payload.get("eligible_matched_trades", sample_filter.get("eligible_matched_trades")),
                        0,
                    ),
                    blocked_trades=_inum(sample_filter.get("blocked_trades"), 0),
                    eligible_rate_pct=_fnum(sample_filter.get("eligible_rate_pct"), 0.0),
                    top_reason_1=_reason_label(top_reasons[0] if len(top_reasons) > 0 else {}),
                    top_reason_2=_reason_label(top_reasons[1] if len(top_reasons) > 1 else {}),
                    learning_status=_learning_status_from_entry(
                        str(entry.get("event_type", "")).strip(),
                        str(payload.get("reason", "")).strip(),
                    ),
                    learning_notes="",
                    update_reason=str(payload.get("reason", "")).strip(),
                )
                prev = grouped.get(date_key)
                if prev is None or str(prev.get("recorded_at", "")) <= recorded_at:
                    grouped[date_key] = row

    state = _read_json(MODEL_STATE_FILE, {}) or {}
    updated_at = str(state.get("updated_at", "")).strip()
    state_date = updated_at.split(" ", 1)[0] if updated_at else ""
    learning = state.get("learning", {}) if isinstance(state.get("learning"), dict) else {}
    sample_filter = learning.get("sample_filter", {}) if isinstance(learning.get("sample_filter"), dict) else {}
    if _date_in_scope(state_date, as_of_date) and state_date >= LEARNING_READINESS_START_DATE and sample_filter:
        top_reasons = sample_filter.get("reason_counts_top", []) if isinstance(sample_filter.get("reason_counts_top"), list) else []
        grouped[state_date] = _build_learning_row(
            date_key=state_date,
            recorded_at=updated_at,
            source_event_type="state_snapshot",
            recent_closed_trades=_inum(learning.get("recent_closed_trades"), 0),
            gross_matched_trades=_inum(sample_filter.get("gross_matched_trades"), 0),
            eligible_matched_trades=_inum(sample_filter.get("eligible_matched_trades"), 0),
            blocked_trades=_inum(sample_filter.get("blocked_trades"), 0),
            eligible_rate_pct=_fnum(sample_filter.get("eligible_rate_pct"), 0.0),
            top_reason_1=_reason_label(top_reasons[0] if len(top_reasons) > 0 else {}),
            top_reason_2=_reason_label(top_reasons[1] if len(top_reasons) > 1 else {}),
            learning_status=str(learning.get("last_status", "")).strip(),
            learning_notes=str(learning.get("notes", "")).strip(),
            update_reason=str(learning.get("last_status", "")).strip(),
        )

    return [grouped[key] for key in sorted(grouped)]


def _connect_tdx() -> Optional["TdxHq_API"]:
    if TdxHq_API is None:
        return None
    api = TdxHq_API()
    for host, port in TDX_HOSTS:
        try:
            if api.connect(host, port):
                return api
        except Exception:
            continue
    return None


def _fetch_index_daily_rows(api: "TdxHq_API", market: int, code: str, max_bars: int = 240) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    fetched = 0
    batch_size = 200
    seen_dates = set()
    while fetched < max_bars:
        batch = api.get_index_bars(9, market, code, fetched, min(batch_size, max_bars - fetched))
        if not batch:
            break
        for item in batch:
            dt = _parse_datetime(str(item.get("datetime", "")))
            if dt is None:
                continue
            date_key = dt.strftime("%Y-%m-%d")
            if date_key in seen_dates:
                continue
            seen_dates.add(date_key)
            rows.append(
                {
                    "date": date_key,
                    "close": round(_fnum(item.get("close")), 4),
                    "open": round(_fnum(item.get("open")), 4),
                    "high": round(_fnum(item.get("high")), 4),
                    "low": round(_fnum(item.get("low")), 4),
                    "amount": round(_fnum(item.get("amount")), 2),
                    "vol": round(_fnum(item.get("vol")), 2),
                }
            )
        fetched += len(batch)
        if len(batch) < batch_size:
            break
    rows.sort(key=lambda r: r["date"])
    return rows


def build_benchmark_daily(nav_daily: List[Dict[str, object]]) -> List[Dict[str, object]]:
    strategy_rows = [row for row in nav_daily if int(row.get("is_valid_snapshot", 0)) == 1]
    strategy_dates = [str(row["date"]) for row in strategy_rows]
    if not strategy_dates:
        return []

    strategy_by_date = {str(row["date"]): row for row in strategy_rows}
    benchmark_data: Dict[str, Dict[str, Dict[str, object]]] = {}
    fetch_status = {key: "unavailable" for key in BENCHMARKS}

    api = _connect_tdx()
    if api is not None:
        try:
            for key, meta in BENCHMARKS.items():
                try:
                    rows = _fetch_index_daily_rows(api, meta["market"], meta["code"])
                except Exception:
                    rows = []
                if rows:
                    benchmark_data[key] = {str(row["date"]): row for row in rows}
                    fetch_status[key] = "ok"
        finally:
            try:
                api.disconnect()
            except Exception:
                pass

    base_assets = _fnum(strategy_rows[0].get("total_assets"), 1.0) or 1.0
    base_close: Dict[str, float] = {}
    for key in BENCHMARKS:
        if key not in benchmark_data:
            continue
        for date_key in strategy_dates:
            row = benchmark_data[key].get(date_key)
            if row and _fnum(row.get("close")) > 0:
                base_close[key] = _fnum(row.get("close"))
                break

    rows: List[Dict[str, object]] = []
    for date_key in strategy_dates:
        strategy = strategy_by_date[date_key]
        payload: Dict[str, object] = {
            "date": date_key,
            "strategy_total_assets": round(_fnum(strategy.get("total_assets")), 2),
            "strategy_nav_norm": round(_fnum(strategy.get("total_assets")) / base_assets, 6),
        }
        statuses = []
        for key, meta in BENCHMARKS.items():
            index_row = benchmark_data.get(key, {}).get(date_key)
            close_value = _fnum(index_row.get("close")) if index_row else 0.0
            payload[f"{key}_name"] = meta["name"]
            payload[f"{key}_close"] = round(close_value, 4) if close_value else ""
            if close_value and base_close.get(key):
                payload[f"{key}_norm"] = round(close_value / base_close[key], 6)
            else:
                payload[f"{key}_norm"] = ""
            statuses.append(f"{key}:{fetch_status.get(key, 'unavailable')}")
        payload["fetch_status"] = ";".join(statuses)
        rows.append(payload)
    return rows


def generate_curve_csvs(as_of_date: str = "") -> Dict[str, int]:
    _ensure_output_dir()
    nav_daily = build_nav_daily(as_of_date=as_of_date)
    realized_pnl_daily = build_realized_pnl_daily(nav_daily, as_of_date=as_of_date)
    trade_success_daily = build_trade_success_daily(as_of_date=as_of_date)
    benchmark_daily = build_benchmark_daily(nav_daily)
    learning_readiness_daily = build_learning_readiness_daily(as_of_date=as_of_date)

    _write_csv(
        CURVE_DATA_DIR / "curve_nav_daily.csv",
        [
            "date",
            "recorded_at",
            "tag",
            "total_assets",
            "avail_balance",
            "total_pos_value",
            "position_count",
            "holding_records",
            "closed_records",
            "realized_pnl",
            "floating_pnl",
            "win_rate_pct",
            "avg_return_pct",
            "is_valid_snapshot",
        ],
        nav_daily,
    )
    _write_csv(
        CURVE_DATA_DIR / "curve_realized_pnl_daily.csv",
        [
            "date",
            "closed_trade_count",
            "win_trade_count",
            "loss_trade_count",
            "daily_closed_pnl",
            "cumulative_closed_pnl",
            "avg_closed_return_pct",
            "nav_realized_pnl_snapshot",
            "nav_snapshot_tag",
            "nav_snapshot_recorded_at",
        ],
        realized_pnl_daily,
    )
    _write_csv(
        CURVE_DATA_DIR / "curve_trade_success_daily.csv",
        [
            "date",
            "total_requests",
            "success_count",
            "failure_count",
            "success_rate_pct",
            "buy_requests",
            "buy_success_count",
            "buy_failure_count",
            "buy_success_rate_pct",
            "sell_requests",
            "sell_success_count",
            "sell_failure_count",
            "sell_success_rate_pct",
            "rate_limit_112_count",
            "insufficient_501_count",
        ],
        trade_success_daily,
    )
    _write_csv(
        CURVE_DATA_DIR / "curve_benchmark_daily.csv",
        [
            "date",
            "strategy_total_assets",
            "strategy_nav_norm",
            "csi1000_name",
            "csi1000_close",
            "csi1000_norm",
            "chinext_name",
            "chinext_close",
            "chinext_norm",
            "fetch_status",
        ],
        benchmark_daily,
    )
    _write_csv(
        CURVE_DATA_DIR / "curve_learning_readiness_daily.csv",
        [
            "date",
            "recorded_at",
            "source_event_type",
            "recent_closed_trades",
            "gross_matched_trades",
            "eligible_matched_trades",
            "blocked_trades",
            "gross_match_rate_pct",
            "eligible_rate_pct",
            "clean_after_match_rate_pct",
            "top_block_reason_1",
            "top_block_reason_2",
            "learning_status",
            "learning_notes",
            "update_reason",
        ],
        learning_readiness_daily,
    )

    return {
        "curve_nav_daily": len(nav_daily),
        "curve_realized_pnl_daily": len(realized_pnl_daily),
        "curve_trade_success_daily": len(trade_success_daily),
        "curve_benchmark_daily": len(benchmark_daily),
        "curve_learning_readiness_daily": len(learning_readiness_daily),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate curve observatory CSVs")
    parser.add_argument(
        "--as-of-date",
        default="",
        help="Only include rows up to this trading date (YYYY-MM-DD). Empty means no clamp.",
    )
    args = parser.parse_args()
    counts = generate_curve_csvs(as_of_date=str(args.as_of_date).strip())
    if args.as_of_date:
        print(f"as_of_date: {args.as_of_date}")
    else:
        print(json.dumps(calendar_context(), ensure_ascii=False))
    for name, size in counts.items():
        print(f"{name}: {size}")


if __name__ == "__main__":
    main()
