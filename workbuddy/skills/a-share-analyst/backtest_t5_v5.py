"""
V5 Backtest: CSI 1000 constituents + daily amount top 200
Strategy: Weekly MA alignment + Daily MA20 pullback
Focus: Win rate AND expected return (EV)
"""
import json
import time
import warnings
from datetime import datetime
from pathlib import Path
from itertools import product

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
    """Read CSI 1000 constituents from XLS"""
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
    """Fetch daily K-line bars, oldest first"""
    try:
        bars = api.get_security_bars(9, market, code, 0, count)
        if not bars:
            return None
        df = api.to_df(bars)
        if df is None or df.empty:
            return None
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.sort_values("datetime").reset_index(drop=True)
        # Normalize column names
        df = df.rename(columns={
            "open": "open", "high": "high", "low": "low", "close": "close",
            "vol": "vol", "amount": "amt"
        })
        return df
    except Exception:
        return None


def fetch_weekly_bars(api, market, code, count=200):
    """Fetch weekly K-line bars"""
    try:
        bars = api.get_security_bars(5, market, code, 0, count)  # 5 = weekly
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


def compute_weekly_ma_alignment(wdf):
    """Check if weekly MA5 > MA10 > MA20 (bullish alignment)"""
    if wdf is None or len(wdf) < 25:
        return None
    wdf = wdf.copy()
    wdf["wma5"] = wdf["close"].rolling(5).mean()
    wdf["wma10"] = wdf["close"].rolling(10).mean()
    wdf["wma20"] = wdf["close"].rolling(20).mean()
    # Return the latest values
    last = wdf.iloc[-1]
    if pd.isna(last["wma5"]) or pd.isna(last["wma10"]) or pd.isna(last["wma20"]):
        return None
    return {
        "wma5": last["wma5"],
        "wma10": last["wma10"],
        "wma20": last["wma20"],
        "weekly_align": last["wma5"] > last["wma10"] > last["wma20"],
        "weekly_slope": (last["wma5"] - last["wma20"]) / last["wma20"] * 100,  # steepness
    }


def compute_signals(df, wdata):
    """Compute all signals for each day in the daily DataFrame"""
    if df is None or len(df) < 30:
        return None

    df = df.copy()
    # Daily MAs
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()

    # MA20 slope (5-day lookback)
    df["ma20_slope"] = (df["ma20"] - df["ma20"].shift(5)) / df["ma20"].shift(5) * 100

    # Volume ratio (today / 5-day avg)
    df["avg_amt_5d"] = df["amt"].rolling(5).mean()
    df["amt_ratio"] = df["amt"] / df["avg_amt_5d"]

    # Daily bullish: MA5 > MA10 > MA20
    df["daily_bullish"] = (df["ma5"] > df["ma10"]) & (df["ma10"] > df["ma20"])

    # Close relative to MA20
    df["close_pct"] = (df["close"] - df["ma20"]) / df["ma20"] * 100

    # Low relative to MA20
    df["low_pct"] = (df["low"] - df["ma20"]) / df["ma20"] * 100

    # MA20 rising?
    df["ma20_rising"] = df["ma20_slope"] > 0

    # Volume shrink (ratio < 0.8)
    df["vol_shrink"] = df["amt_ratio"] < 0.8

    # T+5 return (forward-looking)
    df["ret_5d"] = df["close"].shift(-5) / df["close"] - 1
    df["ret_5d_pct"] = df["ret_5d"] * 100

    # Weekly alignment (broadcast from weekly data)
    if wdata and wdata.get("weekly_align") is not None:
        df["weekly_align"] = wdata["weekly_align"]
        df["weekly_slope"] = wdata["weekly_slope"]
    else:
        df["weekly_align"] = False
        df["weekly_slope"] = 0.0

    return df


