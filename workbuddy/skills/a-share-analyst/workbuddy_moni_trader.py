#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workbuddy 独立下单脚本。

目标：
1. 以 `mx_workbuddy_portfolio_latest.json` 为买入来源；
2. 维护独立的 workbuddy 账本、pending、摘要和交易日志；
3. 不复用主交易 `v10_track_record.csv` 的对仓逻辑，避免把主交易持仓揉进 workbuddy；
4. 复用同一套 mx-moni 交易接口，因此底层账户资金/持仓接口仍是全局共享。
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import v10_moni_trader as base

from market_resolver import build_today_exclusion_map, exclusion_reason_text, resolve_market_info
from package_paths import DATA_DIR


PORTFOLIO_NAME = "workbuddy_live_challenger"
SOURCE_FILE = DATA_DIR / "mx_workbuddy_portfolio_latest.json"
OPENING_TRADABILITY_FILE = DATA_DIR / "opening_tradability_latest.json"

TRACK_FILE = DATA_DIR / "workbuddy_track_record.csv"
NAV_FILE = DATA_DIR / "workbuddy_nav_history.csv"
SUMMARY_FILE = DATA_DIR / "workbuddy_account_summary_latest.json"
PENDING_FILE = DATA_DIR / "workbuddy_pending_orders.json"
TRADE_LOG_FILE = DATA_DIR / "workbuddy_trade_api_log.jsonl"
BUY_PLAN_FILE = DATA_DIR / "workbuddy_buy_plan_latest.json"


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


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


def _find_record_index(records: list[dict[str, Any]], code: str, statuses: tuple[str, ...] = ("holding", "paused")) -> int | None:
    norm_code = _normalize_code(code)
    for idx in range(len(records) - 1, -1, -1):
        item = _normalize_record(records[idx])
        if _normalize_code(item.get("code", "")) == norm_code and str(item.get("status", "")).strip() in statuses:
            return idx
    return None


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


def load_pending_orders() -> list[dict[str, Any]]:
    payload = _read_json(PENDING_FILE)
    return payload if isinstance(payload, list) else []


def save_pending_orders(items: list[dict[str, Any]]) -> None:
    _write_json_atomic(PENDING_FILE, items)


def _load_source_payload() -> dict[str, Any]:
    payload = _read_json(SOURCE_FILE)
    return payload if isinstance(payload, dict) else {}


def _load_today_tradability_exclusions() -> dict[str, dict[str, Any]]:
    payload = _read_json(OPENING_TRADABILITY_FILE)
    return build_today_exclusion_map(payload if isinstance(payload, dict) else {})


def _log_trade_api(action: str, code: str, quantity: int, ref_price: float, result: dict[str, Any] | None) -> None:
    payload = {
        "logged_at": _now_str(),
        "portfolio_name": PORTFOLIO_NAME,
        "action": action,
        "code": _normalize_code(code),
        "quantity": int(quantity),
        "ref_price": round(ref_price, 4),
        "ok": bool(result and result.get("code") in ["0", 0, "200", 200]),
        "result_code": "" if not result else str(result.get("code", "")),
        "message": "" if not result else str(result.get("message", "")),
        "order_id": base._extract_order_id(result or {}),
        "raw": result or {},
    }
    _append_jsonl(TRADE_LOG_FILE, payload)


def register_pending_order(action: str, code: str, quantity: int, ref_price: float, order_id: str, *, context: dict[str, Any] | None = None) -> None:
    context = context or {}
    items = load_pending_orders()
    items.append(
        {
            "recorded_at": _now_str(),
            "portfolio_name": PORTFOLIO_NAME,
            "action": str(action).strip(),
            "code": _normalize_code(code),
            "name": str(context.get("name", "")).strip(),
            "tier": str(context.get("tier", "")).strip(),
            "mode": str(context.get("mode", "")).strip(),
            "build_note": str(context.get("build_note", "")).strip(),
            "target_amount": str(context.get("target_amount", "")).strip(),
            "close_reason": str(context.get("close_reason", "")).strip(),
            "quantity": int(quantity),
            "ref_price": round(ref_price, 4),
            "order_id": str(order_id or "").strip(),
            "status": "submitted",
            "filled_quantity": 0,
            "filled_at": "",
            "stale": False,
            "message": "",
        }
    )
    save_pending_orders(items[-200:])


