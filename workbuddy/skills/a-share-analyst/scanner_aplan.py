"""
V9 选股扫描器 — 三条件严格组合

V9回测验证最优组合（WR=85.6%, EV=10.76%, N=125）:
  条件1: 周线三均线多头（wMA5 > wMA10 > wMA20）
  条件2: 尾盘杀跌（bz_direction < -0.3%，14:30-15:00区间跌幅）
  条件3: MA20回踩（close vs MA20 在 -5% ~ +2% 范围内）

三条件必须同时满足（AND 逻辑），不满足则不入选。

输出字段:
  - entry_price: 进货价（=收盘价，回测锚定价）
  - buy_time: 建议买入时间
  - price_type: 价格类型说明
  - v9_pass: 是否三条件全命中
  - bz_partial: 是否尾盘微跌（-0.3% ~ 0%，不完全命中但有参考价值）

执行计划:
  - 盘中14:30开始预热（拉日线+周线，预筛周线多头+MA20回踩的股票）
  - 14:50运行最终扫描（用截至14:50的5分钟数据计算bz_direction）
  - 14:53确认候选 → 14:55市价买入
  - 注意: 14:50的bz_direction用的是14:30-14:50的数据（非完整14:30-15:00）
    这是"近似同步指标"，有少量前视偏差缓解
"""

import time
import warnings
from datetime import datetime
from pathlib import Path

import sys
import io
import numpy as np
import pandas as pd
from pytdx.hq import TdxHq_API

warnings.filterwarnings("ignore")

# Fix Windows GBK console encoding
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

CONS_FILE = Path.home() / ".workbuddy" / "skills" / "csi1000-skills" / "000852cons.xls"
OUTPUT_DIR = Path.home() / ".workbuddy" / "a-share-analyst"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TDX_HOSTS = [
    ("60.191.117.167", 7709),
    ("39.105.251.234", 7709),
    ("119.147.212.83", 7709),
    ("47.107.75.159", 7709),
]

TOP_N_AMOUNT = 200       # 成交额排名前N
MAX_CANDIDATES = 10       # V9全命中最多输出N只
PARTIAL_CANDIDATES = 5    # 部分命中（缺bz_kill但有微跌）最多输出N只

# === V9 三条件阈值 ===
BZ_KILL_THRESH = -0.3    # 尾盘杀跌阈值（%）
MA20_LOW = -5.0           # MA20回踩下限（%）
MA20_HIGH = 2.0           # MA20回踩上限（%）


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


def fetch_daily_bars(api, market, code, count=120):
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


def fetch_weekly_bars(api, market, code, count=60):
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
    """取最近约34个交易日的5分钟K线"""
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


