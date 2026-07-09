#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Refresh curve observatory data and dashboards using trading-calendar context."""

from __future__ import annotations

import argparse
import json

from generate_curve_csvs import generate_curve_csvs
from generate_curve_dashboard import generate_dashboards
from trading_calendar import calendar_context, latest_completed_trading_day


def update_curve_observatory(as_of_date: str = "") -> dict:
    trade_date = str(as_of_date).strip() or latest_completed_trading_day().isoformat()
    context = calendar_context(trade_date)
    csv_counts = generate_curve_csvs(as_of_date=trade_date)
    dashboards = generate_dashboards(as_of_date=trade_date)
    return {
        "calendar": context,
        "as_of_date": trade_date,
        "csv_counts": csv_counts,
        "dashboards": dashboards,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Update curve observatory after close")
    parser.add_argument(
        "--as-of-date",
        default="",
        help="Override trading date context (YYYY-MM-DD). Empty means latest completed trading day.",
    )
    args = parser.parse_args()
    payload = update_curve_observatory(as_of_date=str(args.as_of_date).strip())
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
