from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(os.environ.get("TLFZ_ARKCLAW_ROOT", "")).resolve() if os.environ.get("TLFZ_ARKCLAW_ROOT", "").strip() else Path(__file__).resolve().parent
WORKBUDDY_ROOT = (
    Path(os.environ.get("TLFZ_WORKBUDDY_ROOT", "")).resolve()
    if os.environ.get("TLFZ_WORKBUDDY_ROOT", "").strip()
    else ROOT / "workbuddy"
)
WORKBUDDY_SKILL_ROOT = (
    Path(os.environ.get("TLFZ_WORKBUDDY_SKILL_ROOT", "")).resolve()
    if os.environ.get("TLFZ_WORKBUDDY_SKILL_ROOT", "").strip()
    else WORKBUDDY_ROOT / "skills" / "a-share-analyst"
)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(WORKBUDDY_SKILL_ROOT))

from build_workbuddy_distill_pool import MAIN_OUTPUT_JSON, main as build_pool_main  # noqa: E402
from trading_calendar import CALENDAR_SOURCE, latest_completed_trading_day  # noqa: E402
from workbuddy_runtime import validate_candidate_pool_artifact  # noqa: E402
from workbuddy_distill.scripts.build_tdx_rankings import BAR_COUNT, build_rankings  # noqa: E402
from workbuddy_distill.scripts.distill_local_templates import (  # noqa: E402
    BUFFER_WINDOW_DAYS,
    CORE_WINDOW_DAYS,
    RAW_TOP100_ROOT,
    run_distill,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh TDX distill data, templates, and Workbuddy challenger pool.")
    parser.add_argument("--trade-date", default="", help="指定交易日 YYYY-MM-DD，默认使用最新已完成交易日")
    parser.add_argument("--skip-fetch", action="store_true", help="若目标交易日原始数据不存在，则不主动抓取")
    parser.add_argument("--force-fetch", action="store_true", help="即使目标交易日已存在，也强制重抓 TDX 排名")
    parser.add_argument("--core-days", type=int, default=CORE_WINDOW_DAYS, help="核心学习窗交易日数量")
    parser.add_argument("--buffer-days", type=int, default=BUFFER_WINDOW_DAYS, help="缓冲观察窗交易日数量")
    parser.add_argument("--bar-count", type=int, default=BAR_COUNT, help="TDX 日线抓取数量")
    parser.add_argument("--preview-size", type=int, default=10, help="窗口摘要预览数量")
    return parser.parse_args()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_trade_date(explicit_trade_date: str) -> str:
    if explicit_trade_date:
        return explicit_trade_date.strip()
    return latest_completed_trading_day(datetime.now()).isoformat()


def ensure_trade_date_rankings(
    trade_date: str,
    *,
    skip_fetch: bool,
    force_fetch: bool,
    bar_count: int,
    preview_size: int,
) -> dict[str, Any]:
    target_dir = RAW_TOP100_ROOT / trade_date
    already_exists = target_dir.exists() and (target_dir / "full_rank.csv").exists()
    if already_exists and not force_fetch:
        return {
            "trade_date": trade_date,
            "fetched": False,
            "reason": "already_exists",
            "path": str(target_dir),
        }
    if skip_fetch and not already_exists:
        raise RuntimeError(f"缺少 {trade_date} 的 raw_top100 数据，且当前配置为 --skip-fetch")
    summary = build_rankings(target_dates=[trade_date], bar_count=bar_count, preview_size=preview_size)
    return {
        "trade_date": trade_date,
        "fetched": True,
        "path": str(target_dir),
        "window_summary": summary.get("window", {}),
    }


def main() -> int:
    args = parse_args()
    trade_date = resolve_trade_date(args.trade_date)
    fetch_summary = ensure_trade_date_rankings(
        trade_date,
        skip_fetch=args.skip_fetch,
        force_fetch=args.force_fetch,
        bar_count=args.bar_count,
        preview_size=args.preview_size,
    )
    distill_result = run_distill(core_days=args.core_days, buffer_days=args.buffer_days)
    build_pool_main()
    validate_candidate_pool_artifact(path=MAIN_OUTPUT_JSON, expected_trade_date=trade_date)
    pool_payload = _read_json(MAIN_OUTPUT_JSON)
    window_profile = distill_result.get("window_profile", {})
    summary = {
        "calendar_source": CALENDAR_SOURCE,
        "trade_date": trade_date,
        "fetch_summary": fetch_summary,
        "window_profile": {
            "mode": window_profile.get("mode"),
            "selected_trade_date_count": window_profile.get("selected_trade_date_count"),
            "core_trade_date_count": window_profile.get("core_trade_date_count"),
            "buffer_trade_date_count": window_profile.get("buffer_trade_date_count"),
            "selected_trade_dates": window_profile.get("selected_trade_dates", []),
            "core_trade_dates": window_profile.get("core_trade_dates", []),
            "buffer_trade_dates": window_profile.get("buffer_trade_dates", []),
        },
        "distill_summary": distill_result.get("payload", {}).get("summary", {}),
        "combined_summary": distill_result.get("combined_payload", {}).get("summary", {}),
        "pool_summary": {
            "trade_date": pool_payload.get("trade_date"),
            "selected_count": pool_payload.get("selected_count"),
            "candidate_count": pool_payload.get("candidate_count"),
            "champion_template_name": pool_payload.get("source_distill_registry", {}).get("champion_template_name"),
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
