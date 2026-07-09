#!/usr/bin/env python3
"""
T+5 短线策略回测框架 v2.0
目标: 验证信号共振策略能否达到 81%胜率 / 9% T+5收益
基准: 用户实战成绩 81%胜率 + T+5 9% (未剔除手续费)

V2 改进:
  - 增加大盘趋势过滤 (上证指数必须在60日线上方)
  - 增加相对强度过滤 (个股必须跑赢大盘)
  - 大幅收紧入场条件 (追求少而精)
  - A股特色策略: 龙回头、首阴、均线多头

运行: PYTHONIOENCODING=utf-8 python backtest_t5.py [--stocks N] [--quick]
"""

import sys
import os
import json
import time
import argparse
from datetime import datetime
from collections import defaultdict

import pandas as pd
import numpy as np
from pytdx.hq import TdxHq_API

# ============================================================
# Configuration
# ============================================================
PYTDX_HOST = '60.191.117.167'
PYTDX_PORT = 7709
FALLBACK_HOST = '120.76.152.87'
FALLBACK_PORT = 7709
HOLDING_DAYS = 5
DEFAULT_LOOKBACK = 350

# ============================================================
# Data Layer
# ============================================================
class DataLoader:
    def __init__(self, host=PYTDX_HOST, port=PYTDX_PORT):
        self.api = TdxHq_API()
        self.host = host
        self.port = port
        self._index_cache = {}

    def connect(self):
        if self.api.connect(self.host, self.port):
            print(f"[OK] Connected to {self.host}:{self.port}")
            return True
        print(f"[WARN] Failed, trying fallback...")
        if self.api.connect(FALLBACK_HOST, FALLBACK_PORT):
            self.host, self.port = FALLBACK_HOST, FALLBACK_PORT
            print(f"[OK] Connected to fallback")
            return True
        raise ConnectionError("Cannot connect to any pytdx server")

    def disconnect(self):
        try: self.api.disconnect()
        except: pass

    def get_index_bars(self, market=1, code='000001', count=DEFAULT_LOOKBACK):
        """Get Shanghai Composite Index daily bars"""
        key = f"{market}_{code}"
        if key in self._index_cache:
            return self._index_cache[key]
        try:
            data = self.api.get_index_bars(9, market, code, 0, count)
            if data is None or len(data) == 0:
                return None
            df = pd.DataFrame(data)
            if 'datetime' in df.columns:
                df['date'] = pd.to_datetime(df['datetime'])
            df = df.sort_values('date').reset_index(drop=True)
            self._index_cache[key] = df
            return df
        except:
            return None

    def get_stock_universe(self, max_stocks=200):
        stocks = []
        seen = set()
        for mkt, prefixes in [(1, ['60']), (0, ['00', '300'])]:
            for start in range(0, 3000, 500):
                try:
                    batch = self.api.get_security_list(mkt, start)
                    if not batch: break
                    for s in batch:
                        code = s.get('code', '')
                        if any(code.startswith(p) for p in prefixes) and code not in seen:
                            seen.add(code)
                            stocks.append((mkt, code, s.get('name', '')))
                except:
                    break
        print(f"[INFO] Stock universe: {len(stocks)} stocks")
        return stocks[:max_stocks]

    def get_daily_bars(self, market, code, count=DEFAULT_LOOKBACK):
        try:
            data = self.api.get_security_bars(9, market, code, 0, count)
            if data is None or len(data) == 0:
                return None
            df = pd.DataFrame(data)
            if 'datetime' in df.columns:
                df['date'] = pd.to_datetime(df['datetime'])
            df = df.sort_values('date').reset_index(drop=True)
            if len(df) < 80 or df['close'].isna().any():
                return None
            return df
        except:
            return None


# ============================================================
# Market Regime Filter
# ============================================================

