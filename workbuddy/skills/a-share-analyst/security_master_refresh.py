#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""09:31 晨间数据任务：证券主数据映射 + 当日流动性判断。

设计目标：
1. 每个交易日 09:31 刷新本地 security master；
2. 对当日交易对象做 09:31 开盘后可交易性判断；
3. 今日剔除仅对今日有效，下一交易日重新进队判断；
4. 执行层读取本地缓存，不在临场买卖时在线查 MX。
"""

from __future__ import annotations

import csv
import importlib.util
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from package_paths import DATA_DIR
from market_resolver import fallback_market_info


SECURITY_MASTER_JSON = DATA_DIR / "security_master_latest.json"
SECURITY_MASTER_CSV = DATA_DIR / "security_master_latest.csv"
OPENING_TRADABILITY_JSON = DATA_DIR / "opening_tradability_latest.json"
OPENING_TRADABILITY_CSV = DATA_DIR / "opening_tradability_latest.csv"
OPENING_TRADABILITY_HISTORY = DATA_DIR / "automation_status" / "opening_tradability_history.jsonl"

TRACK_FILE = DATA_DIR / "v10_track_record.csv"
PENDING_FILE = DATA_DIR / "v10_pending_orders.json"
CHALLENGER_FILE = DATA_DIR / "mx_challenger_pool_latest.json"
WORKBUDDY_FILE = DATA_DIR / "mx_workbuddy_portfolio_latest.json"
ARKCLAW_ROOT = Path(
    os.environ.get("TLFZ_ARKCLAW_ROOT", str(Path(__file__).resolve().parents[3]))
)
WORKBUDDY_CANDIDATE_POOL_FILE = ARKCLAW_ROOT / "workbuddy_pool" / "workbuddy_candidate_pool_latest.json"
MX_DATA_SKILL = Path.home() / ".trae" / "skills" / "mx-data" / "mx_data.py"


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _read_json(path: Path):
    if not path.exists():
        return {}
    for encoding in ("utf-8", "utf-8-sig"):
        try:
            with path.open("r", encoding=encoding) as f:
                return json.load(f)
        except Exception:
            continue
    return {}


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _normalize_code(value) -> str:
    text = str(value or "").strip()
    if "." in text:
        text = text.split(".", 1)[0]
    return text.zfill(6)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        text = str(value).strip()
        return float(text) if text else default
    except Exception:
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        text = str(value).strip()
        return int(float(text)) if text else default
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


def _load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_track_codes() -> list[tuple[str, str]]:
    if not TRACK_FILE.exists():
        return []
    rows: list[tuple[str, str]] = []
    with TRACK_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if str(row.get("status", "")).strip() != "holding":
                continue
            code = _normalize_code(row.get("code", ""))
            name = str(row.get("name", "")).strip()
            if code:
                rows.append((code, name))
    return rows


def _load_pending_codes() -> list[tuple[str, str]]:
    payload = _read_json(PENDING_FILE)
    rows: list[tuple[str, str]] = []
    if not isinstance(payload, list):
        return rows
    for item in payload:
        code = _normalize_code((item or {}).get("code", ""))
        if code:
            rows.append((code, ""))
    return rows


def _load_challenger_codes() -> list[dict]:
    payload = _read_json(CHALLENGER_FILE)
    rows: list[dict] = []
    for item in payload.get("records", []):
        code = _normalize_code(item.get("股票代码") or item.get("代码"))
        if not code:
            continue
        rows.append(
            {
                "code": code,
                "name": str(item.get("股票简称") or item.get("名称") or "").strip(),
                "market_tag": str(item.get("市场代码简称", "")).strip().upper(),
            }
        )
    return rows


def _load_workbuddy_codes() -> list[tuple[str, str]]:
    payload = _read_json(WORKBUDDY_FILE)
    rows: list[tuple[str, str]] = []
    for item in payload.get("selected_records", []):
        code = _normalize_code(item.get("code", ""))
        name = str(item.get("name", "")).strip()
        if code:
            rows.append((code, name))
    return rows


def _load_workbuddy_candidate_pool_codes() -> list[tuple[str, str]]:
    payload = _read_json(WORKBUDDY_CANDIDATE_POOL_FILE)
    rows: list[tuple[str, str]] = []
    for item in payload.get("selected_records", []):
        code = _normalize_code((item or {}).get("code", ""))
        name = str((item or {}).get("name", "")).strip()
        if code:
            rows.append((code, name))
    return rows


def _infer_from_market_tag(code: str, name: str, market_tag: str) -> dict:
    mapping = {
        "SH": (".SH", "SSE", 1, True),
        "SZ": (".SZ", "SZSE", 0, True),
        "BJ": (".BJ", "BSE", None, False),
    }
    market_char, exchange, market_tdx, supported = mapping.get(
        market_tag,
        ("", "", None, False),
    )
    return {
        "code": code,
        "name": name,
        "market_char": market_char,
        "exchange": exchange,
        "market_tdx": market_tdx,
        "entity_type_name": "",
        "class_name": "",
        "tradable_by_current_executor": supported,
        "mapping_source": "challenger_market_tag",
        "mapping_detail": f"market_tag:{market_tag}" if market_tag else "market_tag:unknown",
    }


def _extract_mx_entity_tag(result: dict) -> dict:
    data = (result.get("data") or {}).get("data") or {}
    search_result = data.get("searchDataResultDTO") or {}
    tags = search_result.get("entityTagDTOList") or []
    if not isinstance(tags, list):
        return {}
    for item in tags:
        if not isinstance(item, dict):
            continue
        secu_code = _normalize_code(item.get("secuCode", ""))
        if not secu_code:
            continue
        market_char = str(item.get("marketChar", "")).strip().upper()
        exchange = {
            ".SH": "SSE",
            ".SZ": "SZSE",
            ".BJ": "BSE",
        }.get(market_char, "")
        market_tdx = 1 if market_char == ".SH" else 0 if market_char == ".SZ" else None
        supported = market_char in {".SH", ".SZ"}
        return {
            "code": secu_code,
            "name": str(item.get("fullName", "")).strip(),
            "market_char": market_char,
            "exchange": exchange,
            "market_tdx": market_tdx,
            "entity_type_name": str(item.get("entityTypeName", "")).strip(),
            "class_name": str(item.get("className", "")).strip(),
            "tradable_by_current_executor": supported,
            "mapping_source": "mx_entity_tag",
            "mapping_detail": "mx-data entityTagDTOList",
        }
    return {}


def _query_mx_mapping(code: str) -> dict:
    if not MX_DATA_SKILL.exists():
        return {}
    try:
        mod = _load_module(MX_DATA_SKILL, "mx_data_runtime")
        client = mod.MXData()
        result = client.query(f"{code} 最新价")
        mapping = _extract_mx_entity_tag(result)
        if mapping:
            return mapping
    except Exception:
        return {}
    return {}


def _scanner_universe_records() -> list[dict]:
    import scanner_v10

    rows = scanner_v10.get_stock_list()
    records: list[dict] = []
    for _, row in rows.iterrows():
        code = _normalize_code(row.get("code", ""))
        market = _safe_int(row.get("market", 0), 0)
        if not code:
            continue
        records.append(
            {
                "code": code,
                "name": str(row.get("name", "")).strip(),
                "market_char": ".SH" if market == 1 else ".SZ",
                "exchange": "SSE" if market == 1 else "SZSE",
                "market_tdx": market,
                "entity_type_name": "A股",
                "class_name": "沪深京股票",
                "tradable_by_current_executor": True,
                "mapping_source": "scanner_exchange_universe",
                "mapping_detail": "scanner_v10.get_stock_list",
            }
        )
    return records


def build_security_master() -> list[dict]:
    records_by_code: dict[str, dict] = {}
    source_refs: dict[str, set[str]] = defaultdict(set)

    for item in _scanner_universe_records():
        code = item["code"]
        records_by_code[code] = item
        source_refs[code].add("scanner_universe")

    extras: dict[str, dict] = {}
    for code, name in _load_track_codes():
        extras.setdefault(code, {"code": code, "name": name, "source": "track_holding"})
    for code, name in _load_pending_codes():
        extras.setdefault(code, {"code": code, "name": name, "source": "pending_orders"})
    for item in _load_challenger_codes():
        code = item["code"]
        extras.setdefault(code, {"code": code, "name": item["name"], "source": "mx_challenger"})
        extras[code]["market_tag"] = item.get("market_tag", "")
    for code, name in _load_workbuddy_codes():
        extras.setdefault(code, {"code": code, "name": name, "source": "workbuddy_shadow"})
    for code, name in _load_workbuddy_candidate_pool_codes():
        extras.setdefault(code, {"code": code, "name": name, "source": "workbuddy_candidate_pool"})

    for code, item in extras.items():
        if code in records_by_code:
            if item.get("name") and not records_by_code[code].get("name"):
                records_by_code[code]["name"] = item["name"]
            source_refs[code].add(item.get("source", "extra"))
            continue
        mapped = {}
        market_tag = str(item.get("market_tag", "")).strip().upper()
        if market_tag:
            mapped = _infer_from_market_tag(code, item.get("name", ""), market_tag)
        if not mapped:
            mapped = _query_mx_mapping(code)
        if not mapped:
            mapped = fallback_market_info(code)
            mapped["name"] = item.get("name", "")
        if item.get("name") and not mapped.get("name"):
            mapped["name"] = item["name"]
        records_by_code[code] = mapped
        source_refs[code].add(item.get("source", "extra"))

    rows: list[dict] = []
    for code in sorted(records_by_code):
        item = dict(records_by_code[code])
        item["code"] = code
        item["source_refs"] = sorted(source_refs.get(code, []))
        rows.append(item)
    return rows


def _connect_tdx():
    import scanner_v10

    return scanner_v10.connect_tdx()


def _quote_batches(api, records: list[dict]) -> dict[str, dict]:
    grouped: dict[int, list[str]] = defaultdict(list)
    for item in records:
        market = item.get("market_tdx")
        if market in (0, 1):
            grouped[int(market)].append(item["code"])
    quote_map: dict[str, dict] = {}
    for market, codes in grouped.items():
        for start in range(0, len(codes), 80):
            batch = [(market, code) for code in codes[start:start + 80]]
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


def build_opening_tradability(records: list[dict]) -> list[dict]:
    tradability_rows: list[dict] = []
    tradable_scope = [item for item in records if item.get("tradable_by_current_executor")]
    api = None
    quote_map: dict[str, dict] = {}
    api_error = ""
    try:
        api = _connect_tdx()
        quote_map = _quote_batches(api, tradable_scope)
    except Exception as exc:
        api_error = str(exc)
    finally:
        if api is not None:
            try:
                api.disconnect()
            except Exception:
                pass

    for item in records:
        code = item["code"]
        is_risk_warning = _is_risk_warning_name(item.get("name", ""))
        base = {
            "code": code,
            "name": item.get("name", ""),
            "market_char": item.get("market_char", ""),
            "exchange": item.get("exchange", ""),
            "entity_type_name": item.get("entity_type_name", ""),
            "class_name": item.get("class_name", ""),
            "risk_warning": is_risk_warning,
            "tradable_by_current_executor": bool(item.get("tradable_by_current_executor")),
            "mapping_source": item.get("mapping_source", ""),
        }
        if is_risk_warning:
            tradability_rows.append(
                {
                    **base,
                    "tradability_status": "exclude_today_risk_warning_st",
                    "executor_action": "exclude_today_buy_sell",
                    "open_price": 0.0,
                    "last_price": 0.0,
                    "last_close": 0.0,
                    "volume": 0,
                    "amount": 0.0,
                    "summary": "命中 ST/*ST 风险警示口径，今日直接剔除自动买卖。",
                }
            )
            continue
        if not base["tradable_by_current_executor"]:
            tradability_rows.append(
                {
                    **base,
                    "tradability_status": "review_today_unsupported_market",
                    "executor_action": "review_only",
                    "open_price": 0.0,
                    "last_price": 0.0,
                    "last_close": 0.0,
                    "volume": 0,
                    "amount": 0.0,
                    "summary": "当前执行器尚未支持该市场，今日仅观察不自动交易。",
                }
            )
            continue

        quote = quote_map.get(code)
        if not quote:
            tradability_rows.append(
                {
                    **base,
                    "tradability_status": "review_today_data_incomplete",
                    "executor_action": "review_only",
                    "open_price": 0.0,
                    "last_price": 0.0,
                    "last_close": 0.0,
                    "volume": 0,
                    "amount": 0.0,
                    "summary": f"09:31 未拿到有效行情快照，今日先进入复核观察。{api_error}".strip(),
                }
            )
            continue

        open_price = _safe_float(quote.get("open", 0.0), 0.0)
        last_price = _safe_float(quote.get("price", 0.0), 0.0)
        last_close = _safe_float(quote.get("last_close", 0.0), 0.0)
        volume = _safe_int(quote.get("vol", 0), 0)
        amount = _safe_float(quote.get("amount", 0.0), 0.0)

        status = "tradable_today"
        action = "allow_today"
        summary = "09:31 已确认存在有效开盘与成交，当日允许自动交易。"
        if open_price <= 0 and last_price <= 0 and amount <= 0 and volume <= 0:
            status = "exclude_today_halt_or_no_open"
            action = "exclude_today_buy_sell"
            summary = "09:31 仍无有效开盘与成交，疑似停牌或今日无开盘交易，今日剔除自动买卖。"
        elif amount <= 0 and volume <= 0:
            status = "exclude_today_zero_turnover_0931"
            action = "exclude_today_buy_sell"
            summary = "09:31 仍为 0 成交，判定为今日开盘无流动性，今日剔除自动买卖。"

        tradability_rows.append(
            {
                **base,
                "tradability_status": status,
                "executor_action": action,
                "open_price": round(open_price, 4),
                "last_price": round(last_price, 4),
                "last_close": round(last_close, 4),
                "volume": volume,
                "amount": round(amount, 2),
                "summary": summary,
            }
        )
    tradability_rows.sort(key=lambda item: (item["executor_action"], item["code"]))
    return tradability_rows


def main() -> int:
    records = build_security_master()
    if not records:
        print("[ERROR] 未生成任何 security master 记录")
        return 3

    trade_date = _today_str()
    master_payload = {
        "generated_at": _now_str(),
        "trade_date": trade_date,
        "status": "ok",
        "record_count": len(records),
        "records": records,
        "notes": [
            "security master 负责提供代码到市场/证券类别的本地缓存，不在临场交易时在线查 MX。",
            "当日流动性判断与市场映射分层保存：映射相对稳定，流动性结论仅对今日有效。",
        ],
    }
    _write_json_atomic(SECURITY_MASTER_JSON, master_payload)
    _write_csv(SECURITY_MASTER_CSV, records)

    tradability_rows = build_opening_tradability(records)
    excluded = [item for item in tradability_rows if item.get("executor_action") == "exclude_today_buy_sell"]
    review_only = [item for item in tradability_rows if item.get("executor_action") == "review_only"]
    tradability_payload = {
        "generated_at": _now_str(),
        "trade_date": trade_date,
        "status": "ok",
        "record_count": len(tradability_rows),
        "excluded_today_count": len(excluded),
        "review_only_count": len(review_only),
        "excluded_today_codes": [item["code"] for item in excluded],
        "records": tradability_rows,
        "notes": [
            "09:31 开盘后流动性判断只对今日生效，下一交易日重新进队判断。",
            "早盘无竞价且 09:31 仍为 0 成交的标的，今日剔除自动买卖。",
            "ST/*ST 风险警示证券在 09:31 映射与流动性检查阶段直接剔除，不进入当日自动交易。",
            "当前执行器尚未支持的市场保留到预备军与复核层观察，不在此处赋予长期惩罚标签。",
        ],
    }
    _write_json_atomic(OPENING_TRADABILITY_JSON, tradability_payload)
    _write_csv(OPENING_TRADABILITY_CSV, tradability_rows)
    _append_jsonl(
        OPENING_TRADABILITY_HISTORY,
        {
            "generated_at": tradability_payload["generated_at"],
            "trade_date": trade_date,
            "record_count": len(tradability_rows),
            "excluded_today_count": len(excluded),
            "review_only_count": len(review_only),
            "excluded_today_codes": tradability_payload["excluded_today_codes"],
        },
    )
    print(
        json.dumps(
            {
                "trade_date": trade_date,
                "record_count": len(tradability_rows),
                "excluded_today_count": len(excluded),
                "review_only_count": len(review_only),
                "security_master_json": str(SECURITY_MASTER_JSON),
                "opening_tradability_json": str(OPENING_TRADABILITY_JSON),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
