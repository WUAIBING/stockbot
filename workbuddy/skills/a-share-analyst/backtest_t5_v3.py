"""
T+5 短线策略回测 V3 — 从赢家学规律
=======================================
思路转变：不是"先定策略再测胜率"，而是"先找T+5赢家，再学前涨特征"。

核心步骤：
1. 拉沪深300成分股的日K线（250个交易日）
2. 找出所有T+5涨幅>9%的"赢家交易"
3. 找出所有T+5跌幅>5%的"输家交易"（对照组）
4. 对赢家和输家，拉涨/跌之前的分钟级K线（5分钟）
5. 计算近似同步指标：量价关系、布林带、均线斜率、量比等
6. 对比赢家vs输家的指标差异，找到"涨停前指纹"
7. 用指纹条件回测胜率

输出：
- 赢家vs输家的指标统计对比
- 基于指纹的分类器回测结果
"""

import sys
import json
import time
import argparse
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

# pytdx
from pytdx.hq import TdxHq_API

# ============ 配置 ============
TDX_HOST = '60.191.117.167'
TDX_PORT = 7709
BACKUP_HOST = '120.76.152.87'

# T+5 参数
WIN_THRESHOLD = 0.09   # T+5涨幅>9% → 赢家
LOSS_THRESHOLD = -0.05  # T+5跌幅<-5% → 输家

# 分析窗口
PRE_DAYS = 5           # 涨之前看5个交易日
MINUTE_BARS = 48        # 每天48根5分钟K线（4小时交易），5天=240根
DAILY_BARS = 250        # 日K线拉250根


