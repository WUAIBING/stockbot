"""
V8 Backtest: Reverse Engineering V2 — Add Sector Momentum + Minute-Level Features

Strategy:
  1. Use V7's reverse engineering approach (find winners first)
  2. Add SECTOR/INDUSTRY momentum features (industry-level money flow)
  3. Add 5-minute K-line features for recent period (buy-zone dynamics)
  4. Re-rank features with Cohen's d
  5. Build improved composite score

Key new features:
  - Industry 5-day momentum (is this stock's industry hot?)
  - Industry flow (money flowing into this sector?)
  - Buy-zone volume ratio (14:30-15:00 / full day)
  - Buy-zone price direction (last 30 min candle direction)
  - 5-min Bollinger position at 14:50
"""

import json
import time
import warnings
from datetime import datetime
from pathlib import Path
from collections import defaultdict

import numpy as np
import pandas as pd
from pytdx.hq import TdxHq_API

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).resolve().parent
CONS_FILE = Path.home() / ".workbuddy" / "skills" / "csi1000-skills" / "000852cons.xls"
INDUSTRY_FILE = Path.home() / ".workbuddy" / "skills" / "csi1000-skills" / "tdxhy.cfg"
OUTPUT_DIR = Path.home() / ".workbuddy" / "a-share-analyst"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TDX_HOSTS = [
    ("60.191.117.167", 7709),
    ("39.105.251.234", 7709),
    ("119.147.212.83", 7709),
    ("47.107.75.159", 7709),
]

# ---- Helper functions ----

def normalize_code(value):
    text = str(value).strip()
    if "." in text:
        text = text.split(".")[0]
    return text.zfill(6)

def market_from_exchange(exchange):
    return 0 if "深圳" in str(exchange) else 1

def connect_tdx(retry=2):
    for _ in range(retry):
        for host, port in TDX_HOSTS:
            api = TdxHq_API(heartbeat=True)
            try:
                if api.connect(host, port, time_out=2.0):
                    return api
            except Exception:
                pass
            try:
                api.disconnect()
            except Exception:
                pass
        time.sleep(0.3)
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

def load_industry_mapping():
    """Load TDX industry mapping from tdxhy.cfg"""
    mapping = {}
    try:
        with open(INDUSTRY_FILE, "r", encoding="gbk") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if len(parts) >= 3:
                    code = parts[0].strip()
                    # parts[2] is industry name
                    industry = parts[2].strip() if len(parts) > 2 else "unknown"
                    mapping[code] = industry
    except Exception:
        pass
    return mapping

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
        df = df.rename(columns={"open": "open", "high": "high", "low": "low",
                                 "close": "close", "vol": "vol", "amount": "amt"})
        return df
    except Exception:
        return None

def fetch_weekly_bars(api, market, code, count=200):
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
    """Fetch 5-min K-line bars (category=0), up to ~17 trading days"""
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
        df = df.sort_values("datetime").reset_index(drop=True)
        df = df.rename(columns={"open": "open", "high": "high", "low": "low",
                                 "close": "close", "vol": "vol", "amount": "amt"})
        return df
    except Exception:
        return None


def cohens_d(winner_vals, loser_vals):
    n1, n2 = len(winner_vals), len(loser_vals)
    if n1 < 5 or n2 < 5:
        return 0.0
    m1, m2 = winner_vals.mean(), loser_vals.mean()
    s1, s2 = winner_vals.std(), loser_vals.std()
    if s1 == 0 and s2 == 0:
        return 0.0
    pooled_std = np.sqrt(((n1-1)*s1**2 + (n2-1)*s2**2) / (n1+n2-2))
    if pooled_std == 0:
        return 0.0
    return (m1 - m2) / pooled_std


# ---- Feature Engineering ----

