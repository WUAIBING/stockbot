#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Workbuddy challenger 本地模拟下单脚本。

目标：
1. 不通过 mx-moni，下单/成交全部在本地账本中即时模拟；
2. 起始本金固定为 100 万，与 openclawd / 模拟账户起点保持一致；
3. 买入来源直接使用 arkclaw 当前主链产出的 workbuddy 候选池；
4. 每日输出独立的买卖订单日志、持仓快照、账户摘要、净值历史；
5. 保持字段风格与既有 workbuddy 账本一致，便于后续并行 A/B 观察。
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import v10_moni_trader as base
from market_resolver import build_today_exclusion_map, exclusion_reason_text, resolve_market_info
from package_paths import DATA_DIR
from position_sizer import SizerConfig, compute_position_weights
from trading_calendar import latest_workbuddy_source_trade_date
from workbuddy_runtime import (
    ARKCLAW_ROOT,
    OPENING_TRADABILITY_FILE,
    REFRESH_DISTILL_PIPELINE_SCRIPT,
    RuntimeValidationError,
    WORKBUDDY_CANDIDATE_POOL_FILE,
    WORKBUDDY_LOCAL_EXECUTION_STATE_FILE,
    WORKBUDDY_LOCAL_TRACK_RECORD_FILE,
    raise_on_inconsistent_challenger_state,
    validate_candidate_pool_artifact,
    validate_opening_tradability_artifact,
)


PORTFOLIO_NAME = "Workbuddy"
PORTFOLIO_TYPE = "local_challenger_paper_account"
INITIAL_CAPITAL = 1_000_000.0
SOURCE_FILE = WORKBUDDY_CANDIDATE_POOL_FILE

TRACK_FILE = WORKBUDDY_LOCAL_TRACK_RECORD_FILE
NAV_FILE = DATA_DIR / "workbuddy_local_nav_history.csv"
SUMMARY_FILE = DATA_DIR / "workbuddy_local_account_summary_latest.json"
ORDER_LOG_FILE = DATA_DIR / "workbuddy_local_order_log.jsonl"
BUY_PLAN_FILE = DATA_DIR / "workbuddy_local_buy_plan_latest.json"
BUY_OVERRIDE_FILE = DATA_DIR / "workbuddy_local_buy_override_latest.json"
POSITIONS_FILE = DATA_DIR / "workbuddy_local_positions_latest.json"
EXECUTION_STATE_FILE = WORKBUDDY_LOCAL_EXECUTION_STATE_FILE

# #region debug-point A:challenger-zero-pnl-server
_DEBUG_ROOT = Path(__file__).resolve().parents[2] / ".dbg"
_DEBUG_ENV_FILE = _DEBUG_ROOT / "challenger-zero-pnl.env"


def _debug_emit_event(hypothesis_id: str, location: str, msg: str, data: dict[str, Any]) -> None:
    url = "http://127.0.0.1:7777/event"
    session_id = "challenger-zero-pnl"
    try:
        content = _DEBUG_ENV_FILE.read_text(encoding="utf-8")
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if line.startswith("DEBUG_SERVER_URL="):
                url = line.split("=", 1)[1].strip() or url
            elif line.startswith("DEBUG_SESSION_ID="):
                session_id = line.split("=", 1)[1].strip() or session_id
    except Exception:
        pass
    payload = {
        "sessionId": session_id,
        "runId": "pre-fix",
        "hypothesisId": hypothesis_id,
        "location": location,
        "msg": msg,
        "data": data,
    }
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            ),
            timeout=0.8,
        ).read()
    except Exception:
        pass
# #endregion


BUY_WINDOW_CONFIGS = [
    {"key": "10:00", "label": "opening_probe", "minute": 10 * 60, "tolerance": 10, "entry_cap_ratio": 0.6, "allow_new": True},
    {"key": "10:30", "label": "continuity_confirm", "minute": 10 * 60 + 30, "tolerance": 10, "entry_cap_ratio": 0.4, "allow_new": True},
    {"key": "11:00", "label": "morning_attack", "minute": 11 * 60, "tolerance": 10, "entry_cap_ratio": 0.4, "allow_new": True},
    {"key": "13:30", "label": "afternoon_restart", "minute": 13 * 60 + 30, "tolerance": 10, "entry_cap_ratio": 0.3, "allow_new": True},
    {"key": "14:00", "label": "afternoon_confirm", "minute": 14 * 60, "tolerance": 10, "entry_cap_ratio": 0.2, "allow_new": True},
    {"key": "14:30", "label": "late_probe_only", "minute": 14 * 60 + 30, "tolerance": 10, "entry_cap_ratio": 0.2, "allow_new": True},
    {"key": "14:50", "label": "tail_probe_only", "minute": 14 * 60 + 50, "tolerance": 8, "entry_cap_ratio": 0.15, "allow_new": True},
]

SELL_WINDOW_CONFIGS = [
    {"key": "D1_0945", "trade_day_offset": 1, "minute": 9 * 60 + 45, "tolerance": 12},
    {"key": "D1_1030", "trade_day_offset": 1, "minute": 10 * 60 + 30, "tolerance": 12},
    {"key": "D1_1450", "trade_day_offset": 1, "minute": 14 * 60 + 50, "tolerance": 10},
    {"key": "D2_1030", "trade_day_offset": 2, "minute": 10 * 60 + 30, "tolerance": 12},
    {"key": "D2_1450", "trade_day_offset": 2, "minute": 14 * 60 + 50, "tolerance": 10},
]

BUY_WINDOW_MAP = {item["key"]: item for item in BUY_WINDOW_CONFIGS}
SELL_WINDOW_MAP = {item["key"]: item for item in SELL_WINDOW_CONFIGS}
BUY_WINDOW_ORDER = [item["key"] for item in BUY_WINDOW_CONFIGS]
SELL_WINDOW_ORDER = [item["key"] for item in SELL_WINDOW_CONFIGS]

EXIT_INTENT_FAST = "fast_realize"
EXIT_INTENT_BALANCED = "balanced_split"
EXIT_INTENT_RUNNER = "runner_candidate"


def _now() -> datetime:
    return datetime.now()


def _now_str() -> str:
    return _now().strftime("%Y-%m-%d %H:%M:%S")


def _today_str() -> str:
    return _now().strftime("%Y-%m-%d")


def _parse_hhmm(text: Any) -> tuple[int, int] | None:
    raw = str(text or "").strip()
    if not raw or ":" not in raw:
        return None
    try:
        hour, minute = raw.split(":", 1)
        return int(hour), int(minute)
    except Exception:
        return None


def _minute_of_day(text: Any) -> int | None:
    parsed = _parse_hhmm(text)
    if not parsed:
        return None
    return parsed[0] * 60 + parsed[1]


def _resolve_window(configs: list[dict[str, Any]], *, trigger_slot: str = "", now_dt: datetime | None = None) -> dict[str, Any] | None:
    now_dt = now_dt or _now()
    exact_key = str(trigger_slot or "").strip()
    if exact_key:
        if exact_key in BUY_WINDOW_MAP:
            return BUY_WINDOW_MAP[exact_key]
        if exact_key in SELL_WINDOW_MAP:
            return SELL_WINDOW_MAP[exact_key]
    minute_now = _minute_of_day(exact_key) if exact_key else now_dt.hour * 60 + now_dt.minute
    if minute_now is None:
        return None
    matched = None
    for item in configs:
        tolerance = int(item.get("tolerance", 10) or 10)
        if abs(minute_now - int(item.get("minute", -10_000))) <= tolerance:
            matched = item
    return matched


def _resolve_buy_window(*, trigger_slot: str = "", now_dt: datetime | None = None) -> dict[str, Any] | None:
    return _resolve_window(BUY_WINDOW_CONFIGS, trigger_slot=trigger_slot, now_dt=now_dt)


def _resolve_sell_window(*, trigger_slot: str = "", now_dt: datetime | None = None) -> dict[str, Any] | None:
    return _resolve_window(SELL_WINDOW_CONFIGS, trigger_slot=trigger_slot, now_dt=now_dt)


def _window_order_index(window_key: str, ordering: list[str]) -> int:
    try:
        return ordering.index(str(window_key or "").strip())
    except ValueError:
        return -1


def _default_execution_state() -> dict[str, Any]:
    return {
        "generated_at": "",
        "positions": {},
        "history": [],
    }


def _load_execution_state() -> dict[str, Any]:
    payload = _read_json(EXECUTION_STATE_FILE)
    if not isinstance(payload, dict):
        payload = {}
    positions = payload.get("positions", {})
    payload["positions"] = positions if isinstance(positions, dict) else {}
    history = payload.get("history", [])
    payload["history"] = history if isinstance(history, list) else []
    payload.setdefault("generated_at", "")
    return payload if payload else _default_execution_state()


def _save_execution_state(state: dict[str, Any]) -> None:
    payload = state if isinstance(state, dict) else _default_execution_state()
    payload["generated_at"] = _now_str()
    _write_json_atomic(EXECUTION_STATE_FILE, payload)


def _build_state_snapshot_from_record(record: dict[str, Any]) -> dict[str, Any]:
    item = _normalize_record(record)
    return {
        "last_known_status": str(item.get("status", "")).strip(),
        "last_known_quantity": _safe_int(item.get("quantity", 0), 0),
        "last_known_target_amount": round(_safe_float(item.get("target_amount", 0.0), 0.0), 2),
        "last_known_trade_date": str(item.get("date", "")).strip(),
    }


def _recover_execution_position_from_record(record: dict[str, Any]) -> dict[str, Any]:
    item = _normalize_record(record)
    target_amount = _safe_float(item.get("target_amount", 0.0), 0.0)
    buy_amount = _safe_float(item.get("buy_amount", 0.0), 0.0)
    built_ratio = round(min(1.0, buy_amount / target_amount), 4) if target_amount > 0 else 1.0
    code = _normalize_code(item.get("code", ""))
    return {
        "code": code,
        "name": str(item.get("name", "")).strip(),
        "trade_date": str(item.get("date", "")).strip(),
        "entry_window": "auto_recovered",
        "last_buy_window": "auto_recovered",
        "entry_intent": EXIT_INTENT_FAST,
        "built_ratio": built_ratio,
        "target_weight_pct": 0.0,
        "readiness_score": 0.0,
        "readiness_components": {},
        "quick_take_done": False,
        "quick_take_trigger_pct": 0.0,
        "quick_take_ratio": 0.0,
        "quick_take_window": "",
        "final_exit_window": "D1_0945",
        "runner_day2_window": "",
        "runner_day2_final_window": "",
        "runner_allowed": False,
        "runner_active": False,
        "last_action": "auto_recovered_from_track_record",
        "last_action_at": _now_str(),
        "recovered_from_track_record": True,
    }


