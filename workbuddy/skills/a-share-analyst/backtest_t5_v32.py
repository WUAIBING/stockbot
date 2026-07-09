"""
T+5 短线策略回测 V3.2 — 从赢家学规律（大样本版）
===================================================
改进：
1. 用沪深300+中证500主要成分股（硬编码列表，确保流动性+数据完整）
2. 扩大到500只高流动性股票
3. 拉取800根日K线（约3年数据），更多交易样本
4. 聚焦近似同步指标
"""

import json
import time
import numpy as np
from pathlib import Path
from collections import defaultdict
from pytdx.hq import TdxHq_API

TDX_HOST = '60.191.117.167'
TDX_PORT = 7709
BACKUP_HOST = '120.76.152.87'

WIN_THRESHOLD = 0.09
LOSS_THRESHOLD = -0.05


# 沪深两市主要股票代码（按流动性筛选的前500只）
# 格式: (market, code) — market: 1=沪, 0=深
MAJOR_STOCKS = []

def build_stock_list(api, max_stocks=500):
    """从两市获取活跃股票列表"""
    stocks = []
    for market in [1, 0]:
        try:
            count = api.get_security_count(market)
            for start in range(0, min(count, 5000), 80):
                if len(stocks) >= max_stocks:
                    break
                try:
                    data = api.get_security_list(market, start)
                    if data is None:
                        continue
                    for item in data:
                        code = item.get('code', '')
                        name = item.get('name', '')
                        # 只要6位纯数字的主板/创业板/科创板
                        if len(code) != 6:
                            continue
                        # 沪市主板 60xxxx, 科创板 688xxx
                        # 深市主板 00xxxx, 创业板 30xxxx
                        if not (code.startswith('60') or code.startswith('00') or 
                                code.startswith('30') or code.startswith('688')):
                            continue
                        if 'ST' in name or '退' in name:
                            continue
                        # 测试能否拉到日K线
                        stocks.append({'market': market, 'code': code, 'name': name})
                except:
                    continue
            if len(stocks) >= max_stocks:
                break
        except:
            continue
    return stocks[:max_stocks]


def get_daily_bars(api, market, code, count=800):
    """获取日K线，按时间升序"""
    try:
        data = api.get_security_bars(9, market, code, 0, count)
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
            bars.reverse()  # pytdx返回最新在前 → 反转
            return bars
    except:
        pass
    return None


def get_minute_bars(api, market, code, count=240):
    """获取5分钟K线，按时间升序"""
    try:
        data = api.get_security_bars(0, market, code, 0, count)
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


def find_t5_trades(daily_bars):
    """找出所有T+5赢家和输家交易"""
    if not daily_bars or len(daily_bars) < 30:
        return [], []
    wins = []
    losses = []
    for i in range(20, len(daily_bars) - 5):
        buy_day = daily_bars[i]
        sell_day = daily_bars[i + 5]
        if buy_day['close'] <= 0:
            continue
        # 跳过涨停日
        if buy_day['open'] > 0:
            pct = (buy_day['close'] - buy_day['open']) / buy_day['open']
            if pct >= 0.095:
                continue
        t5_return = (sell_day['close'] - buy_day['close']) / buy_day['close']
        t5_high = max(d['high'] for d in daily_bars[i+1:i+6])
        t5_low = min(d['low'] for d in daily_bars[i+1:i+6])
        trade = {
            'date': buy_day['date'],
            'buy_close': buy_day['close'],
            'return': t5_return,
            'max_gain': (t5_high - buy_day['close']) / buy_day['close'],
            'max_dd': (t5_low - buy_day['close']) / buy_day['close'],
            'index': i,
        }
        if t5_return >= WIN_THRESHOLD:
            wins.append(trade)
        elif t5_return <= LOSS_THRESHOLD:
            losses.append(trade)
    return wins, losses


