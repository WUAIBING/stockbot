#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V10 看门狗 v2 — 近似同步信号盯盘系统

核心理念：发现近似同步信号，而非滞后指标
v1 基础能力:
  - 资金流入加速 = 先于价格上涨的同步信号
  - 板块资金异动 = 板块轮动的前兆
  - 量比突变 = 主力进场的痕迹
  - 个股分时净流入 = 实时资金方向

v2 新增能力:
  - TDX真实资金流: 主力/超大单/大单净流入（日频，远胜K线推算）
  - 多信号共振评分: 板块+资金流+加速度+形态+V10tier → confluence_score(0-100)
  - 日内形态检测: V形反转/双底/恐慌末端/尾盘拉升
  - 盘前预热扫描: 提前识别"setup"股票，14:50只需确认

A股交易时段关键时点：
  9:30  开盘定调 — 跳空方向、持仓股开盘
  10:30 早盘中  — 趋势确认、prescan hotlist
  11:15 午盘前 — 持仓浮盈浮亏、市场情绪
  13:30 午后确认 — 午后是否反转、prescan更新
  14:30 预热    — 为14:50决策预热
  14:50 决策    — V10信号扫描 → 买入/卖出/观望
  14:55 执行    — confluence_score>=60的股票下注

触发式警觉（随时触发）：
  持仓股日内跌幅 >3%   → 立即评估信号衰减
  持仓股急速拉升 >5%   → 评估冲高回落风险
  大盘跌幅 >2%        → 全面防御
  候选股尾盘放量拉升   → 可能升级信号

用法:
  python v10_watchdog.py                    # 全量巡检（任意时点）
  python v10_watchdog.py --sector           # 仅板块资金扫描
  python v10_watchdog.py --holding          # 仅持仓盯盘
  python v10_watchdog.py --flow CODE        # 指定股票资金流分析
  python v10_watchdog.py --alert            # 仅输出触发告警
  python v10_watchdog.py --confluence CODE  # 多信号共振评分
  python v10_watchdog.py --prescan          # 盘前预热扫描(10:30/13:30)
  python v10_watchdog.py --pattern CODE     # 日内形态检测
