#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交易窗口 TDX 数据新鲜度探针。

最小版原则：
1. 只做轻量检测，不做全市场扫描；
2. 只检查买卖窗口真正依赖的 TDX 新鲜度；
3. 先观察落盘，不直接阻断交易主链。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, time
from pathlib import Path

from package_paths import DATA_DIR
from trading_calendar import (
    CALENDAR_SOURCE,
    MARKET_CLOSE_TIME,
    is_trading_day,
    latest_completed_trading_day,
)

try:
    from pytdx.hq import TdxHq_API
except Exception:  # pragma: no cover - 环境缺依赖时留错误证据即可
    TdxHq_API = None


STATUS_DIR = DATA_DIR / "automation_status"
LATEST_FILE = DATA_DIR / "v10_data_freshness_latest.json"
HISTORY_FILE = STATUS_DIR / "data_freshness_history.jsonl"
TDX_HOSTS = [
    ("218.75.126.9", 7709),
    ("60.191.117.167", 7709),
    ("39.105.251.234", 7709),
    ("119.147.212.83", 7709),
]
SAMPLE_STOCK = {"market": 1, "code": "600519", "name": "贵州茅台"}
SAMPLE_INDEX = {"market": 1, "code": "000852", "name": "中证1000"}
PHASE_RULES = {
    "smart-sell": {"max_min5_lag_minutes": 20, "recommended_action": "warn_only"},
    "prewarm": {"max_min5_lag_minutes": 12, "recommended_action": "warn_only"},
    "decision": {"max_min5_lag_minutes": 10, "recommended_action": "block_buy_if_confirmed"},
    "buy": {"max_min5_lag_minutes": 10, "recommended_action": "block_buy_if_confirmed"},
}
TRADING_SESSION_WINDOWS = (
    (time(hour=9, minute=30), time(hour=11, minute=30)),
    (time(hour=13, minute=0), time(hour=15, minute=0)),
)


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def _parse_tdx_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _in_trading_session(now_value: datetime) -> bool:
    current = now_value.time()
    for start_at, end_at in TRADING_SESSION_WINDOWS:
        if start_at <= current <= end_at:
            return True
    return False


def _connect_first_available() -> tuple[object | None, dict]:
    attempts: list[dict] = []
    if TdxHq_API is None:
        return None, {
            "status": "missing_dependency",
            "host_used": "",
            "attempts": attempts,
            "detail": "pytdx is not available",
        }

    for host, port in TDX_HOSTS:
        api = TdxHq_API(heartbeat=True)
        item = {"host": host, "port": port, "connected": False, "error": ""}
        try:
            if api.connect(host, port, time_out=1.5):
                item["connected"] = True
                attempts.append(item)
                return api, {
                    "status": "ok",
                    "host_used": f"{host}:{port}",
                    "attempts": attempts,
                    "detail": "connected",
                }
        except Exception as exc:
            item["error"] = str(exc)
        finally:
            if not item["connected"]:
                try:
                    api.disconnect()
                except Exception:
                    pass
        attempts.append(item)
    return None, {
        "status": "connect_failed",
        "host_used": "",
        "attempts": attempts,
        "detail": "no available TDX host",
    }


def _fetch_latest_datetimes(api) -> dict:
    stock_daily = api.get_security_bars(9, SAMPLE_STOCK["market"], SAMPLE_STOCK["code"], 0, 5) or []
    stock_min5 = api.get_security_bars(0, SAMPLE_STOCK["market"], SAMPLE_STOCK["code"], 0, 20) or []
    index_daily = api.get_index_bars(9, SAMPLE_INDEX["market"], SAMPLE_INDEX["code"], 0, 5) or []
    quotes = api.get_security_quotes([(SAMPLE_STOCK["market"], SAMPLE_STOCK["code"])]) or []

    stock_daily_dt = max((_parse_tdx_datetime(item.get("datetime", "")) for item in stock_daily), default=None)
    stock_min5_dt = max((_parse_tdx_datetime(item.get("datetime", "")) for item in stock_min5), default=None)
    index_daily_dt = max((_parse_tdx_datetime(item.get("datetime", "")) for item in index_daily), default=None)
    quote = quotes[0] if quotes else {}

    return {
        "stock_daily_dt": stock_daily_dt,
        "stock_min5_dt": stock_min5_dt,
        "index_daily_dt": index_daily_dt,
        "quote": {
            "price": quote.get("price", ""),
            "last_close": quote.get("last_close", ""),
            "open": quote.get("open", ""),
            "vol": quote.get("vol", ""),
        },
    }