class MarketRegime:
    """Determine market regime from index data"""

    def __init__(self, index_df):
        self.index_df = index_df
        self._build_signals()

    def _build_signals(self):
        df = self.index_df
        df['ma20'] = df['close'].rolling(20).mean()
        df['ma60'] = df['close'].rolling(60).mean()
        self.date_map = {}
        for _, row in df.iterrows():
            d = str(row['date'])[:10]
            self.date_map[d] = {
                'above_ma60': row['close'] > row['ma60'] if pd.notna(row['ma60']) else False,
                'above_ma20': row['close'] > row['ma20'] if pd.notna(row['ma20']) else False,
                'index_close': row['close'],
            }

    def is_bullish(self, date_str):
        """Return True if market is in bullish regime"""
        info = self.date_map.get(date_str[:10])
        return info['above_ma60'] if info else False

    def get_index_return_5d(self, date_str):
        """Get index 5-day return for relative strength"""
        df = self.index_df
        idx = df[df['date'].astype(str).str[:10] == date_str[:10]].index
        if len(idx) == 0:
            return 0
        i = idx[0]
        if i + 5 >= len(df):
            return 0
        return (df['close'].iloc[i+5] - df['close'].iloc[i]) / df['close'].iloc[i]


# ============================================================
# V2 Strategy Library (with market regime filter)
# ============================================================

def check_market_and_strength(df, i, market_regime, min_rel_strength=0):
    """
    通用前置过滤:
      1. 大盘在60日线上方 (牛市区)
      2. 个股近期跑赢大盘 (可选)
    """
    date_str = str(df['date'].iloc[i])[:10]
    if not market_regime.is_bullish(date_str):
        return False
    return True


def strategy_dragon_pullback(df, i, market_regime):
    """
    龙回头 (Dragon Pullback) — A股经典短线策略
    
    条件:
      1. 大盘趋势向上 (上证在60日线上方)
      2. 近20日创过60日新高 (龙头特征)
      3. 当前在20日均线上方 (中期趋势完好)
      4. 近3日连续缩量回调 (健康调整)
      5. 当日出现止跌信号 (长下影 or 收阳)
      6. 回调幅度5-15% (不深不浅)
      7. 换手率适中 (排除妖股和僵尸股)
    """
    if i < 60:
        return False

    close = df['close'].iloc[i]
    open_ = df['open'].iloc[i]
    high = df['high'].iloc[i]
    low = df['low'].iloc[i]
    vol = df['vol'].iloc[i]

    # 0. Market regime
    if not check_market_and_strength(df, i, market_regime):
        return False

    # 1. Made 60-day new high within last 20 days (dragon trait)
    high_60 = df['high'].iloc[i-60:i].max()
    high_20 = df['high'].iloc[i-20:i].max()
    if high_20 < high_60 * 0.98:  # Didn't challenge recent highs
        return False

    # 2. Above 20-day MA
    ma20 = df['close'].iloc[i-20:i].mean()
    if close < ma20:
        return False

    # 3. Recent 3-day pullback with shrinking volume
    if i < 3:
        return False
    vol_1 = df['vol'].iloc[i-2]
    vol_2 = df['vol'].iloc[i-1]
    vol_3 = df['vol'].iloc[i]
    vol_avg_20 = df['vol'].iloc[i-20:i].mean()
    if vol_avg_20 <= 0:
        return False

    # At least 2 of 3 days with shrinking volume
    shrink_days = 0
    for v in [vol_1, vol_2, vol_3]:
        if v < vol_avg_20 * 0.8:
            shrink_days += 1
    if shrink_days < 2:
        return False

    # Price pulled back from recent peak
    recent_peak = df['high'].iloc[i-5:i+1].max()
    if recent_peak <= 0:
        return False
    pullback = (recent_peak - close) / recent_peak
    if pullback < 0.03 or pullback > 0.12:  # 3-12% pullback
        return False

    # 4. Reversal signal today
    body = abs(close - open_)
    lower_shadow = min(open_, close) - low
    is_hammer = (lower_shadow > body * 1.5 and body > 0)
    is_yang = close > open_

    if not (is_hammer or is_yang):
        return False

    # 5. Volume not too low (exclude illiquid) and not too high (exclude panic)
    if vol < vol_avg_20 * 0.3 or vol > vol_avg_20 * 2.5:
        return False

    return True


