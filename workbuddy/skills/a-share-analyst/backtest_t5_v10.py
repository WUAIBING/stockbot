"""
V10 Backtest: Multi-Tier + Multi-Mode Signal System

Philosophy: "大肉小肉都是肉"
- Tier 1 (大肉): V9 full pass → 100% position
- Tier 2 (中肉): 2 of 3 V9 conditions → 60% position
- Tier 3 (小肉): 1 strong condition → 30% position
- Mode 2: Trend-Riding (no bz_kill needed)
- Mode 3: Volume-Breakout (no bz_kill needed)

Goal: Every trading day should have at least 1 tradeable signal.
Total portfolio return > single-mode V9 return.
"""

import time
import warnings
import json
import math
from pathlib import Path
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd
from pytdx.hq import TdxHq_API

from package_paths import CSI1000_SKILLS_DIR, DATA_DIR

warnings.filterwarnings("ignore")

CONS_FILE = CSI1000_SKILLS_DIR / "000852cons.xls"
OUTPUT_DIR = DATA_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TDX_HOSTS = [
    ("218.75.126.9", 7709),
    ("60.191.117.167", 7709),
    ("39.105.251.234", 7709),
    ("119.147.212.83", 7709),
]
TOP_N_AMOUNT = 200
WINNER_THRESH = 5.0
LOSER_THRESH = -3.0


def to_jsonable(value):
    """Convert numpy/pandas scalars to plain JSON-safe Python values."""
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


def write_json_atomic(path: Path, payload):
    """Write JSON via temp file + replace to avoid half-written output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    normalized = to_jsonable(payload)
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
        f.flush()
    tmp_path.replace(path)


def write_csv_resilient(df: pd.DataFrame, path: Path) -> Path:
    """Write CSV safely; if target is locked, fall back to a timestamped filename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        df.to_csv(path, index=False, encoding="utf-8-sig")
        return path
    except PermissionError:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        fallback = path.with_name(f"{path.stem}_{stamp}{path.suffix}")
        df.to_csv(fallback, index=False, encoding="utf-8-sig")
        print(f"[WARN] {path.name} is locked, wrote fallback file: {fallback.name}")
        return fallback


def normalize_code(value):
    text = str(value).strip()
    if "." in text:
        text = text.split(".")[0]
    return text.zfill(6)

def market_from_exchange(exchange):
    return 0 if "深圳" in str(exchange) else 1

def connect_tdx():
    for _ in range(3):
        for host, port in TDX_HOSTS:
            api = TdxHq_API(heartbeat=True)
            try:
                if api.connect(host, port, time_out=3.0):
                    return api
            except Exception:
                pass
            try:
                api.disconnect()
            except Exception:
                pass
        time.sleep(0.5)
    raise RuntimeError("Cannot connect to pytdx")

def get_stock_list():
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

def fetch_daily_bars(api, market, code, count=800):
    try:
        bars = api.get_security_bars(9, market, code, 0, count)
        if not bars:
            return None
        df = api.to_df(bars)
        if df is None or df.empty:
            return None
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.sort_values("datetime").reset_index(drop=True)
        return df
    except Exception:
        return None

def fetch_weekly_bars(api, market, code, count=100):
    try:
        bars = api.get_security_bars(5, market, code, 0, count)
        if not bars:
            return None
        df = api.to_df(bars)
        if df is None or df.empty:
            return None
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.sort_values("datetime").reset_index(drop=True)
        return df
    except Exception:
        return None

def fetch_5min_bars(api, market, code):
    try:
        all_bars = []
        for offset in [0, 800]:
            bars = api.get_security_bars(0, market, code, offset, 800)
            if bars:
                df = api.to_df(bars)
                if df is not None and not df.empty:
                    all_bars.append(df)
        if not all_bars:
            return None
        df = pd.concat(all_bars, ignore_index=True)
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.sort_values("datetime").drop_duplicates(subset=["datetime"]).reset_index(drop=True)
        return df
    except Exception:
        return None

