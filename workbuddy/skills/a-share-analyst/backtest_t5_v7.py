"""
V7 Backtest: Reverse Engineering — "Find Winners First, Learn Patterns Second"

Methodology shift:
  V1-V6: Define signals → filter → measure win rate
  V7:    Find T+5 winners → extract ALL features on buy day → discover what makes them win

Key steps:
  1. Load CSI 1000 + daily amount top 200
  2. For each stock-day, compute T+5 return
  3. Label: WINNER (T+5 >= +5%), LOSER (T+5 <= -3%), NEUTRAL (in between)
  4. Extract 40+ features on buy day (MA, volume, Bollinger, momentum, flow, pattern)
  5. Cohen's d: which features best discriminate WINNERS vs LOSERS?
  6. Build composite score from top discriminative features
  7. Backtest: does high score predict higher win rate & EV?
"""

import json
import time
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from pytdx.hq import TdxHq_API

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).resolve().parent
CONS_FILE = Path.home() / ".workbuddy" / "skills" / "csi1000-skills" / "000852cons.xls"
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

# ---- Feature Engineering ----

def compute_all_features(df, wdf):
    """
    Compute 40+ features for each day.
    Returns a DataFrame with feature columns + ret_5d_pct (forward-looking).
    """
    if df is None or len(df) < 65:
        return None

    df = df.copy()

    # ===== Daily Moving Averages =====
    for w in [5, 10, 20, 60]:
        df[f"ma{w}"] = df["close"].rolling(w).mean()

    # MA slopes (5-day lookback)
    for w in [5, 10, 20, 60]:
        df[f"ma{w}_slope"] = (df[f"ma{w}"] - df[f"ma{w}"].shift(5)) / df[f"ma{w}"].shift(5) * 100

    # Close relative to each MA (pct deviation)
    for w in [5, 10, 20, 60]:
        df[f"close_vs_ma{w}"] = (df["close"] - df[f"ma{w}"]) / df[f"ma{w}"] * 100

    # Low relative to MA20
    df["low_vs_ma20"] = (df["low"] - df["ma20"]) / df["ma20"] * 100

    # MA alignment signals
    df["daily_bullish"] = (df["ma5"] > df["ma10"]) & (df["ma10"] > df["ma20"])
    df["ma5_above_ma20"] = df["ma5"] > df["ma20"]
    df["ma10_above_ma20"] = df["ma10"] > df["ma20"]
    df["close_above_ma20"] = df["close"] > df["ma20"]

    # MA convergence: MA5-MA20 spread narrowing (vs 5 days ago)
    df["ma_spread"] = (df["ma5"] - df["ma20"]) / df["ma20"] * 100
    df["ma_spread_change"] = df["ma_spread"] - df["ma_spread"].shift(5)  # negative = converging

    # ===== Volume Features =====
    df["avg_amt_5d"] = df["amt"].rolling(5).mean()
    df["avg_amt_10d"] = df["amt"].rolling(10).mean()
    df["amt_ratio"] = df["amt"] / df["avg_amt_5d"]  # today vs 5d avg
    df["amt_ratio_10"] = df["amt"] / df["avg_amt_10d"]  # today vs 10d avg
    df["amt_change_3d"] = df["avg_amt_5d"] / df["avg_amt_5d"].shift(3) - 1  # 5d avg change
    df["vol_shrink"] = df["amt_ratio"] < 0.8
    df["vol_expand"] = df["amt_ratio"] > 1.5

    # ===== Bollinger Bands (20,2) =====
    df["bb_mid"] = df["ma20"]
    df["bb_std"] = df["close"].rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2 * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - 2 * df["bb_std"]
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"] * 100  # bandwidth %
    df["bb_pct"] = (df["close"] - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"])  # 0-1 position
    df["bb_squeeze"] = df["bb_width"] < df["bb_width"].rolling(20).quantile(0.3)  # narrow bandwidth

    # ===== Candlestick Pattern Features =====
    df["body"] = abs(df["close"] - df["open"])
    df["upper_shadow"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["lower_shadow"] = df[["open", "close"]].min(axis=1) - df["low"]
    df["candle_range"] = df["high"] - df["low"]
    df["body_ratio"] = df["body"] / df["candle_range"].replace(0, np.nan)  # body proportion
    df["lower_shadow_ratio"] = df["lower_shadow"] / df["candle_range"].replace(0, np.nan)
    df["is_green"] = df["close"] < df["open"]  # bearish candle

    # ===== Momentum Features =====
    # Rate of change
    for period in [3, 5, 10]:
        df[f"roc_{period}"] = df["close"] / df["close"].shift(period) - 1

    # RSI(14)
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi14"] = 100 - 100 / (1 + rs)

    # Consecutive days up/down
    df["up_day"] = (df["close"] > df["open"]).astype(int)
    df["consec_up"] = df["up_day"].groupby((df["up_day"] != df["up_day"].shift()).cumsum()).cumcount()
    df["consec_down"] = (~df["up_day"].astype(bool)).astype(int).groupby(
        (df["up_day"] != df["up_day"].shift()).cumsum()
    ).cumcount()

    # ===== Flow Pressure (daily-level approximation) =====
    df["flow_pressure"] = df["amt"] * df["close"].pct_change() / 1e8  # in 亿
    df["flow_pressure_5d"] = df["flow_pressure"].rolling(5).sum()
    df["flow_positive_ratio"] = (df["flow_pressure"] > 0).rolling(5).mean()

    # ===== Weekly Features =====
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
            # Weekly MA10 slope
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

    # ===== Forward-looking: T+5 Return =====
    df["ret_5d_pct"] = (df["close"].shift(-5) / df["close"] - 1) * 100

    # ===== Winner / Loser Label =====
    df["label"] = "neutral"
    df.loc[df["ret_5d_pct"] >= 5, "label"] = "winner"
    df.loc[df["ret_5d_pct"] <= -3, "label"] = "loser"

    return df


def cohens_d(winner_vals, loser_vals):
    """Compute Cohen's d effect size between two groups"""
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


def main():
    print("=" * 70)
    print("V7 Reverse Engineering: Find Winners → Learn Patterns")
    print("Universe: CSI 1000 + Daily Amount Top 200")
    print("=" * 70)

    # Step 1: Get CSI 1000 stock list
    stock_list = get_stock_list()
    print(f"CSI 1000 constituents: {len(stock_list)} stocks")

    # Step 2: Fetch daily + weekly bars for all stocks
    all_data = []
    api = connect_tdx()
    success = 0
    failed = 0

    print(f"\nFetching data for {len(stock_list)} stocks...")
    for idx, row in stock_list.iterrows():
        market = int(row["market"])
        code = str(row["code"])
        name = str(row["name"])

        # Reconnect periodically
        if idx > 0 and idx % 80 == 0:
            try:
                api.disconnect()
            except Exception:
                pass
            time.sleep(0.1)
            api = connect_tdx()

        # Fetch daily bars
        ddf = fetch_daily_bars(api, market, code, 800)
        if ddf is None or len(ddf) < 65:
            failed += 1
            continue

        # Fetch weekly bars
        wdf = fetch_weekly_bars(api, market, code, 200)

        # Compute all features
        ddf = compute_all_features(ddf, wdf)
        if ddf is None:
            failed += 1
            continue

        # Filter: need ret_5d_pct and key features
        valid = ddf[
            ddf["ret_5d_pct"].notna() &
            ddf["ma20"].notna() &
            ddf["amt_ratio"].notna()
        ].copy()

        if len(valid) == 0:
            success += 1
            continue

        valid["code"] = code
        valid["name"] = name
        valid["market"] = market

        all_data.append(valid)
        success += 1

        if (idx + 1) % 100 == 0:
            print(f"  Progress: {idx + 1}/{len(stock_list)} (ok={success}, fail={failed})")

    try:
        api.disconnect()
    except Exception:
        pass

    print(f"\nFetched: {success} ok, {failed} failed")

    if not all_data:
        print("No data found!")
        return

    df = pd.concat(all_data, ignore_index=True)
    print(f"Total raw entries: {len(df)}")

    # Step 3: Filter by daily amount top 200
    df["date_str"] = df["datetime"].dt.strftime("%Y%m%d")
    df["amt_rank"] = df.groupby("date_str")["amt"].rank(ascending=False, method="first")
    df = df[df["amt_rank"] <= 200].copy()
    print(f"After amount top-200 filter: {len(df)}")

    # Step 4: Basic statistics
    total = len(df)
    n_winner = (df["label"] == "winner").sum()
    n_loser = (df["label"] == "loser").sum()
    n_neutral = (df["label"] == "neutral").sum()
    base_wr = (df["ret_5d_pct"] > 0).mean() * 100
    base_ar = df["ret_5d_pct"].mean()
    wins = df[df["ret_5d_pct"] > 0]
    losses = df[df["ret_5d_pct"] <= 0]
    base_ev = base_wr/100 * wins["ret_5d_pct"].mean() + (1-base_wr/100) * losses["ret_5d_pct"].mean()

    print(f"\n{'='*70}")
    print(f"BASELINE: N={total}, WR={base_wr:.1f}%, AR={base_ar:.2f}%, EV={base_ev:.2f}%")
    print(f"Winners(>=5%): {n_winner} ({n_winner/total*100:.1f}%), "
          f"Losers(<=-3%): {n_loser} ({n_loser/total*100:.1f}%), "
          f"Neutral: {n_neutral} ({n_neutral/total*100:.1f}%)")
    print(f"{'='*70}")

    # Save full data
    df.to_csv(OUTPUT_DIR / "v7_data_full.csv", index=False, encoding="utf-8-sig")
    print(f"Full data saved to {OUTPUT_DIR / 'v7_data_full.csv'}")

    # Step 5: Reverse Engineering — What makes WINNERS different?
    print(f"\n{'='*70}")
    print("REVERSE ENGINEERING: Winner vs Loser Feature Analysis")
    print(f"{'='*70}")

    winners = df[df["label"] == "winner"]
    losers = df[df["label"] == "loser"]
    print(f"\nComparing: {len(winners)} WINNERS vs {len(losers)} LOSERS")

    # Continuous features to analyze
    continuous_features = [
        # MA features
        "close_vs_ma5", "close_vs_ma10", "close_vs_ma20", "close_vs_ma60",
        "low_vs_ma20", "ma5_slope", "ma10_slope", "ma20_slope", "ma60_slope",
        "ma_spread", "ma_spread_change",
        # Volume features
        "amt_ratio", "amt_ratio_10", "amt_change_3d",
        # Bollinger features
        "bb_width", "bb_pct",
        # Candle features
        "body_ratio", "lower_shadow_ratio",
        # Momentum features
        "roc_3", "roc_5", "roc_10", "rsi14",
        # Flow features
        "flow_pressure", "flow_pressure_5d", "flow_positive_ratio",
        # Weekly features
        "weekly_slope", "weekly_close_vs_wma5", "weekly_close_vs_wma20", "weekly_ma10_slope",
    ]

    # Binary features to analyze
    binary_features = [
        "daily_bullish", "ma5_above_ma20", "ma10_above_ma20", "close_above_ma20",
        "vol_shrink", "vol_expand", "bb_squeeze", "is_green", "weekly_align",
    ]

    # ---- Cohen's d for continuous features ----
    print("\n--- Continuous Feature Discrimination (Cohen's d) ---")
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

    # Sort by absolute d value
    d_results.sort(key=lambda x: abs(x["d"]), reverse=True)

    print(f"\n{'Feature':<30} {'d':>8} {'Winnerμ':>10} {'Loserμ':>10} {'Δ':>10} {'Strength'}")
    print("-" * 85)
    for r in d_results:
        strength = "***" if abs(r["d"]) >= 0.8 else "**" if abs(r["d"]) >= 0.5 else "*" if abs(r["d"]) >= 0.2 else ""
        direction = "↑" if r["diff"] > 0 else "↓"
        print(f"{r['feature']:<30} {r['d']:>8.4f} {r['winner_mean']:>10.4f} {r['loser_mean']:>10.4f} "
              f"{direction}{abs(r['diff']):>9.4f} {strength}")

    # ---- Win rate lift for binary features ----
    print("\n--- Binary Feature Win Rate Lift ---")
    b_results = []
    for feat in binary_features:
        if feat not in df.columns:
            continue
        # When feature = True
        sub_true = df[df[feat] == True]
        sub_false = df[df[feat] == False]
        if len(sub_true) < 50 or len(sub_false) < 50:
            continue
        wr_true = (sub_true["ret_5d_pct"] > 0).mean() * 100
        wr_false = (sub_false["ret_5d_pct"] > 0).mean() * 100
        lift = wr_true - wr_false
        # Also compute EV
        w_t = sub_true[sub_true["ret_5d_pct"] > 0]
        l_t = sub_true[sub_true["ret_5d_pct"] <= 0]
        ev_true = wr_true/100 * w_t["ret_5d_pct"].mean() + (1-wr_true/100) * l_t["ret_5d_pct"].mean() if len(l_t) > 0 else 0

        w_f = sub_false[sub_false["ret_5d_pct"] > 0]
        l_f = sub_false[sub_false["ret_5d_pct"] <= 0]
        ev_false = wr_false/100 * w_f["ret_5d_pct"].mean() + (1-wr_false/100) * l_f["ret_5d_pct"].mean() if len(l_f) > 0 else 0

        b_results.append({
            "feature": feat,
            "wr_true": round(wr_true, 1),
            "wr_false": round(wr_false, 1),
            "lift": round(lift, 1),
            "ev_true": round(ev_true, 2),
            "ev_false": round(ev_false, 2),
            "n_true": len(sub_true),
            "n_false": len(sub_false),
        })

    b_results.sort(key=lambda x: abs(x["lift"]), reverse=True)
    print(f"\n{'Feature':<25} {'WR=True':>8} {'WR=False':>8} {'Lift':>6} {'EV=True':>8} {'EV=False':>8} {'N=True':>8}")
    print("-" * 80)
    for r in b_results:
        print(f"{r['feature']:<25} {r['wr_true']:>7.1f}% {r['wr_false']:>7.1f}% {r['lift']:>+5.1f}pp "
              f"{r['ev_true']:>7.2f}% {r['ev_false']:>7.2f}% {r['n_true']:>8}")

    # Step 6: Winner Profile — What does a typical winner look like?
    print(f"\n{'='*70}")
    print("WINNER PROFILE: Typical winner's buy-day characteristics")
    print(f"{'='*70}")

    profile_features = [r["feature"] for r in d_results[:15]]  # top 15 continuous
    print(f"\n{'Feature':<30} {'Winner Median':>15} {'Loser Median':>15} {'All Median':>15}")
    print("-" * 80)
    for feat in profile_features:
        if feat not in df.columns:
            continue
        w_med = winners[feat].median()
        l_med = losers[feat].median()
        a_med = df[feat].median()
        print(f"{feat:<30} {w_med:>15.4f} {l_med:>15.4f} {a_med:>15.4f}")

    # Step 7: Build Composite Score from Top Features
    print(f"\n{'='*70}")
    print("COMPOSITE SCORE: Predictive Power of Top Features Combined")
    print(f"{'='*70}")

    # Select top discriminative features (|d| >= 0.1)
    top_features = [r for r in d_results if abs(r["d"]) >= 0.1]
    print(f"\nUsing {len(top_features)} features with |d| >= 0.1")

    if len(top_features) >= 3:
        # Build score: for each feature, determine if high or low values predict winners
        # based on winner_mean vs loser_mean
        score_features = []
        for r in top_features:
            feat = r["feature"]
            direction = 1 if r["diff"] > 0 else -1  # positive = higher is better for winners
            score_features.append((feat, direction))

        print(f"\nScore components:")
        for feat, direction in score_features:
            d_val = next(r["d"] for r in d_results if r["feature"] == feat)
            arrow = "↑" if direction > 0 else "↓"
            print(f"  {feat}: {arrow} (d={d_val:.4f})")

        # Compute composite score (percentile-based, normalized)
        score_df = df.copy()
        for feat, direction in score_features:
            # Convert to percentile rank
            score_df[f"{feat}_prank"] = score_df[feat].rank(pct=True)
            if direction < 0:
                score_df[f"{feat}_prank"] = 1 - score_df[f"{feat}_prank"]

        prank_cols = [f"{feat}_prank" for feat, _ in score_features]
        score_df["composite_score"] = score_df[prank_cols].mean(axis=1) * 100

        # Analyze composite score deciles
        print(f"\n--- Composite Score Decile Analysis ---")
        score_df["score_decile"] = pd.qcut(score_df["composite_score"], 10, labels=False, duplicates="drop")
        print(f"{'Decile':>8} {'N':>8} {'WR':>8} {'AR':>8} {'EV':>8} {'AvgWin':>8} {'AvgLoss':>8} {'PF':>8}")
        print("-" * 75)

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
            print(f"{d:>8} {n:>8} {wr:>7.1f}% {sub['ret_5d_pct'].mean():>7.2f}% "
                  f"{ev:>7.2f}% {avg_win:>7.2f}% {avg_loss:>7.2f}% {pf:>7.2f}")
            decile_results.append({
                "decile": int(d), "n": n, "wr": round(wr, 1),
                "ar": round(sub["ret_5d_pct"].mean(), 2),
                "ev": round(ev, 2), "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2), "pf": round(pf, 2),
            })

        # Top decile deep dive
        top_decile = score_df[score_df["score_decile"] == score_df["score_decile"].max()]
        print(f"\n--- Top Decile Deep Dive (N={len(top_decile)}) ---")
        td_wr = (top_decile["ret_5d_pct"] > 0).mean() * 100
        td_ar = top_decile["ret_5d_pct"].mean()
        td_w = top_decile[top_decile["ret_5d_pct"] > 0]
        td_l = top_decile[top_decile["ret_5d_pct"] <= 0]
        td_ev = td_wr/100 * td_w["ret_5d_pct"].mean() + (1-td_wr/100) * td_l["ret_5d_pct"].mean()
        print(f"  WR={td_wr:.1f}%, AR={td_ar:.2f}%, EV={td_ev:.2f}%")
        print(f"  Avg Win={td_w['ret_5d_pct'].mean():.2f}%, Avg Loss={td_l['ret_5d_pct'].mean():.2f}%")
        n_winners_top = (top_decile["label"] == "winner").sum()
        print(f"  Winners(>=5%): {n_winners_top} ({n_winners_top/len(top_decile)*100:.1f}%)")

        # Top 5% score
        score_threshold_95 = score_df["composite_score"].quantile(0.95)
        top5 = score_df[score_df["composite_score"] >= score_threshold_95]
        t5_wr = (top5["ret_5d_pct"] > 0).mean() * 100
        t5_ar = top5["ret_5d_pct"].mean()
        t5_w = top5[top5["ret_5d_pct"] > 0]
        t5_l = top5[top5["ret_5d_pct"] <= 0]
        t5_ev = t5_wr/100 * t5_w["ret_5d_pct"].mean() + (1-t5_wr/100) * t5_l["ret_5d_pct"].mean()
        print(f"\n--- Top 5% Score (N={len(top5)}) ---")
        print(f"  WR={t5_wr:.1f}%, AR={t5_ar:.2f}%, EV={t5_ev:.2f}%")

        # Top 1% score
        score_threshold_99 = score_df["composite_score"].quantile(0.99)
        top1 = score_df[score_df["composite_score"] >= score_threshold_99]
        t1_wr = (top1["ret_5d_pct"] > 0).mean() * 100
        t1_ar = top1["ret_5d_pct"].mean()
        t1_w = top1[top1["ret_5d_pct"] > 0]
        t1_l = top1[top1["ret_5d_pct"] <= 0]
        t1_ev = t1_wr/100 * t1_w["ret_5d_pct"].mean() + (1-t1_wr/100) * t1_l["ret_5d_pct"].mean() if len(t1_l) > 0 else t1_w["ret_5d_pct"].mean()
        print(f"\n--- Top 1% Score (N={len(top1)}) ---")
        print(f"  WR={t1_wr:.1f}%, AR={t1_ar:.2f}%, EV={t1_ev:.2f}%")

        # Step 8: Top-score stocks with weekly alignment (user's key signal)
        print(f"\n{'='*70}")
        print("COMPOSITE SCORE + WEEKLY ALIGNMENT (User's Key Signal)")
        print(f"{'='*70}")

        # Weekly aligned + high score
        wa_high = score_df[(score_df["weekly_align"] == True) &
                          (score_df["composite_score"] >= score_df["composite_score"].quantile(0.8))]
        if len(wa_high) > 20:
            wa_wr = (wa_high["ret_5d_pct"] > 0).mean() * 100
            wa_ar = wa_high["ret_5d_pct"].mean()
            wa_w = wa_high[wa_high["ret_5d_pct"] > 0]
            wa_l = wa_high[wa_high["ret_5d_pct"] <= 0]
            wa_ev = wa_wr/100 * wa_w["ret_5d_pct"].mean() + (1-wa_wr/100) * wa_l["ret_5d_pct"].mean()
            print(f"  Weekly Aligned + Top 20% Score: N={len(wa_high)}, WR={wa_wr:.1f}%, AR={wa_ar:.2f}%, EV={wa_ev:.2f}%")

        # Weekly aligned + close near MA20 + high score
        for cp_lo, cp_hi in [(-3, -1.5), (-1.5, -0.5), (-0.5, 0.5), (0.5, 1.5)]:
            sub = score_df[
                (score_df["weekly_align"] == True) &
                (score_df["close_vs_ma20"] >= cp_lo) &
                (score_df["close_vs_ma20"] < cp_hi) &
                (score_df["composite_score"] >= score_df["composite_score"].quantile(0.7))
            ]
            if len(sub) >= 20:
                s_wr = (sub["ret_5d_pct"] > 0).mean() * 100
                s_ar = sub["ret_5d_pct"].mean()
                s_w = sub[sub["ret_5d_pct"] > 0]
                s_l = sub[sub["ret_5d_pct"] <= 0]
                s_ev = s_wr/100 * s_w["ret_5d_pct"].mean() + (1-s_wr/100) * s_l["ret_5d_pct"].mean()
                print(f"  Weekly+close[{cp_lo},{cp_hi})+Top30%Score: N={len(sub)}, WR={s_wr:.1f}%, AR={s_ar:.2f}%, EV={s_ev:.2f}%")

    # Step 9: Feature-by-feature optimal threshold search
    print(f"\n{'='*70}")
    print("OPTIMAL THRESHOLD SEARCH: For Top Discriminative Features")
    print(f"{'='*70}")

    # For top 5 continuous features, search for the threshold that maximizes EV
    for r in d_results[:8]:
        feat = r["feature"]
        if feat not in df.columns:
            continue
        print(f"\n--- {feat} (d={r['d']:.4f}) ---")
        # Try different thresholds
        for pct in [10, 20, 30, 40, 50, 60, 70, 80, 90]:
            if r["diff"] > 0:
                threshold = df[feat].quantile(pct / 100)
                sub = df[df[feat] >= threshold]
                label = f">={threshold:.4f}"
            else:
                threshold = df[feat].quantile(1 - pct / 100)
                sub = df[df[feat] <= threshold]
                label = f"<={threshold:.4f}"
            if len(sub) < 50:
                continue
            wr = (sub["ret_5d_pct"] > 0).mean() * 100
            ar = sub["ret_5d_pct"].mean()
            w_s = sub[sub["ret_5d_pct"] > 0]
            l_s = sub[sub["ret_5d_pct"] <= 0]
            ev = wr/100 * w_s["ret_5d_pct"].mean() + (1-wr/100) * l_s["ret_5d_pct"].mean()
            print(f"  Top {100-pct}% ({label}): N={len(sub)}, WR={wr:.1f}%, AR={ar:.2f}%, EV={ev:.2f}%")

    # Save summary
    summary = {
        "version": "V7",
        "method": "Reverse Engineering: Find Winners → Learn Patterns",
        "universe": "CSI1000 + Daily Amount Top 200",
        "total_trades": total,
        "baseline_wr": round(base_wr, 1),
        "baseline_ar": round(base_ar, 2),
        "baseline_ev": round(base_ev, 2),
        "n_winners": int(n_winner),
        "n_losers": int(n_loser),
        "top_discriminative_features": d_results[:10],
        "binary_feature_lifts": b_results[:10],
        "decile_analysis": decile_results if 'decile_results' in dir() else [],
    }
    with open(OUTPUT_DIR / "v7_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    print(f"\nDone! Summary saved to {OUTPUT_DIR / 'v7_summary.json'}")


if __name__ == "__main__":
    main()