class T5PatternLearner:
    """从赢家和输家中学习T+5短线模式"""

    def __init__(self, host=TDX_HOST, port=TDX_PORT):
        self.api = TdxHq_API()
        self.host = host
        self.port = port
        self._connected = False

    def connect(self):
        """连接通达信服务器"""
        try:
            if self.api.connect(self.host, self.port):
                self._connected = True
                return True
        except Exception as e:
            print(f"  连接 {self.host} 失败: {e}")

        # 备用服务器
        try:
            if self.api.connect(BACKUP_HOST, self.port):
                self.host = BACKUP_HOST
                self._connected = True
                return True
        except Exception as e:
            print(f"  备用连接也失败: {e}")
        return False

    def disconnect(self):
        if self._connected:
            try:
                self.api.disconnect()
            except:
                pass
            self._connected = False

    def ensure_connection(self):
        """保持连接，断开重连"""
        if not self._connected:
            return self.connect()
        # 试试发个请求看是否还活着
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
        """获取沪深300成分股列表"""
        stocks = []
        # 沪深300代码 - 取常见的300只
        # 先拉沪深两市成分股
        for market in [1, 0]:  # 1=沪, 0=深
            try:
                count = self.api.get_security_count(market)
                # 取前N只
                batch_size = 100
                for start in range(0, min(count, max_stocks * 2), batch_size):
                    if len(stocks) >= max_stocks:
                        break
                    try:
                        data = self.api.get_security_list(market, start)
                        if data is not None and len(data) > 0:
                            for item in data:
                                code = item.get('code', '')
                                name = item.get('name', '')
                                # 过滤ST、退市、北交所
                                if 'ST' in name or '退' in name:
                                    continue
                                if code.startswith('4') or code.startswith('8'):
                                    continue  # 北交所/老三板
                                # 主板+创业板+科创板
                                stocks.append({
                                    'market': market,
                                    'code': code,
                                    'name': name
                                })
                    except:
                        continue
                if len(stocks) >= max_stocks:
                    break
            except:
                continue
        return stocks[:max_stocks]

    def get_daily_bars(self, market, code, count=DAILY_BARS):
        """获取日K线"""
        try:
            self.ensure_connection()
            data = self.api.get_security_bars(9, market, code, 0, count)
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
                return bars
        except Exception as e:
            pass
        return None

    def get_minute_bars(self, market, code, count=240):
        """获取5分钟K线（5天 * 48根/天 = 240根）"""
        try:
            self.ensure_connection()
            # 5分钟K线 = category 0
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
                return bars
        except Exception as e:
            pass
        return None

    def find_t5_trades(self, daily_bars):
        """
        从日K线中找出所有T+5赢家和输家交易
        返回: wins, losses 列表，每个包含买入日及前后数据
        """
        if not daily_bars or len(daily_bars) < 30:
            return [], []

        wins = []
        losses = []

        # 涨停日不能买入（买不到），我们找的是"非涨停日买入，T+5大涨"的交易
        for i in range(20, len(daily_bars) - 6):
            buy_day = daily_bars[i]
            sell_day = daily_bars[i + 5]  # T+5

            # 买入日不能是涨停（买不到）
            pct_buy_day = (buy_day['close'] - buy_day['open']) / buy_day['open'] * 100 if buy_day['open'] > 0 else 0
            if pct_buy_day >= 9.5:  # 涨停日跳过
                continue

            # T+5收益
            t5_return = (sell_day['close'] - buy_day['close']) / buy_day['close']
            t5_high = max(d['high'] for d in daily_bars[i+1:i+6])  # T+1到T+5最高价
            t5_low = min(d['low'] for d in daily_bars[i+1:i+6])    # T+1到T+5最低价
            t5_max_gain = (t5_high - buy_day['close']) / buy_day['close']
            t5_max_dd = (t5_low - buy_day['close']) / buy_day['close']

            trade = {
                'date': buy_day['date'],
                'buy_close': buy_day['close'],
                'sell_close': sell_day['close'],
                'return': t5_return,
                'max_gain': t5_max_gain,
                'max_dd': t5_max_dd,
                'index_in_bars': i,  # 在daily_bars中的位置
            }

            if t5_return >= WIN_THRESHOLD:
                wins.append(trade)
            elif t5_return <= LOSS_THRESHOLD:
                losses.append(trade)

        return wins, losses

    def calc_daily_indicators(self, daily_bars, buy_index):
        """
        计算买入日的日级别近似同步指标
        buy_index: 买入日在daily_bars中的位置
        """
        if buy_index < 20:
            return None

        # 取买入日前20天的数据
        recent = daily_bars[max(0, buy_index-20):buy_index+1]
        if len(recent) < 10:
            return None

        closes = [d['close'] for d in recent]
        volumes = [d['vol'] for d in recent]
        highs = [d['high'] for d in recent]
        lows = [d['low'] for d in recent]
        opens = [d['open'] for d in recent]

        indicators = {}

        # === 1. 量价关系 ===
        # 量比 = 今日成交量 / 过去5日平均成交量
        if len(volumes) >= 6:
            vol_ma5 = np.mean(volumes[-6:-1])
            indicators['vol_ratio'] = volumes[-1] / vol_ma5 if vol_ma5 > 0 else 0
        else:
            indicators['vol_ratio'] = 0

        # 量价齐升 = 今天涨+量比>1.5
        today_return = (closes[-1] - closes[-2]) / closes[-2] if closes[-2] > 0 else 0
        indicators['today_return'] = today_return
        indicators['vol_price_up'] = 1 if today_return > 0 and indicators['vol_ratio'] > 1.5 else 0

        # 缩量上涨 = 今天涨+量比<0.7
        indicators['shrink_vol_up'] = 1 if today_return > 0 and indicators['vol_ratio'] < 0.7 else 0

        # 放量下跌
        indicators['vol_price_down'] = 1 if today_return < 0 and indicators['vol_ratio'] > 1.5 else 0

        # === 2. 均线和量价的关系 ===
        # MA5, MA10, MA20
        if len(closes) >= 5:
            ma5 = np.mean(closes[-5:])
            indicators['close_vs_ma5'] = (closes[-1] - ma5) / ma5 if ma5 > 0 else 0
        else:
            indicators['close_vs_ma5'] = 0

        if len(closes) >= 10:
            ma10 = np.mean(closes[-10:])
            indicators['close_vs_ma10'] = (closes[-1] - ma10) / ma10 if ma10 > 0 else 0
        else:
            indicators['close_vs_ma10'] = 0

        if len(closes) >= 20:
            ma20 = np.mean(closes[-20:])
            indicators['close_vs_ma20'] = (closes[-1] - ma20) / ma20 if ma20 > 0 else 0
        else:
            indicators['close_vs_ma20'] = 0

        # 均线多头排列 = MA5 > MA10 > MA20
        if len(closes) >= 20:
            ma5 = np.mean(closes[-5:])
            ma10 = np.mean(closes[-10:])
            ma20 = np.mean(closes[-20:])
            indicators['ma_bullish'] = 1 if ma5 > ma10 > ma20 else 0
            indicators['ma5_slope'] = (ma5 - np.mean(closes[-6:-1])) / np.mean(closes[-6:-1]) if np.mean(closes[-6:-1]) > 0 else 0
        else:
            indicators['ma_bullish'] = 0
            indicators['ma5_slope'] = 0

        # === 3. 布林带（日级别）===
        if len(closes) >= 20:
            ma20 = np.mean(closes[-20:])
            std20 = np.std(closes[-20:])
            indicators['boll_upper'] = ma20 + 2 * std20
            indicators['boll_lower'] = ma20 - 2 * std20
            indicators['boll_pct'] = (closes[-1] - indicators['boll_lower']) / (indicators['boll_upper'] - indicators['boll_lower']) if (indicators['boll_upper'] - indicators['boll_lower']) > 0 else 0.5
            indicators['boll_width'] = (indicators['boll_upper'] - indicators['boll_lower']) / ma20 if ma20 > 0 else 0
        else:
            indicators['boll_pct'] = 0.5
            indicators['boll_width'] = 0

        # === 4. 价格形态 ===
        # 近5日涨跌幅
        if len(closes) >= 6:
            indicators['ret_5d'] = (closes[-1] - closes[-6]) / closes[-6]
        else:
            indicators['ret_5d'] = 0

        # 近3日涨跌幅
        if len(closes) >= 4:
            indicators['ret_3d'] = (closes[-1] - closes[-4]) / closes[-4]
        else:
            indicators['ret_3d'] = 0

        # 今日振幅
        if opens[-1] > 0:
            indicators['today_amplitude'] = (highs[-1] - lows[-1]) / opens[-1]
        else:
            indicators['today_amplitude'] = 0

        # 今日上影线比例
        body_top = max(closes[-1], opens[-1])
        if highs[-1] > body_top:
            indicators['upper_shadow'] = (highs[-1] - body_top) / (highs[-1] - lows[-1]) if (highs[-1] - lows[-1]) > 0 else 0
        else:
            indicators['upper_shadow'] = 0

        # 下影线比例
        body_bottom = min(closes[-1], opens[-1])
        if body_bottom > lows[-1]:
            indicators['lower_shadow'] = (body_bottom - lows[-1]) / (highs[-1] - lows[-1]) if (highs[-1] - lows[-1]) > 0 else 0
        else:
            indicators['lower_shadow'] = 0

        # === 5. 量能趋势 ===
        # 近5日量能是否递增
        if len(volumes) >= 6:
            vol_increasing = 1
            for vi in range(-5, -1):
                if volumes[vi] > volumes[vi+1]:
                    vol_increasing = 0
                    break
            indicators['vol_increasing_5d'] = vol_increasing
        else:
            indicators['vol_increasing_5d'] = 0

        # 近3日平均量 vs 近10日平均量
        if len(volumes) >= 10:
            vol_ma3 = np.mean(volumes[-3:])
            vol_ma10 = np.mean(volumes[-10:])
            indicators['vol_ma3_vs_ma10'] = vol_ma3 / vol_ma10 if vol_ma10 > 0 else 1
        else:
            indicators['vol_ma3_vs_ma10'] = 1

        return indicators

    def calc_minute_indicators(self, minute_bars):
        """
        计算分钟级别的近似同步指标
        输入: 最近240根5分钟K线（5个交易日）
        """
        if not minute_bars or len(minute_bars) < 48:
            return None

        indicators = {}
        closes = [b['close'] for b in minute_bars]
        volumes = [b['vol'] for b in minute_bars]
        highs = [b['high'] for b in minute_bars]
        lows = [b['low'] for b in minute_bars]

        # === 1. 分钟级布林带 ===
        # 最后一个交易日的5分钟布林（48根）
        last_day_bars = minute_bars[-48:]
        last_closes = [b['close'] for b in last_day_bars]

        if len(last_closes) >= 20:
            ma = np.mean(last_closes[-20:])
            std = np.std(last_closes[-20:])
            boll_upper = ma + 2 * std
            boll_lower = ma - 2 * std
            indicators['m_boll_pct'] = (last_closes[-1] - boll_lower) / (boll_upper - boll_lower) if (boll_upper - boll_lower) > 0 else 0.5
            indicators['m_boll_width'] = (boll_upper - boll_lower) / ma if ma > 0 else 0
            # 布林带收窄后突破（带宽<2%分位 → 放量突破上轨）
            indicators['m_boll_squeeze'] = 1 if indicators['m_boll_width'] < 0.02 else 0
        else:
            indicators['m_boll_pct'] = 0.5
            indicators['m_boll_width'] = 0
            indicators['m_boll_squeeze'] = 0

        # === 2. 分钟级量价关系 ===
        # 最后1小时（12根5分钟K线）的量价
        last_hour = minute_bars[-12:]
        if len(last_hour) >= 2:
            lh_closes = [b['close'] for b in last_hour]
            lh_vols = [b['vol'] for b in last_hour]
            # 尾盘量价齐升 = 最后4根K线涨+量增
            tail_4 = minute_bars[-4:]
            tail_closes = [b['close'] for b in tail_4]
            tail_vols = [b['vol'] for b in tail_4]
            tail_up = tail_closes[-1] > tail_closes[0]
            tail_vol_up = np.mean(tail_vols) > np.mean(volumes[-48:-4]) if len(volumes) > 48 else False
            indicators['tail_rally'] = 1 if tail_up and tail_vol_up else 0

            # 早盘放量（前30分钟=6根K线的量 vs 全天均量）
            if len(last_day_bars) >= 6:
                morning_vol = np.mean([b['vol'] for b in last_day_bars[:6]])
                day_avg_vol = np.mean([b['vol'] for b in last_day_bars])
                indicators['morning_vol_ratio'] = morning_vol / day_avg_vol if day_avg_vol > 0 else 1
            else:
                indicators['morning_vol_ratio'] = 1
        else:
            indicators['tail_rally'] = 0
            indicators['morning_vol_ratio'] = 1

        # === 3. 分钟级均线趋势 ===
        if len(closes) >= 60:
            m_ma20 = np.mean(closes[-20:])
            m_ma60 = np.mean(closes[-60:])
            indicators['m_ma20_vs_ma60'] = (m_ma20 - m_ma60) / m_ma60 if m_ma60 > 0 else 0
            # 5分钟MA20斜率
            m_ma20_prev = np.mean(closes[-21:-1])
            indicators['m_ma20_slope'] = (m_ma20 - m_ma20_prev) / m_ma20_prev if m_ma20_prev > 0 else 0
        else:
            indicators['m_ma20_vs_ma60'] = 0
            indicators['m_ma20_slope'] = 0

        # === 4. 分钟级量能异动 ===
        # 量比 > 2 的5分钟K线数量（近48根中）
        if len(volumes) >= 48:
            vol_ma = np.mean(volumes[-48:])
            big_vol_count = sum(1 for v in volumes[-48:] if v > vol_ma * 2)
            indicators['m_big_vol_bars'] = big_vol_count
        else:
            indicators['m_big_vol_bars'] = 0

        # === 5. 价格加速度 ===
        if len(closes) >= 12:
            ret_1 = closes[-1] - closes[-2]  # 最近1根
            ret_6 = (closes[-1] - closes[-6]) / 6  # 最近30分钟平均
            # 加速度 = 近期变化率正在增加
            indicators['m_accel'] = ret_1 - ret_6 if ret_6 != 0 else 0
        else:
            indicators['m_accel'] = 0

        return indicators