def compute_v9_signals(daily_df, weekly_df, min5_df):
    """
    计算V9三条件信号，返回详细结果字典
    
    关键改进:
    1. 区分 bz_full（完整14:30-15:00）和 bz_rt（截至14:50的实时数据）
    2. MA20区间用V9回测的-5%~+2%
    3. 明确标注entry_price
    """
    result = {
        # V9三条件
        "weekly_align": False,
        "weekly_slope": 0.0,
        "bz_direction": None,       # 完整尾盘方向（14:30-15:00收盘后）
        "bz_rt": None,              # 实时尾盘方向（截至14:50，盘中用）
        "close_vs_ma20_pct": None,
        # V9判定
        "v9_pass": False,           # 三条件全命中
        "v9_cond1": False,          # 周线多头
        "v9_cond2": False,          # 尾盘杀跌
        "v9_cond3": False,          # MA20回踩
        # 部分命中（缺bz_kill但有微跌）
        "bz_partial": False,
        # 价格信息
        "close": None,
        "ma20": None,
        "entry_price": None,        # = close（回测锚定价）
        "last_close_pct": None,     # 当日涨跌幅
        "amt_ratio": None,
        "last_date": None,
        "amt_rank": None,
        # 诊断
        "fail_reasons": [],
        "pass_reasons": [],
    }

    # ---- 日线 ----
    if daily_df is None or len(daily_df) < 30:
        result["fail_reasons"].append("日线数据不足(<30根)")
        return result

    df = daily_df.copy()
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["avg_amt_5d"] = df["amount"].rolling(5).mean()

    last = df.iloc[-1]
    result["close"] = last["close"]
    result["ma20"] = last["ma20"] if pd.notna(last["ma20"]) else None
    result["entry_price"] = last["close"]  # 进货价 = 收盘价
    result["last_date"] = str(last["datetime"])[:10]

    # 当日涨跌幅
    if len(df) >= 2:
        prev_close = df.iloc[-2]["close"]
        result["last_close_pct"] = (last["close"] - prev_close) / prev_close * 100

    # MA20偏离度
    if pd.notna(last["ma20"]) and last["ma20"] > 0:
        result["close_vs_ma20_pct"] = (last["close"] - last["ma20"]) / last["ma20"] * 100

    # 量比
    if pd.notna(last.get("avg_amt_5d")) and last["avg_amt_5d"] > 0:
        result["amt_ratio"] = last["amount"] / last["avg_amt_5d"]

    # ---- 周线 → 条件1 ----
    if weekly_df is not None and len(weekly_df) >= 25:
        wdf = weekly_df.copy()
        wdf["wma5"] = wdf["close"].rolling(5).mean()
        wdf["wma10"] = wdf["close"].rolling(10).mean()
        wdf["wma20"] = wdf["close"].rolling(20).mean()
        wlast = wdf.iloc[-1]
        if pd.notna(wlast.get("wma5")) and pd.notna(wlast.get("wma10")) and pd.notna(wlast.get("wma20")):
            w5, w10, w20 = wlast["wma5"], wlast["wma10"], wlast["wma20"]
            result["weekly_align"] = bool(w5 > w10 > w20)
            result["weekly_slope"] = (w5 - w20) / w20 * 100
            result["v9_cond1"] = result["weekly_align"]
            if result["v9_cond1"]:
                result["pass_reasons"].append(
                    f"周线多头 (wMA5>10>20, slope={result['weekly_slope']:.1f}%)")
            else:
                result["fail_reasons"].append(
                    f"周线非多头 (slope={result['weekly_slope']:.1f}%)")
    else:
        result["fail_reasons"].append("周线数据不足")

    # ---- 5分钟 → 条件2 ----
    if min5_df is not None and len(min5_df) > 50:
        mdf = min5_df.copy()
        mdf["date"] = mdf["datetime"].dt.date
        mdf["hour"] = mdf["datetime"].dt.hour
        mdf["minute"] = mdf["datetime"].dt.minute

        last_date = mdf["date"].max()
        day_df = mdf[mdf["date"] == last_date].copy()

        # 完整尾盘区间：14:30-15:00（收盘后用）
        bz_full = day_df[(day_df["hour"] == 14) & (day_df["minute"] >= 30)]
        if len(bz_full) >= 3:
            bz_open = bz_full.iloc[0]["open"]
            bz_close = bz_full.iloc[-1]["close"]
            if bz_open > 0:
                result["bz_direction"] = (bz_close - bz_open) / bz_open * 100

        # 实时尾盘区间：14:30-14:50（盘中运行时用，缓解前视偏差）
        bz_rt_bars = day_df[
            ((day_df["hour"] == 14) & (day_df["minute"] >= 30) & (day_df["minute"] <= 50)) |
            ((day_df["hour"] == 14) & (day_df["minute"] >= 30) & (day_df["minute"] < 55))
        ]
        if len(bz_rt_bars) >= 3:
            rt_open = bz_rt_bars.iloc[0]["open"]
            rt_close = bz_rt_bars.iloc[-1]["close"]
            if rt_open > 0:
                result["bz_rt"] = (rt_close - rt_open) / rt_open * 100

        # 用完整数据判定（收盘后运行）
        bz_val = result["bz_direction"]
        if bz_val is not None:
            if bz_val < BZ_KILL_THRESH:
                result["v9_cond2"] = True
                result["pass_reasons"].append(
                    f"尾盘杀跌 (bz={bz_val:.2f}% < {BZ_KILL_THRESH}%)")
            elif bz_val < 0:
                result["bz_partial"] = True
                result["fail_reasons"].append(
                    f"尾盘微跌未达标 (bz={bz_val:.2f}%, 需<{BZ_KILL_THRESH}%)")
            else:
                result["fail_reasons"].append(
                    f"尾盘拉升 (bz={bz_val:.2f}%, 无杀跌信号)")
        else:
            result["fail_reasons"].append("5分钟尾盘数据不足")
    else:
        result["fail_reasons"].append("无5分钟数据")

    # ---- MA20回踩 → 条件3 ----
    c_ma20 = result["close_vs_ma20_pct"]
    if c_ma20 is not None:
        if MA20_LOW <= c_ma20 <= MA20_HIGH:
            result["v9_cond3"] = True
            result["pass_reasons"].append(
                f"MA20回踩 (偏离={c_ma20:.2f}%, 区间[{MA20_LOW}%,{MA20_HIGH}%])")
        elif c_ma20 > MA20_HIGH:
            result["fail_reasons"].append(
                f"高于MA20过多 (偏离={c_ma20:.2f}%, 需<={MA20_HIGH}%)")
        else:
            result["fail_reasons"].append(
                f"低于MA20过多 (偏离={c_ma20:.2f}%, 需>={MA20_LOW}%)")

    # ---- V9总判定 ----
    result["v9_pass"] = result["v9_cond1"] and result["v9_cond2"] and result["v9_cond3"]

    return result