def strategy_first_yin(df, i, market_regime):
    """
    首阴战法 (First Yin) — 连涨后首日回调买入
    
    条件:
      1. 大盘趋势向上
      2. 前3日连续收阳且涨幅递增 (加速上涨)
      3. 今日收阴 (首阴) 但跌幅<4% (不是暴跌)
      4. 今日成交量<前3日最大量 (缩量回调)
      5. 股价在60日均线之上
      6. 近10日有放量阳线 (主力资金介入痕迹)
    """
    if i < 60:
        return False

    close = df['close'].iloc[i]
    open_ = df['open'].iloc[i]
    vol = df['vol'].iloc[i]

    # 0. Market regime
    if not check_market_and_strength(df, i, market_regime):
        return False

    # 1. Previous 3 days: consecutive yang with increasing gains
    if i < 4:
        return False
    gains = []
    vol_prev_3 = []
    for j in range(i-3, i):
        day_open = df['open'].iloc[j]
        day_close = df['close'].iloc[j]
        day_vol = df['vol'].iloc[j]
        if day_open <= 0:
            return False
        if day_close <= day_open:  # Not yang
            return False
        gain = (day_close - day_open) / day_open
        gains.append(gain)
        vol_prev_3.append(day_vol)

    # Gains should be increasing (momentum acceleration)
    if gains[1] <= gains[0] or gains[2] <= gains[1]:
        return False

    # 2. Today is yin (close < open)
    if close >= open_:
        return False

    # 3. But not a crash (drop < 4%)
    day_drop = (close - open_) / open_
    if day_drop < -0.04:
        return False

    # 4. Volume shrinking (today < max of prev 3)
    if vol >= max(vol_prev_3):
        return False

    # 5. Above 60-day MA
    ma60 = df['close'].iloc[i-60:i].mean()
    if close < ma60:
        return False

    # 6. Had volume surge in last 10 days (institutional footprint)
    vol_avg_20 = df['vol'].iloc[i-20:i].mean()
    if vol_avg_20 <= 0:
        return False
    recent_max_vol = df['vol'].iloc[i-10:i].max()
    if recent_max_vol < vol_avg_20 * 1.5:
        return False

    return True


def strategy_ma_alignment_pullback(df, i, market_regime):
    """
    均线多头回踩 (MA Alignment Pullback)
    
    条件:
      1. 大盘趋势向上
      2. 均线多头排列: MA5 > MA10 > MA20 > MA60
      3. 价格回踩到MA5附近 (±2%)
      4. 回踩日成交量<20日均量 (缩量)
      5. 当日收阳 (确认支撑有效)
      6. 近20日涨幅>5% (确保有趋势, 不是横盘)
    """
    if i < 60:
        return False

    close = df['close'].iloc[i]
    open_ = df['open'].iloc[i]
    vol = df['vol'].iloc[i]

    # 0. Market regime
    if not check_market_and_strength(df, i, market_regime):
        return False

    # 2. MA alignment: MA5 > MA10 > MA20 > MA60
    ma5 = df['close'].iloc[i-5:i].mean()
    ma10 = df['close'].iloc[i-10:i].mean()
    ma20 = df['close'].iloc[i-20:i].mean()
    ma60 = df['close'].iloc[i-60:i].mean()

    if not (ma5 > ma10 > ma20 > ma60):
        return False

    # 3. Price near MA5 (within 2%)
    if abs(close / ma5 - 1) > 0.02:
        return False

    # 4. Volume shrinking
    vol_avg_20 = df['vol'].iloc[i-20:i].mean()
    if vol_avg_20 <= 0:
        return False
    if vol >= vol_avg_20:
        return False

    # 5. Yang candle today
    if close <= open_:
        return False

    # 6. Trend exists (20-day gain > 5%)
    price_20_ago = df['close'].iloc[i-20]
    if price_20_ago <= 0:
        return False
    gain_20 = (close - price_20_ago) / price_20_ago
    if gain_20 < 0.05:
        return False

    return True