def main():
    print("=" * 70)
    print("V5 Backtest: CSI 1000 + Amount Top 200 + Capital Strength Signals")
    print("=" * 70)

    # Step 1: Get CSI 1000 stock list
    stock_list = get_stock_list()
    print(f"CSI 1000 constituents: {len(stock_list)} stocks")

    # Step 2: Fetch daily bars for all stocks, then filter by daily amount
    all_trades = []
    api = connect_tdx()
    success = 0
    failed = 0

    print(f"\nFetching daily bars for {len(stock_list)} stocks...")
    for idx, row in stock_list.iterrows():
        market = int(row["market"])
        code = str(row["code"])
        name = str(row["name"])

        # Reconnect if needed
        if idx > 0 and idx % 80 == 0:
            try:
                api.disconnect()
            except Exception:
                pass
            time.sleep(0.1)
            api = connect_tdx()

        # Fetch daily bars
        ddf = fetch_daily_bars(api, market, code, 800)
        if ddf is None or len(ddf) < 60:
            failed += 1
            continue

        # Fetch weekly bars
        wdf = fetch_weekly_bars(api, market, code, 200)
        wdata = compute_weekly_ma_alignment(wdf)

        # Compute signals
        ddf = compute_signals(ddf, wdata)
        if ddf is None:
            failed += 1
            continue

        # For each day, check if this stock qualifies
        # Entry condition: low touches MA20 (low_pct between -3% and +1%)
        valid = ddf[
            (ddf["low_pct"] >= -3) &
            (ddf["low_pct"] <= 1) &
            (ddf["ma20"].notna()) &
            (ddf["ret_5d_pct"].notna())
        ].copy()

        if len(valid) == 0:
            success += 1
            continue

        valid["code"] = code
        valid["name"] = name
        valid["market"] = market

        all_trades.append(valid)
        success += 1

        if (idx + 1) % 100 == 0:
            print(f"  Progress: {idx + 1}/{len(stock_list)} (ok={success}, fail={failed})")

    try:
        api.disconnect()
    except Exception:
        pass

    print(f"\nFetched: {success} ok, {failed} failed")

    if not all_trades:
        print("No trades found!")
        return

    trades = pd.concat(all_trades, ignore_index=True)
    print(f"Total raw trade candidates: {len(trades)}")

    # Step 3: For each day, rank stocks by daily amount, keep top 200
    trades["date_str"] = trades["datetime"].dt.strftime("%Y%m%d")
    # Group by date, rank by amount, keep top 200
    trades["amt_rank"] = trades.groupby("date_str")["amt"].rank(ascending=False, method="first")
    trades_top200 = trades[trades["amt_rank"] <= 200].copy()
    print(f"After amount top-200 filter: {len(trades_top200)} trades")

    # Save full trades for analysis
    trades_top200.to_csv(OUTPUT_DIR / "v5_trades_full.csv", index=False, encoding="utf-8-sig")
    print(f"Saved to {OUTPUT_DIR / 'v5_trades_full.csv'}")

    # Step 4: Analyze by signal combinations
    df = trades_top200.copy()
    total = len(df)
    base_wr = (df["ret_5d_pct"] > 0).mean() * 100
    base_ar = df["ret_5d_pct"].mean()
    wins = df[df["ret_5d_pct"] > 0]
    losses = df[df["ret_5d_pct"] <= 0]
    base_ev = base_wr / 100 * wins["ret_5d_pct"].mean() + (1 - base_wr / 100) * losses["ret_5d_pct"].mean()
    print(f"\n=== Baseline (CSI 1000 + Amount Top 200) ===")
    print(f"Total: {total}, Win Rate: {base_wr:.1f}%, Avg Return: {base_ar:.2f}%, EV: {base_ev:.2f}%")

    # Signal analysis
    print("\n=== Individual Signal Effects ===")
    signals = [
        ("weekly_align", True, "周线多头"),
        ("weekly_align", False, "非周线多头"),
        ("ma20_rising", True, "MA20上升"),
        ("ma20_rising", False, "MA20下降"),
        ("vol_shrink", True, "缩量"),
        ("vol_shrink", False, "放量"),
        ("daily_bullish", True, "日线多头"),
        ("daily_bullish", False, "日线非多头"),
    ]
    for col, val, label in signals:
        sub = df[df[col] == val]
        if len(sub) > 10:
            wr = (sub["ret_5d_pct"] > 0).mean() * 100
            w = sub[sub["ret_5d_pct"] > 0]
            l = sub[sub["ret_5d_pct"] <= 0]
            ev = wr / 100 * w["ret_5d_pct"].mean() + (1 - wr / 100) * l["ret_5d_pct"].mean()
            print(f"  {label}: N={len(sub)}, WR={wr:.1f}%, AR={sub['ret_5d_pct'].mean():.2f}%, EV={ev:.2f}%")

    # close_pct vs win rate (for weekly aligned stocks)
    print("\n=== close_pct Grid (Weekly Aligned) ===")
    wa = df[df["weekly_align"] == True]
    for lo, hi in [(-3, -2.5), (-2.5, -2), (-2, -1.5), (-1.5, -1), (-1, -0.5), (-0.5, 0), (0, 0.5), (0.5, 1)]:
        sub = wa[(wa["close_pct"] >= lo) & (wa["close_pct"] < hi)]
        if len(sub) > 5:
            wr = (sub["ret_5d_pct"] > 0).mean() * 100
            w = sub[sub["ret_5d_pct"] > 0]
            l = sub[sub["ret_5d_pct"] <= 0]
            ev = wr / 100 * w["ret_5d_pct"].mean() + (1 - wr / 100) * l["ret_5d_pct"].mean()
            print(f"  close_pct [{lo},{hi}): N={len(sub)}, WR={wr:.1f}%, AR={sub['ret_5d_pct'].mean():.2f}%, EV={ev:.2f}%")

    # Three-condition combos with weekly alignment
    print("\n=== Three-Condition Combos (Weekly Aligned + close<0) ===")
    core = wa[wa["close_pct"] < 0]
    print(f"Core (周线多头+close<MA20): N={len(core)}, WR={(core['ret_5d_pct']>0).mean()*100:.1f}%, AR={core['ret_5d_pct'].mean():.2f}%")

    results = []
    for vs, mr, db in product([True, False], repeat=3):
        mask = (core["vol_shrink"] == vs) & (core["ma20_rising"] == mr) & (core["daily_bullish"] == db)
        sub = core[mask]
        if len(sub) >= 20:
            wr = (sub["ret_5d_pct"] > 0).mean() * 100
            w = sub[sub["ret_5d_pct"] > 0]
            l = sub[sub["ret_5d_pct"] <= 0]
            ev = wr / 100 * w["ret_5d_pct"].mean() + (1 - wr / 100) * l["ret_5d_pct"].mean()
            pf = abs(w["ret_5d_pct"].mean() / l["ret_5d_pct"].mean()) if len(l) > 0 and l["ret_5d_pct"].mean() != 0 else float("inf")
            results.append({
                "cond": f"缩量={vs},MA20升={mr},日线多头={db}",
                "n": len(sub), "wr": wr, "ar": sub["ret_5d_pct"].mean(), "ev": ev,
                "avg_win": w["ret_5d_pct"].mean() if len(w) > 0 else 0,
                "avg_loss": l["ret_5d_pct"].mean() if len(l) > 0 else 0,
                "pf": pf,
            })

    results.sort(key=lambda x: x["ev"], reverse=True)
    for r in results:
        print(f"  {r['cond']}: N={r['n']}, WR={r['wr']:.1f}%, AR={r['ar']:.2f}%, "
              f"均赢={r['avg_win']:.2f}%, 均亏={r['avg_loss']:.2f}%, "
              f"盈亏比={r['pf']:.2f}, EV={r['ev']:.2f}%")

    # Weekly slope analysis (steepness of weekly MA alignment)
    print("\n=== Weekly Slope Analysis ===")
    for lo, hi in [(0, 1), (1, 2), (2, 3), (3, 5), (5, 10)]:
        sub = wa[(wa["weekly_slope"] >= lo) & (wa["weekly_slope"] < hi)]
        if len(sub) > 10:
            wr = (sub["ret_5d_pct"] > 0).mean() * 100
            w = sub[sub["ret_5d_pct"] > 0]
            l = sub[sub["ret_5d_pct"] <= 0]
            ev = wr / 100 * w["ret_5d_pct"].mean() + (1 - wr / 100) * l["ret_5d_pct"].mean()
            print(f"  slope [{lo},{hi})%: N={len(sub)}, WR={wr:.1f}%, AR={sub['ret_5d_pct'].mean():.2f}%, EV={ev:.2f}%")

    # Best combo: Weekly aligned + close_pct range + slope
    print("\n=== Best Combo Search (N>=30) ===")
    best = []
    for cp_lo, cp_hi in [(-3, -2), (-2, -1.5), (-1.5, -1), (-1, -0.5)]:
        for sl_lo, sl_hi in [(0, 2), (2, 5), (5, 10)]:
            for vs in [True, False]:
                sub = df[
                    (df["weekly_align"] == True) &
                    (df["close_pct"] >= cp_lo) & (df["close_pct"] < cp_hi) &
                    (df["weekly_slope"] >= sl_lo) & (df["weekly_slope"] < sl_hi) &
                    (df["vol_shrink"] == vs)
                ]
                if len(sub) >= 30:
                    wr = (sub["ret_5d_pct"] > 0).mean() * 100
                    w = sub[sub["ret_5d_pct"] > 0]
                    l = sub[sub["ret_5d_pct"] <= 0]
                    ev = wr / 100 * w["ret_5d_pct"].mean() + (1 - wr / 100) * l["ret_5d_pct"].mean()
                    best.append({
                        "cond": f"close=[{cp_lo},{cp_hi}),slope=[{sl_lo},{sl_hi}),缩量={vs}",
                        "n": len(sub), "wr": wr, "ar": sub["ret_5d_pct"].mean(), "ev": ev,
                    })

    best.sort(key=lambda x: x["ev"], reverse=True)
    for b in best[:10]:
        print(f"  {b['cond']}: N={b['n']}, WR={b['wr']:.1f}%, AR={b['ar']:.2f}%, EV={b['ev']:.2f}%")

    # Compare V4 vs V5
    print("\n" + "=" * 70)
    print("V4 vs V5 Comparison")
    print("=" * 70)
    print(f"V4 (随机500只): 周线多头+close<-2%MA20 → WR=60.3%, AR=1.53%, 984笔")
    print(f"V5 (中证1000+成交额前200): 基线 → WR={base_wr:.1f}%, AR={base_ar:.2f}%, {total}笔")

    # Save summary
    summary = {
        "version": "V5",
        "universe": "CSI1000 + Daily Amount Top 200",
        "total_trades": total,
        "baseline_wr": round(base_wr, 1),
        "baseline_ar": round(base_ar, 2),
        "baseline_ev": round(base_ev, 2),
        "best_combos": best[:5] if best else [],
    }
    with open(OUTPUT_DIR / "v5_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\nDone! Summary saved to {OUTPUT_DIR / 'v5_summary.json'}")


if __name__ == "__main__":
    main()