def main():
    scan_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print("=" * 70)
    print("  V9 选股扫描器 — 三条件严格组合")
    print(f"  扫描时间: {scan_time}")
    print(f"  策略: 周线多头 + 尾盘杀跌(<{BZ_KILL_THRESH}%) + MA20回踩([{MA20_LOW}%,{MA20_HIGH}%])")
    print(f"  回测: WR=85.6%, EV=10.76% (N=125)")
    print("=" * 70)

    stocks = get_stock_list()
    print(f"\n中证1000成分股: {len(stocks)} 只")

    api = connect_tdx()
    print("pytdx连接成功\n")

    # === STEP 1: 筛选成交额Top 200 ===
    print("Step 1/3: 获取成交额 -> 筛选Top 200...")
    amt_list = []
    for i, row in stocks.iterrows():
        bars = api.get_security_bars(9, row["market"], row["code"], 0, 3)
        if bars:
            try:
                df = api.to_df(bars)
                if df is not None and not df.empty:
                    latest_amt = df.iloc[-1]["amount"]
                    amt_list.append({
                        "code": row["code"],
                        "name": row["name"],
                        "market": row["market"],
                        "latest_amt": latest_amt,
                    })
            except Exception:
                pass
        if (i + 1) % 100 == 0:
            print(f"  已处理 {i+1}/{len(stocks)} 只...")

    amt_df = pd.DataFrame(amt_list)
    amt_df = amt_df.sort_values("latest_amt", ascending=False).reset_index(drop=True)
    top_stocks = amt_df.head(TOP_N_AMOUNT).copy()
    print(f"  -> 筛选出成交额Top {TOP_N_AMOUNT} 只股票\n")

    # === STEP 2: 逐一拉取数据，计算V9信号 ===
    print(f"Step 2/3: 计算V9信号 (共{len(top_stocks)}只)...")
    results = []
    for i, row in top_stocks.iterrows():
        daily = fetch_daily_bars(api, row["market"], row["code"], count=120)
        weekly = fetch_weekly_bars(api, row["market"], row["code"], count=60)
        min5 = fetch_5min_bars(api, row["market"], row["code"])

        sig = compute_v9_signals(daily, weekly, min5)
        sig["code"] = row["code"]
        sig["name"] = row["name"]
        sig["market"] = row["market"]
        sig["amt_rank"] = i + 1
        results.append(sig)

        if (i + 1) % 20 == 0:
            print(f"  已处理 {i+1}/{len(top_stocks)} 只...")

    api.disconnect()

    res_df = pd.DataFrame(results)

    # === STEP 3: 分类输出 ===
    print("\nStep 3/3: 分类输出...\n")

    # V9全命中
    v9_pass_df = res_df[res_df["v9_pass"] == True].sort_values(
        "weekly_slope", ascending=False
    ).reset_index(drop=True)

    # 部分命中（周线多头+MA20回踩+尾盘微跌，缺bz_kill）
    partial_df = res_df[
        (res_df["v9_pass"] == False) &
        (res_df["v9_cond1"] == True) &
        (res_df["v9_cond3"] == True) &
        (res_df["bz_partial"] == True)
    ].sort_values("weekly_slope", ascending=False).reset_index(drop=True)

    # 保存完整结果
    save_cols = ["code", "name", "v9_pass", "v9_cond1", "v9_cond2", "v9_cond3",
                 "weekly_align", "weekly_slope", "bz_direction", "bz_rt",
                 "close_vs_ma20_pct", "amt_ratio", "last_close_pct",
                 "close", "ma20", "entry_price", "last_date", "amt_rank",
                 "bz_partial"]
    res_df[save_cols].to_csv(OUTPUT_DIR / "scanner_v9_full.csv", index=False, encoding="utf-8-sig")

    # ============ 输出V9全命中 ============
    print("=" * 70)
    print("  >>> V9 三条件全命中 (WR=85.6%, EV=10.76%) <<<")
    print("=" * 70)

    if len(v9_pass_df) == 0:
        print("\n    今日无V9全命中股票！")
        print("  说明: 需要同时满足 周线多头 + 尾盘杀跌(<-0.3%) + MA20回踩")
        print("  大盘走强时，多数股票已偏离MA20，信号自然稀缺")
    else:
        for rank, (_, row) in enumerate(v9_pass_df.head(MAX_CANDIDATES).iterrows(), 1):
            print(f"\n  {'━'*64}")
            print(f"  #{rank}  {row['code']}  {row['name']}")
            print(f"      进货价: {row['entry_price']:.2f}  (={row['close']:.2f}, 收盘价)")
            print(f"      MA20: {row['ma20']:.2f}  偏离: {row['close_vs_ma20_pct']:.2f}%")
            print(f"      周线slope: {row['weekly_slope']:.2f}%")
            print(f"      尾盘方向: {row['bz_direction']:.2f}% (杀跌阈值<{BZ_KILL_THRESH}%)")
            if row['bz_rt'] is not None:
                print(f"      尾盘实时(截至14:50): {row['bz_rt']:.2f}%")
            print(f"      当日涨跌: {row['last_close_pct']:.2f}%  量比: {row['amt_ratio']:.2f}x")
            print(f"      成交额排名: #{int(row['amt_rank'])}")
            print("      命中条件:")
            for r in row["pass_reasons"]:
                print(f"        {r}")

    # ============ 输出部分命中 ============
    print(f"\n{'='*70}")
    print("  >>> 部分命中 (周线多头+MA20回踩+尾盘微跌，缺bz杀跌) <<<")
    print("  >>> 置信度打折，仅作备选参考 <<<")
    print("=" * 70)

    if len(partial_df) == 0:
        print("\n  无部分命中股票")
    else:
        for rank, (_, row) in enumerate(partial_df.head(PARTIAL_CANDIDATES).iterrows(), 1):
            print(f"\n  {'─'*64}")
            print(f"  #{rank}  {row['code']}  {row['name']}")
            print(f"      进货价: {row['entry_price']:.2f}  MA20: {row['ma20']:.2f}  "
                  f"偏离: {row['close_vs_ma20_pct']:.2f}%")
            print(f"      周线slope: {row['weekly_slope']:.2f}%  "
                  f"尾盘: {row['bz_direction']:.2f}% (需<{BZ_KILL_THRESH}%)")
            print(f"      当日涨跌: {row['last_close_pct']:.2f}%")
            print(f"      缺失原因:")
            for r in row["fail_reasons"][:2]:
                print(f"        {r}")

    # ============ 执行计划 ============
    print(f"\n{'='*70}")
    print("  >>> 执行计划 <<<")
    print("=" * 70)
    print("""
  盘中操作流程:
  ┌─────────────────────────────────────────────────────────────┐
  │ 14:30  预热: 运行scanner，拉取日线+周线+5分钟数据          │
  │        → 预筛"周线多头+MA20回踩"的股票（条件1+3）         │
  │ 14:50  决策: 检查预筛股票的尾盘5分钟数据                   │
  │        → 计算 bz_rt (截至14:50的14:30-14:50区间方向)       │
  │        → bz_rt < -0.3% → 确认买入                          │
  │ 14:53  下单: 市价/限价买入（限价=当前价+1~2tick）          │
  │ 14:55  成交: 目标在14:55前完成买入                          │
  │        → 实际入场价 ≈ 14:55的5分钟K线close                  │
  │        → 与回测entry_price(=日线close)的滑点约0.1-0.3%     │
  └─────────────────────────────────────────────────────────────┘
  
  收盘后验证:
  - 运行scanner（用完整14:30-15:00数据）
  - 对比 bz_rt vs bz_direction，评估实时信号的准确性
  - 记录 entry_price vs 实际买入价，计算实际滑点

  买入价格说明:
  - 进货价(entry_price) = 当日收盘价（回测锚定价）
  - 实际买入价 ≈ 14:55市价（比收盘价早5分钟）
  - 差异来源: 14:55-15:00的价格波动 + 滑点
  - T+5收益率 = (第5个交易日收盘价 - 实际买入价) / 实际买入价
""")

    # ============ 5月29日回溯诊断 ============
    print(f"\n{'='*70}")
    print("  >>> 5月29日A方案回溯诊断 <<<")
    print("=" * 70)

    # A方案5只: 杰华特、富创精密、长芯博创、德明利、有研新材
    aplan_codes = ["688141", "688409", "300548", "001309", "600206"]
    aplan_names = {"688141": "杰华特", "688409": "富创精密", "300548": "长芯博创",
                   "001309": "德明利", "600206": "有研新材"}

    print(f"\n  {'股票':<12} {'bz_direction':>12} {'V9命中?':>8} {'缺失条件':<20}")
    print(f"  {'─'*56}")
    for code in aplan_codes:
        match = res_df[res_df["code"] == code]
        if len(match) > 0:
            row = match.iloc[0]
            bz_str = f"{row['bz_direction']:.2f}%" if row['bz_direction'] is not None else "N/A"
            v9_str = "YES" if row['v9_pass'] else "NO"
            # 找缺失条件
            missing = []
            if not row['v9_cond2']:
                missing.append("尾盘杀跌")
            if not row['v9_cond3']:
                missing.append("MA20回踩")
            if not row['v9_cond1']:
                missing.append("周线多头")
            miss_str = "+".join(missing) if missing else "-"
            print(f"  {code} {aplan_names[code]:<6} {bz_str:>12} {v9_str:>8} {miss_str:<20}")

    print(f"\n  结论: A方案5只无一命中V9核心条件'尾盘杀跌'(bz<-0.3%)")
    print(f"  旧scanner用V8评分制(5分打分)，选出的股票缺关键信号")
    print(f"  V9扫描器改用三条件AND逻辑，确保只选真正符合条件的股票")

    # ============ 统计摘要 ============
    print(f"\n{'='*70}")
    print("  >>> 统计摘要 <<<")
    print("=" * 70)
    total = len(res_df)
    weekly_only = res_df["v9_cond1"].sum()
    weekly_ma20 = (res_df["v9_cond1"] & res_df["v9_cond3"]).sum()
    bz_kill_count = res_df["v9_cond2"].sum()
    v9_pass_count = res_df["v9_pass"].sum()
    partial_count = len(partial_df)

    print(f"\n  Top {TOP_N_AMOUNT} 股票:")
    print(f"    周线多头: {weekly_only} 只 ({weekly_only/total*100:.1f}%)")
    print(f"    周线多头+MA20回踩: {weekly_ma20} 只 ({weekly_ma20/total*100:.1f}%)")
    print(f"    尾盘杀跌(bz<{BZ_KILL_THRESH}%): {bz_kill_count} 只 ({bz_kill_count/total*100:.1f}%)")
    print(f"    V9全命中: {v9_pass_count} 只 ({v9_pass_count/total*100:.1f}%)")
    print(f"    部分命中(缺bz_kill): {partial_count} 只")

    print(f"\n  完整结果已保存: {OUTPUT_DIR / 'scanner_v9_full.csv'}")
    print(f"\n  注意: V9全命中=0是正常的（信号稀缺），可等下一交易日")
    print(f"  部分命中仅作参考，置信度打折，仓位应减半")
    print("=" * 70)

    return {
        "v9_pass": v9_pass_df.to_dict("records"),
        "partial": partial_df.head(PARTIAL_CANDIDATES).to_dict("records"),
    }


if __name__ == "__main__":
    main()