def _trade_stock(action: str, code: str, quantity: int, *, ref_price: float = 0.0, context: dict[str, Any] | None = None) -> tuple[bool, str, dict[str, Any] | None]:
    result = base.api_request(
        "/api/claw/mockTrading/trade",
        {
            "type": action,
            "stockCode": _normalize_code(code),
            "quantity": int(quantity),
            "useMarketPrice": True,
        },
        is_trade=True,
    )
    _log_trade_api(action, code, quantity, ref_price, result)
    ok = bool(result and result.get("code") in ["0", 0, "200", 200])
    order_id = base._extract_order_id(result or {})
    if ok:
        register_pending_order(action, code, quantity, ref_price, order_id, context=context)
    return ok, order_id, result


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


def reconcile_orders(records: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    records = [_normalize_record(row) for row in (records if records is not None else load_track_record())]
    pending_items = load_pending_orders()
    if not pending_items:
        return records, [], False

    orders = base.get_orders()
    order_by_id = {str(item.get("id", "")).strip(): item for item in orders if str(item.get("id", "")).strip()}
    changed = False
    now = datetime.now()

    for item in pending_items:
        order_id = str(item.get("order_id", "")).strip()
        order = order_by_id.get(order_id)
        prev_filled = _safe_int(item.get("filled_quantity", 0), 0)
        status = str(item.get("status", "submitted")).strip() or "submitted"
        stale = False
        filled_at = str(item.get("filled_at", "")).strip()

        if order:
            filled_qty = max(prev_filled, _safe_int(order.get("trade_count", 0), 0))
            if _safe_int(order.get("status", 0), 0) == 4 or (
                _safe_int(order.get("count", 0), 0) > 0 and filled_qty >= _safe_int(order.get("count", 0), 0)
            ):
                status = "filled"
            elif filled_qty > 0:
                status = "partial"
            item["filled_quantity"] = filled_qty
            if order.get("datetime"):
                filled_at = order["datetime"].strftime("%Y-%m-%d %H:%M:%S")
        else:
            filled_qty = prev_filled

        recorded_at = base._parse_dt(item.get("recorded_at"))
        if status not in {"filled", "cancelled", "rejected"} and recorded_at is not None:
            age_minutes = (now - recorded_at).total_seconds() / 60
            if age_minutes >= base.PENDING_STALE_MINUTES:
                stale = True
                status = "stale"
                item["message"] = str(item.get("message", "")).strip() or f"pending>{base.PENDING_STALE_MINUTES}m"

        code = _normalize_code(item.get("code", ""))
        action = str(item.get("action", "")).strip()
        if action == "buy" and filled_qty > prev_filled:
            delta_qty = filled_qty - prev_filled
            idx = _find_record_index(records, code)
            if idx is None:
                records.append(
                    _empty_record(
                        code=code,
                        name=str(item.get("name", "")).strip(),
                        tier=_safe_int(item.get("tier", 0), 0),
                        mode=str(item.get("mode", "")).strip(),
                        build_note=str(item.get("build_note", "")).strip(),
                        target_amount=_safe_float(item.get("target_amount", 0), 0.0),
                        trade_date=(order.get("datetime").strftime("%Y-%m-%d") if order and order.get("datetime") else _today_str()),
                    )
                )
                idx = len(records) - 1
            fill_payload = {
                "order_id": order_id,
                "trade_time": order["datetime"].strftime("%H:%M:%S") if order and order.get("datetime") else "",
                "trade_date": order["datetime"].strftime("%Y-%m-%d") if order and order.get("datetime") else _today_str(),
                "trade_price": _safe_float((order or {}).get("trade_price", 0.0), _safe_float(item.get("ref_price", 0.0), 0.0)),
                "trade_count": delta_qty,
            }
            records[idx] = base.apply_buy_fill(
                records[idx],
                fill_payload,
                fallback_price=_safe_float(item.get("ref_price", 0.0), 0.0),
                fallback_quantity=delta_qty,
            )
            changed = True

        if action == "sell" and status == "filled":
            idx = _find_record_index(records, code)
            if idx is not None and str(records[idx].get("sell_order_id", "")).strip() != order_id:
                fill_payload = {
                    "order_id": order_id,
                    "trade_time": order["datetime"].strftime("%H:%M:%S") if order and order.get("datetime") else "",
                    "trade_date": order["datetime"].strftime("%Y-%m-%d") if order and order.get("datetime") else _today_str(),
                    "trade_price": _safe_float((order or {}).get("trade_price", 0.0), _safe_float(item.get("ref_price", 0.0), 0.0)),
                }
                records[idx] = base.apply_sell_fill(
                    records[idx],
                    fill_payload,
                    fallback_price=_safe_float(item.get("ref_price", 0.0), 0.0),
                    close_reason=str(item.get("close_reason", "")).strip() or "workbuddy_manual_sell",
                )
                changed = True

        item["status"] = status
        item["stale"] = stale
        item["filled_at"] = filled_at
        item["last_checked_at"] = _now_str()

    if changed:
        save_track_record(records)
    save_pending_orders(pending_items)
    return records, pending_items, changed


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


def _pending_active_codes(items: list[dict[str, Any]], *, action: str) -> set[str]:
    return {
        _normalize_code(item.get("code", ""))
        for item in items
        if str(item.get("action", "")).strip() == action and str(item.get("status", "")).strip() in {"submitted", "partial", "stale"}
    }


def _workbuddy_tier(selection_rank: int) -> int:
    if selection_rank <= 2:
        return 1
    if selection_rank <= 4:
        return 2
    return 3


def build_buy_plan() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    payload = _load_source_payload()
    if payload.get("status") != "ok":
        raise RuntimeError("mx_workbuddy_portfolio_latest.json 不可用")
    selected_records = payload.get("selected_records", [])
    if not isinstance(selected_records, list) or not selected_records:
        raise RuntimeError("workbuddy 候选池为空")

    balance = base.get_balance()
    if not balance:
        raise RuntimeError("无法获取 mx-moni 账户资金")

    records, pending_items, _ = reconcile_orders(load_track_record())
    holding_codes = {
        _normalize_code(item.get("code", ""))
        for item in records
        if str(item.get("status", "")).strip() == "holding"
    }
    active_buy_codes = _pending_active_codes(pending_items, action="buy")
    exclusions = _load_today_tradability_exclusions()
    quote_map = load_quote_map([row.get("code", "") for row in selected_records])

    avail = _safe_float(balance.get("avail_balance", 0.0), 0.0)
    total_assets = _safe_float(balance.get("total_assets", 0.0), 0.0)
    buy_list: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for row in selected_records:
        code = _normalize_code(row.get("code", ""))
        name = str(row.get("name", "")).strip()
        if not code:
            continue

        market_info = resolve_market_info(code)
        if not market_info.get("tradable_by_current_executor"):
            skipped.append({"code": code, "name": name, "reason": "unsupported_market_current_executor"})
            continue
        if code in exclusions:
            skipped.append({"code": code, "name": name, "reason": exclusion_reason_text(exclusions[code])})
            continue
        if code in holding_codes:
            skipped.append({"code": code, "name": name, "reason": "already_holding_in_workbuddy"})
            continue
        if code in active_buy_codes:
            skipped.append({"code": code, "name": name, "reason": "active_pending_buy_exists"})
            continue

        ref_price = _quote_price(quote_map.get(code))
        if ref_price <= 0:
            skipped.append({"code": code, "name": name, "reason": "quote_unavailable"})
            continue

        target_weight_pct = _safe_float(row.get("target_weight_pct", 0.0), 0.0)
        target_amount = total_assets * target_weight_pct / 100 if target_weight_pct > 0 else 0.0
        allowed_amount = min(target_amount, avail)
        quantity = base.calc_buy_quantity(ref_price, allowed_amount)
        if quantity <= 0:
            skipped.append({"code": code, "name": name, "reason": "insufficient_available_cash"})
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
                "mode": f"workbuddy_{str(row.get('role', 'challenger')).strip() or 'challenger'}",
                "selection_rank": selection_rank,
                "selection_score": _safe_float(row.get("selection_score", 0.0), 0.0),
                "target_weight_pct": target_weight_pct,
                "target_amount": round(target_amount, 2),
                "entry_price": round(ref_price, 4),
                "quantity": quantity,
                "cost": cost,
                "build_note": f"MX challenger direct buy | rank={selection_rank or '?'} | score={_safe_float(row.get('selection_score', 0.0), 0.0):.2f}",
                "selection_reasons": list(row.get("selection_reasons", []) or []),
            }
        )

    plan_payload = {
        "generated_at": _now_str(),
        "portfolio_name": PORTFOLIO_NAME,
        "source_file": str(SOURCE_FILE),
        "source_trade_date": str(payload.get("trade_date", "")).strip(),
        "source_run_slot": str(payload.get("run_slot", "")).strip(),
        "account_reference": {
            "total_assets": round(total_assets, 2),
            "avail_balance": round(_safe_float(balance.get("avail_balance", 0.0), 0.0), 2),
            "note": "workbuddy 账本独立，但底层 mx-moni 资金接口仍是全局共享。",
        },
        "buy_candidate_count": len(buy_list),
        "skipped_count": len(skipped),
        "buy_candidates": buy_list,
        "skipped": skipped,
    }
    _write_json_atomic(BUY_PLAN_FILE, plan_payload)
    return plan_payload, buy_list, skipped


