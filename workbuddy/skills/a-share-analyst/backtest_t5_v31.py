"""
T+5 短线策略回测 V3.1 — 从赢家学规律（修复版）
=================================================
核心思路：先找T+5赢家，再学前涨特征，找"近似同步指标"的规律。

修复：
1. pytdx返回K线最新在前 → 排序为最旧在前
2. vol_ratio溢出 → 用amount代替vol计算（更稳定）
3. 增加更多近似同步指标

近似同步指标设计（基于用户指导）：
- 量价关系：放量涨 vs 缩量涨 vs 放量跌
- 均线+量价：MA5上方+量增=强势，MA5下方+量缩=弱势
- 分钟级布林：收窄后突破、接近上轨/下轨
- 量能加速度：量从缩到放的拐点
- 分钟级量价形态：尾盘抢筹、早盘异动
"""

import sys
import json
import time
import argparse
import numpy as np
from pathlib import Path
from datetime import datetime
from collections import defaultdict

from pytdx.hq import TdxHq_API

# ============ 配置 ============
TDX_HOST = '60.191.117.167'
TDX_PORT = 7709
BACKUP_HOST = '120.76.152.87'

WIN_THRESHOLD = 0.09    # T+5涨幅>9%
LOSS_THRESHOLD = -0.05  # T+5跌幅<-5%

DAILY_BARS = 250