def cohens_d(a, b):
    n1, n2 = len(a), len(b)
    if n1 < 5 or n2 < 5:
        return 0.0
    m1, m2 = a.mean(), b.mean()
    s1, s2 = a.std(), b.std()
    if s1 == 0 and s2 == 0:
        return 0.0
    pooled = np.sqrt(((n1-1)*s1**2 + (n2-1)*s2**2) / (n1+n2-2))
    return (m1 - m2) / pooled if pooled > 0 else 0.0


# ─────────────────────────────────────────────
# Feature computation
# ─────────────────────────────────────────────

def compute_weekly_features(wdf):
    if wdf is None or len(wdf) < 25:
        return {}
    wdf = wdf.copy()
    wdf["wma5"] = wdf["close"].rolling(5).mean()
    wdf["wma10"] = wdf["close"].rolling(10).mean()
    wdf["wma20"] = wdf["close"].rolling(20).mean()
    last = wdf.iloc[-1]
    if not all(pd.notna(last.get(k)) for k in ["wma5", "wma10", "wma20"]):
        return {}
    w5, w10, w20 = last["wma5"], last["wma10"], last["wma20"]
    return {
        "weekly_align": bool(w5 > w10 > w20),
        "weekly_slope": (w5 - w20) / w20 * 100 if w20 > 0 else 0.0,
        "weekly_close_vs_wma20": (last["close"] - w20) / w20 * 100 if w20 > 0 else 0.0,
        "weekly_ma10_slope": (w10 - wdf["wma10"].iloc[-2]) / wdf["wma10"].iloc[-2] * 100
            if len(wdf) > 1 and pd.notna(wdf["wma10"].iloc[-2]) and wdf["wma10"].iloc[-2] > 0 else 0.0,
    }

def compute_5min_features_for_day(day_df_5min, cutoff_hour=14, cutoff_minute=50):
    """
    Given 5-min data for a day, extract buy-zone signals.
    cutoff_hour/minute: for real-time version (e.g. 14:50),
    use only bars up to that time for bz_rt.
    """
    if day_df_5min is None or len(day_df_5min) < 5:
        return {}

    d = day_df_5min.copy()
    d["hour"] = d["datetime"].dt.hour
    d["minute"] = d["datetime"].dt.minute

    total_amt = d["amount"].sum()
    total_vol = d["vol"].sum()

    am = d[(d["hour"] == 9) | ((d["hour"] == 10)) | ((d["hour"] == 11) & (d["minute"] <= 30))]
    pm = d[(d["hour"] == 13) | (d["hour"] == 14)]
    bz_full = d[(d["hour"] == 14) & (d["minute"] >= 30)]  # 14:30-15:00
    bz_rt = d[(d["hour"] == 14) & (d["minute"] >= 30) & 
               ((d["hour"] < cutoff_hour) | 
                ((d["hour"] == cutoff_hour) & (d["minute"] <= cutoff_minute)))]
    last3 = d.tail(3)

    feats = {}

    # Full buy-zone direction (14:30-15:00) — backtest uses this
    if len(bz_full) >= 3:
        bz_open = bz_full.iloc[0]["open"]
        bz_close = bz_full.iloc[-1]["close"]
        feats["bz_direction"] = (bz_close - bz_open) / bz_open * 100 if bz_open > 0 else 0.0

    # Real-time buy-zone direction (14:30-14:50) — live scanner uses this
    if len(bz_rt) >= 2:
        bz_rt_open = bz_rt.iloc[0]["open"]
        bz_rt_close = bz_rt.iloc[-1]["close"]
        feats["bz_rt_direction"] = (bz_rt_close - bz_rt_open) / bz_rt_open * 100 if bz_rt_open > 0 else 0.0

    # Buy-zone volume ratio
    if len(bz_full) > 0 and total_vol > 0:
        bz_vol = bz_full["vol"].sum()
        avg_per_bar = total_vol / len(d)
        feats["bz_vol_ratio"] = (bz_vol / len(bz_full)) / avg_per_bar if avg_per_bar > 0 else 1.0

    # PM/AM ratio
    if len(am) > 0 and len(pm) > 0:
        am_avg = am["amount"].mean()
        pm_avg = pm["amount"].mean()
        feats["pm_am_ratio"] = pm_avg / am_avg if am_avg > 0 else 1.0

    # Last 3 bars slope
    if len(last3) >= 2:
        first_c = last3.iloc[0]["close"]
        last_c = last3.iloc[-1]["close"]
        feats["last3_slope"] = (last_c - first_c) / first_c * 100 if first_c > 0 else 0.0

    # Bollinger % from 5-min
    if len(d) >= 10:
        close_series = d["close"]
        bb_mid = close_series.rolling(10).mean().iloc[-1]
        bb_std = close_series.rolling(10).std().iloc[-1]
        if pd.notna(bb_mid) and pd.notna(bb_std) and bb_std > 0:
            bb_upper = bb_mid + 2 * bb_std
            bb_lower = bb_mid - 2 * bb_std
            feats["bb5_pct_end"] = (d.iloc[-1]["close"] - bb_lower) / (bb_upper - bb_lower) \
                if (bb_upper - bb_lower) > 0 else 0.5

    if "bz_vol_ratio" in feats:
        feats["bz_shrink"] = float(feats["bz_vol_ratio"] < 0.8)

    return feats