def _holding_rows(records: list[dict[str, Any]], quote_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        if str(record.get("status", "")).strip() != "holding":
            continue
        code = _normalize_code(record.get("code", ""))
        entry_price = _safe_float(record.get("entry_price", 0.0), 0.0)
        quantity = _safe_int(record.get("quantity", 0), 0)
        current_price = _quote_price(quote_map.get(code))
        market_value = round(current_price * quantity, 2) if current_price > 0 else 0.0
        cost_value = round(entry_price * quantity, 2)
        floating_pnl = round(market_value - cost_value, 2) if market_value > 0 else 0.0
        floating_pnl_pct = round((current_price / entry_price - 1) * 100, 2) if current_price > 0 and entry_price > 0 else 0.0
        try:
            hold_days = (datetime.now() - datetime.strptime(str(record.get("date", "")), "%Y-%m-%d")).days
        except Exception:
            hold_days = 0
        rows.append(
            {
                "code": code,
                "name": str(record.get("name", "")).strip(),
                "tier": _safe_int(record.get("tier", 0), 0),
                "mode": str(record.get("mode", "")).strip(),
                "quantity": quantity,
                "entry_price": round(entry_price, 4),
                "current_price": round(current_price, 4),
                "market_value": market_value,
                "floating_pnl": floating_pnl,
                "floating_pnl_pct": floating_pnl_pct,
                "hold_days": hold_days,
            }
        )
    rows.sort(key=lambda item: (item["floating_pnl_pct"], item["code"]), reverse=True)
    return rows


def write_account_summary(tag: str, records: list[dict[str, Any]], pending_items: list[dict[str, Any]]) -> dict[str, Any]:
    quote_map = load_quote_map([row.get("code", "") for row in records if str(row.get("status", "")).strip() == "holding"])
    holding_rows = _holding_rows(records, quote_map)
    stats = base.compute_track_stats(records)
    floating_pnl = round(sum(item["floating_pnl"] for item in holding_rows), 2)
    market_value = round(sum(item["market_value"] for item in holding_rows), 2)
    balance = base.get_balance() or {}

    summary = {
        "generated_at": _now_str(),
        "tag": tag,
        "portfolio_name": PORTFOLIO_NAME,
        "execution_scope": "logical_subportfolio_on_shared_mx_moni_account",
        "source_file": str(SOURCE_FILE),
        "account_reference": {
            "global_total_assets": round(_safe_float(balance.get("total_assets", 0.0), 0.0), 2),
            "global_avail_balance": round(_safe_float(balance.get("avail_balance", 0.0), 0.0), 2),
            "global_total_pos_value": round(_safe_float(balance.get("total_pos_value", 0.0), 0.0), 2),
            "note": "mx-moni 当前未发现 portfolioId/accountId 切换参数，底层账户/持仓接口仍是全局共享。",
        },
        "portfolio_snapshot": {
            "holding_count": len(holding_rows),
            "closed_count": stats["closed_count"],
            "market_value": market_value,
            "floating_pnl": floating_pnl,
            "realized_pnl": stats["realized_pnl"],
            "win_rate_pct": stats["win_rate_pct"],
            "avg_return_pct": stats["avg_return_pct"],
        },
        "pending_orders": {
            "active_buy_codes": sorted(_pending_active_codes(pending_items, action="buy")),
            "active_sell_codes": sorted(_pending_active_codes(pending_items, action="sell")),
            "items": pending_items[-20:],
        },
        "holdings": holding_rows,
        "notes": [
            "workbuddy 账本、pending、摘要与主交易分开维护。",
            "workbuddy 当前按自身委托号回收成交，不拿全局持仓去自动导入本组合。",
            "若主交易与 workbuddy 在底层共享账户上交易同一代码，仍需人工复核避免物理仓位混淆。",
        ],
        "files": {
            "track_file": str(TRACK_FILE),
            "summary_file": str(SUMMARY_FILE),
            "nav_file": str(NAV_FILE),
            "pending_file": str(PENDING_FILE),
            "trade_log_file": str(TRADE_LOG_FILE),
        },
    }
    _write_json_atomic(SUMMARY_FILE, summary)
    _write_csv_row(
        NAV_FILE,
        [
            "date",
            "time",
            "tag",
            "holding_count",
            "closed_count",
            "market_value",
            "floating_pnl",
            "realized_pnl",
            "win_rate_pct",
            "avg_return_pct",
            "global_total_assets",
            "global_avail_balance",
        ],
        {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "time": datetime.now().strftime("%H:%M:%S"),
            "tag": tag,
            "holding_count": len(holding_rows),
            "closed_count": stats["closed_count"],
            "market_value": market_value,
            "floating_pnl": floating_pnl,
            "realized_pnl": stats["realized_pnl"],
            "win_rate_pct": stats["win_rate_pct"],
            "avg_return_pct": stats["avg_return_pct"],
            "global_total_assets": round(_safe_float(balance.get("total_assets", 0.0), 0.0), 2),
            "global_avail_balance": round(_safe_float(balance.get("avail_balance", 0.0), 0.0), 2),
        },
    )
    return summary


def do_buy(*, dry_run: bool = False) -> int:
    if not base.ensure_trade_window("buy", dry_run=dry_run):
        return base.EXIT_WINDOW_SKIPPED

    plan_payload, buy_list, skipped = build_buy_plan()
    print(f"\n{'=' * 60}")
    print(f" WorkBuddy 独立下单 {'[DRY RUN]' if dry_run else '[LIVE]'}")
    print(f" 来源池: {SOURCE_FILE.name} | 候选{len(buy_list)}只 | 跳过{len(skipped)}只")
    print(f"{'=' * 60}")

    if skipped:
        for item in skipped[:10]:
            print(f"  [SKIP] {item['code']} {item['name']} | {item['reason']}")
    if not buy_list:
        print(" 没有形成可买清单")
        return base.EXIT_NO_ACTION

    success_count = 0
    for item in buy_list:
        reasons = " / ".join(item.get("selection_reasons", [])[:2])
        if dry_run:
            print(
                f"  [DRY] T{item['tier']} {item['code']} {item['name']} "
                f"¥{item['entry_price']:.2f} x {item['quantity']}股 "
                f"≈¥{item['cost']:,.0f} | {reasons}"
            )
            continue

        print(
            f"  T{item['tier']} {item['code']} {item['name']} "
            f"¥{item['entry_price']:.2f} x {item['quantity']}股 "
            f"≈¥{item['cost']:,.0f} | {reasons}"
        )
        ok, order_id, result = _trade_stock(
            "buy",
            item["code"],
            item["quantity"],
            ref_price=item["entry_price"],
            context={
                "name": item["name"],
                "tier": item["tier"],
                "mode": item["mode"],
                "build_note": item["build_note"],
                "target_amount": f"{item['target_amount']:.0f}",
            },
        )
        if ok:
            success_count += 1
            print(f"    -> 委托成功 order_id={order_id}")
        else:
            message = "" if not result else str(result.get("message", ""))
            print(f"    -> 委托失败: {message or 'unknown_error'}")

    if dry_run:
        return base.EXIT_OK

    time.sleep(2.0)
    records, pending_items, _ = reconcile_orders(load_track_record())
    write_account_summary("buy", records, pending_items)
    return base.EXIT_OK if success_count > 0 else base.EXIT_RUNTIME_ERROR


def do_sell(*, dry_run: bool = False) -> int:
    if not base.ensure_trade_window("sell", dry_run=dry_run):
        return base.EXIT_WINDOW_SKIPPED

    records, pending_items, _ = reconcile_orders(load_track_record())
    active_sell_codes = _pending_active_codes(pending_items, action="sell")
    holdings = [row for row in records if str(row.get("status", "")).strip() == "holding"]
    if not holdings:
        print(" WorkBuddy 当前无持仓")
        return base.EXIT_NO_ACTION

    exclusions = _load_today_tradability_exclusions()
    quote_map = load_quote_map([row.get("code", "") for row in holdings])
    sell_list: list[dict[str, Any]] = []
    skipped: list[str] = []
    today = _today_str()

    for record in holdings:
        code = _normalize_code(record.get("code", ""))
        name = str(record.get("name", "")).strip()
        if code in active_sell_codes:
            skipped.append(f"{code} 已有未完成卖单")
            continue
        if code in exclusions:
            skipped.append(f"{code} {exclusion_reason_text(exclusions[code])}")
            continue
        try:
            hold_days = (datetime.now() - datetime.strptime(str(record.get("date", today)), "%Y-%m-%d")).days
        except Exception:
            hold_days = 0
        if hold_days < 5:
            skipped.append(f"{code} 持仓{hold_days}天<5")
            continue
        ref_price = _quote_price(quote_map.get(code)) or _safe_float(record.get("entry_price", 0.0), 0.0)
        quantity = _safe_int(record.get("quantity", 0), 0)
        if quantity < 100:
            skipped.append(f"{code} 数量不足100股")
            continue
        sell_list.append(
            {
                "code": code,
                "name": name,
                "quantity": quantity,
                "ref_price": ref_price,
                "close_reason": f"workbuddy_t5_exit_hold_days={hold_days}",
            }
        )

    print(f"\n{'=' * 60}")
    print(f" WorkBuddy 独立卖出 {'[DRY RUN]' if dry_run else '[LIVE]'}")
    print(f" 拟卖{len(sell_list)}只 | 跳过{len(skipped)}只")
    print(f"{'=' * 60}")
    for item in skipped[:10]:
        print(f"  [SKIP] {item}")

    if not sell_list:
        return base.EXIT_NO_ACTION

    success_count = 0
    for item in sell_list:
        if dry_run:
            print(f"  [DRY] {item['code']} {item['name']} x {item['quantity']}股")
            continue
        ok, order_id, result = _trade_stock(
            "sell",
            item["code"],
            item["quantity"],
            ref_price=item["ref_price"],
            context={
                "name": item["name"],
                "close_reason": item["close_reason"],
            },
        )
        if ok:
            success_count += 1
            print(f"  {item['code']} {item['name']} -> 委托成功 order_id={order_id}")
        else:
            message = "" if not result else str(result.get("message", ""))
            print(f"  {item['code']} {item['name']} -> 委托失败: {message or 'unknown_error'}")

    if dry_run:
        return base.EXIT_OK

    time.sleep(2.0)
    records, pending_items, _ = reconcile_orders(load_track_record())
    write_account_summary("sell", records, pending_items)
    return base.EXIT_OK if success_count > 0 else base.EXIT_RUNTIME_ERROR


def do_status() -> int:
    records, pending_items, _ = reconcile_orders(load_track_record())
    summary = write_account_summary("status", records, pending_items)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return base.EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description="WorkBuddy 独立下单")
    parser.add_argument("--buy", action="store_true", help="按 workbuddy 最新候选池下单")
    parser.add_argument("--sell", action="store_true", help="按 workbuddy 持仓执行 T+5 卖出")
    parser.add_argument("--status", action="store_true", help="查看 workbuddy 独立账本状态")
    parser.add_argument("--dry-run", action="store_true", help="模拟运行，不实际下单")
    args = parser.parse_args()

    if args.buy:
        return do_buy(dry_run=args.dry_run)
    if args.sell:
        return do_sell(dry_run=args.dry_run)
    if args.status:
        return do_status()

    parser.print_help()
    return base.EXIT_CONFIG_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