class T5PatternLearnerV31:
    """从赢家和输家中学习T+5短线模式 - 修复版"""

    def __init__(self, host=TDX_HOST, port=TDX_PORT):
        self.api = TdxHq_API()
        self.host = host
        self.port = port
        self._connected = False

    def connect(self):
        for h in [self.host, BACKUP_HOST]:
            try:
                if self.api.connect(h, self.port):
                    self.host = h
                    self._connected = True
                    return True
            except:
                continue
        return False

    def disconnect(self):
        if self._connected:
            try:
                self.api.disconnect()
            except:
                pass
            self._connected = False

    def ensure_connection(self):
        if not self._connected:
            return self.connect()
        try:
            test = self.api.get_security_bars(9, 1, '000001', 0, 1)
            if test is not None:
                return True
        except:
            pass
        self.disconnect()
        time.sleep(1)
        return self.connect()

    def get_stock_list(self, max_stocks=300):
        """获取沪深主板+创业板+科创板股票列表"""
        stocks = []
        for market in [1, 0]:
            try:
                count = self.api.get_security_count(market)
                for start in range(0, min(count, max_stocks * 3), 80):
                    if len(stocks) >= max_stocks:
                        break
                    try:
                        data = self.api.get_security_list(market, start)
                        if data is not None:
                            for item in data:
                                code = item.get('code', '')
                                name = item.get('name', '')
                                if 'ST' in name or '退' in name:
                                    continue
                                if code.startswith('4') or code.startswith('8'):
                                    continue
                                # 只取6位代码（排除指数等）
                                if len(code) != 6:
                                    continue
                                stocks.append({'market': market, 'code': code, 'name': name})
                    except:
                        continue
                if len(stocks) >= max_stocks:
                    break
            except:
                continue
        return stocks[:max_stocks]

    def get_daily_bars(self, market, code):
        """获取日K线，按时间升序排列（最旧在前）"""
        try:
            self.ensure_connection()
            data = self.api.get_security_bars(9, market, code, 0, DAILY_BARS)
            if data is not None and len(data) > 0:
                bars = []
                for b in data:
                    bars.append({
                        'date': str(b.get('datetime', ''))[:10],
                        'open': float(b.get('open', 0)),
                        'high': float(b.get('high', 0)),
                        'low': float(b.get('low', 0)),
                        'close': float(b.get('close', 0)),
                        'vol': float(b.get('vol', 0)),
                        'amount': float(b.get('amount', 0)),
                    })
                # pytdx返回最新在前 → 反转为最旧在前
                bars.reverse()
                return bars
        except:
            pass
        return None

    def get_minute_bars(self, market, code, count=240):
        """获取5分钟K线，按时间升序"""
        try:
            self.ensure_connection()
            data = self.api.get_security_bars(0, market, code, 0, count)
            if data is not None and len(data) > 0:
                bars = []
                for b in data:
                    bars.append({
                        'datetime': str(b.get('datetime', '')),
                        'open': float(b.get('open', 0)),
                        'high': float(b.get('high', 0)),
                        'low': float(b.get('low', 0)),
                        'close': float(b.get('close', 0)),
                        'vol': float(b.get('vol', 0)),
                        'amount': float(b.get('amount', 0)),
                    })
                bars.reverse()
                return bars
        except:
            pass
        return None

    def find_t5_trades(self, daily_bars):
        """找出所有T+5赢家和输家交易"""
        if not daily_bars or len(daily_bars) < 30:
            return [], []

        wins = []
        losses = []

        for i in range(20, len(daily_bars) - 5):
            buy_day = daily_bars[i]
            sell_day = daily_bars[i + 5]

            # 买入日不能涨停
            if buy_day['open'] > 0:
                pct = (buy_day['close'] - buy_day['open']) / buy_day['open']
                if pct >= 0.095:
                    continue

            t5_return = (sell_day['close'] - buy_day['close']) / buy_day['close']
            if buy_day['close'] <= 0:
                continue

            t5_high = max(d['high'] for d in daily_bars[i+1:i+6])
            t5_low = min(d['low'] for d in daily_bars[i+1:i+6])
            t5_max_gain = (t5_high - buy_day['close']) / buy_day['close']
            t5_max_dd = (t5_low - buy_day['close']) / buy_day['close']

            trade = {
                'date': buy_day['date'],
                'buy_close': buy_day['close'],
                'sell_close': sell_day['close'],
                'return': t5_return,
                'max_gain': t5_max_gain,
                'max_dd': t5_max_dd,
                'index': i,
            }

            if t5_return >= WIN_THRESHOLD:
                wins.append(trade)
            elif t5_return <= LOSS_THRESHOLD:
                losses.append(trade)

        return wins, losses

    def calc_daily_features(self, daily_bars, buy_idx):
        """
        计算买入日的日级别近似同步指标
        聚焦：量价关系 + 均线+量价 + 布林带 + 量能加速度
        """
        if buy_idx < 20 or buy_idx >= len(daily_bars):
            return None

        # 买入日前20天到买入日
        lookback = daily_bars[max(0, buy_idx-20):buy_idx+1]
        if len(lookback) < 10:
            return None

        closes = np.array([d['close'] for d in lookback])
        opens = np.array([d['open'] for d in lookback])
        highs = np.array([d['high'] for d in lookback])
        lows = np.array([d['low'] for d in lookback])
        amounts = np.array([d['amount'] for d in lookback])  # 用成交额代替成交量（更稳定）
        vols = np.array([d['vol'] for d in lookback])

        f = {}

        # ====== 1. 量价关系（核心）======
        # 今日涨跌幅
        today_ret = (closes[-1] - closes[-2]) / closes[-2] if len(closes) >= 2 else 0
        f['today_ret'] = today_ret

        # 量比 = 今日成交额 / 5日均成交额
        if len(amounts) >= 6:
            amt_ma5 = np.mean(amounts[-6:-1])
            f['amt_ratio'] = amounts[-1] / amt_ma5 if amt_ma5 > 0 else 1.0
        else:
            f['amt_ratio'] = 1.0

        # 量价齐升标记
        f['vol_price_up'] = 1 if today_ret > 0.01 and f['amt_ratio'] > 1.5 else 0

        # 缩量上涨标记（控盘上涨，强信号）
        f['shrink_vol_up'] = 1 if today_ret > 0.01 and f['amt_ratio'] < 0.7 else 0

        # 放量下跌标记（危险信号）
        f['vol_price_down'] = 1 if today_ret < -0.01 and f['amt_ratio'] > 1.5 else 0

        # 缩量下跌标记（洗盘可能）
        f['shrink_vol_down'] = 1 if today_ret < -0.01 and f['amt_ratio'] < 0.7 else 0

        # 量价背离：价格连涨3天但量递减
        if len(closes) >= 4 and len(amounts) >= 4:
            price_up_3 = all(closes[-i] > closes[-i-1] for i in range(1, 4))
            vol_down_3 = all(amounts[-i] < amounts[-i-1] for i in range(1, 4))
            f['price_vol_diverge'] = 1 if price_up_3 and vol_down_3 else 0
        else:
            f['price_vol_diverge'] = 0

        # ====== 2. 均线和量价的关系 ======
        # MA5, MA10, MA20
        ma5 = np.mean(closes[-5:]) if len(closes) >= 5 else closes[-1]
        ma10 = np.mean(closes[-10:]) if len(closes) >= 10 else closes[-1]
        ma20 = np.mean(closes[-20:]) if len(closes) >= 20 else closes[-1]

        # 收盘价相对MA位置
        f['close_vs_ma5'] = (closes[-1] - ma5) / ma5 if ma5 > 0 else 0
        f['close_vs_ma10'] = (closes[-1] - ma10) / ma10 if ma10 > 0 else 0
        f['close_vs_ma20'] = (closes[-1] - ma20) / ma20 if ma20 > 0 else 0

        # 均线多头排列
        f['ma_bull'] = 1 if ma5 > ma10 > ma20 else 0

        # 均线空头排列
        f['ma_bear'] = 1 if ma5 < ma10 < ma20 else 0

        # MA5斜率（3天变化）
        if len(closes) >= 8:
            ma5_3ago = np.mean(closes[-8:-3])
            ma5_now = np.mean(closes[-5:])
            f['ma5_slope'] = (ma5_now - ma5_3ago) / ma5_3ago if ma5_3ago > 0 else 0
        else:
            f['ma5_slope'] = 0

        # 关键组合：站在MA5上方 + 量增 = 近似同步强势信号
        f['above_ma5_vol_up'] = 1 if closes[-1] > ma5 and f['amt_ratio'] > 1.2 else 0

        # 站在MA5上方 + 缩量 = 控盘信号
        f['above_ma5_shrink'] = 1 if closes[-1] > ma5 and f['amt_ratio'] < 0.8 else 0

        # ====== 3. 布林带 ======
        if len(closes) >= 20:
            std20 = np.std(closes[-20:])
            boll_upper = ma20 + 2 * std20
            boll_lower = ma20 - 2 * std20
            boll_width = boll_upper - boll_lower

            # 布林位置百分比
            f['boll_pct'] = (closes[-1] - boll_lower) / boll_width if boll_width > 0 else 0.5

            # 布林带宽度（归一化）
            f['boll_width_norm'] = boll_width / ma20 if ma20 > 0 else 0

            # 布林收窄（带宽低于近期70%分位 = 即将变盘）
            if len(closes) >= 40:
                widths = []
                for j in range(20, len(closes)):
                    seg = closes[j-20:j]
                    w = np.std(seg) * 4
                    widths.append(w)
                if widths:
                    pct70 = np.percentile(widths, 70)
                    f['boll_squeeze'] = 1 if boll_width < pct70 else 0
                else:
                    f['boll_squeeze'] = 0
            else:
                f['boll_squeeze'] = 0

            # 布林突破上轨
            f['boll_breakout_up'] = 1 if closes[-1] > boll_upper else 0

            # 布林触及下轨后回弹
            if len(closes) >= 2:
                f['boll_bounce'] = 1 if closes[-2] < boll_lower and closes[-1] > boll_lower else 0
            else:
                f['boll_bounce'] = 0
        else:
            f['boll_pct'] = 0.5
            f['boll_width_norm'] = 0
            f['boll_squeeze'] = 0
            f['boll_breakout_up'] = 0
            f['boll_bounce'] = 0

        # ====== 4. 量能加速度 ======
        # 量能从缩到放的拐点 = 近3日均量 > 近5日均量 > 近10日均量
        if len(amounts) >= 10:
            amt_ma3 = np.mean(amounts[-3:])
            amt_ma5 = np.mean(amounts[-5:])
            amt_ma10 = np.mean(amounts[-10:])
            f['amt_accel'] = 1 if amt_ma3 > amt_ma5 > amt_ma10 else 0
            f['amt_ma3_vs_ma10'] = amt_ma3 / amt_ma10 if amt_ma10 > 0 else 1
        else:
            f['amt_accel'] = 0
            f['amt_ma3_vs_ma10'] = 1

        # ====== 5. 价格形态 ======
        # 近5日涨跌幅
        if len(closes) >= 6:
            f['ret_5d'] = (closes[-1] - closes[-6]) / closes[-6]
        else:
            f['ret_5d'] = 0

        # 近3日涨跌幅
        if len(closes) >= 4:
            f['ret_3d'] = (closes[-1] - closes[-4]) / closes[-4]
        else:
            f['ret_3d'] = 0

        # 振幅
        if opens[-1] > 0:
            f['amplitude'] = (highs[-1] - lows[-1]) / opens[-1]
        else:
            f['amplitude'] = 0

        # 实体比例（阳线/阴线实体占振幅）
        body = abs(closes[-1] - opens[-1])
        total_range = highs[-1] - lows[-1]
        f['body_ratio'] = body / total_range if total_range > 0 else 0

        # 上影线比例
        upper_shadow = highs[-1] - max(closes[-1], opens[-1])
        f['upper_shadow_pct'] = upper_shadow / total_range if total_range > 0 else 0

        # 下影线比例
        lower_shadow = min(closes[-1], opens[-1]) - lows[-1]
        f['lower_shadow_pct'] = lower_shadow / total_range if total_range > 0 else 0

        # 连续阳线天数
        consecutive_up = 0
        for j in range(len(closes)-1, 0, -1):
            if closes[j] > closes[j-1]:
                consecutive_up += 1
            else:
                break
        f['consec_up'] = consecutive_up

        return f

    def calc_minute_features(self, minute_bars):
        """
        计算分钟级别近似同步指标
        输入: 240根5分钟K线（约5个交易日）
        """
        if not minute_bars or len(minute_bars) < 48:
            return None

        closes = np.array([b['close'] for b in minute_bars])
        opens = np.array([b['open'] for b in minute_bars])
        highs = np.array([b['high'] for b in minute_bars])
        lows = np.array([b['low'] for b in minute_bars])
        amounts = np.array([b['amount'] for b in minute_bars])
        vols = np.array([b['vol'] for b in minute_bars])

        f = {}

        # 取最后一个交易日（最后48根5分钟K线）
        last_day = minute_bars[-48:]
        ld_closes = np.array([b['close'] for b in last_day])
        ld_amounts = np.array([b['amount'] for b in last_day])
        ld_vols = np.array([b['vol'] for b in last_day])
        ld_highs = np.array([b['high'] for b in last_day])
        ld_lows = np.array([b['low'] for b in last_day])

        # ====== 1. 分钟级布林带 ======
        if len(ld_closes) >= 20:
            ld_ma = np.mean(ld_closes[-20:])
            ld_std = np.std(ld_closes[-20:])
            boll_u = ld_ma + 2 * ld_std
            boll_l = ld_ma - 2 * ld_std
            bw = boll_u - boll_l
            f['m_boll_pct'] = (ld_closes[-1] - boll_l) / bw if bw > 0 else 0.5
            f['m_boll_width'] = bw / ld_ma if ld_ma > 0 else 0

            # 布林收窄 + 突破上轨
            if len(closes) >= 100:
                all_widths = []
                for j in range(20, len(closes), 5):
                    seg = closes[j-20:j]
                    w = np.std(seg) * 4
                    all_widths.append(w)
                if all_widths:
                    p30 = np.percentile(all_widths, 30)
                    f['m_boll_squeeze'] = 1 if bw < p30 else 0
                else:
                    f['m_boll_squeeze'] = 0
            else:
                f['m_boll_squeeze'] = 0
        else:
            f['m_boll_pct'] = 0.5
            f['m_boll_width'] = 0
            f['m_boll_squeeze'] = 0

        # ====== 2. 分钟级量价关系 ======
        # 早盘量比（前30分钟=6根 vs 全天均量）
        if len(ld_vols) >= 6:
            morning_amt = np.mean(ld_amounts[:6])
            day_avg_amt = np.mean(ld_amounts)
            f['m_morning_amt_ratio'] = morning_amt / day_avg_amt if day_avg_amt > 0 else 1
        else:
            f['m_morning_amt_ratio'] = 1

        # 尾盘量价（最后30分钟=6根）
        if len(ld_vols) >= 6:
            tail_closes = ld_closes[-6:]
            tail_amt = np.mean(ld_amounts[-6:])
            prev_amt = np.mean(ld_amounts[:-6]) if len(ld_amounts) > 6 else 1
            f['m_tail_amt_ratio'] = tail_amt / prev_amt if prev_amt > 0 else 1
            # 尾盘拉升 = 最后6根K线涨 + 量增
            f['m_tail_rally'] = 1 if tail_closes[-1] > tail_closes[0] and f['m_tail_amt_ratio'] > 1.3 else 0
        else:
            f['m_tail_amt_ratio'] = 1
            f['m_tail_rally'] = 0

        # ====== 3. 分钟级均线趋势 ======
        if len(closes) >= 60:
            m_ma20 = np.mean(closes[-20:])
            m_ma60 = np.mean(closes[-60:])
            f['m_ma20_vs_ma60'] = (m_ma20 - m_ma60) / m_ma60 if m_ma60 > 0 else 0
            # MA20斜率
            if len(closes) >= 21:
                m_ma20_prev = np.mean(closes[-21:-1])
                f['m_ma20_slope'] = (m_ma20 - m_ma20_prev) / m_ma20_prev if m_ma20_prev > 0 else 0
            else:
                f['m_ma20_slope'] = 0
        else:
            f['m_ma20_vs_ma60'] = 0
            f['m_ma20_slope'] = 0

        # ====== 4. 分钟级量能异动 ======
        # 大额K线数（成交额>2倍均量）
        if len(ld_amounts) >= 10:
            amt_mean = np.mean(ld_amounts)
            f['m_big_amt_bars'] = sum(1 for a in ld_amounts if a > amt_mean * 2)
        else:
            f['m_big_amt_bars'] = 0

        # 量能递增（连续5根K线量递增 = 加速信号）
        if len(ld_vols) >= 5:
            f['m_vol_accel'] = 1 if all(ld_vols[-i] > ld_vols[-i-1] for i in range(1, 5)) else 0
        else:
            f['m_vol_accel'] = 0

        # ====== 5. 价格形态 ======
        # 当日涨跌幅
        if len(ld_closes) >= 2 and opens[0] > 0:
            f['m_day_ret'] = (ld_closes[-1] - ld_closes[0]) / ld_closes[0]
        else:
            f['m_day_ret'] = 0

        # V型反转（先跌后涨）
        if len(ld_closes) >= 12:
            half = len(ld_closes) // 2
            first_half_ret = (ld_closes[half] - ld_closes[0]) / ld_closes[0] if ld_closes[0] > 0 else 0
            second_half_ret = (ld_closes[-1] - ld_closes[half]) / ld_closes[half] if ld_closes[half] > 0 else 0
            f['m_v_shape'] = 1 if first_half_ret < -0.005 and second_half_ret > 0.01 else 0
        else:
            f['m_v_shape'] = 0

        return f