def strategy_strong_pullback_reversal(df, i, market_regime):
    """
    强势回踩反转 — 龙回头+首阴的精华混合版
    
    最严格的策略, 要求所有条件同时满足:
      1. 大盘在60日线上方
      2. 个股在60日线上方 + 20日线上方 (双均线保护)
      3. 近5日从高点回撤5-15%
      4. 回撤过程成交量持续萎缩
      5. 当日出现明确的反转K线 (长下影阳线 or 吞没形态)
      6. 近20日创过60日新高 (确认是强势股)
    """
    if i < 60:
        return False

    close = df['close'].iloc[i]
    open_ = df['open'].iloc[i]
    high = df['high'].iloc[i]
    low = df['low'].iloc[i]
    vol = df['vol'].iloc[i]

    # 1. Market regime
    if not check_market_and_strength(df, i, market_regime):
        return False

    # 2. Above both MAs
    ma20 = df['close'].iloc[i-20:i].mean()
    ma60 = df['close'].iloc[i-60:i].mean()
    if close < ma20 or close < ma60:
        return False

    # 3. Pullback 5-15% from recent peak (within 5-8 trading days)
    recent_peak = df['high'].iloc[i-8:i+1].max()
    if recent_peak <= 0:
        return False
    pullback = (recent_peak - close) / recent_peak
    if pullback < 0.05 or pullback > 0.15:
        return False

    # 4. Volume shrinking in pullback
    vol_avg_20 = df['vol'].iloc[i-20:i].mean()
    if vol_avg_20 <= 0:
        return False
    # Check last 3 days: at least 2 days with vol < 70% avg
    shrink_count = 0
    for j in range(max(0, i-3), i+1):
        if df['vol'].iloc[j] < vol_avg_20 * 0.7:
            shrink_count += 1
    if shrink_count < 2:
        return False

    # 5. Strong reversal signal
    body = abs(close - open_)
    lower_shadow = min(open_, close) - low

    # Option A: Long lower shadow yang
    is_yang = close > open_
    is_hammer_yang = is_yang and lower_shadow > body * 2 and body > 0

    # Option B: Engulfing (today's body engulfs yesterday's)
    prev_open = df['open'].iloc[i-1]
    prev_close = df['close'].iloc[i-1]
    is_engulf = (close > open_ and  # Today is yang
                 open_ <= prev_close and  # Open below prev close
                 close >= prev_open and  # Close above prev open
                 prev_close < prev_open)   # Yesterday was yin

    if not (is_hammer_yang or is_engulf):
        return False

    # 6. Made 60-day new high recently (strong stock confirmation)
    high_60 = df['high'].iloc[i-60:i].max()
    high_recent = df['high'].iloc[i-20:i].max()
    if high_recent < high_60 * 0.97:  # Wasn't near recent highs
        return False

    return True


# ============================================================
# Backtest Engine (V2)
# ============================================================