"""

import os
import sys
import csv
import json
import argparse
from datetime import datetime, timedelta
from collections import defaultdict

import numpy as np
import pandas as pd
import requests as http_requests
from pytdx.hq import TdxHq_API

from package_paths import CSI1000_SKILLS_DIR, DATA_DIR

# ============================================================
# 配置
# ============================================================
MX_APIKEY = os.environ.get('MX_APIKEY', '')
MX_API_URL = os.environ.get('MX_API_URL', 'https://mkapi2.dfcfs.com/finskillshub')
DATA_DIR = str(DATA_DIR)
SCAN_CSV = os.path.join(DATA_DIR, 'v10_scan_full.csv')
TRACK_FILE = os.path.join(DATA_DIR, 'v10_track_record.csv')

TDX_HOSTS = [
    ('218.75.126.9', 7709),
    ('60.191.117.167', 7709),
    ('112.74.214.43', 7727),
    ('221.231.141.60', 7709),
]

# 通达信行业板块代码 → 名称映射（沪市 880xxx）
SECTOR_MAP = {
    '880301': '软件服务', '880302': 'IT设备', '880310': '互联网',
    '880318': '通信设备', '880322': '元器件', '880350': '电气设备',
    '880360': '建筑', '880380': '机械', '880382': '电器仪表',
    '880400': '医药', '880402': '医疗保健', '880410': '农业',
    '880421': '食品饮料', '880422': '酿酒', '880440': '半导体',
    '880442': '矿物制品', '880450': '航空', '880452': '船舶',
    '880460': '银行', '880462': '证券', '880464': '保险',
    '880466': '多元金融', '880473': '电信运营', '880474': '公共交通',
    '880476': '纺织服饰', '880480': '汽车整车', '880493': '汽车整车',
    '880494': '汽车配件', '880498': '广告包装', '880500': '电力',
    '880502': '石油', '880504': '煤炭', '880506': '钢铁',
    '880508': '有色金属', '880510': '化工', '880512': '化纤',
    '880514': '建材', '880520': '造纸', '880530': '家居用品',
    '880534': '文教休闲', '880536': '商业连锁', '880540': '传媒娱乐',
    '880543': '旅游', '880548': '仓储物流', '880550': '房地产',
    '880552': '水务', '880554': '供气供热', '880556': '环境保护',
    '880560': '综合类',
}

# 指数代码
INDEX_CODES = [
    (1, '000001', '上证指数'),
    (0, '399001', '深证成指'),
    (0, '399006', '创业板指'),
    (1, '000016', '上证50'),
    (0, '399673', '创业板50'),
]

# ============================================================
# 告警阈值
# ============================================================
ALERT_HOLDING_DROP_PCT = -3.0    # 持仓日内跌幅超过3%告警
ALERT_HOLDING_SURGE_PCT = 5.0    # 持仓急速拉升超过5%告警
ALERT_INDEX_DROP_PCT = -2.0      # 大盘跌幅超过2%全面防御
ALERT_NET_FLOW_RATIO = 30.0     # 个股分时净流入比超过30%关注
ALERT_SECTOR_SURGE_PCT = 3.0    # 板块涨幅超过3%关注
ALERT_VOL_ACCEL = 2.0           # 成交量加速度超过2倍关注

# TDX TQLEX API（直连，不需要token）
TQLEX_URL = 'http://tdxhub.icfqs.com:7615/TQLEX'
TQLEX_TIMEOUT = 10  # seconds

# Confluence Score 权重
CONFLUENCE_WEIGHTS = {
    'sector': 20,      # 板块资金流(0-20)
    'capital': 20,     # TDX真实主力资金流(0-20)
    'intraday': 20,    # 分时资金流+加速度(0-20)
    'pattern': 20,     # 日内形态(0-20)
    'tier': 20,        # V10信号tier(0-20)
}
# Confluence 阈值
CONFLUENCE_HIGH = 60     # >=60 高置信度
CONFLUENCE_MEDIUM = 40   # >=40 中等
CONFLUENCE_LOW = 20      # >=20 观察


# ============================================================
# TDX 连接
# ============================================================
def connect_tdx():
    """连接TDX行情服务器"""
    api = TdxHq_API()
    for host, port in TDX_HOSTS:
        try:
            if api.connect(host, port):
                return api
        except Exception:
            continue
    return None


def market_from_code(code):
    """根据股票代码判断市场: 1=沪市 0=深市"""
    if code.startswith(('6', '9', '880')):
        return 1
    return 0


# ============================================================
# 1. 大盘指数扫描
# ============================================================
def scan_index(api):
    """扫描主要指数实时行情"""
    codes = [(mkt, code) for mkt, code, _ in INDEX_CODES]
    quotes = api.get_security_quotes(codes)
    if not quotes:
        return []

    results = []
    for q in quotes:
        chg_pct = (q['price'] / q['last_close'] - 1) * 100 if q.get('last_close', 0) > 0 else 0
        name = [n for m, c, n in INDEX_CODES if c == q['code']]
        results.append({
            'code': q['code'],
            'name': name[0] if name else '',
            'price': q['price'],
            'chg_pct': round(chg_pct, 2),
            'amount_yi': round(q.get('amount', 0) / 1e8, 1),
        })

    return results


# ============================================================
# 2. 板块资金流向扫描（近似同步信号核心）
# ============================================================
def scan_sectors(api):
    """扫描全行业板块资金流向 — 发现资金正在流入的板块

    关键逻辑：
    - 涨幅+成交额 = 资金确认方向（同步信号）
    - 涨幅大+成交额大 = 强资金流入（高确信度）
    - 涨幅大+成交额小 = 弱反弹（低确信度）
    - 跌幅大+成交额大 = 强资金流出（危险信号）
    """
    codes = [(1, c) for c in SECTOR_MAP.keys()]
    all_data = []

    # pytdx 每次最多80只
    for i in range(0, len(codes), 80):
        batch = codes[i:i + 80]
        quotes = api.get_security_quotes(batch)
        if not quotes:
            continue
        for q in quotes:
            chg = (q['price'] / q['last_close'] - 1) * 100 if q.get('last_close', 0) > 0 else 0
            amt = q.get('amount', 0) / 1e8
            all_data.append({
                'code': q['code'],
                'name': SECTOR_MAP.get(q['code'], ''),
                'chg_pct': round(chg, 2),
                'amount_yi': round(amt, 1),
                'price': q.get('price', 0),
            })

    # 资金流入强度 = 涨幅 × log(成交额+1)
    # 涨+量大 = 强流入, 跌+量大 = 强流出
    for s in all_data:
        s['flow_score'] = round(
            s['chg_pct'] * max(1, np.log1p(s['amount_yi'])), 2
        )

    return all_data


# ============================================================
# 3. 个股分时资金流推算（近似同步信号）
# ============================================================
def calc_intraday_flow(api, code):
    """推算个股分时资金流向 — 近似同步信号

    核心方法：5分钟K线量价分析
    - 收阳线（close >= open）= 资金流入，按成交额加权
    - 收阴线 = 资金流出
    - 净流入比 = (流入-流出)/总成交 × 100
    - 加速度 = 近3根K线净流入 vs 前3根净流入 的变化率

    返回:
        dict: 包含 net_pct(净流入比), accel(加速度), trend(趋势),
              recent_3_bars(最近3根K线资金流), total_bars(总K线数)
    """
    mkt = market_from_code(code)
    data = api.get_security_bars(0, mkt, code, 0, 48)
    if not data:
        return None

    df = api.to_df(data)
    today_str = datetime.now().strftime('%Y-%m-%d')
    today = df[df['datetime'].astype(str).str.startswith(today_str)]
    if today.empty:
        return None

    inflow = 0
    outflow = 0
    bar_flows = []

    for _, row in today.iterrows():
        bar_amt = row['amount'] / 1e4  # 万元
        if row['close'] >= row['open']:
            inflow += bar_amt
            bar_flows.append(bar_amt)
        else:
            outflow += bar_amt
            bar_flows.append(-bar_amt)

    total = inflow + outflow
    net_pct = (inflow - outflow) / total * 100 if total > 0 else 0

    # 加速度检测：最近3根 vs 前3根
    n = len(bar_flows)
    if n >= 6:
        recent_3 = sum(bar_flows[-3:])
        prev_3 = sum(bar_flows[-6:-3])
        accel = (recent_3 - prev_3) / max(1, abs(prev_3)) * 100 if prev_3 != 0 else 0
    elif n >= 3:
        recent_3 = sum(bar_flows[-3:])
        accel = recent_3 / max(1, sum(abs(f) for f in bar_flows[-3:])) * 100
    else:
        recent_3 = sum(bar_flows)
        accel = 0

    # 趋势判断
    if net_pct > 20 and accel > 0:
        trend = '强流入加速'
    elif net_pct > 10:
        trend = '资金流入'
    elif net_pct > 0:
        trend = '微流入'
    elif net_pct > -10:
        trend = '微流出'
    elif net_pct > -20:
        trend = '资金流出'
    else:
        trend = '强流出加速'

    return {
        'code': code,
        'inflow_wan': round(inflow / 1e4, 1),
        'outflow_wan': round(outflow / 1e4, 1),
        'net_pct': round(net_pct, 1),
        'accel': round(accel, 1),
        'trend': trend,
        'total_bars': n,
        'recent_3_flows': [round(f / 1e4, 2) for f in bar_flows[-3:]] if bar_flows else [],
    }


# ============================================================
# 3a. TDX真实资金流（TQLEX API直连）— 日频主力/超大单/大单
# ============================================================
def get_tdx_capital_flow(code, days=5):
    """从TQLEX API获取真实资金流向数据

    这是"近似同步信号"的核心数据源：
    - 主力净额占比: 当日主力资金净流入占成交额的百分比
    - 超大单净买入: 大资金(机构)方向
    - 3日连续流入: 强趋势确认
    - 流入→流出转换: 趋势转折点

    Returns:
        dict or None: 包含 main_force_pct, super_large_pct, large_pct,
                      consecutive_inflow, trend_3d, close_price
    """
    try:
        url = f"{TQLEX_URL}?Entry=TdxSharePCCW.tdxf10_gg_jyds"
        payload = {"Params": [code, "zjlx", ""]}
        resp = http_requests.post(url, json=payload, timeout=TQLEX_TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get('ErrorCode', -1) != 0:
            return None
        result_sets = data.get('ResultSets', [])
        if not result_sets or not result_sets[0].get('Content'):
            return None

        rows = result_sets[0]['Content']
        col_names = result_sets[0].get('ColName', [])

        # 解析列: rq, N001(主力净额), N002(主力占比%), N003(超大单净额),
        #          N004(超大单占比%), N005(大单净额), N006(大单占比%),
        #          N007(主买净额), N008(主买占比%), N015(收盘价)
        parsed = []
        for row in rows[:days]:
            if len(row) < 9:
                continue
            parsed.append({
                'date': row[0],
                'main_force_pct': float(row[2]) if row[2] else 0,
                'super_large_pct': float(row[4]) if row[4] else 0,
                'large_pct': float(row[6]) if row[6] else 0,
                'main_buy_pct': float(row[8]) if row[8] else 0,
                'close': float(row[9]) if len(row) > 9 and row[9] else 0,
            })

        if not parsed:
            return None

        # 计算连续流入天数
        consecutive_inflow = 0
        for p in parsed:
            if p['main_force_pct'] > 0:
                consecutive_inflow += 1
            else:
                break

        # 3日趋势: 最近3天主力净额变化方向
        if len(parsed) >= 3:
            p3 = [p['main_force_pct'] for p in parsed[:3]]
            if p3[0] > 0 and p3[1] > 0 and p3[2] > 0:
                trend_3d = '3日连续流入'
            elif p3[0] > 0 and p3[1] < 0:
                trend_3d = '流入反转(昨日流出→今日流入)'
            elif p3[0] < 0 and p3[1] > 0:
                trend_3d = '流出反转(昨日流入→今日流出)'
            elif p3[0] > 0:
                trend_3d = '今日流入'
            else:
                trend_3d = '今日流出'
        else:
            trend_3d = '数据不足'

        today = parsed[0]
        return {
            'code': code,
            'main_force_pct': today['main_force_pct'],
            'super_large_pct': today['super_large_pct'],
            'large_pct': today['large_pct'],
            'main_buy_pct': today['main_buy_pct'],
            'consecutive_inflow': consecutive_inflow,
            'trend_3d': trend_3d,
            'close': today['close'],
            'history': parsed,  # 最近N天明细
        }

    except Exception as e:
        return None


# ============================================================
# 3b. 日内形态检测 — 近似同步信号的关键
# ============================================================
def detect_intraday_patterns(api, code):
    """检测日内形态：V形反转/双底/恐慌末端/尾盘拉升

    核心逻辑：
    - V形反转: 先杀跌后快速拉回 = 主力吸筹的最强同步信号
    - 双底: 两次探底不破 = 支撑确认
    - 恐慌末端: 放量暴跌后量能骤降 = 抛售耗尽
    - 尾盘拉升: 14:00后放量上行 = 机构建仓

    Returns:
        dict: patterns列表 + best_pattern + pattern_score(0-20)
    """
    mkt = market_from_code(code)
    data = api.get_security_bars(0, mkt, code, 0, 48)
    if not data:
        return {'patterns': [], 'best_pattern': None, 'pattern_score': 0}

    df = api.to_df(data)
    today_str = datetime.now().strftime('%Y-%m-%d')
    today = df[df['datetime'].astype(str).str.startswith(today_str)]
    if len(today) < 6:
        return {'patterns': [], 'best_pattern': None, 'pattern_score': 0}

    today = today.sort_values('datetime').reset_index(drop=True)
    patterns = []

    # ── V形反转检测 ──
    # 找到当日最低点，检查之前跌了多少、之后涨了多少
    low_idx = today['low'].idxmin()
    if low_idx > 0 and low_idx < len(today) - 2:
        # 最低点前的最高 → 最低点
        pre_high = today.loc[:low_idx, 'high'].max()
        low_price = today.loc[low_idx, 'low']
        drop_pct = (pre_high - low_price) / pre_high * 100 if pre_high > 0 else 0

        # 最低点 → 之后的最高
        post_high = today.loc[low_idx:, 'high'].max()
        recover_pct = (post_high - low_price) / low_price * 100 if low_price > 0 else 0

        # V形判定：跌>1.5%，恢复>60%
        if drop_pct > 1.5 and recover_pct / drop_pct > 0.6:
            patterns.append({
                'type': 'V形反转',
                'emoji': '',
                'desc': f'先跌{drop_pct:.1f}%后涨{recover_pct:.1f}%(恢复率{recover_pct/drop_pct*100:.0f}%)',
                'score': min(20, int(10 + drop_pct * 2)),  # 跌得越深V形越强
                'drop_pct': round(drop_pct, 2),
                'recover_pct': round(recover_pct, 2),
            })

    # ── 双底检测 ──
    # 两次探底价差<0.5%
    if len(today) >= 10:
        lows = today.nsmallest(5, 'low')
        if len(lows) >= 2:
            first_low_idx = lows.iloc[0].name
            second_low_idx = lows.iloc[1].name
            if abs(first_low_idx - second_low_idx) >= 3:  # 至少间隔3根K线
                l1 = lows.iloc[0]['low']
                l2 = lows.iloc[1]['low']
                spread = abs(l1 - l2) / min(l1, l2) * 100
                if spread < 0.5:
                    patterns.append({
                        'type': '双底',
                        'emoji': '',
                        'desc': f'两次探底价差{spread:.2f}%(L1={l1:.2f} L2={l2:.2f})',
                        'score': 15,
                    })

    # ── 恐慌末端检测 ──
    # 前1/3放量暴跌，后2/3量能骤降
    n = len(today)
    if n >= 9:
        third = n // 3
        first_vol = today.iloc[:third]['vol'].mean()
        rest_vol = today.iloc[third:]['vol'].mean()
        first_chg = (today.iloc[third-1]['close'] - today.iloc[0]['open']) / today.iloc[0]['open'] * 100

        if first_chg < -1.5 and first_vol > rest_vol * 2.0:
            patterns.append({
                'type': '恐慌末端',
                'emoji': '→',
                'desc': f'前段跌{first_chg:.1f}%放量，后段量能降至{rest_vol/first_vol*100:.0f}%',
                'score': 12,
            })

    # ── 尾盘拉升检测 ──
    # 14:00后持续上行+量能放大
    today_copy = today.copy()
    today_copy['datetime'] = pd.to_datetime(today_copy['datetime'])
    today_copy['hour'] = today_copy['datetime'].dt.hour
    today_copy['minute'] = today_copy['datetime'].dt.minute
    late = today_copy[(today_copy['hour'] == 14) | ((today_copy['hour'] == 13) & (today_copy['minute'] >= 30))]
    if len(late) >= 3:
        late_open = late.iloc[0]['open']
        late_close = late.iloc[-1]['close']
        late_chg = (late_close - late_open) / late_open * 100 if late_open > 0 else 0
        late_avg_vol = late['vol'].mean()
        total_avg_vol = today_copy['vol'].mean()

        if late_chg > 0.5 and late_avg_vol > total_avg_vol * 1.3:
            patterns.append({
                'type': '尾盘拉升',
                'emoji': '',
                'desc': f'14点后涨{late_chg:.1f}%且放量(量比{late_avg_vol/total_avg_vol:.1f})',
                'score': min(18, int(10 + late_chg * 3)),
            })

    # 最佳形态
    best = max(patterns, key=lambda p: p['score']) if patterns else None
    pattern_score = best['score'] if best else 0

    return {
        'code': code,
        'patterns': patterns,
        'best_pattern': best,
        'pattern_score': pattern_score,
    }


# ============================================================
# 3c. 多信号共振评分 — 实现80%胜率预判的核心
# ============================================================
def calc_confluence_score(sector_score, capital_flow, intraday_flow,
                          pattern_result, tier=0):
    """多信号共振评分(0-100)

    核心理念：单个信号胜率有限，多信号共振时胜率飙升
    - 板块+个股共振: 板块资金流入 + 个股资金流入 = 高确信
    - 主力+散户共振: 超大单流入 + 主买净流入 = 机构+散户合力
    - 加速度+形态共振: 资金加速流入 + V形反转 = 最强同步信号
    - Tier加成: V10 T1信号本身胜率81%

    Args:
        sector_score: 板块flow_score (原始值，函数内归一化)
        capital_flow: get_tdx_capital_flow() 返回值 or None
        intraday_flow: calc_intraday_flow() 返回值 or None
        pattern_result: detect_intraday_patterns() 返回值 or None
        tier: V10信号tier (0=无信号, 1/2/3)

    Returns:
        dict: score(0-100), breakdown, level, action
    """
    breakdown = {}

    # ── 1. 板块资金流(0-20) ──
    if sector_score is not None:
        # flow_score 范围大约 -50~50, 归一化到0-20
        s_score = min(20, max(0, (sector_score + 10) / 60 * 20))
    else:
        s_score = 5  # 未知给中间值
    breakdown['sector'] = round(s_score, 1)

    # ── 2. TDX真实资金流(0-20) ──
    if capital_flow:
        mf = capital_flow['main_force_pct']
        sl = capital_flow['super_large_pct']
        ci = capital_flow['consecutive_inflow']

        # 主力净额占比评分
        if mf > 10:
            c_score = 18
        elif mf > 5:
            c_score = 15
        elif mf > 2:
            c_score = 10
        elif mf > 0:
            c_score = 6
        elif mf > -2:
            c_score = 4
        elif mf > -5:
            c_score = 2
        else:
            c_score = 0

        # 超大单加成
        if sl > 5:
            c_score = min(20, c_score + 3)
        elif sl > 2:
            c_score = min(20, c_score + 1)

        # 连续流入加成
        if ci >= 3:
            c_score = min(20, c_score + 2)
    else:
        c_score = 5  # 无数据给中间值
    breakdown['capital'] = round(c_score, 1)

    # ── 3. 分时资金流+加速度(0-20) ──
    if intraday_flow:
        net = intraday_flow['net_pct']
        acc = intraday_flow['accel']

        if net > 30 and acc > 50:
            i_score = 20  # 强流入+强加速
        elif net > 20 and acc > 0:
            i_score = 16  # 流入+加速
        elif net > 10:
            i_score = 12  # 温和流入
        elif net > 0:
            i_score = 8   # 微流入
        elif net > -10:
            i_score = 4   # 微流出
        else:
            i_score = 0   # 强流出

        # 加速度修正：即使净流入不高，加速度大也是好信号
        if acc > 100:
            i_score = min(20, i_score + 4)
        elif acc > 50:
            i_score = min(20, i_score + 2)
    else:
        i_score = 5
    breakdown['intraday'] = round(i_score, 1)

    # ── 4. 日内形态(0-20) ──
    p_score = pattern_result.get('pattern_score', 0) if pattern_result else 0
    breakdown['pattern'] = round(p_score, 1)

    # ── 5. V10信号tier(0-20) ──
    tier_scores = {1: 20, 2: 12, 3: 6, 0: 0}
    t_score = tier_scores.get(tier, 0)
    breakdown['tier'] = t_score

    # ── 总分 ──
    total = s_score + c_score + i_score + p_score + t_score

    # ── 共振加成：多个信号同时≥12时额外加分 ──
    high_signals = sum(1 for v in breakdown.values() if v >= 12)
    if high_signals >= 4:
        total = min(100, total + 10)  # 4信号共振+10
        breakdown['resonance_bonus'] = 10
    elif high_signals >= 3:
        total = min(100, total + 5)   # 3信号共振+5
        breakdown['resonance_bonus'] = 5
    else:
        breakdown['resonance_bonus'] = 0

    # ── 评级 ──
    if total >= CONFLUENCE_HIGH:
        level = ' 高置信度'
        action = '可下重注(T1仓位)'
    elif total >= CONFLUENCE_MEDIUM:
        level = ' 中等信号'
        action = '可下注(T2仓位)'
    elif total >= CONFLUENCE_LOW:
        level = ' 观察'
        action = '继续观察，不急下注'
    else:
        level = ' 弱信号'
        action = '不操作'

    return {
        'score': round(total, 1),
        'breakdown': breakdown,
        'level': level,
        'action': action,
    }


# ============================================================
# 3d. 盘前预热扫描 — 10:30/13:30自动巡检用
# ============================================================
def prescan_hotlist(api, top_n=30):
    """盘前预热扫描：识别正在setup的股票

    核心逻辑：
    - 不等14:50，提前发现资金正在流入但价格还没大幅上涨的股票
    - 这些股票到14:50最可能产生V10信号
    - 用分时资金流+TDX真实资金流做初筛

    Returns:
        list: 按潜力排序的候选股列表
    """
    # 1. 从CSI1000成分股中选成交额达标的股票（动态阈值）
    cons_file = str(CSI1000_SKILLS_DIR / '000852cons.xls')
    if not os.path.exists(cons_file):
        print('[ERROR] CSI1000成分股文件不存在')
        return []

    cons = pd.read_excel(cons_file)
    cons = cons.rename(columns={
        '成份券代码Constituent Code': 'code_raw',
        '成份券名称Constituent Name': 'name',
        '交易所Exchange': 'exchange',
    })
    cons['code'] = cons['code_raw'].astype(str).str.split('.').str[0].str.zfill(6)
    cons['market'] = cons['exchange'].apply(lambda x: 0 if '深圳' in str(x) else 1)
    cons = cons.drop_duplicates(subset=['market', 'code']).reset_index(drop=True)

    # 2. 批量获取行情，动态阈值筛选
    print(f'   扫描{len(cons)}只中证1000成分股...')
    all_quotes = []
    codes = [(int(r['market']), str(r['code'])) for _, r in cons.iterrows()]
    for i in range(0, len(codes), 80):
        batch = codes[i:i+80]
        try:
            quotes = api.get_security_quotes(batch)
            if quotes:
                for q in quotes:
                    chg = (q['price'] / q['last_close'] - 1) * 100 if q.get('last_close', 0) > 0 else 0
                    name_row = cons[cons['code'] == q['code']]
                    name = name_row.iloc[0]['name'] if len(name_row) > 0 else ''
                    all_quotes.append({
                        'code': q['code'], 'name': name,
                        'market': market_from_code(q['code']),
                        'price': q['price'],
                        'chg_pct': round(chg, 2),
                        'amount_yi': round(q.get('amount', 0) / 1e8, 1),
                        'last_close': q.get('last_close', 0),
                    })
        except Exception:
            pass

    # 按成交额排序，动态阈值筛选（与scanner_v10同步）
    # amount_yi单位是亿元，total_amt_yi是中证1000总成交额(亿)
    total_amt_yi = sum(q['amount_yi'] for q in all_quotes)
    if total_amt_yi > 5000:  # CSI1000 >5000亿=活跃
        amount_threshold_yi = 0.5  # 5千万即可
        regime = '活跃市'
    elif total_amt_yi < 2000:  # CSI1000 <2000亿=清淡
        amount_threshold_yi = 3.0  # 3亿才扫
        regime = '清淡市'
    else:
        amount_threshold_yi = 1.0  # 1亿
        regime = '正常市'

    filtered = [q for q in all_quotes if q['amount_yi'] >= amount_threshold_yi]
    filtered.sort(key=lambda x: x['amount_yi'], reverse=True)
    # 上下限
    if len(filtered) > 500:
        filtered = filtered[:500]
    elif len(filtered) < 100:
        filtered = sorted(all_quotes, key=lambda x: x['amount_yi'], reverse=True)[:100]

    print(f'   {regime} 总成交{total_amt_yi:.0f}亿 阈值>={amount_threshold_yi:.1f}亿 → 筛选{len(filtered)}只')

    # 3. 对筛选后的股票做分时资金流扫描
    hotlist = []
    for i, stock in enumerate(filtered):
        # 跳过涨停/跌停
        if stock['chg_pct'] > 9.5 or stock['chg_pct'] < -9.5:
            continue

        flow = calc_intraday_flow(api, stock['code'])
        if not flow:
            continue

        # 预热条件：净流入>10% 或 净流入>5%且加速
        if flow['net_pct'] > 10 or (flow['net_pct'] > 5 and flow['accel'] > 0):
            # 尝试获取TDX真实资金流
            capital = get_tdx_capital_flow(stock['code'], days=3)

            # 计算预热评分(简化版confluence)
            prescan_score = 0
            if flow['net_pct'] > 30:
                prescan_score += 30
            elif flow['net_pct'] > 20:
                prescan_score += 20
            elif flow['net_pct'] > 10:
                prescan_score += 10

            if flow['accel'] > 50:
                prescan_score += 20
            elif flow['accel'] > 0:
                prescan_score += 10

            if capital and capital['main_force_pct'] > 3:
                prescan_score += 25
            elif capital and capital['main_force_pct'] > 0:
                prescan_score += 10

            if capital and capital['consecutive_inflow'] >= 2:
                prescan_score += 15

            # 价格还未大涨（有空间）
            if -3 < stock['chg_pct'] < 3:
                prescan_score += 10

            hotlist.append({
                'code': stock['code'],
                'name': stock['name'],
                'price': stock['price'],
                'chg_pct': stock['chg_pct'],
                'amount_yi': stock['amount_yi'],
                'flow': flow,
                'capital': capital,
                'prescan_score': prescan_score,
            })

        if (i + 1) % 50 == 0:
            print(f'   已扫描 {i+1}/{len(filtered)}')

    # 按预热评分排序
    hotlist.sort(key=lambda x: x['prescan_score'], reverse=True)
    return hotlist[:top_n]


# ============================================================
# 4. 持仓盯盘
# ============================================================
def load_holding_from_track():
    """从track record加载当前持仓"""
    records = []
    if not os.path.exists(TRACK_FILE):
        return records
    with open(TRACK_FILE, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            if row.get('status') == 'holding':
                records.append(row)
    return records


def scan_holdings(api, track_records):
    """盯盘：持仓股实时状态 + 信号衰减预检"""
    if not track_records:
        return []

    codes = []
    for r in track_records:
        code = r.get('code', '')
        mkt = market_from_code(code)
        codes.append((mkt, code))

    quotes = api.get_security_quotes(codes)
    if not quotes:
        return []

    results = []
    for q in quotes:
        # 找到对应track record
        record = [r for r in track_records if r.get('code') == q['code']]
        r = record[0] if record else {}

        entry_price = float(r.get('entry_price', 0) or 0)
        chg_pct = (q['price'] / q['last_close'] - 1) * 100 if q.get('last_close', 0) > 0 else 0
        pnl_pct = (q['price'] / entry_price - 1) * 100 if entry_price > 0 else 0
        qty = int(r.get('quantity', 0) or 0)
        pnl_amount = (q['price'] - entry_price) * qty

        # 信号衰减快速预检
        alerts = []
        if chg_pct <= ALERT_HOLDING_DROP_PCT:
            alerts.append(f' 日内跌{abs(chg_pct):.1f}%超阈值')
        if chg_pct >= ALERT_HOLDING_SURGE_PCT:
            alerts.append(f' 日内涨{chg_pct:.1f}%急速拉升')
        if pnl_pct <= -5:
            alerts.append(f' 浮亏{abs(pnl_pct):.1f}%超5%')

        # 分时资金流
        flow = calc_intraday_flow(api, q['code'])

        results.append({
            'code': q['code'],
            'name': r.get('name', ''),
            'tier': r.get('tier', ''),
            'price': q['price'],
            'chg_pct': round(chg_pct, 2),
            'pnl_pct': round(pnl_pct, 2),
            'pnl_amount': round(pnl_amount, 0),
            'entry_price': entry_price,
            'quantity': qty,
            'buy_time': r.get('buy_time', ''),
            'mode': r.get('mode', ''),
            'alerts': alerts,
            'flow': flow,
        })

    return results


# ============================================================
# 5. 候选股资金流扫描（V10信号股）
# ============================================================
def scan_candidates(api):
    """扫描今日V10信号股的资金流 — 发现近似同步信号"""
    if not os.path.exists(SCAN_CSV):
        return []

    signals = []
    with open(SCAN_CSV, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            if row.get('mode', '') != 'no_signal':
                signals.append(row)

    if not signals:
        return []

    # 批量获取行情
    codes = []
    for r in signals:
        code = r.get('code', '')
        mkt = market_from_code(code)
        codes.append((mkt, code))

    results = []
    for i in range(0, len(codes), 80):
        batch = codes[i:i + 80]
        quotes = api.get_security_quotes(batch)
        if not quotes:
            continue
        for q in quotes:
            record = [r for r in signals if r.get('code') == q['code']]
            r = record[0] if record else {}
            chg_pct = (q['price'] / q['last_close'] - 1) * 100 if q.get('last_close', 0) > 0 else 0

            # 获取资金流
            flow = calc_intraday_flow(api, q['code'])

            results.append({
                'code': q['code'],
                'name': r.get('name', ''),
                'tier': r.get('tier', ''),
                'mode': r.get('mode', ''),
                'price': q['price'],
                'chg_pct': round(chg_pct, 2),
                'amount_yi': round(q.get('amount', 0) / 1e8, 1),
                'flow': flow,
            })

    # 按资金净流入比排序
    results.sort(key=lambda x: x['flow']['net_pct'] if x.get('flow') else -999, reverse=True)

    return results


# ============================================================
# 6. 综合告警判断
# ============================================================
def check_alerts(index_data, sector_data, holdings, candidates):
    """综合判断是否触发告警 — 近似同步信号的核心输出"""
    alerts = []

    # 大盘告警
    for idx in index_data:
        if idx['chg_pct'] <= ALERT_INDEX_DROP_PCT:
            alerts.append({
                'level': ' 严重',
                'type': '大盘暴跌',
                'msg': f"{idx['name']}跌{abs(idx['chg_pct']):.1f}%，全面防御，不开新仓",
                'action': '停止买入，评估持仓是否止损',
            })

    # 板块异动告警
    for s in sector_data:
        if s['chg_pct'] >= ALERT_SECTOR_SURGE_PCT and s['amount_yi'] >= 50:
            alerts.append({
                'level': ' 关注',
                'type': '板块资金异动',
                'msg': f"{s['name']}涨{s['chg_pct']:.1f}%成交{s['amount_yi']:.0f}亿，资金正在流入",
                'action': f"关注{s['name']}板块内中证1000成分股，可能产生V10信号",
            })
        if s['chg_pct'] <= -3 and s['amount_yi'] >= 50:
            alerts.append({
                'level': ' 风险',
                'type': '板块资金流出',
                'msg': f"{s['name']}跌{abs(s['chg_pct']):.1f}%成交{s['amount_yi']:.0f}亿，资金正在流出",
                'action': f"回避{s['name']}板块",
            })

    # 持仓告警
    for h in holdings:
        for a in h.get('alerts', []):
            if '日内跌' in a:
                action = '评估信号衰减，考虑smart-sell止损'
            elif '急速拉升' in a:
                action = '评估冲高回落风险，考虑正T锁利'
            else:
                action = '密切关注'
            alerts.append({
                'level': ' 关注',
                'type': '持仓异动',
                'msg': f"{h['code']} {h['name']} {a}",
                'action': action,
            })

    # 候选股资金流异动
    for c in candidates:
        flow = c.get('flow')
        if not flow:
            continue
        if flow['net_pct'] >= ALERT_NET_FLOW_RATIO and flow['accel'] > 0:
            alerts.append({
                'level': ' 机会',
                'type': '候选股资金加速流入',
                'msg': f"{c['code']} {c['name']} T{c['tier']} 净流入{flow['net_pct']:.0f}% 加速度{flow['accel']:.0f}%",
                'action': f"可能在14:50升级为买入信号",
            })

    return alerts


# ============================================================
# 输出格式化
# ============================================================
def print_capital_flow(capital):
    """输出TDX真实资金流"""
    if not capital:
        print('   无TDX资金流数据')
        return
    print(f"   TDX真实资金流 ({capital['code']})")
    mf = capital['main_force_pct']
    sl = capital['super_large_pct']
    lg = capital['large_pct']
    ci = capital['consecutive_inflow']
    print(f"     主力净额: {mf:+.2f}% | 超大单: {sl:+.2f}% | 大单: {lg:+.2f}%")
    print(f"     连续流入: {ci}天 | 3日趋势: {capital['trend_3d']}")
    # 历史明细
    if capital.get('history'):
        print(f"     近{len(capital['history'])}日:")
        for h in capital['history'][:5]:
            emoji = '' if h['main_force_pct'] > 0 else ''
            print(f"       {emoji} {h['date']} 主力{h['main_force_pct']:+.2f}% 超大单{h['super_large_pct']:+.2f}%")


def print_patterns(pattern_result):
    """输出日内形态检测结果"""
    if not pattern_result or not pattern_result.get('patterns'):
        print('   未检测到日内形态')
        return
    print(f"   日内形态 ({pattern_result['code']})")
    for p in pattern_result['patterns']:
        print(f"     {p['emoji']} {p['type']}: {p['desc']} (score={p['score']})")
    best = pattern_result.get('best_pattern')
    if best:
        print(f"     [BEST] 最强形态: {best['emoji']} {best['type']} (score={best['score']})")


def print_confluence(confluence):
    """输出多信号共振评分"""
    if not confluence:
        print('   无法计算共振评分')
        return
    bd = confluence['breakdown']
    print(f"   多信号共振评分: {confluence['score']:.0f}/100 — {confluence['level']}")
    print(f"     板块({bd.get('sector', 0):.0f}) + 资金流({bd.get('capital', 0):.0f}) + "
          f"分时({bd.get('intraday', 0):.0f}) + 形态({bd.get('pattern', 0):.0f}) + "
          f"tier({bd.get('tier', 0)})")
    if bd.get('resonance_bonus', 0) > 0:
        print(f"      共振加成: +{bd['resonance_bonus']}")
    print(f"     → {confluence['action']}")


def print_prescan(hotlist):
    """输出盘前预热扫描结果"""
    if not hotlist:
        print('\n 预热扫描: 未发现setup股票')
        return
    print(f'\n 盘前预热扫描 — 发现{len(hotlist)}只setup股')
    print('-' * 70)
    for i, h in enumerate(hotlist[:20]):
        flow = h.get('flow', {})
        capital = h.get('capital')
        cap_info = ''
        if capital:
            cap_info = f" 主力{capital['main_force_pct']:+.1f}%"
        print(f"  {i+1:2d}. {h['code']} {h['name']} "
              f"¥{h['price']:.2f} ({h['chg_pct']:+.2f}%) "
              f"净比{flow.get('net_pct', 0):+.1f}% 加速{flow.get('accel', 0):+.0f}%{cap_info} "
              f"[score={h['prescan_score']}]")
    print('-' * 70)


def print_index(data):
    """输出指数行情"""
    print('\n 大盘指数')
    print('-' * 60)
    for d in data:
        emoji = '' if d['chg_pct'] > 0 else '' if d['chg_pct'] < 0 else ''
        print(f"  {emoji} {d['name']:6s} {d['price']:>8.2f} ({d['chg_pct']:+.2f}%) 成交额{d['amount_yi']:.0f}亿")


def print_sectors(data, top_n=8):
    """输出板块资金流向"""
    # 按flow_score排序
    sorted_data = sorted(data, key=lambda x: x['flow_score'], reverse=True)

    print('\n 板块资金流向 (flow_score = 涨幅×log(成交额+1))')
    print('-' * 60)

    # 最强板块
    print('  资金最强:')
    for i, s in enumerate(sorted_data[:top_n]):
        emoji = '' if s['chg_pct'] > 2 else '' if s['chg_pct'] > 0 else ''
        print(f"    {emoji} {s['name']:6s} {s['chg_pct']:+5.2f}% 成交{s['amount_yi']:5.0f}亿 score={s['flow_score']:+.1f}")

    # 最弱板块
    print('  资金最弱:')
    for s in sorted_data[-3:]:
        emoji = '' if s['chg_pct'] < -2 else ''
        print(f"    {emoji} {s['name']:6s} {s['chg_pct']:+5.2f}% 成交{s['amount_yi']:5.0f}亿 score={s['flow_score']:+.1f}")


def print_holdings(data):
    """输出持仓盯盘"""
    if not data:
        print('\n 当前无持仓')
        return

    print('\n 持仓盯盘')
    print('-' * 60)
    for h in data:
        pnl_emoji = '' if h['pnl_pct'] >= 0 else ''
        time_info = f" @{h['buy_time']}" if h.get('buy_time') else ''
        print(f"  {pnl_emoji} {h['code']} {h['name']} T{h['tier']}")
        print(f"     ¥{h['price']:.2f} (日内{h['chg_pct']:+.2f}%) | "
              f"浮盈{h['pnl_pct']:+.2f}% (¥{h['pnl_amount']:+,.0f}){time_info}")
        # 资金流
        flow = h.get('flow')
        if flow:
            print(f"     资金流: {flow['trend']} | 净比{flow['net_pct']:+.1f}% | 加速度{flow['accel']:+.1f}%")
        # 告警
        if h.get('alerts'):
            for a in h['alerts']:
                print(f"     {a}")


def print_candidates(data):
    """输出候选股资金流"""
    if not data:
        print('\n 今日无V10信号股')
        return

    print('\n 候选股资金流 (按净流入比排序)')
    print('-' * 60)
    for c in data:
        flow = c.get('flow')
        if not flow:
            continue
        print(f"  {c['code']} {c['name']} T{c['tier']} {c['mode']}")
        print(f"     ¥{c['price']:.2f} (日内{c['chg_pct']:+.2f}%) 成交{c['amount_yi']:.1f}亿")
        print(f"     资金: {flow['trend']} | 净比{flow['net_pct']:+.1f}% | 加速{flow['accel']:+.1f}%")


def print_alerts(alerts):
    """输出告警"""
    if not alerts:
        print('\n 当前无告警')
        return

    print('\n 告警信号')
    print('=' * 60)
    for a in alerts:
        print(f"  {a['level']} [{a['type']}]")
        print(f"     {a['msg']}")
        print(f"     → {a['action']}")


# ============================================================
# 主函数
# ============================================================
def main():
    parser = argparse.ArgumentParser(description='V10 看门狗 v2 — 近似同步信号盯盘系统')
    parser.add_argument('--sector', action='store_true', help='仅板块资金扫描')
    parser.add_argument('--holding', action='store_true', help='仅持仓盯盘')
    parser.add_argument('--flow', type=str, help='指定股票代码做资金流分析')
    parser.add_argument('--alert', action='store_true', help='仅输出触发告警')
    parser.add_argument('--confluence', type=str, help='指定股票多信号共振评分')
    parser.add_argument('--pattern', type=str, help='指定股票日内形态检测')
    parser.add_argument('--prescan', action='store_true', help='盘前预热扫描(10:30/13:30)')
    args = parser.parse_args()

    now = datetime.now()
    print('=' * 60)
    print(f' V10 看门狗 v2 — 近似同步信号盯盘')
    print(f'   {now.strftime("%Y-%m-%d %H:%M:%S")} | 星期{["一","二","三","四","五","六","日"][now.weekday()]}')
    print('=' * 60)

    # 连接TDX
    api = connect_tdx()
    if not api:
        print(' 无法连接TDX行情服务器')
        return

    try:
        # ── 多信号共振评分 ──
        if args.confluence:
            code = args.confluence
            print(f'\n {code} 多信号共振评分')
            print('-' * 60)
            # 1. 实时价格
            mkt = market_from_code(code)
            quotes = api.get_security_quotes([(mkt, code)])
            chg_pct = 0
            if quotes:
                q = quotes[0]
                chg_pct = (q['price'] / q['last_close'] - 1) * 100 if q.get('last_close', 0) > 0 else 0
                print(f"  现价: ¥{q['price']:.2f} (日内{chg_pct:+.2f}%)")

            # 2. 板块资金流(简化：用该股所属行业)
            sector_score = None
            # 3. TDX真实资金流
            print('  [WAIT] 获取TDX真实资金流...')
            capital = get_tdx_capital_flow(code, days=5)
            print_capital_flow(capital)

            # 4. 分时资金流
            flow = calc_intraday_flow(api, code)
            if flow:
                print(f"   分时: {flow['trend']} | 净比{flow['net_pct']:+.1f}% | 加速{flow['accel']:+.1f}%")

            # 5. 日内形态
            pattern = detect_intraday_patterns(api, code)
            print_patterns(pattern)

            # 6. V10 tier (从扫描结果)
            tier = 0
            if os.path.exists(SCAN_CSV):
                with open(SCAN_CSV, encoding='utf-8-sig') as f:
                    for row in csv.DictReader(f):
                        if row.get('code') == code and row.get('mode') != 'no_signal':
                            tier = int(row.get('tier', 0))
                            break
            if tier > 0:
                print(f"   V10信号: T{tier}")

            # 7. 计算共振评分
            confluence = calc_confluence_score(sector_score, capital, flow, pattern, tier)
            print()
            print_confluence(confluence)

            api.disconnect()
            return

        # ── 日内形态检测 ──
        if args.pattern:
            code = args.pattern
            print(f'\n {code} 日内形态检测')
            print('-' * 60)
            # 实时价格
            mkt = market_from_code(code)
            quotes = api.get_security_quotes([(mkt, code)])
            if quotes:
                q = quotes[0]
                chg = (q['price'] / q['last_close'] - 1) * 100 if q.get('last_close', 0) > 0 else 0
                print(f"  现价: ¥{q['price']:.2f} (日内{chg:+.2f}%)")
            pattern = detect_intraday_patterns(api, code)
            print_patterns(pattern)
            api.disconnect()
            return

        # ── 盘前预热扫描 ──
        if args.prescan:
            print('\n 盘前预热扫描')
            print('-' * 60)
            hotlist = prescan_hotlist(api)
            print_prescan(hotlist)
            # 保存hotlist到文件供14:50决策用
            if hotlist:
                hotlist_file = os.path.join(DATA_DIR, 'v10_prescan_hotlist.json')
                # 精简后保存（去掉不可序列化的capital对象中的history）
                save_data = []
                for h in hotlist:
                    item = {k: v for k, v in h.items() if k != 'capital'}
                    if h.get('capital'):
                        c = h['capital']
                        item['capital_summary'] = {
                            'main_force_pct': c['main_force_pct'],
                            'super_large_pct': c['super_large_pct'],
                            'consecutive_inflow': c['consecutive_inflow'],
                            'trend_3d': c['trend_3d'],
                        }
                    save_data.append(item)
                with open(hotlist_file, 'w', encoding='utf-8') as f:
                    json.dump(save_data, f, ensure_ascii=False, indent=2)
                print(f'\n 预热结果已保存: {hotlist_file}')
            api.disconnect()
            return

        # ── 指定股票资金流分析(增强版) ──
        if args.flow:
            code = args.flow
            print(f'\n {code} 分时资金流分析')
            print('-' * 60)
            flow = calc_intraday_flow(api, code)
            if flow:
                # 获取实时价格
                mkt = market_from_code(code)
                quotes = api.get_security_quotes([(mkt, code)])
                if quotes:
                    q = quotes[0]
                    chg = (q['price'] / q['last_close'] - 1) * 100 if q.get('last_close', 0) > 0 else 0
                    print(f"  现价: ¥{q['price']:.2f} (日内{chg:+.2f}%)")
                print(f"  资金趋势: {flow['trend']}")
                print(f"  流入: {flow['inflow_wan']}亿  流出: {flow['outflow_wan']}亿")
                print(f"  净流入比: {flow['net_pct']:+.1f}%")
                print(f"  加速度: {flow['accel']:+.1f}%")
                print(f"  近3根5分钟K线资金: {flow['recent_3_flows']}")
                print(f"  总K线数: {flow['total_bars']}")

            # v2增强：追加TDX真实资金流
            print()
            capital = get_tdx_capital_flow(code, days=5)
            print_capital_flow(capital)

            # v2增强：追加日内形态
            print()
            pattern = detect_intraday_patterns(api, code)
            print_patterns(pattern)

            api.disconnect()
            return

        # ── 全量巡检 ──
        index_data = []
        sector_data = []
        holdings = []
        candidates = []

        if args.alert:
            # 仅告警模式 — 快速扫描
            index_data = scan_index(api)
            track_records = load_holding_from_track()
            if track_records:
                holdings = scan_holdings(api, track_records)
        else:
            # 全量扫描
            print('\n[WAIT] 扫描中...')
            index_data = scan_index(api)

            if not args.holding:
                sector_data = scan_sectors(api)

            track_records = load_holding_from_track()
            if track_records:
                holdings = scan_holdings(api, track_records)

            if not args.holding and not args.sector:
                candidates = scan_candidates(api)

        # 输出
        if not args.alert:
            print_index(index_data)
            if sector_data:
                print_sectors(sector_data)
            print_holdings(holdings)
            if candidates:
                print_candidates(candidates)

        # v2增强：持仓股追加TDX真实资金流
        if holdings and not args.alert:
            print('\n 持仓股TDX真实资金流')
            print('-' * 60)
            for h in holdings:
                code = h['code']
                capital = get_tdx_capital_flow(code, days=3)
                if capital:
                    mf = capital['main_force_pct']
                    sl = capital['super_large_pct']
                    ci = capital['consecutive_inflow']
                    trend = capital['trend_3d']
                    emoji = '' if mf > 0 else ''
                    print(f"  {emoji} {code} {h['name']} 主力{mf:+.2f}% 超大单{sl:+.2f}% {ci}日连续 {trend}")

        # v2增强：候选股追加共振评分
        if candidates and not args.alert:
            print('\n 候选股共振评分(快速)')
            print('-' * 60)
            for c in candidates[:10]:  # 只评分前10只
                flow = c.get('flow')
                if not flow:
                    continue
                # 快速评分（省略板块和形态，用已有数据）
                tier = int(c.get('tier', 0))
                confluence = calc_confluence_score(
                    sector_score=None,  # 无板块数据
                    capital_flow=None,  # 不逐个查TDX（太慢）
                    intraday_flow=flow,
                    pattern_result=None,  # 不做形态检测
                    tier=tier,
                )
                bd = confluence['breakdown']
                print(f"  {c['code']} {c['name']} T{tier} → "
                      f"score={confluence['score']:.0f} {confluence['level']} "
                      f"(分时{bd['intraday']:.0f}+tier{bd['tier']})")

        # 综合告警
        alerts = check_alerts(index_data, sector_data, holdings, candidates)
        print_alerts(alerts)

        # 操作建议
        if not args.alert:
            print('\n 操作建议')
            print('-' * 60)
            now_h = now.hour
            now_m = now.minute
            time_label = f"{now_h:02d}:{now_m:02d}"

            if now_h < 9 or (now_h == 9 and now_m < 30):
                print('  [WAIT] 盘前等待中，9:30开盘')
            elif now_h < 11 or (now_h == 11 and now_m <= 30):
                print(f'  [TIME] {time_label} 早盘时段 — 观察板块资金流向确认')
                if sector_data:
                    top = sorted(sector_data, key=lambda x: x['flow_score'], reverse=True)[0]
                    if top['flow_score'] > 10:
                        print(f'      {top["name"]}板块资金最强，关注其成分股')
            elif now_h < 13 or (now_h == 13 and now_m < 30):
                print(f'  [TIME] {time_label} 午休 — 策略梳理')
            elif now_h < 14 or (now_h == 14 and now_m < 30):
                print(f'  [TIME] {time_label} 午后 — 观察是否延续上午趋势')
                if index_data:
                    avg_chg = sum(d['chg_pct'] for d in index_data) / max(1, len(index_data))
                    if avg_chg < -1:
                        print('      大盘偏弱，下午注意防守')
            elif now_h == 14 and now_m < 50:
                print(f'  [TIME] {time_label} 预热阶段 — 为14:50决策做准备')
                if candidates:
                    strong = [c for c in candidates if c.get('flow') and c['flow']['net_pct'] > 20]
                    if strong:
                        print(f'      {len(strong)}只候选股资金强流入，14:50重点关注')
            elif now_h == 14 and now_m >= 50:
                print(f'  [TIME] {time_label} 决策时点 — V10信号确认 + confluence评分')
            elif now_h >= 15:
                print(f'  [TIME] {time_label} 已收盘 — 15:30收盘打磨')

    finally:
        api.disconnect()


if __name__ == '__main__':
    main()