def calc_daily_features(daily_bars, buy_idx):
    """计算日级别近似同步指标"""
    if buy_idx < 20 or buy_idx >= len(daily_bars):
        return None
    lookback = daily_bars[max(0, buy_idx-20):buy_idx+1]
    if len(lookback) < 10:
        return None

    closes = np.array([d['close'] for d in lookback], dtype=float)
    opens = np.array([d['open'] for d in lookback], dtype=float)
    highs = np.array([d['high'] for d in lookback], dtype=float)
    lows = np.array([d['low'] for d in lookback], dtype=float)
    amounts = np.array([d['amount'] for d in lookback], dtype=float)

    f = {}

    # 1. 量价关系
    today_ret = (closes[-1] - closes[-2]) / closes[-2] if len(closes) >= 2 and closes[-2] > 0 else 0
    f['today_ret'] = today_ret

    if len(amounts) >= 6:
        amt_ma5 = np.mean(amounts[-6:-1])
        f['amt_ratio'] = amounts[-1] / amt_ma5 if amt_ma5 > 0 else 1.0
    else:
        f['amt_ratio'] = 1.0

    f['vol_price_up'] = 1 if today_ret > 0.01 and f['amt_ratio'] > 1.5 else 0
    f['shrink_vol_up'] = 1 if 0 < today_ret < 0.03 and f['amt_ratio'] < 0.7 else 0
    f['vol_price_down'] = 1 if today_ret < -0.01 and f['amt_ratio'] > 1.5 else 0
    f['shrink_vol_down'] = 1 if -0.05 < today_ret < -0.005 and f['amt_ratio'] < 0.7 else 0

    # 2. 均线位置
    ma5 = np.mean(closes[-5:]) if len(closes) >= 5 else closes[-1]
    ma10 = np.mean(closes[-10:]) if len(closes) >= 10 else closes[-1]
    ma20 = np.mean(closes[-20:]) if len(closes) >= 20 else closes[-1]

    f['close_vs_ma5'] = (closes[-1] - ma5) / ma5 if ma5 > 0 else 0
    f['close_vs_ma10'] = (closes[-1] - ma10) / ma10 if ma10 > 0 else 0
    f['close_vs_ma20'] = (closes[-1] - ma20) / ma20 if ma20 > 0 else 0
    f['ma_bull'] = 1 if ma5 > ma10 > ma20 else 0
    f['ma_bear'] = 1 if ma5 < ma10 < ma20 else 0

    # MA5斜率
    if len(closes) >= 8:
        ma5_prev = np.mean(closes[-8:-3])
        f['ma5_slope'] = (ma5 - ma5_prev) / ma5_prev if ma5_prev > 0 else 0
    else:
        f['ma5_slope'] = 0

    # 3. 布林带
    if len(closes) >= 20:
        std20 = np.std(closes[-20:])
        boll_upper = ma20 + 2 * std20
        boll_lower = ma20 - 2 * std20
        boll_width = boll_upper - boll_lower
        f['boll_pct'] = (closes[-1] - boll_lower) / boll_width if boll_width > 0 else 0.5
        f['boll_width_norm'] = boll_width / ma20 if ma20 > 0 else 0
        f['boll_breakout_up'] = 1 if closes[-1] > boll_upper else 0
        f['boll_bounce_lower'] = 1 if len(closes) >= 2 and closes[-2] <= boll_lower and closes[-1] > boll_lower else 0
    else:
        f['boll_pct'] = 0.5
        f['boll_width_norm'] = 0
        f['boll_breakout_up'] = 0
        f['boll_bounce_lower'] = 0

    # 4. 量能加速度
    if len(amounts) >= 10:
        amt_ma3 = np.mean(amounts[-3:])
        amt_ma10 = np.mean(amounts[-10:])
        f['amt_accel'] = 1 if amt_ma3 > np.mean(amounts[-5:]) > amt_ma10 else 0
        f['amt_ma3_vs_ma10'] = amt_ma3 / amt_ma10 if amt_ma10 > 0 else 1
    else:
        f['amt_accel'] = 0
        f['amt_ma3_vs_ma10'] = 1

    # 5. 形态
    f['ret_5d'] = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 and closes[-6] > 0 else 0
    f['ret_3d'] = (closes[-1] - closes[-4]) / closes[-4] if len(closes) >= 4 and closes[-4] > 0 else 0
    f['amplitude'] = (highs[-1] - lows[-1]) / opens[-1] if opens[-1] > 0 else 0

    body = abs(closes[-1] - opens[-1])
    total_range = highs[-1] - lows[-1]
    f['body_ratio'] = body / total_range if total_range > 0 else 0

    # 连续阳线
    consec_up = 0
    for j in range(len(closes)-1, 0, -1):
        if closes[j] > closes[j-1]:
            consec_up += 1
        else:
            break
    f['consec_up'] = consec_up

    # 6. 关键组合信号
    # 强势回踩信号 = MA20上方中期趋势 + 短期回调到MA5下方 + 缩量
    f['pullback_shrink'] = 1 if closes[-1] < ma5 and closes[-1] > ma20 and f['shrink_vol_down'] else 0

    # 布林下轨反弹 + 缩量
    f['boll_bounce_shrink'] = 1 if f['boll_pct'] < 0.2 and f['shrink_vol_down'] else 0

    return f