def _evaluate_probe(
    *,
    phase: str,
    task_name: str,
    trigger_slot: str,
    run_id: str,
) -> dict:
    now_dt = datetime.now()
    rule = PHASE_RULES.get(phase, {"max_min5_lag_minutes": 15, "recommended_action": "warn_only"})
    expected_daily_date = latest_completed_trading_day(now_dt).isoformat()
    acceptable_daily_dates = {expected_daily_date}
    if is_trading_day(now_dt):
        acceptable_daily_dates.add(now_dt.date().isoformat())

    payload = {
        "generated_at": _now_str(),
        "date": now_dt.strftime("%Y-%m-%d"),
        "calendar_source": CALENDAR_SOURCE,
        "phase": phase,
        "task_name": task_name,
        "trigger_slot": trigger_slot,
        "run_id": run_id,
        "probe_mode": "observe_only",
        "status": "ok",
        "recommended_action": rule["recommended_action"],
        "issues": [],
        "tdx_review": {
            "status": "ok",
            "connection": {},
            "daily": {},
            "min5": {},
            "index": {},
            "quote": {},
        },
        "notes": [
            "该探针贴着真实买卖窗口运行，用于证明当次交易所依赖的 TDX 数据是否足够新鲜。",
            "当前版本只做观察和落盘，不直接阻断交易主链。",
        ],
    }

    api, connection = _connect_first_available()
    payload["tdx_review"]["connection"] = connection
    if api is None:
        payload["status"] = "degraded"
        payload["tdx_review"]["status"] = "degraded"
        payload["issues"].append({
            "code": "tdx_connect_failed",
            "severity": "repair_required",
            "detail": connection.get("detail", "TDX connect failed"),
        })
        return payload

    try:
        snapshot = _fetch_latest_datetimes(api)
    finally:
        try:
            api.disconnect()
        except Exception:
            pass

    stock_daily_dt = snapshot["stock_daily_dt"]
    stock_min5_dt = snapshot["stock_min5_dt"]
    index_daily_dt = snapshot["index_daily_dt"]
    quote = snapshot["quote"]

    daily_status = "ok"
    if stock_daily_dt is None:
        daily_status = "missing"
        payload["issues"].append({
            "code": "tdx_daily_missing",
            "severity": "warn",
            "detail": "未取到样本股票日线数据。",
        })
    elif stock_daily_dt.date().isoformat() not in acceptable_daily_dates:
        daily_status = "stale"
        payload["issues"].append({
            "code": "tdx_daily_stale",
            "severity": "warn",
            "detail": (
                f"样本股票日线最新日期为 {stock_daily_dt.date().isoformat()}，"
                f"可接受日期应在 {sorted(acceptable_daily_dates)} 内。"
            ),
        })
    payload["tdx_review"]["daily"] = {
        "status": daily_status,
        "sample_code": SAMPLE_STOCK["code"],
        "sample_name": SAMPLE_STOCK["name"],
        "latest_datetime": stock_daily_dt.strftime("%Y-%m-%d %H:%M:%S") if stock_daily_dt else "",
        "expected_latest_trading_day": expected_daily_date,
    }

    index_status = "ok"
    if index_daily_dt is None:
        index_status = "missing"
        payload["issues"].append({
            "code": "tdx_index_missing",
            "severity": "warn",
            "detail": "未取到样本指数日线数据。",
        })
    elif index_daily_dt.date().isoformat() not in acceptable_daily_dates:
        index_status = "stale"
        payload["issues"].append({
            "code": "tdx_index_stale",
            "severity": "warn",
            "detail": (
                f"样本指数日线最新日期为 {index_daily_dt.date().isoformat()}，"
                f"可接受日期应在 {sorted(acceptable_daily_dates)} 内。"
            ),
        })
    payload["tdx_review"]["index"] = {
        "status": index_status,
        "sample_code": SAMPLE_INDEX["code"],
        "sample_name": SAMPLE_INDEX["name"],
        "latest_datetime": index_daily_dt.strftime("%Y-%m-%d %H:%M:%S") if index_daily_dt else "",
        "expected_latest_trading_day": expected_daily_date,
    }

    min5_status = "not_applicable"
    actual_lag_minutes = ""
    if is_trading_day(now_dt) and _in_trading_session(now_dt):
        if stock_min5_dt is None:
            min5_status = "missing"
            payload["issues"].append({
                "code": "tdx_min5_missing",
                "severity": "repair_required",
                "detail": "未取到样本股票 5 分钟数据。",
            })
        elif stock_min5_dt.date() != now_dt.date():
            min5_status = "stale"
            payload["issues"].append({
                "code": "tdx_min5_date_stale",
                "severity": "repair_required",
                "detail": f"样本股票 5 分钟线日期为 {stock_min5_dt.date().isoformat()}，当前日期为 {now_dt.date().isoformat()}。",
            })
        else:
            actual_lag_minutes = max(0, int((now_dt - stock_min5_dt).total_seconds() // 60))
            if actual_lag_minutes > int(rule["max_min5_lag_minutes"]):
                min5_status = "stale"
                payload["issues"].append({
                    "code": "tdx_min5_lagged",
                    "severity": "repair_required",
                    "detail": (
                        f"样本股票 5 分钟线最新时间为 {stock_min5_dt:%Y-%m-%d %H:%M:%S}，"
                        f"距当前已滞后 {actual_lag_minutes} 分钟，超过阈值 {rule['max_min5_lag_minutes']} 分钟。"
                    ),
                })
            else:
                min5_status = "ok"
    payload["tdx_review"]["min5"] = {
        "status": min5_status,
        "sample_code": SAMPLE_STOCK["code"],
        "sample_name": SAMPLE_STOCK["name"],
        "latest_datetime": stock_min5_dt.strftime("%Y-%m-%d %H:%M:%S") if stock_min5_dt else "",
        "current_datetime": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "max_allowed_lag_minutes": rule["max_min5_lag_minutes"],
        "actual_lag_minutes": actual_lag_minutes,
    }

    quote_status = "ok" if quote.get("price", "") not in ("", None) else "missing"
    if quote_status != "ok":
        payload["issues"].append({
            "code": "tdx_quote_missing",
            "severity": "warn",
            "detail": "样本股票即时报价为空。",
        })
    payload["tdx_review"]["quote"] = {
        "status": quote_status,
        **quote,
    }

    if any(item["code"] in {"tdx_min5_missing", "tdx_min5_date_stale", "tdx_min5_lagged"} for item in payload["issues"]):
        payload["status"] = "degraded"
        payload["tdx_review"]["status"] = "degraded"
    elif payload["issues"]:
        payload["status"] = "warning"
        payload["tdx_review"]["status"] = "warning"
    else:
        payload["status"] = "ok"
        payload["tdx_review"]["status"] = "ok"
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="交易窗口 TDX 数据新鲜度探针")
    parser.add_argument("--phase", required=True, help="当前探针服务的 phase，例如 smart-sell/prewarm/decision/buy")
    parser.add_argument("--task-name", default="", help="触发该探针的任务名")
    parser.add_argument("--trigger-slot", default="", help="计划任务时间槽，例如 14:49")
    parser.add_argument("--run-id", default="", help="当前阶段 run id")
    args = parser.parse_args()

    payload = _evaluate_probe(
        phase=args.phase,
        task_name=args.task_name,
        trigger_slot=args.trigger_slot,
        run_id=args.run_id,
    )
    _write_json_atomic(LATEST_FILE, payload)
    _append_jsonl(HISTORY_FILE, payload)
    print(json.dumps({
        "generated_at": payload["generated_at"],
        "phase": payload["phase"],
        "status": payload["status"],
        "recommended_action": payload["recommended_action"],
        "issue_codes": [item["code"] for item in payload.get("issues", [])],
        "latest_file": str(LATEST_FILE),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