def run_pattern_learning(max_stocks=300, output_dir=None):
    """主流程：从赢家和输家中学习T+5模式"""

    if output_dir is None:
        output_dir = Path.home() / '.workbuddy' / 'a-share-analyst'
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    learner = T5PatternLearner()
    if not learner.connect():
        print("无法连接通达信服务器!")
        return

    try:
        # Step 1: 获取股票列表
        print("=" * 60)
        print("Step 1: 获取股票列表...")
        stocks = learner.get_stock_list(max_stocks)
        print(f"  获取到 {len(stocks)} 只股票")

        # Step 2: 拉日K线，找赢家和输家
        print("\n" + "=" * 60)
        print("Step 2: 扫描日K线，寻找T+5赢家和输家...")
        all_wins = []
        all_losses = []
        processed = 0

        for stock in stocks:
            processed += 1
            if processed % 50 == 0:
                print(f"  进度: {processed}/{len(stocks)}")

            daily = learner.get_daily_bars(stock['market'], stock['code'])
            if not daily or len(daily) < 30:
                continue

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

            # 限流
            time.sleep(0.05)

        print(f"\n  找到赢家交易: {len(all_wins)} 笔")
        print(f"  找到输家交易: {len(all_losses)} 笔")

        # Step 3: 对赢家和输家计算日级指标
        print("\n" + "=" * 60)
        print("Step 3: 计算日级别近似同步指标...")

        win_daily_features = []
        loss_daily_features = []

        for w in all_wins:
            feats = learner.calc_daily_indicators(w['daily_bars'], w['index_in_bars'])
            if feats:
                feats['_label'] = 'win'
                feats['_code'] = w['code']
                feats['_date'] = w['date']
                feats['_return'] = w['return']
                win_daily_features.append(feats)

        for l in all_losses:
            feats = learner.calc_daily_indicators(l['daily_bars'], l['index_in_bars'])
            if feats:
                feats['_label'] = 'loss'
                feats['_code'] = l['code']
                feats['_date'] = l['date']
                feats['_return'] = l['return']
                loss_daily_features.append(feats)

        print(f"  赢家有日级指标: {len(win_daily_features)} 笔")
        print(f"  输家有日级指标: {len(loss_daily_features)} 笔")

        # Step 4: 对赢家和输家拉分钟级K线，计算分钟级指标
        print("\n" + "=" * 60)
        print("Step 4: 拉取分钟级K线，计算分钟级指标...")

        # 为了不超时，取赢家和输家各最多200笔
        win_sample = all_wins[:200] if len(all_wins) > 200 else all_wins
        loss_sample = all_losses[:200] if len(all_losses) > 200 else all_losses

        win_minute_features = []
        loss_minute_features = []

        for i, w in enumerate(win_sample):
            if (i + 1) % 50 == 0:
                print(f"  赢家分钟数据: {i+1}/{len(win_sample)}")
            learner.ensure_connection()
            minute = learner.get_minute_bars(w['market'], w['code'], 240)
            if minute:
                feats = learner.calc_minute_indicators(minute)
                if feats:
                    feats['_label'] = 'win'
                    feats['_code'] = w['code']
                    feats['_date'] = w['date']
                    feats['_return'] = w['return']
                    win_minute_features.append(feats)
            time.sleep(0.05)

        for i, l in enumerate(loss_sample):
            if (i + 1) % 50 == 0:
                print(f"  输家分钟数据: {i+1}/{len(loss_sample)}")
            learner.ensure_connection()
            minute = learner.get_minute_bars(l['market'], l['code'], 240)
            if minute:
                feats = learner.calc_minute_indicators(minute)
                if feats:
                    feats['_label'] = 'loss'
                    feats['_code'] = l['code']
                    feats['_date'] = l['date']
                    feats['_return'] = l['return']
                    loss_minute_features.append(feats)
            time.sleep(0.05)

        print(f"  赢家有分钟指标: {len(win_minute_features)} 笔")
        print(f"  输家有分钟指标: {len(loss_minute_features)} 笔")

        # Step 5: 对比分析赢家 vs 输家的指标差异
        print("\n" + "=" * 60)
        print("Step 5: 赢家 vs 输家指标对比分析")
        print("=" * 60)

        # 合并日级和分钟级特征
        def merge_features(daily_feats, minute_feats):
            """按code+date合并日级和分钟级特征"""
            minute_map = {}
            for m in minute_feats:
                key = f"{m['_code']}_{m['_date']}"
                minute_map[key] = m

            merged = []
            for d in daily_feats:
                key = f"{d['_code']}_{d['_date']}"
                row = dict(d)
                if key in minute_map:
                    row.update({f"m_{k}": v for k, v in minute_map[key].items() if not k.startswith('_')})
                    row['_has_minute'] = True
                else:
                    row['_has_minute'] = False
                merged.append(row)
            return merged

        win_merged = merge_features(win_daily_features, win_minute_features)
        loss_merged = merge_features(loss_daily_features, loss_minute_features)

        # 可比较的指标列表
        daily_indicators = [
            'vol_ratio', 'today_return', 'vol_price_up', 'shrink_vol_up', 'vol_price_down',
            'close_vs_ma5', 'close_vs_ma10', 'close_vs_ma20', 'ma_bullish', 'ma5_slope',
            'boll_pct', 'boll_width', 'ret_5d', 'ret_3d', 'today_amplitude',
            'upper_shadow', 'lower_shadow', 'vol_increasing_5d', 'vol_ma3_vs_ma10'
        ]

        minute_indicators = [
            'm_m_boll_pct', 'm_m_boll_width', 'm_m_boll_squeeze',
            'm_tail_rally', 'm_morning_vol_ratio',
            'm_m_ma20_vs_ma60', 'm_m_ma20_slope',
            'm_m_big_vol_bars', 'm_m_accel'
        ]

        print("\n--- 日级别指标对比 (赢家 vs 输家) ---\n")
        print(f"{'指标':<25} {'赢家均值':>12} {'输家均值':>12} {'差异':>12} {'区分力':>8}")
        print("-" * 75)

        significant_features = []

        for ind in daily_indicators:
            win_vals = [f[ind] for f in win_merged if ind in f and f[ind] is not None]
            loss_vals = [f[ind] for f in loss_merged if ind in f and f[ind] is not None]

            if not win_vals or not loss_vals:
                continue

            win_mean = np.mean(win_vals)
            loss_mean = np.mean(loss_vals)
            diff = win_mean - loss_mean

            # 区分力 = |差异| / max(标准差)
            win_std = np.std(win_vals) if len(win_vals) > 1 else 1
            loss_std = np.std(loss_vals) if len(loss_vals) > 1 else 1
            pooled_std = np.sqrt((win_std**2 + loss_std**2) / 2)
            discrim = abs(diff) / pooled_std if pooled_std > 0 else 0

            print(f"{ind:<25} {win_mean:>12.4f} {loss_mean:>12.4f} {diff:>+12.4f} {discrim:>8.3f}")

            # Cohen's d > 0.5 算中等区分力，> 0.8 算强
            if discrim > 0.3:
                significant_features.append({
                    'indicator': ind,
                    'win_mean': win_mean,
                    'loss_mean': loss_mean,
                    'cohens_d': discrim,
                    'direction': 'win>loss' if diff > 0 else 'loss>win'
                })

        # 分钟级指标
        has_minute_wins = [f for f in win_merged if f.get('_has_minute')]
        has_minute_losses = [f for f in loss_merged if f.get('_has_minute')]

        if has_minute_wins or has_minute_losses:
            print("\n--- 分钟级别指标对比 (赢家 vs 输家) ---\n")
            print(f"{'指标':<25} {'赢家均值':>12} {'输家均值':>12} {'差异':>12} {'区分力':>8}")
            print("-" * 75)

            # 修正分钟指标字段名（merge时加了m_前缀）
            actual_minute_inds = []
            sample = has_minute_wins[0] if has_minute_wins else (has_minute_losses[0] if has_minute_losses else None)
            if sample:
                for k in sample:
                    if k.startswith('m_') and not k.startswith('_'):
                        actual_minute_inds.append(k)

            for ind in actual_minute_inds:
                win_vals = [f[ind] for f in has_minute_wins if ind in f and f[ind] is not None]
                loss_vals = [f[ind] for f in has_minute_losses if ind in f and f[ind] is not None]

                if not win_vals or not loss_vals:
                    continue

                win_mean = np.mean(win_vals)
                loss_mean = np.mean(loss_vals)
                diff = win_mean - loss_mean

                win_std = np.std(win_vals) if len(win_vals) > 1 else 1
                loss_std = np.std(loss_vals) if len(loss_vals) > 1 else 1
                pooled_std = np.sqrt((win_std**2 + loss_std**2) / 2)
                discrim = abs(diff) / pooled_std if pooled_std > 0 else 0

                print(f"{ind:<25} {win_mean:>12.4f} {loss_mean:>12.4f} {diff:>+12.4f} {discrim:>8.3f}")

                if discrim > 0.3:
                    significant_features.append({
                        'indicator': ind,
                        'win_mean': win_mean,
                        'loss_mean': loss_mean,
                        'cohens_d': discrim,
                        'direction': 'win>loss' if diff > 0 else 'loss>win'
                    })

        # Step 6: 显著特征总结
        print("\n" + "=" * 60)
        print("Step 6: 有区分力的特征（Cohen's d > 0.3）")
        print("=" * 60)

        if significant_features:
            significant_features.sort(key=lambda x: -x['cohens_d'])
            print(f"\n共找到 {len(significant_features)} 个有区分力的特征:\n")
            for i, sf in enumerate(significant_features, 1):
                print(f"  {i}. {sf['indicator']}: d={sf['cohens_d']:.3f} ({sf['direction']})")
                print(f"     赢家: {sf['win_mean']:.4f}, 输家: {sf['loss_mean']:.4f}")
        else:
            print("\n没有找到Cohen's d > 0.3的特征。需要更多数据或更好的指标设计。")

        # Step 7: 基于发现的特征构建简单分类器回测
        print("\n" + "=" * 60)
        print("Step 7: 基于发现特征构建分类器回测")
        print("=" * 60)

        if significant_features:
            # 取top 5特征
            top_features = [sf['indicator'] for sf in significant_features[:5]]
            print(f"\n使用Top 5特征构建分类器: {top_features}")

            # 简单打分模型: 每个特征按方向打分
            all_data = win_merged + loss_merged
            results = []

            for sample in all_data:
                score = 0
                for sf in significant_features[:5]:
                    ind = sf['indicator']
                    if ind not in sample or sample[ind] is None:
                        continue
                    val = sample[ind]
                    # 如果赢家方向>输家，则值越大分越高
                    if sf['direction'] == 'win>loss':
                        score += 1 if val > sf['loss_mean'] else 0
                    else:
                        score += 1 if val < sf['loss_mean'] else 0

                results.append({
                    'code': sample.get('_code', ''),
                    'date': sample.get('_date', ''),
                    'label': sample.get('_label', ''),
                    'return': sample.get('_return', 0),
                    'score': score,
                    'max_score': len(significant_features[:5])
                })

            # 按分数分组看胜率
            score_groups = defaultdict(list)
            for r in results:
                score_groups[r['score']].append(r)

            print(f"\n{'分数':<8} {'交易数':>8} {'实际赢家':>8} {'胜率':>8} {'平均T+5收益':>12}")
            print("-" * 50)

            for score in sorted(score_groups.keys()):
                group = score_groups[score]
                wins = [g for g in group if g['label'] == 'win']
                win_rate = len(wins) / len(group) * 100
                avg_ret = np.mean([g['return'] for g in group])
                print(f"{score}/{5:<6} {len(group):>8} {len(wins):>8} {win_rate:>7.1f}% {avg_ret:>11.2%}")

        # 保存结果
        output_file = output_dir / 't5_pattern_analysis.json'
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump({
                'meta': {
                    'stocks_scanned': len(stocks),
                    'win_trades': len(all_wins),
                    'loss_trades': len(all_losses),
                    'win_features': len(win_daily_features),
                    'loss_features': len(loss_daily_features),
                    'win_minute': len(win_minute_features),
                    'loss_minute': len(loss_minute_features),
                    'significant_features': significant_features,
                },
                'win_daily_samples': win_daily_features[:100],  # 限大小
                'loss_daily_samples': loss_daily_features[:100],
            }, f, ensure_ascii=False, indent=2, default=str)

        print(f"\n结果已保存到: {output_file}")

    finally:
        learner.disconnect()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='T+5短线策略回测V3 - 从赢家学规律')
    parser.add_argument('--stocks', type=int, default=300, help='扫描股票数量')
    parser.add_argument('--output-dir', type=str, default=None, help='输出目录')
    args = parser.parse_args()

    run_pattern_learning(max_stocks=args.stocks, output_dir=args.output_dir)