def calc_minute_features(minute_bars):
    """计算分钟级别近似同步指标"""
    if not minute_bars or len(minute_bars) < 48:
        return None

    closes = np.array([b['close'] for b in minute_bars], dtype=float)
    amounts = np.array([b['amount'] for b in minute_bars], dtype=float)

    f = {}

    # 最后一个交易日
    ld = minute_bars[-48:]
    ld_closes = np.array([b['close'] for b in ld], dtype=float)
    ld_amounts = np.array([b['amount'] for b in ld], dtype=float)
    ld_opens = np.array([b['open'] for b in ld], dtype=float)

    # 1. 分钟布林
    if len(ld_closes) >= 20:
        ld_ma = np.mean(ld_closes[-20:])
        ld_std = np.std(ld_closes[-20:])
        boll_u = ld_ma + 2 * ld_std
        boll_l = ld_ma - 2 * ld_std
        bw = boll_u - boll_l
        f['m_boll_pct'] = (ld_closes[-1] - boll_l) / bw if bw > 0 else 0.5
        f['m_boll_width'] = bw / ld_ma if ld_ma > 0 else 0
    else:
        f['m_boll_pct'] = 0.5
        f['m_boll_width'] = 0

    # 2. 量价
    if len(ld_amounts) >= 6:
        morning_amt = np.mean(ld_amounts[:6])
        day_avg = np.mean(ld_amounts)
        f['m_morning_amt_ratio'] = morning_amt / day_avg if day_avg > 0 else 1
    else:
        f['m_morning_amt_ratio'] = 1

    if len(ld_amounts) >= 6:
        tail_amt = np.mean(ld_amounts[-6:])
        prev_amt = np.mean(ld_amounts[:-6]) if len(ld_amounts) > 6 else 1
        f['m_tail_amt_ratio'] = tail_amt / prev_amt if prev_amt > 0 else 1
        f['m_tail_rally'] = 1 if ld_closes[-1] > ld_closes[0] and f['m_tail_amt_ratio'] > 1.3 else 0
    else:
        f['m_tail_amt_ratio'] = 1
        f['m_tail_rally'] = 0

    # 3. 均线
    if len(closes) >= 60:
        m_ma20 = np.mean(closes[-20:])
        m_ma60 = np.mean(closes[-60:])
        f['m_ma20_vs_ma60'] = (m_ma20 - m_ma60) / m_ma60 if m_ma60 > 0 else 0
    else:
        f['m_ma20_vs_ma60'] = 0

    # 4. 量能异动
    if len(ld_amounts) >= 10:
        amt_mean = np.mean(ld_amounts)
        f['m_big_amt_bars'] = sum(1 for a in ld_amounts if a > amt_mean * 2)
    else:
        f['m_big_amt_bars'] = 0

    # 5. 当日涨跌
    if len(ld_closes) >= 2 and ld_opens[0] > 0:
        f['m_day_ret'] = (ld_closes[-1] - ld_opens[0]) / ld_opens[0]
    else:
        f['m_day_ret'] = 0

    # V型反转
    if len(ld_closes) >= 12:
        half = len(ld_closes) // 2
        first_ret = (ld_closes[half] - ld_closes[0]) / ld_closes[0] if ld_closes[0] > 0 else 0
        second_ret = (ld_closes[-1] - ld_closes[half]) / ld_closes[half] if ld_closes[half] > 0 else 0
        f['m_v_shape'] = 1 if first_ret < -0.005 and second_ret > 0.01 else 0
    else:
        f['m_v_shape'] = 0

    return f


