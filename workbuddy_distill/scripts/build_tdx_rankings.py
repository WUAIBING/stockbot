from __future__ import annotations

import argparse
import csv
import json
import time
from collections import Counter
from pathlib import Path

import pandas as pd
from pytdx.hq import TdxHq_API


ROOT = Path(__file__).resolve().parents[2]
DISTILL_ROOT = ROOT / "workbuddy_distill"
RAW_TOP100_ROOT = DISTILL_ROOT / "raw_top100"
ARTIFACTS_ROOT = DISTILL_ROOT / "artifacts"

DEFAULT_WINDOW_DATES = [
    "2026-05-13",
    "2026-05-14",
    "2026-05-15",
    "2026-05-18",
    "2026-05-19",
    "2026-05-20",
    "2026-06-01",
    "2026-06-02",
    "2026-06-03",
    "2026-06-04",
    "2026-06-05",
    "2026-06-08",
    "2026-06-09",
    "2026-06-10",
    "2026-06-11",
    "2026-06-12",
    "2026-06-15",
    "2026-06-16",
    "2026-06-17",
    "2026-06-18",
]

TDX_HOSTS = [
    ("218.75.126.9", 7709),
    ("60.191.117.167", 7709),
    ("39.105.251.234", 7709),
    ("119.147.212.83", 7709),
]

DAILY_BAR_CATEGORY = 9
BAR_COUNT = 80


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build TDX full-rank and top100 artifacts for one or more trading days.")
    parser.add_argument(
        "--dates",
        nargs="*",
        default=DEFAULT_WINDOW_DATES,
        help="Trading dates in YYYY-MM-DD format. Defaults to the first 20-trading-day distill window.",
    )
    parser.add_argument(
        "--bar-count",
        type=int,
        default=BAR_COUNT,
        help="How many daily bars to request per stock. Higher values improve robustness for older dates.",
    )
    parser.add_argument(
        "--preview-size",
        type=int,
        default=10,
        help="How many names to keep in the per-date preview section.",
    )
    return parser.parse_args()


def connect_tdx() -> tuple[TdxHq_API, str]:
    last_error = ""
    for host, port in TDX_HOSTS:
        api = TdxHq_API(heartbeat=True)
        try:
            if api.connect(host, port, time_out=3.0):
                return api, f"{host}:{port}"
        except Exception as exc:
            last_error = str(exc)
        try:
            api.disconnect()
        except Exception:
            pass
    raise RuntimeError(f"Cannot connect to pytdx: {last_error}")


def is_hs_a_share(market: int, code: str) -> bool:
    code = str(code).zfill(6)
    if market == 1:
        return code.startswith(("600", "601", "603", "605", "688", "689"))
    return code.startswith(("000", "001", "002", "003", "300", "301"))