def run_analysis(max_stocks=300, output_dir=None):
    """主流程"""

    if output_dir is None:
        output_dir = Path.home() / '.workbuddy' / 'a-share-analyst'
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    learner = T5PatternLearnerV31()
    if not learner.connect():
        print("无法连接通达信服务器!")
        return

    try:
        # Step 1
        print("=" * 70)
        print("Step 1: 获取股票列表...")
        stocks = learner.get_stock_list(max_stocks)
        print(f"  获取 {len(stocks)} 只股票")

        # Step 2
        print("\n" + "=" * 70)
        print("Step 2: 扫描日K线，寻找T+5赢家和输家...")
        all_wins = []
        all_losses = []
        processed = 0
        errors = 0

        for stock in stocks:
            processed += 1
            if processed % 50 == 0:
                print(f"  进度: {processed}/{len(stocks)}, 赢家:{len(all_wins)}, 输家:{len(all_losses)}, 错误:{errors}")

            daily = learner.get_daily_bars(stock['market'], stock['code'])
            if not daily or len(daily) < 30:
                errors += 1
                continue

            # 验证数据有序
            dates = [d['date'] for d in daily]
            if dates != sorted(dates):
                # 数据未排序，手动排序
                daily.sort(key=lambda x: x['date'])

            wins, losses = learner.find_t5_trades(daily)

            for w in wins:
                w['code'] = stock['code']
                w['name'] = stock['name']
                w['market'] = stock['market']
                w['daily_bars'] = daily
                all_wins.append(w)

            for l in losses:
                l['code'] = stock['code']
                l['name'] = stock['name']
                l['market'] = stock['market']
                l['daily_bars'] = daily
                all_losses.append(l)

            time.sleep(0.03)

        print(f"\n  赢家交易: {len(all_wins)} 笔")
        print(f"  输家交易: {len(all_losses)} 笔")

        # Step 3: 计算日级指标
        print("\n" + "=" * 70)
        print("Step 3: 计算日级别近似同步指标...")

        win_daily = []
        loss_daily = []

        for w in all_wins:
            feats = learner.calc_daily_features(w['daily_bars'], w['index'])
            if feats:
                feats['_label'] = 'win'
                feats['_code'] = w['code']
                feats['_date'] = w['date']
                feats['_return'] = w['return']
                win_daily.append(feats)

        for l in all_losses:
            feats = learner.calc_daily_features(l['daily_bars'], l['index'])
            if feats:
                feats['_label'] = 'loss'
                feats['_code'] = l['code']
                feats['_date'] = l['date']
                feats['_return'] = l['return']
                loss_daily.append(feats)

        print(f"  赢家日级指标: {len(win_daily)} 笔")
        print(f"  输家日级指标: {len(loss_daily)} 笔")

        # Step 4: 分钟级指标（取子样本避免超时）
        print("\n" + "=" * 70)
        print("Step 4: 拉取5分钟K线，计算分钟级指标...")

        win_sample = sorted(all_wins, key=lambda x: -x['return'])[:150]
        loss_sample = sorted(all_losses, key=lambda x: x['return'])[:150]

        win_minute = []
        loss_minute = []

        for i, w in enumerate(win_sample):
            if (i + 1) % 50 == 0:
                print(f"  赢家: {i+1}/{len(win_sample)}")
            learner.ensure_connection()
            m = learner.get_minute_bars(w['market'], w['code'], 240)
            if m:
                feats = learner.calc_minute_features(m)
                if feats:
                    feats['_label'] = 'win'
                    feats['_code'] = w['code']
                    feats['_date'] = w['date']
                    feats['_return'] = w['return']
                    win_minute.append(feats)
            time.sleep(0.05)

        for i, l in enumerate(loss_sample):
            if (i + 1) % 50 == 0:
                print(f"  输家: {i+1}/{len(loss_sample)}")
            learner.ensure_connection()
            m = learner.get_minute_bars(l['market'], l['code'], 240)
            if m:
                feats = learner.calc_minute_features(m)
                if feats:
                    feats['_label'] = 'loss'
                    feats['_code'] = l['code']
                    feats['_date'] = l['date']
                    feats['_return'] = l['return']
                    loss_minute.append(feats)
            time.sleep(0.05)

        print(f"  赢家分钟指标: {len(win_minute)} 笔")
        print(f"  输家分钟指标: {len(loss_minute)} 笔")

        # Step 5: 对比分析
        print("\n" + "=" * 70)
        print("Step 5: 赢家 vs 输家 — 近似同步指标对比")
        print("=" * 70)

        def compare_features(win_list, loss_list, prefix=""):
            """对比两组特征，返回有区分力的特征"""
            if not win_list or not loss_list:
                return []

            # 找出共同的特征字段
            sample = win_list[0]
            feature_keys = [k for k in sample.keys() if not k.startswith('_')]

            results = []
            print(f"\n{prefix}{'指标':<28} {'赢家均值':>12} {'输家均值':>12} {'差异':>12} {'Cohen-d':>8}")
            print("-" * 80)

            for key in feature_keys:
                w_vals = [f[key] for f in win_list if key in f and f[key] is not None]
                l_vals = [f[key] for f in loss_list if key in f and f[key] is not None]

                if not w_vals or not l_vals:
                    continue

                w_vals = np.array(w_vals, dtype=float)
                l_vals = np.array(l_vals, dtype=float)

                # 去异常值
                w_vals = w_vals[np.isfinite(w_vals)]
                l_vals = l_vals[np.isfinite(l_vals)]

                if len(w_vals) < 10 or len(l_vals) < 10:
                    continue

                w_mean = np.mean(w_vals)
                l_mean = np.mean(l_vals)
                diff = w_mean - l_mean

                w_std = np.std(w_vals)
                l_std = np.std(l_vals)
                pooled_std = np.sqrt((w_std**2 + l_std**2) / 2)
                cohens_d = abs(diff) / pooled_std if pooled_std > 0 else 0

                direction = "W>L" if diff > 0 else "L>W"
                marker = " **" if cohens_d > 0.5 else (" *" if cohens_d > 0.3 else "")

                print(f"{prefix}{key:<28} {w_mean:>12.4f} {l_mean:>12.4f} {diff:>+12.4f} {cohens_d:>8.3f}{marker}")

                if cohens_d > 0.15:
                    results.append({
                        'indicator': key,
                        'win_mean': float(w_mean),
                        'loss_mean': float(l_mean),
                        'cohens_d': float(cohens_d),
                        'direction': direction,
                    })

            return results

        daily_sig = compare_features(win_daily, loss_daily, prefix="[日级] ")
        minute_sig = compare_features(win_minute, loss_minute, prefix="[分钟] ")

        # Step 6: 显著特征总结
        print("\n" + "=" * 70)
        print("Step 6: 有区分力的特征排序（Cohen's d）")
        print("=" * 70)

        all_sig = daily_sig + minute_sig
        all_sig.sort(key=lambda x: -x['cohens_d'])

        if all_sig:
            print(f"\n共 {len(all_sig)} 个 d>0.15 的特征，按区分力排序:\n")
            for i, sf in enumerate(all_sig, 1):
                bar = '#' * int(sf['cohens_d'] * 20)
                print(f"  {i:2d}. {sf['indicator']:<28} d={sf['cohens_d']:.3f} {sf['direction']:<5} {bar}")
                print(f"      赢家: {sf['win_mean']:.4f}  输家: {sf['loss_mean']:.4f}")

        # Step 7: 简单分类器回测
        print("\n" + "=" * 70)
        print("Step 7: 基于Top特征的分类器回测")
        print("=" * 70)

        # 合并日级+分钟级
        def merge_data(daily_list, minute_list):
            minute_map = {}
            for m in minute_list:
                key = f"{m['_code']}_{m['_date']}"
                minute_map[key] = m

            merged = []
            for d in daily_list:
                key = f"{d['_code']}_{d['_date']}"
                row = dict(d)
                if key in minute_map:
                    for k, v in minute_map[key].items():
                        if not k.startswith('_'):
                            row[f'm_{k}'] = v
                    row['_has_minute'] = True
                else:
                    row['_has_minute'] = False
                merged.append(row)
            return merged

        win_merged = merge_data(win_daily, win_minute)
        loss_merged = merge_data(loss_daily, loss_minute)

        if all_sig:
            # 取top N特征
            top_n = min(8, len(all_sig))
            top_feats = all_sig[:top_n]
            print(f"\n使用 Top {top_n} 特征构建打分模型:")

            all_data = win_merged + loss_merged
            scores = []

            for sample in all_data:
                score = 0
                for sf in top_feats:
                    key = sf['indicator']
                    val = sample.get(key)
                    if val is None:
                        continue
                    try:
                        val = float(val)
                    except:
                        continue
                    if not np.isfinite(val):
                        continue
                    # 按方向打分
                    if sf['direction'] == 'W>L':
                        score += 1 if val > sf['loss_mean'] else 0
                    else:
                        score += 1 if val < sf['loss_mean'] else 0

                scores.append({
                    'code': sample.get('_code', ''),
                    'date': sample.get('_date', ''),
                    'label': sample.get('_label', ''),
                    'return': sample.get('_return', 0),
                    'score': score,
                })

            # 按分数分组
            score_groups = defaultdict(list)
            for s in scores:
                score_groups[s['score']].append(s)

            print(f"\n{'分数':<10} {'交易数':>8} {'赢家':>8} {'胜率':>8} {'平均T+5收益':>12}")
            print("-" * 55)

            for score in sorted(score_groups.keys()):
                group = score_groups[score]
                wins = [g for g in group if g['label'] == 'win']
                win_rate = len(wins) / len(group) * 100 if group else 0
                avg_ret = np.mean([g['return'] for g in group])
                print(f"{score}/{top_n:<8} {len(group):>8} {len(wins):>8} {win_rate:>7.1f}% {avg_ret:>11.2%}")

        # 保存结果
        output_file = output_dir / 't5_pattern_analysis_v31.json'
        save_data = {
            'meta': {
                'version': '3.1',
                'stocks_scanned': len(stocks),
                'win_trades': len(all_wins),
                'loss_trades': len(all_losses),
                'win_daily_features': len(win_daily),
                'loss_daily_features': len(loss_daily),
                'win_minute_features': len(win_minute),
                'loss_minute_features': len(loss_minute),
                'significant_features': all_sig,
            }
        }
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(save_data, f, ensure_ascii=False, indent=2, default=str)

        print(f"\n结果已保存到: {output_file}")

    finally:
        learner.disconnect()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='T+5短线策略回测V3.1 - 从赢家学规律')
    parser.add_argument('--stocks', type=int, default=300, help='扫描股票数量')
    args = parser.parse_args()

    run_analysis(max_stocks=args.stocks)
