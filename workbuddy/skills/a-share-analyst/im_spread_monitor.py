#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中证1000期指（IM）基差实时监控
数据来源：东方财富网API（无需JS渲染）

用法：
  python im_spread_monitor.py          # 单次查询
  python im_spread_monitor.py --loop   # 循环监控（每60秒）
  python im_spread_monitor.py --alert -80  # 基差<-80点时告警
"""

import sys
import time
import json
import argparse
from datetime import datetime
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─────────────────────────────────────────
# 配置
# ─────────────────────────────────────────
IM_MAIN_CONTRACT = 'IMM'  # 东方财富主力连续代码
CSI1000_INDEX = '000852'  # 中证1000现货指数

# 东方财富API endpoints
SPOT_URL = f'http://push2.eastmoney.com/api/qt/stock/get?secid=1.{CSI1000_INDEX}&fields=f43,f44,f45,f46,f47,f48,f57,f58,f60,f107,f152,f162,f169,f170,f171'
# 期货用腾讯财经API（更可靠）
FUTURES_URL = 'https://qt.gtimg.cn/q=IMM'  # 腾讯财经：IM主连

# 告警阈值（基差=期货-现货，负值=贴水）
ALERT_THRESHOLDS = {
    'extreme_discount': -150,  # 极度贴水，可能反弹
    'deep_discount': -80,       # 深度贴水
    'normal': -50,              # 正常贴水
    'premium': 10,             # 升水（少见）
}

# ─────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────

def get_session():
    """创建带重试的requests session"""
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.headers.update({'User-Agent': 'Mozilla/5.0'})
    return session


def fetch_spot_price(session):
    """
    获取中证1000现货实时价格
    返回: {'price': float, 'change_pct': float, 'timestamp': str}
    """
    try:
        resp = session.get(SPOT_URL, timeout=5)
        data = resp.json()
        if data.get('rc') != 0 or 'data' not in data:
            return None
        
        d = data['data']
        return {
            'price': d.get('f43', 0) / 100.0,  # 最新价（分→元）
            'change_pct': d.get('f170', 0) / 100.0,  # 涨跌幅（百分比×100）
            'open': d.get('f46', 0) / 100.0,
            'high': d.get('f44', 0) / 100.0,
            'low': d.get('f45', 0) / 100.0,
            'vol': d.get('f47', 0),  # 成交量（手）
            'amount': d.get('f48', 0),  # 成交额（元）
            'timestamp': datetime.now().strftime('%H:%M:%S'),
        }
    except Exception as e:
        print(f'  [ERROR] 现货数据获取失败: {e}')
        return None


def fetch_futures_price(session):
    """
    获取IM主连期货实时价格
    使用腾讯财经API: https://qt.gtimg.cn/q=IMM
    返回: {'price': float, 'change_pct': float, 'holding': int, 'timestamp': str}
    """
    try:
        resp = session.get(FUTURES_URL, timeout=5)
        text = resp.text.strip()
        # 腾讯API返回格式: v_IMM="51~IM主连~8352.0~34.8~0.42%~..."
        if '="' in text:
            data_str = text.split('="')[1].rstrip('"')
            parts = data_str.split('~')
            if len(parts) >= 5:
                return {
                    'price': float(parts[3]),  # 最新价
                    'change': float(parts[4]),  # 涨跌值
                    'change_pct': float(parts[5].rstrip('%')) if parts[5] else 0.0,  # 涨跌幅
                    'open': float(parts[6]) if len(parts) > 6 else 0.0,
                    'high': float(parts[7]) if len(parts) > 7 else 0.0,
                    'low': float(parts[8]) if len(parts) > 8 else 0.0,
                    'vol': int(parts[9]) if len(parts) > 9 else 0,
                    'holding': int(float(parts[12])) if len(parts) > 12 else 0,  # 持仓量
                    'timestamp': datetime.now().strftime('%H:%M:%S'),
                }
        return None
    except Exception as e:
        print(f'  [ERROR] 期指数据获取失败: {e}')
        return None


def calc_spread(spot, futures):
    """
    计算基差和年化基差率
    基差 = 期货 - 现货（负值=贴水，正值=升水）
    """
    if not spot or not futures:
        return None
    
    basis = futures['price'] - spot['price']
    basis_pct = (basis / spot['price']) * 100
    
    # 年化基差率（假设主力合约剩余30天）
    days_to_expiry = 30  # 简化：IM2606约6月中旬到期
    annualized_basis = (basis / spot['price']) * (365 / days_to_expiry) * 100
    
    return {
        'basis': basis,
        'basis_pct': basis_pct,
        'annualized_basis': annualized_basis,
        'spot_price': spot['price'],
        'futures_price': futures['price'],
    }


def evaluate_signal(spread):
    """
    评估基差信号强度
    返回: {'signal': str, 'strength': int, 'action': str}
    """
    if not spread:
        return None
    
    basis = spread['basis']
    
    if basis <= ALERT_THRESHOLDS['extreme_discount']:
        return {
            'signal': '极度贴水',
            'strength': 3,
            'action': ' 空头过度悲观，现货可能反弹 → 考虑买入',
            'emoji': '',
        }
    elif basis <= ALERT_THRESHOLDS['deep_discount']:
        return {
            'signal': '深度贴水',
            'strength': 2,
            'action': ' 机构对冲情绪强，谨慎但可逢低布局',
            'emoji': '',
        }
    elif basis <= ALERT_THRESHOLDS['normal']:
        return {
            'signal': '正常贴水',
            'strength': 1,
            'action': ' 市场中性，按常规策略操作',
            'emoji': '',
        }
    elif basis >= ALERT_THRESHOLDS['premium']:
        return {
            'signal': '升水',
            'strength': 2,
            'action': ' 机构看多，期指溢价 → 跟随做多',
            'emoji': '',
        }
    else:
        return {
            'signal': '轻微贴水',
            'strength': 1,
            'action': ' 市场偏多，正常操作',
            'emoji': '',
        }


def print_report(spot, futures, spread, signal):
    """打印监控报告"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    print('=' * 60)
    print(f' 中证1000期指基差监控  {now}')
    print('=' * 60)
    
    if spot:
        print(f'\n 现货（000852）')
        print(f'  现价: {spot["price"]:.2f}  ({spot["change_pct"]:+.2f}%)')
        print(f'  今开: {spot["open"]:.2f}  最高: {spot["high"]:.2f}  最低: {spot["low"]:.2f}')
    
    if futures:
        print(f'\n 期指（IM主连）')
        print(f'  现价: {futures["price"]:.2f}  ({futures["change_pct"]:+.2f}%)')
        print(f'  今开: {futures["open"]:.2f}  最高: {futures["high"]:.2f}  最低: {futures["low"]:.2f}')
        if futures.get('holding'):
            print(f'  持仓量: {futures["holding"]/10000:.2f}万手')
    
    if spread:
        print(f'\n 基差分析')
        print(f'  基差: {spread["basis"]:.2f}点 ({spread["basis_pct"]:+.2f}%)')
        print(f'  年化基差率: {spread["annualized_basis"]:.2f}%')
        
        if signal:
            print(f'\n{signal["emoji"]} 信号: {signal["signal"]} (强度{signal["strength"]}/3)')
            print(f'  {signal["action"]}')
    
    print('=' * 60)
    print()


