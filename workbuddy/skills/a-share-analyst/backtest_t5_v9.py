"""
V9 Backtest: 针对5分钟买入区信号的深度打磨

核心假设（来自V8）:
1. bz_direction < -0.3% (尾盘杀跌) → WR=66.3%，但N只有276
2. 结合周线多头 + MA20回踩 → 能进一步提升WR吗？
3. 新增5分钟特征：
   - 尾盘量比：14:30-15:00成交量 vs 全天均值
   - 尾盘5分钟布林位
   - 下午盘强度（14:00-15:00 vs 9:30-13:00）
   - 最后N根5分钟K线的斜率

目标：在N>=200笔的前提下，找到WR>=65%的组合
"""

import time
import warnings
from pathlib import Path
from collections import defaultdict

import numpy as np
import pandas as pd
from pytdx.hq import TdxHq_API

warnings.filterwarnings("ignore")

CONS_FILE = Path.home() / ".workbuddy" / "skills" / "csi1000-skills" / "000852cons.xls"
OUTPUT_DIR = Path.home() / ".workbuddy" / "a-share-analyst"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TDX_HOSTS = [
    ("60.191.117.167", 7709),
    ("39.105.251.234", 7709),
    ("119.147.212.83", 7709),
]
TOP_N_AMOUNT = 200
WINNER_THRESH = 5.0   # T+5 收益 >= 5% 为赢家
LOSER_THRESH = -3.0   # T+5 收益 <= -3% 为输家


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
    """取最近约34个交易日的5分钟K线（两次请求，各800根）"""
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
# 特征计算
# ─────────────────────────────────────────────

def compute_weekly_features(wdf):
    """从周线数据提取特征（一次性，返回最新状态）"""
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

def compute_5min_features_for_day(day_df_5min):
    """
    给定某交易日的5分钟数据，提取买入区信号
    day_df_5min: 只包含该日数据的DataFrame
    """
    if day_df_5min is None or len(day_df_5min) < 5:
        return {}

    d = day_df_5min.copy()
    d["hour"] = d["datetime"].dt.hour
    d["minute"] = d["datetime"].dt.minute

    total_amt = d["amount"].sum()
    total_vol = d["vol"].sum()

    # 上午盘：9:30-11:30
    am = d[(d["hour"] == 9) | ((d["hour"] == 10)) | ((d["hour"] == 11) & (d["minute"] <= 30))]
    # 下午盘：13:00-15:00
    pm = d[(d["hour"] == 13) | (d["hour"] == 14)]
    # 买入区：14:30-15:00
    bz = d[(d["hour"] == 14) & (d["minute"] >= 30)]
    # 临收前最后3根：14:45-15:00
    last3 = d.tail(3)

    feats = {}

    # 尾盘方向（核心信号）
    if len(bz) >= 3:
        bz_open = bz.iloc[0]["open"]
        bz_close = bz.iloc[-1]["close"]
        feats["bz_direction"] = (bz_close - bz_open) / bz_open * 100 if bz_open > 0 else 0.0

    # 尾盘量比（买入区成交量 / 全天平均）
    if len(bz) > 0 and total_vol > 0:
        bz_vol = bz["vol"].sum()
        avg_per_bar = total_vol / len(d)
        feats["bz_vol_ratio"] = (bz_vol / len(bz)) / avg_per_bar if avg_per_bar > 0 else 1.0

    # 下午/上午量比
    if len(am) > 0 and len(pm) > 0:
        am_avg = am["amount"].mean()
        pm_avg = pm["amount"].mean()
        feats["pm_am_ratio"] = pm_avg / am_avg if am_avg > 0 else 1.0

    # 最后3根5分钟K线方向（斜率）
    if len(last3) >= 2:
        first_c = last3.iloc[0]["close"]
        last_c = last3.iloc[-1]["close"]
        feats["last3_slope"] = (last_c - first_c) / first_c * 100 if first_c > 0 else 0.0

    # 全天布林位（最后一根5分钟K）
    if len(d) >= 10:
        close_series = d["close"]
        bb_mid = close_series.rolling(10).mean().iloc[-1]
        bb_std = close_series.rolling(10).std().iloc[-1]
        if pd.notna(bb_mid) and pd.notna(bb_std) and bb_std > 0:
            bb_upper = bb_mid + 2 * bb_std
            bb_lower = bb_mid - 2 * bb_std
            feats["bb5_pct_end"] = (d.iloc[-1]["close"] - bb_lower) / (bb_upper - bb_lower) \
                if (bb_upper - bb_lower) > 0 else 0.5

    # 尾盘是否缩量（bz量比 < 0.8 为缩量）
    if "bz_vol_ratio" in feats:
        feats["bz_shrink"] = float(feats["bz_vol_ratio"] < 0.8)

    return feats


