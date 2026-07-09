from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from package_paths import DATA_DIR


SECURITY_MASTER_FILE = DATA_DIR / "security_master_latest.json"
OPENING_TRADABILITY_FILE = DATA_DIR / "opening_tradability_latest.json"


def _read_json(path: Path) -> dict:
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


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _normalize_code(code) -> str:
    text = str(code or "").strip()
    if "." in text:
        text = text.split(".", 1)[0]
    return text.zfill(6)


def fallback_market_info(code: str) -> dict:
    norm_code = _normalize_code(code)
    market_char = ""
    exchange = ""
    market_tdx = None
    supported = False
    reason = "unknown_code_prefix"
    if norm_code.startswith(("6", "9")):
        market_char = ".SH"
        exchange = "SSE"
        market_tdx = 1
        supported = True
        reason = "legacy_prefix_sh"
    elif norm_code.startswith(("0", "2", "3")):
        market_char = ".SZ"
        exchange = "SZSE"
        market_tdx = 0
        supported = True
        reason = "legacy_prefix_sz"
    elif norm_code.startswith(("4", "8")) or norm_code.startswith("92"):
        market_char = ".BJ"
        exchange = "BSE"
        market_tdx = None
        supported = False
        reason = "legacy_prefix_bj"
    return {
        "code": norm_code,
        "market_char": market_char,
        "exchange": exchange,
        "market_tdx": market_tdx,
        "tradable_by_current_executor": supported,
        "resolver_source": "fallback_prefix",
        "resolver_detail": reason,
    }


def load_security_master() -> dict:
    return _read_json(SECURITY_MASTER_FILE)


def build_security_master_map(payload: dict | None = None) -> dict[str, dict]:
    payload = payload if payload is not None else load_security_master()
    mapping: dict[str, dict] = {}
    for item in payload.get("records", []):
        code = _normalize_code(item.get("code", ""))
        if code:
            mapping[code] = dict(item)
    return mapping


def resolve_market_info(code: str, payload: dict | None = None) -> dict:
    norm_code = _normalize_code(code)
    mapping = build_security_master_map(payload)
    if norm_code in mapping:
        resolved = dict(mapping[norm_code])
        resolved.setdefault("resolver_source", resolved.get("mapping_source", "security_master"))
        resolved.setdefault("resolver_detail", "security_master_hit")
        return resolved
    return fallback_market_info(norm_code)


def load_opening_tradability() -> dict:
    return _read_json(OPENING_TRADABILITY_FILE)


def build_tradability_map(payload: dict | None = None, *, today_only: bool = True) -> dict[str, dict]:
    payload = payload if payload is not None else load_opening_tradability()
    if today_only and str(payload.get("trade_date", "")).strip() != _today_str():
        return {}
    mapping: dict[str, dict] = {}
    for item in payload.get("records", []):
        code = _normalize_code(item.get("code", ""))
        if code:
            mapping[code] = dict(item)
    return mapping


def build_today_exclusion_map(payload: dict | None = None) -> dict[str, dict]:
    tradability_map = build_tradability_map(payload, today_only=True)
    return {
        code: item
        for code, item in tradability_map.items()
        if str(item.get("executor_action", "")).strip() == "exclude_today_buy_sell"
    }


def exclusion_reason_text(item: dict | None) -> str:
    if not item:
        return ""
    status = str(item.get("tradability_status", "")).strip()
    summary = str(item.get("summary", "")).strip()
    if summary:
        return summary
    return status or "exclude_today_buy_sell"