def save_history(spot, futures, spread, signal, history_file='~/.workbuddy/a-share-analyst/im_spread_history.json'):
    """保存历史数据（用于回测）"""
    import os
    history_file = os.path.expanduser(history_file)
    os.makedirs(os.path.dirname(history_file), exist_ok=True)
    
    record = {
        'timestamp': datetime.now().isoformat(),
        'spot': spot,
        'futures': futures,
        'spread': spread,
        'signal': signal,
    }
    
    # 追加模式
    history = []
    if os.path.exists(history_file):
        with open(history_file, 'r', encoding='utf-8') as f:
            history = json.load(f)
    
    history.append(record)
    
    # 只保留最近1000条
    history = history[-1000:]
    
    with open(history_file, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def monitor_once(alert_threshold=None):
    """单次监控"""
    session = get_session()
    
    print(f'[WAIT] {datetime.now().strftime("%H:%M:%S")} 获取数据...')
    
    spot = fetch_spot_price(session)
    futures = fetch_futures_price(session)
    spread = calc_spread(spot, futures)
    signal = evaluate_signal(spread) if spread else None
    
    print_report(spot, futures, spread, signal)
    
    # 告警检查
    if alert_threshold and spread:
        if spread['basis'] <= alert_threshold:
            print(f' 告警：基差 {spread["basis"]:.2f} 点，低于阈值 {alert_threshold}！')
            # TODO: 发邮件/微信告警
    
    # 保存历史
    save_history(spot, futures, spread, signal)
    
    return spread, signal


def monitor_loop(interval=60, alert_threshold=None):
    """循环监控"""
    print(f' 启动循环监控（间隔{interval}秒）... Ctrl+C 停止\n')
    
    try:
        while True:
            monitor_once(alert_threshold)
            time.sleep(interval)
    except KeyboardInterrupt:
        print('\n 监控已停止')


def main():
    parser = argparse.ArgumentParser(description='中证1000期指基差实时监控')
    parser.add_argument('--loop', action='store_true', help='循环监控模式')
    parser.add_argument('--interval', type=int, default=60, help='循环间隔（秒，默认60）')
    parser.add_argument('--alert', type=float, help='基差告警阈值（点，如-80）')
    args = parser.parse_args()
    
    if args.loop:
        monitor_loop(args.interval, args.alert)
    else:
        monitor_once(args.alert)


if __name__ == '__main__':
    main()