def process_stock(api, code, name, market):
    """
    拉取一只股票的日线+周线+5分钟，
    构建每日特征+T+5标签的记录列表
    """
    daily = fetch_daily_bars(api, market, code, count=800)
    if daily is None or len(daily) < 60:
        return []

    weekly = fetch_weekly_bars(api, market, code, count=100)
    min5 = fetch_5min_bars(api, market, code)

    # 周线特征（只取最新，近似用于整个回测期）
    wfeats = compute_weekly_features(weekly)

    # 日线特征
    d = daily.copy()
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

    # T+5收益
    d["ret_5d"] = (d["close"].shift(-5) / d["close"] - 1) * 100

    # 5分钟数据：按日期建索引
    min5_by_date = {}
    if min5 is not None and len(min5) > 0:
        min5["date"] = min5["datetime"].dt.date
        for dt, grp in min5.groupby("date"):
            min5_by_date[dt] = grp.copy()

    records = []
    for i in range(30, len(d) - 6):
        row = d.iloc[i]
        if pd.isna(row["ret_5d"]):
            continue
        # 基础过滤：MA20要有值
        if pd.isna(row["ma20"]) or pd.isna(row["avg_amt_5d"]):
            continue

        rec = {
            "code": code,
            "name": name,
            "date": str(row["datetime"])[:10],
            "close": row["close"],
            "ret_5d": row["ret_5d"],
            "label": "winner" if row["ret_5d"] >= WINNER_THRESH else
                     ("loser" if row["ret_5d"] <= LOSER_THRESH else "neutral"),
            # 日线特征
            "close_vs_ma20": row["close_vs_ma20"] if pd.notna(row["close_vs_ma20"]) else 0.0,
            "low_vs_ma20": row["low_vs_ma20"] if pd.notna(row["low_vs_ma20"]) else 0.0,
            "amt_ratio": row["amt_ratio"] if pd.notna(row["amt_ratio"]) else 1.0,
            "daily_bullish": bool(row["daily_bullish"]),
            "bb_pct": row["bb_pct"] if pd.notna(row["bb_pct"]) else 0.5,
            # 周线特征（全期使用最新状态近似）
            "weekly_align": wfeats.get("weekly_align", False),
            "weekly_slope": wfeats.get("weekly_slope", 0.0),
        }

        # 5分钟特征
        trade_date = row["datetime"].date()
        if trade_date in min5_by_date:
            m5feats = compute_5min_features_for_day(min5_by_date[trade_date])
            rec.update(m5feats)
        else:
            rec["bz_direction"] = np.nan
            rec["bz_vol_ratio"] = np.nan
            rec["pm_am_ratio"] = np.nan
            rec["last3_slope"] = np.nan
            rec["bb5_pct_end"] = np.nan
            rec["bz_shrink"] = np.nan

        records.append(rec)
    return records


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def main():
    print("=" * 60)
    print("V9 Backtest: 5分钟信号深度打磨")
    print("=" * 60)

    stocks = get_stock_list()
    api = connect_tdx()
    print(f"中证1000成分股: {len(stocks)} 只，pytdx已连接\n")

    # Step 1: 获取成交额，筛Top 200
    print("Step 1/3: 筛选成交额Top 200...")
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
    print(f"  → Top {TOP_N_AMOUNT} 只\n")

    # Step 2: 逐一处理
    print("Step 2/3: 提取日线+5分钟特征...")
    all_records = []
    for i, (_, row) in enumerate(amt_df.iterrows()):
        recs = process_stock(api, row["code"], row["name"], row["market"])
        all_records.extend(recs)
        if (i + 1) % 25 == 0:
            print(f"  已处理 {i+1}/{len(amt_df)} 只，当前记录数 {len(all_records)}")

    api.disconnect()
    print(f"  → 总记录: {len(all_records)}\n")

    df = pd.DataFrame(all_records)
    winners = df[df["label"] == "winner"]
    losers = df[df["label"] == "loser"]
    print(f"赢家(≥{WINNER_THRESH}%): {len(winners)},  输家(≤{LOSER_THRESH}%): {len(losers)},  中性: {len(df)-len(winners)-len(losers)}")

    # Step 3: 核心分析——5分钟特征 Cohen's d
    print("\n=== 5分钟特征 Cohen's d ===")
    min5_feats = ["bz_direction", "bz_vol_ratio", "pm_am_ratio", "last3_slope", "bb5_pct_end"]
    valid_mask = df["bz_direction"].notna()
    df5 = df[valid_mask]
    w5 = df5[df5["label"] == "winner"]
    l5 = df5[df5["label"] == "loser"]
    print(f"有5分钟数据的记录: {len(df5)}  赢家: {len(w5)}  输家: {len(l5)}")
    for f in min5_feats:
        if f in df5.columns:
            d_val = cohens_d(w5[f].dropna().values, l5[f].dropna().values)
            w_mean = w5[f].mean()
            l_mean = l5[f].mean()
            print(f"  {f:<20s}: d={d_val:+.3f}  winner={w_mean:.3f}  loser={l_mean:.3f}")

    # 核心组合回测：bz_direction 分区段
    print("\n=== 尾盘方向(bz_direction)分档 ===")
    df5c = df5.copy()
    bins = [(-99, -1.0), (-1.0, -0.5), (-0.5, -0.3), (-0.3, 0), (0, 0.5), (0.5, 99)]
    for lo, hi in bins:
        seg = df5c[(df5c["bz_direction"] >= lo) & (df5c["bz_direction"] < hi)]
        if len(seg) < 10:
            continue
        wr = (seg["ret_5d"] > 0).mean() * 100
        avg_ret = seg["ret_5d"].mean()
        win_avg = seg[seg["ret_5d"] > 0]["ret_5d"].mean() if (seg["ret_5d"] > 0).any() else 0
        loss_avg = seg[seg["ret_5d"] <= 0]["ret_5d"].mean() if (seg["ret_5d"] <= 0).any() else 0
        ev = wr/100 * win_avg + (1-wr/100) * loss_avg
        print(f"  bz=[{lo:+.1f},{hi:+.1f}): N={len(seg):4d} WR={wr:.1f}% AR={avg_ret:+.2f}% EV={ev:+.2f}%")

    # 关键组合：周线多头 + bz 方向
    print("\n=== 核心组合：周线多头 × 尾盘方向 ===")
    for bz_cond, bz_label in [
        (df5["bz_direction"] < -0.3, "尾盘杀跌<-0.3%"),
        ((df5["bz_direction"] >= -0.3) & (df5["bz_direction"] < 0), "尾盘微跌"),
        (df5["bz_direction"] >= 0, "尾盘拉升"),
    ]:
        for wa_cond, wa_label in [
            (df5["weekly_align"] == True, "周线多头"),
            (df5["weekly_align"] == False, "非多头"),
        ]:
            seg = df5[bz_cond & wa_cond]
            if len(seg) < 10:
                continue
            wr = (seg["ret_5d"] > 0).mean() * 100
            avg_ret = seg["ret_5d"].mean()
            win_avg = seg[seg["ret_5d"] > 0]["ret_5d"].mean() if (seg["ret_5d"] > 0).any() else 0
            loss_avg = seg[seg["ret_5d"] <= 0]["ret_5d"].mean() if (seg["ret_5d"] <= 0).any() else 0
            ev = wr/100 * win_avg + (1-wr/100) * loss_avg
            print(f"  {bz_label} + {wa_label}: N={len(seg):4d} WR={wr:.1f}% AR={avg_ret:+.2f}% EV={ev:+.2f}%")

    # 三条件组合：周线多头 + 尾盘杀跌 + MA20回踩
    print("\n=== 三条件组合：周线多头 + 尾盘杀跌 + MA20区间 ===")
    df5["ma20_bin"] = pd.cut(df5["close_vs_ma20"], bins=[-99,-4,-2,0,2,5,99],
                              labels=["<-4%","-4~-2%","-2~0%","0~2%","2~5%",">5%"])
    mask_bz = df5["bz_direction"] < -0.3
    mask_wa = df5["weekly_align"] == True
    for ma_bin in ["-4~-2%", "-2~0%", "0~2%", "2~5%"]:
        mask_ma = df5["ma20_bin"] == ma_bin
        seg = df5[mask_bz & mask_wa & mask_ma]
        if len(seg) < 5:
            continue
        wr = (seg["ret_5d"] > 0).mean() * 100
        avg_ret = seg["ret_5d"].mean()
        win_avg = seg[seg["ret_5d"] > 0]["ret_5d"].mean() if (seg["ret_5d"] > 0).any() else 0
        loss_avg = seg[seg["ret_5d"] <= 0]["ret_5d"].mean() if (seg["ret_5d"] <= 0).any() else 0
        ev = wr/100 * win_avg + (1-wr/100) * loss_avg
        print(f"  周线多头+尾盘杀跌+MA20({ma_bin}): N={len(seg):3d} WR={wr:.1f}% AR={avg_ret:+.2f}% EV={ev:+.2f}%")

    # 综合最优组合搜索
    print("\n=== 最优组合搜索（N>=30）===")
    combos = []
    df5["bz_kill"] = df5["bz_direction"] < -0.3
    df5["bz_mild"] = (df5["bz_direction"] >= -0.3) & (df5["bz_direction"] < 0)
    df5["ma20_pull"] = (df5["close_vs_ma20"] >= -5) & (df5["close_vs_ma20"] <= 2)
    df5["vol_expand"] = (df5["amt_ratio"] >= 1.2) & (df5["amt_ratio"] <= 2.5)
    df5["bz_shrink_bool"] = df5["bz_vol_ratio"] < 0.8

    # 枚举部分关键组合
    conditions_list = [
        ("bz_kill", df5["bz_kill"]),
        ("bz_kill+weekly", df5["bz_kill"] & df5["weekly_align"]),
        ("bz_kill+weekly+ma20pull", df5["bz_kill"] & df5["weekly_align"] & df5["ma20_pull"]),
        ("bz_kill+weekly+vol_expand", df5["bz_kill"] & df5["weekly_align"] & df5["vol_expand"]),
        ("bz_kill+weekly+ma20pull+vol_expand", df5["bz_kill"] & df5["weekly_align"] & df5["ma20_pull"] & df5["vol_expand"]),
        ("bz_mild+weekly+ma20pull", df5["bz_mild"] & df5["weekly_align"] & df5["ma20_pull"]),
        ("bz_kill+bz_shrink+weekly", df5["bz_kill"] & df5["bz_shrink_bool"] & df5["weekly_align"]),
        ("weekly+ma20pull", df5["weekly_align"] & df5["ma20_pull"]),
        ("weekly+ma20pull+vol_expand", df5["weekly_align"] & df5["ma20_pull"] & df5["vol_expand"]),
    ]
    for label, mask in conditions_list:
        seg = df5[mask]
        if len(seg) < 5:
            continue
        wr = (seg["ret_5d"] > 0).mean() * 100
        avg_ret = seg["ret_5d"].mean()
        win_avg = seg[seg["ret_5d"] > 0]["ret_5d"].mean() if (seg["ret_5d"] > 0).any() else 0
        loss_avg = seg[seg["ret_5d"] <= 0]["ret_5d"].mean() if (seg["ret_5d"] <= 0).any() else 0
        ev = wr/100 * win_avg + (1-wr/100) * loss_avg
        combos.append({"label": label, "N": len(seg), "WR": wr, "AR": avg_ret, "EV": ev})

    combos_df = pd.DataFrame(combos).sort_values("EV", ascending=False)
    print(combos_df.to_string(index=False))

    # 保存结果
    summary = {
        "total_records": len(df),
        "records_with_5min": len(df5),
        "winner_count": len(winners),
        "loser_count": len(losers),
        "combos": combos_df.to_dict("records"),
    }
    import json
    with open(OUTPUT_DIR / "v9_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {OUTPUT_DIR / 'v9_summary.json'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