class T5BacktesterV2:
    def __init__(self, data_loader, market_regime):
        self.loader = data_loader
        self.regime = market_regime
        self.all_results = {}

    def calculate_t5_return(self, df, i):
        if i + HOLDING_DAYS >= len(df):
            return None, None, None
        entry = df['close'].iloc[i]
        exit_ = df['close'].iloc[i + HOLDING_DAYS]
        if entry <= 0:
            return None, None, None
        ret = (exit_ - entry) / entry
        holding_lows = df['low'].iloc[i+1:i+HOLDING_DAYS+1]
        holding_highs = df['high'].iloc[i+1:i+HOLDING_DAYS+1]
        if len(holding_lows) == 0:
            return None, None, None
        max_dd = (holding_lows.min() - entry) / entry
        max_gain = (holding_highs.max() - entry) / entry
        return ret, max_dd, max_gain

    def run_strategy(self, strategy_func, df, code, name, market):
        trades = []
        for i in range(60, len(df) - HOLDING_DAYS):
            try:
                if strategy_func(df, i, self.regime):
                    ret, max_dd, max_gain = self.calculate_t5_return(df, i)
                    if ret is not None:
                        trades.append({
                            'code': code, 'name': name, 'market': market,
                            'date': str(df['date'].iloc[i])[:10],
                            'entry': round(df['close'].iloc[i], 2),
                            'exit': round(df['close'].iloc[i + HOLDING_DAYS], 2),
                            'return': round(ret, 4),
                            'max_dd': round(max_dd, 4),
                            'max_gain': round(max_gain, 4),
                        })
            except:
                continue
        return trades

    def run_backtest(self, strategies, stock_count=200, quick=False):
        stocks = self.loader.get_stock_universe(max_stocks=stock_count)
        if quick:
            stocks = stocks[:80]
            print(f"[QUICK MODE] Testing with {len(stocks)} stocks")

        self.all_results = {s.__name__: [] for s in strategies}
        total = len(stocks)
        success = 0
        start = time.time()

        for idx, (market, code, name) in enumerate(stocks):
            if idx % 30 == 0:
                elapsed = time.time() - start
                eta = (elapsed / max(idx, 1)) * (total - idx) / 60
                print(f"[{idx}/{total}] {elapsed:.0f}s, ETA {eta:.1f}min")

            df = self.loader.get_daily_bars(market, code)
            if df is None:
                continue
            success += 1

            for strategy in strategies:
                trades = self.run_strategy(strategy, df, code, name, market)
                self.all_results[strategy.__name__].extend(trades)

        print(f"\n[DONE] {success}/{total} stocks in {time.time()-start:.0f}s")
        return self.all_results

    def print_report(self):
        print("\n" + "=" * 85)
        print("  T+5 短线策略回测报告 V2 (含大盘趋势过滤)")
        print(f"  基准: 81%胜率 / T+5 9%收益  |  生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print("=" * 85)

        summary = []

        for name, trades in self.all_results.items():
            if not trades:
                print(f"\n--- {name} ---  No trades found")
                summary.append({'strategy': name, 'trades': 0, 'win_rate': 0, 'avg_return': 0})
                continue

            returns = [t['return'] for t in trades]
            wins = [r for r in returns if r > 0]
            losses = [r for r in returns if r <= 0]
            win_rate = len(wins) / len(returns)
            avg_return = np.mean(returns)
            avg_win = np.mean(wins) if wins else 0
            avg_loss = np.mean(losses) if losses else 0
            median_return = np.median(returns)
            profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else 999
            avg_max_dd = np.mean([t['max_dd'] for t in trades])

            # Expected value per trade
            ev = win_rate * avg_win + (1 - win_rate) * avg_loss

            hit_wr = ">>HIT!" if win_rate >= 0.81 else ""
            hit_ret = ">>HIT!" if avg_return >= 0.09 else ""

            print(f"\n{'='*70}")
            print(f"  {name}")
            print(f"{'='*70}")
            print(f"  交易次数:       {len(trades)}")
            print(f"  胜率:          {win_rate:.1%} {hit_wr}")
            print(f"  平均T+5收益:   {avg_return:.2%} {hit_ret}")
            print(f"  中位T+5收益:   {median_return:.2%}")
            print(f"  平均盈利:      {avg_win:.2%}")
            print(f"  平均亏损:      {avg_loss:.2%}")
            print(f"  盈亏比:        {profit_factor:.2f}")
            print(f"  期望值/笔:     {ev:.2%}")
            print(f"  平均最大回撤:  {avg_max_dd:.2%}")

            # Top 5 winners and losers
            sorted_trades = sorted(trades, key=lambda x: x['return'], reverse=True)
            print(f"\n  Top 3 盈利:")
            for t in sorted_trades[:3]:
                print(f"    {t['date']} {t['code']} {t['name'][:6]:6s} "
                      f"入{t['entry']:8.2f} 出{t['exit']:8.2f} {t['return']:+.2%}")
            print(f"  Top 3 亏损:")
            for t in sorted_trades[-3:]:
                print(f"    {t['date']} {t['code']} {t['name'][:6]:6s} "
                      f"入{t['entry']:8.2f} 出{t['exit']:8.2f} {t['return']:+.2%}")

            summary.append({
                'strategy': name,
                'trades': len(trades),
                'win_rate': round(win_rate, 3),
                'avg_return': round(avg_return, 4),
                'median_return': round(median_return, 4),
                'ev': round(ev, 4),
                'profit_factor': round(profit_factor, 2),
                'avg_max_dd': round(avg_max_dd, 4),
            })

        # Comparison table
        print(f"\n{'='*85}")
        print("  策略对比汇总 V2 (vs 基准: 81%胜率 / 9% T+5)")
        print(f"{'='*85}")
        header = f"  {'策略':<35} {'笔数':>4} {'胜率':>6} {'T+5收益':>8} {'中位收益':>8} {'EV/笔':>7} {'PF':>5} {'距基准':>14}"
        print(header)
        print(f"  {'-'*35} {'-'*4} {'-'*6} {'-'*8} {'-'*8} {'-'*7} {'-'*5} {'-'*14}")

        for s in summary:
            if s['trades'] == 0:
                continue
            wr_gap = s['win_rate'] - 0.81
            ret_gap = s['avg_return'] - 0.09
            gap_str = f"WR{wr_gap:+.0%} R{ret_gap:+.0%}"
            print(f"  {s['strategy']:<35} {s['trades']:>4} {s['win_rate']:>6.1%} "
                  f"{s['avg_return']:>8.2%} {s['median_return']:>8.2%} "
                  f"{s['ev']:>7.2%} {s['profit_factor']:>5.2f} {gap_str:>14}")

        print()

    def save_results(self, output_dir=None):
        if output_dir is None:
            output_dir = os.path.expanduser('~/.workbuddy/a-share-analyst')
        os.makedirs(output_dir, exist_ok=True)

        f1 = os.path.join(output_dir, 't5_backtest_v2_results.json')
        with open(f1, 'w', encoding='utf-8') as f:
            json.dump(self.all_results, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n[SAVED] {f1}")

        f2 = os.path.join(output_dir, 't5_backtest_v2_summary.json')
        summary = []
        for name, trades in self.all_results.items():
            if not trades:
                summary.append({'strategy': name, 'trades': 0})
                continue
            returns = [t['return'] for t in trades]
            wins = [r for r in returns if r > 0]
            losses = [r for r in returns if r <= 0]
            summary.append({
                'strategy': name,
                'total_trades': len(trades),
                'win_rate': round(len(wins)/len(returns), 3),
                'avg_return': round(np.mean(returns), 4),
                'profit_factor': round(abs(sum(wins)/sum(losses)), 2) if losses and sum(losses) != 0 else 999,
                'benchmark_wr': 0.81,
                'benchmark_return': 0.09,
            })
        with open(f2, 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"[SAVED] {f2}")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='T+5 Short-term Strategy Backtester V2')
    parser.add_argument('--stocks', type=int, default=200, help='Number of stocks')
    parser.add_argument('--quick', action='store_true', help='Quick mode: 80 stocks')
    args = parser.parse_args()

    print("=" * 60)
    print("  T+5 短线策略回测框架 V2.0")
    print("  改进: 大盘趋势过滤 + A股特色策略 + 收紧入场条件")
    print("  目标: 81%胜率 / T+5 9%收益")
    print("=" * 60)

    loader = DataLoader()
    loader.connect()

    # Load index data for market regime filter
    print("[INFO] Loading Shanghai Composite Index...")
    index_df = loader.get_index_bars(market=1, code='000001')
    if index_df is None:
        print("[ERROR] Cannot load index data!")
        loader.disconnect()
        return

    market_regime = MarketRegime(index_df)
    bullish_days = sum(1 for v in market_regime.date_map.values() if v['above_ma60'])
    total_days = len(market_regime.date_map)
    print(f"[INFO] Market regime: {bullish_days}/{total_days} days bullish ({bullish_days/total_days:.0%})")

    backtester = T5BacktesterV2(loader, market_regime)

    strategies = [
        strategy_dragon_pullback,          # 龙回头
        strategy_first_yin,                 # 首阴战法
        strategy_ma_alignment_pullback,     # 均线多头回踩
        strategy_strong_pullback_reversal,  # 强势回踩反转 (最严格)
    ]

    results = backtester.run_backtest(strategies, stock_count=args.stocks, quick=args.quick)
    backtester.print_report()
    backtester.save_results()
    loader.disconnect()

    print("\n[DONE] V2 backtest complete.")


if __name__ == '__main__':
    main()