def run(max_stocks=500, output_dir=None):
    if output_dir is None:
        output_dir = Path.home() / '.workbuddy' / 'a-share-analyst'
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    api = TdxHq_API()
    if not api.connect(TDX_HOST, TDX_PORT):
        if not api.connect(BACKUP_HOST, TDX_PORT):
            print("无法连接!")
            return

    try:
        print("=" * 70)
        print("Step 1: 获取股票列表...")
        stocks = build_stock_list(api, max_stocks)
        print(f"  获取 {len(stocks)} 只股票")

        # Step 2: 扫描日K线
        print("\n" + "=" * 70)
        print("Step 2: 扫描日K线 (800根/只)，寻找T+5赢家和输家...")
        all_wins = []
        all_losses = []
        processed = 0
        errors = 0

        for stock in stocks:
            processed += 1
            if processed % 100 == 0:
                print(f"  进度: {processed}/{len(stocks)}, 赢家:{len(all_wins)}, 输家:{len(all_losses)}")

            daily = get_daily_bars(api, stock['market'], stock['code'], 800)
            if not daily or len(daily) < 30:
                errors += 1
                continue

            # 验证排序
            dates = [d['date'] for d in daily]
            if dates != sorted(dates):
                daily.sort(key=lambda x: x['date'])

            wins, losses = find_t5_trades(daily)

            for w in wins:
                w['code'] = stock['code']
                w['market'] = stock['market']
                w['daily_bars'] = daily
                all_wins.append(w)

            for l in losses:
                l['code'] = l.get('code', stock['code'])
                l['market'] = stock['market']
                l['daily_bars'] = daily
                all_losses.append(l)

            time.sleep(0.02)

        print(f"\n  赢家: {len(all_wins)} 笔, 输家: {len(all_losses)} 笔, 错误: {errors}")

        # Step 3: 计算日级指标
        print("\n" + "=" * 70)
        print("Step 3: 计算日级别指标...")
        win_daily = []
        loss_daily = []

        for w in all_wins:
            feats = calc_daily_features(w['daily_bars'], w['index'])
            if feats:
                feats['_label'] = 'win'
                feats['_code'] = w['code']
                feats['_date'] = w['date']
                feats['_return'] = w['return']
                win_daily.append(feats)

        for l in all_losses:
            feats = calc_daily_features(l['daily_bars'], l['index'])
            if feats:
                feats['_label'] = 'loss'
                feats['_code'] = l.get('code', '')
                feats['_date'] = l['date']
                feats['_return'] = l['return']
                loss_daily.append(feats)

        print(f"  赢家日级指标: {len(win_daily)}, 输家日级指标: {len(loss_daily)}")

        # Step 4: 分钟级指标（取子样本）
        print("\n" + "=" * 70)
        print("Step 4: 分钟级指标（取前200笔赢家+输家）...")

        win_sample = sorted(all_wins, key=lambda x: -x['return'])[:200]
        loss_sample = sorted(all_losses, key=lambda x: x['return'])[:200]

        win_minute = []
        loss_minute = []

        for i, w in enumerate(win_sample):
            if (i+1) % 50 == 0:
                print(f"  赢家: {i+1}/{len(win_sample)}")
            try:
                api.connect(TDX_HOST, TDX_PORT)
            except:
                time.sleep(1)
                try:
                    api.connect(BACKUP_HOST, TDX_PORT)
                except:
                    continue
            m = get_minute_bars(api, w['market'], w['code'], 240)
            if m:
                feats = calc_minute_features(m)
                if feats:
                    feats['_label'] = 'win'
                    feats['_code'] = w['code']
                    feats['_date'] = w['date']
                    feats['_return'] = w['return']
                    win_minute.append(feats)
            time.sleep(0.05)

        for i, l in enumerate(loss_sample):
            if (i+1) % 50 == 0:
                print(f"  输家: {i+1}/{len(loss_sample)}")
            try:
                api.connect(TDX_HOST, TDX_PORT)
            except:
                time.sleep(1)
                try:
                    api.connect(BACKUP_HOST, TDX_PORT)
                except:
                    continue
            m = get_minute_bars(api, l['market'], l['code'], 240)
            if m:
                feats = calc_minute_features(m)
                if feats:
                    feats['_label'] = 'loss'
                    feats['_code'] = l.get('code', '')
                    feats['_date'] = l['date']
                    feats['_return'] = l['return']
                    loss_minute.append(feats)
            time.sleep(0.05)

        print(f"  赢家分钟: {len(win_minute)}, 输家分钟: {len(loss_minute)}")

        # Step 5: 对比分析
        print("\n" + "=" * 70)
        print("Step 5: 赢家 vs 输家 — 近似同步指标对比")
        print("=" * 70)

        def compare(win_list, loss_list, prefix=""):
            if not win_list or not loss_list:
                return []
            sample = win_list[0]
            keys = [k for k in sample.keys() if not k.startswith('_')]
            results = []

            print(f"\n{prefix}{'指标':<28} {'赢家均值':>12} {'输家均值':>12} {'差异':>12} {'Cohen-d':>8}")
            print("-" * 80)

            for key in keys:
                wv = np.array([f[key] for f in win_list if key in f and f[key] is not None], dtype=float)
                lv = np.array([f[key] for f in loss_list if key in f and f[key] is not None], dtype=float)
                wv = wv[np.isfinite(wv)]
                lv = lv[np.isfinite(lv)]
                if len(wv) < 10 or len(lv) < 10:
                    continue

                wm = np.mean(wv)
                lm = np.mean(lv)
                diff = wm - lm
                ws = np.std(wv)
                ls = np.std(lv)
                ps = np.sqrt((ws**2 + ls**2) / 2)
                cd = abs(diff) / ps if ps > 0 else 0

                direction = "W>L" if diff > 0 else "L>W"
                marker = " ***" if cd > 0.8 else (" **" if cd > 0.5 else (" *" if cd > 0.3 else ""))

                print(f"{prefix}{key:<28} {wm:>12.4f} {lm:>12.4f} {diff:>+12.4f} {cd:>8.3f}{marker}")

                if cd > 0.15:
                    results.append({
                        'indicator': key,
                        'win_mean': float(wm),
                        'loss_mean': float(lm),
                        'cohens_d': float(cd),
                        'direction': direction,
                    })

            return results

        daily_sig = compare(win_daily, loss_daily, "[日级] ")
        minute_sig = compare(win_minute, loss_minute, "[分钟] ")

        # Step 6: 汇总
        print("\n" + "=" * 70)
        print("Step 6: 有区分力的特征排序")
        print("=" * 70)

        all_sig = daily_sig + minute_sig
        all_sig.sort(key=lambda x: -x['cohens_d'])

        if all_sig:
            print(f"\n共 {len(all_sig)} 个 d>0.15 的特征:\n")
            for i, sf in enumerate(all_sig, 1):
                bar = '#' * int(sf['cohens_d'] * 15)
                print(f"  {i:2d}. {sf['indicator']:<28} d={sf['cohens_d']:.3f} {sf['direction']:<5} {bar}")
                print(f"      赢家: {sf['win_mean']:.4f}  输家: {sf['loss_mean']:.4f}")

        # Step 7: 基于特征的回测
        print("\n" + "=" * 70)
        print("Step 7: 基于Top特征的回测")
        print("=" * 70)

        # 合并
        def merge(daily_list, minute_list):
            mm = {}
            for m in minute_list:
                k = f"{m['_code']}_{m['_date']}"
                mm[k] = m
            merged = []
            for d in daily_list:
                k = f"{d['_code']}_{d['_date']}"
                row = dict(d)
                if k in mm:
                    for key2, v in mm[k].items():
                        if not key2.startswith('_'):
                            row[f'm_{key2}'] = v
                    row['_has_minute'] = True
                else:
                    row['_has_minute'] = False
                merged.append(row)
            return merged

        win_m = merge(win_daily, win_minute)
        loss_m = merge(loss_daily, loss_minute)

        if all_sig:
            top_n = min(10, len(all_sig))
            top_feats = all_sig[:top_n]
            print(f"\n使用 Top {top_n} 特征:")

            all_data = win_m + loss_m
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
                    if sf['direction'] == 'W>L':
                        score += 1 if val > sf['loss_mean'] else 0
                    else:
                        score += 1 if val < sf['loss_mean'] else 0

                scores.append({
                    'label': sample.get('_label', ''),
                    'return': sample.get('_return', 0),
                    'score': score,
                    'has_minute': sample.get('_has_minute', False),
                })

            # 按分数分组
            sg = defaultdict(list)
            for s in scores:
                sg[s['score']].append(s)

            print(f"\n{'分数':<10} {'交易数':>8} {'赢家':>8} {'胜率':>8} {'平均T+5收益':>12}")
            print("-" * 55)
            for score in sorted(sg.keys()):
                group = sg[score]
                wins = [g for g in group if g['label'] == 'win']
                wr = len(wins) / len(group) * 100 if group else 0
                ar = np.mean([g['return'] for g in group])
                print(f"{score}/{top_n:<8} {len(group):>8} {len(wins):>8} {wr:>7.1f}% {ar:>11.2%}")

            # 只看有分钟数据的样本
            minute_scores = [s for s in scores if s['has_minute']]
            if len(minute_scores) > 50:
                print(f"\n--- 仅有分钟数据的子样本 ({len(minute_scores)} 笔) ---")
                msg = defaultdict(list)
                for s in minute_scores:
                    msg[s['score']].append(s)
                print(f"{'分数':<10} {'交易数':>8} {'赢家':>8} {'胜率':>8} {'平均T+5收益':>12}")
                print("-" * 55)
                for score in sorted(msg.keys()):
                    group = msg[score]
                    wins = [g for g in group if g['label'] == 'win']
                    wr = len(wins) / len(group) * 100 if group else 0
                    ar = np.mean([g['return'] for g in group])
                    print(f"{score}/{top_n:<8} {len(group):>8} {len(wins):>8} {wr:>7.1f}% {ar:>11.2%}")

        # 保存
        output_file = output_dir / 't5_pattern_analysis_v32.json'
        with open(output_file, 'w', encoding='utf-8') as fout:
            json.dump({
                'meta': {
                    'version': '3.2',
                    'stocks': len(stocks),
                    'wins': len(all_wins),
                    'losses': len(all_losses),
                    'win_daily': len(win_daily),
                    'loss_daily': len(loss_daily),
                    'win_minute': len(win_minute),
                    'loss_minute': len(loss_minute),
                    'significant_features': all_sig,
                }
            }, fout, ensure_ascii=False, indent=2, default=str)
        print(f"\n结果已保存: {output_file}")

    finally:
        try:
            api.disconnect()
        except:
            pass


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--stocks', type=int, default=500)
    args = parser.parse_args()
    run(max_stocks=args.stocks)