def _prune_execution_state(state: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    holding_map: dict[str, dict[str, Any]] = {}
    for item in records:
        norm_item = _normalize_record(item)
        code = _normalize_code(norm_item.get("code", ""))
        if code and str(norm_item.get("status", "")).strip() == "holding":
            holding_map[code] = norm_item
    active_codes = {
        _normalize_code(item.get("code", ""))
        for item in records
        if str(item.get("status", "")).strip() == "holding" and _normalize_code(item.get("code", ""))
    }
    positions = state.get("positions", {}) if isinstance(state, dict) else {}
    clean_positions: dict[str, dict[str, Any]] = {}
    for code in sorted(active_codes):
        existing = positions.get(code, {}) if isinstance(positions.get(code, {}), dict) else {}
        merged = {
            **(_recover_execution_position_from_record(holding_map[code]) if not existing else existing),
            **_build_state_snapshot_from_record(holding_map[code]),
        }
        clean_positions[code] = merged
    history = state.get("history", []) if isinstance(state, dict) else []
    state = {
        "generated_at": _now_str(),
        "positions": clean_positions,
        "history": history[-200:] if isinstance(history, list) else [],
    }
    return state


def _append_execution_history(state: dict[str, Any], payload: dict[str, Any]) -> None:
    history = state.get("history", [])
    if not isinstance(history, list):
        history = []
    history.append(payload)
    state["history"] = history[-200:]


def _safe_pct(numerator: float, denominator: float, *, default: float = 0.0) -> float:
    if denominator <= 0:
        return default
    return (numerator / denominator - 1.0) * 100.0


def _quote_today_chg_pct(quote: dict[str, Any] | None) -> float:
    quote = quote or {}
    price = _quote_price(quote)
    last_close = _safe_float(quote.get("last_close", 0.0), 0.0)
    return round(_safe_pct(price, last_close), 4) if price > 0 and last_close > 0 else 0.0


def _current_chg_pct_for_readiness(row: dict[str, Any], quote: dict[str, Any] | None) -> float:
    quote = quote or {}
    latest_chg_pct = _safe_float((row or {}).get("latest_chg_pct", 0.0), 0.0)
    live_price = _quote_execution_price(quote)
    last_close = _safe_float(quote.get("last_close", 0.0), 0.0)
    if live_price > 0 and last_close > 0:
        return _quote_today_chg_pct(quote)
    return latest_chg_pct


def _recent_closed_same_code(records: list[dict[str, Any]], code: str, *, lookback_days: int = 2) -> dict[str, Any] | None:
    norm_code = _normalize_code(code)
    latest: dict[str, Any] | None = None
    latest_dt: datetime | None = None
    for row in records:
        item = _normalize_record(row)
        if _normalize_code(item.get("code", "")) != norm_code:
            continue
        if str(item.get("status", "")).strip() != "closed":
            continue
        sell_dt = None
        for value in (
            f"{str(item.get('sell_date', '')).strip()} {str(item.get('sell_time', '')).strip()}".strip(),
            str(item.get("sell_date", "")).strip(),
        ):
            if not value:
                continue
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    sell_dt = datetime.strptime(value, fmt)
                    break
                except Exception:
                    continue
            if sell_dt:
                break
        if not sell_dt:
            continue
        if (_now() - sell_dt) > timedelta(days=lookback_days):
            continue
        if latest_dt is None or sell_dt > latest_dt:
            latest_dt = sell_dt
            latest = item
    return latest


def _intraday_position_ratio(quote: dict[str, Any] | None) -> float:
    quote = quote or {}
    high = _safe_float(quote.get("high", 0.0), 0.0)
    low = _safe_float(quote.get("low", 0.0), 0.0)
    price = _quote_price(quote)
    if high <= low or price <= 0:
        return 0.5
    return max(0.0, min(1.0, (price - low) / (high - low)))


def _profitability_priority_value(row: dict[str, Any] | None) -> float:
    row = row if isinstance(row, dict) else {}
    return _safe_float(
        row.get("avg_profitability_priority", row.get("profit_priority_score", 0.0)),
        0.0,
    )


def _execution_candidate_priority_key(row: dict[str, Any] | None) -> tuple[Any, ...]:
    row = row if isinstance(row, dict) else {}
    selection_rank = _safe_int(row.get("selection_rank", 0), 0)
    return (
        -_profitability_priority_value(row),
        -_safe_float(row.get("avg_candidate_avg_return", 0.0), 0.0),
        -_safe_float(row.get("avg_candidate_win_rate", 0.0), 0.0),
        selection_rank if selection_rank > 0 else 999999,
        -_safe_float(row.get("selection_score", 0.0), 0.0),
        _normalize_code(row.get("code", "")),
    )


def _build_execution_readiness(
    row: dict[str, Any],
    quote: dict[str, Any] | None,
    *,
    window_key: str,
    recent_closed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    quote = quote or {}
    selection_rank = _safe_int(row.get("selection_rank", 0), 0)
    profitability_priority = _profitability_priority_value(row)
    avg_candidate_win_rate = _safe_float(row.get("avg_candidate_win_rate", 0.0), 0.0)
    avg_candidate_avg_return = _safe_float(row.get("avg_candidate_avg_return", 0.0), 0.0)
    selection_score = _safe_float(row.get("selection_score", 0.0), 0.0)
    heat_level = str(row.get("heat_level", "")).strip().lower()
    guardrail_status = str(row.get("guardrail_status", "")).strip().lower()
    latest_chg_pct = _safe_float(row.get("latest_chg_pct", 0.0), 0.0)
    current_chg_pct = _current_chg_pct_for_readiness(row, quote)
    price = _quote_price(quote)
    open_price = _safe_float(quote.get("open", 0.0), 0.0)
    high_price = _safe_float(quote.get("high", 0.0), 0.0)
    low_price = _safe_float(quote.get("low", 0.0), 0.0)
    pullback_pct = round((high_price / price - 1.0) * 100, 2) if high_price > 0 and price > 0 else 0.0
    pos_ratio = _intraday_position_ratio(quote)

    continuity = 18.0
    if selection_rank == 1:
        continuity += 8.0
    elif selection_rank == 2:
        continuity += 6.0
    elif selection_rank <= 4:
        continuity += 4.0
    if current_chg_pct >= 4.0:
        continuity += 8.0
    elif current_chg_pct >= 2.0:
        continuity += 5.0
    elif current_chg_pct <= -1.0:
        continuity -= 10.0

    profit_conviction = 0.0
    if profitability_priority >= 110:
        profit_conviction += 18.0
    elif profitability_priority >= 100:
        profit_conviction += 14.0
    elif profitability_priority >= 90:
        profit_conviction += 10.0
    elif profitability_priority >= 80:
        profit_conviction += 6.0
    if avg_candidate_win_rate >= 0.58:
        profit_conviction += 8.0
    elif avg_candidate_win_rate >= 0.52:
        profit_conviction += 5.0
    elif avg_candidate_win_rate <= 0.45:
        profit_conviction -= 6.0
    if avg_candidate_avg_return >= 2.3:
        profit_conviction += 8.0
    elif avg_candidate_avg_return >= 1.8:
        profit_conviction += 5.0
    elif avg_candidate_avg_return <= 0.6:
        profit_conviction -= 6.0
    if selection_score >= 95.0:
        profit_conviction += 3.0

    pullback_quality = 10.0
    if 0.8 <= pullback_pct <= 4.0:
        pullback_quality += 10.0
    elif pullback_pct < 0.5:
        pullback_quality -= 4.0
    elif pullback_pct > 6.0:
        pullback_quality -= 8.0
    if open_price > 0 and price >= open_price:
        pullback_quality += 4.0
    elif open_price > 0 and price < open_price:
        pullback_quality -= 6.0

    liquidity_score = 12.0
    if high_price > 0 and low_price > 0 and (high_price / max(low_price, 0.01) - 1.0) * 100 >= 4.0:
        liquidity_score += 4.0
    if price > 0 and low_price > 0 and price <= low_price * 1.01:
        liquidity_score -= 10.0

    heat_penalty = 0.0
    if heat_level == "extreme":
        heat_penalty += 18.0
    elif heat_level == "warm":
        heat_penalty += 8.0
    if guardrail_status == "hot_primary":
        heat_penalty += 8.0
    if latest_chg_pct >= 19.5:
        heat_penalty += 6.0
    if pos_ratio >= 0.9:
        heat_penalty += 4.0

    reentry_penalty = 0.0
    if recent_closed:
        last_pnl_pct = _safe_float(recent_closed.get("pnl_pct", 0.0), 0.0)
        if last_pnl_pct <= 0:
            reentry_penalty += 10.0
        else:
            reentry_penalty += 4.0

    window_bias = 0.0
    if window_key == "10:00":
        window_bias += 6.0
    elif window_key == "10:30":
        window_bias += 5.0
    elif window_key == "11:00":
        window_bias += 4.0
    elif window_key == "13:30":
        window_bias += 1.0
    elif window_key == "14:00":
        window_bias -= 2.0
    elif window_key == "14:30":
        window_bias -= 8.0
    elif window_key == "14:50":
        window_bias -= 12.0

    score = profit_conviction + continuity + pullback_quality + liquidity_score + window_bias - heat_penalty - reentry_penalty
    score = max(0.0, min(100.0, round(score, 2)))
    return {
        "score": score,
        "components": {
            "profit_conviction": round(profit_conviction, 2),
            "continuity": round(continuity, 2),
            "pullback_quality": round(pullback_quality, 2),
            "liquidity": round(liquidity_score, 2),
            "window_bias": round(window_bias, 2),
            "heat_penalty": round(heat_penalty, 2),
            "reentry_penalty": round(reentry_penalty, 2),
        },
        "profitability_priority": round(profitability_priority, 4),
        "avg_candidate_win_rate": round(avg_candidate_win_rate, 4),
        "avg_candidate_avg_return": round(avg_candidate_avg_return, 4),
        "selection_score": round(selection_score, 4),
        "heat_level": heat_level,
        "guardrail_status": guardrail_status,
        "latest_chg_pct": round(latest_chg_pct, 4),
        "current_chg_pct": round(current_chg_pct, 4),
        "pullback_pct": round(pullback_pct, 2),
        "intraday_position_ratio": round(pos_ratio, 4),
    }


def _resolve_entry_action(window_key: str, readiness: dict[str, Any], *, existing_ratio: float = 0.0) -> dict[str, Any]:
    score = _safe_float(readiness.get("score", 0.0), 0.0)
    profitability_priority = _safe_float(readiness.get("profitability_priority", 0.0), 0.0)
    avg_candidate_win_rate = _safe_float(readiness.get("avg_candidate_win_rate", 0.0), 0.0)
    avg_candidate_avg_return = _safe_float(readiness.get("avg_candidate_avg_return", 0.0), 0.0)
    heat_level = str(readiness.get("heat_level", "")).strip().lower()
    guardrail_status = str(readiness.get("guardrail_status", "")).strip().lower()
    window_profile = BUY_WINDOW_MAP.get(window_key, {})
    entry_cap_ratio = _safe_float(window_profile.get("entry_cap_ratio", 0.0), 0.0)
    if entry_cap_ratio <= 0:
        return {"action": "skip", "target_build_ratio": round(existing_ratio, 4), "buy_ratio": 0.0, "reason": "window_not_supported"}

    max_total_ratio = 1.0 if window_key in {"10:30", "11:00", "13:30", "14:00"} else min(1.0, max(entry_cap_ratio, 0.3))
    if window_key in {"14:30", "14:50"}:
        max_total_ratio = min(max_total_ratio, 0.3)
    if heat_level == "extreme" and window_key in {"14:30", "14:50"}:
        max_total_ratio = min(max_total_ratio, 0.2)

    target_ratio = existing_ratio
    action = "skip"
    reason = "readiness_below_threshold"
    if existing_ratio <= 0:
        if profitability_priority >= 100 and avg_candidate_avg_return >= 2.0 and score >= 68:
            target_ratio = min(max_total_ratio, max(entry_cap_ratio, 0.6 if window_key in {"10:00", "10:30", "11:00"} else 0.4))
            action = "alpha_core_buy"
            reason = "profit_priority_core_entry"
        elif score >= 74:
            target_ratio = min(max_total_ratio, entry_cap_ratio if entry_cap_ratio > 0 else 0.6)
            target_ratio = max(target_ratio, 0.6 if window_key in {"10:00", "10:30", "11:00"} else target_ratio)
            action = "core_buy"
            reason = "high_readiness_core_entry"
        elif (profitability_priority >= 88 or avg_candidate_win_rate >= 0.52 or avg_candidate_avg_return >= 1.6) and score >= 58:
            target_ratio = min(max_total_ratio, 0.3 if window_key in {"10:00", "10:30", "11:00"} else 0.2)
            action = "alpha_probe_buy"
            reason = "profit_priority_probe_entry"
        elif score >= 62:
            target_ratio = min(max_total_ratio, 0.3)
            action = "probe_buy"
            reason = "medium_readiness_probe_entry"
    else:
        last_allow = existing_ratio < 0.999
        if last_allow and profitability_priority >= 95 and avg_candidate_avg_return >= 1.8 and score >= 66 and _window_order_index(window_key, BUY_WINDOW_ORDER) > 0:
            target_ratio = min(1.0, max(existing_ratio, 1.0))
            action = "alpha_add_buy"
            reason = "profit_priority_confirm_add"
        elif last_allow and score >= 70 and _window_order_index(window_key, BUY_WINDOW_ORDER) > 0:
            target_ratio = min(1.0, max(existing_ratio, 1.0))
            action = "add_buy"
            reason = "continuity_confirm_add"
        elif score >= 64 and existing_ratio < 0.6 and _window_order_index(window_key, BUY_WINDOW_ORDER) > 0:
            target_ratio = min(0.6, max_total_ratio)
            action = "add_buy"
            reason = "probe_upgrade_add"

    if action == "skip":
        return {"action": action, "target_build_ratio": round(existing_ratio, 4), "buy_ratio": 0.0, "reason": reason}

    if heat_level == "extreme" and guardrail_status == "hot_primary" and window_key in {"10:00", "10:30", "11:00"}:
        target_ratio = min(target_ratio, 0.6)
    if window_key in {"14:30", "14:50"}:
        target_ratio = min(target_ratio, 0.3)
    buy_ratio = round(max(0.0, target_ratio - existing_ratio), 4)
    if buy_ratio <= 0.009:
        return {"action": "skip", "target_build_ratio": round(existing_ratio, 4), "buy_ratio": 0.0, "reason": "already_fully_built"}
    return {
        "action": action,
        "target_build_ratio": round(target_ratio, 4),
        "buy_ratio": buy_ratio,
        "reason": reason,
    }


def _classify_exit_intent(row: dict[str, Any], readiness: dict[str, Any], *, window_key: str) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    score = _safe_float(readiness.get("score", 0.0), 0.0)
    heat_level = str(readiness.get("heat_level", "")).strip().lower()
    profitability_priority = max(
        _safe_float(readiness.get("profitability_priority", 0.0), 0.0),
        _profitability_priority_value(row),
    )
    avg_candidate_win_rate = max(
        _safe_float(readiness.get("avg_candidate_win_rate", 0.0), 0.0),
        _safe_float(row.get("avg_candidate_win_rate", 0.0), 0.0),
    )
    avg_candidate_avg_return = max(
        _safe_float(readiness.get("avg_candidate_avg_return", 0.0), 0.0),
        _safe_float(row.get("avg_candidate_avg_return", 0.0), 0.0),
    )
    if (
        score >= 74
        and heat_level in {"warm", "normal", ""}
        and window_key in {"10:00", "10:30", "11:00"}
        and (
            profitability_priority >= 100
            or (avg_candidate_win_rate >= 0.58 and avg_candidate_avg_return >= 1.8)
            or avg_candidate_avg_return >= 2.3
        )
    ):
        return {
            "intent": EXIT_INTENT_RUNNER,
            "quick_take_trigger_pct": 3.0,
            "quick_take_ratio": 0.5,
            "quick_take_window": "D1_1030",
            "final_exit_window": "D1_1450",
            "runner_day2_window": "D2_1030",
            "runner_day2_final_window": "D2_1450",
        }
    if (
        score >= 64
        and window_key in {"10:00", "10:30", "11:00"}
        and (
            profitability_priority >= 88
            or avg_candidate_win_rate >= 0.52
            or avg_candidate_avg_return >= 1.5
        )
    ):
        return {
            "intent": EXIT_INTENT_BALANCED,
            "quick_take_trigger_pct": 2.0,
            "quick_take_ratio": 0.7,
            "quick_take_window": "D1_1030",
            "final_exit_window": "D1_1450",
            "runner_day2_window": "",
            "runner_day2_final_window": "",
        }
    return {
        "intent": EXIT_INTENT_FAST,
        "quick_take_trigger_pct": 0.0,
        "quick_take_ratio": 0.0,
        "quick_take_window": "",
        "final_exit_window": "D1_0945",
        "runner_day2_window": "",
        "runner_day2_final_window": "",
    }


def _holding_code_map(records: list[dict[str, Any]]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx in range(len(records) - 1, -1, -1):
        row = _normalize_record(records[idx])
        if str(row.get("status", "")).strip() != "holding":
            continue
        code = _normalize_code(row.get("code", ""))
        if code and code not in mapping:
            mapping[code] = idx
    return mapping


def _calc_lot_quantity_from_ratio(total_qty: int, ratio: float) -> int:
    total_qty = _safe_int(total_qty, 0)
    if total_qty <= 0:
        return 0
    raw_qty = int(total_qty * max(0.0, min(ratio, 1.0)))
    qty = (raw_qty // 100) * 100
    if qty <= 0 and total_qty >= 100:
        qty = 100
    if qty >= total_qty:
        qty = total_qty
    remain = total_qty - qty
    if 0 < remain < 100 and total_qty > 100:
        qty = max(100, total_qty - 100)
    return min(total_qty, (qty // 100) * 100 if qty < total_qty else qty)


def _build_exit_context(entry_state: dict[str, Any], quote: dict[str, Any] | None, *, hold_days: int, pnl_pct: float) -> dict[str, Any]:
    entry_state = entry_state if isinstance(entry_state, dict) else {}
    quote = quote or {}
    intent = str(entry_state.get("entry_intent", EXIT_INTENT_FAST)).strip() or EXIT_INTENT_FAST
    price = _quote_price(quote)
    open_price = _safe_float(quote.get("open", 0.0), 0.0)
    high_price = _safe_float(quote.get("high", 0.0), 0.0)
    weakness = False
    weakness_reasons: list[str] = []
    if hold_days >= 5:
        weakness = True
        weakness_reasons.append(f"T+5到期(持仓{hold_days}天)")
    if price > 0 and open_price > 0 and price < open_price * 0.992:
        weakness = True
        weakness_reasons.append("跌破日内开盘承接")
    if price > 0 and high_price > 0 and price <= high_price * 0.97:
        weakness = True
        weakness_reasons.append("冲高回落幅度偏大")
    if pnl_pct <= -3.5:
        weakness = True
        weakness_reasons.append("浮亏扩大")
    return {
        "intent": intent,
        "weakness": weakness,
        "weakness_reason": " / ".join(weakness_reasons),
        "quick_take_done": bool(entry_state.get("quick_take_done", False)),
        "quick_take_trigger_pct": _safe_float(entry_state.get("quick_take_trigger_pct", 0.0), 0.0),
        "quick_take_ratio": _safe_float(entry_state.get("quick_take_ratio", 0.0), 0.0),
        "quick_take_window": str(entry_state.get("quick_take_window", "")).strip(),
        "final_exit_window": str(entry_state.get("final_exit_window", "")).strip(),
        "runner_day2_window": str(entry_state.get("runner_day2_window", "")).strip(),
        "runner_day2_final_window": str(entry_state.get("runner_day2_final_window", "")).strip(),
        "runner_allowed": bool(entry_state.get("runner_allowed", False)),
        "runner_active": bool(entry_state.get("runner_active", False)),
    }


def _resolve_exit_action(
    entry_state: dict[str, Any],
    *,
    sell_window_key: str,
    hold_days: int,
    quantity: int,
    pnl_pct: float,
    quote: dict[str, Any] | None,
    smart_should_sell: bool = False,
    smart_reason: str = "",
) -> dict[str, Any]:
    ctx = _build_exit_context(entry_state, quote, hold_days=hold_days, pnl_pct=pnl_pct)
    if hold_days >= 5:
        return {"action": "sell_all", "quantity": quantity, "reason": ctx["weakness_reason"] or "T+5到期"}

    if smart_should_sell and hold_days >= 1:
        return {"action": "sell_all", "quantity": quantity, "reason": f"信号衰减[{smart_reason}]"}

    if hold_days <= 0:
        return {"action": "hold", "quantity": 0, "reason": "T+1未到"}

    if ctx["intent"] == EXIT_INTENT_FAST:
        if sell_window_key == "D1_0945":
            return {"action": "sell_all", "quantity": quantity, "reason": "fast_realize 次日09:45兑现"}
        if sell_window_key in {"D1_1030", "D1_1450", "D2_1030", "D2_1450"}:
            reason = ctx["weakness_reason"] or "fast_realize 错过主卖点后兜底兑现"
            return {"action": "sell_all", "quantity": quantity, "reason": reason}
        return {"action": "hold", "quantity": 0, "reason": "等待 fast_realize 主卖点"}

    if sell_window_key == ctx["quick_take_window"] and not ctx["quick_take_done"]:
        if pnl_pct >= ctx["quick_take_trigger_pct"] > 0:
            sell_qty = _calc_lot_quantity_from_ratio(quantity, ctx["quick_take_ratio"])
            if 0 < sell_qty < quantity:
                return {
                    "action": "sell_partial",
                    "quantity": sell_qty,
                    "reason": (
                        f"{ctx['intent']} 达到+{ctx['quick_take_trigger_pct']:.1f}% "
                        f"先锁定{int(ctx['quick_take_ratio'] * 100)}%"
                    ),
                }
        if ctx["weakness"]:
            return {"action": "sell_all", "quantity": quantity, "reason": ctx["weakness_reason"] or "quick_take_window 走弱"}
        return {"action": "hold", "quantity": 0, "reason": "快锁窗口未触发"}

    if sell_window_key == ctx["final_exit_window"]:
        if ctx["intent"] == EXIT_INTENT_RUNNER and ctx["quick_take_done"] and ctx["runner_allowed"] and pnl_pct > 0 and not ctx["weakness"]:
            return {"action": "hold_runner", "quantity": 0, "reason": "runner_candidate 允许隔夜奔跑"}
        return {"action": "sell_all", "quantity": quantity, "reason": f"{ctx['intent']} D1最终兑现"}

    if sell_window_key == ctx["runner_day2_window"]:
        if ctx["runner_active"] and pnl_pct > 0 and not ctx["weakness"]:
            return {"action": "hold", "quantity": 0, "reason": "runner D2 上午继续观察"}
        return {"action": "sell_all", "quantity": quantity, "reason": "runner D2 上午兑现"}

    if sell_window_key == ctx["runner_day2_final_window"]:
        return {"action": "sell_all", "quantity": quantity, "reason": "runner D2 最终兑现"}

    if ctx["weakness"] and sell_window_key in {"D1_1030", "D1_1450", "D2_1030", "D2_1450"}:
        return {"action": "sell_all", "quantity": quantity, "reason": ctx["weakness_reason"] or "持仓转弱"}
    return {"action": "hold", "quantity": 0, "reason": "窗口未触发卖出"}


def _expected_source_trade_date(now_dt: datetime | None = None) -> str:
    return latest_workbuddy_source_trade_date(now_dt or _now()).isoformat()


def _read_json(path: Path) -> Any:
    if not path.exists():
        return {}
    for encoding in ("utf-8", "utf-8-sig"):
        try:
            with path.open("r", encoding=encoding) as f:
                return json.load(f)
        except Exception:
            continue
    return {}


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _load_buy_override() -> dict[str, Any]:
    payload = _read_json(BUY_OVERRIDE_FILE)
    return payload if isinstance(payload, dict) else {}


def _match_buy_override(*, trade_date: str, window_key: str) -> dict[str, Any] | None:
    payload = _load_buy_override()
    if not payload:
        return None
    status = str(payload.get("status", "")).strip().lower()
    if status not in {"scheduled", "active"}:
        return None
    if str(payload.get("effective_trade_date", "")).strip() != str(trade_date).strip():
        return None
    planned_window = str(payload.get("execution_window_key", payload.get("trigger_slot", ""))).strip()
    if planned_window and planned_window != str(window_key).strip():
        return None
    records = payload.get("records", [])
    if not isinstance(records, list) or not records:
        return None
    return payload


def _consume_matching_buy_override(*, trade_date: str, window_key: str, buy_count: int) -> None:
    if buy_count <= 0:
        return
    payload = _match_buy_override(trade_date=trade_date, window_key=window_key)
    if not payload:
        return
    payload["status"] = "consumed"
    payload["consumed_at"] = _now_str()
    payload["consumed_buy_count"] = int(buy_count)
    payload["consumed_window_key"] = str(window_key).strip()
    _write_json_atomic(BUY_OVERRIDE_FILE, payload)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _write_csv_row(path: Path, fieldnames: list[str], row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def _normalize_code(code: Any) -> str:
    text = str(code or "").strip()
    if "." in text:
        text = text.split(".", 1)[0]
    return text.zfill(6)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", ""))
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).replace(",", "")))
    except Exception:
        return default


def _normalize_security_name(name: Any) -> str:
    return str(name or "").strip().replace(" ", "").upper()


def _is_risk_warning_name(name: Any) -> bool:
    text = _normalize_security_name(name)
    if not text:
        return False
    prefixes = ("ST", "*ST", "S*ST", "SST")
    return any(text.startswith(prefix) for prefix in prefixes)


def _is_risk_warning_candidate(row: dict[str, Any]) -> bool:
    if _is_risk_warning_name(row.get("name")):
        return True
    truthy_values = {"1", "true", "yes", "y"}
    for field in ("risk_warning", "special_treatment", "is_st"):
        if str(row.get(field, "")).strip().lower() in truthy_values:
            return True
    return False


def _split_order_ids(raw: Any) -> list[str]:
    return [item.strip() for item in str(raw or "").split("|") if item.strip()]


def _join_order_ids(items: list[str]) -> str:
    seen: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.append(text)
    return "|".join(seen)


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    return base._normalize_record(record)


def _load_source_payload() -> dict[str, Any]:
    payload = _read_json(SOURCE_FILE)
    return payload if isinstance(payload, dict) else {}


def _refresh_source_payload(expected_trade_date: str) -> tuple[dict[str, Any], str]:
    if not REFRESH_DISTILL_PIPELINE_SCRIPT.exists():
        raise RuntimeError(f"refresh_distill_pipeline.py 不存在: {REFRESH_DISTILL_PIPELINE_SCRIPT}")
    cmd = [
        sys.executable,
        str(REFRESH_DISTILL_PIPELINE_SCRIPT),
        "--trade-date",
        expected_trade_date,
    ]
    result = subprocess.run(
        cmd,
        cwd=ARKCLAW_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )
    detail = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        raise RuntimeError(
            f"刷新候选池失败，exit={result.returncode}, trade_date={expected_trade_date}, detail={detail[:500]}"
        )
    return _load_source_payload(), detail[:500]


def _ensure_fresh_source_payload() -> dict[str, Any]:
    expected_trade_date = _expected_source_trade_date()
    payload = _load_source_payload()
    current_trade_date = str(payload.get("trade_date", payload.get("source_trade_date", ""))).strip()
    current_status = str(payload.get("status", "")).strip()
    if current_status == "ok" and current_trade_date >= expected_trade_date:
        validate_candidate_pool_artifact(path=SOURCE_FILE, expected_trade_date=expected_trade_date)
        return payload

    payload, refresh_detail = _refresh_source_payload(expected_trade_date)
    refreshed_trade_date = str(payload.get("trade_date", payload.get("source_trade_date", ""))).strip()
    refreshed_status = str(payload.get("status", "")).strip()
    if refreshed_status != "ok" or refreshed_trade_date < expected_trade_date:
        raise RuntimeError(
            "候选池刷新后仍未达到可接受交易日: "
            f"expected={expected_trade_date}, actual={refreshed_trade_date}, "
            f"status={refreshed_status}, detail={refresh_detail}"
        )
    validate_candidate_pool_artifact(path=SOURCE_FILE, expected_trade_date=expected_trade_date)
    return payload


def _load_today_tradability_exclusions() -> dict[str, dict[str, Any]]:
    payload = _read_json(OPENING_TRADABILITY_FILE)
    return build_today_exclusion_map(payload if isinstance(payload, dict) else {})


def load_track_record() -> list[dict[str, Any]]:
    if not TRACK_FILE.exists():
        return []
    records: list[dict[str, Any]] = []
    with TRACK_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            records.append(_normalize_record(row))
    return records


def save_track_record(records: list[dict[str, Any]]) -> None:
    TRACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with TRACK_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=base.TRACK_FIELDNAMES)
        writer.writeheader()
        for row in records:
            writer.writerow(_normalize_record(row))


def _persist_local_state(records: list[dict[str, Any]], execution_state: dict[str, Any], *, context: str) -> dict[str, Any]:
    normalized_records = [_normalize_record(row) for row in records]
    normalized_state = _prune_execution_state(execution_state, normalized_records)
    consistency = raise_on_inconsistent_challenger_state(
        normalized_records,
        normalized_state,
        context=context,
    )
    save_track_record(normalized_records)
    _save_execution_state(normalized_state)
    return consistency


def _find_record_index(records: list[dict[str, Any]], code: str, statuses: tuple[str, ...] = ("holding", "paused")) -> int | None:
    norm_code = _normalize_code(code)
    for idx in range(len(records) - 1, -1, -1):
        item = _normalize_record(records[idx])
        if _normalize_code(item.get("code", "")) == norm_code and str(item.get("status", "")).strip() in statuses:
            return idx
    return None


def _quote_batches(api: Any, codes: list[str]) -> dict[str, dict[str, Any]]:
    grouped: dict[int, list[str]] = defaultdict(list)
    for code in codes:
        info = resolve_market_info(code)
        market = info.get("market_tdx")
        if market in (0, 1) and info.get("tradable_by_current_executor"):
            grouped[int(market)].append(_normalize_code(code))

    quote_map: dict[str, dict[str, Any]] = {}
    for market, items in grouped.items():
        for start in range(0, len(items), 80):
            batch = [(market, code) for code in items[start:start + 80]]
            try:
                quotes = api.get_security_quotes(batch)
            except Exception:
                quotes = None
            if not quotes:
                continue
            for quote in quotes:
                code = _normalize_code((quote or {}).get("code", ""))
                if code:
                    quote_map[code] = dict(quote)
    return quote_map


def load_quote_map(codes: list[str]) -> dict[str, dict[str, Any]]:
    code_list = sorted({_normalize_code(code) for code in codes if str(code).strip()})
    if not code_list:
        return {}
    api = base.connect_tdx()
    if api is None:
        return {}
    try:
        return _quote_batches(api, code_list)
    finally:
        try:
            api.disconnect()
        except Exception:
            pass


def _quote_price(quote: dict[str, Any] | None) -> float:
    quote = quote or {}
    last_price = _safe_float(quote.get("price", 0.0), 0.0)
    last_close = _safe_float(quote.get("last_close", 0.0), 0.0)
    return last_price if last_price > 0 else last_close


def _quote_execution_price(quote: dict[str, Any] | None) -> float:
    quote = quote or {}
    return _safe_float(quote.get("price", 0.0), 0.0)


def _workbuddy_tier(selection_rank: int) -> int:
    if selection_rank <= 2:
        return 1
    if selection_rank <= 4:
        return 2
    return 3


def _generate_local_order_id(action: str, code: str) -> str:
    return f"WBLOC-{action.upper()}-{_now().strftime('%Y%m%d%H%M%S')}-{_normalize_code(code)}"


def _empty_record(*, code: str, name: str, tier: int, mode: str, build_note: str, target_amount: float, trade_date: str) -> dict[str, Any]:
    return _normalize_record(
        {
            "date": trade_date,
            "buy_time": "",
            "code": _normalize_code(code),
            "name": name,
            "tier": str(tier),
            "entry_price": "0",
            "quantity": "0",
            "buy_amount": "0",
            "buy_order_ids": "",
            "sell_date": "",
            "sell_time": "",
            "sell_price": "",
            "sell_order_id": "",
            "pnl": "",
            "pnl_pct": "",
            "hold_days": "",
            "status": "holding",
            "mode": mode,
            "build_note": build_note,
            "target_amount": f"{target_amount:.0f}" if target_amount > 0 else "",
            "close_reason": "",
            "last_synced_at": _now_str(),
        }
    )


def _holding_rows(
    records: list[dict[str, Any]],
    quote_map: dict[str, dict[str, Any]],
    *,
    execution_positions: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    execution_positions = execution_positions or {}
    rows: list[dict[str, Any]] = []
    for record in records:
        if str(record.get("status", "")).strip() != "holding":
            continue
        code = _normalize_code(record.get("code", ""))
        market_info = resolve_market_info(code)
        entry_price = _safe_float(record.get("entry_price", 0.0), 0.0)
        quantity = _safe_int(record.get("quantity", 0), 0)
        quote = quote_map.get(code) or {}
        live_price = _quote_execution_price(quote)
        last_close = _safe_float(quote.get("last_close", 0.0), 0.0)
        if live_price > 0:
            current_price = live_price
            current_price_source = "live_quote"
            price_is_estimated = False
        elif last_close > 0:
            current_price = last_close
            current_price_source = "last_close_fallback"
            price_is_estimated = True
        else:
            current_price = entry_price
            current_price_source = "entry_fallback"
            price_is_estimated = True
        market_value = round(current_price * quantity, 2) if current_price > 0 else 0.0
        cost_value = round(entry_price * quantity, 2)
        floating_pnl = round(market_value - cost_value, 2)
        floating_pnl_pct = round((current_price / entry_price - 1) * 100, 2) if current_price > 0 and entry_price > 0 else 0.0
        try:
            hold_days = (_now() - datetime.strptime(str(record.get("date", "")), "%Y-%m-%d")).days
        except Exception:
            hold_days = 0
        execution_meta = execution_positions.get(code, {}) if isinstance(execution_positions.get(code, {}), dict) else {}
        rows.append(
            {
                "code": code,
                "name": str(record.get("name", "")).strip(),
                "tier": _safe_int(record.get("tier", 0), 0),
                "mode": str(record.get("mode", "")).strip(),
                "exchange": str(market_info.get("exchange", "")).strip(),
                "market_char": str(market_info.get("market_char", "")).strip(),
                "resolver_source": str(market_info.get("resolver_source", "")).strip(),
                "quantity": quantity,
                "entry_price": round(entry_price, 4),
                "current_price": round(current_price, 4),
                "current_price_source": current_price_source,
                "price_is_estimated": price_is_estimated,
                "market_value": market_value,
                "cost_value": cost_value,
                "floating_pnl": floating_pnl,
                "floating_pnl_pct": floating_pnl_pct,
                "hold_days": hold_days,
                "entry_window": str(execution_meta.get("entry_window", "")).strip(),
                "last_buy_window": str(execution_meta.get("last_buy_window", "")).strip(),
                "entry_intent": str(execution_meta.get("entry_intent", "")).strip(),
                "built_ratio": round(_safe_float(execution_meta.get("built_ratio", 0.0), 0.0), 4),
                "quick_take_done": bool(execution_meta.get("quick_take_done", False)),
                "runner_active": bool(execution_meta.get("runner_active", False)),
            }
        )
    rows.sort(key=lambda item: (item["floating_pnl_pct"], item["code"]), reverse=True)
    return rows


def _load_previous_positions_snapshot() -> dict[str, dict[str, Any]]:
    payload = _read_json(POSITIONS_FILE)
    positions = payload.get("positions", []) if isinstance(payload, dict) else []
    snapshot_map: dict[str, dict[str, Any]] = {}
    for item in positions if isinstance(positions, list) else []:
        code = _normalize_code((item or {}).get("code", ""))
        if code:
            snapshot_map[code] = dict(item or {})
    return snapshot_map


def _holding_rows_with_fallback(
    records: list[dict[str, Any]],
    quote_map: dict[str, dict[str, Any]],
    previous_positions: dict[str, dict[str, Any]] | None = None,
    execution_positions: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    previous_positions = previous_positions or {}
    rows = _holding_rows(records, quote_map, execution_positions=execution_positions)
    if quote_map:
        return rows
    for item in rows:
        previous = previous_positions.get(item["code"], {})
        prev_price = _safe_float(previous.get("current_price", 0.0), 0.0)
        if prev_price <= 0:
            continue
        quantity = _safe_int(item.get("quantity", 0), 0)
        entry_price = _safe_float(item.get("entry_price", 0.0), 0.0)
        market_value = round(prev_price * quantity, 2) if prev_price > 0 else item["market_value"]
        cost_value = round(entry_price * quantity, 2)
        item["current_price"] = round(prev_price, 4)
        item["current_price_source"] = "previous_snapshot_fallback"
        item["price_is_estimated"] = True
        item["market_value"] = market_value
        item["floating_pnl"] = round(market_value - cost_value, 2)
        item["floating_pnl_pct"] = round((prev_price / entry_price - 1) * 100, 2) if prev_price > 0 and entry_price > 0 else 0.0
    rows.sort(key=lambda entry: (entry["floating_pnl_pct"], entry["code"]), reverse=True)
    return rows


def _compute_cash_balance(records: list[dict[str, Any]]) -> float:
    cash = INITIAL_CAPITAL
    for record in records:
        row = _normalize_record(record)
        buy_amount = _safe_float(row.get("buy_amount", 0.0), 0.0)
        quantity = _safe_int(row.get("quantity", 0), 0)
        sell_price = _safe_float(row.get("sell_price", 0.0), 0.0)
        cash -= buy_amount
        if str(row.get("status", "")).strip() == "closed" and sell_price > 0 and quantity > 0:
            cash += sell_price * quantity
    return round(cash, 2)


def _build_account_snapshot(
    records: list[dict[str, Any]],
    quote_map: dict[str, dict[str, Any]],
    *,
    previous_positions: dict[str, dict[str, Any]] | None = None,
    execution_positions: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    holding_rows = _holding_rows_with_fallback(
        records,
        quote_map,
        previous_positions=previous_positions,
        execution_positions=execution_positions,
    )
    stats = base.compute_track_stats(records)
    cash_balance = _compute_cash_balance(records)
    live_price_count = len([item for item in holding_rows if item.get("current_price_source") == "live_quote"])
    estimated_price_count = len([item for item in holding_rows if bool(item.get("price_is_estimated", False))])
    market_value = round(sum(item["market_value"] for item in holding_rows), 2)
    cost_value = round(sum(item["cost_value"] for item in holding_rows), 2)
    floating_pnl = round(sum(item["floating_pnl"] for item in holding_rows), 2)
    total_assets = round(cash_balance + market_value, 2)
    total_pnl = round(total_assets - INITIAL_CAPITAL, 2)
    total_return_pct = round((total_assets / INITIAL_CAPITAL - 1) * 100, 4) if INITIAL_CAPITAL > 0 else 0.0
    holding_win_count = len([item for item in holding_rows if item["floating_pnl"] > 0])
    holding_win_rate_pct = round(holding_win_count / len(holding_rows) * 100, 2) if holding_rows else 0.0
    account_snapshot = {
        "initial_capital": round(INITIAL_CAPITAL, 2),
        "cash_balance": cash_balance,
        "market_value": market_value,
        "holding_cost_value": cost_value,
        "floating_pnl": floating_pnl,
        "realized_pnl": stats["realized_pnl"],
        "total_pnl": total_pnl,
        "total_assets": total_assets,
        "total_return_pct": total_return_pct,
        "cash_usage_pct": round(market_value / total_assets * 100, 2) if total_assets > 0 else 0.0,
        "closed_trade_win_rate_pct": stats["win_rate_pct"],
        "holding_win_rate_pct": holding_win_rate_pct,
        "avg_closed_return_pct": stats["avg_return_pct"],
        "closed_trade_count": stats["closed_count"],
        "holding_count": len(holding_rows),
        "live_price_count": live_price_count,
        "estimated_price_count": estimated_price_count,
    }
    return account_snapshot, holding_rows, stats


def _write_positions_snapshot(holding_rows: list[dict[str, Any]]) -> None:
    payload = {
        "generated_at": _now_str(),
        "portfolio_name": PORTFOLIO_NAME,
        "portfolio_type": PORTFOLIO_TYPE,
        "holding_count": len(holding_rows),
        "positions": holding_rows,
    }
    _write_json_atomic(POSITIONS_FILE, payload)


def _write_order_log(
    *,
    action: str,
    code: str,
    name: str,
    quantity: int,
    fill_price: float,
    cash_before: float,
    cash_after: float,
    context: dict[str, Any] | None = None,
) -> str:
    context = context or {}
    order_id = _generate_local_order_id(action, code)
    payload = {
        "logged_at": _now_str(),
        "portfolio_name": PORTFOLIO_NAME,
        "portfolio_type": PORTFOLIO_TYPE,
        "order_id": order_id,
        "action": action,
        "code": _normalize_code(code),
        "name": str(name).strip(),
        "quantity": int(quantity),
        "fill_price": round(fill_price, 4),
        "amount": round(fill_price * quantity, 2),
        "cash_before": round(cash_before, 2),
        "cash_after": round(cash_after, 2),
        "mode": str(context.get("mode", "")).strip(),
        "tier": _safe_int(context.get("tier", 0), 0),
        "build_note": str(context.get("build_note", "")).strip(),
        "selection_rank": _safe_int(context.get("selection_rank", 0), 0),
        "selection_score": round(_safe_float(context.get("selection_score", 0.0), 0.0), 4),
        "target_weight_pct": round(_safe_float(context.get("target_weight_pct", 0.0), 0.0), 4),
        "close_reason": str(context.get("close_reason", "")).strip(),
        "window_key": str(context.get("window_key", "")).strip(),
        "entry_intent": str(context.get("entry_intent", context.get("intent", ""))).strip(),
        "buy_action": str(context.get("buy_action", "")).strip(),
        "sell_action": str(context.get("sell_action", "")).strip(),
        "selection_reasons": list(context.get("selection_reasons", []) or []),
    }
    _append_jsonl(ORDER_LOG_FILE, payload)
    return order_id


def write_account_summary(tag: str, records: list[dict[str, Any]], *, fast: bool = False) -> dict[str, Any]:
    previous_positions = _load_previous_positions_snapshot()
    execution_state = _prune_execution_state(_load_execution_state(), records)
    consistency_report = raise_on_inconsistent_challenger_state(records, execution_state, context=f"summary:{tag}")
    quote_map = {} if fast else load_quote_map([row.get("code", "") for row in records if str(row.get("status", "")).strip() == "holding"])
    account_snapshot, holding_rows, stats = _build_account_snapshot(
        records,
        quote_map,
        previous_positions=previous_positions,
        execution_positions=execution_state.get("positions", {}),
    )
    previous_summary = _read_json(SUMMARY_FILE) if fast else {}
    source_payload = _load_source_payload()
    payload = source_payload if isinstance(source_payload, dict) and source_payload else previous_summary
    previous_performance = previous_summary.get("performance", {}) if isinstance(previous_summary, dict) else {}
    champion_template = payload.get("champion_template", {}) if isinstance(payload, dict) else {}
    source_trade_date = str(payload.get("trade_date", payload.get("source_trade_date", ""))).strip()
    source_status = str(payload.get("status", payload.get("source_status", ""))).strip()

    summary = {
        "generated_at": _now_str(),
        "tag": tag,
        "portfolio_name": PORTFOLIO_NAME,
        "portfolio_type": PORTFOLIO_TYPE,
        "execution_scope": "local_paper_account_independent_of_mx_moni",
        "source_file": str(SOURCE_FILE),
        "source_trade_date": source_trade_date,
        "source_status": source_status,
        "quote_mode": "snapshot_fallback" if fast else "live_tdx",
        "account_snapshot": account_snapshot,
        "performance": {
            "champion_candidate_win_rate": _safe_float(
                champion_template.get("candidate_win_rate", previous_performance.get("champion_candidate_win_rate", 0.0)),
                0.0,
            ),
            "champion_candidate_avg_return": _safe_float(
                champion_template.get("candidate_avg_return", previous_performance.get("champion_candidate_avg_return", 0.0)),
                0.0,
            ),
            "champion_top50_hit_rate": _safe_float(
                champion_template.get("top50_hit_rate", previous_performance.get("champion_top50_hit_rate", 0.0)),
                0.0,
            ),
            "champion_front_shift_score": _safe_float(
                champion_template.get("front_shift_score", previous_performance.get("champion_front_shift_score", 0.0)),
                0.0,
            ),
            "closed_trade_win_rate_pct": stats["win_rate_pct"],
            "avg_closed_return_pct": stats["avg_return_pct"],
        },
        "execution_profile": {
            "position_state_count": len(execution_state.get("positions", {})) if isinstance(execution_state.get("positions", {}), dict) else 0,
            "history_count": len(execution_state.get("history", [])) if isinstance(execution_state.get("history", []), list) else 0,
            "consistency_check": consistency_report,
        },
        "holdings": holding_rows,
        "notes": [
            "本脚本完全不通过 mx-moni，下单与成交均在本地账本即时模拟。",
            "起始本金固定为 100 万，便于与 openclawd 主交易做 challenger A/B 对比。",
            "候选池直接读取 arkclaw 当前主链输出，买卖、净值、胜率和盈利率独立记录。",
        ],
        "files": {
            "track_file": str(TRACK_FILE),
            "summary_file": str(SUMMARY_FILE),
            "nav_file": str(NAV_FILE),
            "order_log_file": str(ORDER_LOG_FILE),
            "buy_plan_file": str(BUY_PLAN_FILE),
            "positions_file": str(POSITIONS_FILE),
            "execution_state_file": str(EXECUTION_STATE_FILE),
        },
    }
    _write_json_atomic(SUMMARY_FILE, summary)
    _write_positions_snapshot(holding_rows)
    if not fast:
        _write_csv_row(
            NAV_FILE,
            [
                "date",
                "time",
                "tag",
                "holding_count",
                "closed_count",
                "cash_balance",
                "market_value",
                "total_assets",
                "floating_pnl",
                "realized_pnl",
                "total_pnl",
                "total_return_pct",
                "closed_trade_win_rate_pct",
                "holding_win_rate_pct",
                "avg_closed_return_pct",
            ],
            {
                "date": _now().strftime("%Y-%m-%d"),
                "time": _now().strftime("%H:%M:%S"),
                "tag": tag,
                "holding_count": account_snapshot["holding_count"],
                "closed_count": account_snapshot["closed_trade_count"],
                "cash_balance": account_snapshot["cash_balance"],
                "market_value": account_snapshot["market_value"],
                "total_assets": account_snapshot["total_assets"],
                "floating_pnl": account_snapshot["floating_pnl"],
                "realized_pnl": account_snapshot["realized_pnl"],
                "total_pnl": account_snapshot["total_pnl"],
                "total_return_pct": account_snapshot["total_return_pct"],
                "closed_trade_win_rate_pct": account_snapshot["closed_trade_win_rate_pct"],
                "holding_win_rate_pct": account_snapshot["holding_win_rate_pct"],
                "avg_closed_return_pct": account_snapshot["avg_closed_return_pct"],
            },
        )
    return summary


def build_buy_plan(
    *,
    trigger_slot: str = "",
    force: bool = False,
    persist_plan: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    validate_opening_tradability_artifact(expected_trade_date=_today_str())
    buy_window = _resolve_buy_window(trigger_slot=trigger_slot)
    if not buy_window and not force:
        raise RuntimeError("当前时间/trigger_slot 不在 challenger 买入窗口")
    if not buy_window:
        buy_window = BUY_WINDOW_CONFIGS[-1]
    buy_window_key = str(buy_window.get("key", "")).strip()

    buy_override = _match_buy_override(trade_date=_today_str(), window_key=buy_window_key)
    if isinstance(buy_override, dict):
        selected_records = buy_override.get("records", [])
        payload = {
            "status": "ok",
            "trade_date": str(buy_override.get("source_trade_date", _expected_source_trade_date())).strip(),
            "run_slot": str(buy_override.get("source_run_slot", "")).strip(),
            "selected_records": selected_records,
            "generated_at": str(buy_override.get("source_generated_at", _now_str())).strip(),
            "override_plan_id": str(buy_override.get("plan_id", "")).strip(),
            "override_source_file": str(buy_override.get("source_file", "")).strip(),
        }
    else:
        payload = _ensure_fresh_source_payload()
        if payload.get("status") != "ok":
            raise RuntimeError("workbuddy_candidate_pool_latest.json 不可用")
        selected_records = payload.get("selected_records", [])
    if not isinstance(selected_records, list) or not selected_records:
        raise RuntimeError("Workbuddy 候选池为空")

    records = load_track_record()
    execution_state = _prune_execution_state(_load_execution_state(), records)
    holding_map = _holding_code_map(records)
    quote_map_all = load_quote_map([row.get("code", "") for row in records if str(row.get("status", "")).strip() == "holding"])
    account_snapshot, _, _ = _build_account_snapshot(records, quote_map_all)
    avail = _safe_float(account_snapshot.get("cash_balance", 0.0), 0.0)
    total_assets = _safe_float(account_snapshot.get("total_assets", INITIAL_CAPITAL), INITIAL_CAPITAL)
    cash_budget = round(avail, 2)
    override_exit_plan = {}
    if isinstance(buy_override, dict):
        override_exit_plan = buy_override.get("exit_plan", {}) if isinstance(buy_override.get("exit_plan", {}), dict) else {}

    exclusions = _load_today_tradability_exclusions()
    quote_map = load_quote_map([row.get("code", "") for row in selected_records])
    buy_list: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    selected_records = sorted(selected_records, key=_execution_candidate_priority_key)

    # ----- Phase 1: qualify candidates (same filters as before) -----
    qualified: list[dict[str, Any]] = []
    for row in selected_records:
        code = _normalize_code(row.get("code", ""))
        name = str(row.get("name", "")).strip()
        if not code:
            continue
        if _is_risk_warning_candidate(row):
            skipped.append({"code": code, "name": name, "reason": "st_risk_warning_filtered"})
            continue
        market_info = resolve_market_info(code)
        if not market_info.get("tradable_by_current_executor"):
            skipped.append({"code": code, "name": name, "reason": "unsupported_market_current_executor"})
            continue
        if code in exclusions:
            skipped.append({"code": code, "name": name, "reason": exclusion_reason_text(exclusions[code])})
            continue

        ref_price = _quote_execution_price(quote_map.get(code))
        if ref_price <= 0:
            skipped.append({"code": code, "name": name, "reason": "quote_unavailable"})
            continue

        recent_closed = _recent_closed_same_code(records, code, lookback_days=2)
        readiness = _build_execution_readiness(
            row,
            quote_map.get(code),
            window_key=buy_window["key"],
            recent_closed=recent_closed,
        )
        existing_ratio = 0.0
        existing_cost = 0.0
        existing_state = execution_state.get("positions", {}).get(code, {})
        holding_idx = holding_map.get(code)
        if holding_idx is not None:
            holding_record = _normalize_record(records[holding_idx])
            if str(holding_record.get("date", "")).strip() != _today_str():
                skipped.append({"code": code, "name": name, "reason": "already_holding_from_previous_trade_date"})
                continue
            existing_ratio = _safe_float(existing_state.get("built_ratio", 0.0), 0.0)
            existing_cost = _safe_float(holding_record.get("buy_amount", 0.0), 0.0)

        if not buy_override:
            action = _resolve_entry_action(buy_window["key"], readiness, existing_ratio=existing_ratio)
            if action["action"] == "skip":
                skipped.append({"code": code, "name": name, "reason": action["reason"]})
                continue

        qualified.append({
            "row": row,
            "code": code,
            "name": name,
            "market_info": market_info,
            "ref_price": ref_price,
            "readiness": readiness,
            "existing_ratio": existing_ratio,
            "existing_cost": existing_cost,
        })

    # ----- Phase 2: Kelly-based position sizing -----
    if buy_override:
        sizer_weights: dict[str, float] = {}
        sizer_debug: dict[str, Any] = {"mode": "buy_override_disabled"}
    else:
        # Enrich with fields the sizer needs from the pool entry
        sizer_candidates: list[dict[str, Any]] = []
        for q in qualified:
            row = q["row"]
            sizer_candidates.append({
                "code": q["code"],
                "name": q["name"],
                "score": _safe_float(row.get("selection_score", 0.0), 0.0),
                "avg_candidate_win_rate": _safe_float(row.get("avg_candidate_win_rate", 0.0), 0.0),
                "avg_candidate_avg_return": _safe_float(row.get("avg_candidate_avg_return", 0.0), 0.0),
                "avg_profitability_priority": _profitability_priority_value(row),
                "volatility": _safe_float(row.get("volatility", 0.0), 0.0),
                "correlation_group": str(row.get("correlation_group", "") or "").strip(),
                "selection_rank": _safe_int(row.get("selection_rank", 0), 0),
            })

        realized_pnl = account_snapshot.get("realized_pnl", 0.0)
        floating_pnl = account_snapshot.get("floating_pnl", 0.0)
        total_pnl = realized_pnl + floating_pnl
        drawdown_pct = max(0.0, -total_pnl / max(INITIAL_CAPITAL, 1.0) * 100.0)

        sizer_config = SizerConfig()
        allocations, sizer_debug = compute_position_weights(
            sizer_candidates,
            total_assets,
            drawdown_pct=drawdown_pct,
            window_key=buy_window["key"],
            config=sizer_config,
        )
        sizer_weights = {a.code: a.weight_pct for a in allocations}
        sizer_targets = {a.code: a.target_amount for a in allocations}
        sizer_debug["sized_count"] = len(allocations)

    # ----- Phase 3: build buy_list from sized candidates -----
    for q in qualified:
        row = q["row"]
        code = q["code"]
        name = q["name"]
        market_info = q["market_info"]
        ref_price = q["ref_price"]
        readiness = q["readiness"]
        existing_ratio = q["existing_ratio"]
        existing_cost = q["existing_cost"]

        if buy_override:
            target_weight_pct = _safe_float(row.get("target_weight_pct", 0.0), 0.0)
            if target_weight_pct <= 0:
                target_weight_pct = round(100 / max(len(selected_records), 1), 2)
            target_amount = round(cash_budget * target_weight_pct / 100, 2)
            planned_target_amount = target_amount
            action = {
                "action": "scheduled_override_buy" if existing_cost <= 0 else "scheduled_override_topup",
                "target_build_ratio": 1.0,
                "buy_ratio": round(max(0.0, min(1.0, planned_target_amount / max(cash_budget, 1.0))), 4),
                "reason": "scheduled_buy_override",
            }
            exit_plan = {
                "intent": str(override_exit_plan.get("intent", EXIT_INTENT_FAST)).strip() or EXIT_INTENT_FAST,
                "quick_take_trigger_pct": _safe_float(override_exit_plan.get("quick_take_trigger_pct", 0.0), 0.0),
                "quick_take_ratio": _safe_float(override_exit_plan.get("quick_take_ratio", 0.0), 0.0),
                "quick_take_window": str(override_exit_plan.get("quick_take_window", "")).strip(),
                "final_exit_window": str(override_exit_plan.get("final_exit_window", "D1_0945")).strip() or "D1_0945",
                "runner_day2_window": str(override_exit_plan.get("runner_day2_window", "")).strip(),
                "runner_day2_final_window": str(override_exit_plan.get("runner_day2_final_window", "")).strip(),
            }
        else:
            action = _resolve_entry_action(buy_window["key"], readiness, existing_ratio=existing_ratio)
            if action["action"] == "skip":
                skipped.append({"code": code, "name": name, "reason": action["reason"]})
                continue

            kelly_weight = sizer_weights.get(code, 0.0)
            target_weight_pct = kelly_weight if kelly_weight > 0 else round(100 / max(len(qualified), 1), 2)
            target_amount = sizer_targets.get(code, round(total_assets * target_weight_pct / 100, 2))
            planned_target_amount = round(target_amount * _safe_float(action.get("target_build_ratio", 0.0), 0.0), 2)
            exit_plan = _classify_exit_intent(row, readiness, window_key=buy_window["key"])

        additional_amount = max(0.0, planned_target_amount - existing_cost)
        allowed_amount = min(additional_amount, avail)
        quantity = base.calc_buy_quantity(ref_price, allowed_amount)
        if quantity <= 0:
            skipped.append({"code": code, "name": name, "reason": "insufficient_cash_or_lot_size"})
            continue

        cost = round(quantity * ref_price, 2)
        avail = max(0.0, avail - cost)
        selection_rank = _safe_int(row.get("selection_rank", 0), 0)
        tier = _workbuddy_tier(selection_rank if selection_rank > 0 else len(buy_list) + 1)
        buy_list.append(
            {
                "code": code,
                "name": name,
                "tier": tier,
                "mode": (
                    "workbuddy_local_scheduled_buy_override"
                    if buy_override
                    else f"workbuddy_local_{str(row.get('role', 'challenger')).strip() or 'challenger'}"
                ),
                "selection_rank": selection_rank,
                "selection_score": _safe_float(row.get("selection_score", 0.0), 0.0),
                "avg_profitability_priority": _profitability_priority_value(row),
                "avg_candidate_win_rate": _safe_float(row.get("avg_candidate_win_rate", 0.0), 0.0),
                "avg_candidate_avg_return": _safe_float(row.get("avg_candidate_avg_return", 0.0), 0.0),
                "target_weight_pct": target_weight_pct,
                "target_amount": round(target_amount, 2),
                "exchange": str(market_info.get("exchange", "")).strip(),
                "market_char": str(market_info.get("market_char", "")).strip(),
                "resolver_source": str(market_info.get("resolver_source", "")).strip(),
                "entry_price": round(ref_price, 4),
                "quantity": quantity,
                "cost": cost,
                "buy_action": action["action"],
                "buy_ratio": _safe_float(action.get("buy_ratio", 0.0), 0.0),
                "target_build_ratio": _safe_float(action.get("target_build_ratio", 0.0), 0.0),
                "existing_build_ratio": round(existing_ratio, 4),
                "readiness_score": _safe_float(readiness.get("score", 0.0), 0.0),
                "readiness_components": readiness.get("components", {}),
                "window_key": buy_window["key"],
                "window_label": buy_window["label"],
                "intent": exit_plan.get("intent", EXIT_INTENT_FAST),
                "exit_plan": exit_plan,
                "build_note": (
                    (
                        f"Workbuddy scheduled override buy | plan={str(buy_override.get('plan_id', '')).strip() or '?'} | "
                        f"rank={selection_rank or '?'} | score={_safe_float(row.get('selection_score', 0.0), 0.0):.2f} | "
                        f"profit={_profitability_priority_value(row):.1f} | "
                        f"window={buy_window['key']} | action={action['action']} | "
                        f"intent={exit_plan.get('intent', EXIT_INTENT_FAST)} | target_cash={target_amount:.2f}"
                    )
                    if buy_override
                    else (
                        f"Workbuddy local challenger buy | rank={selection_rank or '?'} | "
                        f"score={_safe_float(row.get('selection_score', 0.0), 0.0):.2f} | "
                        f"profit={_profitability_priority_value(row):.1f} | "
                        f"window={buy_window['key']} | readiness={_safe_float(readiness.get('score', 0.0), 0.0):.1f} | "
                        f"action={action['action']} | intent={exit_plan.get('intent', EXIT_INTENT_FAST)}"
                    )
                ),
                "selection_reasons": list(row.get("selection_reasons", []) or []),
            }
        )

    plan_payload = {
        "generated_at": _now_str(),
        "portfolio_name": PORTFOLIO_NAME,
        "portfolio_type": PORTFOLIO_TYPE,
        "source_file": str(SOURCE_FILE),
        "source_trade_date": str(payload.get("trade_date", "")).strip(),
        "source_run_slot": str(payload.get("run_slot", "")).strip(),
        "execution_window": buy_window,
        "account_reference": {
            "initial_capital": round(INITIAL_CAPITAL, 2),
            "total_assets": round(total_assets, 2),
            "cash_balance": round(account_snapshot.get("cash_balance", INITIAL_CAPITAL), 2),
            "note": "本计划基于本地 challenger 账本生成，不依赖 mx-moni 账户余额。",
        },
        "buy_override": (
            {
                "enabled": True,
                "plan_id": str(buy_override.get("plan_id", "")).strip(),
                "effective_trade_date": str(buy_override.get("effective_trade_date", "")).strip(),
                "execution_window_key": str(buy_override.get("execution_window_key", "")).strip(),
                "allocation_base_cash": round(cash_budget, 2),
                "source_file": str(buy_override.get("source_file", "")).strip(),
                "source_trade_date": str(buy_override.get("source_trade_date", "")).strip(),
            }
            if buy_override
            else {"enabled": False}
        ),
        "position_sizer": sizer_debug,
        "buy_candidate_count": len(buy_list),
        "skipped_count": len(skipped),
        "buy_candidates": buy_list,
        "skipped": skipped,
    }
    if persist_plan:
        _write_json_atomic(BUY_PLAN_FILE, plan_payload)
    return plan_payload, buy_list, skipped, execution_state


def _ensure_trade_window(action: str, *, dry_run: bool, force: bool, trigger_slot: str = "") -> bool:
    if force:
        return True
    slot = str(trigger_slot or "").strip()
    if slot:
        if action == "buy" and _resolve_buy_window(trigger_slot=slot):
            return True
        if action in {"sell", "smart_sell"} and _resolve_sell_window(trigger_slot=slot):
            return True
    return base.ensure_trade_window(action, dry_run=dry_run)


def _append_build_note(record: dict[str, Any], note: str) -> dict[str, Any]:
    record = _normalize_record(record)
    text = str(note or "").strip()
    if not text:
        return record
    build_note = str(record.get("build_note", "")).strip()
    record["build_note"] = f"{build_note}; {text}" if build_note else text
    record["last_synced_at"] = _now_str()
    return record


def _emit_local_sell_fill(
    records: list[dict[str, Any]],
    *,
    code: str,
    name: str,
    quantity: int,
    fill_price: float,
    close_reason: str,
    context: dict[str, Any] | None = None,
) -> tuple[bool, str, str]:
    idx = _find_record_index(records, code)
    if idx is None:
        return False, "", ""
    record = _normalize_record(records[idx])
    total_qty = _safe_int(record.get("quantity", 0), 0)
    sell_qty = min(_safe_int(quantity, 0), total_qty)
    if sell_qty <= 0:
        return False, "", ""
    # #region debug-point C:emit-local-sell-fill
    _debug_emit_event(
        "C",
        "workbuddy_local_challenger.py:_emit_local_sell_fill",
        "[DEBUG] challenger emit local sell fill",
        {
            "code": code,
            "name": name,
            "fill_price": round(fill_price, 4),
            "entry_price": round(_safe_float(record.get("entry_price", 0.0), 0.0), 4),
            "sell_qty": sell_qty,
            "total_qty": total_qty,
            "close_reason": close_reason,
            "status_before": str(record.get("status", "")).strip(),
        },
    )
    # #endregion
    cash_before = _compute_cash_balance(records)
    cash_after = cash_before + fill_price * sell_qty
    order_id = _write_order_log(
        action="sell",
        code=code,
        name=name,
        quantity=sell_qty,
        fill_price=fill_price,
        cash_before=cash_before,
        cash_after=cash_after,
        context={**(context or {}), "close_reason": close_reason},
    )
    fill_payload = {
        "order_id": order_id,
        "trade_time": _now().strftime("%H:%M:%S"),
        "trade_date": _today_str(),
        "trade_price": fill_price,
        "trade_count": sell_qty,
    }
    if sell_qty >= total_qty:
        records[idx] = base.apply_sell_fill(
            record,
            fill_payload,
            fallback_price=fill_price,
            close_reason=close_reason,
        )
        return True, order_id, "full"

    entry_price = _safe_float(record.get("entry_price", 0.0), 0.0)
    remain_qty = total_qty - sell_qty
    sold_record = dict(record)
    sold_record["quantity"] = str(sell_qty)
    sold_record["buy_amount"] = f"{entry_price * sell_qty:.2f}"
    target_amount = _safe_float(record.get("target_amount", 0.0), 0.0)
    if target_amount > 0:
        sold_record["target_amount"] = f"{round(target_amount * sell_qty / max(total_qty, 1), 0):.0f}"
    sold_record = base.apply_sell_fill(
        sold_record,
        fill_payload,
        fallback_price=fill_price,
        close_reason=f"{close_reason}[partial]",
    )
    remain_record = dict(record)
    remain_record["quantity"] = str(remain_qty)
    remain_record["buy_amount"] = f"{entry_price * remain_qty:.2f}"
    if target_amount > 0:
        remain_record["target_amount"] = f"{round(target_amount * remain_qty / max(total_qty, 1), 0):.0f}"
    remain_record = _append_build_note(remain_record, f"partial_lock {sell_qty}/{total_qty} @ {fill_price:.2f}")
    records[idx] = remain_record
    records.append(_append_build_note(sold_record, f"partial_exit_from {total_qty}"))
    return True, order_id, "partial"


def do_buy(*, dry_run: bool = False, force: bool = False, trigger_slot: str = "") -> int:
    if not _ensure_trade_window("buy", dry_run=dry_run, force=force, trigger_slot=trigger_slot):
        return base.EXIT_WINDOW_SKIPPED

    buy_window = _resolve_buy_window(trigger_slot=trigger_slot)
    if not buy_window and not force:
        print(" Workbuddy 本地 Challenger 当前不在配置买入窗口")
        return base.EXIT_NO_ACTION

    plan_payload, buy_list, skipped, execution_state = build_buy_plan(trigger_slot=trigger_slot, force=force)
    print(f"\n{'=' * 60}")
    print(f" Workbuddy 本地 Challenger 买入 {'[DRY RUN]' if dry_run else '[LOCAL FILL]'}")
    print(
        f" 来源池: {Path(plan_payload['source_file']).name} | "
        f"窗口 {plan_payload.get('execution_window', {}).get('key', '?')} | "
        f"候选{len(buy_list)}只 | 跳过{len(skipped)}只"
    )
    print(f"{'=' * 60}")
    for item in skipped[:10]:
        print(f"  [SKIP] {item['code']} {item['name']} | {item['reason']}")
    if not buy_list:
        print(" 没有形成可买清单")
        return base.EXIT_NO_ACTION

    if dry_run:
        for item in buy_list:
            print(
                f"  [DRY] {item['window_key']} {item['buy_action']} {item['intent']} "
                f"T{item['tier']} {item['code']} {item['name']} "
                f"¥{item['entry_price']:.2f} x {item['quantity']}股 ≈¥{item['cost']:,.0f} "
                f"| readiness={item['readiness_score']:.1f}"
            )
        return base.EXIT_OK

    records = load_track_record()
    execution_state = _prune_execution_state(execution_state, records)
    holding_map = _holding_code_map(records)
    success_count = 0
    for item in buy_list:
        idx = holding_map.get(item["code"])
        cash_before = _compute_cash_balance(records)
        order_id = _write_order_log(
            action="buy",
            code=item["code"],
            name=item["name"],
            quantity=item["quantity"],
            fill_price=item["entry_price"],
            cash_before=cash_before,
            cash_after=max(0.0, cash_before - item["cost"]),
            context=item,
        )
        if idx is None:
            record = _empty_record(
                code=item["code"],
                name=item["name"],
                tier=item["tier"],
                mode=item["mode"],
                build_note=item["build_note"],
                target_amount=item["target_amount"],
                trade_date=_today_str(),
            )
        else:
            record = _normalize_record(records[idx])
        fill_payload = {
            "order_id": order_id,
            "trade_time": _now().strftime("%H:%M:%S"),
            "trade_date": _today_str(),
            "trade_price": item["entry_price"],
            "trade_count": item["quantity"],
        }
        record = base.apply_buy_fill(
            record,
            fill_payload,
            fallback_price=item["entry_price"],
            fallback_quantity=item["quantity"],
            note_suffix=f"{item['window_key']} {item['buy_action']} {item['intent']}",
        )
        if idx is None:
            records.append(record)
        else:
            records[idx] = record
        success_count += 1
        holding_map = _holding_code_map(records)
        entry_state = execution_state.get("positions", {}).get(item["code"], {}) if isinstance(execution_state.get("positions", {}), dict) else {}
        exit_plan = item.get("exit_plan", {}) if isinstance(item.get("exit_plan", {}), dict) else {}
        execution_state.setdefault("positions", {})
        execution_state["positions"][item["code"]] = {
            "code": item["code"],
            "name": item["name"],
            "trade_date": _today_str(),
            "entry_window": entry_state.get("entry_window") or item["window_key"],
            "last_buy_window": item["window_key"],
            "entry_intent": exit_plan.get("intent", entry_state.get("entry_intent", EXIT_INTENT_FAST)),
            "built_ratio": item["target_build_ratio"],
            "target_weight_pct": item["target_weight_pct"],
            "readiness_score": item["readiness_score"],
            "readiness_components": item.get("readiness_components", {}),
            "quick_take_done": bool(entry_state.get("quick_take_done", False)),
            "quick_take_trigger_pct": exit_plan.get("quick_take_trigger_pct", entry_state.get("quick_take_trigger_pct", 0.0)),
            "quick_take_ratio": exit_plan.get("quick_take_ratio", entry_state.get("quick_take_ratio", 0.0)),
            "quick_take_window": exit_plan.get("quick_take_window", entry_state.get("quick_take_window", "")),
            "final_exit_window": exit_plan.get("final_exit_window", entry_state.get("final_exit_window", "D1_0945")),
            "runner_day2_window": exit_plan.get("runner_day2_window", entry_state.get("runner_day2_window", "")),
            "runner_day2_final_window": exit_plan.get("runner_day2_final_window", entry_state.get("runner_day2_final_window", "")),
            "runner_allowed": bool(exit_plan.get("intent") == EXIT_INTENT_RUNNER),
            "runner_active": bool(entry_state.get("runner_active", False)),
            "last_action": item["buy_action"],
            "last_order_id": order_id,
            "last_action_at": _now_str(),
        }
        _append_execution_history(
            execution_state,
            {
                "logged_at": _now_str(),
                "action": item["buy_action"],
                "window_key": item["window_key"],
                "code": item["code"],
                "name": item["name"],
                "quantity": item["quantity"],
                "fill_price": item["entry_price"],
                "intent": exit_plan.get("intent", EXIT_INTENT_FAST),
                "readiness_score": item["readiness_score"],
            },
        )
        print(
            f"  {item['window_key']} {item['buy_action']} {item['intent']} "
            f"T{item['tier']} {item['code']} {item['name']} "
            f"¥{item['entry_price']:.2f} x {item['quantity']}股 -> 本地成交 {order_id}"
        )

    _persist_local_state(records, execution_state, context="buy")
    write_account_summary("buy", records)
    _consume_matching_buy_override(
        trade_date=_today_str(),
        window_key=str((buy_window or {}).get("key", "")).strip(),
        buy_count=success_count,
    )
    return base.EXIT_OK if success_count > 0 else base.EXIT_RUNTIME_ERROR


def _update_execution_state_after_sell(
    execution_state: dict[str, Any],
    *,
    code: str,
    sell_action: str,
    sell_window_key: str,
    sell_qty: int,
    total_qty: int,
    fill_price: float,
    close_reason: str,
    remove_position: bool,
) -> None:
    positions = execution_state.get("positions", {})
    if not isinstance(positions, dict):
        positions = {}
        execution_state["positions"] = positions
    entry_state = positions.get(code, {}) if isinstance(positions.get(code, {}), dict) else {}
    if sell_action == "sell_partial":
        entry_state["quick_take_done"] = True
        entry_state["runner_active"] = bool(entry_state.get("runner_allowed", False))
        entry_state["last_sell_window"] = sell_window_key
        entry_state["last_action"] = sell_action
        entry_state["last_action_at"] = _now_str()
        entry_state["last_sell_qty"] = sell_qty
        positions[code] = entry_state
    elif sell_action == "hold_runner":
        entry_state["runner_active"] = True
        entry_state["last_sell_window"] = sell_window_key
        entry_state["last_action"] = sell_action
        entry_state["last_action_at"] = _now_str()
        positions[code] = entry_state
    elif remove_position:
        positions.pop(code, None)
    _append_execution_history(
        execution_state,
        {
            "logged_at": _now_str(),
            "action": sell_action,
            "window_key": sell_window_key,
            "code": code,
            "quantity": sell_qty,
            "total_quantity": total_qty,
            "fill_price": round(fill_price, 4),
            "close_reason": close_reason,
        },
    )


def _do_sell_core(*, smart: bool, dry_run: bool = False, force: bool = False, trigger_slot: str = "") -> int:
    action = "smart_sell" if smart else "sell"
    if not _ensure_trade_window(action, dry_run=dry_run, force=force, trigger_slot=trigger_slot):
        return base.EXIT_WINDOW_SKIPPED
    validate_opening_tradability_artifact(expected_trade_date=_today_str())

    sell_window = _resolve_sell_window(trigger_slot=trigger_slot) or (_resolve_sell_window(now_dt=_now()) if force else None)
    if not sell_window and not force:
        print(" Workbuddy 本地 Challenger 当前不在配置卖出窗口")
        return base.EXIT_NO_ACTION
    if not sell_window:
        sell_window = SELL_WINDOW_CONFIGS[0]

    records = load_track_record()
    execution_state = _prune_execution_state(_load_execution_state(), records)
    holdings = [row for row in records if str(row.get("status", "")).strip() == "holding"]
    if not holdings:
        print(" Workbuddy 本地 Challenger 当前无持仓")
        return base.EXIT_NO_ACTION

    exclusions = _load_today_tradability_exclusions()
    quote_map = load_quote_map([row.get("code", "") for row in holdings])
    sell_list: list[dict[str, Any]] = []
    skipped: list[str] = []
    hold_count = 0
    skipped_count = 0
    sold_count = 0
    partial_count = 0
    today = _today_str()
    tdx_api = None
    if smart:
        tdx_api = base.connect_tdx()
        if tdx_api:
            print(" TDX已连接（本地 challenger 信号衰减检测模式）")
        else:
            print(" TDX连接失败，本轮仅执行 T+5 兜底卖出")

    for record in holdings:
        code = _normalize_code(record.get("code", ""))
        name = str(record.get("name", "")).strip()
        market_info = resolve_market_info(code)
        if not market_info.get("tradable_by_current_executor"):
            skipped.append(f"{code} unsupported_market_current_executor")
            skipped_count += 1
            continue
        if code in exclusions:
            skipped.append(f"{code} {exclusion_reason_text(exclusions[code])}")
            skipped_count += 1
            continue
        try:
            hold_days = (_now() - datetime.strptime(str(record.get("date", today)), "%Y-%m-%d")).days
        except Exception:
            hold_days = 0
        quote = quote_map.get(code) or {}
        ref_price = _quote_execution_price(quote)
        # #region debug-point B:ref-price-selection
        _debug_emit_event(
            "B",
            "workbuddy_local_challenger.py:_do_sell_core",
            "[DEBUG] challenger sell ref_price selected",
            {
                "code": code,
                "name": name,
                "sell_window_key": sell_window["key"],
                "hold_days": hold_days,
                "entry_price": round(_safe_float(record.get("entry_price", 0.0), 0.0), 4),
                "quote_price": round(_safe_float(quote.get("price", 0.0), 0.0), 4),
                "quote_last_close": round(_safe_float(quote.get("last_close", 0.0), 0.0), 4),
                "quote_open": round(_safe_float(quote.get("open", 0.0), 0.0), 4),
                "quote_high": round(_safe_float(quote.get("high", 0.0), 0.0), 4),
                "quote_low": round(_safe_float(quote.get("low", 0.0), 0.0), 4),
                "quote_present": bool(quote),
                "ref_price": round(ref_price, 4),
                "ref_source": "live_quote" if ref_price > 0 else "missing_live_quote",
            },
        )
        # #endregion
        if ref_price <= 0:
            skipped.append(f"{code} 实时行情不可用")
            skipped_count += 1
            continue
        quantity = _safe_int(record.get("quantity", 0), 0)
        if quantity < 100:
            skipped.append(f"{code} 数量不足100股")
            skipped_count += 1
            continue
        entry_price = _safe_float(record.get("entry_price", 0.0), 0.0)
        pnl_pct = (ref_price / entry_price - 1) * 100 if ref_price > 0 and entry_price > 0 else 0.0
        entry_state = execution_state.get("positions", {}).get(code, {}) if isinstance(execution_state.get("positions", {}), dict) else {}
        should_sell = False
        decay_reason = ""
        if smart and tdx_api:
            should_sell, decay_reason, _ = base.evaluate_signal_decay(
                tdx_api,
                code,
                entry_price,
                str(record.get("mode", "")).strip(),
                profit_pct=pnl_pct,
            )
        exit_decision = _resolve_exit_action(
            entry_state,
            sell_window_key=sell_window["key"],
            hold_days=hold_days,
            quantity=quantity,
            pnl_pct=pnl_pct,
            quote=quote_map.get(code),
            smart_should_sell=should_sell,
            smart_reason=decay_reason,
        )
        if exit_decision["action"] in {"hold", "hold_runner"}:
            if exit_decision["action"] == "hold_runner":
                _update_execution_state_after_sell(
                    execution_state,
                    code=code,
                    sell_action="hold_runner",
                    sell_window_key=sell_window["key"],
                    sell_qty=0,
                    total_qty=quantity,
                    fill_price=ref_price,
                    close_reason=exit_decision["reason"],
                    remove_position=False,
                )
                _persist_local_state(records, execution_state, context=f"hold_runner:{code}")
            print(
                f"  {code} {name} | {market_info.get('exchange', '')} | "
                f"窗口 {sell_window['key']} | 持仓{hold_days}天 | 收益{pnl_pct:+.1f}% | {exit_decision['reason']} -> 继续持有"
            )
            hold_count += 1
            continue

        sell_list.append(
            {
                "code": code,
                "name": name,
                "quantity": exit_decision["quantity"],
                "total_quantity": quantity,
                "ref_price": ref_price,
                "close_reason": exit_decision["reason"],
                "pnl_pct": pnl_pct,
                "exchange": str(market_info.get("exchange", "")).strip(),
                "sell_action": exit_decision["action"],
                "entry_intent": str(entry_state.get("entry_intent", EXIT_INTENT_FAST)).strip() or EXIT_INTENT_FAST,
                "window_key": sell_window["key"],
                "tier": _safe_int(record.get("tier", 0), 0),
                "mode": str(record.get("mode", "")).strip(),
            }
        )

    print(f"\n{'=' * 60}")
    mode_label = "智能卖出(T+1窗口分层 + 信号衰减 + T+5兜底)" if smart else "T+1窗口分层卖出"
    print(f" Workbuddy 本地 Challenger {mode_label} {'[DRY RUN]' if dry_run else '[LOCAL FILL]'}")
    print(f" 当前窗口 {sell_window['key']} | 拟卖{len(sell_list)}只 | 跳过{len(skipped)}只")
    print(f"{'=' * 60}")
    for item in skipped[:10]:
        print(f"  [SKIP] {item}")
    if not sell_list:
        if tdx_api:
            try:
                tdx_api.disconnect()
            except Exception:
                pass
        return base.EXIT_NO_ACTION

    if dry_run:
        for item in sell_list:
            sold_count += 1
            print(
                f"  [DRY] {item['code']} {item['name']} | {item['exchange']} | "
                f"{item['sell_action']} | {item['close_reason']} | "
                f"预计卖出{item['quantity']}股/{item['total_quantity']}股 | 当前收益{item['pnl_pct']:+.2f}%"
            )
        if tdx_api:
            try:
                tdx_api.disconnect()
            except Exception:
                pass
        return base.EXIT_OK

    for item in sell_list:
        ok, order_id, fill_kind = _emit_local_sell_fill(
            records,
            code=item["code"],
            name=item["name"],
            quantity=item["quantity"],
            fill_price=item["ref_price"],
            close_reason=item["close_reason"],
            context=item,
        )
        if ok:
            sold_count += 1
            if fill_kind == "partial":
                partial_count += 1
            _update_execution_state_after_sell(
                execution_state,
                code=item["code"],
                sell_action=item["sell_action"],
                sell_window_key=item["window_key"],
                sell_qty=item["quantity"],
                total_qty=item["total_quantity"],
                fill_price=item["ref_price"],
                close_reason=item["close_reason"],
                remove_position=(fill_kind == "full"),
            )
            print(
                f"  {item['code']} {item['name']} | {item['exchange']} | "
                f"{item['sell_action']} | {item['close_reason']} | 当前收益{item['pnl_pct']:+.2f}% "
                f"-> 本地成交 {order_id}"
            )
        else:
            skipped_count += 1

    if tdx_api:
        try:
            tdx_api.disconnect()
        except Exception:
            pass

    _persist_local_state(records, execution_state, context="smart_sell" if smart else "sell")
    write_account_summary("smart_sell" if smart else "sell", records)
    print(f"\n{'=' * 50}")
    print(f" {mode_label} 结果")
    print(
        f"  卖出订单: {sold_count} 笔 | 其中部分止盈: {partial_count} 笔 | "
        f"继续持有: {hold_count} 只 | 跳过/失败: {skipped_count} 只"
    )
    print(f"{'=' * 50}")
    return base.EXIT_OK if sold_count > 0 else (base.EXIT_RUNTIME_ERROR if skipped_count > 0 and hold_count <= 0 else base.EXIT_NO_ACTION)


def do_sell(*, dry_run: bool = False, force: bool = False, trigger_slot: str = "") -> int:
    return _do_sell_core(smart=False, dry_run=dry_run, force=force, trigger_slot=trigger_slot)


def do_smart_sell(*, dry_run: bool = False, force: bool = False, trigger_slot: str = "") -> int:
    lock_owner = "workbuddy-smart-sell"
    lock_state = base.acquire_shared_phase_lock(
        "smart_sell_shared",
        owner=lock_owner,
        ttl_seconds=base.SMART_SELL_SHARED_LOCK_TTL_SECONDS,
    )
    if not lock_state.get("acquired"):
        holder = str(lock_state.get("owner", "")).strip() or "unknown"
        print(f" Workbuddy smart-sell 共享锁占用中，当前由 {holder} 执行，本轮快速跳过。")
        return base.EXIT_NO_ACTION
    try:
        return _do_sell_core(smart=True, dry_run=dry_run, force=force, trigger_slot=trigger_slot)
    finally:
        base.release_shared_phase_lock("smart_sell_shared", owner=lock_owner)


def do_status() -> int:
    try:
        validate_opening_tradability_artifact(expected_trade_date=_today_str())
    except RuntimeValidationError as exc:
        print(f" [WARN] opening_tradability 未就绪，status 仅基于本地账本输出: {exc}")
    records = load_track_record()
    summary = write_account_summary("status", records, fast=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return base.EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description="Workbuddy challenger 本地模拟下单")
    parser.add_argument("--buy", action="store_true", help="按 workbuddy 主链候选池执行本地模拟买入")
    parser.add_argument("--sell", action="store_true", help="按 T+5 规则执行本地模拟卖出")
    parser.add_argument("--smart-sell", action="store_true", help="按 smart sell 巡检点执行信号衰减卖出 + T+5 兜底")
    parser.add_argument("--status", action="store_true", help="查看本地 challenger 账户摘要")
    parser.add_argument("--dry-run", action="store_true", help="模拟运行，不落账本")
    parser.add_argument("--force", action="store_true", help="忽略交易时间窗口限制")
    parser.add_argument("--run-id", default="", help="自动化运行ID")
    parser.add_argument("--task-name", default="", help="任务名")
    parser.add_argument("--trigger-slot", default="", help="计划任务时间槽，例如 10:00 / 09:45")
    args = parser.parse_args()

    if args.buy:
        return do_buy(dry_run=args.dry_run, force=args.force, trigger_slot=args.trigger_slot)
    if args.sell:
        return do_sell(dry_run=args.dry_run, force=args.force, trigger_slot=args.trigger_slot)
    if args.smart_sell:
        return do_smart_sell(dry_run=args.dry_run, force=args.force, trigger_slot=args.trigger_slot)
    if args.status:
        return do_status()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
