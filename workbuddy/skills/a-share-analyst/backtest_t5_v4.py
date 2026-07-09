#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V4 Backtest: 资金强势信号组合回测
=================================
用户规则:
  1. 买入时间14:50 (用日线收盘价近似)
  2. 日线回踩MA20后收盘不能太离谱
  3. 周线MA5, MA10, MA20保持多头排列
  4. MA20方向向上 (趋势确认)

信号评分体系 (资金强势度):
  +1 周线多头排列 (MA5>MA10>MA20)
  +1 收盘价 >= MA20 (回踩后站回MA20上方)
  +1 MA20斜率向上 (5日斜率>0)
  +1 回踩日缩量 (成交额 < 5日均量额)
  Score: 0~4

出场: T+5收盘
核心指标: 胜率 + 平均收益率 + 期望值
"""

import sys
import json
import time
import os
import numpy as np
import pandas as pd
from pytdx.hq import TdxHq_API

TDX_SERVER = ('60.191.117.167', 7709)
PYTHON = sys.executable

# ================ 连接管理 ================

def connect_tdx(max_retries=3):
    """连接通达信服务器"""
    for attempt in range(max_retries):
        try:
            api = TdxHq_API()
            if api.connect(*TDX_SERVER):
                print(f"  TDX connected (attempt {attempt+1})")
                return api
        except Exception as e:
            print(f"  Connect failed ({attempt+1}): {e}", file=sys.stderr)
            time.sleep(2)
    raise ConnectionError(f"Failed to connect after {max_retries} attempts")


# ================ 数据获取 ================

def get_stock_universe(api, max_stocks=300):
    """获取沪深主板股票列表"""
    stocks = []
    for market in [0, 1]:  # 0=SZ, 1=SH
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
                # 只取主板: SH 60xxxx, SZ 00xxxx
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
    """获取K线数据，自动分页"""
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
    """转换K线数据为DataFrame，按时间正序"""
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(bars)
    # pytdx返回最新在前，需反转为时间正序
    df = df.iloc[::-1].reset_index(drop=True)
    return df


# ================ 指标计算 ================

def compute_ma(series, period):
    """计算移动平均线"""
    return series.rolling(period, min_periods=period).mean()


def get_weekly_idx(weekly_df, day_date):
    """找到日线日期对应的周线索引"""
    for wi in range(len(weekly_df) - 1, -1, -1):
        if pd.Timestamp(weekly_df['datetime'].iloc[wi]) <= day_date:
            return wi
    return None


def check_weekly_alignment(weekly_close, idx):
    """检查周线MA5>MA10>MA20多头排列"""
    if idx < 20:
        return False
    ma5 = weekly_close.iloc[max(0, idx-4):idx+1].mean()
    ma10 = weekly_close.iloc[max(0, idx-9):idx+1].mean()
    ma20 = weekly_close.iloc[max(0, idx-19):idx+1].mean()
    return (ma5 > ma10) and (ma10 > ma20)


def check_ma20_rising(ma20_series, idx, lookback=5):
    """检查MA20是否向上（近lookback日斜率为正）"""
    if idx < lookback:
        return False
    recent = ma20_series.iloc[idx-lookback+1:idx+1]
    if recent.isna().any() or len(recent) < lookback:
        return False
    # 简单线性回归斜率
    x = np.arange(lookback, dtype=float)
    y = recent.values.astype(float)
    slope = np.polyfit(x, y, 1)[0]
    return slope > 0


# ================ 主回测逻辑 ================

def run_backtest_v4():
    print("=" * 70)
    print("V4 Backtest: 资金强势信号组合回测")
    print("策略: 周线多头 + 日线回踩MA20 + 收盘合理 + MA20向上")
    print("入场: 14:50(用日线close近似) | 出场: T+5收盘")
    print("=" * 70)
    print()

    api = connect_tdx()
    stocks = get_stock_universe(api, max_stocks=300)
    print(f"股票池: {len(stocks)} 只\n")

    # 收集所有交易
    all_trades = []
    processed = 0

    for market, code, name in stocks:
        processed += 1
        if processed % 50 == 0:
            print(f"  处理进度: {processed}/{len(stocks)}, 已收集 {len(all_trades)} 笔交易")

        try:
            # 获取日线数据 (约300交易日)
            daily_bars = fetch_bars(api, 4, market, code, count=600)
            if len(daily_bars) < 60:
                continue
            daily_df = bars_to_df(daily_bars)

            # 获取周线数据 (约100周)
            weekly_bars = fetch_bars(api, 5, market, code, count=120)
            if len(weekly_bars) < 30:
                continue
            weekly_df = bars_to_df(weekly_bars)

            # ---- 计算日线指标 ----
            daily_df['ma20'] = compute_ma(daily_df['close'], 20)
            daily_df['vol_ma5'] = compute_ma(daily_df['amount'], 5)  # 成交额5日均线

            # ---- 遍历每个交易日 ----
            for i in range(25, len(daily_df) - 5):
                row = daily_df.iloc[i]
                ma20 = row['ma20']

                if pd.isna(ma20) or ma20 <= 0:
                    continue

                # ===== 核心条件: 日线回踩MA20 =====
                low_pct = (row['low'] - ma20) / ma20  # low相对MA20的偏离
                # low接近MA20: -5% < low_pct <= 2% (回踩到了MA20附近)
                if low_pct > 0.02 or low_pct < -0.05:
                    continue

                close_pct = (row['close'] - ma20) / ma20  # close相对MA20的偏离

                # ===== 周线对应 =====
                day_date = pd.Timestamp(row['datetime'])
                w_idx = get_weekly_idx(weekly_df, day_date)
                if w_idx is None or w_idx < 20:
                    continue

                weekly_aligned = check_weekly_alignment(weekly_df['close'], w_idx)

                # ===== MA20方向 =====
                ma20_rising = check_ma20_rising(daily_df['ma20'], i)

                # ===== 缩量判断 =====
                vol_ma5 = row['vol_ma5']
                is_shrink = (not pd.isna(vol_ma5) and vol_ma5 > 0 and
                             row['amount'] < vol_ma5)

                # ===== 信号评分 (0~4) =====
                score = 0
                if weekly_aligned:
                    score += 1
                if close_pct >= 0:  # 收盘站回MA20上方
                    score += 1
                if ma20_rising:
                    score += 1
                if is_shrink:
                    score += 1

                # ===== 入场 & 出场 =====
                entry_price = row['close']  # 14:50入场，用close近似
                exit_price = daily_df['close'].iloc[i + 5]  # T+5收盘
                ret = (exit_price - entry_price) / entry_price * 100

                all_trades.append({
                    'code': code,
                    'name': name,
                    'date': str(row['datetime']),
                    'entry': round(entry_price, 2),
                    'exit': round(exit_price, 2),
                    'return': round(ret, 2),
                    'score': score,
                    # 信号明细
                    'weekly_align': weekly_aligned,
                    'close_above_ma20': close_pct >= 0,
                    'ma20_rising': ma20_rising,
                    'vol_shrink': is_shrink,
                    # 特征值
                    'low_pct': round(low_pct * 100, 2),
                    'close_pct': round(close_pct * 100, 2),
                })

        except Exception as e:
            # 静默跳过单只股票错误
            continue

        time.sleep(0.03)

    api.disconnect()
    print(f"\n  处理完成: {processed} 只股票, {len(all_trades)} 笔交易\n")

    if not all_trades:
        print("未找到任何交易！")
        return

    # ================ 分析结果 ================
    df = pd.DataFrame(all_trades)
    analyze_results(df)


def analyze_results(df):
    """分析回测结果：按信号评分分组"""

    print("=" * 70)
    print("  V4 回测结果汇总")
    print("=" * 70)

    # ---- 1. 总体概览 ----
    print("\n【1. 总体概览 - 所有MA20回踩日】")
    print_summary(df, "All Pullbacks")

    # ---- 2. 按信号评分分组 ----
    print("\n【2. 按信号评分分组 (资金强势度 0~4)】")
    print("-" * 70)
    print(f"{'Score':>6} | {'数量':>6} | {'胜率':>8} | {'均收益':>8} | {'均赢':>8} | {'均亏':>8} | {'盈亏比':>8} | {'期望值':>8}")
    print("-" * 70)

    score_results = {}
    for score in range(5):
        sub = df[df['score'] == score]
        if len(sub) == 0:
            continue
        stats = compute_stats(sub)
        score_results[score] = stats
        print(f"{score:>6} | {stats['total']:>6} | {stats['win_rate']:>7.1f}% | {stats['avg_ret']:>7.2f}% | {stats['avg_win']:>7.2f}% | {stats['avg_loss']:>7.2f}% | {stats['pf']:>8.2f} | {stats['ev']:>7.2f}%")

    # ---- 3. 关键条件对比 ----
    print("\n【3. 关键条件增量价值】")
    print("-" * 70)

    # A: 周线多头 vs 非周线多头
    w_yes = df[df['weekly_align'] == True]
    w_no = df[df['weekly_align'] == False]
    if len(w_yes) > 0 and len(w_no) > 0:
        print(f"\n  周线多头排列:")
        print(f"    有周线多头: {len(w_yes)}笔, 胜率{compute_stats(w_yes)['win_rate']:.1f}%, 均收益{compute_stats(w_yes)['avg_ret']:.2f}%, 期望值{compute_stats(w_yes)['ev']:.2f}%")
        print(f"    无周线多头: {len(w_no)}笔, 胜率{compute_stats(w_no)['win_rate']:.1f}%, 均收益{compute_stats(w_no)['avg_ret']:.2f}%, 期望值{compute_stats(w_no)['ev']:.2f}%")

    # B: 收盘站回MA20 vs 低于MA20
    c_yes = df[df['close_above_ma20'] == True]
    c_no = df[df['close_above_ma20'] == False]
    if len(c_yes) > 0 and len(c_no) > 0:
        print(f"\n  收盘站回MA20:")
        print(f"    close>=MA20: {len(c_yes)}笔, 胜率{compute_stats(c_yes)['win_rate']:.1f}%, 均收益{compute_stats(c_yes)['avg_ret']:.2f}%, 期望值{compute_stats(c_yes)['ev']:.2f}%")
        print(f"    close<MA20:  {len(c_no)}笔, 胜率{compute_stats(c_no)['win_rate']:.1f}%, 均收益{compute_stats(c_no)['avg_ret']:.2f}%, 期望值{compute_stats(c_no)['ev']:.2f}%")

    # C: MA20向上 vs 非向上
    r_yes = df[df['ma20_rising'] == True]
    r_no = df[df['ma20_rising'] == False]
    if len(r_yes) > 0 and len(r_no) > 0:
        print(f"\n  MA20方向向上:")
        print(f"    MA20向上: {len(r_yes)}笔, 胜率{compute_stats(r_yes)['win_rate']:.1f}%, 均收益{compute_stats(r_yes)['avg_ret']:.2f}%, 期望值{compute_stats(r_yes)['ev']:.2f}%")
        print(f"    MA20非向上: {len(r_no)}笔, 胜率{compute_stats(r_no)['win_rate']:.1f}%, 均收益{compute_stats(r_no)['avg_ret']:.2f}%, 期望值{compute_stats(r_no)['ev']:.2f}%")

    # D: 缩量 vs 放量
    s_yes = df[df['vol_shrink'] == True]
    s_no = df[df['vol_shrink'] == False]
    if len(s_yes) > 0 and len(s_no) > 0:
        print(f"\n  回踩日缩量:")
        print(f"    缩量: {len(s_yes)}笔, 胜率{compute_stats(s_yes)['win_rate']:.1f}%, 均收益{compute_stats(s_yes)['avg_ret']:.2f}%, 期望值{compute_stats(s_yes)['ev']:.2f}%")
        print(f"    放量: {len(s_no)}笔, 胜率{compute_stats(s_no)['win_rate']:.1f}%, 均收益{compute_stats(s_no)['avg_ret']:.2f}%, 期望值{compute_stats(s_no)['ev']:.2f}%")

    # ---- 4. 最强组合 (score>=3) ----
    print("\n【4. 最强信号组合 (score>=3)】")
    strong = df[df['score'] >= 3]
    if len(strong) > 0:
        print_summary(strong, "Strong Signals (score>=3)")

        # 进一步细分 score=3 vs score=4
        for s in [3, 4]:
            sub = df[df['score'] == s]
            if len(sub) > 0:
                stats = compute_stats(sub)
                print(f"  Score={s}: {stats['total']}笔, 胜率{stats['win_rate']:.1f}%, 均收益{stats['avg_ret']:.2f}%, 期望值{stats['ev']:.2f}%")
    else:
        print("  无score>=3的交易")

    # ---- 5. 用户规则验证 ----
    print("\n【5. 用户规则组合验证】")
    print("  用户规则 = 周线多头 + 收盘>=MA20 + MA20向上")
    user_rule = df[
        (df['weekly_align'] == True) &
        (df['close_above_ma20'] == True) &
        (df['ma20_rising'] == True)
    ]
    if len(user_rule) > 0:
        print_summary(user_rule, "User Rules (weekly+close>=MA20+rising)")

        # 额外: 加缩量
        user_with_shrink = user_rule[user_rule['vol_shrink'] == True]
        if len(user_with_shrink) > 0:
            print(f"\n  加缩量条件:")
            print_summary(user_with_shrink, "User Rules + Volume Shrink")
    else:
        print("  用户规则组合无交易")

    # ---- 6. 收益分布 ----
    print("\n【6. T+5收益分布 (用户规则)】")
    if len(user_rule) > 0:
        rets = user_rule['return']
        print(f"  最小值: {rets.min():.2f}%")
        print(f"  25分位: {rets.quantile(0.25):.2f}%")
        print(f"  中位数: {rets.median():.2f}%")
        print(f"  75分位: {rets.quantile(0.75):.2f}%")
        print(f"  最大值: {rets.max():.2f}%")
        print(f"  标准差: {rets.std():.2f}%")
        print(f"  >5%占比: {(rets > 5).sum() / len(rets) * 100:.1f}%")
        print(f"  >9%占比: {(rets > 9).sum() / len(rets) * 100:.1f}%")
        print(f"  <-5%占比: {(rets < -5).sum() / len(rets) * 100:.1f}%")

    # ---- 7. Cohen's d 效应量 (用户规则 vs 对照) ----
    print("\n【7. 效应量分析 (Cohen's d)】")
    if len(user_rule) > 0 and len(w_no) > 0:
        d = cohens_d(user_rule['return'].values, w_no['return'].values)
        print(f"  用户规则 vs 无周线多头: d={d:.3f} ({effect_label(d)})")
    if len(user_rule) > 0 and len(df) > len(user_rule):
        rest = df[~df.index.isin(user_rule.index)]
        if len(rest) > 0:
            d = cohens_d(user_rule['return'].values, rest['return'].values)
            print(f"  用户规则 vs 其他回踩: d={d:.3f} ({effect_label(d)})")

    # ---- 8. 与基准对比 ----
    print("\n【8. 与用户基准对比】")
    print(f"  用户基准: 胜率81%, T+5收益9%")
    if len(user_rule) > 0:
        stats = compute_stats(user_rule)
        print(f"  当前结果: 胜率{stats['win_rate']:.1f}%, 均收益{stats['avg_ret']:.2f}%, 期望值{stats['ev']:.2f}%")
        gap_wr = 81 - stats['win_rate']
        gap_ret = 9 - stats['avg_ret']
        print(f"  胜率差距: {gap_wr:.1f}个百分点")
        print(f"  收益差距: {gap_ret:.2f}个百分点")
        print(f"  → {'还需继续进化' if gap_wr > 10 else '接近目标!'}")

    # ---- 保存结果 ----
    output_dir = os.path.expanduser("~/.workbuddy/a-share-analyst")
    os.makedirs(output_dir, exist_ok=True)

    # 保存汇总
    summary = {
        "version": "V4",
        "strategy": "资金强势信号组合",
        "total_trades": len(df),
        "by_score": {str(k): compute_stats(df[df['score']==k]) for k in range(5) if len(df[df['score']==k]) > 0},
        "user_rule": compute_stats(user_rule) if len(user_rule) > 0 else None,
        "user_rule_plus_shrink": compute_stats(user_with_shrink) if len(user_with_shrink) > 0 else None,
        "benchmark": {"win_rate": 81, "t5_return": 9},
    }

    with open(os.path.join(output_dir, "v4_backtest_results.json"), 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False, default=str)

    # 保存交易样本
    sample = df.sample(min(200, len(df)), random_state=42)
    sample.to_csv(os.path.join(output_dir, "v4_trades_sample.csv"), index=False, encoding='utf-8-sig')

    print(f"\n结果已保存到 {output_dir}/")
    print("  v4_backtest_results.json - 汇总统计")
    print("  v4_trades_sample.csv - 交易样本")


# ================ 统计工具 ================

def compute_stats(df):
    """计算交易统计"""
    total = len(df)
    wins = df[df['return'] > 0]
    losses = df[df['return'] <= 0]

    win_rate = len(wins) / total * 100 if total > 0 else 0
    avg_ret = df['return'].mean() if total > 0 else 0
    avg_win = wins['return'].mean() if len(wins) > 0 else 0
    avg_loss = losses['return'].mean() if len(losses) > 0 else 0

    total_win = wins['return'].sum() if len(wins) > 0 else 0
    total_loss = abs(losses['return'].sum()) if len(losses) > 0 else 0
    pf = total_win / total_loss if total_loss > 0 else float('inf')

    ev = win_rate / 100 * avg_win + (1 - win_rate / 100) * avg_loss

    return {
        'total': total,
        'win_rate': round(win_rate, 1),
        'avg_ret': round(avg_ret, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'pf': round(pf, 2),
        'ev': round(ev, 2),
    }


def print_summary(df, label):
    """打印统计摘要"""
    stats = compute_stats(df)
    print(f"\n  [{label}]")
    print(f"    交易数: {stats['total']}")
    print(f"    胜率:   {stats['win_rate']:.1f}%")
    print(f"    均收益: {stats['avg_ret']:.2f}%")
    print(f"    均赢:   {stats['avg_win']:.2f}% | 均亏: {stats['avg_loss']:.2f}%")
    print(f"    盈亏比: {stats['pf']:.2f}")
    print(f"    期望值: {stats['ev']:.2f}% (每笔交易预期收益)")


def cohens_d(group1, group2):
    """计算Cohen's d效应量"""
    n1, n2 = len(group1), len(group2)
    if n1 < 2 or n2 < 2:
        return 0
    m1, m2 = np.mean(group1), np.mean(group2)
    v1, v2 = np.var(group1, ddof=1), np.var(group2, ddof=1)
    pooled_std = np.sqrt(((n1-1)*v1 + (n2-1)*v2) / (n1+n2-2))
    if pooled_std == 0:
        return 0
    return (m1 - m2) / pooled_std


def effect_label(d):
    """效应量等级"""
    d = abs(d)
    if d < 0.2: return "微弱"
    if d < 0.5: return "弱"
    if d < 0.8: return "中等"
    return "强"


# ================ 入口 ================

if __name__ == '__main__':
    run_backtest_v4()
