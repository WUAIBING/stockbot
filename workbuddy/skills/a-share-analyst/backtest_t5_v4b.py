#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V4b Backtest: 资金强势信号组合回测 (修正版)
==========================================
V4教训:
  - "收盘站回MA20上方"是错误的入场条件！
  - close略低于MA20 (-2%~0%) 反而是更好的入场点
  - MA20向上反而不如非向上（可能是趋势末期）
  - 周线多头和缩量是正向信号，但独立效果有限

V4b策略:
  用户规则重新解读:
  1. 14:50入场 (用日线close近似)
  2. "回踩MA20后收盘不能太离谱" = close在MA20附近，允许略低(-2%~+1%)
  3. 周线MA5>MA10>MA20多头排列 = 趋势确认
  4. 回踩日缩量 = 洗盘而非出货

  额外探索:
  - close_pct分档对胜率和收益的影响
  - 周线多头+缩量的交叉效果
  - MA20斜率分组
  - 入场时MA5/MA10位置关系

出场: T+5收盘
"""

import sys
import json
import time
import os
import numpy as np
import pandas as pd
from pytdx.hq import TdxHq_API

TDX_SERVER = ('60.191.117.167', 7709)

def connect_tdx(max_retries=3):
    for attempt in range(max_retries):
        try:
            api = TdxHq_API()
            if api.connect(*TDX_SERVER):
                print(f"  TDX connected (attempt {attempt+1})")
                return api
        except:
            time.sleep(2)
    raise ConnectionError("Failed to connect")

def get_stock_universe(api, max_stocks=300):
    stocks = []
    for market in [0, 1]:
        start = 0
        while True:
            try:
                batch = api.get_security_list(market, start)
            except:
                break
            if not batch:
                break
            for s in batch:
                code = s.get('code', '')
                name = s.get('name', '')
                if market == 1 and code.startswith('60'):
                    stocks.append((market, code, name))
                elif market == 0 and code.startswith('00'):
                    stocks.append((market, code, name))
                if len(stocks) >= max_stocks:
                    break
            start += len(batch)
            if len(stocks) >= max_stocks:
                break
            time.sleep(0.02)
    return stocks

def fetch_bars(api, category, market, code, count=800):
    all_bars = []
    fetched = 0
    while fetched < count:
        batch_size = min(800, count - fetched)
        try:
            batch = api.get_security_bars(category, market, code, fetched, batch_size)
        except:
            break
        if not batch:
            break
        all_bars.extend(batch)
        fetched += len(batch)
        if len(batch) < batch_size:
            break
        time.sleep(0.02)
    return all_bars

def bars_to_df(bars):
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(bars)
    df = df.iloc[::-1].reset_index(drop=True)
    return df

def compute_ma(series, period):
    return series.rolling(period, min_periods=period).mean()

def get_weekly_idx(weekly_df, day_date):
    for wi in range(len(weekly_df) - 1, -1, -1):
        if pd.Timestamp(weekly_df['datetime'].iloc[wi]) <= day_date:
            return wi
    return None

def check_weekly_alignment(weekly_close, idx):
    if idx < 20:
        return False
    ma5 = weekly_close.iloc[max(0, idx-4):idx+1].mean()
    ma10 = weekly_close.iloc[max(0, idx-9):idx+1].mean()
    ma20 = weekly_close.iloc[max(0, idx-19):idx+1].mean()
    return (ma5 > ma10) and (ma10 > ma20)

def run_backtest_v4b():
    print("=" * 70)
    print("V4b Backtest: 资金强势信号组合 (修正版)")
    print("=" * 70)

    api = connect_tdx()
    stocks = get_stock_universe(api, max_stocks=300)
    print(f"股票池: {len(stocks)} 只\n")

    all_trades = []
    processed = 0

    for market, code, name in stocks:
        processed += 1
        if processed % 50 == 0:
            print(f"  进度: {processed}/{len(stocks)}, 已收集 {len(all_trades)} 笔")

        try:
            daily_bars = fetch_bars(api, 4, market, code, count=600)
            if len(daily_bars) < 60:
                continue
            daily_df = bars_to_df(daily_bars)

            weekly_bars = fetch_bars(api, 5, market, code, count=120)
            if len(weekly_bars) < 30:
                continue
            weekly_df = bars_to_df(weekly_bars)

            # 日线指标
            daily_df['ma5'] = compute_ma(daily_df['close'], 5)
            daily_df['ma10'] = compute_ma(daily_df['close'], 10)
            daily_df['ma20'] = compute_ma(daily_df['close'], 20)
            daily_df['vol_ma5'] = compute_ma(daily_df['amount'], 5)
            daily_df['ma20_slope'] = daily_df['ma20'].diff(5) / daily_df['ma20'].shift(5) * 100  # 5日斜率%

            for i in range(25, len(daily_df) - 5):
                row = daily_df.iloc[i]
                ma20 = row['ma20']

                if pd.isna(ma20) or ma20 <= 0:
                    continue

                # ===== 核心条件: 日线回踩MA20 =====
                low_pct = (row['low'] - ma20) / ma20 * 100
                if low_pct > 2.0 or low_pct < -5.0:
                    continue

                close_pct = (row['close'] - ma20) / ma20 * 100
                # "收盘不能太离谱": close在MA20附近，-3% ~ +2%
                if close_pct < -3.0 or close_pct > 2.0:
                    continue

                # ===== 周线 =====
                day_date = pd.Timestamp(row['datetime'])
                w_idx = get_weekly_idx(weekly_df, day_date)
                if w_idx is None or w_idx < 20:
                    continue
                weekly_aligned = check_weekly_alignment(weekly_df['close'], w_idx)

                # ===== MA20斜率 =====
                ma20_slope = row['ma20_slope']
                ma20_rising = (not pd.isna(ma20_slope) and ma20_slope > 0)

                # ===== 缩量 =====
                vol_ma5 = row['vol_ma5']
                is_shrink = (not pd.isna(vol_ma5) and vol_ma5 > 0 and row['amount'] < vol_ma5)

                # ===== 日线MA5/MA10位置 =====
                ma5 = row['ma5']
                ma10 = row['ma10']
                daily_bullish = (not pd.isna(ma5) and not pd.isna(ma10) and ma5 > ma10 and ma10 > ma20)

                # ===== 入场 & 出场 =====
                entry_price = row['close']
                exit_price = daily_df['close'].iloc[i + 5]
                ret = (exit_price - entry_price) / entry_price * 100

                all_trades.append({
                    'code': code,
                    'name': name,
                    'date': str(row['datetime']),
                    'return': round(ret, 2),
                    # 信号
                    'weekly_align': weekly_aligned,
                    'ma20_rising': ma20_rising,
                    'vol_shrink': is_shrink,
                    'daily_bullish': daily_bullish,
                    # 特征
                    'low_pct': round(low_pct, 2),
                    'close_pct': round(close_pct, 2),
                    'ma20_slope': round(ma20_slope, 2) if not pd.isna(ma20_slope) else None,
                    # close与MA20的距离分组
                    'close_grp': classify_close(close_pct),
                })

        except:
            continue
        time.sleep(0.03)

    api.disconnect()
    print(f"\n  完成: {len(stocks)} 只, {len(all_trades)} 笔交易\n")

    if not all_trades:
        print("无交易!")
        return

    df = pd.DataFrame(all_trades)
    analyze_v4b(df)

def classify_close(pct):
    """收盘位置分组"""
    if pct < -2: return 'A_below2'
    if pct < -1: return 'B_below1'
    if pct < 0: return 'C_slight_below'
    if pct < 1: return 'D_slight_above'
    return 'E_above1'

def compute_stats(df):
    total = len(df)
    if total == 0:
        return {'total': 0, 'win_rate': 0, 'avg_ret': 0, 'avg_win': 0, 'avg_loss': 0, 'pf': 0, 'ev': 0}
    wins = df[df['return'] > 0]
    losses = df[df['return'] <= 0]
    wr = len(wins) / total * 100
    ar = df['return'].mean()
    aw = wins['return'].mean() if len(wins) > 0 else 0
    al = losses['return'].mean() if len(losses) > 0 else 0
    tw = wins['return'].sum() if len(wins) > 0 else 0
    tl = abs(losses['return'].sum()) if len(losses) > 0 else 0
    pf = tw / tl if tl > 0 else float('inf')
    ev = wr/100 * aw + (1-wr/100) * al
    return {
        'total': total, 'win_rate': round(wr, 1),
        'avg_ret': round(ar, 2), 'avg_win': round(aw, 2),
        'avg_loss': round(al, 2), 'pf': round(pf, 2), 'ev': round(ev, 2),
    }

def ps(label, stats):
    """打印统计"""
    print(f"  [{label}] N={stats['total']}, 胜率={stats['win_rate']:.1f}%, "
          f"均收益={stats['avg_ret']:.2f}%, 均赢={stats['avg_win']:.2f}%, "
          f"均亏={stats['avg_loss']:.2f}%, 盈亏比={stats['pf']:.2f}, "
          f"期望值={stats['ev']:.2f}%")

def analyze_v4b(df):
    print("=" * 70)
    print("  V4b 回测结果")
    print("=" * 70)

    # ---- 1. 总体 ----
    print("\n【1. 总体 (所有回踩MA20日, close在±3%)】")
    ps("All", compute_stats(df))

    # ---- 2. close_pct分档 vs 胜率/收益 ----
    print("\n【2. 收盘位置分档 vs 胜率/收益 (核心发现)】")
    print("-" * 85)
    print(f"{'分组':>18} | {'数量':>6} | {'胜率':>7} | {'均收益':>7} | {'均赢':>7} | {'均亏':>7} | {'盈亏比':>7} | {'期望值':>7}")
    print("-" * 85)
    for grp in ['A_below2', 'B_below1', 'C_slight_below', 'D_slight_above', 'E_above1']:
        sub = df[df['close_grp'] == grp]
        if len(sub) > 0:
            s = compute_stats(sub)
            label = grp.replace('A_', '<-2%').replace('B_', '-2~-1%').replace('C_', '-1~0%').replace('D_', '0~1%').replace('E_', '>1%')
            print(f"{label:>18} | {s['total']:>6} | {s['win_rate']:>6.1f}% | {s['avg_ret']:>6.2f}% | {s['avg_win']:>6.2f}% | {s['avg_loss']:>6.2f}% | {s['pf']:>7.2f} | {s['ev']:>6.2f}%")

    # ---- 3. 周线多头 × close_pct ----
    print("\n【3. 周线多头 × 收盘位置 (交叉分析)】")
    print("-" * 85)
    for grp in ['C_slight_below', 'D_slight_above', 'B_below1']:
        for wa in [True, False]:
            sub = df[(df['close_grp'] == grp) & (df['weekly_align'] == wa)]
            if len(sub) > 10:
                s = compute_stats(sub)
                label = f"close={grp.replace('C_','-1~0%').replace('D_','0~1%').replace('B_','-2~-1%')} + 周线{'多头' if wa else '非多头'}"
                ps(label, s)

    # ---- 4. 周线多头 × 缩量 ----
    print("\n【4. 周线多头 × 缩量 × close位置】")
    for wa in [True, False]:
        for vs in [True, False]:
            sub = df[(df['weekly_align'] == wa) & (df['vol_shrink'] == vs)]
            if len(sub) > 10:
                s = compute_stats(sub)
                ps(f"周线{'多头' if wa else '非多头'}+{'缩量' if vs else '放量'}", s)

    # ---- 5. 用户最优规则探索 ----
    print("\n【5. 最优规则探索】")

    # 组合A: 周线多头 + 缩量 + close略低
    for grp in ['C_slight_below', 'B_below1']:
        sub = df[(df['weekly_align'] == True) & (df['vol_shrink'] == True) & (df['close_grp'] == grp)]
        if len(sub) > 5:
            s = compute_stats(sub)
            ps(f"周线多头+缩量+close={grp}", s)

    # 组合B: 周线多头 + 日线MA5>MA10
    sub = df[(df['weekly_align'] == True) & (df['daily_bullish'] == True)]
    if len(sub) > 10:
        s = compute_stats(sub)
        ps("周线多头+日线MA5>MA10>MA20", s)

    # 组合C: 周线多头 + 缩量 + close略低 + 日线MA5>MA10
    for grp in ['C_slight_below', 'B_below1']:
        sub = df[(df['weekly_align'] == True) & (df['vol_shrink'] == True) &
                 (df['daily_bullish'] == True) & (df['close_grp'] == grp)]
        if len(sub) > 3:
            s = compute_stats(sub)
            ps(f"全条件+close={grp}", s)

    # 组合D: 缩量 + close略低 (无周线多头要求)
    sub = df[(df['vol_shrink'] == True) & (df['close_grp'].isin(['C_slight_below', 'B_below1']))]
    if len(sub) > 10:
        s = compute_stats(sub)
        ps("缩量+close(-2%~0%)", s)

    # ---- 6. MA20斜率分组 ----
    print("\n【6. MA20斜率分组】")
    for lo, hi, label in [(-100, 0, '下降'), (0, 0.5, '微升'), (0.5, 100, '明显上升')]:
        sub = df[(df['ma20_slope'] > lo) & (df['ma20_slope'] <= hi)]
        if len(sub) > 10:
            s = compute_stats(sub)
            ps(f"MA20斜率{label}({lo}~{hi}%)", s)

    # ---- 7. 与基准对比 ----
    print("\n【7. 与基准对比】")
    print(f"  用户基准: 胜率81%, T+5收益9%")

    # 找最优组合
    best_ev = -999
    best_label = ""
    best_stats = None

    # 遍历所有可能的双条件组合
    conditions = {
        'weekly_align': [True, False],
        'vol_shrink': [True, False],
        'daily_bullish': [True, False],
        'close_grp': ['C_slight_below', 'B_below1', 'D_slight_above'],
    }

    from itertools import product
    for wa, vs, db, cg in product([True, False], [True, False], [True, False],
                                   ['C_slight_below', 'B_below1', 'D_slight_above']):
        sub = df[(df['weekly_align']==wa) & (df['vol_shrink']==vs) &
                 (df['daily_bullish']==db) & (df['close_grp']==cg)]
        if len(sub) >= 20:
            s = compute_stats(sub)
            if s['ev'] > best_ev and s['win_rate'] > 50:
                best_ev = s['ev']
                best_label = f"周线多头={wa},缩量={vs},日线多头={db},close={cg}"
                best_stats = s

    if best_stats:
        print(f"\n  最优组合 (胜率>50%且期望值最高):")
        ps(best_label, best_stats)
    else:
        print("  未找到胜率>50%的组合")

    # ---- 8. top trades分布 ----
    print("\n【8. 最优组合的收益分布】")
    if best_stats and best_stats['total'] > 0:
        # 重新找这组数据
        wa_b, vs_b, db_b, cg_b = best_label.split(',')
        wa_b = 'True' in wa_b
        vs_b = 'True' in vs_b
        db_b = 'True' in db_b
        cg_b = [c for c in ['C_slight_below', 'B_below1', 'D_slight_above'] if c in best_label][0]
        sub = df[(df['weekly_align']==wa_b) & (df['vol_shrink']==vs_b) &
                 (df['daily_bullish']==db_b) & (df['close_grp']==cg_b)]
        rets = sub['return']
        print(f"  最小: {rets.min():.2f}%")
        print(f"  25%: {rets.quantile(0.25):.2f}%")
        print(f"  中位: {rets.median():.2f}%")
        print(f"  75%: {rets.quantile(0.75):.2f}%")
        print(f"  最大: {rets.max():.2f}%")
        print(f"  >5%: {(rets>5).sum()/len(rets)*100:.1f}%, >9%: {(rets>9).sum()/len(rets)*100:.1f}%")
        print(f"  <-5%: {(rets<-5).sum()/len(rets)*100:.1f}%")

    # ---- 保存 ----
    output_dir = os.path.expanduser("~/.workbuddy/a-share-analyst")
    os.makedirs(output_dir, exist_ok=True)

    summary = {
        "version": "V4b",
        "total_trades": len(df),
        "best_combo": {"label": best_label, **best_stats} if best_stats else None,
        "by_close_grp": {grp: compute_stats(df[df['close_grp']==grp]) for grp in df['close_grp'].unique()},
        "weekly_yes": compute_stats(df[df['weekly_align']==True]),
        "weekly_no": compute_stats(df[df['weekly_align']==False]),
        "shrink_yes": compute_stats(df[df['vol_shrink']==True]),
        "shrink_no": compute_stats(df[df['vol_shrink']==False]),
        "benchmark": {"win_rate": 81, "t5_return": 9},
    }

    with open(os.path.join(output_dir, "v4b_backtest_results.json"), 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False, default=str)

    # 保存完整交易数据 (不用sample了，全量存CSV)
    df.to_csv(os.path.join(output_dir, "v4b_trades_full.csv"), index=False, encoding='utf-8-sig')

    print(f"\n结果保存: {output_dir}/")
    print("  v4b_backtest_results.json")
    print("  v4b_trades_full.csv")

if __name__ == '__main__':
    run_backtest_v4b()