def load_hs_a_share_universe(api: TdxHq_API) -> list[dict[str, str | int]]:
    universe: list[dict[str, str | int]] = []
    for market in (0, 1):
        total = int(api.get_security_count(market) or 0)
        for start in range(0, total, 1000):
            rows = api.get_security_list(market, start) or []
            if not rows:
                continue
            df = api.to_df(rows)
            if df is None or df.empty:
                continue
            for row in df.to_dict("records"):
                code = str(row.get("code", "")).zfill(6)
                name = str(row.get("name", "")).strip()
                if not code or not name or not is_hs_a_share(market, code):
                    continue
                universe.append(
                    {
                        "market": market,
                        "code": code,
                        "name": name,
                    }
                )
    seen: set[tuple[int, str]] = set()
    deduped: list[dict[str, str | int]] = []
    for item in universe:
        key = (int(item["market"]), str(item["code"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def market_label(market: int) -> str:
    return "SH" if market == 1 else "SZ"


def fetch_daily_frame(api: TdxHq_API, market: int, code: str, bar_count: int) -> pd.DataFrame | None:
    try:
        bars = api.get_security_bars(DAILY_BAR_CATEGORY, market, code, 0, bar_count)
    except Exception:
        return None
    if not bars:
        return None
    df = api.to_df(bars)
    if df is None or df.empty:
        return None
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    df["trade_date"] = df["datetime"].dt.strftime("%Y-%m-%d")
    return df


def write_csv(file_path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    with file_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(file_path: Path, payload: object) -> None:
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_rankings(target_dates: list[str], bar_count: int, preview_size: int) -> dict[str, object]:
    started_at = time.perf_counter()
    api, host = connect_tdx()
    try:
        universe_started = time.perf_counter()
        universe = load_hs_a_share_universe(api)
        universe_elapsed = time.perf_counter() - universe_started

        results_by_date: dict[str, list[dict[str, object]]] = {date: [] for date in target_dates}
        status_by_date: dict[str, Counter[str]] = {date: Counter() for date in target_dates}

        fetch_started = time.perf_counter()
        for idx, item in enumerate(universe, start=1):
            market = int(item["market"])
            code = str(item["code"])
            name = str(item["name"])
            df = fetch_daily_frame(api, market, code, bar_count)
            if df is None:
                for date in target_dates:
                    status_by_date[date]["empty_bars"] += 1
                continue

            date_to_index = {trade_date: i for i, trade_date in enumerate(df["trade_date"].tolist())}
            for date in target_dates:
                hit_index = date_to_index.get(date)
                if hit_index is None:
                    status_by_date[date]["target_date_not_found"] += 1
                    continue
                if hit_index <= 0:
                    status_by_date[date]["no_prev_bar"] += 1
                    continue
                prev_close = float(df.iloc[hit_index - 1]["close"])
                close = float(df.iloc[hit_index]["close"])
                if prev_close <= 0:
                    status_by_date[date]["prev_close_non_positive"] += 1
                    continue
                pct_change = round((close / prev_close - 1.0) * 100, 4)
                results_by_date[date].append(
                    {
                        "trade_date": date,
                        "market": market,
                        "market_label": market_label(market),
                        "code": code,
                        "name": name,
                        "close": round(close, 3),
                        "prev_close": round(prev_close, 3),
                        "pct_change": pct_change,
                    }
                )
                status_by_date[date]["ok"] += 1

            if idx % 500 == 0:
                print(f"[progress] {idx}/{len(universe)}")

        fetch_elapsed = time.perf_counter() - fetch_started

        for date, rows in results_by_date.items():
            rows.sort(key=lambda row: (float(row["pct_change"]), str(row["code"])), reverse=True)
            for rank, row in enumerate(rows, start=1):
                row["rank"] = rank

            date_dir = RAW_TOP100_ROOT / date
            date_dir.mkdir(parents=True, exist_ok=True)

            full_rank_path = date_dir / "full_rank.csv"
            top100_path = date_dir / "top100.csv"
            top100_json_path = date_dir / "top100.json"
            summary_path = date_dir / "summary.json"

            write_csv(
                full_rank_path,
                rows,
                ["rank", "trade_date", "market", "market_label", "code", "name", "close", "prev_close", "pct_change"],
            )
            write_csv(
                top100_path,
                rows[:100],
                ["rank", "trade_date", "market", "market_label", "code", "name", "close", "prev_close", "pct_change"],
            )
            write_json(top100_json_path, rows[:100])
            write_json(
                summary_path,
                {
                    "trade_date": date,
                    "connected_host": host,
                    "scope": "hs_a_share",
                    "universe_count": len(universe),
                    "valid_result_count": len(rows),
                    "status_counter": dict(status_by_date[date]),
                    "preview": rows[:preview_size],
                    "paths": {
                        "full_rank_csv": str(full_rank_path),
                        "top100_csv": str(top100_path),
                        "top100_json": str(top100_json_path),
                    },
                },
            )

        total_elapsed = time.perf_counter() - started_at
        window_summary = {
            "window": {
                "start_date": target_dates[0],
                "end_date": target_dates[-1],
                "trade_date_count": len(target_dates),
                "trade_dates": target_dates,
            },
            "connected_host": host,
            "scope": "hs_a_share",
            "universe_count": len(universe),
            "timing": {
                "universe_seconds": round(universe_elapsed, 3),
                "fetch_seconds": round(fetch_elapsed, 3),
                "total_seconds": round(total_elapsed, 3),
            },
            "per_date": {
                date: {
                    "valid_result_count": len(results_by_date[date]),
                    "status_counter": dict(status_by_date[date]),
                    "preview": results_by_date[date][:preview_size],
                }
                for date in target_dates
            },
        }
        stamp = f"{target_dates[0]}_{target_dates[-1]}"
        ARTIFACTS_ROOT.mkdir(parents=True, exist_ok=True)
        write_json(ARTIFACTS_ROOT / f"window_summary_{stamp}.json", window_summary)
        write_json(ARTIFACTS_ROOT / "window_summary_latest.json", window_summary)
        return window_summary
    finally:
        try:
            api.disconnect()
        except Exception:
            pass


def main() -> int:
    args = parse_args()
    target_dates = sorted(dict.fromkeys(args.dates))
    summary = build_rankings(target_dates=target_dates, bar_count=args.bar_count, preview_size=args.preview_size)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
