#!/usr/bin/env python3
"""Unified backtest framework — shared plumbing used by all T5 strategy variants.

Centralizes TDX connectivity, bar fetching, output utilities, and pipeline
orchestration. Strategy-specific signal logic lives in strategy modules that
plug into this framework via config dicts.

Usage:
    from backtest_framework import BacktestEngine, StrategyConfig

    config = StrategyConfig(
        name="v10",
        top_n_amount=200,
        winner_thresh=5.0,
        loser_thresh=-3.0,
        daily_bar_count=800,
        weekly_bar_count=100,
    )
    engine = BacktestEngine(config)
    results = engine.run()
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pytdx.hq import TdxHq_API

from package_paths import CSI1000_SKILLS_DIR, DATA_DIR


CONS_FILE = CSI1000_SKILLS_DIR / "000852cons.xls"
OUTPUT_DIR = DATA_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TDX_HOSTS = [
    ("218.75.126.9", 7709),
    ("60.191.117.167", 7709),
    ("39.105.251.234", 7709),
    ("119.147.212.83", 7709),
]

STRATEGY_REGISTRY: dict[str, StrategyConfig] = {}


@dataclass
class StrategyConfig:
    name: str = ""
    top_n_amount: int = 200
    winner_thresh: float = 5.0
    loser_thresh: float = -3.0
    daily_bar_count: int = 800
    weekly_bar_count: int = 100
    min_bars_required: int = 60
    lookback_start: int = 30
    forward_skip: int = 6
    ma_windows: tuple[int, ...] = (5, 10, 20, 60)
    bollinger_window: int = 20
    bollinger_std_mult: float = 2.0
    rsi_window: int = 14
    roc_windows: tuple[int, ...] = (3, 5, 10)
    amt_ratio_low: float = 0.7
    amt_ratio_high: float = 1.3
    amt_ratio_cap: float = 2.5
    output_prefix: str = "backtest"
    tdx_retries: int = 3
    tdx_timeout: float = 3.0

    def __post_init__(self):
        if not self.name:
            self.name = self.output_prefix


def register_strategy(config: StrategyConfig) -> StrategyConfig:
    STRATEGY_REGISTRY[config.name] = config
    return config


def to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, np.generic):
        return to_jsonable(value.item())
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, float):
        return None if not math.isfinite(value) else value
    if value is pd.NA:
        return None
    return value


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    normalized = to_jsonable(payload)
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
        f.flush()
    tmp_path.replace(path)


def write_csv_resilient(df: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        df.to_csv(path, index=False, encoding="utf-8-sig")
        return path
    except PermissionError:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        fallback = path.with_name(f"{path.stem}_{stamp}{path.suffix}")
        df.to_csv(fallback, index=False, encoding="utf-8-sig")
        print(f"[WARN] {path.name} locked, wrote: {fallback.name}")
        return fallback


def normalize_code(value: Any) -> str:
    text = str(value).strip()
    if "." in text:
        text = text.split(".")[0]
    return text.zfill(6)


def market_from_exchange(exchange: Any) -> int:
    return 0 if "深圳" in str(exchange) else 1


class BacktestEngine:
    def __init__(self, config: StrategyConfig | None = None):
        self.config = config or StrategyConfig()
        self.api: Any = None

    def connect(self) -> None:
        cfg = self.config
        for _ in range(cfg.tdx_retries):
            for host, port in TDX_HOSTS:
                api = TdxHq_API(heartbeat=True)
                try:
                    if api.connect(host, port, time_out=cfg.tdx_timeout):
                        self.api = api
                        return
                except Exception:
                    pass
                try:
                    api.disconnect()
                except Exception:
                    pass
            time.sleep(0.5)
        raise RuntimeError("Cannot connect to pytdx")

    def disconnect(self) -> None:
        if self.api:
            try:
                self.api.disconnect()
            except Exception:
                pass
            self.api = None

    def load_stock_list(self) -> pd.DataFrame:
        cons = pd.read_excel(CONS_FILE)
        cons = cons.rename(columns={
            "成份券代码Constituent Code": "code_raw",
            "成份券名称Constituent Name": "name",
            "交易所Exchange": "exchange",
        })
        cons["code"] = cons["code_raw"].map(normalize_code)
        cons["market"] = cons["exchange"].map(market_from_exchange)
        cons = cons.drop_duplicates(subset=["market", "code"]).reset_index(drop=True)
        return cons[["code", "name", "market"]].copy()

    def fetch_daily_bars(self, market: int, code: str) -> pd.DataFrame | None:
        try:
            bars = self.api.get_security_bars(9, market, code, 0, self.config.daily_bar_count)
            if not bars:
                return None
            df = self.api.to_df(bars)
            if df is None or df.empty:
                return None
            df["datetime"] = pd.to_datetime(df["datetime"])
            return df.sort_values("datetime").reset_index(drop=True)
        except Exception:
            return None

    def fetch_weekly_bars(self, market: int, code: str) -> pd.DataFrame | None:
        try:
            bars = self.api.get_security_bars(5, market, code, 0, self.config.weekly_bar_count)
            if not bars:
                return None
            df = self.api.to_df(bars)
            if df is None or df.empty:
                return None
            df["datetime"] = pd.to_datetime(df["datetime"])
            return df.sort_values("datetime").reset_index(drop=True)
        except Exception:
            return None

    def fetch_5min_bars(self, market: int, code: str) -> pd.DataFrame | None:
        try:
            all_bars = []
            for offset in [0, 800]:
                bars = self.api.get_security_bars(0, market, code, offset, 800)
                if bars:
                    df = self.api.to_df(bars)
                    if df is not None and not df.empty:
                        all_bars.append(df)
            if not all_bars:
                return None
            df = pd.concat(all_bars, ignore_index=True)
            df["datetime"] = pd.to_datetime(df["datetime"])
            return df.sort_values("datetime").drop_duplicates(subset=["datetime"]).reset_index(drop=True)
        except Exception:
            return None

    # ── Shared feature computers ──

    @staticmethod
    def cohens_d(a: np.ndarray, b: np.ndarray) -> float:
        n1, n2 = len(a), len(b)
        if n1 < 5 or n2 < 5:
            return 0.0
        m1, m2 = a.mean(), b.mean()
        s1, s2 = a.std(), b.std()
        if s1 == 0 and s2 == 0:
            return 0.0
        pooled = math.sqrt(((n1 - 1) * s1**2 + (n2 - 1) * s2**2) / (n1 + n2 - 2))
        return float((m1 - m2) / pooled) if pooled > 0 else 0.0

    @staticmethod
    def compute_weekly_features(wdf: pd.DataFrame | None) -> dict[str, Any]:
        if wdf is None or len(wdf) < 20:
            return {"weekly_align": False, "weekly_slope": 0.0, "weekly_close_vs_wma20": 0.0, "weekly_ma10_slope": 0.0}
        w = wdf.copy()
        for win in [5, 10, 20]:
            w[f"wma{win}"] = w["close"].rolling(win).mean()
        w["weekly_align"] = (w["wma5"] > w["wma10"]) & (w["wma10"] > w["wma20"])
        last = w.iloc[-1]
        wma20_slope = 0.0
        if len(w) >= 5 and pd.notna(w["wma20"].iloc[-1]) and pd.notna(w["wma20"].iloc[-5]):
            wma20_slope = float((w["wma20"].iloc[-1] - w["wma20"].iloc[-5]) / max(abs(w["wma20"].iloc[-5]), 0.01) * 100)
        w10_slope = 0.0
        if "wma10" in w and len(w) >= 3:
            v_last = w["wma10"].iloc[-1]
            v_prev = w["wma10"].iloc[-3]
            if pd.notna(v_last) and pd.notna(v_prev):
                w10_slope = float((v_last - v_prev) / max(abs(v_prev), 0.01) * 100)
        close_vs_wma20 = 0.0
        if pd.notna(last.get("close")) and pd.notna(last.get("wma20")) and last["wma20"] > 0:
            close_vs_wma20 = float((last["close"] - last["wma20"]) / last["wma20"] * 100)
        return {
            "weekly_align": bool(last.get("weekly_align", False)),
            "weekly_slope": wma20_slope,
            "weekly_close_vs_wma20": close_vs_wma20,
            "weekly_ma10_slope": w10_slope,
        }

    def compute_daily_features(self, daily_df: pd.DataFrame, wfeats: dict[str, Any]) -> pd.DataFrame:
        cfg = self.config
        d = daily_df.copy()
        for w in cfg.ma_windows:
            d[f"ma{w}"] = d["close"].rolling(w).mean()
        d["avg_amt_5d"] = d["amount"].rolling(5).mean()
        d["amt_ratio"] = d["amount"] / d["avg_amt_5d"]
        d["close_vs_ma20"] = (d["close"] - d["ma20"]) / d["ma20"] * 100
        d["low_vs_ma20"] = (d["low"] - d["ma20"]) / d["ma20"] * 100
        d["daily_bullish"] = (d["ma5"] > d["ma10"]) & (d["ma10"] > d["ma20"])

        bbw = cfg.bollinger_window
        d["bb_std"] = d["close"].rolling(bbw).std()
        d["bb_upper"] = d["ma20"] + cfg.bollinger_std_mult * d["bb_std"]
        d["bb_lower"] = d["ma20"] - cfg.bollinger_std_mult * d["bb_std"]
        d["bb_pct"] = (d["close"] - d["bb_lower"]) / (d["bb_upper"] - d["bb_lower"])
        d["bb_width"] = (d["bb_upper"] - d["bb_lower"]) / d["ma20"] * 100

        delta = d["close"].diff()
        gain = delta.where(delta > 0, 0).rolling(cfg.rsi_window).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(cfg.rsi_window).mean()
        rs = gain / loss
        d["rsi14"] = 100 - (100 / (1 + rs))

        for roc in cfg.roc_windows:
            d[f"roc_{roc}"] = d["close"].pct_change(roc) * 100

        d["body"] = abs(d["close"] - d["open"])
        d["candle_range"] = d["high"] - d["low"]
        d["lower_shadow"] = d[["low", "open", "close"]].min(axis=1) - d["low"]
        d["lower_shadow_ratio"] = d["lower_shadow"] / d["candle_range"]
        d["is_green"] = d["close"] > d["open"]

        d["up_day"] = (d["close"] > d["close"].shift(1)).astype(int)
        d["consec_down"] = 0
        streak = 0
        for i in range(1, len(d)):
            if d.iloc[i]["close"] < d.iloc[i - 1]["close"]:
                streak += 1
            else:
                streak = 0
            d.iloc[i, d.columns.get_loc("consec_down")] = streak

        d["vol_expand"] = (d["amt_ratio"] >= cfg.amt_ratio_high) & (d["amt_ratio"] <= cfg.amt_ratio_cap)
        d["vol_shrink"] = d["amt_ratio"] < cfg.amt_ratio_low
        d["ret_5d"] = (d["close"].shift(-5) / d["close"] - 1) * 100

        d["weekly_align"] = wfeats.get("weekly_align", False)
        d["weekly_slope"] = wfeats.get("weekly_slope", 0.0)
        d["weekly_close_vs_wma20"] = wfeats.get("weekly_close_vs_wma20", 0.0)
        d["weekly_ma10_slope"] = wfeats.get("weekly_ma10_slope", 0.0)
        return d

    def process_stock(self, code: str, name: str, market: int) -> list[dict[str, Any]]:
        """Process one stock: fetch bars, compute features, return signal records."""
        daily = self.fetch_daily_bars(market, code)
        if daily is None or len(daily) < self.config.min_bars_required:
            return []

        weekly = self.fetch_weekly_bars(market, code)
        min5 = self.fetch_5min_bars(market, code)

        wfeats = self.compute_weekly_features(weekly)
        d = self.compute_daily_features(daily, wfeats)

        min5_by_date: dict[Any, pd.DataFrame] = {}
        if min5 is not None and len(min5) > 0:
            min5["date"] = min5["datetime"].dt.date
            for dt, grp in min5.groupby("date"):
                min5_by_date[dt] = grp.copy()

        records: list[dict[str, Any]] = []
        cfg = self.config
        for i in range(cfg.lookback_start, len(d) - cfg.forward_skip):
            row = d.iloc[i]
            if pd.isna(row.get("ret_5d")) or pd.isna(row.get("ma20")) or pd.isna(row.get("avg_amt_5d")):
                continue

            rec = {
                "code": code,
                "name": name,
                "date": str(row["datetime"])[:10],
                "close": float(row["close"]),
                "ret_5d": float(row["ret_5d"]),
                "label": "winner" if row["ret_5d"] >= cfg.winner_thresh else
                         ("loser" if row["ret_5d"] <= cfg.loser_thresh else "neutral"),
                "close_vs_ma20": float(row["close_vs_ma20"]) if pd.notna(row["close_vs_ma20"]) else 0.0,
                "low_vs_ma20": float(row["low_vs_ma20"]) if pd.notna(row["low_vs_ma20"]) else 0.0,
                "amt_ratio": float(row["amt_ratio"]) if pd.notna(row["amt_ratio"]) else 1.0,
                "daily_bullish": bool(row["daily_bullish"]),
                "bb_pct": float(row["bb_pct"]) if pd.notna(row["bb_pct"]) else 0.5,
                "bb_width": float(row["bb_width"]) if pd.notna(row["bb_width"]) else 0.0,
                "rsi14": float(row["rsi14"]) if pd.notna(row["rsi14"]) else 50.0,
                "roc_5": float(row["roc_5"]) if pd.notna(row["roc_5"]) else 0.0,
                "roc_10": float(row["roc_10"]) if pd.notna(row["roc_10"]) else 0.0,
                "is_green": bool(row["is_green"]),
                "consec_down": int(row["consec_down"]),
                "vol_expand": bool(row["vol_expand"]),
                "vol_shrink": bool(row["vol_shrink"]),
                "weekly_align": bool(row["weekly_align"]),
                "weekly_slope": float(row["weekly_slope"]),
                "weekly_close_vs_wma20": float(row["weekly_close_vs_wma20"]),
            }
            records.append(rec)
        return records

    def run(self, *, limit_stocks: int | None = None) -> dict[str, Any]:
        """Full backtest pipeline: connect -> scan universe -> output results."""
        self.connect()
        try:
            stocks = self.load_stock_list()

            amt_list = []
            for _, row in stocks.iterrows():
                daily = self.fetch_daily_bars(int(row["market"]), str(row["code"]))
                if daily is not None and len(daily) > 0:
                    last = daily.iloc[-1]
                    amt_list.append({"code": row["code"], "name": row["name"], "market": row["market"], "latest_amt": float(last["amount"])})

            amt_df = pd.DataFrame(amt_list).sort_values("latest_amt", ascending=False).head(self.config.top_n_amount)
            if limit_stocks:
                amt_df = amt_df.head(limit_stocks)

            all_records: list[dict[str, Any]] = []
            total = len(amt_df)
            for idx, (_, stock) in enumerate(amt_df.iterrows()):
                recs = self.process_stock(str(stock["code"]), str(stock["name"]), int(stock["market"]))
                all_records.extend(recs)
                if (idx + 1) % 10 == 0 or idx + 1 == total:
                    print(f"  [{self.config.name}] processed {idx + 1}/{total} stocks, {len(all_records)} signals")

            result_df = pd.DataFrame(all_records)
            if result_df.empty:
                return {"status": "empty", "signals": 0, "strategy": self.config.name}

            out_path = OUTPUT_DIR / f"{self.config.output_prefix}_signals.csv"
            write_csv_resilient(result_df, out_path)

            summary = {
                "status": "ok",
                "strategy": self.config.name,
                "total_stocks": len(all_records),
                "winners": int((result_df["label"] == "winner").sum()),
                "losers": int((result_df["label"] == "loser").sum()),
                "neutral": int((result_df["label"] == "neutral").sum()),
                "output": str(out_path),
            }
            write_json_atomic(OUTPUT_DIR / f"{self.config.output_prefix}_summary.json", summary)
            print(f"  [{self.config.name}] done: {summary}")
            return summary
        finally:
            self.disconnect()