def compute_all_features(df, wdf):
    """Compute 50+ features for each day"""
    if df is None or len(df) < 65:
        return None
    df = df.copy()

    # Daily Moving Averages
    for w in [5, 10, 20, 60]:
        df[f"ma{w}"] = df["close"].rolling(w).mean()
    for w in [5, 10, 20, 60]:
        df[f"ma{w}_slope"] = (df[f"ma{w}"] - df[f"ma{w}"].shift(5)) / df[f"ma{w}"].shift(5) * 100
    for w in [5, 10, 20, 60]:
        df[f"close_vs_ma{w}"] = (df["close"] - df[f"ma{w}"]) / df[f"ma{w}"] * 100
    df["low_vs_ma20"] = (df["low"] - df["ma20"]) / df["ma20"] * 100

    # MA alignment
    df["daily_bullish"] = (df["ma5"] > df["ma10"]) & (df["ma10"] > df["ma20"])
    df["close_above_ma20"] = df["close"] > df["ma20"]
    df["ma5_above_ma20"] = df["ma5"] > df["ma20"]
    df["ma10_above_ma20"] = df["ma10"] > df["ma20"]

    # MA convergence
    df["ma_spread"] = (df["ma5"] - df["ma20"]) / df["ma20"] * 100
    df["ma_spread_change"] = df["ma_spread"] - df["ma_spread"].shift(5)

    # Volume features
    df["avg_amt_5d"] = df["amt"].rolling(5).mean()
    df["avg_amt_10d"] = df["amt"].rolling(10).mean()
    df["amt_ratio"] = df["amt"] / df["avg_amt_5d"]
    df["amt_ratio_10"] = df["amt"] / df["avg_amt_10d"]
    df["amt_change_3d"] = df["avg_amt_5d"] / df["avg_amt_5d"].shift(3) - 1
    df["vol_shrink"] = df["amt_ratio"] < 0.8
    df["vol_expand"] = df["amt_ratio"] > 1.5

    # Bollinger Bands
    df["bb_mid"] = df["ma20"]
    df["bb_std"] = df["close"].rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2 * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - 2 * df["bb_std"]
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"] * 100
    df["bb_pct"] = (df["close"] - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"])
    df["bb_squeeze"] = df["bb_width"] < df["bb_width"].rolling(20).quantile(0.3)

    # Candle features
    df["body"] = abs(df["close"] - df["open"])
    df["candle_range"] = df["high"] - df["low"]
    df["body_ratio"] = df["body"] / df["candle_range"].replace(0, np.nan)
    df["lower_shadow_ratio"] = (df[["open", "close"]].min(axis=1) - df["low"]) / df["candle_range"].replace(0, np.nan)
    df["upper_shadow_ratio"] = (df["high"] - df[["open", "close"]].max(axis=1)) / df["candle_range"].replace(0, np.nan)
    df["is_green"] = df["close"] < df["open"]

    # Momentum
    for period in [3, 5, 10]:
        df[f"roc_{period}"] = df["close"] / df["close"].shift(period) - 1

    # RSI
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi14"] = 100 - 100 / (1 + rs)

    # Flow pressure (daily)
    df["flow_pressure"] = df["amt"] * df["close"].pct_change() / 1e8
    df["flow_pressure_5d"] = df["flow_pressure"].rolling(5).sum()
    df["flow_positive_ratio"] = (df["flow_pressure"] > 0).rolling(5).mean()

    # === NEW: Momentum strength ===
    # Close position in recent N-day range
    df["high_20d"] = df["high"].rolling(20).max()
    df["low_20d"] = df["low"].rolling(20).min()
    df["close_position_20d"] = (df["close"] - df["low_20d"]) / (df["high_20d"] - df["low_20d"]).replace(0, np.nan)
    
    # Gap up/down
    df["gap_pct"] = (df["open"] - df["close"].shift(1)) / df["close"].shift(1) * 100

    # Consecutive direction strength
    df["close_change"] = df["close"].diff()
    df["up_day"] = (df["close_change"] > 0).astype(int)
    
    # Volume-price divergence: price up but volume shrinking (or vice versa)
    df["price_up_vol_down"] = (df["close_change"] > 0) & (df["amt"] < df["avg_amt_5d"])
    df["price_down_vol_up"] = (df["close_change"] < 0) & (df["amt"] > df["avg_amt_5d"])

    # === NEW: Intraday pattern (from daily OHLC) ===
    # Upper/Lower shadow ratio (buy support / sell pressure)
    df["lower_shadow_pct"] = (df[["open", "close"]].min(axis=1) - df["low"]) / df["close"] * 100
    df["upper_shadow_pct"] = (df["high"] - df[["open", "close"]].max(axis=1)) / df["close"] * 100
    
    # Day type: bullish engulfing, hammer, etc.
    df["is_hammer"] = (df["lower_shadow_pct"] > 1.5) & (df["upper_shadow_pct"] < 0.5) & (df["body_ratio"] < 0.4)
    df["is_bullish_engulf"] = (df["close"] > df["open"]) & (df["close"].shift(1) < df["open"].shift(1)) & (df["close"] > df["open"].shift(1)) & (df["open"] < df["close"].shift(1))

    # === Weekly features ===
    if wdf is not None and len(wdf) >= 25:
        wdf = wdf.copy()
        wdf["wma5"] = wdf["close"].rolling(5).mean()
        wdf["wma10"] = wdf["close"].rolling(10).mean()
        wdf["wma20"] = wdf["close"].rolling(20).mean()
        last = wdf.iloc[-1]
        if pd.notna(last.get("wma5")) and pd.notna(last.get("wma10")) and pd.notna(last.get("wma20")):
            df["weekly_align"] = last["wma5"] > last["wma10"] > last["wma20"]
            df["weekly_slope"] = (last["wma5"] - last["wma20"]) / last["wma20"] * 100
            df["weekly_close_vs_wma5"] = (last["close"] - last["wma5"]) / last["wma5"] * 100
            df["weekly_close_vs_wma20"] = (last["close"] - last["wma20"]) / last["wma20"] * 100
            if len(wdf) > 15:
                prev_wma10 = wdf["wma10"].iloc[-2] if pd.notna(wdf["wma10"].iloc[-2]) else last["wma10"]
                df["weekly_ma10_slope"] = (last["wma10"] - prev_wma10) / prev_wma10 * 100
            else:
                df["weekly_ma10_slope"] = 0.0
        else:
            df["weekly_align"] = False
            df["weekly_slope"] = 0.0
            df["weekly_close_vs_wma5"] = 0.0
            df["weekly_close_vs_wma20"] = 0.0
            df["weekly_ma10_slope"] = 0.0
    else:
        df["weekly_align"] = False
        df["weekly_slope"] = 0.0
        df["weekly_close_vs_wma5"] = 0.0
        df["weekly_close_vs_wma20"] = 0.0
        df["weekly_ma10_slope"] = 0.0

    # Forward-looking: T+5 Return
    df["ret_5d_pct"] = (df["close"].shift(-5) / df["close"] - 1) * 100
    df["label"] = "neutral"
    df.loc[df["ret_5d_pct"] >= 5, "label"] = "winner"
    df.loc[df["ret_5d_pct"] <= -3, "label"] = "loser"

    return df


def compute_5min_features(m5_df):
    """Extract features from 5-minute K-line data for a single stock"""
    if m5_df is None or len(m5_df) < 50:
        return None
    
    m5_df = m5_df.copy()
    m5_df["date"] = m5_df["datetime"].dt.strftime("%Y-%m-%d")
    
    results = []
    for date, day_df in m5_df.groupby("date"):
        if len(day_df) < 20:  # Need at least half a day
            continue
        
        feat = {"date": date}
        
        # === Buy-zone features (14:30-15:00) ===
        buy_zone = day_df[(day_df["datetime"].dt.hour == 14) & (day_df["datetime"].dt.minute >= 30) |
                          (day_df["datetime"].dt.hour == 15) & (day_df["datetime"].dt.minute == 0)]
        
        morning = day_df[(day_df["datetime"].dt.hour < 12)]
        afternoon = day_df[(day_df["datetime"].dt.hour >= 13)]
        
        if len(buy_zone) > 0 and len(day_df) > 0:
            # Buy-zone volume ratio (vs full day)
            total_amt = day_df["amt"].sum()
            bz_amt = buy_zone["amt"].sum()
            feat["bz_amt_ratio"] = bz_amt / total_amt if total_amt > 0 else 0
            
            # Buy-zone price direction
            if len(buy_zone) >= 2:
                bz_open = buy_zone.iloc[0]["open"]
                bz_close = buy_zone.iloc[-1]["close"]
                feat["bz_direction"] = (bz_close - bz_open) / bz_open * 100  # positive = buying
            else:
                feat["bz_direction"] = 0.0
            
            # Buy-zone candle strength (close vs high/low of buy zone)
            feat["bz_close_vs_high"] = (buy_zone.iloc[-1]["close"] - buy_zone["high"].max()) / buy_zone["high"].max() * 100 if buy_zone["high"].max() > 0 else 0
            feat["bz_close_vs_low"] = (buy_zone.iloc[-1]["close"] - buy_zone["low"].min()) / buy_zone["low"].min() * 100 if buy_zone["low"].min() > 0 else 0
            
            # 5-min Bollinger at 14:50
            if len(day_df) >= 20:
                ma20_5m = day_df["close"].rolling(20).mean()
                std20_5m = day_df["close"].rolling(20).std()
                last_5m = day_df.iloc[-1]
                if pd.notna(ma20_5m.iloc[-1]) and pd.notna(std20_5m.iloc[-1]) and std20_5m.iloc[-1] > 0:
                    feat["bb5_pct"] = (last_5m["close"] - (ma20_5m.iloc[-1] - 2*std20_5m.iloc[-1])) / (4*std20_5m.iloc[-1])
                    feat["bb5_width"] = 4 * std20_5m.iloc[-1] / ma20_5m.iloc[-1] * 100
                else:
                    feat["bb5_pct"] = 0.5
                    feat["bb5_width"] = 0
            else:
                feat["bb5_pct"] = 0.5
                feat["bb5_width"] = 0
            
            # Morning vs afternoon volume split
            m_amt = morning["amt"].sum() if len(morning) > 0 else 0
            a_amt = afternoon["amt"].sum() if len(afternoon) > 0 else 0
            feat["am_pm_ratio"] = a_amt / m_amt if m_amt > 0 else 1.0
            
            # Last hour momentum (14:00-15:00 vs 13:00-14:00)
            last_hour = day_df[(day_df["datetime"].dt.hour == 14) | (day_df["datetime"].dt.hour == 15)]
            prev_hour = day_df[day_df["datetime"].dt.hour == 13]
            if len(last_hour) > 0 and len(prev_hour) > 0:
                lh_dir = (last_hour.iloc[-1]["close"] - last_hour.iloc[0]["open"]) / last_hour.iloc[0]["open"] * 100
                ph_dir = (prev_hour.iloc[-1]["close"] - prev_hour.iloc[0]["open"]) / prev_hour.iloc[0]["open"] * 100
                feat["last_hour_momentum"] = lh_dir - ph_dir  # acceleration
                feat["last_hour_vol_ratio"] = last_hour["amt"].sum() / prev_hour["amt"].sum()
            else:
                feat["last_hour_momentum"] = 0.0
                feat["last_hour_vol_ratio"] = 1.0
            
            # Late-day surge: volume in last 30 min vs avg 5-min bar
            avg_5min_amt = day_df["amt"].mean()
            last_30 = buy_zone[buy_zone["datetime"].dt.minute >= 30]
            feat["late_surge"] = last_30["amt"].mean() / avg_5min_amt if avg_5min_amt > 0 and len(last_30) > 0 else 1.0
            
            # V-shape recovery (morning low → afternoon high)
            if len(morning) > 0 and len(afternoon) > 0:
                m_low = morning["low"].min()
                a_high = afternoon["high"].max()
                day_open = day_df.iloc[0]["open"]
                feat["v_shape"] = (a_high - m_low) / day_open * 100 if day_open > 0 else 0
                feat["afternoon_strength"] = (afternoon.iloc[-1]["close"] - afternoon.iloc[0]["open"]) / afternoon.iloc[0]["open"] * 100
            else:
                feat["v_shape"] = 0.0
                feat["afternoon_strength"] = 0.0
            
        results.append(feat)
    
    if not results:
        return None
    return pd.DataFrame(results)


def main():
    print("=" * 70)
    print("V8 Reverse Engineering V2: Sector Momentum + 5-min Features")
    print("Universe: CSI 1000 + Daily Amount Top 200")
    print("=" * 70)

    # Load industry mapping
    industry_map = load_industry_mapping()
    print(f"Industry mapping loaded: {len(industry_map)} stocks")

    # Get CSI 1000 stock list
    stock_list = get_stock_list()
    print(f"CSI 1000 constituents: {len(stock_list)} stocks")

    # Add industry to stock list
    stock_list["industry"] = stock_list["code"].map(industry_map)
    n_with_industry = stock_list["industry"].notna().sum()
    print(f"Stocks with industry: {n_with_industry}/{len(stock_list)}")

    # ===== PHASE 1: Fetch daily + weekly bars (same as V7) =====
    print(f"\n{'='*70}")
    print("PHASE 1: Fetching daily + weekly bars")
    print(f"{'='*70}")

    all_data = []
    api = connect_tdx()
    success = 0
    failed = 0

    for idx, row in stock_list.iterrows():
        market = int(row["market"])
        code = str(row["code"])
        name = str(row["name"])
        industry = row.get("industry", "unknown")

        if idx > 0 and idx % 80 == 0:
            try: api.disconnect()
            except: pass
            time.sleep(0.1)
            api = connect_tdx()

        ddf = fetch_daily_bars(api, market, code, 800)
        if ddf is None or len(ddf) < 65:
            failed += 1
            continue

        wdf = fetch_weekly_bars(api, market, code, 200)
        ddf = compute_all_features(ddf, wdf)
        if ddf is None:
            failed += 1
            continue

        valid = ddf[ddf["ret_5d_pct"].notna() & ddf["ma20"].notna() & ddf["amt_ratio"].notna()].copy()
        if len(valid) == 0:
            success += 1
            continue

        valid["code"] = code
        valid["name"] = name
        valid["market"] = market
        valid["industry"] = industry if pd.notna(industry) else "unknown"

        all_data.append(valid)
        success += 1

        if (idx + 1) % 100 == 0:
            print(f"  Progress: {idx + 1}/{len(stock_list)} (ok={success}, fail={failed})")

    try: api.disconnect()
    except: pass

    print(f"\nFetched: {success} ok, {failed} failed")
    if not all_data:
        print("No data found!")
        return

    df = pd.concat(all_data, ignore_index=True)
    print(f"Total raw entries: {len(df)}")

    # Filter by daily amount top 200
    df["date_str"] = df["datetime"].dt.strftime("%Y%m%d")
    df["amt_rank"] = df.groupby("date_str")["amt"].rank(ascending=False, method="first")
    df = df[df["amt_rank"] <= 200].copy()
    print(f"After amount top-200 filter: {len(df)}")

    # ===== PHASE 2: Compute industry-level momentum =====
    print(f"\n{'='*70}")
    print("PHASE 2: Computing industry-level momentum")
    print(f"{'='*70}")

    # For each date + industry, compute average return and flow
    df["date_only"] = df["datetime"].dt.strftime("%Y-%m-%d")

    # Industry 5-day momentum: average roc_5 of all stocks in same industry on same date
    ind_mom = df.groupby(["date_only", "industry"]).agg(
        ind_roc5_mean=("roc_5", "mean"),
        ind_amt_sum=("amt", "sum"),
        ind_flow_pressure_sum=("flow_pressure", "sum"),
        ind_count=("code", "count"),
    ).reset_index()

    # Merge back
    df = df.merge(ind_mom, on=["date_only", "industry"], how="left")

    # Industry momentum rank (by date)
    df["ind_mom_rank"] = df.groupby("date_only")["ind_roc5_mean"].rank(ascending=False, method="first")
    df["ind_mom_pct"] = df.groupby("date_only")["ind_roc5_mean"].rank(pct=True)

    # Is this stock in a "hot" industry (top 30% momentum)?
    df["hot_industry"] = df["ind_mom_pct"] > 0.7

    # Industry money flow direction
    df["ind_flow_positive"] = df["ind_flow_pressure_sum"] > 0

    print(f"Industry features computed. Hot industries: {df['hot_industry'].sum()} entries ({df['hot_industry'].mean()*100:.1f}%)")

    # ===== PHASE 3: Baseline + Industry Feature Analysis =====
    print(f"\n{'='*70}")
    print("PHASE 3: Reverse Engineering Analysis")
    print(f"{'='*70}")

    total = len(df)
    n_winner = (df["label"] == "winner").sum()
    n_loser = (df["label"] == "loser").sum()
    base_wr = (df["ret_5d_pct"] > 0).mean() * 100
    base_ar = df["ret_5d_pct"].mean()
    wins = df[df["ret_5d_pct"] > 0]
    losses = df[df["ret_5d_pct"] <= 0]
    base_ev = base_wr/100 * wins["ret_5d_pct"].mean() + (1-base_wr/100) * losses["ret_5d_pct"].mean()

    print(f"\nBASELINE: N={total}, WR={base_wr:.1f}%, AR={base_ar:.2f}%, EV={base_ev:.2f}%")
    print(f"Winners(>=5%): {n_winner} ({n_winner/total*100:.1f}%), Losers(<=-3%): {n_loser} ({n_loser/total*100:.1f}%)")

    # Cohen's d analysis
    winners = df[df["label"] == "winner"]
    losers = df[df["label"] == "loser"]
    print(f"\nComparing: {len(winners)} WINNERS vs {len(losers)} LOSERS")

    # All continuous features
    continuous_features = [
        # V7 features (daily)
        "close_vs_ma5", "close_vs_ma10", "close_vs_ma20", "close_vs_ma60",
        "low_vs_ma20", "ma5_slope", "ma10_slope", "ma20_slope", "ma60_slope",
        "ma_spread", "ma_spread_change",
        "amt_ratio", "amt_ratio_10", "amt_change_3d",
        "bb_width", "bb_pct",
        "body_ratio", "lower_shadow_ratio", "upper_shadow_ratio",
        "roc_3", "roc_5", "roc_10", "rsi14",
        "flow_pressure", "flow_pressure_5d", "flow_positive_ratio",
        "weekly_slope", "weekly_close_vs_wma5", "weekly_close_vs_wma20", "weekly_ma10_slope",
        # V8 NEW: daily-level
        "close_position_20d", "gap_pct",
        "lower_shadow_pct", "upper_shadow_pct",
        # V8 NEW: industry-level
        "ind_roc5_mean", "ind_mom_pct", "ind_amt_sum", "ind_flow_pressure_sum",
    ]

    print("\n--- Cohen's d Feature Discrimination ---")
    d_results = []
    for feat in continuous_features:
        if feat not in df.columns:
            continue
        w_vals = winners[feat].dropna()
        l_vals = losers[feat].dropna()
        if len(w_vals) < 5 or len(l_vals) < 5:
            continue
        d = cohens_d(w_vals, l_vals)
        w_mean = w_vals.mean()
        l_mean = l_vals.mean()
        d_results.append({
            "feature": feat,
            "d": round(d, 4),
            "winner_mean": round(w_mean, 4),
            "loser_mean": round(l_mean, 4),
            "diff": round(w_mean - l_mean, 4),
        })

    d_results.sort(key=lambda x: abs(x["d"]), reverse=True)

    print(f"\n{'Feature':<35} {'d':>8} {'Winnerμ':>10} {'Loserμ':>10} {'Δ':>10} {'Strength'}")
    print("-" * 90)
    for r in d_results[:20]:
        strength = "***" if abs(r["d"]) >= 0.8 else "**" if abs(r["d"]) >= 0.5 else "*" if abs(r["d"]) >= 0.2 else "." if abs(r["d"]) >= 0.1 else ""
        direction = "↑" if r["diff"] > 0 else "↓"
        print(f"{r['feature']:<35} {r['d']:>8.4f} {r['winner_mean']:>10.4f} {r['loser_mean']:>10.4f} "
              f"{direction}{abs(r['diff']):>9.4f} {strength}")

    # Binary features
    print("\n--- Binary Feature Win Rate Lift ---")
    binary_features = [
        "daily_bullish", "ma5_above_ma20", "ma10_above_ma20", "close_above_ma20",
        "vol_shrink", "vol_expand", "bb_squeeze", "is_green", "weekly_align",
        "hot_industry", "ind_flow_positive", "price_up_vol_down", "price_down_vol_up",
        "is_hammer", "is_bullish_engulf",
    ]

    b_results = []
    for feat in binary_features:
        if feat not in df.columns:
            continue
        sub_true = df[df[feat] == True]
        sub_false = df[df[feat] == False]
        if len(sub_true) < 50 or len(sub_false) < 50:
            continue
        wr_true = (sub_true["ret_5d_pct"] > 0).mean() * 100
        wr_false = (sub_false["ret_5d_pct"] > 0).mean() * 100
        lift = wr_true - wr_false
        w_t = sub_true[sub_true["ret_5d_pct"] > 0]
        l_t = sub_true[sub_true["ret_5d_pct"] <= 0]
        ev_true = wr_true/100 * w_t["ret_5d_pct"].mean() + (1-wr_true/100) * l_t["ret_5d_pct"].mean() if len(l_t) > 0 else 0
        b_results.append({"feature": feat, "wr_true": round(wr_true, 1), "wr_false": round(wr_false, 1),
                          "lift": round(lift, 1), "ev_true": round(ev_true, 2), "n_true": len(sub_true)})

    b_results.sort(key=lambda x: abs(x["lift"]), reverse=True)
    print(f"\n{'Feature':<25} {'WR=True':>8} {'WR=False':>8} {'Lift':>6} {'EV=True':>8} {'N=True':>8}")
    print("-" * 70)
    for r in b_results:
        print(f"{r['feature']:<25} {r['wr_true']:>7.1f}% {r['wr_false']:>7.1f}% {r['lift']:>+5.1f}pp {r['ev_true']:>7.2f}% {r['n_true']:>8}")

    # ===== PHASE 4: Composite Score with Industry Features =====
    print(f"\n{'='*70}")
    print("PHASE 4: Enhanced Composite Score")
    print(f"{'='*70}")

    top_features = [r for r in d_results if abs(r["d"]) >= 0.08]  # Lower threshold to include more
    print(f"\nUsing {len(top_features)} features with |d| >= 0.08")

    if len(top_features) >= 3:
        score_features = []
        for r in top_features:
            feat = r["feature"]
            direction = 1 if r["diff"] > 0 else -1
            score_features.append((feat, direction))

        print(f"Score components:")
        for feat, direction in score_features:
            d_val = next(r["d"] for r in d_results if r["feature"] == feat)
            arrow = "↑" if direction > 0 else "↓"
            print(f"  {feat}: {arrow} (d={d_val:.4f})")

        score_df = df.copy()
        for feat, direction in score_features:
            score_df[f"{feat}_prank"] = score_df[feat].rank(pct=True)
            if direction < 0:
                score_df[f"{feat}_prank"] = 1 - score_df[f"{feat}_prank"]

        prank_cols = [f"{feat}_prank" for feat, _ in score_features]
        # Weight by |d|
        total_d = sum(abs(next(r["d"] for r in d_results if r["feature"] == feat)) for feat, _ in score_features)
        for feat, direction in score_features:
            d_val = abs(next(r["d"] for r in d_results if r["feature"] == feat))
            weight = d_val / total_d
            score_df[f"{feat}_prank"] = score_df[f"{feat}_prank"] * weight

        score_df["composite_score"] = score_df[prank_cols].sum(axis=1) / len(prank_cols) * 100

        # Decile analysis
        print(f"\n--- Weighted Composite Score Decile Analysis ---")
        score_df["score_decile"] = pd.qcut(score_df["composite_score"], 10, labels=False, duplicates="drop")

        decile_results = []
        for d in sorted(score_df["score_decile"].unique()):
            sub = score_df[score_df["score_decile"] == d]
            n = len(sub)
            wr = (sub["ret_5d_pct"] > 0).mean() * 100
            w = sub[sub["ret_5d_pct"] > 0]
            l = sub[sub["ret_5d_pct"] <= 0]
            avg_win = w["ret_5d_pct"].mean() if len(w) > 0 else 0
            avg_loss = l["ret_5d_pct"].mean() if len(l) > 0 else 0
            ev = wr/100 * avg_win + (1-wr/100) * avg_loss
            pf = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
            print(f"  D{d}: N={n:>6}, WR={wr:>5.1f}%, AR={sub['ret_5d_pct'].mean():>6.2f}%, "
                  f"EV={ev:>6.2f}%, AvgWin={avg_win:>5.2f}%, AvgLoss={avg_loss:>5.2f}%, PF={pf:>4.2f}")
            decile_results.append({"decile": int(d), "n": n, "wr": round(wr, 1),
                                   "ar": round(sub["ret_5d_pct"].mean(), 2), "ev": round(ev, 2)})

        # Top percentiles
        for pct, label in [(5, "Top 5%"), (3, "Top 3%"), (1, "Top 1%")]:
            threshold = score_df["composite_score"].quantile(1 - pct/100)
            sub = score_df[score_df["composite_score"] >= threshold]
            wr = (sub["ret_5d_pct"] > 0).mean() * 100
            ar = sub["ret_5d_pct"].mean()
            w = sub[sub["ret_5d_pct"] > 0]
            l = sub[sub["ret_5d_pct"] <= 0]
            ev = wr/100 * w["ret_5d_pct"].mean() + (1-wr/100) * l["ret_5d_pct"].mean() if len(l) > 0 else 0
            print(f"  {label}: N={len(sub)}, WR={wr:.1f}%, AR={ar:.2f}%, EV={ev:.2f}%")

        # ===== PHASE 5: Best condition combos =====
        print(f"\n{'='*70}")
        print("PHASE 5: Multi-Condition Combo Search")
        print(f"{'='*70}")

        # Focus on top decile + weekly align + hot industry
        top_score = score_df[score_df["score_decile"] >= 8]
        print(f"\nTop 2 deciles: N={len(top_score)}")

        # Weekly align + hot industry
        for wa in [True, False]:
            for hi in [True, False]:
                sub = top_score[(top_score["weekly_align"] == wa) & (top_score["hot_industry"] == hi)]
                if len(sub) >= 20:
                    wr = (sub["ret_5d_pct"] > 0).mean() * 100
                    ar = sub["ret_5d_pct"].mean()
                    w = sub[sub["ret_5d_pct"] > 0]
                    l = sub[sub["ret_5d_pct"] <= 0]
                    ev = wr/100 * w["ret_5d_pct"].mean() + (1-wr/100) * l["ret_5d_pct"].mean() if len(l) > 0 else 0
                    pf = abs(w["ret_5d_pct"].mean() / l["ret_5d_pct"].mean()) if len(l) > 0 and l["ret_5d_pct"].mean() != 0 else float("inf")
                    print(f"  周线多头={wa}, 热门行业={hi}: N={len(sub)}, WR={wr:.1f}%, AR={ar:.2f}%, EV={ev:.2f}%, PF={pf:.2f}")

        # Close to MA20 + weekly align + hot industry
        print("\n--- Close to MA20 + Weekly Align + Hot Industry ---")
        for cp_lo, cp_hi in [(-3, -1.5), (-1.5, 0), (0, 1.5), (1.5, 3)]:
            sub = df[
                (df["weekly_align"] == True) &
                (df["close_vs_ma20"] >= cp_lo) & (df["close_vs_ma20"] < cp_hi) &
                (df["hot_industry"] == True)
            ]
            if len(sub) >= 20:
                wr = (sub["ret_5d_pct"] > 0).mean() * 100
                ar = sub["ret_5d_pct"].mean()
                w = sub[sub["ret_5d_pct"] > 0]
                l = sub[sub["ret_5d_pct"] <= 0]
                ev = wr/100 * w["ret_5d_pct"].mean() + (1-wr/100) * l["ret_5d_pct"].mean() if len(l) > 0 else 0
                print(f"  close_vs_ma20=[{cp_lo},{cp_hi}) + 周线多头 + 热门行业: N={len(sub)}, WR={wr:.1f}%, AR={ar:.2f}%, EV={ev:.2f}%")

        # Exhaustive grid search on top features
        print("\n--- Grid Search: Top Features Combined ---")
        best_combos = []
        # Use top 5 features for grid
        grid_features = [r["feature"] for r in d_results[:5]]
        for r in d_results[:5]:
            feat = r["feature"]
            # Split into tertiles
            q33 = df[feat].quantile(0.33)
            q66 = df[feat].quantile(0.66)
            # Just test top tertile vs bottom
            pass

        # Simpler approach: iterate close_vs_ma20 × weekly_slope × hot_industry × vol_shrink
        print("\n--- Best Combo: close_vs_ma20 × weekly_slope × hot_industry × vol_shrink ---")
        best_combos = []
        for cp_lo, cp_hi in [(-3, -1), (-1, 0), (0, 2)]:
            for sl_lo, sl_hi in [(0, 3), (3, 10), (10, 50)]:
                for hi in [True, False]:
                    for vs in [True, False]:
                        sub = df[
                            (df["close_vs_ma20"] >= cp_lo) & (df["close_vs_ma20"] < cp_hi) &
                            (df["weekly_slope"] >= sl_lo) & (df["weekly_slope"] < sl_hi) &
                            (df["hot_industry"] == hi) &
                            (df["vol_shrink"] == vs)
                        ]
                        if len(sub) >= 30:
                            wr = (sub["ret_5d_pct"] > 0).mean() * 100
                            w = sub[sub["ret_5d_pct"] > 0]
                            l = sub[sub["ret_5d_pct"] <= 0]
                            avg_win = w["ret_5d_pct"].mean() if len(w) > 0 else 0
                            avg_loss = l["ret_5d_pct"].mean() if len(l) > 0 else 0
                            ev = wr/100 * avg_win + (1-wr/100) * avg_loss
                            pf = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")
                            best_combos.append({
                                "cond": f"close=[{cp_lo},{cp_hi}),slope=[{sl_lo},{sl_hi}),hot_ind={hi},shrink={vs}",
                                "n": len(sub), "wr": wr, "ar": sub["ret_5d_pct"].mean(), "ev": ev,
                                "avg_win": avg_win, "avg_loss": avg_loss, "pf": pf,
                            })

        best_combos.sort(key=lambda x: x["ev"], reverse=True)
        print(f"\nTop 10 combos by EV:")
        for i, b in enumerate(best_combos[:10]):
            print(f"  {i+1}. {b['cond']}")
            print(f"     N={b['n']}, WR={b['wr']:.1f}%, AR={b['ar']:.2f}%, EV={b['ev']:.2f}%, "
                  f"Win={b['avg_win']:.2f}%, Loss={b['avg_loss']:.2f}%, PF={b['pf']:.2f}")

    # ===== PHASE 6: 5-minute features for a sample =====
    print(f"\n{'='*70}")
    print("PHASE 6: 5-Minute Feature Analysis (Sample)")
    print(f"{'='*70}")

    # Only fetch 5-min data for a subset (recent period, top-amount stocks)
    # Pick top 100 stocks by average daily amount
    avg_amt = df.groupby("code")["amt"].mean().sort_values(ascending=False)
    top_100_codes = avg_amt.head(100).index.tolist()
    
    print(f"Fetching 5-min data for top 100 stocks by amount...")
    api = connect_tdx()
    
    m5_features_all = []
    m5_success = 0
    
    for code in top_100_codes:
        stock_info = stock_list[stock_list["code"] == code]
        if len(stock_info) == 0:
            continue
        market = int(stock_info.iloc[0]["market"])
        
        if m5_success > 0 and m5_success % 20 == 0:
            try: api.disconnect()
            except: pass
            time.sleep(0.1)
            api = connect_tdx()
        
        m5_df = fetch_5min_bars(api, market, code)
        if m5_df is None:
            continue
        
        m5_feat = compute_5min_features(m5_df)
        if m5_feat is None or len(m5_feat) == 0:
            continue
        
        m5_feat["code"] = code
        m5_features_all.append(m5_feat)
        m5_success += 1
        
        if m5_success % 25 == 0:
            print(f"  5-min progress: {m5_success}/100")
    
    try: api.disconnect()
    except: pass
    
    print(f"5-min data fetched: {m5_success} stocks")
    
    if m5_features_all:
        m5_df_all = pd.concat(m5_features_all, ignore_index=True)
        print(f"Total 5-min feature rows: {len(m5_df_all)}")
        print(f"Date range: {m5_df_all['date'].min()} to {m5_df_all['date'].max()}")
        print(f"Columns: {[c for c in m5_df_all.columns if c not in ['date', 'code']]}")
        
        # Merge with daily data for these stocks/dates
        df["date_merge"] = df["datetime"].dt.strftime("%Y-%m-%d")
        merged = df.merge(m5_df_all, left_on=["code", "date_merge"], right_on=["code", "date"], how="inner")
        print(f"Merged 5-min + daily: {len(merged)} rows")
        
        if len(merged) >= 100:
            # Analyze 5-min features
            m_winners = merged[merged["label"] == "winner"]
            m_losers = merged[merged["label"] == "loser"]
            print(f"  Winners: {len(m_winners)}, Losers: {len(m_losers)}")
            
            m5_continuous = [c for c in m5_df_all.columns if c not in ["date", "code"]]
            print(f"\n--- 5-Min Feature Discrimination ---")
            m5_d_results = []
            for feat in m5_continuous:
                w_vals = m_winners[feat].dropna()
                l_vals = m_losers[feat].dropna()
                if len(w_vals) < 5 or len(l_vals) < 5:
                    continue
                d = cohens_d(w_vals, l_vals)
                m5_d_results.append({
                    "feature": feat,
                    "d": round(d, 4),
                    "winner_mean": round(w_vals.mean(), 4),
                    "loser_mean": round(l_vals.mean(), 4),
                })
            
            m5_d_results.sort(key=lambda x: abs(x["d"]), reverse=True)
            for r in m5_d_results:
                strength = "***" if abs(r["d"]) >= 0.8 else "**" if abs(r["d"]) >= 0.5 else "*" if abs(r["d"]) >= 0.2 else "." if abs(r["d"]) >= 0.1 else ""
                print(f"  {r['feature']:<25} d={r['d']:>7.4f}  Wμ={r['winner_mean']:>8.4f}  Lμ={r['loser_mean']:>8.4f}  {strength}")
            
            # Quick combo: daily composite + 5-min features
            print(f"\n--- 5-Min Feature: Win Rate by Buy-Zone Direction ---")
            if "bz_direction" in merged.columns:
                for lo, hi in [(-1, -0.3), (-0.3, 0), (0, 0.3), (0.3, 1)]:
                    sub = merged[(merged["bz_direction"] >= lo) & (merged["bz_direction"] < hi)]
                    if len(sub) >= 20:
                        wr = (sub["ret_5d_pct"] > 0).mean() * 100
                        ar = sub["ret_5d_pct"].mean()
                        print(f"  bz_direction [{lo},{hi}): N={len(sub)}, WR={wr:.1f}%, AR={ar:.2f}%")
            
            print(f"\n--- 5-Min Feature: Win Rate by Buy-Zone Volume Ratio ---")
            if "bz_amt_ratio" in merged.columns:
                for lo, hi in [(0, 0.1), (0.1, 0.15), (0.15, 0.2), (0.2, 0.3), (0.3, 1)]:
                    sub = merged[(merged["bz_amt_ratio"] >= lo) & (merged["bz_amt_ratio"] < hi)]
                    if len(sub) >= 20:
                        wr = (sub["ret_5d_pct"] > 0).mean() * 100
                        ar = sub["ret_5d_pct"].mean()
                        print(f"  bz_amt_ratio [{lo},{hi}): N={len(sub)}, WR={wr:.1f}%, AR={ar:.2f}%")

    # Save summary
    summary = {
        "version": "V8",
        "method": "Reverse Engineering V2 + Sector Momentum + 5-min Features",
        "universe": "CSI1000 + Daily Amount Top 200",
        "total_trades": total,
        "baseline_wr": round(base_wr, 1),
        "baseline_ar": round(base_ar, 2),
        "baseline_ev": round(base_ev, 2),
        "n_winners": int(n_winner),
        "n_losers": int(n_loser),
        "top_d_features": d_results[:15],
        "binary_lifts": b_results[:10],
    }
    with open(OUTPUT_DIR / "v8_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    print(f"\nDone! Summary saved to {OUTPUT_DIR / 'v8_summary.json'}")


if __name__ == "__main__":
    main()