def compute_daily_features(daily_df, wfeats):
    """Compute all daily-level features (same as V7 but leaner)"""
    d = daily_df.copy()
    for w in [5, 10, 20, 60]:
        d[f"ma{w}"] = d["close"].rolling(w).mean()
    d["avg_amt_5d"] = d["amount"].rolling(5).mean()
    d["amt_ratio"] = d["amount"] / d["avg_amt_5d"]
    d["close_vs_ma20"] = (d["close"] - d["ma20"]) / d["ma20"] * 100
    d["low_vs_ma20"] = (d["low"] - d["ma20"]) / d["ma20"] * 100
    d["daily_bullish"] = (d["ma5"] > d["ma10"]) & (d["ma10"] > d["ma20"])

    # Bollinger
    d["bb_std"] = d["close"].rolling(20).std()
    d["bb_upper"] = d["ma20"] + 2 * d["bb_std"]
    d["bb_lower"] = d["ma20"] - 2 * d["bb_std"]
    d["bb_pct"] = (d["close"] - d["bb_lower"]) / (d["bb_upper"] - d["bb_lower"])
    d["bb_width"] = (d["bb_upper"] - d["bb_lower"]) / d["ma20"] * 100

    # RSI
    delta = d["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    d["rsi14"] = 100 - (100 / (1 + rs))

    # ROC
    d["roc_3"] = d["close"].pct_change(3) * 100
    d["roc_5"] = d["close"].pct_change(5) * 100
    d["roc_10"] = d["close"].pct_change(10) * 100

    # Candle features
    d["body"] = abs(d["close"] - d["open"])
    d["candle_range"] = d["high"] - d["low"]
    d["lower_shadow"] = d[["low", "open", "close"]].min(axis=1) - d["low"]
    d["lower_shadow_ratio"] = d["lower_shadow"] / d["candle_range"]
    d["is_green"] = d["close"] > d["open"]

    # Consecutive days
    d["up_day"] = (d["close"] > d["close"].shift(1)).astype(int)
    d["consec_down"] = 0
    streak = 0
    for i in range(1, len(d)):
        if d.iloc[i]["close"] < d.iloc[i-1]["close"]:
            streak += 1
        else:
            streak = 0
        d.iloc[i, d.columns.get_loc("consec_down")] = streak

    # Volume flags
    d["vol_expand"] = (d["amt_ratio"] >= 1.3) & (d["amt_ratio"] <= 2.5)
    d["vol_shrink"] = d["amt_ratio"] < 0.7

    # T+5 return
    d["ret_5d"] = (d["close"].shift(-5) / d["close"] - 1) * 100

    # Weekly features
    d["weekly_align"] = wfeats.get("weekly_align", False)
    d["weekly_slope"] = wfeats.get("weekly_slope", 0.0)
    d["weekly_close_vs_wma20"] = wfeats.get("weekly_close_vs_wma20", 0.0)
    d["weekly_ma10_slope"] = wfeats.get("weekly_ma10_slope", 0.0)

    return d


def process_stock(api, code, name, market):
    daily = fetch_daily_bars(api, market, code, count=800)
    if daily is None or len(daily) < 60:
        return []

    weekly = fetch_weekly_bars(api, market, code, count=100)
    min5 = fetch_5min_bars(api, market, code)

    wfeats = compute_weekly_features(weekly)
    d = compute_daily_features(daily, wfeats)

    # 5-min index
    min5_by_date = {}
    if min5 is not None and len(min5) > 0:
        min5["date"] = min5["datetime"].dt.date
        for dt, grp in min5.groupby("date"):
            min5_by_date[dt] = grp.copy()

    records = []
    for i in range(30, len(d) - 6):
        row = d.iloc[i]
        if pd.isna(row.get("ret_5d")) or pd.isna(row.get("ma20")) or pd.isna(row.get("avg_amt_5d")):
            continue

        rec = {
            "code": code,
            "name": name,
            "date": str(row["datetime"])[:10],
            "close": row["close"],
            "ret_5d": row["ret_5d"],
            "label": "winner" if row["ret_5d"] >= WINNER_THRESH else
                     ("loser" if row["ret_5d"] <= LOSER_THRESH else "neutral"),
            # Daily features
            "close_vs_ma20": row["close_vs_ma20"] if pd.notna(row["close_vs_ma20"]) else 0.0,
            "low_vs_ma20": row["low_vs_ma20"] if pd.notna(row["low_vs_ma20"]) else 0.0,
            "amt_ratio": row["amt_ratio"] if pd.notna(row["amt_ratio"]) else 1.0,
            "daily_bullish": bool(row["daily_bullish"]),
            "bb_pct": row["bb_pct"] if pd.notna(row["bb_pct"]) else 0.5,
            "bb_width": row["bb_width"] if pd.notna(row["bb_width"]) else 0.0,
            "rsi14": row["rsi14"] if pd.notna(row["rsi14"]) else 50.0,
            "roc_3": row["roc_3"] if pd.notna(row["roc_3"]) else 0.0,
            "roc_5": row["roc_5"] if pd.notna(row["roc_5"]) else 0.0,
            "roc_10": row["roc_10"] if pd.notna(row["roc_10"]) else 0.0,
            "is_green": bool(row["is_green"]),
            "lower_shadow_ratio": row["lower_shadow_ratio"] if pd.notna(row["lower_shadow_ratio"]) else 0.0,
            "consec_down": int(row["consec_down"]),
            "vol_expand": bool(row["vol_expand"]),
            "vol_shrink": bool(row["vol_shrink"]),
            # Weekly features
            "weekly_align": bool(row["weekly_align"]),
            "weekly_slope": row["weekly_slope"],
            "weekly_close_vs_wma20": row["weekly_close_vs_wma20"],
            "weekly_ma10_slope": row["weekly_ma10_slope"],
        }

        # 5-min features
        trade_date = row["datetime"].date()
        if trade_date in min5_by_date:
            m5feats = compute_5min_features_for_day(min5_by_date[trade_date])
            rec.update(m5feats)
        else:
            rec["bz_direction"] = np.nan
            rec["bz_rt_direction"] = np.nan
            rec["bz_vol_ratio"] = np.nan
            rec["pm_am_ratio"] = np.nan
            rec["last3_slope"] = np.nan
            rec["bb5_pct_end"] = np.nan
            rec["bz_shrink"] = np.nan

        records.append(rec)
    return records


def compute_signal_tier(row):
    """
    Multi-tier + multi-mode signal classification.
    Returns (tier, mode, position_size, signal_desc)
    """
    bz = row.get("bz_direction", np.nan)
    if pd.isna(bz):
        bz = 0.0
    bz_rt = row.get("bz_rt_direction", np.nan)
    if pd.isna(bz_rt):
        bz_rt = bz  # fallback

    weekly_a = row.get("weekly_align", False)
    weekly_sl = row.get("weekly_slope", 0.0)
    ma20_off = row.get("close_vs_ma20", 0.0)
    vol_exp = row.get("vol_expand", False)
    rsi = row.get("rsi14", 50.0)
    roc5 = row.get("roc_5", 0.0)
    bb_pct = row.get("bb_pct", 0.5)
    bb_width = row.get("bb_width", 0.0)
    is_green = row.get("is_green", False)
    ls_ratio = row.get("lower_shadow_ratio", 0.0)
    consec_down = row.get("consec_down", 0)
    amt_r = row.get("amt_ratio", 1.0)

    # V9 conditions
    bz_kill = bz < -0.3
    bz_mild = -0.3 <= bz < 0
    ma20_pull = -5.0 <= ma20_off <= 2.0
    ma20_near = -3.0 <= ma20_off <= 3.0
    weekly_strong = weekly_a and weekly_sl > 5.0  # slope > 5% = strong trend

    # ── TIER 1: V9 full pass ──
    if bz_kill and weekly_a and ma20_pull:
        return (1, "V9_full", 1.0, f"bz={bz:+.2f}%+weekly+MA20({ma20_off:+.1f}%)")

    # ── TIER 2: Two strong conditions ──
    # 2a: weekly+ma20pull + mild bz (near-kill)
    if bz_mild and weekly_a and ma20_pull:
        return (2, "near_kill+weekly+MA20", 0.6, f"bz_mild={bz:+.2f}%+weekly+MA20({ma20_off:+.1f}%)")
    # 2b: bz_kill + weekly (no ma20_pull but close)
    if bz_kill and weekly_a and ma20_near:
        return (2, "kill+weekly+nearMA20", 0.6, f"bz={bz:+.2f}%+weekly+MA20({ma20_off:+.1f}%)")
    # 2c: bz_kill + ma20_pull (no weekly)
    if bz_kill and ma20_pull and not weekly_a and roc5 > -5:
        return (2, "kill+MA20_pull", 0.5, f"bz={bz:+.2f}%+MA20({ma20_off:+.1f}%)")

    # ── MODE 2: Trend-Riding (no bz_kill) ──
    # Strong weekly trend + MA20 pullback + volume confirmation
    if weekly_strong and ma20_pull and vol_exp:
        return (2, "trend_ride+vol", 0.6, f"slope={weekly_sl:.1f}%+MA20({ma20_off:+.1f}%)+vol_expand")
    if weekly_strong and ma20_pull and is_green:
        return (2, "trend_ride+green", 0.5, f"slope={weekly_sl:.1f}%+MA20({ma20_off:+.1f}%)+green")

    # ── MODE 3: Volume-Breakout ──
    # Big volume + green candle + weekly align + not overbought
    if vol_exp and is_green and weekly_a and rsi < 70 and roc5 < 10:
        return (2, "vol_breakout", 0.5, f"vol×{amt_r:.1f}+green+weekly+RSI={rsi:.0f}")

    # ── MODE 4: Dip-Buy (consecutive down + MA20 support) ──
    if consec_down >= 3 and ma20_pull and weekly_a and ls_ratio > 0.2:
        return (2, "dip_buy", 0.5, f"down{consec_down}d+MA20({ma20_off:+.1f}%)+weekly+ls={ls_ratio:.2f}")

    # ── TIER 3: One strong condition ──
    # 3a: Just bz_kill (even without other conditions)
    if bz_kill:
        return (3, "kill_only", 0.3, f"bz={bz:+.2f}%")
    # 3b: Strong weekly + MA20 near
    if weekly_strong and ma20_near:
        return (3, "trend_only", 0.3, f"slope={weekly_sl:.1f}%+MA20({ma20_off:+.1f}%)")
    # 3c: Vol expand + green + MA20 near
    if vol_exp and is_green and ma20_near:
        return (3, "vol_green", 0.3, f"vol×{amt_r:.1f}+green+MA20({ma20_off:+.1f}%)")

    return (0, "no_signal", 0.0, "")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    print("=" * 60)
    print("V10 Backtest: Multi-Tier + Multi-Mode Signal System")
    print("Philosophy: 大肉小肉都是肉 — every day is a trading day")
    print("=" * 60)

    stocks = get_stock_list()
    api = connect_tdx()
    print(f"CSI1000 stocks: {len(stocks)}, pytdx connected\n")

    # Step 1: Amount filter → Top 200
    print("Step 1/3: Filter by daily amount Top 200...")
    amt_list = []
    for _, row in stocks.iterrows():
        bars = api.get_security_bars(9, row["market"], row["code"], 0, 3)
        if bars:
            try:
                df = api.to_df(bars)
                if df is not None and not df.empty:
                    amt_list.append({
                        "code": row["code"], "name": row["name"],
                        "market": row["market"],
                        "latest_amt": df.iloc[-1]["amount"]
                    })
            except Exception:
                pass
    amt_df = pd.DataFrame(amt_list).sort_values("latest_amt", ascending=False).head(TOP_N_AMOUNT)
    print(f"  -> Top {TOP_N_AMOUNT}\n")

    # Step 2: Process each stock
    print("Step 2/3: Extract daily + weekly + 5min features...")
    all_records = []
    for i, (_, row) in enumerate(amt_df.iterrows()):
        recs = process_stock(api, row["code"], row["name"], row["market"])
        all_records.extend(recs)
        if (i + 1) % 25 == 0:
            print(f"  Processed {i+1}/{len(amt_df)}, records: {len(all_records)}")

    api.disconnect()
    print(f"  -> Total records: {len(all_records)}\n")

    df = pd.DataFrame(all_records)

    # Save raw data for future analysis
    raw_data_path = write_csv_resilient(df, OUTPUT_DIR / "v10_data_raw.csv")
    print(f"Raw data saved: {raw_data_path}")

    winners = df[df["label"] == "winner"]
    losers = df[df["label"] == "loser"]
    print(f"Winners(>={WINNER_THRESH}%): {len(winners)}, Losers(<={LOSER_THRESH}%): {len(losers)}, Neutral: {len(df)-len(winners)-len(losers)}")

    # Step 3: Multi-tier + multi-mode analysis
    print("\n" + "=" * 60)
    print("Step 3/3: Multi-Tier Signal Analysis")
    print("=" * 60)

    # Only analyze records with 5-min data
    df5 = df[df["bz_direction"].notna()].copy()
    print(f"Records with 5min data: {len(df5)}")

    # Assign tiers
    tier_results = df5.apply(compute_signal_tier, axis=1)
    df5["tier"] = tier_results.apply(lambda x: x[0])
    df5["mode"] = tier_results.apply(lambda x: x[1])
    df5["position"] = tier_results.apply(lambda x: x[2])
    df5["signal_desc"] = tier_results.apply(lambda x: x[3])

    # ── Tier summary ──
    print("\n" + "-" * 60)
    print("TIER SUMMARY (non-overlapping)")
    print("-" * 60)
    tier_data = []
    for tier in [1, 2, 3]:
        t_df = df5[(df5["tier"] == tier)]
        if len(t_df) == 0:
            continue
        wr = (t_df["ret_5d"] > 0).mean() * 100
        avg_ret = t_df["ret_5d"].mean()
        win_avg = t_df[t_df["ret_5d"] > 0]["ret_5d"].mean() if (t_df["ret_5d"] > 0).any() else 0
        loss_avg = t_df[t_df["ret_5d"] <= 0]["ret_5d"].mean() if (t_df["ret_5d"] <= 0).any() else 0
        ev = wr/100 * win_avg + (1-wr/100) * loss_avg
        avg_pos = t_df["position"].mean()
        # Weighted EV by position
        weighted_ev = ev * avg_pos
        tier_data.append({
            "tier": tier,
            "N": len(t_df),
            "WR": wr,
            "avg_ret": avg_ret,
            "win_avg": win_avg,
            "loss_avg": loss_avg,
            "EV": ev,
            "avg_position": avg_pos,
            "weighted_EV": weighted_ev,
        })
        print(f"  Tier {tier}: N={len(t_df):5d}  WR={wr:.1f}%  AR={avg_ret:+.2f}%  "
              f"EV={ev:+.2f}%  Pos={avg_pos:.0%}  W-EV={weighted_ev:+.2f}%")

    # ── Mode breakdown ──
    print("\n" + "-" * 60)
    print("MODE BREAKDOWN")
    print("-" * 60)
    mode_data = []
    for mode in sorted(df5[df5["tier"] > 0]["mode"].unique()):
        m_df = df5[df5["mode"] == mode]
        if len(m_df) < 5:
            continue
        wr = (m_df["ret_5d"] > 0).mean() * 100
        avg_ret = m_df["ret_5d"].mean()
        win_avg = m_df[m_df["ret_5d"] > 0]["ret_5d"].mean() if (m_df["ret_5d"] > 0).any() else 0
        loss_avg = m_df[m_df["ret_5d"] <= 0]["ret_5d"].mean() if (m_df["ret_5d"] <= 0).any() else 0
        ev = wr/100 * win_avg + (1-wr/100) * loss_avg
        avg_pos = m_df["position"].mean()
        tier_val = m_df["tier"].iloc[0]
        mode_data.append({
            "tier": tier_val,
            "mode": mode,
            "N": len(m_df),
            "WR": wr,
            "avg_ret": avg_ret,
            "EV": ev,
            "avg_position": avg_pos,
            "weighted_EV": ev * avg_pos,
        })
        print(f"  T{tier_val} {mode:<25s}: N={len(m_df):4d}  WR={wr:.1f}%  AR={avg_ret:+.2f}%  "
              f"EV={ev:+.2f}%  Pos={avg_pos:.0%}")

    # ── Compare: V9-only vs Multi-Tier ──
    print("\n" + "=" * 60)
    print("COMPARISON: V9-only vs Multi-Tier Portfolio")
    print("=" * 60)

    # V9-only (Tier 1)
    v9_df = df5[df5["tier"] == 1]
    if len(v9_df) > 0:
        v9_wr = (v9_df["ret_5d"] > 0).mean() * 100
        v9_ev = v9_df["ret_5d"].mean()
        v9_n = len(v9_df)
    else:
        v9_wr, v9_ev, v9_n = 0, 0, 0

    # Multi-tier (all tiers)
    all_sig = df5[df5["tier"] > 0]
    if len(all_sig) > 0:
        mt_wr = (all_sig["ret_5d"] > 0).mean() * 100
        mt_ev = all_sig["ret_5d"].mean()
        mt_n = len(all_sig)
        # Position-weighted return
        all_sig = all_sig.copy()
        all_sig["pos_ret"] = all_sig["ret_5d"] * all_sig["position"]
        mt_weighted_avg = all_sig["pos_ret"].mean()
    else:
        mt_wr, mt_ev, mt_n, mt_weighted_avg = 0, 0, 0, 0

    print(f"  V9-only:      N={v9_n:5d}  WR={v9_wr:.1f}%  EV={v9_ev:+.2f}%")
    print(f"  Multi-Tier:   N={mt_n:5d}  WR={mt_wr:.1f}%  EV={mt_ev:+.2f}%  W-EV={mt_weighted_avg:+.2f}%")
    print(f"  Signal boost: {mt_n/max(v9_n,1):.1f}x more signals")

    # ── Daily signal count ──
    print("\n" + "-" * 60)
    print("DAILY SIGNAL DISTRIBUTION")
    print("-" * 60)
    df5_with_sig = df5[df5["tier"] > 0].copy()
    if len(df5_with_sig) > 0:
        daily_counts = df5_with_sig.groupby("date").size()
        print(f"  Trading days with signals: {len(daily_counts)}")
        print(f"  Signals per day: mean={daily_counts.mean():.1f}  median={daily_counts.median():.0f}  "
              f"min={daily_counts.min()}  max={daily_counts.max()}")
        days_no_signal = len(df5["date"].unique()) - len(daily_counts)
        print(f"  Days with NO signal: {days_no_signal} ({days_no_signal/len(df5['date'].unique())*100:.1f}%)")

        # Per-tier daily counts
        for tier in [1, 2, 3]:
            t_daily = df5[df5["tier"] == tier].groupby("date").size()
            t_days_with = len(t_daily)
            print(f"  Tier {tier}: {t_days_with} days with signal, avg {t_daily.mean():.1f} signals/day")

    # ── Save summary ──
    summary = {
        "total_records": len(df),
        "records_with_5min": len(df5),
        "winner_count": len(winners),
        "loser_count": len(losers),
        "tiers": tier_data,
        "modes": mode_data,
        "comparison": {
            "v9_only": {"N": v9_n, "WR": v9_wr, "EV": v9_ev},
            "multi_tier": {"N": mt_n, "WR": mt_wr, "EV": mt_ev, "weighted_EV": mt_weighted_avg},
        }
    }
    summary_path = OUTPUT_DIR / "v10_summary.json"
    write_json_atomic(summary_path, summary)
    print(f"\nSummary saved: {summary_path}")

    # ── Feature Cohen's d for 5-min features (same as V9 for reference) ──
    print("\n" + "=" * 60)
    print("5-MIN FEATURE Cohen's d (for reference)")
    print("=" * 60)
    w5 = df5[df5["label"] == "winner"]
    l5 = df5[df5["label"] == "loser"]
    min5_feats = ["bz_direction", "bz_vol_ratio", "pm_am_ratio", "last3_slope", "bb5_pct_end", "bz_shrink"]
    for f in min5_feats:
        if f in df5.columns:
            d_val = cohens_d(w5[f].dropna().values, l5[f].dropna().values)
            print(f"  {f:<20s}: d={d_val:+.3f}")

    # ── Daily features Cohen's d (for Mode 2/3/4 discovery) ──
    print("\n" + "-" * 60)
    print("DAILY FEATURE Cohen's d (for multi-mode discovery)")
    print("-" * 60)
    w_all = df[df["label"] == "winner"]
    l_all = df[df["label"] == "loser"]
    daily_feats = ["weekly_slope", "weekly_ma10_slope", "weekly_close_vs_wma20",
                   "bb_width", "rsi14", "roc_3", "roc_5", "roc_10",
                   "lower_shadow_ratio", "consec_down", "amt_ratio"]
    for f in daily_feats:
        if f in df.columns:
            d_val = cohens_d(w_all[f].dropna().values, l_all[f].dropna().values)
            w_m = w_all[f].mean()
            l_m = l_all[f].mean()
            print(f"  {f:<25s}: d={d_val:+.3f}  winner={w_m:.3f}  loser={l_m:.3f}")

    print("\n" + "=" * 60)
    print("V10 BACKTEST COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
