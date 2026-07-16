#!/usr/bin/env python3
"""Challenger_1 Refresh — replaces distill pipeline for TDX-driven candidate pool.

This is the challenger_1 equivalent of refresh_distill_pipeline.py.
It:
  1. Downloads latest TDX data (optional, if market open)
  2. Screens entire A-share universe via .day files
  3. Writes candidate pool in format compatible with workbuddy_local_challenger
  4. Integrates at the SAME level as the distill refresh

Usage:
  python challenger_1_refresh.py [--trade-date 2026-07-17] [--force]
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, date
from pathlib import Path

# ── Path resolution ────────────────────────────────────────────────
SKILLS_DIR = Path(r"C:\Users\Aibing\AppData\Local\Temp\stockbot\workbuddy\skills\a-share-analyst")
sys.path.insert(0, str(SKILLS_DIR))

from challenger_1_strategy import (
    Challenger1Config, scan_universe, write_candidate_pool, write_buy_plan,
    init_track_record, calculate_performance, register_challenger_1_strategy,
)


def main(trade_date: str = None, force: bool = False):
    """Full challenger_1 refresh pipeline."""
    if trade_date is None:
        trade_date = date.today().isoformat()

    print(f"[challenger_1] Refresh for trade_date={trade_date}")
    start = datetime.now()

    # 1. Configure
    cfg = Challenger1Config()

    # 2. Register with backtest framework (optional, won't fail if not available)
    register_challenger_1_strategy()

    # 3. Scan A-share universe
    print("[challenger_1] Scanning A-share universe...")
    candidates = scan_universe(cfg, max_to_scan=800)
    print(f"[challenger_1] Scored {len(candidates)} candidates")

    # 4. Write pool (compatible with workbuddy pipeline_schema)
    pool_path = write_candidate_pool(candidates, cfg, trade_date)
    print(f"[challenger_1] Pool: {pool_path}")

    # 5. Write buy plan (for human review + execution)
    plan_path = write_buy_plan(candidates, trade_date, cfg)
    print(f"[challenger_1] Buy plan: {plan_path}")

    # 6. Initialize track record if new
    track_path = init_track_record()
    print(f"[challenger_1] Track record: {track_path}")

    elapsed = (datetime.now() - start).total_seconds()
    print(f"[challenger_1] Done in {elapsed:.1f}s")

    return {
        "status": "ok",
        "trade_date": trade_date,
        "candidates": len(candidates),
        "pool_path": str(pool_path),
        "plan_path": str(plan_path),
        "elapsed_s": round(elapsed, 1),
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--trade-date", default=None)
    p.add_argument("--force", action="store_true")
    args = p.parse_args()
    result = main(args.trade_date, args.force)
    print(json.dumps(result, indent=2, ensure_ascii=False))
