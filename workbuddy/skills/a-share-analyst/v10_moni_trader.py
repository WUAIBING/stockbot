#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V10 + mx-moni 整合脚本

将V10扫描信号自动推送到妙想模拟组合：
1. 读取 v10_scan_full.csv
2. 按tier选择推荐标的
3. 在mx-moni模拟组合中买入
4. 灵活卖出：信号衰减随时走人，T+5只是兜底
   注意：A股T+1规则，当日买入最早T+1才能卖出（除部分ETF可T+0）
5. 记录战绩到 track_record.csv

用法:
  python v10_moni_trader.py --buy              # 按14:50决策买入
  python v10_moni_trader.py --sell             # 仅卖T+5到期持仓（兜底）
  python v10_moni_trader.py --smart-sell       # 智能卖出：信号衰减+T+5兜底
  python v10_moni_trader.py --status           # 查看当前持仓和战绩
  python v10_moni_trader.py --init 100000      # 重置组合（如需新账户）

注意：这是模拟组合，不涉及实盘资金
"""

import os
import sys
import csv
import json
import hashlib
import argparse
import subprocess
import time
import re
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from pytdx.hq import TdxHq_API

from market_resolver import build_today_exclusion_map, exclusion_reason_text
from mx_api_env import ensure_mx_runtime_env
from package_paths import DATA_DIR
from trading_calendar import previous_trading_day
from evolving_model import (
    model_summary as get_evolving_model_summary,
    rank_signals,
    record_decisions as record_model_decisions,
    refresh_model_state,
)


def _configure_stdio():
    """Force UTF-8 console output on Windows so emoji/status logs do not crash."""
    for stream_name in ('stdout', 'stderr'):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass


#region debug-point A:buywatch-1450-fail-server
_DEBUG_ROOT = Path(__file__).resolve().parents[2] / '.dbg'
_DEBUG_ENV_FILE = _DEBUG_ROOT / 'buywatch-1450-fail.env'


def _debug_emit_event(hypothesis_id: str, location: str, msg: str, data: dict) -> None:
    url = 'http://127.0.0.1:7777/event'
    session_id = 'buywatch-1450-fail'
    try:
        content = _DEBUG_ENV_FILE.read_text(encoding='utf-8')
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if line.startswith('DEBUG_SERVER_URL='):
                url = line.split('=', 1)[1].strip() or url
            elif line.startswith('DEBUG_SESSION_ID='):
                session_id = line.split('=', 1)[1].strip() or session_id
    except Exception:
        pass
    payload = {
        'sessionId': session_id,
        'runId': os.environ.get('TRAE_DEBUG_RUN_ID', 'pre-fix'),
        'hypothesisId': hypothesis_id,
        'location': location,
        'msg': msg,
        'data': data,
    }
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
            ),
            timeout=0.8,
        ).read()
    except Exception:
        pass
#endregion


_configure_stdio()


# #region debug-point B:main-strategy-chain-helper
def _main_strategy_chain_emit(hypothesis_id: str, location: str, msg: str, data: dict) -> None:
    env_path = Path(__file__).resolve().parent / '.dbg' / 'main-strategy-chain.env'
    url = 'http://127.0.0.1:7777/event'
    session_id = 'main-strategy-chain'
    try:
        if env_path.exists():
            content = env_path.read_text(encoding='utf-8', errors='replace')
            for raw_line in content.splitlines():
                line = raw_line.strip()
                if line.startswith('DEBUG_SERVER_URL='):
                    url = line.split('=', 1)[1].strip() or url
                elif line.startswith('DEBUG_SESSION_ID='):
                    session_id = line.split('=', 1)[1].strip() or session_id
    except Exception:
        pass
    payload = {
        'sessionId': session_id,
        'runId': os.environ.get('TRAE_DEBUG_RUN_ID', 'pre-fix'),
        'hypothesisId': hypothesis_id,
        'location': location,
        'msg': msg,
        'data': data,
    }
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
            ),
            timeout=0.8,
        ).read()
    except Exception:
        pass
# #endregion


# #region debug-point A:midday-review-debug-helper
def _midday_review_debug_emit(hypothesis_id, msg, **data):
    _env_path = Path(__file__).resolve().parent / '.dbg' / 'midday-review-stale.env'
    _url = 'http://127.0.0.1:7777/event'
    _session = 'midday-review-stale'
    try:
        if _env_path.exists():
            _content = _env_path.read_text(encoding='utf-8', errors='replace')
            for _line in _content.splitlines():
                if _line.startswith('DEBUG_SERVER_URL='):
                    _url = _line.split('=', 1)[1].strip() or _url
                elif _line.startswith('DEBUG_SESSION_ID='):
                    _session = _line.split('=', 1)[1].strip() or _session
    except Exception:
        pass
    try:
        _payload = {
            'sessionId': _session,
            'runId': 'pre-fix',
            'hypothesisId': hypothesis_id,
            'location': 'v10_moni_trader.py',
            'msg': f'[DEBUG] {msg}',
            'data': data,
            'ts': int(time.time() * 1000),
        }
        urllib.request.urlopen(
            urllib.request.Request(
                _url,
                data=json.dumps(_payload, ensure_ascii=False).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
            ),
            timeout=1.2,
        ).read()
    except Exception:
        pass
# #endregion

# 环境变量
def _midday_api_fail_debug_emit(hypothesis_id: str, msg: str, data: dict, *, location: str) -> None:
    env_path = Path(__file__).resolve().parent / '.dbg' / 'midday-api-fail.env'
    url = 'http://127.0.0.1:7788/event'
    session_id = 'midday-api-fail'
    try:
        if env_path.exists():
            content = env_path.read_text(encoding='utf-8', errors='replace')
            for raw_line in content.splitlines():
                line = raw_line.strip()
                if line.startswith('DEBUG_SERVER_URL='):
                    url = line.split('=', 1)[1].strip() or url
                elif line.startswith('DEBUG_SESSION_ID='):
                    session_id = line.split('=', 1)[1].strip() or session_id
    except Exception:
        pass
    payload = {
        'sessionId': session_id,
        'runId': os.environ.get('TRAE_DEBUG_RUN_ID', 'pre'),
        'hypothesisId': hypothesis_id,
        'location': location,
        'msg': msg,
        'data': data,
    }
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
            ),
            timeout=0.8,
        ).read()
    except Exception:
        pass


# #region debug-point A:mx-api-flap-debug-helper
def _mx_api_flap_debug_emit(hypothesis_id: str, msg: str, data: dict, *, location: str) -> None:
    env_path = Path(__file__).resolve().parent / '.dbg' / 'mx-api-flap.env'
    url = 'http://127.0.0.1:7777/event'
    session_id = 'mx-api-flap'
    try:
        if env_path.exists():
            content = env_path.read_text(encoding='utf-8', errors='replace')
            for raw_line in content.splitlines():
                line = raw_line.strip()
                if line.startswith('DEBUG_SERVER_URL='):
                    url = line.split('=', 1)[1].strip() or url
                elif line.startswith('DEBUG_SESSION_ID='):
                    session_id = line.split('=', 1)[1].strip() or session_id
    except Exception:
        pass
    payload = {
        'sessionId': session_id,
        'runId': os.environ.get('TRAE_DEBUG_RUN_ID', 'pre-fix'),
        'hypothesisId': hypothesis_id,
        'location': location,
        'msg': msg,
        'data': data,
    }
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
            ),
            timeout=0.8,
        ).read()
    except Exception:
        pass
# #endregion


_MX_RUNTIME_ENV = ensure_mx_runtime_env()
MX_APIKEY = _MX_RUNTIME_ENV.get('MX_APIKEY', '') or os.environ.get('MX_APIKEY', '')
MX_API_URL = _MX_RUNTIME_ENV.get('MX_API_URL', '') or os.environ.get('MX_API_URL', 'https://mkapi2.dfcfs.com/finskillshub')


# 数据目录
SCAN_CSV = str(DATA_DIR / 'v10_scan_full.csv')
SCAN_META_FILE = str(DATA_DIR / 'v10_scan_meta.json')
SCAN_LATEST_FILE = str(DATA_DIR / 'v10_scan_latest.json')
TRACK_FILE = str(DATA_DIR / 'v10_track_record.csv')
POSITION_STATE_FILE = str(DATA_DIR / 'v10_position_state.json')
NAV_FILE = str(DATA_DIR / 'v10_nav_history.csv')
SUMMARY_FILE = str(DATA_DIR / 'v10_account_summary_latest.json')
BALANCE_CACHE_FILE = str(DATA_DIR / 'v10_balance_cache.json')
POSITIONS_CACHE_FILE = str(DATA_DIR / 'v10_positions_cache.json')
ORDERS_CACHE_FILE = str(DATA_DIR / 'v10_orders_cache.json')
MIDDAY_REVIEW_FILE = str(DATA_DIR / 'v10_midday_review_latest.json')
MIDDAY_NODE_FILE = str(DATA_DIR / 'v10_midday_node_latest.json')
MIDDAY_GATE_FILE = str(DATA_DIR / 'v10_midday_gate_latest.json')
PM_GATE_FILE = str(DATA_DIR / 'v10_pm_gate_status.json')
CLOSE_NODE_FILE = str(DATA_DIR / 'v10_close_node_latest.json')
LEARNING_GATE_FILE = str(DATA_DIR / 'v10_learning_gate_status.json')
READ_ONLY_ENDPOINT_CACHE_MAX_AGE_SECONDS = 600
DAILY_EVOLUTION_BUNDLE_FILE = str(DATA_DIR / 'v10_daily_evolution_bundle_latest.json')
ENGINEERING_REVIEW_FILE = str(DATA_DIR / 'v10_engineering_review_latest.json')
ENGINEERING_MANUAL_INCIDENTS_FILE = str(DATA_DIR / 'v10_engineering_manual_incidents_latest.json')
TRADE_EPISODE_HISTORY_FILE = str(DATA_DIR / 'v10_trade_episode_history_latest.json')
LEARNING_ACTIONS_FILE = str(DATA_DIR / 'v10_learning_actions_latest.json')
REGIME_EXECUTION_HISTORY_FILE = str(DATA_DIR / 'v10_regime_execution_history.jsonl')
OPENING_TRADABILITY_FILE = str(DATA_DIR / 'opening_tradability_latest.json')
EXTERNAL_MARKET_REVIEW_FILE = str(DATA_DIR / 'v10_external_market_review_latest.json')
MODEL_DECISIONS_FILE = str(DATA_DIR / 'v10_model_decisions.jsonl')
AUTOMATION_STATUS_DIR = DATA_DIR / 'automation_status'
PHASE_HISTORY_DETAILED_FILE = str(AUTOMATION_STATUS_DIR / 'phase_history_detailed.csv')
EXTERNAL_MARKET_REVIEW_HISTORY_FILE = str(AUTOMATION_STATUS_DIR / 'external_market_review_history.jsonl')
LATEST_DECISION_STATUS_FILE = str(AUTOMATION_STATUS_DIR / 'latest_decision_status.json')
BACKTEST_SUMMARY_FILE = str(DATA_DIR / 'v10_summary.json')
TRADE_API_LOG_FILE = str(DATA_DIR / 'v10_trade_api_log.jsonl')
PENDING_ORDERS_FILE = str(DATA_DIR / 'v10_pending_orders.json')
PENDING_ORDERS_ARCHIVE_FILE = str(DATA_DIR / 'v10_pending_orders_archive.json')
SMART_SELL_RETRY_STATE_FILE = str(DATA_DIR / 'v10_smart_sell_retry_state.json')
SMART_SELL_SHARED_LOCK_TTL_SECONDS = 420
SMART_SELL_RATE_LIMIT_COOLDOWN_SECONDS = 35 * 60
SCAN_FRESHNESS_MINUTES = 20
DECISION_READY_MAX_WAIT_SECONDS = 90
DECISION_READY_POLL_SECONDS = 3
TRADE_MIN_INTERVAL_SECONDS = 2.0
TRADE_BUY_MIN_INTERVAL_SECONDS = 2.5
TRADE_SELL_MIN_INTERVAL_SECONDS = 3.5
TRADE_RETRYABLE_CODES = {'112'}
TRADE_MAX_RETRIES = 4
TRADE_RETRY_BASE_SECONDS = 2.5
TRADE_RETRY_JITTER_SECONDS = 0.4
TRADE_TAIL_RETRY_DELAY_SECONDS = 6.0
TRADE_RATE_LIMIT_GLOBAL_COOLDOWN_SECONDS = 8.0
TRADE_RATE_LIMIT_FINAL_FAILURE_COOLDOWN_SECONDS = 6.0
TRADE_OPENING_BURST_EXTRA_SECONDS = 0.8
TRADE_OPENING_BURST_RETRY_EXTRA_SECONDS = 1.8
TRADE_ACTION_MIN_INTERVALS = {
    'buy': 0.2,
    'sell': 0.3,
    'add_position': 0.8,
    'smart_sell': 0.8,
}
TRADE_ACTION_TAIL_RETRY_DELAYS = {
    'buy': 10.0,
    'sell': 12.0,
    'add_position': 18.0,
    'smart_sell': 15.0,
}
TRADE_ACTION_RATE_LIMIT_COOLDOWNS = {
    'buy': 2.0,
    'sell': 2.5,
    'add_position': 4.0,
    'smart_sell': 4.0,
}
TRADE_ACTION_MAX_RETRIES = {
    'buy': 4,
    'sell': 4,
    'add_position': 4,
    'smart_sell': 3,
}
TRADE_PHASE_MAX_RETRIES = {
    ('smart_sell', 'tail_retry'): 2,
    ('sell', 'tail_retry'): 2,
}
TRADE_PHASE_MIN_INTERVALS = {
    'primary': 0.0,
    'tail_retry': 0.8,
    'add_position': 0.3,
}
TRACK_MISMATCH_RATIO = 0.35
TRACK_MISMATCH_MIN_SHARES = 200
ADD_POSITION_MAX_HOLD_DAYS = 4
PENDING_STALE_MINUTES = 20
PENDING_REPRICE_RECHECK_MINUTES = 20
PENDING_CANCEL_BATCH_LIMIT = 8
PENDING_ARCHIVE_MAX_ITEMS = 500
ADD_POSITION_PENDING_CLEANUP_MAX_CANCEL = 2
ADD_POSITION_PENDING_CLEANUP_BUDGET_SECONDS = 15.0
ADD_POSITION_PENDING_CANCEL_TIMEOUT_SECONDS = 5.0
HIGH_PROFIT_TAKE_PROFIT_PCT = 15.0
MEDIUM_PROFIT_TAKE_PROFIT_PCT = 8.0
ADD_POSITION_BIG_MEAT_EARLY_PROFIT_PCT = 3.0
ADD_POSITION_BIG_MEAT_PROFIT_PCT = 6.0
HOLDING_BIG_MEAT_PROMOTE_PROFIT_PCT = 12.0
HOLDING_BIG_MEAT_STRONG_PROFIT_PCT = 20.0
HOLDING_BIG_MEAT_PROMOTE_SCORE_THRESHOLD = 7.2
HOLDING_BIG_MEAT_STRONG_SCORE_THRESHOLD = 9.0
HOLDING_BIG_MEAT_PRELOCK_PROFIT_PCT = 10.0
HOLDING_BIG_MEAT_PRELOCK_SCORE_THRESHOLD = 6.6
HOLDING_BIG_MEAT_NEAR_HIGH_RATIO = 0.965
HOLDING_BIG_MEAT_PULLBACK_LIMIT_PCT = 7.5
BIG_MEAT_CORE_HOLD_RATIO = 0.50
BIG_MEAT_CORE_HOLD_STRONG_RATIO = 0.60
BIG_MEAT_HOLD_LOCK_DAYS = 3
BIG_MEAT_STRONG_HOLD_LOCK_DAYS = 5
BIG_MEAT_CANDIDATE_PRELOCK_MAX_DECAY = 3.5
ADD_POSITION_BIG_MEAT_DAY_CHG_PCT = 3.0
ADD_POSITION_BIG_MEAT_FLOW_SCORE = 50.0
ADD_POSITION_BIG_MEAT_SECTOR_SCORE = 75.0
ADD_POSITION_BIG_MEAT_STOCK_SCORE = 65.0
ADD_POSITION_BIG_MEAT_TOTAL_SCORE = 62.0
ADD_POSITION_BIG_MEAT_TARGET_MULTIPLIER = 1.3
ADD_POSITION_BIG_MEAT_NEAR_HIGH_RATIO = 0.985
ADD_POSITION_BIG_MEAT_REBREAKOUT_RATIO = 0.995
ADD_POSITION_BIG_MEAT_INTRADAY_ANCHOR_RATIO = 0.997
ADD_POSITION_BIG_MEAT_SCORE_THRESHOLD = 5
MODE_CAPITAL_PROFILE_SAMPLE_SCALE = 4
MODE_CAPITAL_TARGET_MULTIPLIER_MIN = 0.85
MODE_CAPITAL_TARGET_MULTIPLIER_MAX = 1.20
MODE_CAPITAL_INITIAL_MULTIPLIER_MIN = 0.80
MODE_CAPITAL_INITIAL_MULTIPLIER_MAX = 1.20
MODE_CAPITAL_ADD_POSITION_TARGET_MAX = 1.45
MODE_CAPITAL_NOTE_THRESHOLD = 0.03
MIDDAY_NODE_TRIGGER_SLOT = '11:35'
MIDDAY_GATE_TRIGGER_SLOT = '13:00'
MIDDAY_NODE_HARD_DEADLINE = '13:05'
PM_REALTIME_CORRECTION_WINDOW = '13:00-13:05'
PM_STRONG_CONFIRM_MIN_CONFIDENCE = 0.68
REGIME_EXECUTION_RISK_ON_SIGNAL_FLOOR = 20
MIDDAY_RELEASE_SIGNAL_FLOOR = 20
MIDDAY_RELEASE_T1_FLOOR = 1
MIDDAY_RELEASE_T2_FLOOR = 3
PM_BUY_RESTRICTED_MODES = {'V9_full', 'near_kill+weekly+MA20', 'trend_only'}
PM_BUY_LIMITED_MODE_RATIO = 0.65
PM_BUY_DEFENSIVE_GLOBAL_RATIO = 0.50
PM_BUY_MAX_NEW_POSITIONS_LIMITED = 2
PM_BUY_MAX_NEW_POSITIONS_DEFENSIVE = 1
RECENT_REENTRY_LOSS_BLOCK_PCT = -4.0
RECENT_REENTRY_LOSS_BLOCK_DAYS = 3
RECENT_REENTRY_SEVERE_LOSS_BLOCK_PCT = -8.0
RECENT_REENTRY_SEVERE_LOSS_BLOCK_DAYS = 7
RECENT_REENTRY_SELL_PENALTY_DAYS = 2
RECENT_REENTRY_REPEAT_LOOKBACK_DAYS = 7
RECENT_REENTRY_REPEAT_COUNT_THRESHOLD = 2
RECENT_FAILURE_PENALTY_SCORE = 3.5
RECENT_SELL_PENALTY_SCORE = 2.0
RECENT_REPEAT_PENALTY_SCORE = 1.5
FRESH_OPPORTUNITY_LOOKBACK_DAYS = 20
FRESH_OPPORTUNITY_BONUS_SCORE = 1.8
ADD_POSITION_RESERVE_CASH_RATIO = 0.35
ADD_POSITION_RESERVE_CASH_MIN_RATIO = 0.08
ADD_POSITION_NON_AGGRESSIVE_MAX_ITEMS = 2
ADD_POSITION_UNDERPERFORMING_MODE_SKIP_EDGE = -0.5

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_WINDOW_SKIPPED = 2
EXIT_RUNTIME_ERROR = 3
EXIT_STALE_SCAN = 4
EXIT_NO_SIGNAL = 10
EXIT_NO_ACTION = 11
TRACK_FIELDNAMES = [
    'date', 'buy_time', 'code', 'name', 'tier', 'entry_price', 'quantity',
    'buy_amount', 'buy_order_ids', 'sell_date', 'sell_time', 'sell_price',
    'sell_order_id', 'pnl', 'pnl_pct', 'hold_days', 'status', 'mode',
    'build_note', 'target_amount', 'decision_id', 'decision_run_slot',
    'selected_reason_hash', 'close_reason',
    'big_meat_state', 'big_meat_score', 'big_meat_aggressive_score',
    'big_meat_reason', 'big_meat_window_tag', 'big_meat_first_seen_at',
    'big_meat_confirmed_at', 'big_meat_last_eval_at',
    'holding_big_meat_score', 'holding_big_meat_reason', 'holding_big_meat_promoted_at',
    'big_meat_hold_state', 'big_meat_core_qty', 'big_meat_trade_qty',
    'big_meat_hold_lock_until',
    'last_synced_at',
]

BIG_MEAT_STATE_CANDIDATE = 'big_meat_candidate'
BIG_MEAT_STATE_CONFIRMED = 'big_meat_confirmed'
BIG_MEAT_ACTION_HOLD_CORE = 'hold_core'
BIG_MEAT_ACTION_RISK_TRIM = 'risk_trim'
BIG_MEAT_ACTION_HARD_EXIT = 'hard_exit'
BIG_MEAT_STATE_MIN_DECAY_FOR_HARD_EXIT = 5.0
BIG_MEAT_RISK_TRIM_RATIO = 0.5
BIG_MEAT_CANDIDATE_OPENING_SHOCK_MAX_DECAY = 5.0
BIG_MEAT_OPENING_EXCEPTIONAL_CONFIRM_SCORE_BONUS = 2.0
BIG_MEAT_OPENING_EXCEPTIONAL_CONFIRM_AGGR_BONUS = 1.2
BIG_MEAT_OPENING_EXCEPTIONAL_DAY_CHG_PCT = 5.0
BIG_MEAT_BUY_POOL_CANDIDATE_NOTE = '[BIG_MEAT_CANDIDATE_POOL]'
BIG_MEAT_BUY_POOL_OBSERVE_NOTE = '[BIG_MEAT_OBSERVE_POOL]'
BIG_MEAT_BUY_SEED_T2_THRESHOLD = 4.8
BIG_MEAT_BUY_SEED_STRONG_THRESHOLD = 6.8
BIG_MEAT_BUY_T2_TARGET_RATIO = 1.00
BIG_MEAT_BUY_T2_INITIAL_RATIO = 1.00
BIG_MEAT_BUY_T2_STRONG_TARGET_RATIO = 1.08
BIG_MEAT_BUY_T2_STRONG_INITIAL_RATIO = 1.05
BIG_MEAT_BUY_T3_TARGET_RATIO = 0.82
BIG_MEAT_BUY_T3_INITIAL_RATIO = 0.85
LEARNING_BIG_MEAT_SUCCESS_PNL_PCT = 8.0
LEARNING_FALSE_SELECTION_NEG_PNL_PCT = -2.5
LEARNING_FALSE_SELECTION_SOFT_NEG_PNL_PCT = -1.0
LEARNING_CODE_COOLDOWN_DAYS = 4
LEARNING_CODE_STRONG_COOLDOWN_DAYS = 7

# 仓位配置 — 分批建仓，不满仓！
# position_pct: 每只股票满仓目标金额 = 总资产 × position_pct%
# initial_build_pct: 首次建仓比例（T+0买入时的比例，留子弹给T+1做反T或加仓）
# 只有超级大行情（V9_full+板块共振+量价齐升）才允许100%首仓
TIER_CONFIG = {
    1: {'position_pct': 10, 'initial_build_pct': 50, 'label': 'T1大肉', 'max_stocks': 3},
       # T1满仓10万/只，首次建仓50%=5万，先回到更稳健的试错节奏
    2: {'position_pct': 6,  'initial_build_pct': 60, 'label': 'T2候选', 'max_stocks': 5},
       # T2=大肉候选培养池：允许保留更完整底仓，等待候选->确认后放大
    3: {'position_pct': 3,  'initial_build_pct': 50, 'label': 'T3观察', 'max_stocks': 3},
       # T3=观察试错池：小仓验证，不占用大肉培养资源
}

# 超级大行情标志：当V9_full信号+多模式共振时可满仓首建
# 在信号CSV中 mode=='V9_full' 时自动检测
PARTIAL_ROLLBACK_DISABLE_FULL_V9_BUILD = True
PARTIAL_ROLLBACK_DISABLE_POSITIVE_CAPITAL_BIAS = True

# 每只股票买入金额 = 总资产 × position_pct%
# 例如：1M × 10% = 10万/只(T1), 1M × 6% = 6万/只(T2), 1M × 3% = 3万/只(T3)
# 由 do_buy() 动态计算，此处仅作默认值
BUY_AMOUNT_DEFAULT = 50000  # 默认5万/只


# Watchdog去重cache —— 5分钟内同code+quantity+price不重复下委托
# 解决14:00:38-48的6笔废单问题（watchdog重复推送/scanner 14:00再跑）
DEDUP_FILE = os.path.join(DATA_DIR, 'v10_dedup_cache.json')
DEDUP_WINDOW_SEC = 300  # 5分钟


def _load_dedup_cache():
    """加载去重cache"""
    if not os.path.exists(DEDUP_FILE):
        return []
    try:
        with open(DEDUP_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_dedup_cache(cache):
    """保存去重cache（原子写，防崩溃截断）"""
    tmp = DEDUP_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DEDUP_FILE)


def _is_duplicate(code, quantity, price, action='buy'):
    """5min内同code+quantity+price+action是否重复"""
    cache = _load_dedup_cache()
    now_ts = int(datetime.now().timestamp())
    # 顺手清理过期
    cache = [c for c in cache if now_ts - c.get('ts', 0) < DEDUP_WINDOW_SEC]
    for c in cache:
        if (c.get('code') == code
                and c.get('quantity') == quantity
                and abs(c.get('price', 0) - price) < 0.01
                and c.get('action') == action):
            return True
    return False


def _record_order(code, quantity, price, action='buy'):
    """记录订单到去重cache（清理后追加）"""
    cache = _load_dedup_cache()
    now_ts = int(datetime.now().timestamp())
    cache = [c for c in cache if now_ts - c.get('ts', 0) < DEDUP_WINDOW_SEC]
    cache.append({
        'code': code,
        'quantity': quantity,
        'price': round(_fnum(price, 0.0), 2),
        'action': action,
        'ts': now_ts,
    })
    _save_dedup_cache(cache)


# 信号强度聚合 —— 30min内同code被记录N次=市场反复推荐=信心加分
# 与5min去重互补：5min内=bug拦截，5-30min=信号仍在=加分
SIGNAL_WINDOW_SEC = 1800  # 30分钟


def _get_signal_strength(code, action='buy', window_sec=SIGNAL_WINDOW_SEC):
    """返回30min内同code+action被记录次数

    用途：
    - 加仓场景：strength>=2时给confluence加分（信心增强）
    - 新建仓场景：strength>=2时也轻量+5分（说明scanner反复推=强信号）
    - 超过30min视为新周期信号，count从1重置
    """
    cache = _load_dedup_cache()
    now_ts = int(datetime.now().timestamp())
    count = 0
    for c in cache:
        if (c.get('code') == code
                and c.get('action') == action
                and now_ts - c.get('ts', 0) <= window_sec):
            count += 1
    return count


def _apply_signal_bonus(confluence, strength):
    """信号累加→confluence加分

    strength=1: 0分 (单次信号=基线)
    strength=2: +10分 (二次确认=轻度加分)
    strength=3: +20分 (三次确认=强势)
    strength>=4: +30分封顶 (反复推=最强信号)
    上限100分保护
    """
    if strength <= 1:
        return confluence
    bonus = min((strength - 1) * 10, 30)
    return min(_inum(confluence, 0) + bonus, 100)


def _fnum(value, default=0.0):
    try:
        if value in (None, ''):
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _inum(value, default=0):
    try:
        if value in (None, ''):
            return int(default)
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


MARKET_TZ = timezone(timedelta(hours=8), name='UTC+08')


def _market_now():
    return datetime.now(MARKET_TZ)


def _market_today():
    return _market_now().strftime('%Y-%m-%d')


def _now_str():
    return _market_now().strftime('%Y-%m-%d %H:%M:%S')


_LAST_TRADE_API_TS = 0.0
_TRADE_API_COOLDOWN_UNTIL_TS = 0.0


def _throttle_trade_api(min_interval_seconds=None):
    global _LAST_TRADE_API_TS
    min_interval = _fnum(min_interval_seconds, TRADE_MIN_INTERVAL_SECONDS) or TRADE_MIN_INTERVAL_SECONDS
    now_ts = time.time()
    rate_limit_wait = max(_TRADE_API_COOLDOWN_UNTIL_TS - now_ts, 0.0)
    interval_wait = max(min_interval - (now_ts - _LAST_TRADE_API_TS), 0.0)
    wait_seconds = max(rate_limit_wait, interval_wait)
    if wait_seconds > 0:
        time.sleep(wait_seconds)
        now_ts = time.time()
    _LAST_TRADE_API_TS = now_ts


def _trade_result_code(result):
    return '' if not isinstance(result, dict) else str(result.get('code', ''))


def _trade_result_ok(result):
    return _trade_result_code(result) in ['0', 0, '200', 200]


def _annotate_trade_response(response, *, retry_attempts=0, min_interval_seconds=None):
    payload = dict(response) if isinstance(response, dict) else {}
    if not payload.get('message'):
        payload['message'] = '网络错误' if not response else str(payload.get('message', '') or '')
    payload['__retry_attempts'] = max(_inum(retry_attempts, 0), 0)
    payload['__trade_min_interval'] = round(
        _fnum(min_interval_seconds, TRADE_MIN_INTERVAL_SECONDS) or TRADE_MIN_INTERVAL_SECONDS,
        2,
    )
    return payload


def _resolve_trade_min_interval(base_interval_seconds, order_context=None):
    order_context = order_context or {}
    execution_phase = str(order_context.get('execution_phase', '')).strip()
    strategy_action = str(order_context.get('strategy_action', '')).strip()
    phase_extra = _fnum(TRADE_PHASE_MIN_INTERVALS.get(execution_phase, 0.0), 0.0)
    action_extra = _fnum(TRADE_ACTION_MIN_INTERVALS.get(strategy_action, 0.0), 0.0)
    opening_extra = TRADE_OPENING_BURST_EXTRA_SECONDS if _is_opening_burst_window() and strategy_action in {'add_position', 'smart_sell'} else 0.0
    return round(
        max(_fnum(base_interval_seconds, TRADE_MIN_INTERVAL_SECONDS), 0.0)
        + phase_extra
        + action_extra
        + opening_extra,
        2,
    )


def _is_opening_burst_window(now_dt=None):
    now_dt = now_dt or _market_now()
    time_tag = now_dt.strftime('%H:%M')
    return '09:30' <= time_tag < '10:00'


def _resolve_trade_rate_limit_sleep_seconds(attempt, order_context=None):
    order_context = order_context or {}
    strategy_action = str(order_context.get('strategy_action', '')).strip()
    execution_phase = str(order_context.get('execution_phase', '')).strip()
    base = (TRADE_RETRY_BASE_SECONDS * attempt) + (TRADE_RETRY_JITTER_SECONDS * attempt)
    action_extra = _fnum(TRADE_ACTION_MIN_INTERVALS.get(strategy_action, 0.0), 0.0)
    phase_extra = 0.8 if execution_phase == 'tail_retry' else 0.0
    opening_extra = (
        TRADE_OPENING_BURST_RETRY_EXTRA_SECONDS
        if _is_opening_burst_window() and strategy_action in {'add_position', 'smart_sell'}
        else 0.0
    )
    return round(base + action_extra + phase_extra + opening_extra, 2)


def _resolve_trade_rate_limit_cooldown_seconds(order_context=None, *, final_failure=False):
    order_context = order_context or {}
    strategy_action = str(order_context.get('strategy_action', '')).strip()
    cooldown = TRADE_RATE_LIMIT_GLOBAL_COOLDOWN_SECONDS + _fnum(
        TRADE_ACTION_RATE_LIMIT_COOLDOWNS.get(strategy_action, 0.0),
        0.0,
    )
    if _is_opening_burst_window() and strategy_action in {'add_position', 'smart_sell'}:
        cooldown += TRADE_OPENING_BURST_RETRY_EXTRA_SECONDS
    if final_failure:
        cooldown += TRADE_RATE_LIMIT_FINAL_FAILURE_COOLDOWN_SECONDS
    return round(cooldown, 2)


def _mark_trade_api_cooldown(cooldown_seconds):
    global _TRADE_API_COOLDOWN_UNTIL_TS
    cooldown = max(_fnum(cooldown_seconds, 0.0), 0.0)
    if cooldown <= 0:
        return
    _TRADE_API_COOLDOWN_UNTIL_TS = max(_TRADE_API_COOLDOWN_UNTIL_TS, time.time() + cooldown)


def _resolve_tail_retry_delay_seconds(strategy_action):
    strategy_action = str(strategy_action or '').strip()
    return round(
        max(
            TRADE_TAIL_RETRY_DELAY_SECONDS,
            _fnum(TRADE_ACTION_TAIL_RETRY_DELAYS.get(strategy_action, TRADE_TAIL_RETRY_DELAY_SECONDS), TRADE_TAIL_RETRY_DELAY_SECONDS),
        ),
        2,
    )


def _resolve_trade_max_retries(trade_meta=None):
    trade_meta = trade_meta or {}
    strategy_action = str(trade_meta.get('strategy_action', '')).strip()
    execution_phase = str(trade_meta.get('execution_phase', '')).strip()
    phase_override = TRADE_PHASE_MAX_RETRIES.get((strategy_action, execution_phase))
    if phase_override is not None:
        return max(1, _inum(phase_override, TRADE_MAX_RETRIES))
    action_value = TRADE_ACTION_MAX_RETRIES.get(strategy_action)
    if action_value is not None:
        return max(1, _inum(action_value, TRADE_MAX_RETRIES))
    return max(1, TRADE_MAX_RETRIES)


def _trade_log_retry_event(trade_meta, *, result_code, sleep_seconds, attempt, total_attempts):
    payload = {
        'logged_at': _now_str(),
        'event_type': 'retry_wait',
        'action': str((trade_meta or {}).get('action', '')).strip(),
        'code': str((trade_meta or {}).get('code', '')).zfill(6),
        'quantity': _inum((trade_meta or {}).get('quantity', 0), 0),
        'ref_price': round(_fnum((trade_meta or {}).get('ref_price', 0.0), 0.0), 4),
        'result_code': str(result_code or ''),
        'retry_attempt': _inum(attempt, 0),
        'retry_next_attempt': _inum(attempt, 0) + 1,
        'retry_total_attempts': _inum(total_attempts, 0),
        'sleep_seconds': round(_fnum(sleep_seconds, 0.0), 2),
        'execution_phase': str((trade_meta or {}).get('execution_phase', '')).strip(),
        'strategy_action': str((trade_meta or {}).get('strategy_action', '')).strip(),
    }
    _append_jsonl(TRADE_API_LOG_FILE, payload)


def _split_order_ids(raw):
    text = str(raw or '').strip()
    if not text:
        return []
    return [item for item in text.split('|') if item]


def _join_order_ids(order_ids):
    seen = []
    for order_id in order_ids:
        text = str(order_id or '').strip()
        if text and text not in seen:
            seen.append(text)
    return '|'.join(seen)


def _normalize_record(record):
    item = dict(record)
    for key in TRACK_FIELDNAMES:
        item.setdefault(key, '')
    item['code'] = str(item.get('code', '')).zfill(6)
    item['last_synced_at'] = item.get('last_synced_at') or _now_str()
    return item


def _read_json(path):
    if not os.path.exists(path):
        return {}
    for encoding in ('utf-8', 'utf-8-sig'):
        try:
            with open(path, encoding=encoding) as f:
                return json.load(f)
        except Exception:
            continue
    return {}


def _write_json_atomic(path, payload):
    json_path = Path(path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = json_path.with_suffix(json_path.suffix + '.tmp')
    with tmp_path.open('w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp_path.replace(json_path)


def _write_live_endpoint_cache(path, data):
    safe_data = json.loads(json.dumps(data, ensure_ascii=False, default=str))
    _write_json_atomic(path, {
        'cached_at': _now_str(),
        'cached_ts': int(time.time()),
        'data': safe_data,
    })


def _read_live_endpoint_cache(path, *, max_age_seconds=READ_ONLY_ENDPOINT_CACHE_MAX_AGE_SECONDS):
    payload = _read_json(path)
    if not isinstance(payload, dict):
        return None, None
    data = payload.get('data')
    cached_ts = _inum(payload.get('cached_ts', 0), 0)
    if cached_ts <= 0:
        return None, None
    age_seconds = max(0, int(time.time()) - cached_ts)
    if max_age_seconds > 0 and age_seconds > max_age_seconds:
        return None, age_seconds
    return data, age_seconds


def _rehydrate_cached_orders(items):
    restored = []
    for raw in items or []:
        order = dict(raw if isinstance(raw, dict) else {})
        ts = _inum(order.get('time', 0), 0)
        if ts > 0:
            order['datetime'] = datetime.fromtimestamp(ts)
        else:
            dt_raw = str(order.get('datetime', '')).strip()
            if dt_raw:
                try:
                    order['datetime'] = datetime.fromisoformat(dt_raw)
                except ValueError:
                    order['datetime'] = None
            else:
                order['datetime'] = None
        restored.append(order)
    return restored


def _append_jsonl(path, payload):
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(payload, ensure_ascii=False) + '\n')


def _read_jsonl(path, *, limit=0):
    jsonl_path = Path(path)
    if not jsonl_path.exists():
        return []
    rows = []
    try:
        with jsonl_path.open('r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []
    if limit > 0:
        return rows[-limit:]
    return rows


def _load_regime_execution_history(*, limit=240):
    latest_by_trade_date = {}
    for row in _read_jsonl(REGIME_EXECUTION_HISTORY_FILE, limit=limit):
        if not isinstance(row, dict):
            continue
        trade_date = _date_key(row.get('trade_date') or '')
        if not trade_date:
            continue
        latest_by_trade_date[trade_date] = row
    return [latest_by_trade_date[key] for key in sorted(latest_by_trade_date.keys())]


def _append_regime_execution_history(review, *, source='close_node'):
    review = review if isinstance(review, dict) else {}
    trade_date = _date_key(review.get('trade_date') or '')
    if not trade_date:
        return
    _append_jsonl(REGIME_EXECUTION_HISTORY_FILE, {
        'generated_at': _now_str(),
        'trade_date': trade_date,
        'source': str(source or '').strip() or 'close_node',
        'verdict': str(review.get('verdict', '')).strip(),
        'positive_sample': bool(review.get('positive_sample')),
        'label': str(review.get('label', '')).strip(),
        'score': _inum(review.get('score', 0), 0),
        'pressure_days': _inum(review.get('pressure_days', 0), 0),
        'high_risk_days': _inum(review.get('high_risk_days', 0), 0),
        'candidate_pool_exists': bool(review.get('candidate_pool_exists')),
        'stocks_with_signal': _inum(review.get('stocks_with_signal', 0), 0),
        'today_opened_count': _inum(review.get('today_opened_count', 0), 0),
        'today_closed_count': _inum(review.get('today_closed_count', 0), 0),
        'close_clean': bool(review.get('close_clean')),
        'defensive_intraday_confirmed': bool(review.get('defensive_intraday_confirmed')),
        'notes': [str(item).strip() for item in (review.get('notes') or []) if str(item).strip()][:10],
    })


def _read_csv_rows(path):
    csv_path = Path(path)
    if not csv_path.exists():
        return []
    for encoding in ('utf-8', 'utf-8-sig'):
        try:
            with csv_path.open('r', encoding=encoding, newline='') as f:
                return [dict(row) for row in csv.DictReader(f)]
        except Exception:
            continue
    return []


def _trade_date_from_run_slot(run_slot):
    text = str(run_slot or '').strip()
    if len(text) >= 10 and text[4] == '-' and text[7] == '-':
        return text[:10]
    return _market_today()


def _build_selected_reason_hash(item):
    payload = {
        'code': str(item.get('code', '')).zfill(6),
        'tier': _inum(item.get('tier', 0), 0),
        'mode': str(item.get('mode', '')).strip(),
        'score': round(_fnum(item.get('model_score', 0.0), 0.0), 4),
        'market': round(_fnum(item.get('model_market_score', 0.0), 0.0), 4),
        'sector': round(_fnum(item.get('model_sector_score', 0.0), 0.0), 4),
        'stock': round(_fnum(item.get('model_stock_score', 0.0), 0.0), 4),
        'flow': round(_fnum(item.get('model_flow_score', 0.0), 0.0), 4),
        'target_amount': round(_fnum(item.get('target_amount', 0.0), 0.0), 2),
    }
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
    ).hexdigest()[:16]


def _attach_decision_identity(item, run_slot):
    payload = dict(item or {})
    code = str(payload.get('code', '')).zfill(6)
    trade_date = _trade_date_from_run_slot(run_slot)
    payload['decision_run_slot'] = str(run_slot or '').strip()
    payload['decision_id'] = str(payload.get('decision_id') or f'{trade_date}|{payload["decision_run_slot"]}|{code}').strip()
    payload['selected_reason_hash'] = str(payload.get('selected_reason_hash') or _build_selected_reason_hash(payload)).strip()
    return payload


ORDER_STATUS_LABELS = {
    1: '未报',
    2: '已报',
    3: '部成',
    4: '已成',
    5: '部成待撤',
    6: '已报待撤',
    7: '部撤',
    8: '已撤',
    9: '废单',
    10: '撤单失败',
}

PENDING_ACTIVE_STATUSES = {'submitted', 'partial', 'cancel_pending', 'cancel_failed'}
PENDING_TERMINAL_STATUSES = {'filled', 'cancelled', 'rejected'}


def _order_status_label(status_code):
    return ORDER_STATUS_LABELS.get(_inum(status_code, 0), f'状态{_inum(status_code, 0)}')


def _pending_status_from_order(order):
    status_code = _inum((order or {}).get('status', 0), 0)
    trade_count = _inum((order or {}).get('trade_count', 0), 0)
    count = _inum((order or {}).get('count', 0), 0)
    label = _order_status_label(status_code)
    if status_code == 4 or (count > 0 and trade_count >= count):
        return 'filled', label
    if status_code == 3:
        return 'partial', label
    if status_code in {5, 6}:
        return 'cancel_pending', label
    if status_code in {7, 8}:
        return 'cancelled', label
    if status_code == 9:
        return 'rejected', label
    if status_code == 10:
        return 'cancel_failed', label
    return 'submitted', label


def _load_smart_sell_retry_state():
    payload = _read_json(SMART_SELL_RETRY_STATE_FILE)
    return payload if isinstance(payload, dict) else {}


def _save_smart_sell_retry_state(payload):
    _write_json_atomic(SMART_SELL_RETRY_STATE_FILE, payload if isinstance(payload, dict) else {})


def _clear_smart_sell_retry_state(code):
    code = str(code or '').zfill(6)
    payload = _load_smart_sell_retry_state()
    if code in payload:
        payload.pop(code, None)
        _save_smart_sell_retry_state(payload)


def _get_smart_sell_retry_state(code):
    code = str(code or '').zfill(6)
    payload = _load_smart_sell_retry_state()
    entry = payload.get(code)
    if not isinstance(entry, dict):
        return {}
    cooldown_until = _parse_dt(entry.get('cooldown_until'))
    if cooldown_until and cooldown_until > datetime.now():
        return entry
    if code in payload:
        payload.pop(code, None)
        _save_smart_sell_retry_state(payload)
    return {}


def _mark_smart_sell_rate_limit(code, quantity, *, cooldown_seconds=SMART_SELL_RATE_LIMIT_COOLDOWN_SECONDS):
    code = str(code or '').zfill(6)
    payload = _load_smart_sell_retry_state()
    prev = payload.get(code) if isinstance(payload.get(code), dict) else {}
    fail_count = _inum(prev.get('fail_count', 0), 0) + 1
    payload[code] = {
        'last_failed_at': _now_str(),
        'cooldown_until': (datetime.now() + timedelta(seconds=cooldown_seconds)).strftime('%Y-%m-%d %H:%M:%S'),
        'last_quantity': _inum(quantity, 0),
        'last_result_code': '112',
        'fail_count': fail_count,
    }
    _save_smart_sell_retry_state(payload)


def acquire_shared_phase_lock(lock_name, *, owner='', ttl_seconds=300):
    lock_path = Path(DATA_DIR) / f'{str(lock_name or "").strip() or "phase"}.lock.json'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    owner = str(owner or lock_name).strip() or str(lock_name or 'phase').strip() or 'phase'
    ttl_seconds = max(_fnum(ttl_seconds, 0.0), 1.0)
    now = datetime.now()
    payload = {
        'lock_name': str(lock_name or '').strip(),
        'owner': owner,
        'pid': os.getpid(),
        'acquired_at': now.strftime('%Y-%m-%d %H:%M:%S'),
        'expires_at': (now + timedelta(seconds=ttl_seconds)).strftime('%Y-%m-%d %H:%M:%S'),
    }
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = _read_json(str(lock_path))
            existing = existing if isinstance(existing, dict) else {}
            existing_acquired = _parse_dt(existing.get('acquired_at'))
            existing_expires = _parse_dt(existing.get('expires_at'))
            stale = not existing
            if existing_expires and existing_expires <= now:
                stale = True
            elif existing_acquired and (now - existing_acquired).total_seconds() > ttl_seconds:
                stale = True
            if stale:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                except Exception:
                    return {
                        'acquired': False,
                        'lock_file': str(lock_path),
                        'owner': str(existing.get('owner', '')).strip(),
                        'stale': True,
                    }
                continue
            return {
                'acquired': False,
                'lock_file': str(lock_path),
                'owner': str(existing.get('owner', '')).strip(),
                'pid': _inum(existing.get('pid', 0), 0),
                'acquired_at': str(existing.get('acquired_at', '')).strip(),
                'expires_at': str(existing.get('expires_at', '')).strip(),
                'stale': False,
            }
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            try:
                lock_path.unlink()
            except Exception:
                pass
            raise
        return {
            'acquired': True,
            'lock_file': str(lock_path),
            **payload,
        }


def release_shared_phase_lock(lock_name, *, owner=''):
    lock_path = Path(DATA_DIR) / f'{str(lock_name or "").strip() or "phase"}.lock.json'
    existing = _read_json(str(lock_path))
    existing = existing if isinstance(existing, dict) else {}
    if not existing:
        return False
    existing_owner = str(existing.get('owner', '')).strip()
    existing_pid = _inum(existing.get('pid', 0), 0)
    owner = str(owner or '').strip()
    if owner and owner != existing_owner:
        return False
    if existing_pid and existing_pid != os.getpid():
        return False
    try:
        lock_path.unlink()
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return False


def _load_today_tradability_exclusions():
    payload = _read_json(OPENING_TRADABILITY_FILE)
    if not isinstance(payload, dict):
        return {}
    return build_today_exclusion_map(payload)


def _is_excluded_today_for_trading(code, exclusions=None):
    exclusion_map = exclusions if exclusions is not None else _load_today_tradability_exclusions()
    return exclusion_map.get(str(code or '').zfill(6))


def _parse_dt(text, fmt='%Y-%m-%d %H:%M:%S'):
    value = str(text or '').strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, fmt)
    except ValueError:
        return None


def _is_time_in_window(text, start_hm, end_hm):
    dt = _parse_dt(text)
    if dt is None:
        return False
    current = (dt.hour, dt.minute)
    return start_hm <= current <= end_hm


def _normalize_sector_names(values, limit=8):
    items = []
    seen = set()
    for value in values or []:
        text = str(value or '').strip()
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
        if len(items) >= limit:
            break
    return items


def _load_opening_tradability_payload():
    payload = _read_json(OPENING_TRADABILITY_FILE)
    return payload if isinstance(payload, dict) else {}


def _build_opening_liquidity_snapshot(payload=None):
    payload = payload if isinstance(payload, dict) else _load_opening_tradability_payload()
    trade_date = str(payload.get('trade_date', '')).strip()
    records = payload.get('records', []) if isinstance(payload.get('records', []), list) else []
    record_count = _inum(payload.get('record_count', len(records)), len(records))
    excluded_today_count = _inum(payload.get('excluded_today_count', 0), 0)
    review_only_count = _inum(payload.get('review_only_count', 0), 0)
    generated_at = str(payload.get('generated_at', '')).strip()
    available = bool(payload) and str(payload.get('status', '')).strip() == 'ok'
    action_counts = {}
    sample_codes = []
    for item in records:
        action = str(item.get('executor_action', '')).strip() or str(item.get('tradability_status', '')).strip() or 'unknown'
        action_counts[action] = action_counts.get(action, 0) + 1
        if action not in {'allow_today', 'tradable_today'}:
            code = str(item.get('code', '')).zfill(6)
            if code and code not in sample_codes:
                sample_codes.append(code)
            if len(sample_codes) >= 8:
                break
    issue_ratio = round((excluded_today_count + review_only_count) / max(record_count, 1), 4) if record_count else 0.0
    if not available:
        verdict = 'unavailable'
    elif excluded_today_count <= 0 and review_only_count <= 0:
        verdict = 'clean'
    elif issue_ratio <= 0.03:
        verdict = 'mixed'
    else:
        verdict = 'fragile'
    notes = []
    if available and _is_time_in_window(generated_at, (9, 31), (9, 40)):
        notes.append('开盘流动性样本落在 09:31 窗口，可用于第一轮门控。')
    elif available:
        notes.append('开盘流动性样本不在 09:31 窗口内，应降低其对第一轮门控的权重。')
    if excluded_today_count > 0:
        notes.append(f'09:31 流动性门控排除了 {excluded_today_count} 只当日不可自动交易样本。')
    if review_only_count > 0:
        notes.append(f'另有 {review_only_count} 只样本仅允许观察，不宜直接自动参与。')
    return {
        'available': available,
        'trade_date': trade_date,
        'generated_at': generated_at,
        'status': str(payload.get('status', '')).strip(),
        'record_count': record_count,
        'excluded_today_count': excluded_today_count,
        'review_only_count': review_only_count,
        'issue_ratio': issue_ratio,
        'verdict': verdict,
        'in_0931_window': bool(available and _is_time_in_window(generated_at, (9, 31), (9, 40))),
        'excluded_today_codes': _dedupe_codes(payload.get('excluded_today_codes', []))[:10],
        'review_sample_codes': sample_codes[:8],
        'action_counts': action_counts,
        'notes': notes,
    }


def _load_external_market_review_payload():
    payload = _read_json(EXTERNAL_MARKET_REVIEW_FILE)
    return payload if isinstance(payload, dict) else {}


def _derive_external_review_window_tag(generated_at, explicit_tag=''):
    text = str(explicit_tag or '').strip()
    if text:
        return text
    dt = _parse_dt(generated_at)
    if dt is None:
        return ''
    if dt.hour < 9 or (dt.hour == 9 and dt.minute <= 31):
        return 'opening_0931'
    if dt.hour == 9 and dt.minute <= 40:
        return 'opening_confirmation'
    return 'daytime_followup'


def _build_external_market_context(payload=None):
    payload = payload if isinstance(payload, dict) else _load_external_market_review_payload()
    if not payload:
        return {
            'available': False,
            'trade_date': '',
            'generated_at': '',
            'window_tag': '',
            'risk_level': 'unknown',
            'a_share_bias': '',
            'headline': '',
            'negative_sectors': [],
            'neutral_sectors': [],
            'positive_sectors': [],
            'short_flow_monitor': {},
            'opening_anchor_break_monitor': {},
            'weekend_digest_monitor': {},
            'impact_summary': '',
            'source': '',
            'notes': ['尚未接入隔夜/开盘外部资讯复核文件。'],
        }
    risk_level = str(
        payload.get('risk_level')
        or payload.get('global_risk_level')
        or payload.get('market_risk_level')
        or payload.get('risk_bias')
        or 'unknown'
    ).strip().lower()
    a_share_bias = str(
        payload.get('a_share_bias')
        or payload.get('a_share_risk_bias')
        or payload.get('a_share_verdict')
        or ''
    ).strip()
    generated_at = str(payload.get('generated_at', '')).strip()
    notes = payload.get('notes', []) if isinstance(payload.get('notes', []), list) else []
    impact_summary = str(
        payload.get('impact_summary')
        or payload.get('a_share_impact_summary')
        or payload.get('summary')
        or ''
    ).strip()
    return {
        'available': True,
        'trade_date': str(payload.get('trade_date', '')).strip(),
        'generated_at': generated_at,
        'window_tag': _derive_external_review_window_tag(generated_at, payload.get('window_tag') or payload.get('window')),
        'risk_level': risk_level or 'unknown',
        'a_share_bias': a_share_bias,
        'headline': str(payload.get('headline') or payload.get('title') or '').strip(),
        'negative_sectors': _normalize_sector_names(
            payload.get('negative_sectors')
            or payload.get('a_share_negative_sectors')
            or payload.get('risk_sectors')
            or []
        ),
        'neutral_sectors': _normalize_sector_names(
            payload.get('neutral_sectors')
            or payload.get('watch_sectors')
            or []
        ),
        'positive_sectors': _normalize_sector_names(
            payload.get('positive_sectors')
            or payload.get('a_share_positive_sectors')
            or payload.get('support_sectors')
            or []
        ),
        'recommended_actions': payload.get('recommended_actions', {}) if isinstance(payload.get('recommended_actions', {}), dict) else {},
        'horizon_assessment': payload.get('horizon_assessment', {}) if isinstance(payload.get('horizon_assessment', {}), dict) else {},
        'short_flow_monitor': payload.get('short_flow_monitor', {}) if isinstance(payload.get('short_flow_monitor', {}), dict) else {},
        'opening_anchor_break_monitor': payload.get('opening_anchor_break_monitor', {}) if isinstance(payload.get('opening_anchor_break_monitor', {}), dict) else {},
        'weekend_digest_monitor': payload.get('weekend_digest_monitor', {}) if isinstance(payload.get('weekend_digest_monitor', {}), dict) else {},
        'impact_summary': impact_summary,
        'source': str(payload.get('source') or payload.get('provider') or '').strip(),
        'notes': [str(item).strip() for item in notes if str(item).strip()][:6],
    }


def _extract_order_id(result):
    data = (result or {}).get('data') or {}
    if not isinstance(data, dict):
        return ''
    for payload in (data, data.get('result') or {}):
        if not isinstance(payload, dict):
            continue
        for key in ('orderId', 'orderID'):
            value = str(payload.get(key, '') or '').strip()
            if value:
                return value
    return ''


def load_scan_context():
    latest = _read_json(SCAN_LATEST_FILE)
    csv_path = str(latest.get('scan_csv') or SCAN_CSV)
    meta_path = str(latest.get('scan_meta') or SCAN_META_FILE)
    meta = _read_json(meta_path) or _read_json(SCAN_META_FILE)
    run_time = _parse_dt(meta.get('run_time'))
    if run_time is None:
        try:
            run_time = datetime.fromtimestamp(Path(csv_path).stat().st_mtime)
        except OSError:
            run_time = None
    return {
        'csv_path': csv_path,
        'meta_path': meta_path,
        'meta': meta,
        'run_time': run_time,
    }


def validate_scan_freshness(*, max_age_minutes=SCAN_FRESHNESS_MINUTES):
    ctx = load_scan_context()
    csv_path = ctx['csv_path']
    run_time = ctx['run_time']
    if not os.path.exists(csv_path):
        return False, f"扫描快照不存在: {csv_path}", ctx
    if run_time is None:
        return False, f"无法判断扫描时间: {ctx['meta_path']}", ctx
    now = datetime.now()
    if run_time.date() != now.date():
        return False, f"扫描日期不是今天: {run_time.strftime('%Y-%m-%d %H:%M:%S')}", ctx
    age_minutes = (now - run_time).total_seconds() / 60
    if age_minutes > max_age_minutes:
        return False, f"扫描结果已过期 {age_minutes:.1f} 分钟: {csv_path}", ctx
    ctx['age_minutes'] = age_minutes
    return True, "", ctx


def get_scan_status(*, max_age_minutes=SCAN_FRESHNESS_MINUTES):
    ok, message, ctx = validate_scan_freshness(max_age_minutes=max_age_minutes)
    run_time = ctx.get('run_time')
    meta = ctx.get('meta', {}) or {}
    age_minutes = ctx.get('age_minutes')
    if age_minutes is None and run_time is not None:
        age_minutes = round((datetime.now() - run_time).total_seconds() / 60, 1)
    return {
        'is_fresh': ok,
        'message': message or 'ok',
        'run_time': run_time.strftime('%Y-%m-%d %H:%M:%S') if run_time else '',
        'age_minutes': age_minutes,
        'scan_csv': ctx.get('csv_path', ''),
        'scan_meta': ctx.get('meta_path', ''),
        'stocks_with_signal': _inum(meta.get('stocks_with_signal', 0), 0),
        'signals_by_tier': meta.get('signals_by_tier', {}),
    }


def _build_scan_snapshot_manifest(*, max_files=240):
    manifest = {}
    scan_dir = Path(DATA_DIR)
    dated_paths = sorted(scan_dir.glob('v10_scan_full.*_*.csv'))
    if len(dated_paths) > max_files:
        dated_paths = dated_paths[-max_files:]
    for path in dated_paths:
        suffix = path.name[len('v10_scan_full.'):-len('.csv')]
        trade_date = _date_key(suffix[:10])
        if not trade_date:
            continue
        current = manifest.get(trade_date)
        if current is None or path.name >= Path(current).name:
            manifest[trade_date] = str(path)
    return manifest


def _load_scan_snapshot_rows(*, trade_date='', scan_manifest=None):
    trade_date = _date_key(trade_date)
    manifest = scan_manifest if isinstance(scan_manifest, dict) else _build_scan_snapshot_manifest()
    csv_path = str((manifest or {}).get(trade_date, '')).strip()
    rows = _read_csv_rows(csv_path) if csv_path else []
    by_code = {}
    for row in rows:
        code = str(row.get('code', '')).zfill(6)
        if code:
            by_code[code] = row
    return {
        'trade_date': trade_date,
        'csv_path': csv_path,
        'rows': rows,
        'by_code': by_code,
    }


def wait_for_today_decision_ready(*, max_wait_seconds=DECISION_READY_MAX_WAIT_SECONDS, poll_seconds=DECISION_READY_POLL_SECONDS):
    now = _market_now()
    deadline = time.time() + max(0, max_wait_seconds)
    while True:
        payload = _read_json(LATEST_DECISION_STATUS_FILE) or {}
        status = str(payload.get('status', '')).strip().lower()
        started_at = _parse_dt(payload.get('started_at'))
        finished_at = _parse_dt(payload.get('finished_at'))
        trigger_slot = str(payload.get('trigger_slot', '')).strip()
        if finished_at and finished_at.date() == now.date() and status == 'ok':
            return True, '', payload
        if started_at and started_at.date() == now.date() and not finished_at:
            if time.time() < deadline:
                print(f" 等待当日 decision 完成: slot={trigger_slot or '-'} status={status or 'running'}")
                time.sleep(max(1, poll_seconds))
                continue
            return False, f"decision 阶段等待超时: {LATEST_DECISION_STATUS_FILE}", payload
        if finished_at and finished_at.date() == now.date() and status and status != 'ok':
            return False, f"decision 阶段未成功完成: status={status}", payload
        return True, '', payload


def _log_trade_api(action, code, quantity, ref_price, result, extra=None):
    extra = extra or {}
    payload = {
        'logged_at': _now_str(),
        'event_type': 'trade_result',
        'action': action,
        'code': str(code).zfill(6),
        'quantity': _inum(quantity, 0),
        'ref_price': round(_fnum(ref_price, 0.0), 4),
        'ok': _trade_result_ok(result),
        'result_code': _trade_result_code(result),
        'message': '' if not result else str(result.get('message', '')),
        'order_id': _extract_order_id(result),
        'retry_attempts': _inum((result or {}).get('__retry_attempts', 0), 0),
        'trade_min_interval': _fnum((result or {}).get('__trade_min_interval', 0.0), 0.0),
        'execution_phase': str(extra.get('execution_phase', '')).strip(),
        'strategy_action': str(extra.get('strategy_action', '')).strip(),
        'final_outcome': str(
            extra.get('final_outcome') or ('success' if _trade_result_ok(result) else 'failed')
        ).strip(),
        'raw': result or {},
    }
    _append_jsonl(TRADE_API_LOG_FILE, payload)


def load_pending_orders():
    payload = _read_json(PENDING_ORDERS_FILE)
    if isinstance(payload, list):
        return payload
    return []


def save_pending_orders(items):
    _write_json_atomic(PENDING_ORDERS_FILE, items if isinstance(items, list) else [])


def load_pending_orders_archive():
    payload = _read_json(PENDING_ORDERS_ARCHIVE_FILE)
    if isinstance(payload, list):
        return payload
    return []


def save_pending_orders_archive(items):
    _write_json_atomic(PENDING_ORDERS_ARCHIVE_FILE, items if isinstance(items, list) else [])


def _pending_archive_key(item):
    return '|'.join([
        str(item.get('recorded_at', '')).strip(),
        str(item.get('action', '')).strip(),
        str(item.get('code', '')).zfill(6),
        str(_inum(item.get('quantity', 0), 0)),
        str(item.get('order_id', '')).strip(),
    ])


def _append_pending_order_archive(items):
    items = [item for item in (items or []) if isinstance(item, dict)]
    if not items:
        return
    archive = load_pending_orders_archive()
    seen = {_pending_archive_key(item) for item in archive if isinstance(item, dict)}
    for item in items:
        key = _pending_archive_key(item)
        if key in seen:
            continue
        archive.append(item)
        seen.add(key)
    save_pending_orders_archive(archive[-PENDING_ARCHIVE_MAX_ITEMS:])


def _should_archive_pending_item(item, *, now, active_pos_map):
    if str(item.get('status', '')).strip() != 'stale':
        return ''
    recorded_at = _parse_dt(item.get('recorded_at'))
    if recorded_at is None or recorded_at.date() >= now.date():
        return ''
    if str(item.get('order_id', '')).strip():
        return ''
    if str(item.get('action', '')).strip() != 'sell':
        return ''
    code = str(item.get('code', '')).zfill(6)
    if code in active_pos_map:
        return ''
    return 'historical_stale_sell_without_order_id_and_no_active_position'


def register_pending_order(action, code, quantity, ref_price, order_id):
    items = load_pending_orders()
    items.append({
        'recorded_at': _now_str(),
        'action': str(action).strip(),
        'code': str(code).zfill(6),
        'quantity': _inum(quantity, 0),
        'ref_price': round(_fnum(ref_price, 0.0), 4),
        'order_id': str(order_id or '').strip(),
        'status': 'submitted',
        'filled_quantity': 0,
        'filled_at': '',
        'stale': False,
        'message': '',
    })
    save_pending_orders(items[-200:])


def refresh_pending_orders(*, orders=None, positions=None):
    items = load_pending_orders()
    if not items:
        return []
    orders = orders if orders is not None else get_orders()
    positions = positions if positions is not None else get_positions()
    active_pos_map = _active_position_map(positions)
    order_by_id = {
        str(order.get('id', '')).strip(): order
        for order in (orders or [])
        if str(order.get('id', '')).strip()
    }
    now = datetime.now()
    refreshed = []
    archived = []
    for item in items:
        order_id = str(item.get('order_id', '')).strip()
        code = str(item.get('code', '')).zfill(6)
        action = str(item.get('action', '')).strip()
        status = str(item.get('status', 'submitted')).strip() or 'submitted'
        filled_quantity = _inum(item.get('filled_quantity', 0), 0)
        message = str(item.get('message', '')).strip()
        stale = False
        raw_order_status = _inum(item.get('raw_order_status', 0), 0)
        raw_order_status_label = str(item.get('raw_order_status_label', '')).strip()
        order = order_by_id.get(order_id)
        if order:
            trade_count = _inum(order.get('trade_count', 0), 0)
            filled_quantity = max(filled_quantity, trade_count)
            status, order_message = _pending_status_from_order(order)
            raw_order_status = _inum(order.get('status', 0), 0)
            raw_order_status_label = order_message
            if order_message:
                message = order_message
        if action == 'buy' and code in active_pos_map and status not in PENDING_TERMINAL_STATUSES and filled_quantity <= 0:
            filled_quantity = _inum(active_pos_map[code].get('count', 0), 0)
            if filled_quantity > 0:
                status = 'filled'
                raw_order_status_label = raw_order_status_label or '持仓到账确认'
                message = raw_order_status_label
        recorded_at = _parse_dt(item.get('recorded_at'))
        if status not in PENDING_TERMINAL_STATUSES and recorded_at is not None:
            age_minutes = (now - recorded_at).total_seconds() / 60
            if age_minutes >= PENDING_STALE_MINUTES:
                stale = True
                status = 'stale'
                message = message or f'pending>{PENDING_STALE_MINUTES}m'
        refreshed_item = dict(item)
        refreshed_item.update({
            'status': status,
            'filled_quantity': filled_quantity,
            'filled_at': _now_str() if status == 'filled' and not str(item.get('filled_at', '')).strip() else item.get('filled_at', ''),
            'stale': stale,
            'message': message,
            'raw_order_status': raw_order_status,
            'raw_order_status_label': raw_order_status_label,
        })
        archive_reason = _should_archive_pending_item(refreshed_item, now=now, active_pos_map=active_pos_map)
        if archive_reason:
            archived_item = dict(refreshed_item)
            archived_item['archived_at'] = _now_str()
            archived_item['archive_reason'] = archive_reason
            archived.append(archived_item)
            continue
        refreshed.append(refreshed_item)
    if archived:
        _append_pending_order_archive(archived)
    save_pending_orders(refreshed[-200:])
    return refreshed[-200:]


def _refresh_live_artifact_state(records):
    balance = get_balance()
    positions = get_positions() or []
    orders = get_orders() or []
    pending_items = refresh_pending_orders(orders=orders, positions=positions)
    records, changed = sync_track_record(
        records,
        positions=positions,
        orders=orders,
        pending_items=pending_items,
    )
    records, full_changed, reconcile_summary = full_reconcile_positions(
        records,
        positions=positions,
        orders=orders,
        pending_items=pending_items,
    )
    if changed or full_changed:
        save_track_record(records)
    return {
        'balance': balance,
        'positions': positions,
        'orders': orders,
        'pending_items': pending_items,
        'records': records,
        'reconcile_summary': reconcile_summary,
    }


def summarize_pending_orders(items=None):
    items = items if items is not None else load_pending_orders()
    counts = {
        'submitted': 0,
        'partial': 0,
        'filled': 0,
        'stale': 0,
        'cancel_pending': 0,
        'cancelled': 0,
        'rejected': 0,
        'cancel_failed': 0,
    }
    active_codes = {'buy': [], 'sell': []}
    for item in items:
        status = str(item.get('status', '')).strip()
        if status in counts and status != 'submitted':
            counts[status] += 1
        if status in PENDING_ACTIVE_STATUSES:
            counts['submitted'] += 1
        action = str(item.get('action', '')).strip()
        if status in PENDING_ACTIVE_STATUSES and action in active_codes:
            active_codes[action].append(str(item.get('code', '')).zfill(6))
    return {
        'counts': counts,
        'active_buy_codes': sorted(set(active_codes['buy'])),
        'active_sell_codes': sorted(set(active_codes['sell'])),
    }


def _filled_pending_sell_map(items=None):
    items = items if items is not None else load_pending_orders()
    filled_map = {}
    for item in items:
        if str(item.get('action', '')).strip() != 'sell':
            continue
        if str(item.get('status', '')).strip() != 'filled':
            continue
        code = str(item.get('code', '')).zfill(6)
        recorded_at = _parse_dt(item.get('filled_at')) or _parse_dt(item.get('recorded_at')) or datetime.min
        previous = filled_map.get(code)
        previous_at = previous.get('_filled_dt', datetime.min) if isinstance(previous, dict) else datetime.min
        if recorded_at >= previous_at:
            payload = dict(item)
            payload['_filled_dt'] = recorded_at
            filled_map[code] = payload
    return filled_map


def _pending_remaining_quantity(item):
    quantity = _inum(item.get('quantity', 0), 0)
    filled = _inum(item.get('filled_quantity', 0), 0)
    return max(0, quantity - filled)


def _active_pending_context_by_code(items=None, *, action=None):
    items = items if items is not None else load_pending_orders()
    contexts = {}
    for item in items:
        status = str(item.get('status', '')).strip()
        item_action = str(item.get('action', '')).strip()
        if status not in PENDING_ACTIVE_STATUSES:
            continue
        if action and item_action != action:
            continue
        code = str(item.get('code', '')).zfill(6)
        remaining_qty = _pending_remaining_quantity(item)
        ctx = contexts.setdefault(code, {
            'reserved_qty': 0,
            'items': [],
        })
        ctx['reserved_qty'] += remaining_qty
        ctx['items'].append(item)
    return contexts


def _has_active_position(pos):
    return _inum(pos.get('count', 0), 0) > 0 or _inum(pos.get('avail_count', 0), 0) > 0


def _active_position_map(positions):
    active = {}
    for pos in positions or []:
        if not _has_active_position(pos):
            continue
        active[str(pos.get('code', '')).zfill(6)] = pos
    return active


def _record_trade_date(record):
    value = str(record.get('date', '')).strip()
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return None


def _has_native_strategy_context(record):
    record = record if isinstance(record, dict) else {}
    build_note = str(record.get('build_note', '')).strip()
    if '[LIVE_POSITION_ONLY]' in build_note:
        return False
    if str(record.get('decision_id', '')).strip():
        return True
    if str(record.get('decision_run_slot', '')).strip():
        return True
    if str(record.get('selected_reason_hash', '')).strip():
        return True
    if str(record.get('buy_order_ids', '')).strip():
        return True
    if _fnum(record.get('target_amount', 0.0), 0.0) > 0:
        return True
    mode = str(record.get('mode', '')).strip()
    tier = str(record.get('tier', '')).strip()
    return bool(tier and mode)


def _strip_reconcile_auto_tags(note):
    parts = [str(part).strip() for part in str(note or '').split(';') if str(part).strip()]
    filtered = [
        part for part in parts
        if not part.startswith('[AUTO_PAUSED]')
        and part != '[AUTO_IMPORTED] full_reconcile_from_positions'
    ]
    return '; '.join(filtered)


def _strip_live_position_only_tags(note):
    parts = [str(part).strip() for part in str(note or '').split(';') if str(part).strip()]
    filtered = [part for part in parts if not part.startswith('[LIVE_POSITION_ONLY]')]
    return '; '.join(filtered)


def _merge_build_notes(*parts):
    merged = []
    seen = set()
    for part in parts:
        for item in [str(token).strip() for token in str(part or '').split(';') if str(token).strip()]:
            if item in seen:
                continue
            seen.add(item)
            merged.append(item)
    return '; '.join(merged)


def _big_meat_window_rank(window_tag):
    tag = str(window_tag or '').strip()
    if not tag:
        return 0
    return {
        '09:36': 1,
        '10:28': 2,
        '13:28': 3,
    }.get(tag, 0)


def _resolve_big_meat_transition(record, profile, *, score_min=0.0, aggressive_score_min=0.0, window_tag=''):
    record = _normalize_record(record)
    profile = profile if isinstance(profile, dict) else {}
    score = _fnum(profile.get('score', 0.0), 0.0)
    aggressive_score = _fnum(profile.get('aggressive_score', 0.0), 0.0)
    today_chg_pct = _fnum(profile.get('today_chg_pct', 0.0), 0.0)
    eligible = bool(profile.get('eligible'))
    current_state = str(record.get('big_meat_state', '')).strip()
    prev_window_tag = str(record.get('big_meat_window_tag', '')).strip()
    current_rank = _big_meat_window_rank(window_tag)
    prev_rank = _big_meat_window_rank(prev_window_tag)
    same_day_eval = _state_eval_trade_date(record.get('big_meat_last_eval_at', '')) == datetime.now().strftime('%Y-%m-%d')
    immediate_exceptional_confirm = (
        current_rank == 1
        and eligible
        and score >= score_min + BIG_MEAT_OPENING_EXCEPTIONAL_CONFIRM_SCORE_BONUS
        and aggressive_score >= aggressive_score_min + BIG_MEAT_OPENING_EXCEPTIONAL_CONFIRM_AGGR_BONUS
        and today_chg_pct >= BIG_MEAT_OPENING_EXCEPTIONAL_DAY_CHG_PCT
        and bool(profile.get('strong_trend'))
        and bool(profile.get('near_day_high'))
        and bool(profile.get('intraday_anchor_hold'))
        and bool(profile.get('rebreakout'))
    )
    promote_from_candidate = (
        eligible
        and aggressive_score >= aggressive_score_min
        and current_state == BIG_MEAT_STATE_CANDIDATE
        and same_day_eval
        and current_rank > prev_rank > 0
    )
    keep_confirmed = (
        eligible
        and aggressive_score >= aggressive_score_min
        and current_state == BIG_MEAT_STATE_CONFIRMED
    )
    if immediate_exceptional_confirm or promote_from_candidate or keep_confirmed:
        return {
            'state': BIG_MEAT_STATE_CONFIRMED,
            'allow_add': True,
            'allow_aggressive_add': True,
            'reason': 'opening_exceptional_confirm' if immediate_exceptional_confirm else 'two_stage_confirm',
        }
    if score >= score_min:
        return {
            'state': BIG_MEAT_STATE_CANDIDATE,
            'allow_add': False,
            'allow_aggressive_add': False,
            'reason': 'candidate_watch',
        }
    return {
        'state': '',
        'allow_add': False,
        'allow_aggressive_add': False,
        'reason': 'below_threshold',
    }


def _apply_big_meat_state(record, *, state='', profile=None, reason='', window_tag=''):
    target = record if isinstance(record, dict) else {}
    item = _normalize_record(target)
    profile = profile if isinstance(profile, dict) else {}
    normalized_state = str(state or '').strip()
    now_text = _now_str()
    if normalized_state:
        if (
            normalized_state in {BIG_MEAT_STATE_CANDIDATE, BIG_MEAT_STATE_CONFIRMED}
            and not str(item.get('big_meat_first_seen_at', '')).strip()
        ):
            item['big_meat_first_seen_at'] = now_text
        if normalized_state == BIG_MEAT_STATE_CONFIRMED and not str(item.get('big_meat_confirmed_at', '')).strip():
            item['big_meat_confirmed_at'] = now_text
    else:
        item['big_meat_first_seen_at'] = ''
        item['big_meat_confirmed_at'] = ''
        item['holding_big_meat_score'] = ''
        item['holding_big_meat_reason'] = ''
        item['holding_big_meat_promoted_at'] = ''
        item['big_meat_hold_state'] = ''
        item['big_meat_core_qty'] = ''
        item['big_meat_trade_qty'] = ''
        item['big_meat_hold_lock_until'] = ''
    item['big_meat_state'] = normalized_state
    item['big_meat_score'] = f"{_fnum(profile.get('score', 0.0), 0.0):.2f}" if normalized_state else ''
    item['big_meat_aggressive_score'] = f"{_fnum(profile.get('aggressive_score', 0.0), 0.0):.2f}" if normalized_state else ''
    item['big_meat_reason'] = str(reason or profile.get('reason', '') or '').strip()
    item['big_meat_window_tag'] = str(window_tag or profile.get('window_tag', '') or '').strip()
    item['big_meat_last_eval_at'] = now_text if normalized_state else ''
    if isinstance(record, dict):
        record.clear()
        record.update(item)
        return record
    return item


def _state_eval_trade_date(text):
    stamp = str(text or '').strip()
    if not stamp:
        return ''
    return stamp.split(' ')[0]


def _big_meat_record_snapshot(record):
    record = _normalize_record(record)
    return (
        str(record.get('big_meat_state', '')).strip(),
        str(record.get('big_meat_score', '')).strip(),
        str(record.get('big_meat_aggressive_score', '')).strip(),
        str(record.get('big_meat_reason', '')).strip(),
        str(record.get('big_meat_window_tag', '')).strip(),
        str(record.get('big_meat_first_seen_at', '')).strip(),
        str(record.get('big_meat_confirmed_at', '')).strip(),
        str(record.get('holding_big_meat_score', '')).strip(),
        str(record.get('holding_big_meat_reason', '')).strip(),
        str(record.get('holding_big_meat_promoted_at', '')).strip(),
        str(record.get('big_meat_hold_state', '')).strip(),
        str(record.get('big_meat_core_qty', '')).strip(),
        str(record.get('big_meat_trade_qty', '')).strip(),
        str(record.get('big_meat_hold_lock_until', '')).strip(),
    )


def _resolve_big_meat_core_ratio(record=None, *, holding_profile=None):
    profile = holding_profile if isinstance(holding_profile, dict) else {}
    if bool(profile.get('dominant_winner')):
        return BIG_MEAT_CORE_HOLD_STRONG_RATIO
    record = record if isinstance(record, dict) else {}
    if _fnum(record.get('holding_big_meat_score', 0.0), 0.0) >= HOLDING_BIG_MEAT_STRONG_SCORE_THRESHOLD:
        return BIG_MEAT_CORE_HOLD_STRONG_RATIO
    return BIG_MEAT_CORE_HOLD_RATIO


def _sync_big_meat_position_split(record, *, qty=None, desired_core_ratio=None, reset_core=False):
    record = _normalize_record(record)
    total_qty = max(0, _inum(qty, _inum(record.get('quantity', 0), 0)))
    state = str(record.get('big_meat_state', '')).strip()
    if total_qty <= 0:
        record['big_meat_core_qty'] = ''
        record['big_meat_trade_qty'] = ''
        if state != BIG_MEAT_STATE_CONFIRMED:
            record['big_meat_hold_state'] = ''
        return record
    if state != BIG_MEAT_STATE_CONFIRMED:
        record['big_meat_core_qty'] = '0'
        record['big_meat_trade_qty'] = str(total_qty)
        if str(record.get('big_meat_hold_state', '')).strip() == BIG_MEAT_ACTION_HARD_EXIT:
            record['big_meat_hold_state'] = ''
        return record

    existing_core = _inum(record.get('big_meat_core_qty', 0), 0)
    if reset_core or existing_core <= 0:
        core_ratio = max(0.0, min(0.95, _fnum(desired_core_ratio, BIG_MEAT_CORE_HOLD_RATIO)))
        if total_qty < 100:
            core_qty = total_qty
        else:
            core_qty = _normalize_sell_quantity(max(int(total_qty * core_ratio), 100))
            if core_qty >= total_qty and total_qty >= 200:
                core_qty = _normalize_sell_quantity(total_qty - 100)
            core_qty = max(0, min(core_qty, total_qty))
    else:
        core_qty = max(0, min(existing_core, total_qty))
    trade_qty = max(0, total_qty - core_qty)
    if trade_qty > 0 and total_qty >= 200:
        trade_qty = _normalize_sell_quantity(trade_qty)
        core_qty = max(0, total_qty - trade_qty)
    record['big_meat_core_qty'] = str(core_qty)
    record['big_meat_trade_qty'] = str(trade_qty)
    if not str(record.get('big_meat_hold_state', '')).strip():
        record['big_meat_hold_state'] = BIG_MEAT_ACTION_HOLD_CORE
    return record


def _big_meat_trade_qty(record, *, qty=None):
    record = _normalize_record(record)
    total_qty = _inum(qty, _inum(record.get('quantity', 0), 0))
    trade_qty = _inum(record.get('big_meat_trade_qty', 0), 0)
    if trade_qty <= 0:
        core_qty = _inum(record.get('big_meat_core_qty', 0), 0)
        trade_qty = max(0, total_qty - max(0, min(core_qty, total_qty)))
    return max(0, min(trade_qty, total_qty))


def _risk_trim_quantity_for_record(record, qty):
    trade_qty = _big_meat_trade_qty(record, qty=qty)
    if trade_qty <= 0:
        return 0
    trimmed = _risk_trim_quantity(trade_qty)
    if trimmed <= 0:
        return min(trade_qty, qty)
    return min(trimmed, trade_qty, qty)


def _is_big_meat_hold_lock_active(record, *, trade_date=''):
    record = _normalize_record(record)
    until_text = _date_key(record.get('big_meat_hold_lock_until') or '')
    current_date = _date_key(trade_date or datetime.now().strftime('%Y-%m-%d'))
    if not until_text or not current_date:
        return False
    return current_date <= until_text


def _apply_holding_big_meat_profile(record, *, profile=None, promote=False, hold_state=''):
    record = _normalize_record(record)
    profile = profile if isinstance(profile, dict) else {}
    state = str(record.get('big_meat_state', '')).strip()
    now_text = _now_str()
    if state not in {BIG_MEAT_STATE_CANDIDATE, BIG_MEAT_STATE_CONFIRMED}:
        record['holding_big_meat_score'] = ''
        record['holding_big_meat_reason'] = ''
        record['holding_big_meat_promoted_at'] = ''
        record['big_meat_hold_state'] = ''
        record['big_meat_core_qty'] = ''
        record['big_meat_trade_qty'] = ''
        record['big_meat_hold_lock_until'] = ''
        return record

    score = _fnum(profile.get('holding_score', 0.0), 0.0)
    record['holding_big_meat_score'] = f"{score:.2f}" if score > 0 else str(record.get('holding_big_meat_score', '')).strip()
    reason = str(profile.get('reason', '')).strip()
    if reason:
        record['holding_big_meat_reason'] = reason
    if promote and not str(record.get('holding_big_meat_promoted_at', '')).strip():
        record['holding_big_meat_promoted_at'] = now_text
    if hold_state:
        record['big_meat_hold_state'] = str(hold_state).strip()
    elif state == BIG_MEAT_STATE_CONFIRMED and not str(record.get('big_meat_hold_state', '')).strip():
        record['big_meat_hold_state'] = BIG_MEAT_ACTION_HOLD_CORE

    if state == BIG_MEAT_STATE_CONFIRMED:
        record = _sync_big_meat_position_split(
            record,
            desired_core_ratio=_fnum(profile.get('core_ratio', 0.0), 0.0) or _resolve_big_meat_core_ratio(record, holding_profile=profile),
            reset_core=promote or _inum(record.get('big_meat_core_qty', 0), 0) <= 0,
        )
        profit_pct = _fnum(profile.get('profit_pct', 0.0), 0.0)
        lock_days = _inum(profile.get('hold_lock_days', 0), 0)
        if lock_days > 0 and (
            bool(profile.get('late_bloom_eligible'))
            or bool(profile.get('dominant_winner'))
            or profit_pct >= HOLDING_BIG_MEAT_PROMOTE_PROFIT_PCT
        ):
            target_text = (datetime.now() + timedelta(days=lock_days)).strftime('%Y-%m-%d')
            existing_until = _date_key(record.get('big_meat_hold_lock_until') or '')
            if not existing_until or existing_until < target_text:
                record['big_meat_hold_lock_until'] = target_text
    else:
        record = _sync_big_meat_position_split(record)
    return record


def _build_holding_big_meat_profile(record=None, *, profit_pct=0.0, add_profile=None, decision_row=None):
    record = record if isinstance(record, dict) else {}
    add_profile = add_profile if isinstance(add_profile, dict) else {}
    decision_row = decision_row if isinstance(decision_row, dict) else {}
    score = _fnum(add_profile.get('score', 0.0), 0.0)
    notes = []
    base_reason = str(add_profile.get('reason', '')).strip()
    if base_reason:
        notes.extend([part for part in re.split(r'[\\/;]+', base_reason) if part][:4])
    if profit_pct >= HOLDING_BIG_MEAT_PROMOTE_PROFIT_PCT:
        score += 1.2
        notes.append(f'浮盈{profit_pct:+.1f}%')
    elif profit_pct >= MEDIUM_PROFIT_TAKE_PROFIT_PCT:
        score += 0.6
        notes.append(f'浮盈扩张{profit_pct:+.1f}%')
    if profit_pct >= HOLDING_BIG_MEAT_STRONG_PROFIT_PCT:
        score += 1.4
        notes.append('赢家扩张')
    if str(record.get('big_meat_state', '')).strip() == BIG_MEAT_STATE_CONFIRMED:
        score += 0.8
        notes.append('已确认大肉')
    if bool(add_profile.get('strong_trend')):
        score += 0.9
        notes.append('强趋势')
    if bool(add_profile.get('weekly_up')):
        score += 0.6
        notes.append('周线向上')
    if bool(add_profile.get('near_day_high')):
        score += 0.7
        notes.append('贴近日高')
    if bool(add_profile.get('intraday_anchor_hold')):
        score += 0.5
        notes.append('站稳日内锚')
    if bool(add_profile.get('rebreakout')):
        score += 0.6
        notes.append('再突破')
    total_score = _fnum(add_profile.get('total_score', decision_row.get('score', 0.0)), 0.0)
    flow_score = _fnum(add_profile.get('flow_score', 0.0), 0.0)
    if total_score >= ADD_POSITION_BIG_MEAT_TOTAL_SCORE + 6:
        score += 0.6
        notes.append(f'总分{total_score:.1f}')
    if flow_score >= ADD_POSITION_BIG_MEAT_FLOW_SCORE + 8:
        score += 0.4
        notes.append(f'流{flow_score:.1f}')
    today_chg_pct = _fnum(add_profile.get('today_chg_pct', 0.0), 0.0)
    close_to_high = bool(add_profile.get('near_day_high')) or today_chg_pct >= ADD_POSITION_BIG_MEAT_DAY_CHG_PCT
    pullback_ok = abs(today_chg_pct) <= HOLDING_BIG_MEAT_PULLBACK_LIMIT_PCT
    score = round(max(score, 0.0), 2)
    dominant_winner = (
        profit_pct >= HOLDING_BIG_MEAT_STRONG_PROFIT_PCT
        and score >= HOLDING_BIG_MEAT_STRONG_SCORE_THRESHOLD
        and bool(add_profile.get('strong_trend'))
        and bool(add_profile.get('weekly_up'))
    )
    late_bloom_eligible = (
        profit_pct >= HOLDING_BIG_MEAT_PROMOTE_PROFIT_PCT
        and score >= HOLDING_BIG_MEAT_PROMOTE_SCORE_THRESHOLD
        and bool(add_profile.get('weekly_up'))
        and pullback_ok
        and (
            bool(add_profile.get('strong_trend'))
            or bool(add_profile.get('intraday_anchor_hold'))
            or bool(add_profile.get('rebreakout'))
            or close_to_high
        )
    )
    prelock_candidate = (
        not late_bloom_eligible
        and profit_pct >= HOLDING_BIG_MEAT_PRELOCK_PROFIT_PCT
        and score >= HOLDING_BIG_MEAT_PRELOCK_SCORE_THRESHOLD
        and bool(add_profile.get('weekly_up'))
        and pullback_ok
        and (
            bool(add_profile.get('strong_trend'))
            or bool(add_profile.get('intraday_anchor_hold'))
            or bool(add_profile.get('rebreakout'))
            or close_to_high
        )
    )
    if prelock_candidate:
        notes.append('临界大肉保护')
    return {
        'holding_score': score,
        'reason': '/'.join(notes[:8]),
        'late_bloom_eligible': bool(late_bloom_eligible),
        'dominant_winner': bool(dominant_winner),
        'prelock_candidate': bool(prelock_candidate),
        'core_ratio': BIG_MEAT_CORE_HOLD_STRONG_RATIO if dominant_winner else BIG_MEAT_CORE_HOLD_RATIO,
        'hold_lock_days': BIG_MEAT_STRONG_HOLD_LOCK_DAYS if dominant_winner else (BIG_MEAT_HOLD_LOCK_DAYS if late_bloom_eligible else 0),
        'profit_pct': round(_fnum(profit_pct, 0.0), 2),
        'allow_core_hold': bool(
            prelock_candidate
            or late_bloom_eligible
            or dominant_winner
            or str(record.get('big_meat_state', '')).strip() == BIG_MEAT_STATE_CONFIRMED
        ),
    }


def _resolve_big_meat_state_action(record, *, should_sell=False, decay_score=0.0, decay_reason='', holding_profile=None, learning_action=None):
    record = _normalize_record(record)
    holding_profile = holding_profile if isinstance(holding_profile, dict) else {}
    learning_action = learning_action if isinstance(learning_action, dict) else {}
    state = str(record.get('big_meat_state', '')).strip()
    if state not in {BIG_MEAT_STATE_CANDIDATE, BIG_MEAT_STATE_CONFIRMED}:
        return {'action': '', 'reason': '', 'state': state}
    reason_text = str(decay_reason or '').strip()
    opening_shock_only = (
        ('冲高回落上影线' in reason_text or '大阴线' in reason_text)
        and '趋势终结' not in reason_text
        and '连跌2日' not in reason_text
        and '放量滞涨' not in reason_text
    )
    state_trade_date = _state_eval_trade_date(record.get('big_meat_last_eval_at', ''))
    same_day_state = state_trade_date == datetime.now().strftime('%Y-%m-%d')
    if not should_sell:
        return {
            'action': BIG_MEAT_ACTION_HOLD_CORE if state == BIG_MEAT_STATE_CONFIRMED else '',
            'reason': f'{state} 当前未衰减，继续观察' if state == BIG_MEAT_STATE_CONFIRMED else '',
            'state': state,
        }
    candidate_prelock = (
        state == BIG_MEAT_STATE_CANDIDATE
        and bool(holding_profile.get('prelock_candidate'))
        and _fnum(decay_score, 0.0) <= BIG_MEAT_CANDIDATE_PRELOCK_MAX_DECAY
        and '趋势终结' not in reason_text
        and '连跌2日' not in reason_text
    )
    if candidate_prelock:
        return {
            'action': BIG_MEAT_ACTION_HOLD_CORE,
            'reason': f'{state} 接近 late_bloom 确认，先保护观察不做临门误杀',
            'state': state,
        }
    candidate_opening_shock_hold = (
        state == BIG_MEAT_STATE_CANDIDATE
        and (
            bool(holding_profile.get('prelock_candidate'))
            or (
                bool(learning_action.get('opening_shock_hold'))
                and _fnum(holding_profile.get('profit_pct', 0.0), 0.0) >= LEARNING_BIG_MEAT_SUCCESS_PNL_PCT
            )
        )
        and _is_opening_burst_window()
        and opening_shock_only
        and _fnum(decay_score, 0.0) <= BIG_MEAT_CANDIDATE_OPENING_SHOCK_MAX_DECAY
        and _fnum(holding_profile.get('profit_pct', 0.0), 0.0) >= LEARNING_BIG_MEAT_SUCCESS_PNL_PCT
    )
    if candidate_opening_shock_hold:
        return {
            'action': BIG_MEAT_ACTION_HOLD_CORE,
            'reason': f'{state} 开盘强震荡但未趋势终结，先保护观察等待二次确认',
            'state': state,
        }
    severe = (
        _fnum(decay_score, 0.0) >= BIG_MEAT_STATE_MIN_DECAY_FOR_HARD_EXIT
        or '趋势终结' in reason_text
        or '连跌2日' in reason_text
    )
    if severe:
        return {
            'action': BIG_MEAT_ACTION_HARD_EXIT,
            'reason': f'{state} 命中强衰减，转为 hard_exit',
            'state': state,
        }
    if state == BIG_MEAT_STATE_CONFIRMED and _is_big_meat_hold_lock_active(record):
        return {
            'action': BIG_MEAT_ACTION_HOLD_CORE,
            'reason': f'{state} 赢家冷静期内，普通衰减不卖 core',
            'state': state,
        }
    if state == BIG_MEAT_STATE_CONFIRMED and same_day_state:
        return {
            'action': BIG_MEAT_ACTION_HOLD_CORE,
            'reason': f'{state} 当日刚确认/加仓，普通衰减不卖 core',
            'state': state,
        }
    if state == BIG_MEAT_STATE_CONFIRMED and _big_meat_trade_qty(record) <= 0:
        return {
            'action': BIG_MEAT_ACTION_HOLD_CORE,
            'reason': f'{state} 当前仅剩 core，等待更强退出信号',
            'state': state,
        }
    if state == BIG_MEAT_STATE_CONFIRMED:
        return {
            'action': BIG_MEAT_ACTION_RISK_TRIM,
            'reason': f'{state} 强度回落，先 risk_trim 保留 core',
            'state': state,
        }
    return {
        'action': BIG_MEAT_ACTION_RISK_TRIM,
        'reason': f'{state} 候选失败，先 risk_trim 再观察',
        'state': state,
    }


def _risk_trim_quantity(qty):
    qty = _inum(qty, 0)
    if qty <= 0:
        return 0
    trimmed = _normalize_sell_quantity(max(int(qty * BIG_MEAT_RISK_TRIM_RATIO), 100))
    if trimmed <= 0:
        return 0
    if trimmed >= qty and qty >= 200:
        return _normalize_sell_quantity(qty - 100)
    return min(trimmed, qty)


def _is_legacy_holding_record(record):
    trade_date = _record_trade_date(record)
    if trade_date is None:
        return True
    return not _has_native_strategy_context(record)


def _track_qty_mismatch(record, pos):
    tracked_qty = _inum(record.get('quantity', 0), 0)
    actual_qty = _inum((pos or {}).get('count', 0), 0)
    if tracked_qty <= 0 or actual_qty <= 0:
        return False
    tolerance = max(TRACK_MISMATCH_MIN_SHARES, int(max(tracked_qty, actual_qty) * TRACK_MISMATCH_RATIO))
    return abs(actual_qty - tracked_qty) > tolerance


def _pause_record(record, reason):
    record = _normalize_record(record)
    record['status'] = 'paused'
    record['close_reason'] = reason
    note = str(record.get('build_note', '')).strip()
    pause_note = f"[AUTO_PAUSED] {reason}"
    if pause_note not in note:
        record['build_note'] = f"{note}; {pause_note}" if note else pause_note
    record['last_synced_at'] = _now_str()
    return record


def _normalize_sell_quantity(raw_qty):
    qty = _inum(raw_qty, 0)
    if qty <= 0:
        return 0
    if qty < 100:
        # Allow true odd-lot liquidation when the remaining position itself is below one board lot.
        return qty
    return int(qty / 100) * 100


def _position_broker_sellable_cap(pos):
    pos = pos or {}
    count = _inum(pos.get('count', 0), 0)
    raw_avail_count = pos.get('avail_count', None)
    if raw_avail_count in (None, ''):
        return count
    return max(0, min(count, _inum(raw_avail_count, 0)))


def _sellable_quantity(pos, tracked_qty=None):
    pos = pos or {}
    sellable = _position_broker_sellable_cap(pos)
    if tracked_qty is not None and tracked_qty > 0:
        sellable = min(sellable, tracked_qty)
    return _normalize_sell_quantity(sellable)


def _effective_sellable_quantity(pos, tracked_qty=None, pending_reserved_qty=0):
    pos = pos or {}
    count = _inum(pos.get('count', 0), 0)
    tracked_cap = _inum(tracked_qty, 0) if tracked_qty is not None else 0
    tracked_cap = tracked_cap if tracked_cap > 0 else count
    broker_cap = _position_broker_sellable_cap(pos)
    remaining_cap = max(0, tracked_cap - max(_inum(pending_reserved_qty, 0), 0))
    sellable = min(count, broker_cap, remaining_cap)
    return _normalize_sell_quantity(sellable)


def _should_reprice_pending_sell(code, pending_ctx, *, smart, tdx_api, entry_price, mode, profit_pct, current_price):
    items = (pending_ctx or {}).get('items', [])
    if not items:
        return False, '无活跃卖单'
    latest_item = max(items, key=lambda item: _parse_dt(item.get('recorded_at')) or datetime.min)
    recorded_at = _parse_dt(latest_item.get('recorded_at'))
    age_minutes = 0.0
    if recorded_at is not None:
        age_minutes = (datetime.now() - recorded_at).total_seconds() / 60
    if age_minutes < PENDING_REPRICE_RECHECK_MINUTES:
        return False, f'挂单等待{age_minutes:.1f}分钟，未到下一窗口重评估'

    weaker_reasons = []
    ref_price = _fnum(latest_item.get('ref_price', 0.0), 0.0)
    if ref_price > 0 and current_price > 0 and current_price < ref_price * 0.997:
        weaker_reasons.append(f'现价{current_price:.2f}低于原参考价{ref_price:.2f}')
    if smart and tdx_api:
        should_sell, decay_reason, decay_score = evaluate_signal_decay(
            tdx_api, code, entry_price, mode, profit_pct=profit_pct
        )
        if should_sell or decay_score >= 3:
            weaker_reasons.append(f'信号更弱:{decay_reason}')
    if not weaker_reasons:
        return False, f'挂单等待{age_minutes:.1f}分钟，但盘面未见进一步转弱'
    return True, f'挂单等待{age_minutes:.1f}分钟，且{"；".join(weaker_reasons)}'


def _normalize_order(order):
    price_dec = _inum(order.get('priceDec', 2), 2)
    trade_price_raw = order.get('tradePrice', 0)
    if trade_price_raw in (None, ''):
        trade_price_raw = 0
    trade_price = _fnum(trade_price_raw) / (10 ** price_dec) if _fnum(trade_price_raw) else 0.0
    order_price = _fnum(order.get('price', 0)) / (10 ** price_dec) if _fnum(order.get('price', 0)) else 0.0
    return {
        'id': str(order.get('id', '')).strip(),
        'code': str(order.get('secCode', '')).zfill(6),
        'name': str(order.get('secName', '')).strip(),
        'direction': _inum(order.get('drt', 0), 0),
        'status': _inum(order.get('status', 0), 0),
        'count': _inum(order.get('count', 0), 0),
        'trade_count': _inum(order.get('tradeCount', 0), 0),
        'price': order_price,
        'actual_trade_price': trade_price,
        'trade_price': trade_price if trade_price > 0 else order_price,
        'time': _inum(order.get('time', 0), 0),
        'datetime': datetime.fromtimestamp(_inum(order.get('time', 0), 0)) if _inum(order.get('time', 0), 0) > 0 else None,
    }


def get_orders(flt_order_drt=0, flt_order_status=0):
    result = api_request('/api/claw/mockTrading/orders', {
        'fltOrderDrt': flt_order_drt,
        'fltOrderStatus': flt_order_status,
    })
    # #region debug-point D:orders-fetch
    _mx_api_flap_debug_emit(
        'D',
        '[DEBUG] get_orders result',
        {
            'ok': bool(result and result.get('code') in ['0', 0, '200', 200]),
            'flt_order_drt': flt_order_drt,
            'flt_order_status': flt_order_status,
            'result_code': None if not isinstance(result, dict) else result.get('code'),
            'message': '' if not isinstance(result, dict) else str(result.get('message', ''))[:160],
        },
        location='v10_moni_trader.py:get_orders',
    )
    # #endregion
    if not result or result.get('code') not in ['0', 0, '200', 200]:
        cached_orders, cache_age_seconds = _read_live_endpoint_cache(ORDERS_CACHE_FILE)
        if isinstance(cached_orders, list):
            # #region debug-point F:orders-cache-fallback
            _mx_api_flap_debug_emit(
                'F',
                '[DEBUG] get_orders cache fallback',
                {
                    'flt_order_drt': flt_order_drt,
                    'flt_order_status': flt_order_status,
                    'cache_age_seconds': cache_age_seconds,
                    'cached_count': len(cached_orders),
                },
                location='v10_moni_trader.py:get_orders',
            )
            # #endregion
            return _rehydrate_cached_orders(cached_orders)
        return []
    data = result.get('data', {})
    orders = data.get('orders', []) or []
    normalized_orders = [_normalize_order(order) for order in orders]
    _write_live_endpoint_cache(ORDERS_CACHE_FILE, normalized_orders)
    return normalized_orders


def _cancel_result_ok(result):
    if not result or result.get('code') not in ['0', 0, '200', 200]:
        return False
    data = result.get('data', {}) if isinstance(result.get('data', {}), dict) else {}
    rc = str(data.get('rc', '0')).strip()
    cancel_count = _inum(data.get('cancelCount', 0), 0)
    fail_count = _inum(data.get('failCount', 0), 0)
    return rc in {'0', ''} and (cancel_count > 0 or fail_count == 0)


def _log_cancel_api(order_id, stock_code, result, *, reason=''):
    payload = {
        'logged_at': _now_str(),
        'event_type': 'cancel_result',
        'order_id': str(order_id or '').strip(),
        'code': str(stock_code or '').zfill(6),
        'reason': str(reason or '').strip(),
        'ok': _cancel_result_ok(result),
        'result_code': _trade_result_code(result),
        'message': '' if not result else str(result.get('message', '')),
        'raw': result or {},
    }
    _append_jsonl(TRADE_API_LOG_FILE, payload)


def cancel_order(order_id, stock_code, *, reason='', request_timeout_seconds=None):
    payload = {
        'type': 'order',
        'orderId': str(order_id or '').strip(),
        'stockCode': str(stock_code or '').zfill(6),
    }
    result = api_request(
        '/api/claw/mockTrading/cancel',
        payload,
        request_timeout_seconds=request_timeout_seconds,
    )
    _log_cancel_api(order_id, stock_code, result, reason=reason)
    return _cancel_result_ok(result), result


def cleanup_pending_orders(
    *,
    items=None,
    orders=None,
    positions=None,
    max_cancel=PENDING_CANCEL_BATCH_LIMIT,
    time_budget_seconds=None,
    cancel_timeout_seconds=None,
):
    items = items if items is not None else refresh_pending_orders(orders=orders, positions=positions)
    if not items:
        return {
            'attempted': 0,
            'cancelled': 0,
            'failed': 0,
            'codes': [],
            'items': items,
            'budget_exhausted': False,
            'remaining_candidates': 0,
        }
    now = datetime.now()
    budget_seconds = _fnum(time_budget_seconds, 0.0)
    budget_enabled = budget_seconds > 0.0
    started_at = time.perf_counter()
    attempts = []
    seen_ids = set()
    for item in items:
        status = str(item.get('status', '')).strip()
        order_id = str(item.get('order_id', '')).strip()
        if status in PENDING_TERMINAL_STATUSES or not order_id or order_id in seen_ids:
            continue
        recorded_at = _parse_dt(item.get('recorded_at'))
        age_minutes = (now - recorded_at).total_seconds() / 60 if recorded_at else 0.0
        stale = bool(item.get('stale'))
        if not stale and status not in {'cancel_pending', 'cancel_failed'}:
            continue
        attempts.append((item, age_minutes))
        seen_ids.add(order_id)

    cancelled = 0
    failed = 0
    codes = []
    executed_attempts = []
    budget_exhausted = False
    for item, age_minutes in attempts[:max_cancel]:
        if budget_enabled:
            elapsed_seconds = time.perf_counter() - started_at
            remaining_budget = budget_seconds - elapsed_seconds
            if remaining_budget <= 0.35:
                budget_exhausted = True
                break
            effective_timeout = min(
                _fnum(cancel_timeout_seconds, remaining_budget),
                remaining_budget,
            )
            if effective_timeout <= 0.35:
                budget_exhausted = True
                break
        else:
            effective_timeout = cancel_timeout_seconds
        reason = (
            f"auto_cleanup:{item.get('status', 'submitted')}"
            f":age={age_minutes:.1f}m"
        )
        ok, _ = cancel_order(
            item.get('order_id', ''),
            item.get('code', ''),
            reason=reason,
            request_timeout_seconds=effective_timeout,
        )
        executed_attempts.append(item)
        if ok:
            cancelled += 1
            codes.append(str(item.get('code', '')).zfill(6))
        else:
            failed += 1
        if budget_enabled and (time.perf_counter() - started_at) >= budget_seconds:
            budget_exhausted = True
            break

    refreshed = items
    attempted_count = len(executed_attempts)
    if attempted_count:
        wait_seconds = 1.0
        if budget_enabled:
            remaining_budget = budget_seconds - (time.perf_counter() - started_at)
            if remaining_budget <= 0.35:
                budget_exhausted = True
                remaining_budget = 0.0
            wait_seconds = min(1.0, max(0.0, remaining_budget - 0.15))
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        fresh_orders = get_orders()
        fresh_positions = get_positions()
        refreshed = refresh_pending_orders(
            orders=fresh_orders,
            positions=fresh_positions,
        )
    return {
        'attempted': attempted_count,
        'cancelled': cancelled,
        'failed': failed,
        'codes': sorted(set(codes)),
        'items': refreshed,
        'budget_exhausted': budget_exhausted,
        'remaining_candidates': max(0, len(attempts) - attempted_count),
    }


def cancel_pending_context(code, pending_ctx, *, reason):
    items = list((pending_ctx or {}).get('items', []))
    if not items:
        return {'attempted': 0, 'cancelled': 0, 'failed': 0}
    cancelled = 0
    failed = 0
    seen_ids = set()
    for item in items:
        order_id = str(item.get('order_id', '')).strip()
        if not order_id or order_id in seen_ids:
            continue
        seen_ids.add(order_id)
        ok, _ = cancel_order(order_id, code, reason=reason)
        if ok:
            cancelled += 1
        else:
            failed += 1
    return {
        'attempted': len(seen_ids),
        'cancelled': cancelled,
        'failed': failed,
    }


def find_recent_filled_order(code, direction, *, quantity=None, exclude_ids=None, lookback_minutes=10, retries=4):
    code = str(code).zfill(6)
    target_direction = 1 if str(direction).lower() == 'buy' else 2
    seen_ids = set(exclude_ids or [])
    quantity = _inum(quantity, 0) if quantity is not None else None
    best = None
    for attempt in range(retries):
        orders = get_orders(flt_order_drt=target_direction, flt_order_status=4)
        now = datetime.now()
        candidates = []
        for order in orders:
            if order['code'] != code or order['status'] != 4 or order['direction'] != target_direction:
                continue
            if order['id'] in seen_ids:
                continue
            if order['datetime'] is None:
                continue
            if (now - order['datetime']).total_seconds() > lookback_minutes * 60:
                continue
            if quantity is not None and quantity > 0 and order['trade_count'] not in (0, quantity):
                continue
            candidates.append(order)
        if candidates:
            best = max(candidates, key=lambda item: item['time'])
            break
        if attempt < retries - 1:
            time.sleep(1.0)
    return best


def _safe_avg_price(total_amount, total_quantity):
    if total_quantity <= 0:
        return 0.0
    return round(total_amount / total_quantity, 4)


def load_backtest_targets():
    payload = _read_json(BACKTEST_SUMMARY_FILE)
    tiers = {}
    for item in payload.get('tiers', []) or []:
        tier = _inum(item.get('tier', 0), 0)
        if tier <= 0:
            continue
        tiers[tier] = {
            'wr': _fnum(item.get('WR', 0.0), 0.0),
            'avg_ret': _fnum(item.get('avg_ret', 0.0), 0.0),
            'ev': _fnum(item.get('EV', 0.0), 0.0),
        }
    if tiers:
        return tiers

    text = ""
    for encoding in ('utf-8', 'utf-8-sig'):
        try:
            with open(BACKTEST_SUMMARY_FILE, encoding=encoding, errors='replace') as f:
                text = f.read()
            if text:
                break
        except OSError:
            continue

    pattern = re.compile(
        r'"tier"\s*:\s*(\d+).*?"WR"\s*:\s*([-+]?\d+(?:\.\d+)?).*?"avg_ret"\s*:\s*([-+]?\d+(?:\.\d+)?).*?"EV"\s*:\s*([-+]?\d+(?:\.\d+)?)',
        re.S,
    )
    for match in pattern.finditer(text):
        tier = _inum(match.group(1), 0)
        if tier <= 0:
            continue
        tiers[tier] = {
            'wr': _fnum(match.group(2), 0.0),
            'avg_ret': _fnum(match.group(3), 0.0),
            'ev': _fnum(match.group(4), 0.0),
        }
    return tiers


def api_request(
    endpoint,
    payload,
    *,
    is_trade=False,
    trade_meta=None,
    min_interval_seconds=None,
    request_timeout_seconds=None,
):
    """发送 API 请求到妙想服务器"""
    url = f"{MX_API_URL}{endpoint}"
    headers = {
        'apikey': MX_APIKEY,
        'Content-Type': 'application/json; charset=UTF-8',
    }
    attempts = _resolve_trade_max_retries(trade_meta) if is_trade else 1
    request_timeout = _fnum(request_timeout_seconds, 30.0)
    if request_timeout <= 0:
        request_timeout = 30.0
    for attempt in range(1, attempts + 1):
        if is_trade:
            _throttle_trade_api(min_interval_seconds=min_interval_seconds)
        try:
            response_obj = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=request_timeout,
            )
            response_obj.raise_for_status()
            try:
                response = response_obj.json()
            except ValueError:
                # #region debug-point D:json-decode-failed
                _midday_api_fail_debug_emit(
                    'D',
                    '[DEBUG] json decode failed',
                    {
                        'endpoint': endpoint,
                        'status_code': response_obj.status_code,
                        'response_text': (response_obj.text or '')[:400],
                        'has_apikey': bool(MX_APIKEY),
                        'transport': 'requests',
                    },
                    location='v10_moni_trader.py:1099',
                )
                # #endregion
                print("[ERROR] JSON decode failed")
                response = None
        except requests.RequestException as e:
            # #region debug-point A:requests-failed
            response_obj = getattr(e, 'response', None)
            _midday_api_fail_debug_emit(
                'A',
                '[DEBUG] requests transport failed',
                {
                    'endpoint': endpoint,
                    'error_type': type(e).__name__,
                    'error': str(e),
                    'status_code': getattr(response_obj, 'status_code', None),
                    'response_text': ((response_obj.text if response_obj is not None else '') or '')[:400],
                    'has_apikey': bool(MX_APIKEY),
                    'transport': 'requests',
                },
                location='v10_moni_trader.py:1093',
            )
            # #endregion
            print(f"[ERROR] request failed: {e}")
            response = None
        except Exception as e:
            # #region debug-point E:python-exception
            _midday_api_fail_debug_emit(
                'E',
                '[DEBUG] api_request exception',
                {
                    'endpoint': endpoint,
                    'error_type': type(e).__name__,
                    'error': str(e),
                    'cwd': os.getcwd(),
                    'has_apikey': bool(MX_APIKEY),
                    'transport': 'requests',
                },
                location='v10_moni_trader.py:1104',
            )
            # #endregion
            print(f"[ERROR] API request failed: {e}")
            response = None

        if not is_trade:
            return response

        code = _trade_result_code(response)
        if code not in TRADE_RETRYABLE_CODES or attempt >= attempts:
            if code in TRADE_RETRYABLE_CODES:
                _mark_trade_api_cooldown(
                    _resolve_trade_rate_limit_cooldown_seconds(
                        trade_meta,
                        final_failure=attempt >= attempts,
                    )
                )
            return _annotate_trade_response(
                response,
                retry_attempts=attempt - 1,
                min_interval_seconds=min_interval_seconds,
            )
        sleep_seconds = _resolve_trade_rate_limit_sleep_seconds(attempt, trade_meta)
        if trade_meta:
            _trade_log_retry_event(
                trade_meta,
                result_code=code,
                sleep_seconds=sleep_seconds,
                attempt=attempt,
                total_attempts=attempts,
            )
        _mark_trade_api_cooldown(_resolve_trade_rate_limit_cooldown_seconds(trade_meta))
        print(f"[WARN] trade 接口限流(code={code})，{sleep_seconds:.1f}s 后重试第 {attempt + 1}/{attempts} 次")
        time.sleep(sleep_seconds)
    return _annotate_trade_response(
        None,
        retry_attempts=max(attempts - 1, 0),
        min_interval_seconds=min_interval_seconds,
    )


def get_balance():
    """查询账户资金（API moneyUnit=1 时返回单位为元，无需额外换算）"""
    result = api_request('/api/claw/mockTrading/balance', {'moneyUnit': 1})
    # #region debug-point B:balance-fetch
    _mx_api_flap_debug_emit(
        'B',
        '[DEBUG] get_balance result',
        {
            'ok': bool(result and result.get('code') in ['0', 0, '200', 200]),
            'result_code': None if not isinstance(result, dict) else result.get('code'),
            'message': '' if not isinstance(result, dict) else str(result.get('message', ''))[:160],
        },
        location='v10_moni_trader.py:get_balance',
    )
    # #endregion
    if not result or result.get('code') not in ['0', 0, '200', 200]:
        cached_balance, cache_age_seconds = _read_live_endpoint_cache(BALANCE_CACHE_FILE)
        if isinstance(cached_balance, dict) and cached_balance:
            # #region debug-point F:balance-cache-fallback
            _mx_api_flap_debug_emit(
                'F',
                '[DEBUG] get_balance cache fallback',
                {
                    'cache_age_seconds': cache_age_seconds,
                    'has_total_assets': bool(_fnum(cached_balance.get('total_assets', 0.0), 0.0) > 0),
                },
                location='v10_moni_trader.py:get_balance',
            )
            # #endregion
            return cached_balance
        return None
    data = result['data']
    balance = {
        'total_assets': data.get('totalAssets', 0),
        'avail_balance': data.get('availBalance', 0),
        'total_pos_value': data.get('totalPosValue', 0),
    }
    _write_live_endpoint_cache(BALANCE_CACHE_FILE, balance)
    return balance


def get_positions():
    """查询持仓（API返回单位：count=股, value/profit=元, price需按priceDec除）"""
    result = api_request('/api/claw/mockTrading/positions', {'moneyUnit': 1})
    # #region debug-point C:positions-fetch
    _mx_api_flap_debug_emit(
        'C',
        '[DEBUG] get_positions result',
        {
            'ok': bool(result and result.get('code') in ['0', 0, '200', 200]),
            'result_code': None if not isinstance(result, dict) else result.get('code'),
            'message': '' if not isinstance(result, dict) else str(result.get('message', ''))[:160],
            'raw_pos_count': _inum((((result or {}).get('data') or {}).get('posList') or []).__len__(), 0) if isinstance(result, dict) else 0,
        },
        location='v10_moni_trader.py:get_positions',
    )
    # #endregion
    if not result or result.get('code') not in ['0', 0, '200', 200]:
        cached_positions, cache_age_seconds = _read_live_endpoint_cache(POSITIONS_CACHE_FILE)
        if isinstance(cached_positions, list):
            # #region debug-point F:positions-cache-fallback
            _mx_api_flap_debug_emit(
                'F',
                '[DEBUG] get_positions cache fallback',
                {
                    'cache_age_seconds': cache_age_seconds,
                    'cached_count': len(cached_positions),
                    'cached_codes': [str((item or {}).get('code', '')).zfill(6) for item in cached_positions[:10]],
                },
                location='v10_moni_trader.py:get_positions',
            )
            # #endregion
            return cached_positions
        return []
    data = result['data']
    pos_list = data.get('posList', [])
    positions = []
    for pos in pos_list:
        price_dec = pos.get('priceDec', 2)
        cost_price_dec = pos.get('costPriceDec', 3)
        item = {
            'code': pos.get('secCode', ''),
            'name': pos.get('secName', ''),
            'count': pos.get('count', 0),
            'avail_count': pos.get('availCount', 0),
            'price': pos.get('price', 0) / (10 ** price_dec),
            'cost_price': pos.get('costPrice', 0) / (10 ** cost_price_dec),
            'value': pos.get('value', 0),
            'profit': pos.get('profit', 0),
            'profit_pct': pos.get('profitPct', 0),
            'pos_pct': pos.get('posPct', 0),
        }
        # mx moni 偶发返回已清仓后的幽灵持仓行：代码仍在，但数量/可卖数量/市值均为 0。
        # 这些行不应继续污染午盘复检、状态摘要与运行时账本重建。
        if not _has_active_position(item):
            continue
        positions.append(item)
    _write_live_endpoint_cache(POSITIONS_CACHE_FILE, positions)
    return positions


def buy_stock(code, quantity, ref_price=None, order_context=None):
    """市价买入（带5min去重拦截）

    Args:
        code: 股票代码
        quantity: 买入股数
        ref_price: 参考价（用于去重key，传0表示市价占位）
    """
    order_context = order_context or {}
    p = _fnum(ref_price, 0.0)
    min_interval_seconds = _resolve_trade_min_interval(TRADE_BUY_MIN_INTERVAL_SECONDS, order_context)
    if _is_duplicate(code, quantity, p, 'buy'):
        print(f"  [SKIP] [DEDUP] {code} {quantity}股 @ {p:.2f}  5min内已下过，跳过")
        result = {'code': 'DEDUP', 'message': '5min内重复报单已跳过', '__retry_attempts': 0}
        _log_trade_api(
            'buy', code, quantity, p, result,
            extra={**order_context, 'final_outcome': 'dedup_skipped'},
        )
        return {
            'success': False,
            'result': result,
            'result_code': 'DEDUP',
            'order_id': '',
        }
    result = api_request('/api/claw/mockTrading/trade', {
        'type': 'buy',
        'stockCode': code,
        'quantity': quantity,
        'useMarketPrice': True,
    }, is_trade=True, trade_meta={
        'action': 'buy',
        'code': code,
        'quantity': quantity,
        'ref_price': p,
        **order_context,
    }, min_interval_seconds=min_interval_seconds)
    _log_trade_api('buy', code, quantity, p, result, extra=order_context)
    if _trade_result_ok(result):
        order_id = _extract_order_id(result)
        print(f"   买入 {code} {quantity}股 委托号={order_id}")
        _record_order(code, quantity, p, 'buy')
        register_pending_order('buy', code, quantity, p, order_id)
        return {
            'success': True,
            'result': result,
            'result_code': _trade_result_code(result),
            'order_id': order_id,
        }
    msg = result.get('message', '未知错误') if result else '网络错误'
    print(f"   买入 {code} 失败: {msg}")
    return {
        'success': False,
        'result': result,
        'result_code': _trade_result_code(result),
        'order_id': '',
    }


def sell_stock(code, quantity, ref_price=None, order_context=None):
    """市价卖出"""
    order_context = order_context or {}
    p = _fnum(ref_price, 0.0)
    min_interval_seconds = _resolve_trade_min_interval(TRADE_SELL_MIN_INTERVAL_SECONDS, order_context)
    result = api_request('/api/claw/mockTrading/trade', {
        'type': 'sell',
        'stockCode': code,
        'quantity': quantity,
        'useMarketPrice': True,
    }, is_trade=True, trade_meta={
        'action': 'sell',
        'code': code,
        'quantity': quantity,
        'ref_price': p,
        **order_context,
    }, min_interval_seconds=min_interval_seconds)
    _log_trade_api('sell', code, quantity, p, result, extra=order_context)
    if _trade_result_ok(result):
        order_id = _extract_order_id(result)
        print(f"   卖出 {code} {quantity}股 委托号={order_id}")
        register_pending_order('sell', code, quantity, p, order_id)
        return {
            'success': True,
            'result': result,
            'result_code': _trade_result_code(result),
            'order_id': order_id,
        }
    msg = result.get('message', '未知错误') if result else '网络错误'
    print(f"   卖出 {code} 失败: {msg}")
    return {
        'success': False,
        'result': result,
        'result_code': _trade_result_code(result),
        'order_id': '',
    }


def execute_trade_action(action, code, quantity, *, ref_price=None, execution_phase='primary', strategy_action=''):
    order_context = {
        'execution_phase': str(execution_phase or '').strip(),
        'strategy_action': str(strategy_action or action or '').strip(),
    }
    trade_fn = buy_stock if str(action).strip() == 'buy' else sell_stock
    return trade_fn(code, quantity, ref_price=ref_price, order_context=order_context)


def is_rate_limited_trade_result(trade_result):
    return str((trade_result or {}).get('result_code', '')).strip() == '112'


def enqueue_buy_tail_retry(retry_tail_queue, item):
    retry_tail_queue.append(item)
    print(f"   [REQUEUE] {item['code']} 命中112限流，加入尾部补单队列")


def enqueue_sell_tail_retry(sell_retry_queue, *, code, name, tier, qty, cur_price, sell_reason):
    sell_retry_queue.append({
        'code': str(code).zfill(6),
        'name': name,
        'tier': tier,
        'qty': qty,
        'cur_price': cur_price,
        'sell_reason': sell_reason,
    })
    print(f"  [REQUEUE] {code} {name} 命中112限流，加入本窗口延时重试队列")


def _build_buy_reconcile_context(item, *, today, build_note, tier=None, note_suffix=''):
    resolved_tier = tier if tier is not None else item.get('tier')
    return {
        'direction': 'buy',
        'date': today,
        'name': item.get('name', ''),
        'tier': resolved_tier,
        'mode': item.get('mode', ''),
        'build_note': build_note,
        'target_amount': f"{_fnum(item.get('target_amount', 0.0), 0.0):.0f}",
        'decision_id': str(item.get('decision_id', '')).strip(),
        'decision_run_slot': str(item.get('decision_run_slot', '')).strip(),
        'selected_reason_hash': str(item.get('selected_reason_hash', '')).strip(),
        'big_meat_state': str(item.get('big_meat_state', '')).strip(),
        'big_meat_score': f"{_fnum(item.get('big_meat_score', 0.0), 0.0):.2f}" if str(item.get('big_meat_state', '')).strip() else '',
        'big_meat_aggressive_score': f"{_fnum(item.get('big_meat_aggressive_score', 0.0), 0.0):.2f}" if str(item.get('big_meat_state', '')).strip() else '',
        'big_meat_reason': str(item.get('big_meat_reason', '')).strip(),
        'big_meat_window_tag': str(item.get('big_meat_window_tag', '')).strip(),
        'big_meat_first_seen_at': str(item.get('big_meat_first_seen_at', '')).strip(),
        'big_meat_confirmed_at': str(item.get('big_meat_confirmed_at', '')).strip(),
        'big_meat_last_eval_at': str(item.get('big_meat_last_eval_at', '')).strip(),
        'holding_big_meat_score': str(item.get('holding_big_meat_score', '')).strip(),
        'holding_big_meat_reason': str(item.get('holding_big_meat_reason', '')).strip(),
        'holding_big_meat_promoted_at': str(item.get('holding_big_meat_promoted_at', '')).strip(),
        'big_meat_hold_state': str(item.get('big_meat_hold_state', '')).strip(),
        'big_meat_core_qty': str(item.get('big_meat_core_qty', '')).strip(),
        'big_meat_trade_qty': str(item.get('big_meat_trade_qty', '')).strip(),
        'big_meat_hold_lock_until': str(item.get('big_meat_hold_lock_until', '')).strip(),
        'expected_quantity': _inum(item.get('quantity', 0), 0),
        'fallback_quantity': _inum(item.get('quantity', 0), 0),
        'fallback_price': _fnum(item.get('entry_price', item.get('price', 0.0)), 0.0),
        'note_suffix': str(note_suffix or '').strip(),
    }


def _build_sell_reconcile_context(code, *, quantity, price, close_reason):
    return {
        str(code).zfill(6): {
            'direction': 'sell',
            'expected_quantity': _inum(quantity, 0),
            'fallback_price': _fnum(price, 0.0),
            'close_reason': str(close_reason or '').strip(),
        }
    }


#region debug-point smart-sell-timeout-report
def _debug_report_smart_sell(stage, **data):
    try:
        import urllib.request
        payload = {
            "sessionId": "smart-sell-timeout",
            "runId": "pre",
            "hypothesisId": "timing",
            "location": f"v10_moni_trader.py:{stage}",
            "msg": f"[DEBUG] smart-sell stage {stage}",
            "data": data,
        }
        env_path = Path.cwd() / ".dbg" / "smart-sell-timeout.env"
        server_url = "http://127.0.0.1:7789/event"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("DEBUG_SERVER_URL="):
                    server_url = line.split("=", 1)[1].strip() or server_url
                    break
        req = urllib.request.Request(
            server_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=1.5).read()
    except Exception:
        try:
            debug_log_file = DATA_DIR / "smart_sell_debug_latest.ndjson"
            with debug_log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": _now_str(),
                    "stage": stage,
                    "data": data,
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass
#endregion


def _apply_successful_sell(code, *, quantity, price, close_reason, records, balance, positions, defer_reconcile=False):
    _debug_report_smart_sell("apply_successful_sell.start", code=str(code).zfill(6), quantity=_inum(quantity, 0))
    reconcile_changed = False
    #region debug-point smart-sell-apply-reconcile
    reconcile_started_at = time.perf_counter()
    #endregion
    if not defer_reconcile:
        records, balance, positions, reconcile_changed = reconcile_after_trade(
            [code],
            records=records,
            trade_contexts=_build_sell_reconcile_context(
                code,
                quantity=quantity,
                price=price,
                close_reason=close_reason,
            ),
        )
        #region debug-point smart-sell-apply-reconcile-done
        _debug_report_smart_sell(
            "apply_successful_sell.after_reconcile",
            code=str(code).zfill(6),
            defer_reconcile=bool(defer_reconcile),
            reconcile_changed=bool(reconcile_changed),
            elapsed_ms=round((time.perf_counter() - reconcile_started_at) * 1000, 2),
        )
        #endregion
        if reconcile_changed:
            #region debug-point smart-sell-apply-save-start
            save_started_at = time.perf_counter()
            #endregion
            save_track_record(records)
            #region debug-point smart-sell-apply-save-done
            _debug_report_smart_sell(
                "apply_successful_sell.after_save_track_record",
                code=str(code).zfill(6),
                elapsed_ms=round((time.perf_counter() - save_started_at) * 1000, 2),
            )
            #endregion
    pending_items = load_pending_orders()
    pending_ctx = _active_pending_context_by_code(pending_items, action='sell').get(str(code).zfill(6), {})
    if defer_reconcile and _inum((pending_ctx or {}).get('reserved_qty', 0), 0) <= 0:
        pending_ctx = {
            **(pending_ctx or {}),
            'reserved_qty': _inum(quantity, 0),
            'items': list((pending_ctx or {}).get('items', [])),
        }
    _debug_report_smart_sell(
        "apply_successful_sell.local_pending_snapshot",
        code=str(code).zfill(6),
        pending_count=len(pending_items or []),
        reserved_qty=_inum((pending_ctx or {}).get('reserved_qty', 0), 0),
    )
    record_idx = _find_record_index(records, code, statuses=('closed', 'holding', 'paused'))
    updated_record = records[record_idx] if record_idx is not None else {}
    return {
        'records': records,
        'balance': balance,
        'positions': positions,
        'record': updated_record,
        'pending_ctx': pending_ctx,
        'sell_confirmed': (not defer_reconcile) and str(updated_record.get('status', '')).strip() == 'closed',
    }


def _apply_successful_buys(codes, *, records, balance, positions, trade_contexts):
    records, balance, positions, reconcile_changed = reconcile_after_trade(
        codes,
        records=records,
        trade_contexts=trade_contexts,
    )
    if reconcile_changed:
        save_track_record(records)
    return {
        'records': records,
        'balance': balance,
        'positions': positions,
        'changed': reconcile_changed,
    }


def _run_buy_tail_retry_queue(retry_tail_queue, *, success_codes, success_count, trade_contexts, today):
    if not retry_tail_queue:
        return success_count
    tail_delay_seconds = _resolve_tail_retry_delay_seconds('buy')
    print(
        f"\n [INFO] 首轮买入存在 {len(retry_tail_queue)} 只 112 限流，"
        f"{tail_delay_seconds:.0f}s 后执行尾部补单"
    )
    time.sleep(tail_delay_seconds)
    for item in retry_tail_queue:
        code = str(item['code']).zfill(6)
        if code in success_codes:
            continue
        print(f"   [TAIL-RETRY] {item['code']} {item['name']} 再次尝试买入")
        trade_result = execute_trade_action(
            'buy',
            item['code'],
            item['quantity'],
            ref_price=item['entry_price'],
            execution_phase='tail_retry',
            strategy_action='buy',
        )
        if not trade_result['success']:
            continue
        build_note_full = (
            f"{item.get('build_note', '')} | "
            f"模型{item.get('model_score', 0.0):.1f} "
            f"(市{item.get('model_market_score', 0.0):.0f}/"
            f"板{item.get('model_sector_score', 0.0):.0f}/"
            f"股{item.get('model_stock_score', 0.0):.0f}/"
            f"流{item.get('model_flow_score', 0.0):.0f}) "
            f"[{item.get('model_industry', 'unknown')}]"
        )
        success_count += 1
        success_codes.append(code)
        trade_contexts[code] = _build_buy_reconcile_context(
            item,
            today=today,
            build_note=build_note_full,
            tier=item.get('tier'),
        )
    return success_count


def _run_add_position_tail_retry_queue(
    retry_tail_queue,
    *,
    records,
    balance,
    positions,
    success_count,
    today,
):
    unresolved_items = []
    tail_retry_success_count = 0
    if not retry_tail_queue:
        return records, balance, positions, success_count, tail_retry_success_count, unresolved_items
    tail_delay_seconds = _resolve_tail_retry_delay_seconds('add_position')
    print(
        f"\n [INFO] 首轮加仓存在 {len(retry_tail_queue)} 只 112 限流，"
        f"{tail_delay_seconds:.0f}s 后执行尾部补单"
    )
    time.sleep(tail_delay_seconds)
    for item in retry_tail_queue:
        print(f"   [TAIL-RETRY] {item['code']} {item['name']} 再次尝试加仓")
        trade_result = execute_trade_action(
            'buy',
            item['code'],
            item['quantity'],
            ref_price=item['price'],
            execution_phase='tail_retry',
            strategy_action='add_position',
        )
        if not trade_result['success']:
            unresolved_items.append(item)
            continue
        success_count += 1
        tail_retry_success_count += 1
        buy_apply = _apply_successful_buys(
            [item['code']],
            records=records,
            balance=balance,
            positions=positions,
            trade_contexts={
                str(item['code']).zfill(6): _build_buy_reconcile_context(
                    item,
                    today=today,
                    build_note='加仓至满仓',
                    tier=item.get('tier'),
                    note_suffix='加仓至满仓',
                )
            },
        )
        records = buy_apply['records']
        balance = buy_apply['balance']
        positions = buy_apply['positions']
    return records, balance, positions, success_count, tail_retry_success_count, unresolved_items


def _run_sell_tail_retry_queue(
    sell_retry_queue,
    *,
    action,
    records,
    balance,
    positions,
    sold_count,
    confirmed_count,
    skipped_count,
):
    if not sell_retry_queue:
        return records, balance, positions, sold_count, confirmed_count, skipped_count
    tail_delay_seconds = _resolve_tail_retry_delay_seconds(action)
    print(
        f"\n [INFO] smart sell 首轮存在 {len(sell_retry_queue)} 只 112 限流，"
        f"{tail_delay_seconds:.0f}s 后执行本窗口延时重试"
    )
    time.sleep(tail_delay_seconds)
    for item in sell_retry_queue:
        code = item['code']
        latest_positions = get_positions() or positions
        latest_pos_map = _active_position_map(latest_positions)
        pos = latest_pos_map.get(code)
        if not pos:
            _clear_smart_sell_retry_state(code)
            continue
        retry_qty = min(
            _inum(item.get('qty', 0), 0),
            _position_broker_sellable_cap(pos),
        )
        if retry_qty <= 0:
            _mark_smart_sell_rate_limit(code, item.get('qty', 0))
            skipped_count += 1
            print(f"  [COOLDOWN] {code} {item['name']} 延时重试前已无可卖数量，等待下一窗口复核")
            continue
        print(f"  [TAIL-RETRY] {code} {item['name']} 再次尝试卖出 {retry_qty} 股")
        trade_result = execute_trade_action(
            'sell',
            code,
            retry_qty,
            ref_price=item.get('cur_price', 0.0),
            execution_phase='tail_retry',
            strategy_action=action,
        )
        if not trade_result['success']:
            if is_rate_limited_trade_result(trade_result):
                _mark_smart_sell_rate_limit(code, retry_qty)
                print(f"  [COOLDOWN] {code} {item['name']} 尾部重试后仍触发112，进入下一窗口冷却")
            skipped_count += 1
            continue
        _clear_smart_sell_retry_state(code)
        sell_apply = _apply_successful_sell(
            code,
            quantity=retry_qty,
            price=item.get('cur_price', 0.0),
            close_reason=item.get('sell_reason', ''),
            records=records,
            balance=balance,
            positions=positions,
        )
        records = sell_apply['records']
        balance = sell_apply['balance']
        positions = sell_apply['positions']
        updated_record = sell_apply['record']
        sold_count += 1
        if sell_apply.get('sell_confirmed'):
            confirmed_count += 1
            pnl = _fnum(updated_record.get('pnl', 0.0), 0.0)
            pnl_pct = _fnum(updated_record.get('pnl_pct', 0.0), 0.0)
            sell_time = updated_record.get('sell_time', '')
            time_info = f" @{sell_time}" if sell_time else ""
            print(
                f"  {code} {item['name']} T{item['tier']} | {item.get('sell_reason', '')} | "
                f"尾部重试卖出成功 收益{pnl_pct:+.2f}% (¥{pnl:+,.0f}){time_info}"
            )
        else:
            pending_ctx = sell_apply.get('pending_ctx') or {}
            reserved_qty = _inum(pending_ctx.get('reserved_qty', 0), retry_qty)
            print(
                f"  [PENDING] {code} {item['name']} T{item['tier']} | {item.get('sell_reason', '')} | "
                f"尾部重试卖单已受理 {reserved_qty} 股，等待后续成交确认"
            )
    return records, balance, positions, sold_count, confirmed_count, skipped_count


def get_order_timestamp(code, direction='buy', lookback_minutes=5):
    """从mx-moni订单API获取最近一笔委托的精确成交时间

    Args:
        code: 股票代码
        direction: 'buy' 或 'sell'
        lookback_minutes: 只看最近N分钟内的订单（避免误取历史订单）

    Returns:
        str: HH:MM:SS 格式的时间，如 '14:50:23'；失败返回空字符串
    """
    result = api_request('/api/claw/mockTrading/orders', {
        'fltOrderDrt': 0,
        'fltOrderStatus': 0
    })
    if not result or result.get('code') not in ['0', 0, '200', 200]:
        return ''

    orders = result.get('data', {}).get('orders', [])
    # drt=1买入, drt=2卖出
    target_drt = 1 if direction == 'buy' else 2
    # 按时间倒序（最新在前）
    for order in reversed(orders):
        if order.get('secCode') != code:
            continue
        if order.get('drt') != target_drt:
            continue
        if order.get('status') != 4:  # 4=已成
            continue
        ts = order.get('time', 0)
        if ts <= 0:
            continue
        dt = datetime.fromtimestamp(ts)
        # 只取最近N分钟内的订单
        if (datetime.now() - dt).total_seconds() > lookback_minutes * 60:
            continue
        return dt.strftime('%H:%M:%S')

    return ''


def capture_trade_fill(code, direction, quantity, *, existing_order_ids=None, lookback_minutes=10):
    fill = find_recent_filled_order(
        code,
        direction,
        quantity=quantity,
        exclude_ids=existing_order_ids,
        lookback_minutes=lookback_minutes,
        retries=5,
    )
    if not fill:
        return None
    actual_trade_price = _fnum(fill.get('actual_trade_price', 0.0), 0.0)
    return {
        'order_id': fill['id'],
        'trade_time': fill['datetime'].strftime('%H:%M:%S') if fill['datetime'] else '',
        'trade_date': fill['datetime'].strftime('%Y-%m-%d') if fill['datetime'] else '',
        'trade_price': actual_trade_price,
        'trade_count': fill['trade_count'] if fill['trade_count'] > 0 else fill['count'],
    }


def _has_trade_price(fill):
    return _fnum((fill or {}).get('trade_price', 0.0), 0.0) > 0


def apply_buy_fill(record, fill, *, fallback_price, fallback_quantity, note_suffix=''):
    record = _normalize_record(record)
    fill_price = _fnum((fill or {}).get('trade_price', 0.0), fallback_price)
    fill_qty = _inum((fill or {}).get('trade_count', 0), fallback_quantity)
    fill_time = str((fill or {}).get('trade_time', '')).strip()
    fill_order_id = str((fill or {}).get('order_id', '')).strip()

    old_qty = _inum(record.get('quantity', 0), 0)
    old_amount = _fnum(record.get('buy_amount', 0.0), 0.0)
    new_amount = old_amount + fill_price * fill_qty
    new_qty = old_qty + fill_qty
    if old_qty <= 0:
        record['date'] = str((fill or {}).get('trade_date', '') or record.get('date') or datetime.now().strftime('%Y-%m-%d'))
    record['quantity'] = str(new_qty)
    record['buy_amount'] = f"{new_amount:.2f}"
    record['entry_price'] = f"{_safe_avg_price(new_amount, new_qty):.4f}"
    existing_ids = _split_order_ids(record.get('buy_order_ids', ''))
    if fill_order_id:
        existing_ids.append(fill_order_id)
        record['buy_order_ids'] = _join_order_ids(existing_ids)
    if fill_time:
        existing_time = str(record.get('buy_time', '')).strip()
        record['buy_time'] = f"{existing_time}+{fill_time}" if existing_time else fill_time
    if note_suffix:
        build_note = str(record.get('build_note', '')).strip()
        record['build_note'] = f"{build_note}; {note_suffix}" if build_note else note_suffix
    record['last_synced_at'] = _now_str()
    return record


def apply_sell_fill(record, fill, *, fallback_price, close_reason):
    record = _normalize_record(record)
    qty = _inum(record.get('quantity', 0), 0)
    entry_price = _fnum(record.get('entry_price', 0.0), 0.0)
    sell_price = _fnum((fill or {}).get('trade_price', 0.0), fallback_price)
    sell_time = str((fill or {}).get('trade_time', '')).strip()
    sell_date = str((fill or {}).get('trade_date', '')).strip() or datetime.now().strftime('%Y-%m-%d')
    pnl = (sell_price - entry_price) * qty
    pnl_pct = (sell_price / entry_price - 1) * 100 if entry_price > 0 else 0.0
    try:
        hold_days = (datetime.strptime(sell_date, '%Y-%m-%d') - datetime.strptime(record.get('date', sell_date), '%Y-%m-%d')).days
    except ValueError:
        hold_days = _inum(record.get('hold_days', 0), 0)
    record['status'] = 'closed'
    record['sell_date'] = sell_date
    record['sell_time'] = sell_time
    record['sell_price'] = f"{sell_price:.2f}"
    record['sell_order_id'] = str((fill or {}).get('order_id', '')).strip()
    record['pnl'] = f"{pnl:.2f}"
    record['pnl_pct'] = f"{pnl_pct:.2f}"
    record['hold_days'] = str(max(hold_days, 0))
    record['close_reason'] = close_reason
    record['last_synced_at'] = _now_str()
    return record


def _make_record_from_context(code, pos=None, ctx=None):
    ctx = ctx or {}
    pos = pos or {}
    count = _inum(pos.get('count', 0), _inum(ctx.get('fallback_quantity', 0), 0))
    cost_price = _fnum(pos.get('cost_price', 0.0), _fnum(ctx.get('fallback_price', 0.0), 0.0))
    buy_amount = cost_price * count if count > 0 and cost_price > 0 else 0.0
    item = _normalize_record({
        'date': str(ctx.get('date') or datetime.now().strftime('%Y-%m-%d')),
        'buy_time': str(ctx.get('buy_time', '')).strip(),
        'code': str(code).zfill(6),
        'name': str(pos.get('name') or ctx.get('name', '')).strip(),
        'tier': str(ctx.get('tier', '')),
        'entry_price': f"{cost_price:.4f}" if cost_price > 0 else '0',
        'quantity': str(count if count > 0 else _inum(ctx.get('fallback_quantity', 0), 0)),
        'buy_amount': f"{buy_amount:.2f}" if buy_amount > 0 else '0',
        'buy_order_ids': str(ctx.get('buy_order_ids', '')).strip(),
        'sell_date': '',
        'sell_time': '',
        'sell_price': '',
        'sell_order_id': '',
        'pnl': '',
        'pnl_pct': '',
        'hold_days': '',
        'status': 'holding',
        'mode': str(ctx.get('mode', '')).strip(),
        'build_note': str(ctx.get('build_note', '')).strip(),
        'target_amount': str(ctx.get('target_amount', '')),
        'decision_id': str(ctx.get('decision_id', '')).strip(),
        'decision_run_slot': str(ctx.get('decision_run_slot', '')).strip(),
        'selected_reason_hash': str(ctx.get('selected_reason_hash', '')).strip(),
        'close_reason': '',
        'big_meat_state': str(ctx.get('big_meat_state', '')).strip(),
        'big_meat_score': str(ctx.get('big_meat_score', '')).strip(),
        'big_meat_aggressive_score': str(ctx.get('big_meat_aggressive_score', '')).strip(),
        'big_meat_reason': str(ctx.get('big_meat_reason', '')).strip(),
        'big_meat_window_tag': str(ctx.get('big_meat_window_tag', '')).strip(),
        'big_meat_first_seen_at': str(ctx.get('big_meat_first_seen_at', '')).strip(),
        'big_meat_confirmed_at': str(ctx.get('big_meat_confirmed_at', '')).strip(),
        'big_meat_last_eval_at': str(ctx.get('big_meat_last_eval_at', '')).strip(),
        'holding_big_meat_score': str(ctx.get('holding_big_meat_score', '')).strip(),
        'holding_big_meat_reason': str(ctx.get('holding_big_meat_reason', '')).strip(),
        'holding_big_meat_promoted_at': str(ctx.get('holding_big_meat_promoted_at', '')).strip(),
        'big_meat_hold_state': str(ctx.get('big_meat_hold_state', '')).strip(),
        'big_meat_core_qty': str(ctx.get('big_meat_core_qty', '')).strip(),
        'big_meat_trade_qty': str(ctx.get('big_meat_trade_qty', '')).strip(),
        'big_meat_hold_lock_until': str(ctx.get('big_meat_hold_lock_until', '')).strip(),
        'last_synced_at': _now_str(),
    })
    return _sync_big_meat_position_split(item)


def _overlay_record_from_position(record, pos):
    record = _normalize_record(record)
    pos = pos or {}
    count = _inum(pos.get('count', 0), 0)
    cost_price = _fnum(pos.get('cost_price', 0.0), _fnum(record.get('entry_price', 0.0), 0.0))
    record['code'] = str(pos.get('code', record.get('code', ''))).zfill(6)
    record['name'] = str(pos.get('name', record.get('name', ''))).strip()
    record['status'] = 'holding'
    record['quantity'] = str(count)
    if cost_price > 0:
        record['entry_price'] = f"{cost_price:.4f}"
        record['buy_amount'] = f"{cost_price * count:.2f}"
    if not str(record.get('date', '')).strip():
        record['date'] = datetime.now().strftime('%Y-%m-%d')
    record['sell_date'] = ''
    record['sell_time'] = ''
    record['sell_price'] = ''
    record['sell_order_id'] = ''
    record['pnl'] = ''
    record['pnl_pct'] = ''
    record['hold_days'] = ''
    record['close_reason'] = ''
    record['last_synced_at'] = _now_str()
    return _sync_big_meat_position_split(record, qty=count)


def _find_record_index(records, code, statuses=('holding', 'paused')):
    code = str(code).zfill(6)
    for idx in range(len(records) - 1, -1, -1):
        record = _normalize_record(records[idx])
        if record.get('code') == code and record.get('status') in statuses:
            return idx
    return None


def _find_latest_record_by_code(records, code, *, require_native_context=False):
    code = str(code).zfill(6)
    for idx in range(len(records) - 1, -1, -1):
        record = _normalize_record(records[idx])
        if record.get('code') != code:
            continue
        if require_native_context and not _has_native_strategy_context(record):
            continue
        return record
    return {}


def _build_selected_decision_code_map(rows):
    reference = {}
    for row in rows or []:
        if not bool(row.get('selected')):
            continue
        code = str(row.get('code', '')).zfill(6)
        if code:
            reference[code] = row
    return reference


def _recover_live_position_record(record, *, records=None, selected_decision=None):
    record = _normalize_record(record)
    code = str(record.get('code', '')).zfill(6)
    history = list(records or [])
    source = _find_latest_record_by_code(history, code, require_native_context=True)
    cleaned_note = _strip_reconcile_auto_tags(record.get('build_note', ''))
    recovered = False

    if source:
        for field in ('tier', 'mode', 'target_amount', 'decision_id', 'decision_run_slot', 'selected_reason_hash'):
            value = str(source.get(field, '')).strip()
            if value:
                record[field] = value
                recovered = True
        if str(source.get('status', '')).strip() in {'holding', 'paused'}:
            for field in (
                'date', 'buy_time', 'buy_order_ids',
                'big_meat_state', 'big_meat_score', 'big_meat_aggressive_score',
                'big_meat_reason', 'big_meat_window_tag', 'big_meat_first_seen_at',
                'big_meat_confirmed_at', 'big_meat_last_eval_at',
                'holding_big_meat_score', 'holding_big_meat_reason', 'holding_big_meat_promoted_at',
                'big_meat_hold_state', 'big_meat_core_qty', 'big_meat_trade_qty',
                'big_meat_hold_lock_until',
            ):
                value = str(source.get(field, '')).strip()
                if value:
                    record[field] = value
                    recovered = True
        source_note = _strip_reconcile_auto_tags(source.get('build_note', ''))
        if source_note:
            cleaned_note = source_note

    decision = selected_decision if isinstance(selected_decision, dict) else {}
    if decision:
        decision_date = _date_key(decision.get('trade_date'))
        decision_id = str(decision.get('decision_id', '')).strip()
        decision_slot = str(decision.get('decision_run_slot', '')).strip()
        decision_reason_hash = str(decision.get('selected_reason_hash', '')).strip()
        decision_mode = str(decision.get('mode', '')).strip()
        decision_tier = str(decision.get('tier', '')).strip()
        if (
            decision_id and str(record.get('decision_id', '')).strip() == decision_id
        ) or (
            decision_slot and str(record.get('decision_run_slot', '')).strip() == decision_slot
        ) or (
            decision_reason_hash and str(record.get('selected_reason_hash', '')).strip() == decision_reason_hash
        ) or (
            decision_mode and str(record.get('mode', '')).strip() == decision_mode
        ) or (
            decision_tier and str(record.get('tier', '')).strip() == decision_tier
        ):
            recovered = True
        if not str(record.get('tier', '')).strip():
            tier = decision_tier
            if tier:
                record['tier'] = tier
                recovered = True
        current_mode = str(record.get('mode', '')).strip()
        if not current_mode:
            mode = decision_mode
            if mode:
                record['mode'] = mode
                recovered = True
        for field in ('decision_id', 'decision_run_slot', 'selected_reason_hash'):
            if not str(record.get(field, '')).strip():
                value = str(decision.get(field, '')).strip()
                if value:
                    record[field] = value
                    recovered = True
        if decision_date and not str(record.get('date', '')).strip():
            record['date'] = decision_date
            recovered = True
        if not cleaned_note and recovered:
            cleaned_note = '[AUTO_RECOVERED] selected_decision_context'

    if recovered:
        recovery_note = '[AUTO_RECOVERED] live_position_context'
        record['build_note'] = _merge_build_notes(_strip_live_position_only_tags(cleaned_note), recovery_note)
        return record

    if not _has_native_strategy_context(record):
        record['mode'] = ''
        live_only_note = '[LIVE_POSITION_ONLY] mx_moni_position_without_strategy_context'
        record['build_note'] = _merge_build_notes(cleaned_note, live_only_note)
    return record


def _ensure_live_position_target_amount(record, *, pos=None):
    record = _normalize_record(record)
    if _fnum(record.get('target_amount', 0.0), 0.0) > 0:
        return record
    qty = _inum((pos or {}).get('count', 0), _inum(record.get('quantity', 0), 0))
    if qty <= 0:
        return record
    broker_value = _fnum((pos or {}).get('value', 0.0), 0.0)
    entry_price = _fnum((pos or {}).get('cost_price', 0.0), _fnum(record.get('entry_price', 0.0), 0.0))
    buy_amount = _fnum(record.get('buy_amount', 0.0), 0.0)
    fallback_amount = max(broker_value, buy_amount, entry_price * qty)
    if fallback_amount <= 0:
        return record
    record['target_amount'] = str(int(round(fallback_amount)))
    note = _strip_reconcile_auto_tags(record.get('build_note', ''))
    target_note = '[AUTO_RECOVERED] target_amount_from_live_position'
    if target_note not in note:
        record['build_note'] = _merge_build_notes(note, target_note)
    return record


def _clean_track_record_noise(record, *, active_pos_map=None):
    record = _normalize_record(record)
    pos_map = active_pos_map if isinstance(active_pos_map, dict) else {}
    code = str(record.get('code', '')).zfill(6)
    status = str(record.get('status', '')).strip()
    note = str(record.get('build_note', '')).strip()
    cleaned_note = _merge_build_notes(_strip_reconcile_auto_tags(note))
    if status in {'holding', 'paused'} and code in pos_map:
        record = _overlay_record_from_position(record, pos_map.get(code) or {})
        record = _ensure_live_position_target_amount(record, pos=pos_map.get(code) or {})
        normalized_note = _merge_build_notes(record.get('build_note', ''))
        if normalized_note != str(record.get('build_note', '')).strip():
            record['build_note'] = normalized_note
        return record
    if status == 'closed' and _has_native_strategy_context(record) and cleaned_note != note:
        record['build_note'] = cleaned_note
    return record


def _is_retired_sync_artifact(record):
    record = _normalize_record(record)
    note = str(record.get('build_note', '')).strip()
    return '[AUTO_IMPORTED]' in note


def repair_track_record_from_live_positions(*, records=None, positions=None, orders=None, pending_items=None, save=False):
    records = [_normalize_record(r) for r in (records if records is not None else load_track_record())]
    positions = positions if positions is not None else get_positions()
    orders = orders if orders is not None else get_orders()
    pending_items = pending_items if pending_items is not None else refresh_pending_orders(orders=orders, positions=positions)
    records, reconcile_changed, reconcile_summary = full_reconcile_positions(
        records,
        positions=positions,
        orders=orders,
        pending_items=pending_items,
    )
    active_pos_map = _active_position_map(positions)
    cleaned_records = []
    cleaned_count = 0
    purged_count = 0
    for raw in records:
        if _is_retired_sync_artifact(raw):
            purged_count += 1
            continue
        cleaned = _clean_track_record_noise(raw, active_pos_map=active_pos_map)
        if cleaned != _normalize_record(raw):
            cleaned_count += 1
        cleaned_records.append(cleaned)
    if save and (reconcile_changed or cleaned_count > 0 or purged_count > 0):
        save_track_record(cleaned_records)
    return cleaned_records, {
        'changed': bool(reconcile_changed or cleaned_count > 0 or purged_count > 0),
        'reconcile_changed': bool(reconcile_changed),
        'cleaned_records': cleaned_count,
        'purged_retired_sync_records': purged_count,
        'reconcile_summary': reconcile_summary,
    }


def reconcile_after_trade(codes, *, records=None, trade_contexts=None):
    records = [_normalize_record(r) for r in (records if records is not None else load_track_record())]
    trade_contexts = trade_contexts or {}
    code_list = [str(code).zfill(6) for code in codes if str(code).strip()]
    if not code_list:
        balance = get_balance()
        positions = get_positions()
        return records, balance, positions, False

    balance = get_balance()
    positions = get_positions()
    orders = get_orders()
    pending_items = refresh_pending_orders(orders=orders, positions=positions)
    records, changed = sync_track_record(records, positions=positions, orders=orders, pending_items=pending_items)
    active_pos_map = _active_position_map(positions)

    for code in sorted(set(code_list)):
        ctx = trade_contexts.get(code, {})
        direction = str(ctx.get('direction', '')).strip().lower()
        pos = active_pos_map.get(code)
        idx = _find_record_index(records, code, statuses=('holding', 'paused'))

        if pos:
            if idx is None:
                records.append(_make_record_from_context(code, pos=pos, ctx=ctx))
                idx = len(records) - 1
                changed = True

            record = records[idx]
            if direction == 'buy':
                existing_ids = _split_order_ids(record.get('buy_order_ids', ''))
                fill = capture_trade_fill(
                    code,
                    'buy',
                    _inum(ctx.get('expected_quantity', 0), _inum(pos.get('count', 0), 0)),
                    existing_order_ids=existing_ids,
                    lookback_minutes=15,
                )
                if fill and _has_trade_price(fill):
                    record = apply_buy_fill(
                        record,
                        fill,
                        fallback_price=_fnum(pos.get('cost_price', 0.0), _fnum(ctx.get('fallback_price', 0.0), 0.0)),
                        fallback_quantity=_inum(pos.get('count', 0), _inum(ctx.get('fallback_quantity', 0), 0)),
                        note_suffix=str(ctx.get('note_suffix', '')).strip(),
                    )
            records[idx] = _overlay_record_from_position(record, pos)
            changed = True
            continue

        if direction == 'sell' and idx is not None:
            record = records[idx]
            fill = capture_trade_fill(
                code,
                'sell',
                _inum(ctx.get('expected_quantity', 0), _inum(record.get('quantity', 0), 0)),
                lookback_minutes=15,
            )
            if fill and _has_trade_price(fill):
                records[idx] = apply_sell_fill(
                    record,
                    fill,
                    fallback_price=_fnum(ctx.get('fallback_price', 0.0), _fnum(record.get('entry_price', 0.0), 0.0)),
                    close_reason=str(ctx.get('close_reason', 'post_trade_reconcile_sell')).strip(),
                )
                changed = True
            elif fill:
                records[idx] = _pause_record(record, 'post_trade_reconcile_sell_missing_trade_price')
                changed = True

    return records, balance, positions, changed


def sync_track_record(records, *, positions=None, orders=None, pending_items=None):
    records = [_normalize_record(r) for r in records]
    positions = positions if positions is not None else get_positions()
    orders = orders if orders is not None else get_orders()
    pending_items = pending_items if pending_items is not None else refresh_pending_orders(orders=orders, positions=positions)
    pos_map = _active_position_map(positions)
    filled_pending_sell = _filled_pending_sell_map(pending_items)
    used_sell_order_ids = {
        str(r.get('sell_order_id', '')).strip()
        for r in records
        if str(r.get('status', '')).strip() == 'closed' and str(r.get('sell_order_id', '')).strip()
    }
    changed = False
    for idx, record in enumerate(records):
        code = record.get('code', '')
        if record.get('status') in {'holding', 'paused'}:
            if not record.get('buy_order_ids'):
                fill = capture_trade_fill(code, 'buy', _inum(record.get('quantity', 0), 0), lookback_minutes=60 * 24)
                if fill and _has_trade_price(fill):
                    record = apply_buy_fill(
                        {**record, 'quantity': '0', 'buy_amount': '0'},
                        fill,
                        fallback_price=_fnum(record.get('entry_price', 0.0), 0.0),
                        fallback_quantity=_inum(record.get('quantity', 0), 0),
                    )
                    changed = True
            if code not in pos_map:
                sell_candidates = [
                    order for order in orders
                    if order['code'] == code
                    and order['direction'] == 2
                    and order['status'] == 4
                    and order['id'] not in used_sell_order_ids
                ]
                if sell_candidates:
                    qty = _inum(record.get('quantity', 0), 0)
                    matched = [
                        order for order in sell_candidates
                        if _inum(order.get('trade_count', 0), 0) in (0, qty) or _inum(order.get('count', 0), 0) in (0, qty)
                    ]
                    fill = max(matched or sell_candidates, key=lambda item: item['time'])
                    actual_trade_price = _fnum(fill.get('actual_trade_price', 0.0), 0.0)
                    if actual_trade_price > 0:
                        record = apply_sell_fill(
                            record,
                            {
                                'order_id': fill['id'],
                                'trade_time': fill['datetime'].strftime('%H:%M:%S') if fill['datetime'] else '',
                                'trade_date': fill['datetime'].strftime('%Y-%m-%d') if fill['datetime'] else '',
                                'trade_price': actual_trade_price,
                            },
                            fallback_price=actual_trade_price,
                            close_reason='sync_detected_sell_fill',
                        )
                        used_sell_order_ids.add(fill['id'])
                    else:
                        record = _pause_record(record, 'sync_detected_sell_missing_trade_price')
                    changed = True
                elif code in filled_pending_sell:
                    pending_fill = filled_pending_sell[code]
                    pending_order_id = str(pending_fill.get('order_id', '')).strip()
                    if pending_order_id and pending_order_id not in used_sell_order_ids:
                        fill_time = _parse_dt(pending_fill.get('filled_at')) or _parse_dt(pending_fill.get('recorded_at'))
                        trade_price = _fnum(pending_fill.get('trade_price', 0.0), 0.0)
                        if trade_price > 0:
                            record = apply_sell_fill(
                                record,
                                {
                                    'order_id': pending_order_id,
                                    'trade_time': fill_time.strftime('%H:%M:%S') if fill_time else '',
                                    'trade_date': fill_time.strftime('%Y-%m-%d') if fill_time else '',
                                    'trade_price': trade_price,
                                },
                                fallback_price=trade_price,
                                close_reason='pending_orders_filled_sell',
                            )
                            used_sell_order_ids.add(pending_order_id)
                        else:
                            record = _pause_record(record, 'pending_filled_sell_missing_trade_price')
                        changed = True
                elif _is_legacy_holding_record(record):
                    record = _pause_record(record, 'missing_active_position')
                    changed = True
            else:
                pos = pos_map.get(code)
                if pos and _is_legacy_holding_record(record) and _track_qty_mismatch(record, pos):
                    tracked_qty = _inum(record.get('quantity', 0), 0)
                    actual_qty = _inum(pos.get('count', 0), 0)
                    record = _pause_record(record, f'position_qty_mismatch tracked={tracked_qty} actual={actual_qty}')
                    changed = True
        record['last_synced_at'] = _now_str()
        records[idx] = record
    return records, changed


def full_reconcile_positions(records, *, positions=None, orders=None, pending_items=None):
    records = [_normalize_record(r) for r in records]
    positions = positions if positions is not None else get_positions()
    orders = orders if orders is not None else get_orders()
    pending_items = pending_items if pending_items is not None else refresh_pending_orders(orders=orders, positions=positions)
    decision_by_code = _build_selected_decision_code_map(_read_jsonl(MODEL_DECISIONS_FILE, limit=5000))
    pending_state = summarize_pending_orders(pending_items)
    active_pos_map = _active_position_map(positions)
    active_sell_codes = set(pending_state.get('active_sell_codes', []))
    changed = False
    imported = 0
    overlaid = 0
    paused = 0

    for code, pos in active_pos_map.items():
        idx = _find_record_index(records, code, statuses=('holding', 'paused'))
        if idx is None:
            native_source = _find_latest_record_by_code(records, code, require_native_context=True)
            native_note = _strip_reconcile_auto_tags((native_source or {}).get('build_note', ''))
            decision = decision_by_code.get(str(code).zfill(6), {})
            decision_date = _date_key(decision.get('trade_date'))
            decision_id = str(decision.get('decision_id', '')).strip()
            decision_run_slot = str(decision.get('decision_run_slot', '')).strip()
            decision_reason_hash = str(decision.get('selected_reason_hash', '')).strip()
            decision_mode = str(decision.get('mode', '')).strip()
            decision_tier = str(decision.get('tier', '')).strip()
            decision_target_amount = f"{_fnum(decision.get('target_amount', 0.0), 0.0):.0f}" if _fnum(decision.get('target_amount', 0.0), 0.0) > 0 else ''
            decision_has_native_context = bool(
                decision_id or decision_run_slot or decision_reason_hash or decision_mode or decision_tier
            )
            imported_note = native_note
            if not imported_note and decision_has_native_context:
                imported_note = '[AUTO_RECOVERED] selected_decision_context'
            if not imported_note:
                imported_note = '[LIVE_POSITION_ONLY] full_reconcile_from_positions'
            records.append(_make_record_from_context(
                code,
                pos=pos,
                ctx={
                    'date': str((native_source or {}).get('date', '')).strip() or decision_date or datetime.now().strftime('%Y-%m-%d'),
                    'buy_time': str((native_source or {}).get('buy_time', '')).strip(),
                    'buy_order_ids': str((native_source or {}).get('buy_order_ids', '')).strip(),
                    'name': pos.get('name', ''),
                    'tier': str((native_source or {}).get('tier', '')).strip() or decision_tier,
                    'mode': str((native_source or {}).get('mode', '')).strip() or decision_mode,
                    'build_note': imported_note,
                    'target_amount': str((native_source or {}).get('target_amount', '')).strip() or decision_target_amount,
                    'decision_id': str((native_source or {}).get('decision_id', '')).strip() or decision_id,
                    'decision_run_slot': str((native_source or {}).get('decision_run_slot', '')).strip() or decision_run_slot,
                    'selected_reason_hash': str((native_source or {}).get('selected_reason_hash', '')).strip() or decision_reason_hash,
                },
            ))
            idx = len(records) - 1
            imported += 1
            changed = True
        updated = _overlay_record_from_position(records[idx], pos)
        updated = _recover_live_position_record(
            updated,
            records=records,
            selected_decision=decision_by_code.get(str(code).zfill(6), {}),
        )
        updated = _ensure_live_position_target_amount(updated, pos=pos)
        if updated != records[idx]:
            overlaid += 1
            changed = True
        records[idx] = updated

    for idx, record in enumerate(records):
        if record.get('status') != 'holding':
            continue
        code = str(record.get('code', '')).zfill(6)
        if code in active_pos_map or code in active_sell_codes:
            continue
        if _is_legacy_holding_record(record):
            records[idx] = _pause_record(record, 'missing_after_full_reconcile')
            paused += 1
            changed = True

    return records, changed, {
        'imported_positions': imported,
        'overlaid_positions': overlaid,
        'paused_records': paused,
        'pending': pending_state,
    }


def load_scan_signals(csv_path=None):
    """读取V10扫描结果"""
    if csv_path is None:
        csv_path = load_scan_context()['csv_path']
    signals = {1: [], 2: [], 3: []}
    if not os.path.exists(csv_path):
        print(f"[WARN] 扫描文件不存在: {csv_path}")
        return signals
    with open(csv_path, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            tier = _inum(row.get('tier', 0), 0)
            if tier in signals:
                signals[tier].append(row)
    for t in signals:
        signals[t].sort(key=lambda r: _fnum(r.get('weekly_slope', 0), 0.0), reverse=True)
    return signals


POSITION_STATE_FIELDS = (
    'date', 'buy_time', 'buy_order_ids', 'name', 'tier', 'mode',
    'build_note', 'target_amount', 'decision_id', 'decision_run_slot',
    'selected_reason_hash',
    'big_meat_state', 'big_meat_score', 'big_meat_aggressive_score',
    'big_meat_reason', 'big_meat_window_tag', 'big_meat_first_seen_at',
    'big_meat_confirmed_at', 'big_meat_last_eval_at',
    'holding_big_meat_score', 'holding_big_meat_reason', 'holding_big_meat_promoted_at',
    'big_meat_hold_state', 'big_meat_core_qty', 'big_meat_trade_qty',
    'big_meat_hold_lock_until',
)


def _load_position_state():
    payload = _read_json(POSITION_STATE_FILE)
    entries = payload.get('entries', {}) if isinstance(payload, dict) else {}
    state = {}
    for code, raw in (entries or {}).items():
        norm_code = str(code).zfill(6)
        entry = {}
        source = raw if isinstance(raw, dict) else {}
        for field in POSITION_STATE_FIELDS:
            value = str(source.get(field, '')).strip()
            if value:
                entry[field] = value
        if entry:
            state[norm_code] = entry
    return state


def _save_position_state(entries):
    normalized = {}
    for code, raw in (entries or {}).items():
        norm_code = str(code).zfill(6)
        if not norm_code:
            continue
        item = {}
        source = raw if isinstance(raw, dict) else {}
        for field in POSITION_STATE_FIELDS:
            value = str(source.get(field, '')).strip()
            if value:
                item[field] = value
        if item:
            normalized[norm_code] = item
    _write_json_atomic(POSITION_STATE_FILE, {
        'generated_at': _now_str(),
        'entries': normalized,
    })


def _extract_position_state_from_record(record):
    record = _normalize_record(record)
    entry = {}
    for field in POSITION_STATE_FIELDS:
        value = str(record.get(field, '')).strip()
        if value:
            entry[field] = value
    return entry


def _resolve_runtime_note_from_decision(decision_row):
    decision_row = decision_row if isinstance(decision_row, dict) else {}
    tier = _inum(decision_row.get('tier', 0), 0)
    base_note = {
        1: '超级大行情满仓首建',
        2: '首建60%',
        3: '首建50%',
    }.get(tier, '[MX_MONI_RUNTIME] selected_decision_context')
    score = _fnum(decision_row.get('score', 0.0), 0.0)
    components = decision_row.get('components', {}) if isinstance(decision_row.get('components', {}), dict) else {}
    if score <= 0:
        return base_note
    market_score = _fnum(components.get('market', 0.0), 0.0)
    sector_score = _fnum(components.get('sector', 0.0), 0.0)
    stock_score = _fnum(components.get('stock', 0.0), 0.0)
    flow_score = _fnum(components.get('flow', 0.0), 0.0)
    industry = str(decision_row.get('industry', 'unknown') or 'unknown').strip()
    return (
        f"{base_note} | 模型{score:.1f} "
        f"(市{market_score:.0f}/板{sector_score:.0f}/股{stock_score:.0f}/流{flow_score:.0f}) "
        f"[{industry}]"
    )


def _resolve_runtime_decision_row(code, *, trade_date='', decision_reference=None):
    reference = decision_reference if isinstance(decision_reference, dict) else {}
    code = str(code).zfill(6)
    trade_date_key = _date_key(trade_date)
    if trade_date_key:
        row = (reference.get('by_trade_date_code') or {}).get((trade_date_key, code))
        if row:
            return row
    return (reference.get('by_code_latest') or {}).get(code, {})


def _trade_fill_payload_from_event(event):
    event = event if isinstance(event, dict) else {}
    logged_at = str(event.get('logged_at', '')).strip()
    trade_date = _date_key(event.get('trade_date') or logged_at)
    trade_time = ''
    if ' ' in logged_at:
        trade_time = logged_at.split(' ', 1)[1].strip()
    return {
        'order_id': str(event.get('order_id', '')).strip(),
        'trade_date': trade_date,
        'trade_time': trade_time,
        'trade_price': _fnum(event.get('price', 0.0), 0.0),
        'trade_count': _inum(event.get('quantity', 0), 0),
    }


def _runtime_close_reason_from_event(event):
    event = event if isinstance(event, dict) else {}
    strategy_action = str(event.get('strategy_action', '')).strip() or 'mx_moni'
    return f'mx_moni_sell_fill[{strategy_action}]'


def _build_runtime_record_context(code, *, fill_event=None, decision_row=None, base_record=None, state_entry=None, pos=None):
    code = str(code).zfill(6)
    fill_event = fill_event if isinstance(fill_event, dict) else {}
    decision_row = decision_row if isinstance(decision_row, dict) else {}
    base_record = _normalize_record(base_record) if isinstance(base_record, dict) and base_record else {}
    state_entry = state_entry if isinstance(state_entry, dict) else {}
    pos = pos if isinstance(pos, dict) else {}
    quantity = _inum(
        fill_event.get('quantity', 0),
        _inum(pos.get('count', 0), _inum(base_record.get('quantity', 0), 0)),
    )
    price = _fnum(
        fill_event.get('price', 0.0),
        _fnum(pos.get('cost_price', 0.0), _fnum(base_record.get('entry_price', 0.0), 0.0)),
    )
    trade_date = (
        str(state_entry.get('date', '')).strip()
        or str(base_record.get('date', '')).strip()
        or _date_key(fill_event.get('trade_date') or fill_event.get('logged_at'))
        or _date_key(decision_row.get('trade_date'))
        or datetime.now().strftime('%Y-%m-%d')
    )
    build_note = (
        str(state_entry.get('build_note', '')).strip()
        or str(base_record.get('build_note', '')).strip()
        or _resolve_runtime_note_from_decision(decision_row)
    )
    target_amount = (
        str(state_entry.get('target_amount', '')).strip()
        or str(base_record.get('target_amount', '')).strip()
        or (
            f"{_fnum(decision_row.get('target_amount', 0.0), 0.0):.0f}"
            if _fnum(decision_row.get('target_amount', 0.0), 0.0) > 0 else ''
        )
        or (f"{price * quantity:.0f}" if price > 0 and quantity > 0 else '')
    )
    ctx = {
        'date': trade_date,
        'buy_time': (
            str(state_entry.get('buy_time', '')).strip()
            or str(base_record.get('buy_time', '')).strip()
            or (str(fill_event.get('logged_at', '')).split(' ', 1)[1].strip() if ' ' in str(fill_event.get('logged_at', '')).strip() else '')
        ),
        'buy_order_ids': (
            str(state_entry.get('buy_order_ids', '')).strip()
            or str(base_record.get('buy_order_ids', '')).strip()
            or str(fill_event.get('order_id', '')).strip()
        ),
        'name': (
            str(pos.get('name', '')).strip()
            or str(state_entry.get('name', '')).strip()
            or str(base_record.get('name', '')).strip()
            or str(decision_row.get('name', '')).strip()
        ),
        'tier': (
            str(state_entry.get('tier', '')).strip()
            or str(base_record.get('tier', '')).strip()
            or str(decision_row.get('tier', '')).strip()
        ),
        'mode': (
            str(state_entry.get('mode', '')).strip()
            or str(base_record.get('mode', '')).strip()
            or str(decision_row.get('mode', '')).strip()
        ),
        'build_note': build_note,
        'target_amount': target_amount,
        'decision_id': (
            str(state_entry.get('decision_id', '')).strip()
            or str(base_record.get('decision_id', '')).strip()
            or str(decision_row.get('decision_id', '')).strip()
        ),
        'decision_run_slot': (
            str(state_entry.get('decision_run_slot', '')).strip()
            or str(base_record.get('decision_run_slot', '')).strip()
            or str(decision_row.get('decision_run_slot', '')).strip()
        ),
        'selected_reason_hash': (
            str(state_entry.get('selected_reason_hash', '')).strip()
            or str(base_record.get('selected_reason_hash', '')).strip()
            or str(decision_row.get('selected_reason_hash', '')).strip()
        ),
        'fallback_quantity': quantity,
        'fallback_price': price,
    }
    for field in POSITION_STATE_FIELDS:
        if field in ctx:
            continue
        value = (
            str(state_entry.get(field, '')).strip()
            or str(base_record.get(field, '')).strip()
        )
        if value:
            ctx[field] = value
    return ctx


def _apply_runtime_context_to_record(record, ctx):
    record = _normalize_record(record)
    ctx = ctx if isinstance(ctx, dict) else {}
    for field in POSITION_STATE_FIELDS:
        value = str(ctx.get(field, '')).strip()
        if value:
            record[field] = value
    if str(ctx.get('date', '')).strip():
        record['date'] = str(ctx.get('date', '')).strip()
    if str(ctx.get('buy_time', '')).strip():
        record['buy_time'] = str(ctx.get('buy_time', '')).strip()
    if str(ctx.get('buy_order_ids', '')).strip():
        record['buy_order_ids'] = str(ctx.get('buy_order_ids', '')).strip()
    if str(ctx.get('name', '')).strip():
        record['name'] = str(ctx.get('name', '')).strip()
    record['last_synced_at'] = _now_str()
    return _sync_big_meat_position_split(record)


def _repair_runtime_record_identity(record, *, code='', name='', decision_row=None):
    record = _normalize_record(record)
    decision_row = decision_row if isinstance(decision_row, dict) else {}
    resolved_code = str(code or record.get('code', '')).strip()
    if not resolved_code or resolved_code == '000000':
        resolved_code = str(decision_row.get('code', '')).strip()
    if resolved_code:
        record['code'] = str(resolved_code).zfill(6)
    resolved_name = str(name or record.get('name', '')).strip()
    if not resolved_name:
        resolved_name = str(decision_row.get('name', '')).strip()
    if resolved_name:
        record['name'] = resolved_name
    return record


def _build_runtime_trade_records(*, decision_reference=None):
    reference = decision_reference if isinstance(decision_reference, dict) else _build_selected_decision_reference(
        _read_jsonl(MODEL_DECISIONS_FILE, limit=5000)
    )
    fill_index = _build_trade_fill_index()
    events = []
    for items in fill_index.values():
        events.extend(items)
    events.sort(key=lambda item: (
        str(item.get('logged_at', '')).strip(),
        0 if str(item.get('action', '')).strip() == 'buy' else 1,
        str(item.get('order_id', '')).strip(),
    ))
    open_records = {}
    closed_records = []
    for event in events:
        code = str(event.get('code', '')).zfill(6)
        action = str(event.get('action', '')).strip()
        if not code or action not in {'buy', 'sell'}:
            continue
        fill_payload = _trade_fill_payload_from_event(event)
        if action == 'buy':
            record = _normalize_record(open_records.get(code, {}))
            decision_row = _resolve_runtime_decision_row(code, trade_date=fill_payload.get('trade_date', ''), decision_reference=reference)
            if not record:
                ctx = _build_runtime_record_context(code, fill_event=event, decision_row=decision_row)
                record = _make_record_from_context(code, ctx=ctx)
                record['quantity'] = '0'
                record['buy_amount'] = '0'
                record['entry_price'] = '0'
                record = _apply_runtime_context_to_record(record, ctx)
            record = apply_buy_fill(
                record,
                fill_payload,
                fallback_price=_fnum(fill_payload.get('trade_price', 0.0), 0.0),
                fallback_quantity=_inum(fill_payload.get('trade_count', 0), 0),
            )
            ctx = _build_runtime_record_context(code, fill_event=event, decision_row=decision_row, base_record=record)
            record = _apply_runtime_context_to_record(record, ctx)
            open_records[code] = _repair_runtime_record_identity(
                record,
                code=code,
                name=str(ctx.get('name', '')).strip() or str(event.get('name', '')).strip(),
                decision_row=decision_row,
            )
            continue

        record = _normalize_record(open_records.get(code, {}))
        open_qty = _inum(record.get('quantity', 0), 0)
        sell_qty = min(_inum(fill_payload.get('trade_count', 0), 0), open_qty)
        if sell_qty <= 0:
            continue
        closed_piece = _normalize_record(dict(record))
        closed_piece['quantity'] = str(sell_qty)
        closed_piece['buy_amount'] = f"{_fnum(record.get('entry_price', 0.0), 0.0) * sell_qty:.2f}"
        closed_piece = _sync_big_meat_position_split(closed_piece, qty=sell_qty, reset_core=True)
        closed_piece = apply_sell_fill(
            closed_piece,
            fill_payload,
            fallback_price=_fnum(fill_payload.get('trade_price', 0.0), 0.0),
            close_reason=_runtime_close_reason_from_event(event),
        )
        closed_piece = _repair_runtime_record_identity(
            closed_piece,
            code=code,
            name=str(record.get('name', '')).strip() or str(event.get('name', '')).strip(),
            decision_row=_resolve_runtime_decision_row(
                code,
                trade_date=str(record.get('date', '')).strip(),
                decision_reference=reference,
            ),
        )
        closed_records.append(closed_piece)
        remaining_qty = max(0, open_qty - sell_qty)
        if remaining_qty <= 0:
            open_records.pop(code, None)
            continue
        record['quantity'] = str(remaining_qty)
        record['buy_amount'] = f"{_fnum(record.get('entry_price', 0.0), 0.0) * remaining_qty:.2f}"
        record['last_synced_at'] = _now_str()
        record = _sync_big_meat_position_split(record, qty=remaining_qty)
        open_records[code] = _repair_runtime_record_identity(
            record,
            code=code,
            name=str(record.get('name', '')).strip(),
            decision_row=_resolve_runtime_decision_row(
                code,
                trade_date=str(record.get('date', '')).strip(),
                decision_reference=reference,
            ),
        )
    closed_records.sort(key=lambda item: (
        str(item.get('sell_date', '')),
        str(item.get('sell_time', '')),
        str(item.get('date', '')),
        str(item.get('code', '')),
    ))
    return closed_records, open_records


def _build_runtime_holding_records(*, positions=None, decision_reference=None, position_state=None, open_records=None):
    positions = positions if positions is not None else get_positions()
    reference = decision_reference if isinstance(decision_reference, dict) else _build_selected_decision_reference(
        _read_jsonl(MODEL_DECISIONS_FILE, limit=5000)
    )
    state_map = position_state if isinstance(position_state, dict) else _load_position_state()
    open_map = open_records if isinstance(open_records, dict) else {}
    holdings = []
    for pos in positions or []:
        if not _has_active_position(pos):
            continue
        code = str(pos.get('code', '')).zfill(6)
        base_record = _normalize_record(open_map.get(code, {}))
        state_entry = state_map.get(code, {})
        decision_row = _resolve_runtime_decision_row(
            code,
            trade_date=str(base_record.get('date', '')).strip() or str(state_entry.get('date', '')).strip(),
            decision_reference=reference,
        )
        ctx = _build_runtime_record_context(
            code,
            decision_row=decision_row,
            base_record=base_record,
            state_entry=state_entry,
            pos=pos,
        )
        record = _make_record_from_context(code, pos=pos, ctx=ctx)
        record = _apply_runtime_context_to_record(record, ctx)
        record = _overlay_record_from_position(record, pos)
        record['status'] = 'holding'
        record['sell_date'] = ''
        record['sell_time'] = ''
        record['sell_price'] = ''
        record['sell_order_id'] = ''
        record['pnl'] = ''
        record['pnl_pct'] = ''
        record['hold_days'] = ''
        record['close_reason'] = ''
        record['last_synced_at'] = _now_str()
        holdings.append(record)
    holdings.sort(key=lambda item: (str(item.get('date', '')), str(item.get('code', ''))))
    return holdings


def load_track_record(*, positions=None, decision_reference=None):
    """加载运行时战绩视图。

    当前持仓以 mx moni 为唯一账户真相，策略状态来自本地决策日志/状态文件，
    不再把 v10_track_record.csv 作为持仓主账本。
    """
    reference = decision_reference if isinstance(decision_reference, dict) else _build_selected_decision_reference(
        _read_jsonl(MODEL_DECISIONS_FILE, limit=5000)
    )
    active_positions = positions if positions is not None else get_positions()
    state_map = _load_position_state()
    closed_records, open_records = _build_runtime_trade_records(decision_reference=reference)
    holding_records = _build_runtime_holding_records(
        positions=active_positions,
        decision_reference=reference,
        position_state=state_map,
        open_records=open_records,
    )
    return [_normalize_record(r) for r in (closed_records + holding_records)]


def save_track_record(records):
    """保存持仓策略状态。

    账户账本以 mx moni 为准，这里只持久化本地策略语义字段，
    供下次从 mx moni 实仓重建当前持仓视图。
    """
    entries = {}
    for raw in records or []:
        record = _normalize_record(raw)
        if str(record.get('status', '')).strip() != 'holding':
            continue
        code = str(record.get('code', '')).zfill(6)
        if not code:
            continue
        entries[code] = _extract_position_state_from_record(record)
    _save_position_state(entries)
    print(f" 持仓策略状态已保存: {POSITION_STATE_FILE} (mx moni 为账户真相，已停用本地持仓账本主链)")


def compute_track_stats(records):
    records = [_normalize_record(r) for r in records]
    native_records = [r for r in records if _is_native_strategy_record(r)]
    non_native_records = [r for r in records if not _is_native_strategy_record(r)]
    holding = [r for r in native_records if r.get('status') == 'holding']
    closed = [r for r in native_records if r.get('status') == 'closed']
    all_holding = [r for r in records if r.get('status') == 'holding']
    all_closed = [r for r in records if r.get('status') == 'closed']
    wins = [r for r in closed if _fnum(r.get('pnl', 0.0), 0.0) > 0]
    total_pnl = sum(_fnum(r.get('pnl', 0.0), 0.0) for r in closed)
    avg_pnl_pct = (
        sum(_fnum(r.get('pnl_pct', 0.0), 0.0) for r in closed) / len(closed)
        if closed else 0.0
    )
    wr = len(wins) / len(closed) * 100 if closed else 0.0
    tier_stats = {}
    mode_stats = {}
    for r in closed:
        tier = _inum(r.get('tier', 0), 0)
        mode = str(r.get('mode', '')).strip()
        tier_stats.setdefault(tier, []).append(r)
        mode_stats.setdefault(mode, []).append(r)
    return {
        'holding_count': len(holding),
        'closed_count': len(closed),
        'win_count': len(wins),
        'win_rate_pct': round(wr, 2),
        'avg_return_pct': round(avg_pnl_pct, 4),
        'realized_pnl': round(total_pnl, 2),
        'all_holding_count': len(all_holding),
        'all_closed_count': len(all_closed),
        'native_record_count': len(native_records),
        'all_record_count': len(records),
        'non_native_count': len(non_native_records),
        'tier_stats': tier_stats,
        'mode_stats': mode_stats,
    }


def _is_native_strategy_record(record):
    record = record if isinstance(record, dict) else {}
    build_note = str(record.get('build_note', '')).strip()
    target_amount = _fnum(record.get('target_amount', 0.0), 0.0)
    return '[LIVE_POSITION_ONLY]' not in build_note and target_amount > 0


def _normalize_market_regime(value):
    text = str(value or '').strip()
    return text or 'unknown'


def _mode_profile_key(mode, market_regime=''):
    mode_text = str(mode or '').strip()
    if not mode_text:
        return ''
    regime = _normalize_market_regime(market_regime)
    return f'{regime}::{mode_text}' if regime and regime != 'unknown' else mode_text


def _build_selected_decision_reference(rows):
    reference = {
        'by_id': {},
        'by_trade_date_code': {},
        'by_code_latest': {},
    }
    for row in rows or []:
        if not bool(row.get('selected')):
            continue
        decision_id = str(row.get('decision_id', '')).strip()
        if decision_id:
            reference['by_id'][decision_id] = row
        trade_date = _date_key(row.get('trade_date'))
        code = str(row.get('code', '')).zfill(6)
        if trade_date and code:
            reference['by_trade_date_code'][(trade_date, code)] = row
            latest = reference['by_code_latest'].get(code)
            latest_key = (
                _date_key((latest or {}).get('trade_date')),
                str((latest or {}).get('recorded_at', '')).strip(),
            ) if isinstance(latest, dict) else ('', '')
            current_key = (
                trade_date,
                str(row.get('recorded_at', '')).strip(),
            )
            if current_key >= latest_key:
                reference['by_code_latest'][code] = row
    return reference


def _resolve_record_decision_row(record, decision_reference=None):
    record = record if isinstance(record, dict) else {}
    reference = decision_reference if isinstance(decision_reference, dict) else {}
    decision_id = str(record.get('decision_id', '')).strip()
    if decision_id:
        row = (reference.get('by_id') or {}).get(decision_id)
        if row:
            return row
    trade_date = _date_key(record.get('date'))
    code = str(record.get('code', '')).zfill(6)
    return (reference.get('by_trade_date_code') or {}).get((trade_date, code), {})


def _has_capital_bias_note(record):
    note = str((record or {}).get('build_note', '')).strip()
    return '盈利倾斜' in note


def _is_partial_rollback_blocked_buy(*, tier=0, mode='', pm_buy_guard=None):
    mode_text = str(mode or '').strip()
    tier_num = _inum(tier, 0)
    if PARTIAL_ROLLBACK_DISABLE_FULL_V9_BUILD and tier_num == 1 and mode_text == 'V9_full':
        return True, '部分回退: 禁用T1 V9_full满仓首建'
    return False, ''


def _summarize_return_stats(records):
    rows = list(records or [])
    count = len(rows)
    wins = len([row for row in rows if _fnum(row.get('pnl_pct', 0.0), 0.0) > 0])
    avg_return_pct = (
        sum(_fnum(row.get('pnl_pct', 0.0), 0.0) for row in rows) / count
        if count else 0.0
    )
    return {
        'count': count,
        'win_rate_pct': round(wins / count * 100.0, 2) if count else 0.0,
        'avg_return_pct': round(avg_return_pct, 4),
    }


def _build_mode_capital_profile(records):
    decision_reference = _build_selected_decision_reference(_read_jsonl(MODEL_DECISIONS_FILE, limit=5000))
    closed_native = [
        _normalize_record(r)
        for r in (records or [])
        if str((r or {}).get('status', '')).strip() == 'closed' and _is_native_strategy_record(r)
    ]
    grouped = {}
    grouped_meta = {}
    for record in closed_native:
        mode = str(record.get('mode', '')).strip()
        if not mode:
            continue
        decision_row = _resolve_record_decision_row(record, decision_reference)
        market_regime = _normalize_market_regime(decision_row.get('market_regime', ''))
        keys = [mode]
        composite_key = _mode_profile_key(mode, market_regime)
        if composite_key and composite_key != mode:
            keys.append(composite_key)
        for key in keys:
            grouped.setdefault(key, []).append(record)
            grouped_meta.setdefault(key, {
                'mode': mode,
                'market_regime': market_regime if key != mode else '',
            })

    profile = {}
    for profile_key, items in grouped.items():
        sample_count = len(items)
        win_count = len([item for item in items if _fnum(item.get('pnl_pct', 0.0), 0.0) > 0])
        win_rate_pct = win_count / sample_count * 100.0 if sample_count else 0.0
        avg_return_pct = (
            sum(_fnum(item.get('pnl_pct', 0.0), 0.0) for item in items) / sample_count
            if sample_count else 0.0
        )
        confidence = min(sample_count / MODE_CAPITAL_PROFILE_SAMPLE_SCALE, 1.0)
        raw_edge = avg_return_pct + (win_rate_pct - 50.0) * 0.12
        adjusted_edge = raw_edge * confidence
        legacy_target_multiplier = max(
            MODE_CAPITAL_TARGET_MULTIPLIER_MIN,
            min(MODE_CAPITAL_TARGET_MULTIPLIER_MAX, 1.0 + adjusted_edge / 20.0),
        )
        legacy_initial_multiplier = max(
            MODE_CAPITAL_INITIAL_MULTIPLIER_MIN,
            min(MODE_CAPITAL_INITIAL_MULTIPLIER_MAX, 1.0 + adjusted_edge / 18.0),
        )
        ranking_bonus = max(-3.0, min(3.0, adjusted_edge * 0.8))
        meta = grouped_meta.get(profile_key, {})
        mode = str(meta.get('mode', profile_key)).strip()
        if adjusted_edge >= 2.0:
            bias_label = 'profit_priority'
        elif adjusted_edge <= -1.5:
            bias_label = 'capital_conserve'
        else:
            bias_label = 'neutral'
        profile[profile_key] = {
            'mode': mode,
            'market_regime': str(meta.get('market_regime', '')).strip(),
            'sample_count': sample_count,
            'win_rate_pct': round(win_rate_pct, 2),
            'avg_return_pct': round(avg_return_pct, 4),
            'confidence': round(confidence, 2),
            'edge_score': round(adjusted_edge, 4),
            'target_multiplier': 1.0,
            'initial_multiplier': 1.0,
            'legacy_target_multiplier': round(legacy_target_multiplier, 4),
            'legacy_initial_multiplier': round(legacy_initial_multiplier, 4),
            'ranking_bonus': round(ranking_bonus, 2),
            'bias_label': bias_label,
        }
    return profile


def _resolve_mode_capital_plan(mode, *, base_target_amount, base_initial_amount, mode_capital_profile=None, market_regime=''):
    base_target_amount = _fnum(base_target_amount, 0.0)
    base_initial_amount = _fnum(base_initial_amount, 0.0)
    mode_text = str(mode or '').strip()
    profile = {}
    if isinstance(mode_capital_profile, dict):
        composite_key = _mode_profile_key(mode_text, market_regime)
        profile = mode_capital_profile.get(composite_key, {}) or mode_capital_profile.get(mode_text, {})
    raw_ranking_bonus = _fnum(profile.get('ranking_bonus', 0.0), 0.0)
    ranking_bonus = raw_ranking_bonus
    if PARTIAL_ROLLBACK_DISABLE_POSITIVE_CAPITAL_BIAS and ranking_bonus > 0:
        ranking_bonus = 0.0
    target_amount = round(base_target_amount, 2)
    initial_amount = round(min(target_amount, base_initial_amount), 2)
    note = ''
    if abs(ranking_bonus) >= 0.5:
        direction = '优先' if ranking_bonus >= 0.0 else '后置'
        regime_note = str(profile.get('market_regime', '')).strip()
        note = (
            f"盈利倾斜参考{direction}"
            f"(排序{ranking_bonus:+.1f}/"
            f"样本{_inum(profile.get('sample_count', 0), 0)}"
            f"/均收{_fnum(profile.get('avg_return_pct', 0.0), 0.0):+.2f}%"
            f"{f'/市况{regime_note}' if regime_note else ''})"
        )
    elif PARTIAL_ROLLBACK_DISABLE_POSITIVE_CAPITAL_BIAS and raw_ranking_bonus > 0:
        regime_note = str(profile.get('market_regime', '')).strip()
        note = (
            "盈利倾斜正向加码已停用"
            f"(原排序{raw_ranking_bonus:+.1f}/"
            f"样本{_inum(profile.get('sample_count', 0), 0)}"
            f"/均收{_fnum(profile.get('avg_return_pct', 0.0), 0.0):+.2f}%"
            f"{f'/市况{regime_note}' if regime_note else ''})"
        )
    return {
        'target_amount': target_amount,
        'initial_amount': initial_amount,
        'ranking_bonus': round(ranking_bonus, 2),
        'note': note,
        'profile': profile,
    }


def _resolve_add_position_target_amount(target_amount, *, record=None, mode_capital_profile=None, market_regime='', big_meat_profile=None):
    target_amount = _fnum(target_amount, 0.0)
    if target_amount <= 0:
        return {
            'target_amount': 0.0,
            'capital_note': '',
            'aggressive_add_note': '',
            'mode_profile': {},
        }
    record = record if isinstance(record, dict) else {}
    mode = str(record.get('mode', '')).strip()
    mode_plan = _resolve_mode_capital_plan(
        mode,
        base_target_amount=target_amount,
        base_initial_amount=target_amount,
        mode_capital_profile=mode_capital_profile if isinstance(mode_capital_profile, dict) else {},
        market_regime=market_regime,
    )
    adjusted_target = _fnum(mode_plan.get('target_amount', target_amount), target_amount)
    capital_note = str(mode_plan.get('note', '')).strip()
    aggressive_add_note = ''
    profile = big_meat_profile if isinstance(big_meat_profile, dict) else {}
    if profile.get('eligible'):
        multiplier = max(_fnum(profile.get('target_multiplier', 1.0), 1.0), 1.0)
        adjusted_target = min(max(adjusted_target, target_amount) * multiplier, target_amount * MODE_CAPITAL_ADD_POSITION_TARGET_MAX)
        reason = str(profile.get('reason', '')).strip()
        aggressive_add_note = f"大肉激进加仓: 目标提升至{adjusted_target / target_amount:.2f}x"
        if reason:
            aggressive_add_note = f"{aggressive_add_note} ({reason})"
    return {
        'target_amount': round(adjusted_target, 2),
        'capital_note': capital_note,
        'aggressive_add_note': aggressive_add_note,
        'mode_profile': mode_plan.get('profile', {}),
    }


def _build_big_meat_buy_seed_profile(item, *, original_tier=0, recent_adjustment=None):
    item = item if isinstance(item, dict) else {}
    recent_adjustment = recent_adjustment if isinstance(recent_adjustment, dict) else {}
    mode = str(item.get('mode', '')).strip()
    model_score = _fnum(item.get('model_score', 0.0), 0.0)
    market_score = _fnum(item.get('model_market_score', 0.0), 0.0)
    sector_score = _fnum(item.get('model_sector_score', 0.0), 0.0)
    stock_score = _fnum(item.get('model_stock_score', 0.0), 0.0)
    flow_score = _fnum(item.get('model_flow_score', 0.0), 0.0)
    tier_num = _inum(original_tier or item.get('tier', 0), 0)
    score = 0.0
    notes = []

    if tier_num == 1 or mode == 'V9_full':
        return {
            'effective_tier': 1,
            'seed_score': 9.0,
            'ranking_bonus': 2.5,
            'target_amount_ratio': 1.0,
            'initial_amount_ratio': 1.0,
            'pool_note': '',
            'pool_label': 'T1核心',
            'reason': 'V9_full/T1 强确认核心',
            'priority_rank': 3,
        }

    mode_seed_bonus = {
        'near_kill+weekly+MA20': 1.8,
        'trend_only': 1.5,
        'vol_breakout': 1.2,
        'trend_ride+green': 0.8,
        'kill_only': 0.5,
    }
    mode_bonus = _fnum(mode_seed_bonus.get(mode, 0.3), 0.3)
    if mode_bonus > 0:
        score += mode_bonus
        notes.append(f'{mode or "mode?"}种子')

    if tier_num == 2:
        score += 0.7
        notes.append('原T2入围')
    elif tier_num == 3:
        score += 0.2
        notes.append('原T3观察')

    if model_score >= 80:
        score += 1.2
        notes.append(f'总分{model_score:.1f}')
    elif model_score >= 76:
        score += 0.9
        notes.append(f'总分{model_score:.1f}')
    elif model_score >= 72:
        score += 0.5
        notes.append(f'总分{model_score:.1f}')

    if flow_score >= 85:
        score += 1.2
        notes.append(f'流{flow_score:.0f}')
    elif flow_score >= 75:
        score += 0.7
        notes.append(f'流{flow_score:.0f}')

    if stock_score >= 82:
        score += 0.9
        notes.append(f'股{stock_score:.0f}')
    elif stock_score >= 74:
        score += 0.5
        notes.append(f'股{stock_score:.0f}')

    if sector_score >= 80:
        score += 0.6
        notes.append(f'板{sector_score:.0f}')
    elif sector_score >= 72:
        score += 0.3
        notes.append(f'板{sector_score:.0f}')

    if market_score >= 64:
        score += 0.4
        notes.append(f'市{market_score:.0f}')
    elif market_score >= 58:
        score += 0.2
        notes.append(f'市{market_score:.0f}')

    if mode == 'vol_breakout' and flow_score >= 85:
        score += 0.5
        notes.append('放量突破')
    if mode in {'near_kill+weekly+MA20', 'trend_only'} and stock_score >= 74 and flow_score >= 75:
        score += 0.4
        notes.append('趋势种子共振')

    bonus = _fnum(recent_adjustment.get('bonus', 0.0), 0.0)
    penalty = _fnum(recent_adjustment.get('penalty', 0.0), 0.0)
    if penalty > bonus:
        drag = min(1.0, (penalty - bonus) * 0.25 + 0.25)
        score -= drag
        notes.append('近期交易惩罚')
    elif bonus > penalty and bonus >= 0.5:
        lift = min(0.6, (bonus - penalty) * 0.2 + 0.15)
        score += lift
        notes.append('近期交易加分')

    score = round(max(score, 0.0), 2)
    if score >= BIG_MEAT_BUY_SEED_STRONG_THRESHOLD:
        return {
            'effective_tier': 2,
            'seed_score': score,
            'ranking_bonus': 2.2,
            'target_amount_ratio': BIG_MEAT_BUY_T2_STRONG_TARGET_RATIO,
            'initial_amount_ratio': BIG_MEAT_BUY_T2_STRONG_INITIAL_RATIO,
            'pool_note': BIG_MEAT_BUY_POOL_CANDIDATE_NOTE,
            'pool_label': 'T2候选核心',
            'reason': '; '.join(notes[:6]) or '大肉候选高分',
            'priority_rank': 2,
        }
    if score >= BIG_MEAT_BUY_SEED_T2_THRESHOLD:
        return {
            'effective_tier': 2,
            'seed_score': score,
            'ranking_bonus': 1.1,
            'target_amount_ratio': BIG_MEAT_BUY_T2_TARGET_RATIO,
            'initial_amount_ratio': BIG_MEAT_BUY_T2_INITIAL_RATIO,
            'pool_note': BIG_MEAT_BUY_POOL_CANDIDATE_NOTE,
            'pool_label': 'T2候选',
            'reason': '; '.join(notes[:6]) or '大肉候选',
            'priority_rank': 2,
        }
    return {
        'effective_tier': 3,
        'seed_score': score,
        'ranking_bonus': -0.4,
        'target_amount_ratio': BIG_MEAT_BUY_T3_TARGET_RATIO,
        'initial_amount_ratio': BIG_MEAT_BUY_T3_INITIAL_RATIO,
        'pool_note': BIG_MEAT_BUY_POOL_OBSERVE_NOTE,
        'pool_label': 'T3观察',
        'reason': '; '.join(notes[:6]) or '观察试错',
        'priority_rank': 1,
    }


def _build_capital_allocation_feedback(records, *, trade_date=None, decision_reference=None):
    trade_date = _date_key(trade_date or datetime.now().strftime('%Y-%m-%d'))
    reference = decision_reference if isinstance(decision_reference, dict) else _build_selected_decision_reference(
        _read_jsonl(MODEL_DECISIONS_FILE, limit=5000)
    )
    closed_native = [
        _normalize_record(r)
        for r in (records or [])
        if str((r or {}).get('status', '')).strip() == 'closed' and _is_native_strategy_record(r)
    ]
    enriched = []
    for record in closed_native:
        decision_row = _resolve_record_decision_row(record, reference)
        enriched.append({
            **record,
            'market_regime': _normalize_market_regime(decision_row.get('market_regime', '')),
            'capital_biased': _has_capital_bias_note(record),
        })
    biased_closed = [row for row in enriched if row.get('capital_biased')]
    unbiased_closed = [row for row in enriched if not row.get('capital_biased')]
    today_biased_closed = [row for row in biased_closed if _date_key(row.get('sell_date')) == trade_date]
    today_unbiased_closed = [row for row in unbiased_closed if _date_key(row.get('sell_date')) == trade_date]
    regime_mode_groups = {}
    for row in biased_closed:
        key = (_normalize_market_regime(row.get('market_regime', '')), str(row.get('mode', '')).strip())
        regime_mode_groups.setdefault(key, []).append(row)
    regime_mode_summary = []
    for (market_regime, mode), items in regime_mode_groups.items():
        stats = _summarize_return_stats(items)
        regime_mode_summary.append({
            'market_regime': market_regime,
            'mode': mode,
            'closed_count': stats['count'],
            'win_rate_pct': stats['win_rate_pct'],
            'avg_return_pct': stats['avg_return_pct'],
        })
    regime_mode_summary.sort(key=lambda item: (item['avg_return_pct'], item['closed_count']), reverse=True)
    biased_stats = _summarize_return_stats(biased_closed)
    unbiased_stats = _summarize_return_stats(unbiased_closed)
    if biased_stats['count'] <= 0:
        verdict = 'insufficient_samples'
    elif unbiased_stats['count'] <= 0:
        verdict = 'observe_only'
    elif biased_stats['avg_return_pct'] >= unbiased_stats['avg_return_pct'] + 0.5:
        verdict = 'positive'
    elif biased_stats['avg_return_pct'] + 0.5 < unbiased_stats['avg_return_pct']:
        verdict = 'underperforming'
    else:
        verdict = 'mixed'
    return {
        'trade_date': trade_date,
        'verdict': verdict,
        'historical_biased_closed': biased_stats,
        'historical_unbiased_closed': unbiased_stats,
        'today_biased_closed': {
            **_summarize_return_stats(today_biased_closed),
            'codes': [str(row.get('code', '')).zfill(6) for row in today_biased_closed[:10]],
        },
        'today_unbiased_closed': {
            **_summarize_return_stats(today_unbiased_closed),
            'codes': [str(row.get('code', '')).zfill(6) for row in today_unbiased_closed[:10]],
        },
        'regime_mode_leaders': regime_mode_summary[:10],
    }


def _write_csv_row(path, fieldnames, row):
    csv_path = Path(path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = csv_path.exists()
    with csv_path.open('a', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def write_account_artifacts(tag='snapshot', *, balance=None, positions=None, records=None, execution_result=None, pending_items=None):
    balance = balance if balance is not None else get_balance()
    positions = positions if positions is not None else get_positions()
    records = records if records is not None else load_track_record()
    pending_summary = summarize_pending_orders(
        pending_items if pending_items is not None else refresh_pending_orders(positions=positions)
    )
    stats = compute_track_stats(records)
    evolving_model = refresh_model_state(records)
    now = datetime.now()
    previous_summary = _read_json(SUMMARY_FILE) if os.path.exists(SUMMARY_FILE) else {}
    fallback_account = previous_summary.get('account', {}) if isinstance(previous_summary, dict) else {}
    account_live = bool(balance) or bool(positions)
    # #region debug-point E:summary-fallback
    _mx_api_flap_debug_emit(
        'E',
        '[DEBUG] write_account_artifacts pre-summary',
        {
            'tag': tag,
            'account_live': account_live,
            'has_balance': bool(balance),
            'positions_count': len(positions or []),
            'fallback_position_count': _inum(fallback_account.get('position_count', 0), 0),
            'pending_active_buy_count': len((pending_summary or {}).get('active_buy_codes', []) or []),
            'pending_active_sell_count': len((pending_summary or {}).get('active_sell_codes', []) or []),
        },
        location='v10_moni_trader.py:write_account_artifacts',
    )
    # #endregion
    if not account_live and fallback_account:
        balance = {
            'total_assets': _fnum(fallback_account.get('total_assets', 0.0), 0.0),
            'avail_balance': _fnum(fallback_account.get('avail_balance', 0.0), 0.0),
            'total_pos_value': _fnum(fallback_account.get('total_pos_value', 0.0), 0.0),
        }
    floating_pnl = round(
        sum(_fnum(pos.get('profit', 0.0), 0.0) for pos in positions)
        if positions else _fnum(fallback_account.get('floating_pnl', 0.0), 0.0),
        2,
    )
    nav_row = {
        'date': now.strftime('%Y-%m-%d'),
        'time': now.strftime('%H:%M:%S'),
        'tag': tag,
        'total_assets': round(_fnum((balance or {}).get('total_assets', 0.0), 0.0), 2),
        'avail_balance': round(_fnum((balance or {}).get('avail_balance', 0.0), 0.0), 2),
        'total_pos_value': round(_fnum((balance or {}).get('total_pos_value', 0.0), 0.0), 2),
        'position_count': len(positions),
        'holding_records': stats['holding_count'],
        'closed_records': stats['closed_count'],
        'realized_pnl': stats['realized_pnl'],
        'floating_pnl': floating_pnl,
        'win_rate_pct': stats['win_rate_pct'],
        'avg_return_pct': stats['avg_return_pct'],
    }
    if account_live:
        _write_csv_row(NAV_FILE, list(nav_row.keys()), nav_row)

    backtest_targets = load_backtest_targets()
    scan_status = get_scan_status()
    learning_notes = []
    tier_summary = []
    for tier in [1, 2, 3]:
        trades = stats['tier_stats'].get(tier, [])
        wins = [r for r in trades if _fnum(r.get('pnl', 0.0), 0.0) > 0]
        actual_wr = len(wins) / len(trades) * 100 if trades else 0.0
        actual_avg = sum(_fnum(r.get('pnl_pct', 0.0), 0.0) for r in trades) / len(trades) if trades else 0.0
        target = backtest_targets.get(tier, {})
        tier_summary.append({
            'tier': tier,
            'closed_trades': len(trades),
            'actual_win_rate_pct': round(actual_wr, 2),
            'actual_avg_return_pct': round(actual_avg, 4),
            'target_win_rate_pct': round(_fnum(target.get('wr', 0.0), 0.0), 2),
            'target_avg_return_pct': round(_fnum(target.get('avg_ret', 0.0), 0.0), 4),
        })
        if trades and target:
            if actual_wr + 5 < _fnum(target.get('wr', 0.0), 0.0):
                learning_notes.append(f"T{tier} 胜率低于回测基线，需收紧或复盘模式过滤。")
            if actual_avg + 1 < _fnum(target.get('avg_ret', 0.0), 0.0):
                learning_notes.append(f"T{tier} 平均收益低于回测基线，需检查买点或卖点执行。")

    mode_summary = []
    for mode, trades in sorted(stats['mode_stats'].items(), key=lambda item: len(item[1]), reverse=True):
        if not mode:
            continue
        wins = [r for r in trades if _fnum(r.get('pnl', 0.0), 0.0) > 0]
        mode_summary.append({
            'mode': mode,
            'closed_trades': len(trades),
            'win_rate_pct': round(len(wins) / len(trades) * 100 if trades else 0.0, 2),
            'avg_return_pct': round(
                sum(_fnum(r.get('pnl_pct', 0.0), 0.0) for r in trades) / len(trades) if trades else 0.0,
                4,
            ),
        })

    summary = {
        'generated_at': _now_str(),
        'tag': tag,
        'data_dir': str(DATA_DIR),
        'account': {
            'total_assets': nav_row['total_assets'],
            'avail_balance': nav_row['avail_balance'],
            'total_pos_value': nav_row['total_pos_value'],
            'floating_pnl': floating_pnl,
            'position_count': len(positions),
        },
        'performance': {
            'holding_count': stats['holding_count'],
            'closed_count': stats['closed_count'],
            'win_rate_pct': stats['win_rate_pct'],
            'avg_return_pct': stats['avg_return_pct'],
            'realized_pnl': stats['realized_pnl'],
            'all_holding_count': stats['all_holding_count'],
            'all_closed_count': stats['all_closed_count'],
        },
        'tier_summary': tier_summary,
        'mode_summary_top10': mode_summary[:10],
        'learning_notes': learning_notes or ["样本不足，继续积累成交并观察模式表现。"],
        'scan_status': scan_status,
        'account_status': {
            'live': account_live,
            'message': 'live' if account_live else 'account_api_unavailable_using_last_snapshot',
        },
        'pending_orders': pending_summary,
        'sample_filter': {
            'native_record_count': stats['native_record_count'],
            'all_record_count': stats['all_record_count'],
            'excluded_non_native_count': stats['non_native_count'],
            'mode': 'native_only_primary_stats',
        },
        'evolving_model': evolving_model,
        'nav_file': NAV_FILE,
        'position_state_file': POSITION_STATE_FILE,
        'track_file_deprecated': TRACK_FILE,
    }
    if execution_result:
        summary['latest_execution_result'] = execution_result
    _write_json_atomic(SUMMARY_FILE, summary)
    return summary


def build_midday_review(*, balance=None, positions=None, orders=None, records=None):
    balance = balance if balance is not None else get_balance()
    positions = positions if positions is not None else get_positions()
    orders = orders if orders is not None else get_orders()
    records = [_normalize_record(r) for r in (records if records is not None else load_track_record())]
    pending_items = refresh_pending_orders(orders=orders, positions=positions)
    pending_summary = summarize_pending_orders(pending_items)
    active_pos_map = _active_position_map(positions)
    today = _market_today()
    review_items = []
    market_temperature = 'neutral'
    total_profit_pct = 0.0
    counted = 0

    for record in records:
        if record.get('status') != 'holding':
            continue
        code = str(record.get('code', '')).zfill(6)
        pos = active_pos_map.get(code)
        risk_score = 0
        reasons = []
        qty_mismatch = False
        pnl_pct = 0.0
        if pos:
            pnl_pct = _fnum(pos.get('profit_pct', 0.0), 0.0)
            counted += 1
            total_profit_pct += pnl_pct
            if pnl_pct >= HIGH_PROFIT_TAKE_PROFIT_PCT:
                risk_score += 3
                reasons.append(f'高盈利{pnl_pct:+.1f}%')
            elif pnl_pct >= MEDIUM_PROFIT_TAKE_PROFIT_PCT:
                risk_score += 2
                reasons.append(f'中高盈利{pnl_pct:+.1f}%')
            elif pnl_pct <= -5.0:
                risk_score += 2
                reasons.append(f'亏损扩大{pnl_pct:+.1f}%')
            qty_mismatch = _is_legacy_holding_record(record) and _track_qty_mismatch(record, pos)
            if qty_mismatch:
                risk_score += 4
                reasons.append('账本与实仓数量不一致')
        else:
            risk_score += 5
            reasons.append('账本holding但真实无仓')

        if code in pending_summary.get('active_sell_codes', []):
            risk_score += 3
            reasons.append('存在未完成卖单')
        if code in pending_summary.get('active_buy_codes', []):
            risk_score += 1
            reasons.append('存在未完成买单')

        try:
            hold_days = (datetime.now() - datetime.strptime(record.get('date', today), '%Y-%m-%d')).days
        except ValueError:
            hold_days = 0
        if hold_days >= 5:
            risk_score += 2
            reasons.append(f'T+5持仓{hold_days}天')

        if risk_score >= 6:
            afternoon_action = 'high_priority_review'
        elif risk_score >= 3:
            afternoon_action = 'watch_close'
        else:
            afternoon_action = 'hold_observe'

        review_items.append({
            'code': code,
            'name': str(record.get('name', '')).strip(),
            'tier': _inum(record.get('tier', 0), 0),
            'mode': str(record.get('mode', '')).strip(),
            'hold_days': hold_days,
            'profit_pct': round(pnl_pct, 2),
            'risk_score': risk_score,
            'afternoon_action': afternoon_action,
            'reasons': reasons or ['状态正常'],
        })

    avg_profit_pct = round(total_profit_pct / counted, 2) if counted else 0.0
    if pending_summary['counts']['stale'] > 0 or avg_profit_pct < -1.0:
        market_temperature = 'risk_off'
    elif avg_profit_pct > 2.0 and pending_summary['counts']['submitted'] == 0:
        market_temperature = 'risk_on'

    previous_summary = _read_json(SUMMARY_FILE) if os.path.exists(SUMMARY_FILE) else {}
    aggressive_add_summary = _extract_aggressive_add_summary(previous_summary)
    opening_liquidity = _build_opening_liquidity_snapshot()
    external_market = _build_external_market_context()
    scan_status = get_scan_status()
    # #region debug-point A:midday-review-sources
    _midday_review_debug_emit(
        'A',
        'midday review source snapshot',
        positions_count=len(positions or []),
        position_codes=[str((item or {}).get('code', '')).zfill(6) for item in (positions or [])[:10]],
        active_pos_map_count=len(active_pos_map),
        record_holding_count=len([r for r in records if r.get('status') == 'holding']),
        record_holding_codes=[str(r.get('code', '')).zfill(6) for r in records if r.get('status') == 'holding'][:10],
        opening_trade_date=str(opening_liquidity.get('trade_date', '')).strip(),
        opening_generated_at=str(opening_liquidity.get('generated_at', '')).strip(),
        external_trade_date=str(external_market.get('trade_date', '')).strip(),
        external_generated_at=str(external_market.get('generated_at', '')).strip(),
    )
    # #endregion
    review_items.sort(key=lambda item: (item['risk_score'], abs(item['profit_pct'])), reverse=True)
    payload = {
        'generated_at': _now_str(),
        'date': today,
        'tag': 'midday_review',
        'account': {
            'total_assets': round(_fnum((balance or {}).get('total_assets', 0.0), 0.0), 2),
            'avail_balance': round(_fnum((balance or {}).get('avail_balance', 0.0), 0.0), 2),
            'total_pos_value': round(_fnum((balance or {}).get('total_pos_value', 0.0), 0.0), 2),
            'position_count': len(positions),
        },
        'market_temperature': market_temperature,
        'avg_profit_pct': avg_profit_pct,
        'opening_liquidity': opening_liquidity,
        'external_market': external_market,
        'scan_status': scan_status,
        'pending_orders': pending_summary,
        'aggressive_add_review': aggressive_add_summary,
        'focus_sell_watch': [item for item in review_items if item['afternoon_action'] != 'hold_observe'][:10],
        'holdings_review_top15': review_items[:15],
        'afternoon_watchlist': {
            'high_priority_review': [item['code'] for item in review_items if item['afternoon_action'] == 'high_priority_review'][:10],
            'watch_close': [item['code'] for item in review_items if item['afternoon_action'] == 'watch_close'][:10],
        },
        'summary': {
            'holding_records': len([r for r in records if r.get('status') == 'holding']),
            'real_positions': len(active_pos_map),
            'notes': [
                '午间复盘用于中场校准，不直接下单。',
                '优先检查高盈利转弱、未完成卖单和账本实仓不一致的仓位。',
                '若上午命中过大肉激进加仓，应同步核对命中原因与实际执行结果是否一致。',
                '午盘判断默认同时吸收 09:31 开盘流动性门控和隔夜/开盘外部资讯板块冲击结论。',
                '若扫描结果仍新鲜，午盘判断会同时参考候选强度，避免只继承早盘负面先验。',
            ],
        },
    }
    return payload


def _build_account_snapshot(balance=None, positions=None):
    previous_summary = _read_json(SUMMARY_FILE) if os.path.exists(SUMMARY_FILE) else {}
    fallback_account = previous_summary.get('account', {}) if isinstance(previous_summary, dict) else {}
    account_live = bool(balance) or bool(positions)
    snapshot = {
        'total_assets': round(_fnum((balance or {}).get('total_assets', 0.0), 0.0), 2),
        'avail_balance': round(_fnum((balance or {}).get('avail_balance', 0.0), 0.0), 2),
        'total_pos_value': round(_fnum((balance or {}).get('total_pos_value', 0.0), 0.0), 2),
        'position_count': len(positions or []),
    }
    if not account_live and fallback_account:
        snapshot = {
            'total_assets': round(_fnum(fallback_account.get('total_assets', 0.0), 0.0), 2),
            'avail_balance': round(_fnum(fallback_account.get('avail_balance', 0.0), 0.0), 2),
            'total_pos_value': round(_fnum(fallback_account.get('total_pos_value', 0.0), 0.0), 2),
            'position_count': len(positions or []),
        }
    snapshot['live'] = account_live
    snapshot['source'] = 'live' if account_live else ('fallback_summary' if fallback_account else 'unavailable')
    return snapshot


def _extract_aggressive_add_summary(summary):
    execution_result = summary.get('latest_execution_result', {}) if isinstance(summary, dict) else {}
    if str(execution_result.get('action', '')).strip() != 'add_position':
        return {
            'available': False,
            'count': 0,
            'codes': [],
            'items': [],
            'notes': ['最近一次执行结果不是 add-position，暂无大肉激进加仓摘要。'],
        }
    items = execution_result.get('aggressive_add_items', [])
    items = [item for item in items if isinstance(item, dict)]
    codes = [str(item.get('code', '')).zfill(6) for item in items if str(item.get('code', '')).strip()]
    count = _inum(execution_result.get('aggressive_add_count', len(items)), len(items))
    return {
        'available': bool(items),
        'count': count,
        'codes': codes,
        'items': items,
        'notes': (
            [f'最近一次 add-position 命中 {count} 只大肉激进加仓。']
            if items else
            ['最近一次 add-position 未命中大肉激进加仓。']
        ),
    }


def _review_issue(category, severity, code, summary, *, action='', blocks_buy=False, blocks_all=False):
    return {
        'category': category,
        'severity': severity,
        'code': code,
        'summary': summary,
        'action': action,
        'blocks_buy': bool(blocks_buy),
        'blocks_all': bool(blocks_all),
    }


def _collect_reconcile_context():
    # #region debug-point C:close-node-context-start
    _main_strategy_chain_emit(
        'C',
        'v10_moni_trader.py:_collect_reconcile_context',
        '[DEBUG] collect reconcile context start',
        {},
    )
    # #endregion
    balance = get_balance()
    positions = get_positions()
    records = load_track_record()
    orders = get_orders()
    pending_items = refresh_pending_orders(orders=orders, positions=positions)
    records, changed = sync_track_record(records, positions=positions, orders=orders, pending_items=pending_items)
    records, full_changed, reconcile_summary = full_reconcile_positions(records, positions=positions, orders=orders, pending_items=pending_items)
    changed = changed or full_changed
    if changed:
        save_track_record(records)
    pending_summary = reconcile_summary.get('pending') or summarize_pending_orders(pending_items)
    # #region debug-point C:close-node-context-done
    _main_strategy_chain_emit(
        'C',
        'v10_moni_trader.py:_collect_reconcile_context',
        '[DEBUG] collect reconcile context done',
        {
            'balance_keys': sorted(list((balance or {}).keys()))[:12],
            'positions_count': len(positions or []),
            'records_count': len(records or []),
            'orders_count': len(orders or []),
            'changed': bool(changed),
            'pending_active_buy_count': len((pending_summary or {}).get('active_buy_codes', []) or []),
            'pending_active_sell_count': len((pending_summary or {}).get('active_sell_codes', []) or []),
        },
    )
    # #endregion
    # #region debug-point B:midday-review-context
    _midday_review_debug_emit(
        'B',
        'reconcile context collected',
        balance_position_count=_inum((balance or {}).get('position_count', 0), 0),
        positions_count=len(positions or []),
        position_codes=[str((item or {}).get('code', '')).zfill(6) for item in (positions or [])[:10]],
        records_count=len(records or []),
        holding_record_count=len([r for r in (records or []) if str((r or {}).get('status', '')).strip() == 'holding']),
        holding_record_codes=[
            str((r or {}).get('code', '')).zfill(6)
            for r in (records or [])
            if str((r or {}).get('status', '')).strip() == 'holding'
        ][:10],
        pending_active_buy=list((pending_summary or {}).get('active_buy_codes', []))[:10],
        pending_active_sell=list((pending_summary or {}).get('active_sell_codes', []))[:10],
    )
    # #endregion
    return {
        'balance': balance,
        'positions': positions,
        'orders': orders,
        'records': records,
        'changed': changed,
        'reconcile_summary': reconcile_summary,
        'pending_summary': pending_summary,
        'account_snapshot': _build_account_snapshot(balance=balance, positions=positions),
    }


def _derive_midday_gate(issues):
    if any(issue.get('blocks_all') for issue in issues):
        return 'block_all'
    if any(issue.get('blocks_buy') for issue in issues):
        return 'block_buy'
    if issues:
        return 'pass_with_limit'
    return 'pass'


def _derive_review_status(issues):
    if any(issue.get('blocks_all') for issue in issues):
        return 'BLOCK'
    if any(str(issue.get('severity', '')).strip() == 'repair_required' for issue in issues):
        return 'REPAIR_REQUIRED'
    if issues:
        return 'WARN'
    return 'PASS'


def _safe_ratio(numerator, denominator):
    denominator = _fnum(denominator, 0.0)
    if denominator <= 0:
        return 0.0
    return round(_fnum(numerator, 0.0) / denominator, 4)


def _dedupe_codes(values):
    seen = set()
    result = []
    for value in values or []:
        code = str(value or '').zfill(6)
        if not code or code in seen:
            continue
        seen.add(code)
        result.append(code)
    return result


def _build_intraday_judgment(*, context, review_payload, review_status, pm_gate_status):
    balance = context.get('balance') or {}
    pending_summary = context.get('pending_summary') or {}
    review_payload = review_payload if isinstance(review_payload, dict) else {}
    account_total_assets = _fnum(balance.get('total_assets', 0.0), 0.0)
    avg_profit_pct = _fnum(review_payload.get('avg_profit_pct', 0.0), 0.0)
    market_temperature = str(review_payload.get('market_temperature', 'neutral')).strip() or 'neutral'
    opening_liquidity = review_payload.get('opening_liquidity', {}) if isinstance(review_payload.get('opening_liquidity', {}), dict) else {}
    external_market = review_payload.get('external_market', {}) if isinstance(review_payload.get('external_market', {}), dict) else {}
    scan_status = review_payload.get('scan_status', {}) if isinstance(review_payload.get('scan_status', {}), dict) else {}
    signals_by_tier = scan_status.get('signals_by_tier', {}) if isinstance(scan_status.get('signals_by_tier', {}), dict) else {}
    scan_is_fresh = bool(scan_status.get('is_fresh'))
    stocks_with_signal = _inum(scan_status.get('stocks_with_signal', 0), 0)
    tier1_signal_count = _inum(signals_by_tier.get('T1', signals_by_tier.get('1', 0)), 0)
    tier2_signal_count = _inum(signals_by_tier.get('T2', signals_by_tier.get('2', 0)), 0)
    tier3_signal_count = _inum(signals_by_tier.get('T3', signals_by_tier.get('3', 0)), 0)
    active_sell_codes = _dedupe_codes(pending_summary.get('active_sell_codes', []))
    high_priority_review = _dedupe_codes(((review_payload.get('afternoon_watchlist') or {}).get('high_priority_review') or []))
    watch_close = _dedupe_codes(((review_payload.get('afternoon_watchlist') or {}).get('watch_close') or []))
    review_items = review_payload.get('holdings_review_top15', []) or []
    strong_hold_codes = _dedupe_codes(
        item.get('code')
        for item in review_items
        if str(item.get('afternoon_action', '')).strip() == 'hold_observe'
        and _fnum(item.get('profit_pct', 0.0), 0.0) >= 0.0
    )
    reduce_watch_codes = _dedupe_codes(active_sell_codes + high_priority_review + watch_close)
    defensive_pressure = 0
    if market_temperature == 'risk_off':
        defensive_pressure += 2
    elif market_temperature == 'neutral':
        defensive_pressure += 1
    if pm_gate_status in {'block_all', 'block_buy', 'pass_with_limit'}:
        defensive_pressure += 1
    if active_sell_codes:
        defensive_pressure += 1
    if len(high_priority_review) >= 2:
        defensive_pressure += 1
    if avg_profit_pct < 0:
        defensive_pressure += 1
    opening_liquidity_verdict = str(opening_liquidity.get('verdict', '')).strip()
    if opening_liquidity.get('available'):
        if not bool(opening_liquidity.get('in_0931_window')):
            defensive_pressure += 1
        if opening_liquidity_verdict == 'fragile':
            defensive_pressure += 2
        elif opening_liquidity_verdict == 'mixed':
            defensive_pressure += 1
    external_risk_level = str(external_market.get('risk_level', '')).strip().lower()
    external_bias = str(external_market.get('a_share_bias', '')).strip().lower()
    external_negative_sectors = _normalize_sector_names(external_market.get('negative_sectors', []), limit=4)
    external_neutral_sectors = _normalize_sector_names(external_market.get('neutral_sectors', []), limit=4)
    external_positive_sectors = _normalize_sector_names(external_market.get('positive_sectors', []), limit=4)
    external_actions = external_market.get('recommended_actions', {}) if isinstance(external_market.get('recommended_actions', {}), dict) else {}
    external_opening_gate_bias = str(external_actions.get('opening_gate_bias', '')).strip().lower()
    external_horizon = external_market.get('horizon_assessment', {}) if isinstance(external_market.get('horizon_assessment', {}), dict) else {}
    short_flow_monitor = external_market.get('short_flow_monitor', {}) if isinstance(external_market.get('short_flow_monitor', {}), dict) else {}
    short_flow_level = str(short_flow_monitor.get('pressure_level', '')).strip().lower()
    short_flow_sectors = _normalize_sector_names(short_flow_monitor.get('targeted_sectors', []), limit=4)
    opening_anchor_monitor = external_market.get('opening_anchor_break_monitor', {}) if isinstance(external_market.get('opening_anchor_break_monitor', {}), dict) else {}
    opening_anchor_level = str(opening_anchor_monitor.get('pressure_level', '')).strip().lower()
    broken_anchor_names = [str(item).strip() for item in opening_anchor_monitor.get('broken_anchor_names', []) if str(item).strip()][:6]
    weekend_digest_monitor = external_market.get('weekend_digest_monitor', {}) if isinstance(external_market.get('weekend_digest_monitor', {}), dict) else {}
    weekend_digest_bias = str(weekend_digest_monitor.get('bias', '')).strip().lower()
    weekend_negative_sectors = _normalize_sector_names(weekend_digest_monitor.get('negative_sectors', []), limit=4)
    weekend_positive_sectors = _normalize_sector_names(weekend_digest_monitor.get('positive_sectors', []), limit=4)
    short_term_view = external_horizon.get('short_term', {}) if isinstance(external_horizon.get('short_term', {}), dict) else {}
    short_term_bias = str(short_term_view.get('bias', '')).strip().lower()
    mid_term_view = external_horizon.get('mid_term', {}) if isinstance(external_horizon.get('mid_term', {}), dict) else {}
    long_term_view = external_horizon.get('long_term', {}) if isinstance(external_horizon.get('long_term', {}), dict) else {}
    midday_release_soft_ready = (
        scan_is_fresh
        and stocks_with_signal >= MIDDAY_RELEASE_SIGNAL_FLOOR
        and tier2_signal_count >= MIDDAY_RELEASE_T2_FLOOR
    )
    midday_release_ready = (
        midday_release_soft_ready
        and tier1_signal_count >= MIDDAY_RELEASE_T1_FLOOR
    )
    midday_release_context_ready = (
        market_temperature == 'risk_on'
        and pm_gate_status == 'pass'
    )
    if external_market.get('available'):
        if external_risk_level in {'high', 'severe'}:
            defensive_pressure += 2
        elif external_risk_level in {'medium', 'elevated'}:
            defensive_pressure += 1
        if external_bias in {'risk_off', 'defensive', 'cautious'}:
            defensive_pressure += 1
        elif external_bias == 'selective_supportive':
            defensive_pressure -= 1
        elif external_bias == 'broad_supportive':
            defensive_pressure -= 2
        if short_term_bias == 'negative':
            defensive_pressure += 1
        elif short_term_bias == 'selective_positive':
            defensive_pressure -= 1
        elif short_term_bias == 'broad_positive':
            defensive_pressure -= 2
            if external_opening_gate_bias == 'supportive' and short_flow_level != 'high' and opening_anchor_level != 'high':
                defensive_pressure -= 1
        if short_flow_level == 'high':
            defensive_pressure += 2
        elif short_flow_level == 'medium':
            defensive_pressure += 1
        if opening_anchor_level == 'high':
            defensive_pressure += 2
        elif opening_anchor_level == 'medium':
            defensive_pressure += 1
        if weekend_digest_bias == 'negative':
            defensive_pressure += 1
        elif weekend_digest_bias == 'positive':
            defensive_pressure -= 1
    if midday_release_context_ready and midday_release_soft_ready:
        defensive_pressure -= 1
    if midday_release_context_ready and midday_release_ready:
        defensive_pressure -= 1
    if midday_release_context_ready and short_flow_level != 'high':
        defensive_pressure -= 1
    if midday_release_context_ready and opening_anchor_level != 'high':
        defensive_pressure -= 1
    if midday_release_context_ready and opening_liquidity_verdict in {'healthy', 'mixed'}:
        defensive_pressure -= 1
    defensive_pressure = max(0, defensive_pressure)

    if defensive_pressure >= 4:
        risk_bias = 'defensive'
        rebound_bias = 'avoid_broad_rebound'
    elif defensive_pressure >= 2:
        risk_bias = 'balanced'
        rebound_bias = 'selective_only'
    else:
        if external_bias in {'broad_supportive', 'selective_supportive'} and short_term_bias == 'broad_positive' and external_opening_gate_bias == 'supportive':
            risk_bias = 'offensive'
            rebound_bias = 'can_expand'
        else:
            risk_bias = 'balanced'
            rebound_bias = 'selective_only'
    midday_release_override = (
        midday_release_context_ready
        and midday_release_ready
        and short_flow_level != 'high'
        and opening_anchor_level != 'high'
    )
    if midday_release_override and risk_bias == 'defensive':
        risk_bias = 'balanced'
        rebound_bias = 'selective_only'

    confidence = 0.45
    if market_temperature != 'neutral':
        confidence += 0.15
    if review_status == 'PASS':
        confidence += 0.1
    elif review_status == 'WARN':
        confidence += 0.05
    if high_priority_review or active_sell_codes:
        confidence += 0.1
    if midday_release_ready:
        confidence += 0.05
    confidence = round(max(0.25, min(0.9, confidence)), 2)

    notes = []
    if market_temperature == 'risk_off':
        notes.append('午盘识别为风险偏好收缩，下午优先防守而非抢普反。')
    elif market_temperature == 'risk_on':
        notes.append('午盘识别为偏暖环境，下午可保留强票利润奔跑。')
    else:
        notes.append('午盘识别为中性偏分化环境，下午只做结构化处理。')
    if scan_is_fresh:
        notes.append(
            f'午盘扫描仍新鲜，候选强度 signals={stocks_with_signal} '
            f'(T1={tier1_signal_count}/T2={tier2_signal_count}/T3={tier3_signal_count})。'
        )
        if midday_release_context_ready and midday_release_ready:
            notes.append('午盘门控已放行且强候选达标，下午不再机械延续早盘防守偏置。')
        elif midday_release_context_ready and midday_release_soft_ready:
            notes.append('午盘门控已放行且候选密度充足，下午至少按结构性扩张处理。')
    elif stocks_with_signal > 0:
        notes.append(f'午盘扫描候选总量 {stocks_with_signal}，但样本已过时，释放权重自动下调。')
    if external_market.get('available'):
        window_tag = str(external_market.get('window_tag', '')).strip()
        if external_risk_level in {'high', 'severe', 'medium', 'elevated'}:
            notes.append(
                f'外部资讯在 {window_tag or "预开盘"} 窗口提示风险等级 {external_risk_level}，'
                f'先按板块冲击做防守映射。'
            )
        if external_bias == 'neutral':
            notes.append('外部资讯偏中性分化，不支持脑补普反，先做结构性验证。')
        elif external_bias == 'selective_supportive':
            notes.append('外部资讯偏结构性利好，可围绕强分支做选择性应变。')
        elif external_bias == 'broad_supportive':
            notes.append('外部资讯偏全面利好，但仍需尊重 09:31 流动性确认。')
        if external_negative_sectors:
            notes.append(f'隔夜/开盘资讯预警的承压板块: {", ".join(external_negative_sectors)}。')
        if external_neutral_sectors:
            notes.append(f'隔夜/开盘资讯提示应观察而非追价的板块: {", ".join(external_neutral_sectors)}。')
        if external_positive_sectors:
            notes.append(f'隔夜/开盘资讯相对受益板块: {", ".join(external_positive_sectors)}。')
        if short_flow_level in {'high', 'medium', 'low'}:
            notes.append(
                f'做空资金动向压力等级 {short_flow_level}，'
                f'{str(short_flow_monitor.get("summary", "")).strip()}'
            )
        if short_flow_sectors:
            notes.append(f'空头/卖压重点指向板块: {", ".join(short_flow_sectors)}。')
        if opening_anchor_level in {'high', 'medium', 'low'}:
            notes.append(
                f'09:31 核心锚股破位压力等级 {opening_anchor_level}，'
                f'{str(opening_anchor_monitor.get("summary", "")).strip()}'
            )
        if broken_anchor_names:
            notes.append(f'开盘被明显压制的核心锚股: {", ".join(broken_anchor_names)}。')
        if weekend_digest_monitor.get('active'):
            notes.append(
                f'周一周末汇总判断 {weekend_digest_bias or "neutral"}，'
                f'{str(weekend_digest_monitor.get("summary", "")).strip()}'
            )
        if weekend_negative_sectors:
            notes.append(f'周末汇总预警承压板块: {", ".join(weekend_negative_sectors)}。')
        if weekend_positive_sectors:
            notes.append(f'周末汇总关注受益板块: {", ".join(weekend_positive_sectors)}。')
        if str(short_term_view.get('summary', '')).strip():
            notes.append(f'短期判断: {str(short_term_view.get("summary", "")).strip()}')
        if str(mid_term_view.get('summary', '')).strip():
            notes.append(f'中期判断: {str(mid_term_view.get("summary", "")).strip()}')
        if str(long_term_view.get('summary', '')).strip():
            notes.append(f'长期判断: {str(long_term_view.get("summary", "")).strip()}')
    if opening_liquidity.get('available'):
        if opening_liquidity.get('in_0931_window'):
            notes.append('09:31 开盘流动性检查已纳入午盘判断。')
        elif opening_liquidity_verdict:
            notes.append('开盘流动性样本不在 09:31 窗口，门控权重已自动下调。')
    if reduce_watch_codes:
        notes.append(f'下午重点减压/复检名单: {", ".join(reduce_watch_codes[:6])}。')
    if strong_hold_codes:
        notes.append(f'下午允许继续观察的强票: {", ".join(strong_hold_codes[:6])}。')

    return {
        'available': True,
        'trade_date': _market_today(),
        'generated_at': _now_str(),
        'market_temperature': market_temperature,
        'review_status': review_status,
        'pm_gate_status': pm_gate_status,
        'risk_bias': risk_bias,
        'rebound_bias': rebound_bias,
        'confidence': confidence,
        'avg_profit_pct': round(avg_profit_pct, 2),
        'cash_ratio': _safe_ratio(balance.get('avail_balance', 0.0), account_total_assets),
        'position_exposure_ratio': _safe_ratio(balance.get('total_pos_value', 0.0), account_total_assets),
        'strong_hold_codes': strong_hold_codes[:10],
        'reduce_watch_codes': reduce_watch_codes[:10],
        'active_sell_codes': active_sell_codes[:10],
        'scan_status': {
            'is_fresh': scan_is_fresh,
            'stocks_with_signal': stocks_with_signal,
            'signals_by_tier': {
                'T1': tier1_signal_count,
                'T2': tier2_signal_count,
                'T3': tier3_signal_count,
            },
            'midday_release_soft_ready': midday_release_soft_ready,
            'midday_release_ready': midday_release_ready,
            'midday_release_override': midday_release_override,
        },
        'opening_liquidity': {
            'available': bool(opening_liquidity.get('available')),
            'generated_at': opening_liquidity.get('generated_at', ''),
            'verdict': opening_liquidity_verdict,
            'in_0931_window': bool(opening_liquidity.get('in_0931_window')),
            'issue_ratio': _fnum(opening_liquidity.get('issue_ratio', 0.0), 0.0),
            'excluded_today_count': _inum(opening_liquidity.get('excluded_today_count', 0), 0),
            'review_only_count': _inum(opening_liquidity.get('review_only_count', 0), 0),
        },
        'external_market': {
            'available': bool(external_market.get('available')),
            'generated_at': external_market.get('generated_at', ''),
            'window_tag': external_market.get('window_tag', ''),
            'risk_level': external_risk_level or 'unknown',
            'a_share_bias': external_market.get('a_share_bias', ''),
            'negative_sectors': external_negative_sectors,
            'neutral_sectors': external_neutral_sectors,
            'positive_sectors': external_positive_sectors,
            'recommended_actions': external_actions,
            'horizon_assessment': external_horizon,
            'short_flow_monitor': short_flow_monitor,
            'opening_anchor_break_monitor': opening_anchor_monitor,
            'weekend_digest_monitor': weekend_digest_monitor,
            'headline': external_market.get('headline', ''),
        },
        'notes': notes,
    }


def _build_midday_node_payload(*, context, review_payload, stage):
    records = context['records']
    positions = context['positions']
    pending_summary = context['pending_summary']
    reconcile_summary = context['reconcile_summary']
    account_snapshot = context['account_snapshot']
    holding_records = len([r for r in records if r.get('status') == 'holding'])
    real_positions = len(_active_position_map(positions))
    imported_positions = _inum(reconcile_summary.get('imported_positions', 0), 0)
    overlaid_positions = _inum(reconcile_summary.get('overlaid_positions', 0), 0)
    paused_records = _inum(reconcile_summary.get('paused_records', 0), 0)
    stale_count = _inum((pending_summary.get('counts') or {}).get('stale', 0), 0)
    active_buy_codes = list(pending_summary.get('active_buy_codes', []))
    active_sell_codes = list(pending_summary.get('active_sell_codes', []))

    issues = []
    repair_actions = []

    if not account_snapshot.get('live'):
        issues.append(_review_issue(
            'account_snapshot', 'critical', 'account_snapshot_unavailable',
            '账户接口快照不可用，当前仅能依赖旧摘要，下午自动交易应降级。',
            action='block_pm_until_account_live',
            blocks_buy=True,
            blocks_all=True,
        ))
    if imported_positions > 0:
        issues.append(_review_issue(
            'ledger_sync', 'warn', 'auto_imported_positions',
            f'午间对仓自动导入 {imported_positions} 条真实持仓到账本。',
            action='review_auto_imported_positions',
        ))
        repair_actions.append('full_reconcile_import_positions')
    if overlaid_positions > 0:
        issues.append(_review_issue(
            'ledger_sync', 'warn', 'overlay_applied',
            f'午间对仓用真实持仓覆盖更新了 {overlaid_positions} 条账本记录。',
            action='review_overlaid_positions',
        ))
        repair_actions.append('full_reconcile_overlay_positions')
    if paused_records > 0:
        issues.append(_review_issue(
            'ledger_sync', 'repair_required', 'holding_paused_after_reconcile',
            f'午间对仓暂停了 {paused_records} 条缺失真实仓位的 holding 记录。',
            action='review_paused_holdings_before_pm',
            blocks_buy=True,
        ))
        repair_actions.append('pause_missing_holdings')
    if holding_records != real_positions:
        issues.append(_review_issue(
            'base_position', 'repair_required', 'holding_position_count_mismatch',
            f'账本 holding 数 {holding_records} 与真实持仓数 {real_positions} 仍不一致。',
            action='block_buy_until_ledger_matches_positions',
            blocks_buy=True,
        ))
    if stale_count > 0:
        issues.append(_review_issue(
            'pending_orders', 'repair_required', 'stale_pending_orders',
            f'当前仍有 {stale_count} 条 stale pending 订单，下午买单存在冲突风险。',
            action='rebuild_pending_and_review_open_orders',
            blocks_buy=True,
        ))
        repair_actions.append('refresh_pending_orders')
    if active_buy_codes:
        issues.append(_review_issue(
            'pending_orders', 'warn', 'active_buy_orders_present',
            f'当前仍有未完成买单占用：{", ".join(active_buy_codes)}。',
            action='block_codes_from_pm_buy',
            blocks_buy=True,
        ))
    if active_sell_codes:
        issues.append(_review_issue(
            'pending_orders', 'warn', 'active_sell_orders_present',
            f'当前仍有未完成卖单：{", ".join(active_sell_codes)}。',
            action='keep_sell_watch_and_skip_duplicate_sell',
        ))
    if context['changed']:
        repair_actions.append('sync_track_record')
        repair_actions.append('save_track_record')

    seen_actions = []
    for action in repair_actions:
        if action and action not in seen_actions:
            seen_actions.append(action)

    review_status = _derive_review_status(issues)
    pm_gate_status = _derive_midday_gate(issues)
    intraday_judgment = _build_intraday_judgment(
        context=context,
        review_payload=review_payload,
        review_status=review_status,
        pm_gate_status=pm_gate_status,
    )
    payload = {
        'generated_at': _now_str(),
        'date': datetime.now().strftime('%Y-%m-%d'),
        'node': 'midday_node',
        'stage': stage,
        'trigger_slot': MIDDAY_NODE_TRIGGER_SLOT,
        'realtime_correction_window': PM_REALTIME_CORRECTION_WINDOW,
        'hard_deadline': MIDDAY_NODE_HARD_DEADLINE,
        'review_status': review_status,
        'pm_gate_status': pm_gate_status,
        'blocked_buy_codes': active_buy_codes,
        'account': account_snapshot,
        'pending_orders': pending_summary,
        'reconcile': reconcile_summary,
        'issues': issues,
        'repair_actions_executed': seen_actions,
        'summary': {
            'holding_records': holding_records,
            'real_positions': real_positions,
            'active_buy_codes': active_buy_codes,
            'active_sell_codes': active_sell_codes,
            'notes': [
                '午间节点先复核上午已发生事实，再决定下午是否放行。',
                '13:00-13:05 仅保留低风险实时纠偏窗口，不在该窗口内自动做高风险改单动作。',
            ],
        },
        'midday_review': {
            'market_temperature': review_payload.get('market_temperature', 'neutral'),
            'avg_profit_pct': review_payload.get('avg_profit_pct', 0.0),
            'opening_liquidity': review_payload.get('opening_liquidity', {}),
            'external_market': review_payload.get('external_market', {}),
            'focus_sell_watch': review_payload.get('focus_sell_watch', [])[:10],
            'afternoon_watchlist': review_payload.get('afternoon_watchlist', {}),
        },
        'intraday_judgment': intraday_judgment,
    }
    return payload


def _write_pm_gate_payload(payload, *, file_path):
    gate_payload = {
        'generated_at': payload.get('generated_at', _now_str()),
        'date': payload.get('date', datetime.now().strftime('%Y-%m-%d')),
        'node': payload.get('node', 'midday_node'),
        'stage': payload.get('stage', ''),
        'review_status': payload.get('review_status', 'PASS'),
        'pm_gate_status': payload.get('pm_gate_status', 'pass'),
        'blocked_buy_codes': payload.get('blocked_buy_codes', []),
        'hard_deadline': payload.get('hard_deadline', MIDDAY_NODE_HARD_DEADLINE),
        'reason_codes': [item.get('code', '') for item in payload.get('issues', []) if item.get('code')],
    }
    _write_json_atomic(file_path, gate_payload)
    return gate_payload


def _date_key(value):
    text = str(value or '').strip()
    return text[:10] if len(text) >= 10 else text


def _parse_trade_date(value):
    text = _date_key(value)
    if not text:
        return None
    try:
        return datetime.strptime(text, '%Y-%m-%d').date()
    except Exception:
        return None


def _build_recent_trade_memory(records, *, as_of_date=None):
    as_of = _parse_trade_date(as_of_date) or datetime.now().date()
    memory = {}
    for raw in records or []:
        record = _normalize_record(raw)
        if not _is_native_strategy_record(record):
            continue
        code = str(record.get('code', '')).zfill(6)
        if not code:
            continue
        item = memory.setdefault(code, {
            'last_buy_date': None,
            'last_sell_date': None,
            'last_closed_pnl_pct': None,
            'recent_buy_count': 0,
            'last_mode': '',
        })
        buy_date = _parse_trade_date(record.get('date'))
        sell_date = _parse_trade_date(record.get('sell_date'))
        if buy_date and (item['last_buy_date'] is None or buy_date > item['last_buy_date']):
            item['last_buy_date'] = buy_date
            item['last_mode'] = str(record.get('mode', '')).strip()
        if buy_date and (as_of - buy_date).days <= RECENT_REENTRY_REPEAT_LOOKBACK_DAYS:
            item['recent_buy_count'] += 1
        if str(record.get('status', '')).strip() == 'closed' and sell_date:
            if item['last_sell_date'] is None or sell_date > item['last_sell_date']:
                item['last_sell_date'] = sell_date
                item['last_closed_pnl_pct'] = _fnum(record.get('pnl_pct', 0.0), 0.0)
                item['last_mode'] = str(record.get('mode', '')).strip()
    for item in memory.values():
        last_buy_date = item.get('last_buy_date')
        last_sell_date = item.get('last_sell_date')
        item['days_since_buy'] = (as_of - last_buy_date).days if last_buy_date else 999
        item['days_since_sell'] = (as_of - last_sell_date).days if last_sell_date else 999
    return memory


def _resolve_recent_trade_selection_adjustment(code, recent_trade_memory, *, allow_block=True):
    info = (recent_trade_memory or {}).get(str(code).zfill(6), {})
    reasons = []
    penalty = 0.0
    bonus = 0.0
    block_reentry = False
    last_closed_pnl_pct = _fnum(info.get('last_closed_pnl_pct', 0.0), 0.0)
    days_since_sell = _inum(info.get('days_since_sell', 999), 999)
    days_since_buy = _inum(info.get('days_since_buy', 999), 999)
    recent_buy_count = _inum(info.get('recent_buy_count', 0), 0)

    if days_since_sell <= RECENT_REENTRY_SELL_PENALTY_DAYS:
        penalty += RECENT_SELL_PENALTY_SCORE
        reasons.append(f'近{days_since_sell}天刚卖出')
    if days_since_sell <= RECENT_REENTRY_LOSS_BLOCK_DAYS and last_closed_pnl_pct <= RECENT_REENTRY_LOSS_BLOCK_PCT:
        penalty += RECENT_FAILURE_PENALTY_SCORE
        reasons.append(f'近期亏损{last_closed_pnl_pct:+.1f}%')
        if allow_block:
            block_reentry = True
    elif days_since_sell <= RECENT_REENTRY_SEVERE_LOSS_BLOCK_DAYS and last_closed_pnl_pct <= RECENT_REENTRY_SEVERE_LOSS_BLOCK_PCT:
        penalty += RECENT_FAILURE_PENALTY_SCORE + 1.0
        reasons.append(f'重亏后冷却{last_closed_pnl_pct:+.1f}%')
        if allow_block:
            block_reentry = True
    elif days_since_sell <= RECENT_REENTRY_SEVERE_LOSS_BLOCK_DAYS and last_closed_pnl_pct < 0:
        penalty += RECENT_FAILURE_PENALTY_SCORE
        reasons.append(f'近窗负收益{last_closed_pnl_pct:+.1f}%')

    if recent_buy_count >= RECENT_REENTRY_REPEAT_COUNT_THRESHOLD:
        penalty += RECENT_REPEAT_PENALTY_SCORE
        reasons.append(f'{RECENT_REENTRY_REPEAT_LOOKBACK_DAYS}天重复参与{recent_buy_count}次')
    if min(days_since_buy, days_since_sell) >= FRESH_OPPORTUNITY_LOOKBACK_DAYS:
        bonus += FRESH_OPPORTUNITY_BONUS_SCORE
        reasons.append('新鲜机会加分')

    return {
        'bonus': round(bonus, 2),
        'penalty': round(penalty, 2),
        'net_adjustment': round(bonus - penalty, 2),
        'block_reentry': block_reentry,
        'reasons': reasons,
    }


def _build_selected_decision_index(rows):
    index = {}
    for row in rows or []:
        if not bool(row.get('selected')):
            continue
        trade_date = _date_key(row.get('trade_date'))
        code = str(row.get('code', '')).zfill(6)
        if not trade_date or not code:
            continue
        index[(trade_date, code)] = row
    return index


def _build_alpha_loss_events(trade_date):
    return _collect_alpha_loss_events(trade_date=trade_date)


def _collect_alpha_loss_events(*, trade_date=''):
    events = []
    seen = set()
    for row in _read_jsonl(TRADE_API_LOG_FILE, limit=10000):
        logged_date = _date_key(row.get('logged_at'))
        if trade_date and logged_date != trade_date:
            continue
        code = str(row.get('code', '')).zfill(6)
        action = str(row.get('action', '')).strip()
        event_type = str(row.get('event_type', '')).strip()
        retry_attempts = _inum(row.get('retry_attempts', 0), 0)
        result_code = str(row.get('result_code', '')).strip()
        ok = bool(row.get('ok'))
        reason = ''
        if event_type == 'retry_wait':
            reason = 'rate_limit_retry'
        elif event_type == 'trade_result' and not ok:
            reason = f'trade_failed_{result_code or "unknown"}'
        elif event_type == 'trade_result' and retry_attempts > 0:
            reason = 'trade_success_after_retry'
        if not reason or not code:
            continue
        dedup_key = (code, action, reason)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        events.append({
            'code': code,
            'action': action,
            'reason': reason,
            'logged_at': str(row.get('logged_at', '')).strip(),
            'trade_date': logged_date,
            'retry_attempts': retry_attempts,
            'result_code': result_code,
        })
    return events


def _extract_trade_api_order_id(row):
    row = row if isinstance(row, dict) else {}
    order_id = str(row.get('order_id', '')).strip()
    if order_id:
        return order_id
    raw = row.get('raw', {}) if isinstance(row.get('raw', {}), dict) else {}
    data = raw.get('data', {}) if isinstance(raw.get('data', {}), dict) else {}
    result = data.get('result', {}) if isinstance(data.get('result', {}), dict) else {}
    return str(
        data.get('orderID')
        or data.get('orderId')
        or result.get('orderID')
        or result.get('orderId')
        or ''
    ).strip()


def _build_trade_fill_index():
    index = {}
    seen = set()
    for row in _read_jsonl(TRADE_API_LOG_FILE, limit=0):
        event_type = str(row.get('event_type', 'trade_result') or 'trade_result').strip()
        if event_type and event_type != 'trade_result':
            continue
        if not bool(row.get('ok')):
            continue
        action = str(row.get('action', '')).strip()
        if action not in {'buy', 'sell'}:
            continue
        code = str(row.get('code', '')).zfill(6)
        if not code:
            continue
        logged_at = str(row.get('logged_at', '')).strip()
        order_id = _extract_trade_api_order_id(row)
        dedup_key = order_id or (
            action,
            code,
            logged_at,
            _inum(row.get('quantity', 0), 0),
            f"{_fnum(row.get('ref_price', 0.0), 0.0):.4f}",
        )
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        index.setdefault((code, action), []).append({
            'code': code,
            'action': action,
            'logged_at': logged_at,
            'trade_date': _date_key(logged_at),
            'order_id': order_id,
            'quantity': _inum(row.get('quantity', 0), 0),
            'price': round(_fnum(row.get('ref_price', 0.0), 0.0), 4),
            'strategy_action': str(row.get('strategy_action', '')).strip(),
            'retry_attempts': _inum(row.get('retry_attempts', 0), 0),
        })
    for key in index:
        index[key].sort(key=lambda item: str(item.get('logged_at', '')))
    return index


def _match_trade_fill_events(record, *, action='', fill_index=None):
    record = _normalize_record(record)
    fill_index = fill_index if isinstance(fill_index, dict) else {}
    action_text = str(action or '').strip()
    code = str(record.get('code', '')).zfill(6)
    events = list(fill_index.get((code, action_text), []))
    if not events:
        return []
    if action_text == 'buy':
        target_ids = set(_split_order_ids(record.get('buy_order_ids', '')))
        target_date = _date_key(record.get('date'))
    else:
        sell_order_id = str(record.get('sell_order_id', '')).strip()
        target_ids = {sell_order_id} if sell_order_id else set()
        target_date = _date_key(record.get('sell_date'))
    matched = []
    for item in events:
        order_id = str(item.get('order_id', '')).strip()
        if target_ids and order_id and order_id in target_ids:
            matched.append(dict(item))
            continue
        event_date = _date_key(item.get('trade_date') or item.get('logged_at'))
        if target_date and event_date != target_date:
            continue
        matched.append(dict(item))
    if not target_ids:
        return matched
    ordered = []
    seen = set()
    for target_id in target_ids:
        for item in matched:
            order_id = str(item.get('order_id', '')).strip()
            dedup_key = order_id or f"{item.get('logged_at', '')}|{item.get('quantity', 0)}"
            if order_id == target_id and dedup_key not in seen:
                ordered.append(item)
                seen.add(dedup_key)
    for item in matched:
        dedup_key = str(item.get('order_id', '')).strip() or f"{item.get('logged_at', '')}|{item.get('quantity', 0)}"
        if dedup_key in seen:
            continue
        ordered.append(item)
        seen.add(dedup_key)
    return ordered


def _load_smart_sell_trigger_reason_index():
    debug_log_file = DATA_DIR / "smart_sell_debug_latest.ndjson"
    if not debug_log_file.exists():
        return {}
    order_reason_index = {}
    recent_before_trade = {}
    try:
        with debug_log_file.open('r', encoding='utf-8') as f:
            for line in f:
                text = str(line or '').strip()
                if not text:
                    continue
                try:
                    item = json.loads(text)
                except Exception:
                    continue
                stage = str(item.get('stage', '')).strip()
                data = item.get('data', {}) if isinstance(item.get('data', {}), dict) else {}
                code = str(data.get('code', '')).zfill(6)
                ts = _parse_dt(item.get('ts'))
                if not code or not ts:
                    continue
                if stage == 'do_smart_sell.before_trade':
                    reason = str(data.get('reason', '')).strip()
                    if reason:
                        recent_before_trade[code] = {
                            'reason': reason,
                            'ts': ts,
                        }
                    continue
                if stage != 'do_smart_sell.trade_success':
                    continue
                order_id = str(data.get('order_id', '')).strip()
                cached = recent_before_trade.get(code, {})
                cached_reason = str(cached.get('reason', '')).strip()
                cached_ts = cached.get('ts')
                if not order_id or not cached_reason or not cached_ts:
                    continue
                if abs((ts - cached_ts).total_seconds()) > 180:
                    continue
                order_reason_index[order_id] = cached_reason
    except Exception:
        return {}
    return order_reason_index


def _date_in_episode_window(value, start_date, end_date):
    current = _date_key(value)
    start = _date_key(start_date)
    end = _date_key(end_date) or start
    if not current or not start:
        return False
    if end and end < start:
        end = start
    return start <= current <= (end or start)


def _episode_is_big_meat_success(record, *, pnl_pct=0.0):
    record = _normalize_record(record)
    build_note = str(record.get('build_note', '')).strip()
    big_meat_state = str(record.get('big_meat_state', '')).strip()
    realized = _fnum(pnl_pct, 0.0)
    return (
        realized >= LEARNING_BIG_MEAT_SUCCESS_PNL_PCT
        or (big_meat_state == BIG_MEAT_STATE_CONFIRMED and realized >= 5.0)
        or (BIG_MEAT_BUY_POOL_CANDIDATE_NOTE in build_note and realized >= 6.0)
        or (_is_big_meat_identity_record(record) and realized >= 6.0)
    )


def _classify_false_selection(record, *, pnl_pct=0.0, hold_days=0, close_reason='', execution_damaged=False, profit_truncation=False):
    record = _normalize_record(record)
    if execution_damaged or profit_truncation:
        return {'flag': False, 'level': '', 'reason_codes': []}
    realized = _fnum(pnl_pct, 0.0)
    if _episode_is_big_meat_success(record, pnl_pct=realized):
        return {'flag': False, 'level': '', 'reason_codes': []}
    reason_text = str(close_reason or '').strip()
    build_note = str(record.get('build_note', '')).strip()
    is_t3_observe = BIG_MEAT_BUY_POOL_OBSERVE_NOTE in build_note or _inum(record.get('tier', 0), 0) == 3
    reason_codes = []
    level = ''
    if realized <= -4.0 or '趋势终结' in reason_text or '连跌2日' in reason_text:
        level = 'strong'
        reason_codes.append('hard_decay_or_large_loss')
    elif realized <= LEARNING_FALSE_SELECTION_NEG_PNL_PCT:
        level = 'medium'
        reason_codes.append('quick_negative_return')
    elif hold_days <= 1 and realized < 0:
        level = 'medium'
        reason_codes.append('day1_failed')
    elif '信号衰减' in reason_text and hold_days <= 2 and realized <= 1.0:
        level = 'medium'
        reason_codes.append('early_decay_failed')
    elif is_t3_observe and realized <= LEARNING_FALSE_SELECTION_SOFT_NEG_PNL_PCT:
        level = 'soft'
        reason_codes.append('t3_observe_failed')
    elif is_t3_observe and hold_days <= 2 and realized < 0:
        level = 'soft'
        reason_codes.append('t3_weak_followthrough')
    return {
        'flag': bool(level),
        'level': level,
        'reason_codes': reason_codes,
    }


def _build_trade_episode_history(records, *, decision_reference=None):
    decision_reference = decision_reference if isinstance(decision_reference, dict) else _build_selected_decision_reference([])
    fill_index = _build_trade_fill_index()
    smart_sell_reason_index = _load_smart_sell_trigger_reason_index()
    alpha_loss_history = _collect_alpha_loss_events()
    alpha_loss_by_code = {}
    for item in alpha_loss_history:
        code = str(item.get('code', '')).zfill(6)
        alpha_loss_by_code.setdefault(code, []).append(item)
    episodes = []
    closed_native = [
        _normalize_record(r)
        for r in (records or [])
        if str((r or {}).get('status', '')).strip() == 'closed' and _is_native_strategy_record(r)
    ]
    for record in closed_native:
        code = str(record.get('code', '')).zfill(6)
        decision_row = _resolve_record_decision_row(record, decision_reference)
        pnl_pct = round(_fnum(record.get('pnl_pct', 0.0), 0.0), 4)
        hold_days = _inum(record.get('hold_days', 0), 0)
        close_reason = str(record.get('close_reason', '')).strip()
        sell_order_id = str(record.get('sell_order_id', '')).strip()
        if (
            sell_order_id
            and close_reason in {'mx_moni_sell_fill[smart_sell]', 'sync_detected_sell_fill'}
            and smart_sell_reason_index.get(sell_order_id)
        ):
            close_reason = str(smart_sell_reason_index.get(sell_order_id) or '').strip() or close_reason
        alpha_events = [
            item for item in (alpha_loss_by_code.get(code) or [])
            if _date_in_episode_window(item.get('logged_at') or item.get('trade_date'), record.get('date'), record.get('sell_date'))
        ]
        execution_damaged = bool(alpha_events)
        build_note = str(record.get('build_note', '')).strip()
        big_meat_state = str(record.get('big_meat_state', '')).strip()
        opening_shock_profit_expansion_miss = (
            pnl_pct >= LEARNING_BIG_MEAT_SUCCESS_PNL_PCT
            and hold_days <= 3
            and any(token in close_reason for token in ('hard_exit[', '信号衰减['))
            and any(token in close_reason for token in ('冲高回落上影线', '大阴线'))
            and '趋势终结' not in close_reason
            and '连跌2日' not in close_reason
            and '放量滞涨' not in close_reason
        )
        profit_truncation = (
            (
                _is_big_meat_identity_record(record)
                or big_meat_state == BIG_MEAT_STATE_CONFIRMED
                or BIG_MEAT_BUY_POOL_CANDIDATE_NOTE in build_note
            )
            and pnl_pct > 0
            and pnl_pct < LEARNING_BIG_MEAT_SUCCESS_PNL_PCT
            and any(token in close_reason for token in ('信号衰减', 'risk_trim[', 'hard_exit['))
        ) or opening_shock_profit_expansion_miss
        false_selection = _classify_false_selection(
            record,
            pnl_pct=pnl_pct,
            hold_days=hold_days,
            close_reason=close_reason,
            execution_damaged=execution_damaged,
            profit_truncation=profit_truncation,
        )
        big_meat_success = _episode_is_big_meat_success(record, pnl_pct=pnl_pct)
        decision_match = bool(str(record.get('decision_id', '')).strip()) or bool(decision_row)
        blocked_reasons = []
        if not decision_match:
            blocked_reasons.append('missing_decision_match')
        if execution_damaged:
            blocked_reasons.append('execution_damaged')
        if profit_truncation:
            blocked_reasons.append('profit_truncation')
        selection_verdict = 'neutral'
        if execution_damaged:
            selection_verdict = 'execution_damaged'
        elif profit_truncation:
            selection_verdict = 'profit_truncation'
        elif big_meat_success:
            selection_verdict = 'big_meat_success'
        elif false_selection.get('flag'):
            selection_verdict = 'false_selection'
        elif pnl_pct > 0:
            selection_verdict = 'clean_positive'
        elif pnl_pct < 0:
            selection_verdict = 'clean_negative'
        quality_score = 60.0 + pnl_pct * 2.5
        if false_selection.get('level') == 'soft':
            quality_score -= 10.0
        elif false_selection.get('level') == 'medium':
            quality_score -= 18.0
        elif false_selection.get('level') == 'strong':
            quality_score -= 28.0
        if execution_damaged:
            quality_score -= min(25.0, len(alpha_events) * 8.0)
        if profit_truncation:
            quality_score -= 12.0
        buy_events = _match_trade_fill_events(record, action='buy', fill_index=fill_index)
        sell_events = _match_trade_fill_events(record, action='sell', fill_index=fill_index)
        episodes.append({
            'episode_id': str(record.get('decision_id') or f"{record.get('date', '')}|{code}|{record.get('buy_time', '')}").strip(),
            'code': code,
            'name': str(record.get('name', '')).strip(),
            'mode': str(record.get('mode', '')).strip(),
            'tier': _inum(record.get('tier', 0), 0),
            'status': str(record.get('status', '')).strip(),
            'buy_date': _date_key(record.get('date')),
            'buy_time': str(record.get('buy_time', '')).strip(),
            'sell_date': _date_key(record.get('sell_date')),
            'sell_time': str(record.get('sell_time', '')).strip(),
            'hold_days': hold_days,
            'entry_price': round(_fnum(record.get('entry_price', 0.0), 0.0), 4),
            'sell_price': round(_fnum(record.get('sell_price', 0.0), 0.0), 4),
            'quantity': _inum(record.get('quantity', 0), 0),
            'buy_amount': round(_fnum(record.get('buy_amount', 0.0), 0.0), 2),
            'pnl': round(_fnum(record.get('pnl', 0.0), 0.0), 2),
            'pnl_pct': pnl_pct,
            'decision_id': str(record.get('decision_id') or decision_row.get('decision_id', '')).strip(),
            'decision_run_slot': str(record.get('decision_run_slot') or decision_row.get('decision_run_slot', '')).strip(),
            'selected_reason_hash': str(record.get('selected_reason_hash') or decision_row.get('selected_reason_hash', '')).strip(),
            'market_regime': _normalize_market_regime(decision_row.get('market_regime', '')),
            'build_note': build_note,
            'close_reason': close_reason,
            'big_meat_state': big_meat_state,
            'big_meat_confirmed_at': str(record.get('big_meat_confirmed_at', '')).strip(),
            'big_meat_success_flag': bool(big_meat_success),
            'false_selection_flag': bool(false_selection.get('flag')),
            'falsify_level': str(false_selection.get('level', '')).strip(),
            'falsify_reason_codes': list(false_selection.get('reason_codes', [])),
            't3_observe_flag': bool(BIG_MEAT_BUY_POOL_OBSERVE_NOTE in build_note or _inum(record.get('tier', 0), 0) == 3),
            'candidate_pool_flag': bool(BIG_MEAT_BUY_POOL_CANDIDATE_NOTE in build_note),
            'execution_damaged': execution_damaged,
            'execution_damage_score': len(alpha_events),
            'execution_damage_reasons': [str(item.get('reason', '')).strip() for item in alpha_events if str(item.get('reason', '')).strip()][:6],
            'profit_truncation': profit_truncation,
            'decision_match': decision_match,
            'selection_verdict': selection_verdict,
            'sample_quality_score': round(max(0.0, min(100.0, quality_score)), 2),
            'buy_fill_count': len(buy_events),
            'sell_fill_count': len(sell_events),
            'buy_fill_times': [str(item.get('logged_at', '')).strip() for item in buy_events[:8]],
            'sell_fill_times': [str(item.get('logged_at', '')).strip() for item in sell_events[:8]],
            'buy_order_ids': _split_order_ids(record.get('buy_order_ids', '')),
            'sell_order_id': str(record.get('sell_order_id', '')).strip(),
            'blocked_reasons': blocked_reasons,
        })
    episodes.sort(
        key=lambda item: (
            str(item.get('sell_date', '')),
            str(item.get('sell_time', '')),
            str(item.get('buy_date', '')),
            str(item.get('code', '')),
        )
    )
    return episodes


def _summarize_trade_episode_history(episodes):
    episodes = [item for item in (episodes or []) if isinstance(item, dict)]
    count = len(episodes)
    big_meat_success_count = len([item for item in episodes if bool(item.get('big_meat_success_flag'))])
    false_selection_count = len([item for item in episodes if bool(item.get('false_selection_flag'))])
    execution_damaged_count = len([item for item in episodes if bool(item.get('execution_damaged'))])
    profit_truncation_count = len([item for item in episodes if bool(item.get('profit_truncation'))])
    avg_return_pct = (
        sum(_fnum(item.get('pnl_pct', 0.0), 0.0) for item in episodes) / count
        if count else 0.0
    )
    return {
        'episode_count': count,
        'big_meat_success_count': big_meat_success_count,
        'false_selection_count': false_selection_count,
        'execution_damaged_count': execution_damaged_count,
        'profit_truncation_count': profit_truncation_count,
        'big_meat_success_rate_pct': round(big_meat_success_count / count * 100.0, 2) if count else 0.0,
        'false_selection_rate_pct': round(false_selection_count / count * 100.0, 2) if count else 0.0,
        'avg_return_pct': round(avg_return_pct, 4),
    }


def _is_big_meat_identity_record(record):
    mode = str(record.get('mode', '')).strip()
    note = str(record.get('build_note', '')).strip()
    tier = _inum(record.get('tier', 0), 0)
    return (
        mode == 'V9_full'
        or '超级大行情' in note
        or ('满仓首建' in note and tier == 1)
    )


def _build_missed_opportunity_items(records, *, decision_reference=None, trade_date=None, max_history=40):
    trade_date = _date_key(trade_date or datetime.now().strftime('%Y-%m-%d'))
    decision_reference = decision_reference if isinstance(decision_reference, dict) else _build_selected_decision_reference([])
    selected_rows = list((decision_reference.get('by_trade_date_code') or {}).values())
    if not selected_rows:
        return {'matured_items': [], 'pending_items': [], 'summary': {}}

    opened_native_keys = {
        (_date_key(record.get('date')), str(record.get('code', '')).zfill(6))
        for record in (_normalize_record(r) for r in (records or []))
        if _is_native_strategy_record(record) and _date_key(record.get('date')) and str(record.get('code', '')).strip()
    }
    scan_manifest = _build_scan_snapshot_manifest()
    scan_dates = sorted([date for date in scan_manifest.keys() if date])
    next_scan_by_date = {}
    for idx, date in enumerate(scan_dates):
        next_scan_by_date[date] = scan_dates[idx + 1] if idx + 1 < len(scan_dates) else ''

    matured_items = []
    pending_items = []
    selected_rows.sort(key=lambda item: (_date_key(item.get('trade_date')), str(item.get('recorded_at', '')).strip(), str(item.get('code', '')).strip()))
    for row in selected_rows:
        row_trade_date = _date_key(row.get('trade_date'))
        code = str(row.get('code', '')).zfill(6)
        if not row_trade_date or not code or row_trade_date > trade_date:
            continue
        if (row_trade_date, code) in opened_native_keys:
            continue
        source_snapshot = _load_scan_snapshot_rows(trade_date=row_trade_date, scan_manifest=scan_manifest)
        source_row = (source_snapshot.get('by_code') or {}).get(code, {})
        entry_price = _fnum(source_row.get('entry_price', 0.0), _fnum(source_row.get('close', 0.0), 0.0))
        if entry_price <= 0:
            continue
        base_item = {
            'trade_date': row_trade_date,
            'decision_run_slot': str(row.get('run_slot', '')).strip(),
            'code': code,
            'name': str(row.get('name', '')).strip(),
            'tier': _inum(row.get('tier', 0), 0),
            'mode': str(row.get('mode', '')).strip(),
            'market_regime': _normalize_market_regime(row.get('market_regime', '')),
            'score': round(_fnum(row.get('score', 0.0), 0.0), 2),
            'entry_price': round(entry_price, 4),
            'source_scan_csv': str(source_snapshot.get('csv_path', '')).strip(),
        }
        next_trade_date = next_scan_by_date.get(row_trade_date, '')
        if not next_trade_date or next_trade_date > trade_date:
            pending_items.append({
                **base_item,
                'posterior_status': 'pending_next_session_snapshot',
                'posterior_trade_date': next_trade_date,
            })
            continue
        posterior_snapshot = _load_scan_snapshot_rows(trade_date=next_trade_date, scan_manifest=scan_manifest)
        posterior_row = (posterior_snapshot.get('by_code') or {}).get(code, {})
        posterior_price = _fnum(posterior_row.get('entry_price', 0.0), _fnum(posterior_row.get('close', 0.0), 0.0))
        if posterior_price <= 0:
            pending_items.append({
                **base_item,
                'posterior_status': 'missing_next_session_code_snapshot',
                'posterior_trade_date': next_trade_date,
            })
            continue
        posterior_return_pct = round(_safe_ratio(posterior_price - entry_price, entry_price) * 100.0, 4)
        matured_items.append({
            **base_item,
            'posterior_status': 'matured',
            'posterior_trade_date': next_trade_date,
            'posterior_price': round(posterior_price, 4),
            'posterior_return_pct': posterior_return_pct,
            'positive_opportunity': posterior_return_pct > 0,
            'strong_positive_opportunity': posterior_return_pct >= 2.0,
            'posterior_scan_csv': str(posterior_snapshot.get('csv_path', '')).strip(),
        })

    matured_items.sort(key=lambda item: (item.get('trade_date', ''), item.get('posterior_return_pct', 0.0), item.get('code', '')), reverse=True)
    pending_items.sort(key=lambda item: (item.get('trade_date', ''), item.get('score', 0.0), item.get('code', '')), reverse=True)
    matured_items = matured_items[:max_history]
    pending_items = pending_items[:max_history]
    matured_positive = [item for item in matured_items if bool(item.get('positive_opportunity'))]
    matured_strong = [item for item in matured_items if bool(item.get('strong_positive_opportunity'))]
    avg_return_pct = (
        sum(_fnum(item.get('posterior_return_pct', 0.0), 0.0) for item in matured_items) / len(matured_items)
        if matured_items else 0.0
    )
    summary = {
        'matured_count': len(matured_items),
        'positive_count': len(matured_positive),
        'strong_positive_count': len(matured_strong),
        'avg_return_pct': round(avg_return_pct, 4),
        'pending_count': len(pending_items),
        'positive_codes': [item.get('code', '') for item in matured_positive[:10]],
        'strong_positive_codes': [item.get('code', '') for item in matured_strong[:10]],
    }
    return {
        'matured_items': matured_items,
        'pending_items': pending_items,
        'summary': summary,
    }


def _build_daily_evolution_bundle(*, summary, records, trade_date=None):
    trade_date = _date_key(trade_date or datetime.now().strftime('%Y-%m-%d'))
    records = [_normalize_record(r) for r in (records or [])]
    today_closed = [r for r in records if r.get('status') == 'closed' and _date_key(r.get('sell_date')) == trade_date]
    today_opened = [r for r in records if _date_key(r.get('date')) == trade_date]
    today_closed_native = [r for r in today_closed if _is_native_strategy_record(r)]
    today_opened_native = [r for r in today_opened if _is_native_strategy_record(r)]

    decision_reference = _build_selected_decision_reference(_read_jsonl(MODEL_DECISIONS_FILE, limit=5000))
    alpha_loss_events = _build_alpha_loss_events(trade_date)
    trade_episode_history = _build_trade_episode_history(records, decision_reference=decision_reference)
    history_summary = _summarize_trade_episode_history(trade_episode_history)
    today_episodes = [
        dict(item)
        for item in trade_episode_history
        if _date_key(item.get('sell_date')) == trade_date
    ]

    direct_learn_items = []
    observe_only_items = []
    execution_damaged_items = []
    profit_truncation_items = []

    for item in today_episodes:
        if item.get('blocked_reasons'):
            observe_only_items.append(item)
        else:
            direct_learn_items.append(item)
        if bool(item.get('execution_damaged')):
            execution_damaged_items.append(item)
        if bool(item.get('profit_truncation')):
            profit_truncation_items.append(item)

    capital_allocation_feedback = _build_capital_allocation_feedback(
        records,
        trade_date=trade_date,
        decision_reference=decision_reference,
    )
    missed_opportunity_bundle = _build_missed_opportunity_items(
        records,
        trade_date=trade_date,
        decision_reference=decision_reference,
    )
    bundle = {
        'generated_at': _now_str(),
        'trade_date': trade_date,
        'source_stats': {
            'today_closed_count': len(today_closed_native),
            'today_closed_all_count': len(today_closed),
            'today_closed_excluded_count': max(0, len(today_closed) - len(today_closed_native)),
            'today_opened_count': len(today_opened_native),
            'today_opened_all_count': len(today_opened),
            'today_opened_excluded_count': max(0, len(today_opened) - len(today_opened_native)),
            'selected_decision_count': len([
                row for row in (decision_reference.get('by_trade_date_code') or {}).values()
                if _date_key(row.get('trade_date')) == trade_date
            ]),
        },
        'summary': {
            'learnable_sample_count': len(direct_learn_items),
            'observe_only_count': len(observe_only_items),
            'execution_damaged_count': len(execution_damaged_items),
            'profit_truncation_count': len(profit_truncation_items),
            'alpha_loss_event_count': len(alpha_loss_events),
            'missed_opportunity_count': _inum((missed_opportunity_bundle.get('summary') or {}).get('matured_count', 0), 0),
            'missed_opportunity_pending_count': _inum((missed_opportunity_bundle.get('summary') or {}).get('pending_count', 0), 0),
        },
        'history_summary': history_summary,
        'capital_allocation_feedback': capital_allocation_feedback,
        'direct_learn_items': direct_learn_items,
        'observe_only_items': observe_only_items,
        'execution_damaged_items': execution_damaged_items,
        'profit_truncation_items': profit_truncation_items,
        'missed_opportunity_items': missed_opportunity_bundle.get('matured_items', []),
        'missed_opportunity_pending_items': missed_opportunity_bundle.get('pending_items', []),
        'missed_opportunity_summary': missed_opportunity_bundle.get('summary', {}),
        'alpha_loss_events': alpha_loss_events,
        'trade_episode_history': trade_episode_history,
        'notes': [
            '该文件用于收盘后把样本先分级，再决定哪些进入学习层、哪些只进入观察层。',
            'execution_damaged 与 profit_truncation 样本默认不直接进入参数学习，但必须进入进化复盘。',
            '若存在午盘判断样本，收盘阶段会继续做尾盘验证，并将判断质量沉淀进学习闭环。',
            '已选未买样本会补充 D1 后验收益回填，用于校准午盘放仓与尾盘执行是否错失机会。',
        ],
    }
    return bundle


def _load_latest_midday_payload():
    for file_path in [MIDDAY_GATE_FILE, MIDDAY_NODE_FILE]:
        if not os.path.exists(file_path):
            continue
        payload = _read_json(file_path)
        if isinstance(payload, dict) and payload.get('intraday_judgment'):
            return payload
    return {}


def _build_pm_buy_guardrails():
    today = _market_today()
    payload = _load_latest_midday_payload()
    if not payload and os.path.exists(PM_GATE_FILE):
        payload = _read_json(PM_GATE_FILE)
    payload = payload if isinstance(payload, dict) else {}
    judgment = payload.get('intraday_judgment', {}) if isinstance(payload.get('intraday_judgment', {}), dict) else {}
    payload_date = _date_key(judgment.get('trade_date') or payload.get('date') or '')
    is_today = payload_date == today
    available = bool(judgment) and is_today
    pm_gate_status = str(judgment.get('pm_gate_status') or payload.get('pm_gate_status') or 'pass').strip() or 'pass'
    risk_bias = str(judgment.get('risk_bias', '')).strip()
    rebound_bias = str(judgment.get('rebound_bias', '')).strip()
    market_temperature = str(judgment.get('market_temperature', '')).strip()
    confidence = _fnum(judgment.get('confidence', 0.0), 0.0)
    judgment_scan_status = judgment.get('scan_status', {}) if isinstance(judgment.get('scan_status', {}), dict) else {}
    midday_release_ready = bool(judgment_scan_status.get('midday_release_ready'))
    midday_release_override = bool(judgment_scan_status.get('midday_release_override'))
    learning_actions = _load_learning_actions()
    learning_summary = (
        learning_actions.get('summary', {})
        if isinstance(learning_actions, dict) and isinstance(learning_actions.get('summary', {}), dict)
        else {}
    )
    missed_positive_opportunity_count = _inum(learning_summary.get('missed_opportunity_positive_count', 0), 0)
    missed_opportunity_avg_return_pct = _fnum(learning_summary.get('missed_opportunity_avg_return_pct', 0.0), 0.0)
    blocked_modes = set()
    limited_modes = set()
    allow_buy = pm_gate_status not in {'block_all', 'block_buy'}
    allow_full_v9_build = False
    global_amount_ratio = 1.0
    mode_amount_ratio = 1.0
    max_new_positions = 0
    notes = []
    reason = 'midday_judgment_missing'

    if not available:
        blocked_modes.add('V9_full')
        limited_modes.update(PM_BUY_RESTRICTED_MODES - blocked_modes)
        mode_amount_ratio = 0.75
        max_new_positions = PM_BUY_MAX_NEW_POSITIONS_LIMITED
        notes.append('缺少当日有效午盘判断样本，尾盘不放行激进模式。')
    elif not allow_buy:
        reason = f'pm_gate_{pm_gate_status}'
        notes.append(f'午盘门控状态={pm_gate_status}，尾盘新开仓直接阻断。')
    else:
        strong_confirm = (
            pm_gate_status == 'pass'
            and market_temperature == 'risk_on'
            and risk_bias == 'offensive'
            and rebound_bias == 'can_expand'
            and confidence >= PM_STRONG_CONFIRM_MIN_CONFIDENCE
        )
        release_opportunity_confirm = (
            pm_gate_status == 'pass'
            and market_temperature == 'risk_on'
            and (midday_release_ready or midday_release_override)
            and missed_positive_opportunity_count > 0
            and missed_opportunity_avg_return_pct > 0
        )
        if strong_confirm:
            allow_full_v9_build = True
            reason = 'strong_confirm'
            notes.append('午盘判断给出强确认，允许保留强势模式的正常尾盘首建。')
        elif release_opportunity_confirm:
            limited_modes.update(PM_BUY_RESTRICTED_MODES - {'V9_full'})
            mode_amount_ratio = max(mode_amount_ratio, PM_BUY_LIMITED_MODE_RATIO)
            max_new_positions = max(PM_BUY_MAX_NEW_POSITIONS_LIMITED, 2)
            reason = 'release_opportunity_confirm'
            notes.append('近期已验证存在正向错失机会，午盘放行优先围绕盈利机会做选择性扩张。')
        else:
            blocked_modes.add('V9_full')
            if pm_gate_status == 'pass_with_limit' or risk_bias == 'defensive':
                blocked_modes.update(PM_BUY_RESTRICTED_MODES)
                global_amount_ratio = PM_BUY_DEFENSIVE_GLOBAL_RATIO
                max_new_positions = (
                    PM_BUY_MAX_NEW_POSITIONS_DEFENSIVE
                    if risk_bias == 'defensive' else
                    PM_BUY_MAX_NEW_POSITIONS_LIMITED
                )
                reason = 'defensive_limit'
                notes.append('午盘偏防守或仅限放行，尾盘只允许极少量试错且整体缩仓。')
            else:
                limited_modes.update(PM_BUY_RESTRICTED_MODES - blocked_modes)
                mode_amount_ratio = PM_BUY_LIMITED_MODE_RATIO
                if rebound_bias != 'can_expand' or market_temperature != 'risk_on':
                    max_new_positions = PM_BUY_MAX_NEW_POSITIONS_LIMITED
                reason = 'selective_limit'
                notes.append('午盘未形成强确认，尾盘仅保留选择性试错并收紧高波动模式。')

    limited_modes -= blocked_modes
    if PARTIAL_ROLLBACK_DISABLE_FULL_V9_BUILD:
        allow_full_v9_build = False
        notes.append('部分回退生效: T1 V9_full 恢复为普通首建，不再允许满仓首建。')
    return {
        'available': available,
        'pm_gate_status': pm_gate_status,
        'risk_bias': risk_bias,
        'rebound_bias': rebound_bias,
        'market_temperature': market_temperature,
        'confidence': round(confidence, 2),
        'allow_buy': allow_buy,
        'allow_full_v9_build': allow_full_v9_build,
        'blocked_modes': sorted(blocked_modes),
        'limited_modes': sorted(limited_modes),
        'global_amount_ratio': round(global_amount_ratio, 2),
        'mode_amount_ratio': round(mode_amount_ratio, 2),
        'max_new_positions': _inum(max_new_positions, 0),
        'reason': reason,
        'notes': notes,
    }


def _build_intraday_judgment_review(*, bundle, summary, records=None, positions=None):
    midday_payload = _load_latest_midday_payload()
    judgment = midday_payload.get('intraday_judgment', {}) if isinstance(midday_payload, dict) else {}
    trade_date = _date_key((bundle or {}).get('trade_date') or datetime.now().strftime('%Y-%m-%d'))
    if not judgment:
        return {
            'available': False,
            'trade_date': trade_date,
            'verdict': 'missing_midday_judgment',
            'score': 0,
            'notes': ['当日未发现可用的午盘判断样本，尾盘无法做判断校准。'],
        }
    if _date_key(judgment.get('trade_date') or midday_payload.get('date')) != trade_date:
        return {
            'available': False,
            'trade_date': trade_date,
            'verdict': 'stale_midday_judgment',
            'score': 0,
            'notes': ['午盘判断样本日期与收盘节点不一致，跳过当日校准。'],
        }

    records = [_normalize_record(r) for r in (records or [])]
    positions = positions or []
    bundle = bundle if isinstance(bundle, dict) else {}
    close_account = summary.get('account', {}) if isinstance(summary, dict) else {}
    close_total_assets = _fnum(close_account.get('total_assets', 0.0), 0.0)
    midday_cash_ratio = _fnum(judgment.get('cash_ratio', 0.0), 0.0)
    midday_exposure_ratio = _fnum(judgment.get('position_exposure_ratio', 0.0), 0.0)
    close_cash_ratio = _safe_ratio(close_account.get('avail_balance', 0.0), close_total_assets)
    close_exposure_ratio = _safe_ratio(close_account.get('total_pos_value', 0.0), close_total_assets)
    cash_ratio_change = round(close_cash_ratio - midday_cash_ratio, 4)
    exposure_ratio_change = round(close_exposure_ratio - midday_exposure_ratio, 4)
    today_closed_codes = _dedupe_codes(
        [item.get('code') for item in (bundle.get('direct_learn_items') or [])]
        + [item.get('code') for item in (bundle.get('observe_only_items') or [])]
    )
    holding_codes = _dedupe_codes(
        [row.get('code') for row in records if str(row.get('status', '')).strip() == 'holding']
        + [row.get('code') for row in (positions or [])]
    )
    reduce_watch_codes = _dedupe_codes(judgment.get('reduce_watch_codes', []))
    strong_hold_codes = _dedupe_codes(judgment.get('strong_hold_codes', []))
    reduced_focus_codes = [code for code in reduce_watch_codes if code in today_closed_codes]
    retained_strong_codes = [code for code in strong_hold_codes if code in holding_codes]
    opening_liquidity = judgment.get('opening_liquidity', {}) if isinstance(judgment.get('opening_liquidity', {}), dict) else {}
    external_market = judgment.get('external_market', {}) if isinstance(judgment.get('external_market', {}), dict) else {}
    risk_bias = str(judgment.get('risk_bias', '')).strip()
    market_temperature = str(judgment.get('market_temperature', '')).strip()
    pm_gate_status = str(judgment.get('pm_gate_status') or midday_payload.get('pm_gate_status') or 'pass').strip() or 'pass'
    source_stats = bundle.get('source_stats', {}) if isinstance(bundle.get('source_stats', {}), dict) else {}
    scan_status = summary.get('scan_status', {}) if isinstance(summary, dict) and isinstance(summary.get('scan_status', {}), dict) else {}
    stocks_with_signal = _inum(scan_status.get('stocks_with_signal', 0), 0)
    today_opened_count = _inum(source_stats.get('today_opened_count', 0), 0)
    evidence = 0
    notes = []

    if risk_bias == 'defensive':
        if cash_ratio_change >= 0.05 or close_cash_ratio >= 0.65:
            evidence += 2
            notes.append('尾盘现金占比较午盘明显提升，防守判断得到兑现。')
        elif cash_ratio_change >= 0.02:
            evidence += 1
        if exposure_ratio_change <= -0.05 or close_exposure_ratio <= 0.35:
            evidence += 2
            notes.append('尾盘仓位暴露显著下降，说明下午执行以收缩风险为主。')
        elif exposure_ratio_change <= -0.02:
            evidence += 1
        if reduced_focus_codes:
            evidence += 1
            notes.append(f'午盘减压名单在尾盘得到处理: {", ".join(reduced_focus_codes[:6])}。')
        if retained_strong_codes:
            evidence += 1
            notes.append(f'同时保留了相对强势仓位: {", ".join(retained_strong_codes[:6])}。')
        if str(external_market.get('risk_level', '')).strip().lower() in {'high', 'severe', 'medium', 'elevated'}:
            evidence += 1
            notes.append('隔夜/开盘外部风险信号与尾盘防守收缩动作基本一致。')
    elif risk_bias == 'offensive':
        if exposure_ratio_change >= -0.02:
            evidence += 2
        if cash_ratio_change <= 0.02:
            evidence += 1
        if retained_strong_codes:
            evidence += 1
            notes.append(f'午盘看多保留的强票尾盘仍在: {", ".join(retained_strong_codes[:6])}。')
    else:
        if reduced_focus_codes:
            evidence += 1
            notes.append(f'午盘复检名单尾盘已有兑现: {", ".join(reduced_focus_codes[:6])}。')
        if retained_strong_codes:
            evidence += 1
            notes.append(f'午盘允许观察的强票尾盘继续保留: {", ".join(retained_strong_codes[:6])}。')
        if cash_ratio_change >= 0.02 or exposure_ratio_change <= -0.02:
            evidence += 1
    if opening_liquidity.get('available') and bool(opening_liquidity.get('in_0931_window')):
        notes.append('本次复检参考了 09:31 开盘流动性样本。')
    elif opening_liquidity.get('available'):
        notes.append('开盘流动性样本不在 09:31 窗口，本次只作辅助证据。')
    if external_market.get('available'):
        negative_sectors = _normalize_sector_names(external_market.get('negative_sectors', []), limit=4)
        neutral_sectors = _normalize_sector_names(external_market.get('neutral_sectors', []), limit=4)
        positive_sectors = _normalize_sector_names(external_market.get('positive_sectors', []), limit=4)
        short_flow_monitor = external_market.get('short_flow_monitor', {}) if isinstance(external_market.get('short_flow_monitor', {}), dict) else {}
        short_flow_level = str(short_flow_monitor.get('pressure_level', '')).strip().lower()
        opening_anchor_monitor = external_market.get('opening_anchor_break_monitor', {}) if isinstance(external_market.get('opening_anchor_break_monitor', {}), dict) else {}
        opening_anchor_level = str(opening_anchor_monitor.get('pressure_level', '')).strip().lower()
        weekend_digest_monitor = external_market.get('weekend_digest_monitor', {}) if isinstance(external_market.get('weekend_digest_monitor', {}), dict) else {}
        weekend_digest_bias = str(weekend_digest_monitor.get('bias', '')).strip().lower()
        short_term = (external_market.get('horizon_assessment', {}) or {}).get('short_term', {})
        if negative_sectors:
            notes.append(f'外部资讯预警承压板块: {", ".join(negative_sectors)}。')
        if neutral_sectors:
            notes.append(f'外部资讯提示先观察的中性板块: {", ".join(neutral_sectors)}。')
        if positive_sectors:
            notes.append(f'外部资讯相对受益板块: {", ".join(positive_sectors)}。')
        if short_flow_level in {'high', 'medium', 'low'}:
            notes.append(f'做空资金压力等级 {short_flow_level}: {str(short_flow_monitor.get("summary", "")).strip()}')
        if opening_anchor_level in {'high', 'medium', 'low'}:
            notes.append(f'核心锚股开盘破位等级 {opening_anchor_level}: {str(opening_anchor_monitor.get("summary", "")).strip()}')
        if weekend_digest_monitor.get('active'):
            notes.append(f'周一周末汇总判断 {weekend_digest_bias or "neutral"}: {str(weekend_digest_monitor.get("summary", "")).strip()}')
        if isinstance(short_term, dict) and str(short_term.get('summary', '')).strip():
            notes.append(f'短期资讯判断: {str(short_term.get("summary", "")).strip()}')

    if evidence >= 4:
        verdict = 'validated'
    elif evidence >= 2:
        verdict = 'mixed'
    else:
        verdict = 'invalidated'
    score = min(100, max(20, 20 + evidence * 20))
    missed_risk_on_deployment = (
        market_temperature == 'risk_on'
        and pm_gate_status == 'pass'
        and stocks_with_signal >= REGIME_EXECUTION_RISK_ON_SIGNAL_FLOOR
        and today_opened_count <= 0
        and close_exposure_ratio <= 0.05
    )
    if missed_risk_on_deployment:
        notes.append(
            f'午盘已转 risk_on 且门控放行，但 signals={stocks_with_signal} 仍零开仓并维持低仓，'
            '本次更适合作为午盘释放校准样本，不应直接视为完全验证。'
        )
        if verdict == 'validated':
            verdict = 'mixed'
            score = min(score, 70)
    if not notes:
        notes.append('尾盘验证信号有限，午盘判断需要继续累计样本观察。')

    return {
        'available': True,
        'trade_date': trade_date,
        'midday_generated_at': judgment.get('generated_at', midday_payload.get('generated_at', '')),
        'midday_stage': midday_payload.get('stage', ''),
        'risk_bias': risk_bias,
        'market_temperature': market_temperature,
        'pm_gate_status': pm_gate_status,
        'rebound_bias': str(judgment.get('rebound_bias', '')).strip(),
        'confidence': _fnum(judgment.get('confidence', 0.0), 0.0),
        'verdict': verdict,
        'score': score,
        'stocks_with_signal': stocks_with_signal,
        'today_opened_count': today_opened_count,
        'missed_risk_on_deployment': bool(missed_risk_on_deployment),
        'midday_cash_ratio': round(midday_cash_ratio, 4),
        'close_cash_ratio': round(close_cash_ratio, 4),
        'cash_ratio_change': cash_ratio_change,
        'midday_exposure_ratio': round(midday_exposure_ratio, 4),
        'close_exposure_ratio': round(close_exposure_ratio, 4),
        'exposure_ratio_change': exposure_ratio_change,
        'reduce_watch_codes': reduce_watch_codes[:10],
        'reduced_focus_codes': reduced_focus_codes[:10],
        'strong_hold_codes': strong_hold_codes[:10],
        'retained_strong_codes': retained_strong_codes[:10],
        'opening_liquidity': {
            'available': bool(opening_liquidity.get('available')),
            'generated_at': opening_liquidity.get('generated_at', ''),
            'verdict': str(opening_liquidity.get('verdict', '')).strip(),
            'in_0931_window': bool(opening_liquidity.get('in_0931_window')),
            'issue_ratio': _fnum(opening_liquidity.get('issue_ratio', 0.0), 0.0),
        },
        'external_market': {
            'available': bool(external_market.get('available')),
            'generated_at': external_market.get('generated_at', ''),
            'window_tag': str(external_market.get('window_tag', '')).strip(),
            'risk_level': str(external_market.get('risk_level', '')).strip().lower(),
            'a_share_bias': str(external_market.get('a_share_bias', '')).strip(),
            'negative_sectors': _normalize_sector_names(external_market.get('negative_sectors', [])),
            'neutral_sectors': _normalize_sector_names(external_market.get('neutral_sectors', [])),
            'positive_sectors': _normalize_sector_names(external_market.get('positive_sectors', [])),
            'recommended_actions': external_market.get('recommended_actions', {}) if isinstance(external_market.get('recommended_actions', {}), dict) else {},
            'horizon_assessment': external_market.get('horizon_assessment', {}) if isinstance(external_market.get('horizon_assessment', {}), dict) else {},
            'short_flow_monitor': external_market.get('short_flow_monitor', {}) if isinstance(external_market.get('short_flow_monitor', {}), dict) else {},
            'opening_anchor_break_monitor': external_market.get('opening_anchor_break_monitor', {}) if isinstance(external_market.get('opening_anchor_break_monitor', {}), dict) else {},
            'weekend_digest_monitor': external_market.get('weekend_digest_monitor', {}) if isinstance(external_market.get('weekend_digest_monitor', {}), dict) else {},
            'headline': str(external_market.get('headline', '')).strip(),
        },
        'notes': notes,
    }


def _build_regime_execution_review(*, bundle, summary):
    bundle = bundle if isinstance(bundle, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    trade_date = _date_key(bundle.get('trade_date') or datetime.now().strftime('%Y-%m-%d'))
    account = summary.get('account', {}) if isinstance(summary.get('account', {}), dict) else {}
    scan_status = summary.get('scan_status', {}) if isinstance(summary.get('scan_status', {}), dict) else {}
    pending_orders = summary.get('pending_orders', {}) if isinstance(summary.get('pending_orders', {}), dict) else {}
    intraday_review = bundle.get('intraday_judgment_review', {}) if isinstance(bundle.get('intraday_judgment_review', {}), dict) else {}
    source_stats = bundle.get('source_stats', {}) if isinstance(bundle.get('source_stats', {}), dict) else {}

    history_rows = []
    for row in _read_jsonl(EXTERNAL_MARKET_REVIEW_HISTORY_FILE, limit=200):
        if not isinstance(row, dict):
            continue
        row_trade_date = _date_key(row.get('trade_date') or '')
        if not row_trade_date or row_trade_date > trade_date:
            continue
        history_rows.append(row)
    history_rows.sort(key=lambda item: (
        _date_key(item.get('trade_date') or ''),
        _parse_dt(str(item.get('generated_at', '')).strip()) or datetime.min,
    ))
    latest_external_by_date = {}
    for row in history_rows:
        row_trade_date = _date_key(row.get('trade_date') or '')
        if row_trade_date:
            latest_external_by_date[row_trade_date] = row
    recent_external = [
        latest_external_by_date[key]
        for key in sorted(latest_external_by_date.keys())[-3:]
    ]
    recent_external_dates = [_date_key(item.get('trade_date') or '') for item in recent_external]
    pressure_days = len([
        item for item in recent_external
        if (
            str(item.get('risk_level', '')).strip().lower() in {'medium', 'high', 'severe', 'elevated'}
            or str(item.get('a_share_bias', '')).strip().lower() in {'risk_off', 'neutral'}
            or len([sector for sector in (item.get('negative_sectors') or []) if str(sector or '').strip()]) >= 2
        )
    ])
    risk_off_streak_days = 0
    for item in reversed(recent_external):
        if str(item.get('a_share_bias', '')).strip().lower() == 'risk_off':
            risk_off_streak_days += 1
        else:
            break
    high_risk_days = len([
        item for item in recent_external
        if str(item.get('risk_level', '')).strip().lower() in {'high', 'severe', 'elevated'}
    ])
    recent_negative_sectors = _normalize_sector_names([
        sector
        for item in recent_external
        for sector in (item.get('negative_sectors') or [])
    ], limit=8)

    nav_rows = []
    trade_dt = _parse_dt(f'{trade_date} 00:00:00') or datetime.now()
    target_trade_dates = [
        (trade_dt - timedelta(days=offset)).strftime('%Y-%m-%d')
        for offset in reversed(range(3))
    ]
    target_trade_date_set = set(target_trade_dates)
    for row in _read_csv_rows(NAV_FILE):
        row_date = _date_key(row.get('date') or '')
        tag = str(row.get('tag', '')).strip().lower()
        if (
            not row_date
            or row_date not in target_trade_date_set
            or tag not in {'report', 'status', 'close_node_refresh', 'smart_sell', 'buy', 'add_position'}
        ):
            continue
        row_dt = _parse_dt(f"{row_date} {str(row.get('time', '')).strip() or '00:00:00'}")
        nav_rows.append({
            **row,
            '_date': row_date,
            '_dt': row_dt or datetime.min,
            '_assets': _fnum(row.get('total_assets', 0.0), 0.0),
            '_pos_value': _fnum(row.get('total_pos_value', 0.0), 0.0),
        })
    latest_nav_by_date = {}
    for row in sorted(nav_rows, key=lambda item: (item.get('_date', ''), item.get('_dt', datetime.min))):
        if row.get('_assets', 0.0) <= 0:
            continue
        latest_nav_by_date[row['_date']] = row
    recent_nav = [latest_nav_by_date[key] for key in target_trade_dates if key in latest_nav_by_date]
    nav_dates = [row.get('_date', '') for row in recent_nav]
    start_assets = _fnum((recent_nav[0] if recent_nav else {}).get('_assets', account.get('total_assets', 0.0)), 0.0)
    end_assets = _fnum((recent_nav[-1] if recent_nav else {}).get('_assets', account.get('total_assets', 0.0)), 0.0)
    start_exposure_ratio = _safe_ratio(
        (recent_nav[0] if recent_nav else {}).get('_pos_value', 0.0),
        start_assets,
    )
    close_exposure_ratio = _safe_ratio(account.get('total_pos_value', 0.0), account.get('total_assets', 0.0))
    three_day_return_pct = round((_safe_ratio(end_assets - start_assets, start_assets) * 100.0), 4) if start_assets > 0 else 0.0
    max_daily_drawdown_pct = 0.0
    if len(recent_nav) >= 2:
        day_returns = []
        for prev, curr in zip(recent_nav, recent_nav[1:]):
            prev_assets = _fnum(prev.get('_assets', 0.0), 0.0)
            curr_assets = _fnum(curr.get('_assets', 0.0), 0.0)
            if prev_assets <= 0:
                continue
            day_returns.append(_safe_ratio(curr_assets - prev_assets, prev_assets) * 100.0)
        if day_returns:
            max_daily_drawdown_pct = round(min(day_returns), 4)
    exposure_reduction_pct = round((close_exposure_ratio - start_exposure_ratio) * 100.0, 4)

    stocks_with_signal = _inum(scan_status.get('stocks_with_signal', 0), 0)
    candidate_pool_exists = stocks_with_signal > 0
    no_new_openings_today = _inum(source_stats.get('today_opened_count', 0), 0) <= 0
    close_clean = (
        not list((pending_orders.get('active_buy_codes') or []))
        and not list((pending_orders.get('active_sell_codes') or []))
        and bool((summary.get('account_status') or {}).get('live'))
    )
    defensive_intraday = (
        str(intraday_review.get('risk_bias', '')).strip().lower() == 'defensive'
        and str(intraday_review.get('verdict', '')).strip().lower() in {'validated', 'mixed'}
    )
    risk_on_release_missed = (
        str(intraday_review.get('market_temperature', '')).strip().lower() == 'risk_on'
        and str(intraday_review.get('pm_gate_status', '')).strip().lower() == 'pass'
        and stocks_with_signal >= REGIME_EXECUTION_RISK_ON_SIGNAL_FLOOR
        and no_new_openings_today
    )
    current_external = recent_external[-1] if recent_external else {}
    current_risk_level = str(current_external.get('risk_level', '')).strip().lower()
    current_bias = str(current_external.get('a_share_bias', '')).strip().lower()

    evidence = 0
    notes = []
    if len(recent_external) >= 3 and pressure_days >= 2:
        evidence += 2
        notes.append(f'最近三日中有 {pressure_days} 日出现明显压力/震荡信号。')
    if len(recent_nav) >= 3 and exposure_reduction_pct <= -15.0:
        evidence += 2
        notes.append('近三日仓位暴露持续大幅收缩，说明主策略主动降风险。')
    if high_risk_days >= 1 or current_risk_level in {'high', 'severe', 'elevated'}:
        evidence += 1
        notes.append('最近窗口至少出现 1 个 high/elevated 风险日。')
    if candidate_pool_exists and no_new_openings_today:
        evidence += 2
        notes.append(f'当日候选池存在（signals={stocks_with_signal}），但未新增开仓。')
    if close_exposure_ratio <= 0.05:
        evidence += 2
        notes.append('收盘仓位暴露接近空仓，风险收缩执行到位。')
    elif exposure_reduction_pct <= -10.0:
        evidence += 1
        notes.append('相对窗口起点显著降仓。')
    if three_day_return_pct >= -0.8 and max_daily_drawdown_pct >= -0.8:
        evidence += 2
        notes.append('近三日账户回撤控制优于防守基线。')
    elif three_day_return_pct >= -1.2:
        evidence += 1
        notes.append('近三日账户总回撤仍处于可接受防守区间。')
    if close_clean:
        evidence += 1
        notes.append('收盘账户、挂单与账本状态干净，可排除大部分执行残留噪音。')
    if defensive_intraday:
        evidence += 1
        notes.append('午盘判断与尾盘结果一致，验证了防守型仓位执行。')
    if risk_on_release_missed:
        notes.append(
            f'午盘门控已 pass 且市场转为 risk_on，但 signals={stocks_with_signal} 时仍零开仓；'
            '该样本应进入释放校准，不应计入 defensive 正样本。'
        )
    if current_bias == 'risk_off' and recent_negative_sectors:
        notes.append(f'最近承压方向集中在: {", ".join(recent_negative_sectors[:6])}。')

    positive_sample = (
        (len(recent_external) >= 3 and pressure_days >= 2)
        and candidate_pool_exists
        and no_new_openings_today
        and close_exposure_ratio <= 0.05
        and close_clean
        and defensive_intraday
        and not risk_on_release_missed
        and evidence >= 8
    )
    if positive_sample and evidence >= 7:
        verdict = 'positive'
        label = 'defensive-low-exposure-good-execution'
    elif risk_on_release_missed and evidence >= 4:
        verdict = 'mixed'
        label = 'defensive-low-exposure-needs-calibration'
    elif evidence >= 4:
        verdict = 'mixed'
        label = 'defensive-low-exposure-watch'
    else:
        verdict = 'insufficient_evidence'
        label = ''
    score = min(100, max(20, 20 + evidence * 10))
    if not notes:
        notes.append('连续防守样本证据不足，继续观察更多风险窗口。')
    return {
        'available': bool(len(recent_external) >= 2 or len(recent_nav) >= 2),
        'trade_date': trade_date,
        'window_trade_dates': recent_external_dates or nav_dates,
        'verdict': verdict,
        'score': score,
        'positive_sample': bool(positive_sample and verdict == 'positive'),
        'label': label,
        'pressure_days': pressure_days,
        'risk_off_streak_days': risk_off_streak_days,
        'high_risk_days': high_risk_days,
        'current_risk_level': current_risk_level,
        'current_bias': current_bias,
        'candidate_pool_exists': candidate_pool_exists,
        'stocks_with_signal': stocks_with_signal,
        'today_opened_count': _inum(source_stats.get('today_opened_count', 0), 0),
        'today_closed_count': _inum(source_stats.get('today_closed_count', 0), 0),
        'risk_on_release_missed': bool(risk_on_release_missed),
        'three_day_return_pct': three_day_return_pct,
        'max_daily_drawdown_pct': max_daily_drawdown_pct,
        'baseline_return_floor_pct': -0.8,
        'baseline_close_exposure_ratio': 0.05,
        'start_exposure_ratio': round(start_exposure_ratio, 4),
        'close_exposure_ratio': round(close_exposure_ratio, 4),
        'exposure_reduction_pct': exposure_reduction_pct,
        'close_clean': close_clean,
        'defensive_intraday_confirmed': defensive_intraday,
        'negative_sectors': recent_negative_sectors,
        'notes': notes,
    }


def _attach_regime_execution_review(bundle, *, summary):
    bundle = dict(bundle or {})
    review = _build_regime_execution_review(bundle=bundle, summary=summary)
    summary_section = dict(bundle.get('summary', {}) if isinstance(bundle.get('summary', {}), dict) else {})
    summary_section['regime_execution_available'] = bool(review.get('available'))
    summary_section['regime_execution_verdict'] = str(review.get('verdict', '')).strip()
    summary_section['regime_execution_score'] = _inum(review.get('score', 0), 0)
    summary_section['regime_execution_positive_sample'] = bool(review.get('positive_sample'))
    summary_section['regime_execution_label'] = str(review.get('label', '')).strip()
    bundle['summary'] = summary_section
    bundle['regime_execution_review'] = review
    return bundle


def _attach_intraday_judgment_review(bundle, *, summary, records=None, positions=None):
    bundle = dict(bundle or {})
    review = _build_intraday_judgment_review(
        bundle=bundle,
        summary=summary,
        records=records,
        positions=positions,
    )
    summary_section = dict(bundle.get('summary', {}) if isinstance(bundle.get('summary', {}), dict) else {})
    summary_section['intraday_judgment_available'] = bool(review.get('available'))
    summary_section['intraday_judgment_verdict'] = str(review.get('verdict', '')).strip()
    summary_section['intraday_judgment_score'] = _inum(review.get('score', 0), 0)
    bundle['summary'] = summary_section
    bundle['intraday_judgment_review'] = review
    return bundle


def _summarize_daily_evolution_bundle(bundle):
    bundle = bundle if isinstance(bundle, dict) else {}
    summary = bundle.get('summary', {}) if isinstance(bundle.get('summary', {}), dict) else {}
    capital_feedback = bundle.get('capital_allocation_feedback', {}) if isinstance(bundle.get('capital_allocation_feedback', {}), dict) else {}
    intraday_judgment_review = (
        bundle.get('intraday_judgment_review', {})
        if isinstance(bundle.get('intraday_judgment_review', {}), dict) else {}
    )
    regime_execution_review = (
        bundle.get('regime_execution_review', {})
        if isinstance(bundle.get('regime_execution_review', {}), dict) else {}
    )
    missed_opportunity_summary = (
        bundle.get('missed_opportunity_summary', {})
        if isinstance(bundle.get('missed_opportunity_summary', {}), dict) else {}
    )
    blocked_reason_counts = {}
    for item in (bundle.get('observe_only_items') or []):
        for reason in (item.get('blocked_reasons') or []):
            key = str(reason or '').strip()
            if not key:
                continue
            blocked_reason_counts[key] = blocked_reason_counts.get(key, 0) + 1
    return {
        'learnable_sample_count': _inum(summary.get('learnable_sample_count', 0), 0),
        'observe_only_count': _inum(summary.get('observe_only_count', 0), 0),
        'execution_damaged_count': _inum(summary.get('execution_damaged_count', 0), 0),
        'profit_truncation_count': _inum(summary.get('profit_truncation_count', 0), 0),
        'alpha_loss_event_count': _inum(summary.get('alpha_loss_event_count', 0), 0),
        'learnable_codes': [item.get('code', '') for item in (bundle.get('direct_learn_items') or [])[:10]],
        'observe_only_codes': [item.get('code', '') for item in (bundle.get('observe_only_items') or [])[:10]],
        'execution_damaged_codes': [item.get('code', '') for item in (bundle.get('execution_damaged_items') or [])[:10]],
        'profit_expansion_codes': [item.get('code', '') for item in (bundle.get('profit_truncation_items') or [])[:10]],
        'blocked_reason_counts': blocked_reason_counts,
        'capital_allocation_verdict': str(capital_feedback.get('verdict', '')).strip(),
        'today_biased_closed_count': _inum((capital_feedback.get('today_biased_closed') or {}).get('count', 0), 0),
        'historical_biased_closed_count': _inum((capital_feedback.get('historical_biased_closed') or {}).get('count', 0), 0),
        'intraday_judgment_verdict': str(intraday_judgment_review.get('verdict', '')).strip(),
        'intraday_judgment_score': _inum(intraday_judgment_review.get('score', 0), 0),
        'regime_execution_verdict': str(regime_execution_review.get('verdict', '')).strip(),
        'regime_execution_score': _inum(regime_execution_review.get('score', 0), 0),
        'regime_execution_positive_sample': bool(regime_execution_review.get('positive_sample')),
        'regime_execution_label': str(regime_execution_review.get('label', '')).strip(),
        'missed_opportunity_count': _inum(missed_opportunity_summary.get('matured_count', 0), 0),
        'missed_opportunity_positive_count': _inum(missed_opportunity_summary.get('positive_count', 0), 0),
        'missed_opportunity_strong_positive_count': _inum(missed_opportunity_summary.get('strong_positive_count', 0), 0),
        'missed_opportunity_avg_return_pct': _fnum(missed_opportunity_summary.get('avg_return_pct', 0.0), 0.0),
        'missed_opportunity_pending_count': _inum(missed_opportunity_summary.get('pending_count', 0), 0),
        'missed_opportunity_codes': [item.get('code', '') for item in (bundle.get('missed_opportunity_items') or [])[:10]],
        'missed_positive_opportunity_codes': [code for code in (missed_opportunity_summary.get('positive_codes') or []) if code][:10],
    }


def _build_evolution_followups(evolution_absorption, *, engineering_review=None):
    evolution_absorption = evolution_absorption if isinstance(evolution_absorption, dict) else {}
    engineering_review = engineering_review if isinstance(engineering_review, dict) else {}
    followups = []
    execution_damaged_count = _inum(evolution_absorption.get('execution_damaged_count', 0), 0)
    profit_truncation_count = _inum(evolution_absorption.get('profit_truncation_count', 0), 0)
    capital_allocation_verdict = str(evolution_absorption.get('capital_allocation_verdict', '')).strip()
    intraday_judgment_verdict = str(evolution_absorption.get('intraday_judgment_verdict', '')).strip()
    regime_execution_verdict = str(evolution_absorption.get('regime_execution_verdict', '')).strip()
    regime_execution_positive_sample = bool(evolution_absorption.get('regime_execution_positive_sample'))
    missed_opportunity_positive_count = _inum(evolution_absorption.get('missed_opportunity_positive_count', 0), 0)
    missed_opportunity_strong_positive_count = _inum(evolution_absorption.get('missed_opportunity_strong_positive_count', 0), 0)
    if execution_damaged_count > 0:
        followups.append({
            'category': 'execution_layer',
            'severity': 'warn',
            'code': 'execution_damaged_samples_present',
            'summary': f'识别到 {execution_damaged_count} 个执行折损样本，应隔离出参数学习，只进入执行层/复检层进化。',
            'action': 'exclude_execution_damaged_from_parameter_learning',
            'codes': [code for code in (evolution_absorption.get('execution_damaged_codes') or []) if code][:10],
        })
    if profit_truncation_count > 0:
        followups.append({
            'category': 'strategy_layer',
            'severity': 'warn',
            'code': 'profit_expansion_followup_required',
            'summary': f'识别到 {profit_truncation_count} 个利润扩张样本，应进入大肉连续管理复盘，而不是直接更新参数。',
            'action': 'route_to_profit_expansion_review',
            'codes': [code for code in (evolution_absorption.get('profit_expansion_codes') or []) if code][:10],
        })
    if intraday_judgment_verdict == 'invalidated':
        followups.append({
            'category': 'judgment_layer',
            'severity': 'warn',
            'code': 'intraday_judgment_invalidated',
            'summary': '午盘判断在尾盘未被验证，需进入判断层校准而不是直接复用午盘结论。',
            'action': 'review_intraday_judgment_features_and_confidence',
        })
    elif intraday_judgment_verdict == 'mixed':
        followups.append({
            'category': 'judgment_layer',
            'severity': 'info',
            'code': 'intraday_judgment_needs_calibration',
            'summary': '午盘判断只得到部分验证，需继续积累相似盘面样本做校准。',
            'action': 'track_intraday_judgment_mixed_cases',
        })
    if capital_allocation_verdict == 'underperforming':
        followups.append({
            'category': 'strategy_layer',
            'severity': 'warn',
            'code': 'capital_bias_underperforming',
            'summary': '历史资金倾斜样本表现弱于非倾斜样本，需复核加码对象与市况分层。',
            'action': 'review_capital_bias_by_regime',
            'codes': [code for code in (evolution_absorption.get('learnable_codes') or []) if code][:10],
        })
    if regime_execution_positive_sample and regime_execution_verdict == 'positive':
        followups.append({
            'category': 'regime_layer',
            'severity': 'info',
            'code': 'defensive_low_exposure_positive_sample',
            'summary': (
                '识别到连续高风险窗口下的优秀防守样本，应沉淀为市场环境/仓位执行正样本，'
                '用于提醒学习层不要只奖励买卖动作本身。'
            ),
            'action': 'record_regime_execution_positive_sample_for_defensive_learning',
        })
    if missed_opportunity_positive_count > 0:
        followups.append({
            'category': 'deployment_layer',
            'severity': 'warn' if missed_opportunity_strong_positive_count > 0 else 'info',
            'code': 'missed_positive_opportunities_present',
            'summary': (
                f'识别到 {missed_opportunity_positive_count} 个已选未买且 D1 后验为正的样本，'
                '需复核午盘放仓与尾盘执行门控是否过度保守。'
            ),
            'action': 'review_release_thresholds_against_missed_opportunity_history',
            'codes': [code for code in (evolution_absorption.get('missed_positive_opportunity_codes') or []) if code][:10],
        })
    engineering_incident_count = _inum(engineering_review.get('incident_count', 0), 0)
    if engineering_incident_count > 0:
        followups.append({
            'category': 'engineering_layer',
            'severity': 'warn',
            'code': 'engineering_incidents_need_guardrails',
            'summary': (
                f'当日识别到 {engineering_incident_count} 个代码能力事件，'
                '应沉淀为硬约束与回归测试，而不是只做现场修复。'
            ),
            'action': 'promote_engineering_incidents_to_constraints_and_tests',
            'codes': [code for code in (engineering_review.get('incident_codes') or []) if code][:10],
        })
    return followups


def _build_regime_bias_action(bundle, *, trade_date=''):
    bundle = bundle if isinstance(bundle, dict) else {}
    trade_date = _date_key(trade_date or bundle.get('trade_date') or datetime.now().strftime('%Y-%m-%d'))
    review = bundle.get('regime_execution_review', {}) if isinstance(bundle.get('regime_execution_review', {}), dict) else {}
    history = _load_regime_execution_history(limit=240)
    positive_items = [
        item for item in history
        if bool(item.get('positive_sample'))
        and str(item.get('label', '')).strip() == 'defensive-low-exposure-good-execution'
    ]
    positive_dates = [str(item.get('trade_date', '')).strip() for item in positive_items if str(item.get('trade_date', '')).strip()]
    positive_count = len(positive_dates)
    current_positive = bool(
        review.get('positive_sample')
        and str(review.get('label', '')).strip() == 'defensive-low-exposure-good-execution'
        and _date_key(review.get('trade_date') or trade_date) == trade_date
    )
    stage = 'observe_only'
    active = False
    action = {
        'seed_penalty': 0.0,
        'ranking_penalty': 0.0,
        'target_amount_ratio': 1.0,
        'initial_amount_ratio': 1.0,
        'add_position_target_ratio': 1.0,
        'allow_aggressive_add': True,
        'block_new_position': False,
        'skip_add_position': False,
        'reason': '',
    }
    notes = []
    if current_positive:
        notes.append('当日新增 1 条 defensive-low-exposure-good-execution 样本，但不再用于次日缩仓奖励。')
    if positive_count > 0:
        notes.append(f"当前累计 {positive_count} 条风控正样本，仅保留复核记录，不触发仓位收缩。")
    else:
        notes.append('当前无风控正样本累计，仓位学习保持中性。')
    return {
        'trade_date': trade_date,
        'active': active,
        'stage': stage,
        'positive_sample_count': positive_count,
        'positive_sample_dates': positive_dates[-8:],
        'current_positive_sample': current_positive,
        'soft_threshold': 3,
        'strong_threshold': 5,
        'notes': notes,
        **action,
    }


def _build_learning_actions(bundle, *, trade_date=''):
    bundle = bundle if isinstance(bundle, dict) else {}
    trade_date = _date_key(trade_date or bundle.get('trade_date') or datetime.now().strftime('%Y-%m-%d'))
    history = [item for item in (bundle.get('trade_episode_history') or []) if isinstance(item, dict)]
    today_dt = _parse_dt(f'{trade_date} 00:00:00') or datetime.now()
    code_cooldowns = {}
    mode_stats = {}
    t3_mode_stats = {}
    profit_expansion_mode_stats = {}
    profit_expansion_code_actions = {}
    for item in history:
        if str(item.get('status', '')).strip() != 'closed':
            continue
        mode = str(item.get('mode', '')).strip()
        if not mode:
            continue
        stats = mode_stats.setdefault(mode, {
            'sample_count': 0,
            'false_selection_count': 0,
            'big_meat_success_count': 0,
            'execution_damaged_count': 0,
            'profit_truncation_count': 0,
            'total_return_pct': 0.0,
        })
        stats['sample_count'] += 1
        stats['false_selection_count'] += 1 if bool(item.get('false_selection_flag')) else 0
        stats['big_meat_success_count'] += 1 if bool(item.get('big_meat_success_flag')) else 0
        stats['execution_damaged_count'] += 1 if bool(item.get('execution_damaged')) else 0
        stats['profit_truncation_count'] += 1 if bool(item.get('profit_truncation')) else 0
        stats['total_return_pct'] += _fnum(item.get('pnl_pct', 0.0), 0.0)
        if bool(item.get('t3_observe_flag')):
            t3_stats = t3_mode_stats.setdefault(mode, {
                'sample_count': 0,
                'false_selection_count': 0,
                'big_meat_success_count': 0,
            })
            t3_stats['sample_count'] += 1
            t3_stats['false_selection_count'] += 1 if bool(item.get('false_selection_flag')) else 0
            t3_stats['big_meat_success_count'] += 1 if bool(item.get('big_meat_success_flag')) else 0
        if bool(item.get('profit_truncation')):
            reason_text = str(item.get('close_reason', '')).strip()
            sell_date = _date_key(item.get('sell_date'))
            sell_dt = _parse_dt(f'{sell_date} 00:00:00') if sell_date else None
            age_days = (today_dt - sell_dt).days if sell_dt else 999
            opening_shock_truncation = (
                ('冲高回落上影线' in reason_text or '大阴线' in reason_text)
                and '趋势终结' not in reason_text
                and '连跌2日' not in reason_text
                and '放量滞涨' not in reason_text
            )
            mode_profit_stats = profit_expansion_mode_stats.setdefault(mode, {
                'count': 0,
                'candidate_count': 0,
                'opening_shock_count': 0,
                'recent_count': 0,
            })
            mode_profit_stats['count'] += 1
            candidate_flag = (
                str(item.get('big_meat_state', '')).strip() == BIG_MEAT_STATE_CANDIDATE
                or bool(item.get('candidate_pool_flag'))
            )
            if candidate_flag:
                mode_profit_stats['candidate_count'] += 1
            if opening_shock_truncation:
                mode_profit_stats['opening_shock_count'] += 1
            if age_days <= LEARNING_CODE_STRONG_COOLDOWN_DAYS:
                mode_profit_stats['recent_count'] += 1
                code = str(item.get('code', '')).zfill(6)
                previous = profit_expansion_code_actions.get(code, {})
                if _date_key(previous.get('last_sell_date')) < sell_date:
                    profit_expansion_code_actions[code] = {
                        'code': code,
                        'mode': mode,
                        'last_sell_date': sell_date,
                        'opening_shock_hold': bool(opening_shock_truncation),
                        'allow_prelock_add': bool(candidate_flag),
                        'source_episode_id': str(item.get('episode_id', '')).strip(),
                        'reason': 'recent_profit_truncation_sample',
                    }
        if not bool(item.get('false_selection_flag')):
            continue
        sell_date = _date_key(item.get('sell_date'))
        sell_dt = _parse_dt(f'{sell_date} 00:00:00') if sell_date else None
        if not sell_dt:
            continue
        age_days = (today_dt - sell_dt).days
        if age_days < 0 or age_days > LEARNING_CODE_STRONG_COOLDOWN_DAYS:
            continue
        cooldown_days = (
            LEARNING_CODE_STRONG_COOLDOWN_DAYS
            if str(item.get('falsify_level', '')).strip() == 'strong'
            else LEARNING_CODE_COOLDOWN_DAYS
        )
        cooldown_until = (sell_dt + timedelta(days=cooldown_days)).strftime('%Y-%m-%d')
        code = str(item.get('code', '')).zfill(6)
        previous = code_cooldowns.get(code, {})
        previous_until = _date_key(previous.get('cooldown_until'))
        if previous_until and previous_until >= cooldown_until:
            continue
        code_cooldowns[code] = {
            'code': code,
            'mode': mode,
            'falsify_level': str(item.get('falsify_level', '')).strip(),
            'reason_codes': list(item.get('falsify_reason_codes', [])),
            'cooldown_until': cooldown_until,
            'last_sell_date': sell_date,
            'source_episode_id': str(item.get('episode_id', '')).strip(),
        }
    mode_biases = {}
    blocked_t3_modes = []
    for mode, stats in mode_stats.items():
        sample_count = _inum(stats.get('sample_count', 0), 0)
        if sample_count <= 0:
            continue
        false_rate = _inum(stats.get('false_selection_count', 0), 0) / sample_count
        success_rate = _inum(stats.get('big_meat_success_count', 0), 0) / sample_count
        avg_return_pct = _fnum(stats.get('total_return_pct', 0.0), 0.0) / sample_count
        action = {
            'sample_count': sample_count,
            'false_selection_rate_pct': round(false_rate * 100.0, 2),
            'big_meat_success_rate_pct': round(success_rate * 100.0, 2),
            'avg_return_pct': round(avg_return_pct, 4),
            'seed_penalty': 0.0,
            'ranking_penalty': 0.0,
            'target_amount_ratio': 1.0,
            'initial_amount_ratio': 1.0,
            'block_new_position': False,
            'skip_add_position': False,
            'reason': '',
        }
        if sample_count >= 3 and false_rate >= 0.67 and success_rate <= 0.1:
            action.update({
                'seed_penalty': 1.8,
                'ranking_penalty': 1.5,
                'target_amount_ratio': 0.72,
                'initial_amount_ratio': 0.72,
                'block_new_position': True,
                'skip_add_position': True,
                'reason': '历史错选率过高，进入强降权/冷却',
            })
        elif sample_count >= 3 and false_rate >= 0.5:
            action.update({
                'seed_penalty': 1.1,
                'ranking_penalty': 0.9,
                'target_amount_ratio': 0.82,
                'initial_amount_ratio': 0.85,
                'skip_add_position': True,
                'reason': '历史错选率偏高，降权并停止培养',
            })
        elif sample_count >= 2 and avg_return_pct <= -2.0 and success_rate == 0.0:
            action.update({
                'seed_penalty': 0.8,
                'ranking_penalty': 0.6,
                'target_amount_ratio': 0.88,
                'initial_amount_ratio': 0.9,
                'reason': '历史回报偏弱，先收缩试错',
            })
        if action['seed_penalty'] > 0 or action['block_new_position'] or action['skip_add_position']:
            mode_biases[mode] = action
    for mode, stats in t3_mode_stats.items():
        sample_count = _inum(stats.get('sample_count', 0), 0)
        if sample_count < 2:
            continue
        false_rate = _inum(stats.get('false_selection_count', 0), 0) / sample_count
        success_rate = _inum(stats.get('big_meat_success_count', 0), 0) / sample_count
        if false_rate >= 0.67 and success_rate == 0.0:
            blocked_t3_modes.append(mode)
    profit_expansion_mode_actions = {}
    for mode, stats in profit_expansion_mode_stats.items():
        if _inum(stats.get('count', 0), 0) <= 0:
            continue
        allow_prelock_add = _inum(stats.get('candidate_count', 0), 0) > 0
        opening_shock_hold = _inum(stats.get('opening_shock_count', 0), 0) > 0
        if not allow_prelock_add and not opening_shock_hold:
            continue
        reasons = []
        if allow_prelock_add:
            reasons.append('利润扩张样本提示候选态继续培养')
        if opening_shock_hold:
            reasons.append('利润扩张样本提示开盘强震荡先保护观察')
        profit_expansion_mode_actions[mode] = {
            'profit_truncation_count': _inum(stats.get('count', 0), 0),
            'recent_profit_truncation_count': _inum(stats.get('recent_count', 0), 0),
            'allow_prelock_add': allow_prelock_add,
            'opening_shock_hold': opening_shock_hold,
            'reason': '；'.join(reasons[:2]),
        }
    regime_bias_action = _build_regime_bias_action(bundle, trade_date=trade_date)
    summary = {
        'history_episode_count': _inum((bundle.get('history_summary') or {}).get('episode_count', 0), 0),
        'mode_bias_count': len(mode_biases),
        'code_cooldown_count': len(code_cooldowns),
        'blocked_t3_mode_count': len(blocked_t3_modes),
        'profit_expansion_mode_count': len(profit_expansion_mode_actions),
        'profit_expansion_code_count': len(profit_expansion_code_actions),
        'regime_positive_sample_count': _inum(regime_bias_action.get('positive_sample_count', 0), 0),
        'regime_bias_stage': str(regime_bias_action.get('stage', '')).strip(),
        'regime_bias_active': bool(regime_bias_action.get('active')),
        'recent_false_selection_codes': sorted(code_cooldowns.keys())[:12],
    }
    return {
        'generated_at': _now_str(),
        'trade_date': trade_date,
        'summary': summary,
        'mode_biases': mode_biases,
        'regime_bias_action': regime_bias_action,
        'code_cooldowns': code_cooldowns,
        'profit_expansion_mode_actions': profit_expansion_mode_actions,
        'profit_expansion_code_actions': profit_expansion_code_actions,
        'blocked_t3_modes': sorted(set(blocked_t3_modes)),
        'notes': [
            '学习动作直接由真实买卖 episode 历史生成，优先约束错选重入与弱模式继续培养。',
            'code_cooldowns 用于阻断近期证伪票重入；mode_biases 用于降低弱模式的新开仓和加仓优先级。',
            'profit_expansion_actions 会把卖飞/漏培养样本自动转成次日可消费的持有与加仓保护动作。',
            'regime_bias_action 会在风控正样本累计达到 3/5 条后，微幅收缩新仓和加仓强度，但不直接一刀切封死交易。',
        ],
    }


def _blocked_t3_modes_from_learning_actions(learning_actions):
    learning_actions = learning_actions if isinstance(learning_actions, dict) else {}
    return {
        str(item or '').strip()
        for item in (learning_actions.get('blocked_t3_modes') or [])
        if str(item or '').strip()
    }


def _load_learning_gate_payload():
    payload = _read_json(LEARNING_GATE_FILE)
    return payload if isinstance(payload, dict) else {}


def _resolve_learning_preflight_guard(*, trade_date='', use_previous_close_for_intraday=True):
    trade_date = _date_key(trade_date or datetime.now().strftime('%Y-%m-%d'))
    payload = _load_learning_gate_payload()
    learning_actions = _load_learning_actions()
    regime_bias_action = (
        learning_actions.get('regime_bias_action', {})
        if isinstance(learning_actions.get('regime_bias_action', {}), dict) else {}
    )
    expected_gate_date = trade_date
    if use_previous_close_for_intraday and trade_date:
        try:
            expected_gate_date = previous_trading_day(trade_date).isoformat()
        except Exception:
            expected_gate_date = trade_date
    if not payload:
        return {
            'available': False,
            'status': 'missing',
            'allow_buy': False,
            'allow_add_position': False,
            'allow_aggressive_add': False,
            'reason': 'missing_learning_gate',
            'notes': [
                (
                    f'缺少最近有效收盘 learning gate（expected={expected_gate_date or "unknown"}），'
                    '先完成 close-node/账本复核再交易。'
                )
            ],
        }

    payload_date = _date_key(payload.get('date') or payload.get('generated_at'))
    if not payload_date or payload_date != expected_gate_date:
        return {
            'available': False,
            'status': 'stale',
            'allow_buy': False,
            'allow_add_position': False,
            'allow_aggressive_add': False,
            'reason': f'stale_learning_gate:{payload_date or "unknown"}',
            'notes': [
                (
                    f'learning gate 日期={payload_date or "unknown"}，'
                    f'预期最近有效收盘={expected_gate_date or "unknown"}，未通过交易前复核。'
                )
            ],
        }

    status = str(payload.get('learning_gate_status', '')).strip().lower() or 'reject'
    reason_codes = [
        str(item or '').strip()
        for item in (payload.get('reason_codes') or [])
        if str(item or '').strip()
    ]
    reason_code_set = set(reason_codes)
    basis = payload.get('learning_gate_basis', {}) if isinstance(payload.get('learning_gate_basis', {}), dict) else {}
    strict_hold_codes = {
        'close_summary_not_live',
        'stale_pending_orders_at_close',
        'close_reconcile_adjustments_present',
    }
    benign_hold_codes = {
        'no_closed_samples_today',
        'no_learnable_samples_after_absorption',
    }
    benign_hold = status == 'hold' and reason_code_set and reason_code_set <= benign_hold_codes
    strict_hold = (
        status == 'hold'
        and not benign_hold
        and (
            bool(reason_code_set & strict_hold_codes)
            or _inum(basis.get('engineering_incident_count', 0), 0) > 0
        )
    )

    allow_buy = status == 'allow'
    allow_add_position = status == 'allow'
    allow_aggressive_add = status == 'allow'
    notes = []
    reason = status

    if status == 'reject':
        reason = 'learning_gate_reject'
        notes.append('收盘复核明确拒绝放行，次日禁止新开仓和加仓。')
    elif strict_hold:
        reason = 'learning_gate_hold_strict'
        notes.append('收盘复核处于 hold 且涉及账本/挂单/工程风险，次日禁止新开仓和加仓。')
    elif benign_hold:
        reason = 'learning_gate_hold_benign'
        allow_buy = True
        allow_add_position = True
        allow_aggressive_add = False
        notes.append('收盘复核仅因样本不足进入 hold，允许正常交易但禁止激进加仓。')
    elif status == 'hold':
        reason = 'learning_gate_hold'
        notes.append('收盘复核处于 hold，先暂停进攻动作，等待下一次 close-node 放行。')
    else:
        notes.append('收盘复核通过，允许按当前策略执行交易。')

    if reason_codes:
        notes.append(f"gate原因: {'/'.join(reason_codes[:4])}")
    if bool(regime_bias_action.get('active')):
        allow_aggressive_add = False
        notes.append(
            f"风控正样本累计{_inum(regime_bias_action.get('positive_sample_count', 0), 0)}条，"
            f"阶段={str(regime_bias_action.get('stage', '')).strip() or 'unknown'}，"
            '次日仅做保守培养，不放大激进加仓。'
        )

    return {
        'available': True,
        'status': status,
        'allow_buy': allow_buy,
        'allow_add_position': allow_add_position,
        'allow_aggressive_add': allow_aggressive_add,
        'reason': reason,
        'reason_codes': reason_codes,
        'notes': notes,
    }


def _as_report_only_learning_preflight(payload=None):
    payload = payload if isinstance(payload, dict) else {}
    normalized = dict(payload)
    normalized['reported_allow_buy'] = bool(payload.get('allow_buy', False))
    normalized['reported_allow_add_position'] = bool(payload.get('allow_add_position', False))
    normalized['reported_allow_aggressive_add'] = bool(payload.get('allow_aggressive_add', False))
    normalized['allow_buy'] = True
    normalized['allow_add_position'] = True
    normalized['allow_aggressive_add'] = True
    normalized['report_only'] = True
    notes = list(normalized.get('notes', []) or [])
    if (
        (not normalized['reported_allow_buy'])
        or (not normalized['reported_allow_add_position'])
        or (not normalized['reported_allow_aggressive_add'])
    ):
        notes.append('当前配置: learning gate 仅汇报，不再阻断买入、加仓或激进加仓。')
    normalized['notes'] = notes
    return normalized


def _load_learning_actions():
    payload = _read_json(LEARNING_ACTIONS_FILE)
    return payload if isinstance(payload, dict) else {}


def _resolve_learning_trade_action(code, mode, learning_actions, *, trade_date=''):
    learning_actions = learning_actions if isinstance(learning_actions, dict) else {}
    trade_date = _date_key(trade_date or datetime.now().strftime('%Y-%m-%d'))
    code = str(code or '').zfill(6)
    mode = str(mode or '').strip()
    code_entry = (
        learning_actions.get('code_cooldowns', {}).get(code, {})
        if isinstance(learning_actions.get('code_cooldowns', {}), dict) else {}
    )
    mode_entry = (
        learning_actions.get('mode_biases', {}).get(mode, {})
        if isinstance(learning_actions.get('mode_biases', {}), dict) else {}
    )
    regime_entry = (
        learning_actions.get('regime_bias_action', {})
        if isinstance(learning_actions.get('regime_bias_action', {}), dict) else {}
    )
    profit_mode_entry = (
        learning_actions.get('profit_expansion_mode_actions', {}).get(mode, {})
        if isinstance(learning_actions.get('profit_expansion_mode_actions', {}), dict) else {}
    )
    profit_code_entry = (
        learning_actions.get('profit_expansion_code_actions', {}).get(code, {})
        if isinstance(learning_actions.get('profit_expansion_code_actions', {}), dict) else {}
    )
    cooldown_until = _date_key(code_entry.get('cooldown_until'))
    code_block_active = bool(cooldown_until) and trade_date <= cooldown_until
    reasons = []
    if code_block_active:
        reasons.append(f"近期证伪冷却至{cooldown_until}")
    if bool(mode_entry.get('reason')):
        reasons.append(str(mode_entry.get('reason', '')).strip())
    if bool(regime_entry.get('active')) and bool(regime_entry.get('reason')):
        reasons.append(str(regime_entry.get('reason', '')).strip())
    if bool(profit_code_entry.get('reason') or profit_mode_entry.get('reason')):
        reasons.append(str(profit_code_entry.get('reason') or profit_mode_entry.get('reason') or '').strip())
    seed_penalty = max(
        _fnum(mode_entry.get('seed_penalty', 0.0), 0.0),
        _fnum(regime_entry.get('seed_penalty', 0.0), 0.0) if bool(regime_entry.get('active')) else 0.0,
    )
    ranking_penalty = max(
        _fnum(mode_entry.get('ranking_penalty', 0.0), 0.0),
        _fnum(regime_entry.get('ranking_penalty', 0.0), 0.0) if bool(regime_entry.get('active')) else 0.0,
    )
    target_amount_ratio = min(
        _fnum(mode_entry.get('target_amount_ratio', 1.0), 1.0),
        _fnum(regime_entry.get('target_amount_ratio', 1.0), 1.0) if bool(regime_entry.get('active')) else 1.0,
    )
    initial_amount_ratio = min(
        _fnum(mode_entry.get('initial_amount_ratio', 1.0), 1.0),
        _fnum(regime_entry.get('initial_amount_ratio', 1.0), 1.0) if bool(regime_entry.get('active')) else 1.0,
    )
    add_position_target_ratio = (
        _fnum(regime_entry.get('add_position_target_ratio', 1.0), 1.0)
        if bool(regime_entry.get('active')) else 1.0
    )
    allow_prelock_add = bool(profit_code_entry.get('allow_prelock_add') or profit_mode_entry.get('allow_prelock_add'))
    opening_shock_hold = bool(profit_code_entry.get('opening_shock_hold') or profit_mode_entry.get('opening_shock_hold'))
    if code_block_active:
        seed_penalty = max(seed_penalty, 1.2)
        ranking_penalty = max(ranking_penalty, 1.0)
        target_amount_ratio = min(target_amount_ratio, 0.75)
        initial_amount_ratio = min(initial_amount_ratio, 0.75)
    return {
        'block_new_position': code_block_active or bool(mode_entry.get('block_new_position')) or bool(regime_entry.get('block_new_position')),
        'skip_add_position': code_block_active or bool(mode_entry.get('skip_add_position')) or bool(regime_entry.get('skip_add_position')),
        'seed_penalty': round(seed_penalty, 2),
        'ranking_penalty': round(ranking_penalty, 2),
        'target_amount_ratio': round(target_amount_ratio, 4),
        'initial_amount_ratio': round(initial_amount_ratio, 4),
        'add_position_target_ratio': round(add_position_target_ratio, 4),
        'allow_prelock_add': allow_prelock_add,
        'opening_shock_hold': opening_shock_hold,
        'regime_stage': str(regime_entry.get('stage', '')).strip(),
        'reason': ' / '.join([item for item in reasons if item]),
    }


def _apply_learning_action_to_seed_profile(seed_profile, learning_action, *, preserve_t1=False):
    seed_profile = dict(seed_profile or {})
    learning_action = learning_action if isinstance(learning_action, dict) else {}
    if preserve_t1 or _inum(seed_profile.get('effective_tier', 0), 0) == 1:
        if learning_action.get('reason'):
            reason = str(seed_profile.get('reason', '')).strip()
            seed_profile['reason'] = f"{reason}; 学习层={learning_action['reason']}" if reason else f"学习层={learning_action['reason']}"
        return seed_profile
    adjusted_score = max(0.0, _fnum(seed_profile.get('seed_score', 0.0), 0.0) - _fnum(learning_action.get('seed_penalty', 0.0), 0.0))
    seed_profile['seed_score'] = round(adjusted_score, 2)
    seed_profile['ranking_bonus'] = round(
        _fnum(seed_profile.get('ranking_bonus', 0.0), 0.0) - _fnum(learning_action.get('ranking_penalty', 0.0), 0.0),
        2,
    )
    seed_profile['target_amount_ratio'] = round(
        _fnum(seed_profile.get('target_amount_ratio', 1.0), 1.0) * _fnum(learning_action.get('target_amount_ratio', 1.0), 1.0),
        4,
    )
    seed_profile['initial_amount_ratio'] = round(
        _fnum(seed_profile.get('initial_amount_ratio', 1.0), 1.0) * _fnum(learning_action.get('initial_amount_ratio', 1.0), 1.0),
        4,
    )
    if adjusted_score >= BIG_MEAT_BUY_SEED_STRONG_THRESHOLD:
        seed_profile['effective_tier'] = 2
        seed_profile['pool_note'] = BIG_MEAT_BUY_POOL_CANDIDATE_NOTE
        seed_profile['pool_label'] = 'T2候选核心'
        seed_profile['priority_rank'] = 2
    elif adjusted_score >= BIG_MEAT_BUY_SEED_T2_THRESHOLD:
        seed_profile['effective_tier'] = 2
        seed_profile['pool_note'] = BIG_MEAT_BUY_POOL_CANDIDATE_NOTE
        seed_profile['pool_label'] = 'T2候选'
        seed_profile['priority_rank'] = 2
    else:
        seed_profile['effective_tier'] = 3
        seed_profile['pool_note'] = BIG_MEAT_BUY_POOL_OBSERVE_NOTE
        seed_profile['pool_label'] = 'T3观察'
        seed_profile['priority_rank'] = 1
    if learning_action.get('reason'):
        reason = str(seed_profile.get('reason', '')).strip()
        seed_profile['reason'] = f"{reason}; 学习层={learning_action['reason']}" if reason else f"学习层={learning_action['reason']}"
    return seed_profile


def _phase_history_trade_date(row):
    row = row if isinstance(row, dict) else {}
    for key in ('generated_at', 'started_at', 'finished_at'):
        text = str(row.get(key, '')).strip()
        if len(text) >= 10 and text[4:5] == '-' and text[7:8] == '-':
            return text[:10]
    run_id = str(row.get('run_id', '')).strip()
    if len(run_id) >= 8 and run_id[:8].isdigit():
        return f'{run_id[:4]}-{run_id[4:6]}-{run_id[6:8]}'
    return ''


def _classify_engineering_failure_kind(row):
    row = row if isinstance(row, dict) else {}
    detail = str(row.get('detail', '')).strip().lower()
    exit_code = _inum(row.get('exit_code', 0), 0)
    if 'timeout' in detail or exit_code == 124:
        return 'timeout'
    if any(token in detail for token in ('nameerror', 'attributeerror', 'importerror', 'module not found')):
        return 'missing_symbol_or_dependency'
    if 'syntaxerror' in detail:
        return 'syntax_error'
    if any(token in detail for token in ('json decode', 'decode failed', 'schema', 'contract')):
        return 'data_contract_break'
    if str(row.get('status', '')).strip().lower() == 'failed' and exit_code != 0:
        return 'runtime_failure'
    return 'step_failure'


def _engineering_constraint_for_failure(failure_kind, *, phase='', step=''):
    phase = str(phase or '').strip()
    step = str(step or '').strip()
    if failure_kind == 'missing_symbol_or_dependency':
        return f'{phase or step} 改动后必须先跑同阶段定向回归，严禁未定义 helper/常量直接上线。'
    if failure_kind == 'timeout':
        return f'{phase or step} 关键阶段必须维持可控耗时，新增逻辑后要补耗时回归与降级路径。'
    if failure_kind == 'data_contract_break':
        return '跨节点新增字段必须补默认值、兼容读取与链路回归测试。'
    if failure_kind == 'syntax_error':
        return '提交前必须完成最小可运行校验，避免语法级故障进入自动化任务。'
    return f'{phase or step} 关键阶段改动后必须补定向回归测试并记录根因。'


def _engineering_test_hint_for_failure(failure_kind):
    if failure_kind == 'timeout':
        return '补阶段耗时/超时回归，确保关键脚本在预算内完成。'
    if failure_kind == 'data_contract_break':
        return '补跨节点字段传递与默认值兼容测试。'
    if failure_kind == 'missing_symbol_or_dependency':
        return '补同阶段 smoke test，锁死 helper/依赖接线。'
    return '补定向回归测试，覆盖出错阶段的最小复现路径。'


def _engineering_incident_code(phase, step, failure_kind):
    text = f'{str(phase or "").strip()}_{Path(str(step or "")).stem}_{str(failure_kind or "").strip()}'
    return re.sub(r'[^a-zA-Z0-9_]+', '_', text).strip('_').lower()


def _load_manual_engineering_incidents(*, trade_date=''):
    payload = _read_json(ENGINEERING_MANUAL_INCIDENTS_FILE)
    items = payload.get('incidents', []) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    normalized = []
    target_trade_date = str(trade_date or '').strip()
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item_trade_date = str(raw.get('trade_date', '')).strip()
        if target_trade_date and item_trade_date and item_trade_date != target_trade_date:
            continue
        if str(raw.get('status', 'active')).strip().lower() not in {'', 'active', 'open'}:
            continue
        phase = str(raw.get('phase', '')).strip() or 'engineering-review'
        step = str(raw.get('step', '')).strip() or 'manual'
        failure_kind = str(raw.get('failure_kind', '')).strip() or 'logic_boundary'
        incident_code = str(raw.get('incident_code', '')).strip() or _engineering_incident_code(phase, step, failure_kind)
        impact_level = str(raw.get('impact_level', '')).strip().lower() or 'high'
        normalized.append({
            'phase': phase,
            'step': step,
            'task_name': str(raw.get('task_name', '')).strip(),
            'trigger_slot': str(raw.get('trigger_slot', '')).strip(),
            'status': 'confirmed',
            'exit_code': _inum(raw.get('exit_code', 0), 0),
            'failure_kind': failure_kind,
            'impact_level': impact_level,
            'recurrence_count': max(1, _inum(raw.get('recurrence_count', 1), 1)),
            'incident_code': incident_code,
            'detail': str(raw.get('detail', '')).strip(),
            'command': str(raw.get('command', '')).strip(),
            'root_cause_hint': str(raw.get('root_cause_hint', '')).strip(),
            'test_hint': str(raw.get('test_hint', '')).strip(),
            'source': str(raw.get('source', 'manual_confirmation')).strip() or 'manual_confirmation',
        })
    return normalized


def _build_engineering_review(*, trade_date=''):
    rows = _read_csv_rows(PHASE_HISTORY_DETAILED_FILE)
    if not rows:
        base_review = {
            'available': False,
            'trade_date': str(trade_date or '').strip(),
            'verdict': 'unavailable',
            'incident_count': 0,
            'recurring_incident_count': 0,
            'category_counts': {},
            'high_severity_count': 0,
            'incident_codes': [],
            'hardening_actions': [],
            'incidents': [],
            'summary': '未找到自动化阶段明细，暂无法构建代码能力复检摘要。',
        }
        manual_incidents = _load_manual_engineering_incidents(trade_date=trade_date)
        if manual_incidents:
            base_review.update({
                'available': True,
                'verdict': 'needs_hardening',
                'incident_count': len(manual_incidents),
                'recurring_incident_count': len([item for item in manual_incidents if _inum(item.get('recurrence_count', 0), 0) > 1]),
                'category_counts': dict(Counter(str(item.get('failure_kind', '')).strip() for item in manual_incidents)),
                'high_severity_count': len([item for item in manual_incidents if str(item.get('impact_level', '')).strip() == 'high']),
                'incident_codes': [item.get('incident_code', '') for item in manual_incidents if item.get('incident_code')],
                'hardening_actions': [
                    str(item.get('root_cause_hint', '')).strip()
                    for item in manual_incidents
                    if str(item.get('root_cause_hint', '')).strip()
                ][:8],
                'incidents': manual_incidents[:12],
                'summary': f'当日通过人工确认补录 {len(manual_incidents)} 个代码能力事件。',
            })
        return base_review

    trade_date = str(trade_date or '').strip()
    if not trade_date:
        trade_date = max((_phase_history_trade_date(row) for row in rows if _phase_history_trade_date(row)), default='')

    failure_rows = []
    history_counts = {}
    for row in rows:
        status = str(row.get('status', '')).strip().lower()
        step = str(row.get('step', '')).strip()
        if step == 'phase' or status != 'failed':
            continue
        failure_kind = _classify_engineering_failure_kind(row)
        key = (
            str(row.get('phase', '')).strip(),
            step,
            failure_kind,
        )
        history_counts[key] = history_counts.get(key, 0) + 1
        if trade_date and _phase_history_trade_date(row) != trade_date:
            continue
        failure_rows.append((row, failure_kind))

    if not trade_date:
        trade_date = datetime.now().strftime('%Y-%m-%d')

    manual_incidents = _load_manual_engineering_incidents(trade_date=trade_date)
    if not failure_rows and not manual_incidents:
        return {
            'available': True,
            'trade_date': trade_date,
            'verdict': 'clean',
            'incident_count': 0,
            'recurring_incident_count': 0,
            'category_counts': {},
            'high_severity_count': 0,
            'incident_codes': [],
            'hardening_actions': [],
            'incidents': [],
            'summary': '当日自动化关键阶段未记录新的代码级失败事件。',
        }

    incidents = []
    category_counts = {}
    hardening_actions = []
    hardening_seen = set()
    high_severity_count = 0
    recurring_incident_count = 0
    for row, failure_kind in failure_rows:
        phase = str(row.get('phase', '')).strip()
        step = str(row.get('step', '')).strip()
        recurrence_count = history_counts.get((phase, step, failure_kind), 1)
        impact_level = 'high' if phase in {'buy', 'add-position', 'smart-sell', 'midday-gate', 'close-node'} else 'medium'
        if impact_level == 'high':
            high_severity_count += 1
        if recurrence_count > 1:
            recurring_incident_count += 1
        category_counts[failure_kind] = category_counts.get(failure_kind, 0) + 1
        action_text = _engineering_constraint_for_failure(failure_kind, phase=phase, step=step)
        if action_text not in hardening_seen:
            hardening_seen.add(action_text)
            hardening_actions.append(action_text)
        incidents.append({
            'phase': phase,
            'step': step,
            'task_name': str(row.get('task_name', '')).strip(),
            'trigger_slot': str(row.get('trigger_slot', '')).strip(),
            'status': str(row.get('status', '')).strip(),
            'exit_code': _inum(row.get('exit_code', 0), 0),
            'failure_kind': failure_kind,
            'impact_level': impact_level,
            'recurrence_count': recurrence_count,
            'incident_code': _engineering_incident_code(phase, step, failure_kind),
            'detail': str(row.get('detail', '')).strip(),
            'command': str(row.get('command', '')).strip(),
            'root_cause_hint': action_text,
            'test_hint': _engineering_test_hint_for_failure(failure_kind),
        })

    existing_codes = {
        str(item.get('incident_code', '')).strip()
        for item in incidents
        if str(item.get('incident_code', '')).strip()
    }
    for item in manual_incidents:
        incident_code = str(item.get('incident_code', '')).strip()
        if incident_code and incident_code in existing_codes:
            continue
        incidents.append(item)

    incidents.sort(
        key=lambda item: (
            0 if item.get('impact_level') == 'high' else 1,
            -_inum(item.get('recurrence_count', 0), 0),
            item.get('phase', ''),
            item.get('step', ''),
        )
    )
    category_counts = {}
    hardening_actions = []
    hardening_seen = set()
    high_severity_count = 0
    recurring_incident_count = 0
    for item in incidents:
        failure_kind = str(item.get('failure_kind', '')).strip() or 'unknown'
        category_counts[failure_kind] = category_counts.get(failure_kind, 0) + 1
        if str(item.get('impact_level', '')).strip() == 'high':
            high_severity_count += 1
        if _inum(item.get('recurrence_count', 0), 0) > 1:
            recurring_incident_count += 1
        action_text = str(item.get('root_cause_hint', '')).strip()
        if action_text and action_text not in hardening_seen:
            hardening_seen.add(action_text)
            hardening_actions.append(action_text)
    verdict = 'needs_hardening'
    if high_severity_count >= 2 or recurring_incident_count >= 2:
        verdict = 'priority_hardening'
    return {
        'available': True,
        'trade_date': trade_date,
        'verdict': verdict,
        'incident_count': len(incidents),
        'recurring_incident_count': recurring_incident_count,
        'category_counts': category_counts,
        'high_severity_count': high_severity_count,
        'incident_codes': [item.get('incident_code', '') for item in incidents if item.get('incident_code')][:12],
        'hardening_actions': hardening_actions[:8],
        'incidents': incidents[:12],
        'summary': (
            f'当日识别到 {len(incidents)} 个代码能力事件，'
            f'其中高影响 {high_severity_count} 个，重复出现 {recurring_incident_count} 个。'
        ),
    }


def _build_learning_gate_payload(close_payload):
    close_payload = close_payload if isinstance(close_payload, dict) else {}
    return {
        'generated_at': close_payload.get('generated_at', ''),
        'date': close_payload.get('date', ''),
        'node': close_payload.get('node', ''),
        'review_status': close_payload.get('review_status', ''),
        'learning_gate_status': close_payload.get('learning_gate_status', ''),
        'reason_codes': [item.get('code', '') for item in (close_payload.get('issues') or []) if item.get('code')],
        'followup_codes': [item.get('code', '') for item in (close_payload.get('evolution_followups') or []) if item.get('code')],
        'learning_gate_basis': close_payload.get('learning_gate_basis', {}),
        'evolution_absorption': close_payload.get('evolution_absorption', {}),
        'capital_allocation_feedback': close_payload.get('capital_allocation_feedback', {}),
        'engineering_review': close_payload.get('engineering_review', {}),
        'learning_actions_summary': close_payload.get('learning_actions_summary', {}),
    }


def _build_close_node_payload(*, summary, reconcile_summary, daily_evolution_bundle=None):
    pending_summary = reconcile_summary.get('pending') or {}
    performance = summary.get('performance', {}) if isinstance(summary, dict) else {}
    account_status = summary.get('account_status', {}) if isinstance(summary, dict) else {}
    imported_positions = _inum(reconcile_summary.get('imported_positions', 0), 0)
    overlaid_positions = _inum(reconcile_summary.get('overlaid_positions', 0), 0)
    paused_records = _inum(reconcile_summary.get('paused_records', 0), 0)
    stale_count = _inum((pending_summary.get('counts') or {}).get('stale', 0), 0)
    closed_count = _inum(performance.get('closed_count', 0), 0)
    source_stats = (
        daily_evolution_bundle.get('source_stats', {})
        if isinstance(daily_evolution_bundle, dict) else {}
    )
    today_closed_count = _inum(source_stats.get('today_closed_count', 0), 0)

    issues = []
    if not bool(account_status.get('live')):
        issues.append(_review_issue(
            'account_snapshot', 'critical', 'close_summary_not_live',
            '收盘节点未拿到 live 账户快照，当天样本不应直接进入学习层。',
            action='hold_learning_until_live_snapshot',
            blocks_all=True,
        ))
    if stale_count > 0:
        issues.append(_review_issue(
            'pending_orders', 'repair_required', 'stale_pending_orders_at_close',
            f'收盘时仍有 {stale_count} 条 stale pending，执行层状态不够干净。',
            action='hold_learning_until_pending_clean',
        ))
    if imported_positions > 0 or overlaid_positions > 0 or paused_records > 0:
        issues.append(_review_issue(
            'ledger_sync', 'warn', 'close_reconcile_adjustments_present',
            '收盘节点仍需依赖对仓导入/覆盖/暂停动作，学习样本应谨慎放行。',
            action='hold_learning_for_manual_confirmation',
        ))
    if today_closed_count <= 0:
        issues.append(_review_issue(
            'learning_input', 'warn', 'no_closed_samples_today',
            '当日无新增 closed 样本，学习层维持继续观察。',
            action='hold_learning_due_to_no_closed_samples',
        ))

    aggressive_add_summary = _extract_aggressive_add_summary(summary)
    if any(item.get('blocks_all') for item in issues):
        learning_gate_status = 'reject'
    elif issues:
        learning_gate_status = 'hold'
    else:
        learning_gate_status = 'allow'
    trade_date = ''
    if isinstance(daily_evolution_bundle, dict):
        trade_date = str(daily_evolution_bundle.get('trade_date', '')).strip()
    if not trade_date:
        trade_date = datetime.now().strftime('%Y-%m-%d')
    evolution_absorption = _summarize_daily_evolution_bundle(daily_evolution_bundle)
    engineering_review = _build_engineering_review(trade_date=trade_date)
    capital_allocation_feedback = (
        daily_evolution_bundle.get('capital_allocation_feedback', {})
        if isinstance(daily_evolution_bundle, dict) else {}
    )
    intraday_judgment_review = (
        daily_evolution_bundle.get('intraday_judgment_review', {})
        if isinstance(daily_evolution_bundle, dict) else {}
    )
    regime_execution_review = (
        daily_evolution_bundle.get('regime_execution_review', {})
        if isinstance(daily_evolution_bundle, dict) else {}
    )
    evolution_followups = _build_evolution_followups(
        evolution_absorption,
        engineering_review=engineering_review,
    )
    if today_closed_count > 0 and _inum(evolution_absorption.get('learnable_sample_count', 0), 0) <= 0:
        issues.append(_review_issue(
            'learning_input', 'warn', 'no_learnable_samples_after_absorption',
            '收盘样本经吸收分级后没有可直接学习样本，今晚只做观察和归因，不更新参数。',
            action='hold_learning_due_to_zero_learnable_samples',
        ))
    if any(item.get('blocks_all') for item in issues):
        learning_gate_status = 'reject'
    elif issues:
        learning_gate_status = 'hold'
    else:
        learning_gate_status = 'allow'
    close_status = _derive_review_status(issues)
    payload = {
        'generated_at': _now_str(),
        'date': datetime.now().strftime('%Y-%m-%d'),
        'node': 'close_node',
        'review_status': close_status,
        'learning_gate_status': learning_gate_status,
        'issues': issues,
        'summary': {
            'closed_count': closed_count,
            'holding_count': _inum(performance.get('holding_count', 0), 0),
            'win_rate_pct': _fnum(performance.get('win_rate_pct', 0.0), 0.0),
            'avg_return_pct': _fnum(performance.get('avg_return_pct', 0.0), 0.0),
            'realized_pnl': _fnum(performance.get('realized_pnl', 0.0), 0.0),
        },
        'pending_orders': pending_summary,
        'aggressive_add_review': aggressive_add_summary,
        'evolution_absorption': evolution_absorption,
        'capital_allocation_feedback': capital_allocation_feedback,
        'intraday_judgment_review': intraday_judgment_review,
        'regime_execution_review': regime_execution_review,
        'engineering_review': engineering_review,
        'evolution_followups': evolution_followups,
        'learning_actions_summary': {},
        'learning_gate_basis': {
            'closed_count': closed_count,
            'today_closed_count': today_closed_count,
            'learnable_sample_count': _inum(evolution_absorption.get('learnable_sample_count', 0), 0),
            'observe_only_count': _inum(evolution_absorption.get('observe_only_count', 0), 0),
            'blocked_reason_counts': evolution_absorption.get('blocked_reason_counts', {}),
            'capital_allocation_verdict': evolution_absorption.get('capital_allocation_verdict', ''),
            'intraday_judgment_verdict': evolution_absorption.get('intraday_judgment_verdict', ''),
            'intraday_judgment_score': _inum(evolution_absorption.get('intraday_judgment_score', 0), 0),
            'regime_execution_verdict': evolution_absorption.get('regime_execution_verdict', ''),
            'regime_execution_score': _inum(evolution_absorption.get('regime_execution_score', 0), 0),
            'regime_execution_positive_sample': bool(evolution_absorption.get('regime_execution_positive_sample', False)),
            'regime_execution_label': evolution_absorption.get('regime_execution_label', ''),
            'missed_opportunity_count': _inum(evolution_absorption.get('missed_opportunity_count', 0), 0),
            'missed_opportunity_positive_count': _inum(evolution_absorption.get('missed_opportunity_positive_count', 0), 0),
            'missed_opportunity_pending_count': _inum(evolution_absorption.get('missed_opportunity_pending_count', 0), 0),
            'missed_opportunity_avg_return_pct': _fnum(evolution_absorption.get('missed_opportunity_avg_return_pct', 0.0), 0.0),
            'regime_positive_sample_count': 0,
            'regime_bias_stage': '',
            'engineering_incident_count': _inum(engineering_review.get('incident_count', 0), 0),
            'engineering_verdict': str(engineering_review.get('verdict', '')).strip(),
        },
        'full_reconcile': reconcile_summary,
        'learning_notes': summary.get('learning_notes', []) if isinstance(summary, dict) else [],
        'notes': [
            '收盘节点负责全天闭环核对、问题归因与学习放行。',
            '只有通过收盘节点复核的样本，才应该继续进入学习层候选。',
            '每日进化吸收摘要已并入收盘节点，用于识别可学样本、执行折损和利润扩张机会。',
            '午盘判断会在尾盘做后验验证，并以 judgment calibration 形式进入进化闭环。',
            '代码能力事件会同步进入 engineering review，要求沉淀为根因、约束与回归测试。',
        ],
    }
    return payload


def calc_buy_quantity(entry_price, amount=BUY_AMOUNT_DEFAULT):
    """计算买入数量（整百股），amount=目标买入金额（元）"""
    if entry_price <= 0 or amount <= 0:
        return 0
    qty = int(amount / entry_price / 100) * 100
    return qty if qty >= 100 else 0  # 买不起100股就返回0


# ─── TDX 连接（信号衰减检测用） ───

TDX_HOSTS = [
    ("218.75.126.9", 7709),
    ("60.191.117.167", 7709),
    ("39.105.251.234", 7709),
    ("119.147.212.83", 7709),
]

BUY_WINDOW = ((14, 50), (14, 57))
SELL_CUTOFF_TIME = (14, 49)
GENERAL_SELL_WINDOW = ((9, 35), SELL_CUTOFF_TIME)
SMART_SELL_CHECKPOINTS = [
    (9, 45),
    (10, 15),
    (10, 45),
    (11, 15),
    (13, 15),
    (13, 45),
    (14, 15),
    (14, 45),
]
SMART_SELL_CHECKPOINT_GRACE_MINUTES = 5
ADD_POSITION_CHECKPOINTS = [
    (9, 36),
    (10, 28),
    (13, 28),
]
ADD_POSITION_CHECKPOINT_GRACE_MINUTES = 4
ADD_POSITION_WINDOW_SETTINGS = {
    '09:36': {
        'label': 'opening_confirm',
        'score_min': 4.5,
        'aggressive_score_min': 6.0,
        'reserve_cash_ratio': 0.35,
        'non_aggressive_max_items': 2,
    },
    '10:28': {
        'label': 'trend_promote',
        'score_min': 5.0,
        'aggressive_score_min': 6.5,
        'reserve_cash_ratio': 0.28,
        'non_aggressive_max_items': 2,
    },
    '13:28': {
        'label': 'pm_reaccel',
        'score_min': 5.0,
        'aggressive_score_min': 6.5,
        'reserve_cash_ratio': 0.20,
        'non_aggressive_max_items': 1,
    },
}


def _time_in_range(now_value, start_hm, end_hm):
    start = start_hm[0] * 60 + start_hm[1]
    end = end_hm[0] * 60 + end_hm[1]
    current = now_value.hour * 60 + now_value.minute
    return start <= current <= end


def _is_trading_session(now_value):
    return (
        _time_in_range(now_value, (9, 30), (11, 30))
        or _time_in_range(now_value, (13, 0), (15, 0))
    )


def _in_checkpoint_grace(now_value, checkpoints, *, grace_minutes=3):
    current = now_value.hour * 60 + now_value.minute + now_value.second / 60.0
    for hour, minute in checkpoints:
        checkpoint = hour * 60 + minute
        if checkpoint <= current <= checkpoint + grace_minutes:
            return True
    return False


def _resolve_add_position_window(now_value=None):
    now_value = now_value or _market_now()
    for hour, minute in ADD_POSITION_CHECKPOINTS:
        current = now_value.hour * 60 + now_value.minute + now_value.second / 60.0
        checkpoint = hour * 60 + minute
        if checkpoint <= current <= checkpoint + ADD_POSITION_CHECKPOINT_GRACE_MINUTES:
            slot = f'{hour:02d}:{minute:02d}'
            payload = dict(ADD_POSITION_WINDOW_SETTINGS.get(slot, {}))
            payload.setdefault('label', slot)
            payload['slot'] = slot
            return payload
    return {}


def _current_add_position_window_tag(now_value=None):
    payload = _resolve_add_position_window(now_value)
    return str(payload.get('slot', '')).strip()


def ensure_trade_window(action, *, dry_run=False):
    now = _market_now()
    if dry_run:
        return True
    current = now.strftime('%H:%M')
    sell_cutoff = f'{SELL_CUTOFF_TIME[0]:02d}:{SELL_CUTOFF_TIME[1]:02d}'
    if action == 'buy':
        if not _time_in_range(now, BUY_WINDOW[0], BUY_WINDOW[1]):
            print(f"[WARN] 当前 {current} 不在买入窗口 14:50-14:57，已跳过自动买入。")
            return False
    elif action == 'smart_sell':
        if not _time_in_range(now, GENERAL_SELL_WINDOW[0], GENERAL_SELL_WINDOW[1]):
            print(f"[WARN] 当前 {current} 已超过卖单截止时间 {sell_cutoff}，已跳过智能卖出。")
            return False
        if not _is_trading_session(now) or not _in_checkpoint_grace(
            now,
            SMART_SELL_CHECKPOINTS,
            grace_minutes=SMART_SELL_CHECKPOINT_GRACE_MINUTES,
        ):
            checkpoints = ' / '.join(f'{h:02d}:{m:02d}' for h, m in SMART_SELL_CHECKPOINTS)
            print(
                f"[WARN] 当前 {current} 不在智能卖出巡检点 {checkpoints} "
                f"(容差 {SMART_SELL_CHECKPOINT_GRACE_MINUTES} 分钟) 内，已跳过智能卖出。"
            )
            return False
    elif action == 'add_position':
        if not _is_trading_session(now) or not _time_in_range(now, GENERAL_SELL_WINDOW[0], GENERAL_SELL_WINDOW[1]):
            print(f"[WARN] 当前 {current} 不在卖单/加仓窗口 09:35-{sell_cutoff}，已跳过 {action}。")
            return False
        if not _resolve_add_position_window(now):
            checkpoints = ' / '.join(f'{h:02d}:{m:02d}' for h, m in ADD_POSITION_CHECKPOINTS)
            print(
                f"[WARN] 当前 {current} 不在加仓确认点 {checkpoints} "
                f"(容差 {ADD_POSITION_CHECKPOINT_GRACE_MINUTES} 分钟) 内，已跳过加仓。"
            )
            return False
    elif action == 'sell':
        if not _is_trading_session(now) or not _time_in_range(now, GENERAL_SELL_WINDOW[0], GENERAL_SELL_WINDOW[1]):
            print(f"[WARN] 当前 {current} 不在卖单/加仓窗口 09:35-{sell_cutoff}，已跳过 {action}。")
            return False
    return True


def connect_tdx():
    """连接TDX行情服务器"""
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
    return None


def market_from_code(code):
    """从股票代码推断市场: 6xx=沪(1), 其他=深(0)"""
    code = str(code).strip()
    if code.startswith(('6', '9')):
        return 1
    return 0


# ─── 信号衰减检测 ───

def evaluate_signal_decay(api, code, entry_price, buy_mode, *, profit_pct=0.0):
    """
    评估持仓股的信号是否衰减，返回 (should_sell, reason, decay_score)

    decay_score: 0=信号完好, 越高越应卖
    触发条件（任一命中即建议卖出）:
      1. 尾盘拉高出货: bz_direction 从杀跌(负)变拉高(正>+0.5%) → 主力出逃
      2. 放量滞涨: 今日量比>1.3 但涨幅<0.5% → 资金进场不涨=出货
      3. 冲高回落上影线: 日内最高>收盘*1.01 且 收盘<开盘 → 多头力竭
      4. 周线slope走平/转跌: weekly_slope 从正变<=0 → 趋势终结
      5. 有利润+信号弱化: 浮盈>2% + decay_score>=2 → 落袋为安
      6. 高盈利仓位对转弱更敏感，优先兑现

    T+5是兜底框架，信号衰减随时走人！
    """
    market = market_from_code(code)
    decay_score = 0
    reasons = []
    should_sell = False

    # ── 1. 日线数据：冲高回落、量价背离 ──
    daily_bars = api.get_security_bars(9, market, code, 0, 10)
    if daily_bars:
        try:
            ddf = api.to_df(daily_bars)
            if ddf is not None and len(ddf) >= 2:
                ddf = ddf.sort_values('datetime').reset_index(drop=True)
                last = ddf.iloc[-1]
                prev = ddf.iloc[-2]

                # 冲高回落上影线: 最高>收盘*1.01 且 收盘<开盘
                high = last['high']
                close = last['close']
                open_ = last['open']
                if high > close * 1.01 and close < open_:
                    upper_shadow = (high - close) / close * 100
                    if upper_shadow > 1.0:  # 上影线>1%
                        decay_score += 2
                        reasons.append(f"冲高回落上影线{upper_shadow:.1f}%")

                # 放量滞涨: 量比>1.3 但涨幅<0.5%
                if prev['amount'] > 0:
                    amt_ratio = last['amount'] / prev['amount']
                    chg_pct = (close - prev['close']) / prev['close'] * 100
                    if amt_ratio > 1.3 and chg_pct < 0.5:
                        decay_score += 2
                        reasons.append(f"放量滞涨(量比{amt_ratio:.1f}涨幅{chg_pct:+.1f}%)")

                # 大阴线: 跌幅>2%
                chg_from_open = (close - open_) / open_ * 100
                if chg_from_open < -2.0:
                    decay_score += 3
                    reasons.append(f"大阴线{chg_from_open:+.1f}%")

                # 连跌2日
                if len(ddf) >= 3:
                    c0 = ddf.iloc[-1]['close']
                    c1 = ddf.iloc[-2]['close']
                    c2 = ddf.iloc[-3]['close']
                    if c0 < c1 < c2:
                        decay_score += 1
                        reasons.append("连跌2日")
        except Exception:
            pass

    # ── 2. 5分钟数据：尾盘拉高出货 ──
    min5_bars = api.get_security_bars(0, market, code, 0, 50)
    if min5_bars:
        try:
            mdf = api.to_df(min5_bars)
            if mdf is not None and len(mdf) >= 5:
                mdf = mdf.sort_values('datetime').reset_index(drop=True)
                mdf['hour'] = mdf['datetime'].dt.hour
                mdf['minute'] = mdf['datetime'].dt.minute

                # 尾盘14:30-15:00区间
                bz = mdf[(mdf['hour'] == 14) & (mdf['minute'] >= 30)]
                if len(bz) >= 3:
                    bz_open = bz.iloc[0]['open']
                    bz_close = bz.iloc[-1]['close']
                    bz_dir = (bz_close - bz_open) / bz_open * 100 if bz_open > 0 else 0

                    # 如果买入时是杀跌(bz<0)，现在变成拉高出货(bz>+0.5%) → 主力出逃
                    if 'kill' in buy_mode and bz_dir > 0.5:
                        decay_score += 2
                        reasons.append(f"尾盘杀跌→拉高出货(bz={bz_dir:+.2f}%)")

                    # 尾盘无量阴跌
                    bz_avg_vol = bz['vol'].mean()
                    total_avg_vol = mdf['vol'].mean()
                    if bz_dir < -0.2 and bz_avg_vol < total_avg_vol * 0.8:
                        decay_score += 1
                        reasons.append(f"尾盘无量阴跌(bz={bz_dir:+.2f}%)")
        except Exception:
            pass

    # ── 3. 周线趋势 ──
    weekly_bars = api.get_security_bars(5, market, code, 0, 30)
    if weekly_bars:
        try:
            wdf = api.to_df(weekly_bars)
            if wdf is not None and len(wdf) >= 25:
                wdf = wdf.sort_values('datetime').reset_index(drop=True)
                wdf['wma5'] = wdf['close'].rolling(5).mean()
                wdf['wma20'] = wdf['close'].rolling(20).mean()

                last_w = wdf.iloc[-1]
                if pd.notna(last_w.get('wma5')) and pd.notna(last_w.get('wma20')):
                    w20 = last_w['wma20']
                    if w20 > 0:
                        ws = (last_w['wma5'] - w20) / w20 * 100
                        if ws <= 0 and 'trend' in buy_mode:
                            decay_score += 3
                            reasons.append(f"周线slope转负({ws:+.1f}%)趋势终结")
                        elif ws <= 1.0 and 'trend' in buy_mode:
                            decay_score += 1
                            reasons.append(f"周线slope走平({ws:+.1f}%)")
        except Exception:
            pass

    # ── 决策：是否应该卖出 ──
    if decay_score >= 3:
        should_sell = True
    elif decay_score >= 2:
        # 有利润+信号弱化 → 落袋为安
        cur_vs_entry = profit_pct
        if entry_price > 0:
            # 尝试从日线获取当前价
            if daily_bars:
                try:
                    ddf = api.to_df(daily_bars)
                    if ddf is not None and len(ddf) >= 1:
                        cur_price = ddf.sort_values('datetime').iloc[-1]['close']
                        cur_vs_entry = (cur_price - entry_price) / entry_price * 100
                        if cur_vs_entry > 2.0:  # 浮盈>2%+信号弱化
                            should_sell = True
                            reasons.append(f"浮盈{cur_vs_entry:+.1f}%落袋为安")
                except Exception:
                    pass

    if not should_sell and profit_pct >= HIGH_PROFIT_TAKE_PROFIT_PCT and decay_score >= 1:
        should_sell = True
        reasons.append(f"高盈利{profit_pct:+.1f}%转弱优先兑现")
    elif not should_sell and profit_pct >= MEDIUM_PROFIT_TAKE_PROFIT_PCT and decay_score >= 2:
        should_sell = True
        reasons.append(f"中高盈利{profit_pct:+.1f}%且信号转弱")

    reason_str = " | ".join(reasons) if reasons else "信号完好"
    return should_sell, reason_str, decay_score


def _market_from_code(code):
    code = str(code).zfill(6)
    return 1 if code.startswith('6') else 0


def _build_big_meat_identity_profile(record=None, *, profit_pct=0.0):
    profile = {
        'score': 0,
        'notes': [],
    }
    if profit_pct >= ADD_POSITION_BIG_MEAT_EARLY_PROFIT_PCT:
        profile['score'] += 1
        profile['notes'].append(f"浮盈起势{profit_pct:+.1f}%")
    if profit_pct >= ADD_POSITION_BIG_MEAT_PROFIT_PCT:
        profile['score'] += 1
        profile['notes'].append(f"浮盈{profit_pct:+.1f}%")
    record = record if isinstance(record, dict) else {}
    tier = _inum(record.get('tier', 0), 0)
    mode = str(record.get('mode', '')).strip()
    build_note = str(record.get('build_note', '')).strip()
    if tier == 1:
        profile['score'] += 1
        profile['notes'].append('T1持仓')
    elif tier == 2 or BIG_MEAT_BUY_POOL_CANDIDATE_NOTE in build_note:
        profile['score'] += 1
        profile['notes'].append('T2候选底仓')
    elif BIG_MEAT_BUY_POOL_OBSERVE_NOTE in build_note:
        profile['notes'].append('T3观察试错')
    if mode == 'V9_full':
        profile['score'] += 2
        profile['notes'].append('V9_full')
    elif mode in {'kill_only', 'trend_only', 'near_kill+weekly+MA20'}:
        profile['score'] += 1
        profile['notes'].append(f'{mode}种子')
    if '超级大行情' in build_note:
        profile['score'] += 1
        profile['notes'].append('超级大行情首建')
    return profile


def _build_big_meat_add_profile(api, code, *, record=None, profit_pct=0.0, decision_row=None, window_tag=''):
    profile = {
        'eligible': False,
        'reason': '',
        'target_multiplier': 1.0,
        'score': 0,
        'aggressive_score': 0.0,
        'window_tag': str(window_tag or '').strip(),
        'today_chg_pct': 0.0,
        'strong_trend': False,
        'near_day_high': False,
        'intraday_anchor_hold': False,
        'rebreakout': False,
        'noon_rebound': False,
        'min5_rising': False,
        'weekly_up': False,
        'flow_score': 0.0,
        'sector_score': 0.0,
        'stock_score': 0.0,
        'total_score': 0.0,
    }
    identity = _build_big_meat_identity_profile(record, profit_pct=profit_pct)
    profile['score'] = _inum(identity.get('score', 0), 0)
    identity_notes = list(identity.get('notes', []) or [])
    row = decision_row if isinstance(decision_row, dict) else {}
    components = row.get('components', {}) if isinstance(row.get('components', {}), dict) else {}
    flow_score = _fnum(components.get('flow', 0.0), 0.0)
    sector_score = _fnum(components.get('sector', 0.0), 0.0)
    stock_score = _fnum(components.get('stock', 0.0), 0.0)
    total_score = _fnum(row.get('score', 0.0), 0.0)
    if flow_score >= ADD_POSITION_BIG_MEAT_FLOW_SCORE:
        profile['score'] += 1
        identity_notes.append(f'flow{flow_score:.1f}')
    if sector_score >= ADD_POSITION_BIG_MEAT_SECTOR_SCORE:
        profile['score'] += 1
        identity_notes.append(f'sector{sector_score:.1f}')
    if stock_score >= ADD_POSITION_BIG_MEAT_STOCK_SCORE:
        profile['score'] += 1
        identity_notes.append(f'stock{stock_score:.1f}')
    if total_score >= ADD_POSITION_BIG_MEAT_TOTAL_SCORE:
        profile['score'] += 1
        identity_notes.append(f'total{total_score:.1f}')
    if api is None:
        return profile
    try:
        market = _market_from_code(code)
        daily_bars = api.get_security_bars(9, market, str(code).zfill(6), 0, 25) or []
        min5_bars = api.get_security_bars(0, market, str(code).zfill(6), 0, 24) or []
        weekly_bars = api.get_security_bars(5, market, str(code).zfill(6), 0, 8) or []
        if not daily_bars or not min5_bars or not weekly_bars:
            return profile
        ddf = api.to_df(daily_bars)
        mdf = api.to_df(min5_bars)
        wdf = api.to_df(weekly_bars)
        if ddf is None or mdf is None or wdf is None:
            return profile
        ddf = ddf.sort_values('datetime').reset_index(drop=True)
        mdf = mdf.sort_values('datetime').reset_index(drop=True)
        wdf = wdf.sort_values('datetime').reset_index(drop=True)
        if len(ddf) < 10 or len(mdf) < 6 or len(wdf) < 2:
            return profile

        ddf['ma5'] = ddf['close'].rolling(5).mean()
        ddf['ma10'] = ddf['close'].rolling(10).mean()
        ddf['ma20'] = ddf['close'].rolling(20).mean()
        last = ddf.iloc[-1]
        prev = ddf.iloc[-2]
        intraday_anchor = _fnum(last.get('open', 0.0), 0.0)
        latest_close = _fnum(last.get('close', 0.0), 0.0)
        day_high = _fnum(last.get('high', 0.0), 0.0)
        ma5 = _fnum(last.get('ma5', 0.0), 0.0)
        ma10 = _fnum(last.get('ma10', 0.0), 0.0)
        ma20 = _fnum(last.get('ma20', 0.0), 0.0)
        prev_close = _fnum(prev.get('close', 0.0), 0.0)
        today_chg_pct = ((latest_close / prev_close - 1.0) * 100.0) if prev_close > 0 else 0.0
        weekly_up = _fnum(wdf.iloc[-1].get('close', 0.0), 0.0) >= _fnum(wdf.iloc[-2].get('close', 0.0), 0.0)
        near_day_high = latest_close >= day_high * ADD_POSITION_BIG_MEAT_NEAR_HIGH_RATIO if day_high > 0 else False

        last4 = mdf.tail(4).reset_index(drop=True)
        last_close = _fnum(last4.iloc[-1].get('close', 0.0), 0.0)
        first_close = _fnum(last4.iloc[0].get('close', 0.0), 0.0)
        max_recent_close = _fnum(last4['close'].max(), 0.0)
        noon_rebound = last_close >= first_close if first_close > 0 else False
        min5_rising = last_close >= _fnum(last4.iloc[-2].get('close', 0.0), 0.0)
        intraday_anchor_hold = latest_close >= intraday_anchor * ADD_POSITION_BIG_MEAT_INTRADAY_ANCHOR_RATIO if intraday_anchor > 0 else False
        rebreakout = last_close >= max_recent_close * ADD_POSITION_BIG_MEAT_REBREAKOUT_RATIO if max_recent_close > 0 else False

        strong_trend = (
            latest_close > 0
            and latest_close >= ma5 > 0
            and latest_close >= ma10 > 0
            and latest_close >= ma20 > 0
            and weekly_up
        )
        profile['today_chg_pct'] = round(today_chg_pct, 2)
        profile['strong_trend'] = bool(strong_trend)
        profile['near_day_high'] = bool(near_day_high)
        profile['intraday_anchor_hold'] = bool(intraday_anchor_hold)
        profile['rebreakout'] = bool(rebreakout)
        profile['noon_rebound'] = bool(noon_rebound)
        profile['min5_rising'] = bool(min5_rising)
        profile['weekly_up'] = bool(weekly_up)
        profile['flow_score'] = round(flow_score, 2)
        profile['sector_score'] = round(sector_score, 2)
        profile['stock_score'] = round(stock_score, 2)
        profile['total_score'] = round(total_score, 2)
        if strong_trend:
            profile['score'] += 1
        if near_day_high:
            profile['score'] += 1
        if today_chg_pct >= ADD_POSITION_BIG_MEAT_DAY_CHG_PCT:
            profile['score'] += 1
        if noon_rebound or min5_rising:
            profile['score'] += 1
        if intraday_anchor_hold:
            profile['score'] += 1
        if rebreakout:
            profile['score'] += 1
        aggressive_score = 0.0
        if strong_trend:
            aggressive_score += 1.2
        if near_day_high:
            aggressive_score += 1.0
        if today_chg_pct >= ADD_POSITION_BIG_MEAT_DAY_CHG_PCT:
            aggressive_score += 1.0
        if noon_rebound or min5_rising:
            aggressive_score += 0.8
        if intraday_anchor_hold:
            aggressive_score += 0.8
        if rebreakout:
            aggressive_score += 1.0
        if flow_score >= ADD_POSITION_BIG_MEAT_FLOW_SCORE:
            aggressive_score += 0.7
        if sector_score >= ADD_POSITION_BIG_MEAT_SECTOR_SCORE:
            aggressive_score += 0.7
        if total_score >= ADD_POSITION_BIG_MEAT_TOTAL_SCORE:
            aggressive_score += 0.5
        profile['aggressive_score'] = round(aggressive_score, 2)
        if strong_trend and near_day_high and intraday_anchor_hold and (noon_rebound or min5_rising or rebreakout) and profile['score'] >= ADD_POSITION_BIG_MEAT_SCORE_THRESHOLD:
            profile['eligible'] = True
            multiplier = ADD_POSITION_BIG_MEAT_TARGET_MULTIPLIER
            if profile['aggressive_score'] >= 7.5:
                multiplier = min(multiplier + 0.15, MODE_CAPITAL_ADD_POSITION_TARGET_MAX)
            profile['target_multiplier'] = multiplier
            notes = identity_notes + [f"日涨幅{today_chg_pct:+.1f}%", '贴近日高', '周线向上']
            if intraday_anchor_hold:
                notes.append('站稳日内锚')
            if rebreakout:
                notes.append('5分钟再突破')
            if profile['window_tag']:
                notes.append(profile['window_tag'])
            profile['reason'] = "/".join(notes)
    except Exception:
        return profile
    return profile


def do_buy(dry_run=False):
    """按V10信号分批建仓（围绕大肉候选概率组织 T2/T3）

    仓位管理原则：
    - 不满仓！留子弹给T+1做反T或加仓
    - T1=强确认核心，T2=大肉候选培养池，T3=观察试错池
    - T2/T3 不再直接沿用扫描 tier，而是先做大肉候选种子评分再分池
    - T+1确认后可加仓到满仓目标
    - 超级大行情（V9_full+多模式共振）才允许首次100%建仓
    """
    #region debug-point B:buywatch-1450-fail-entry
    _debug_emit_event(
        'A',
        'v10_moni_trader.py:do_buy',
        '[DEBUG] buy flow entered',
        {
            'argv': sys.argv,
            'cwd': os.getcwd(),
            'dry_run': bool(dry_run),
        },
    )
    #endregion
    if not ensure_trade_window('buy', dry_run=dry_run):
        return EXIT_WINDOW_SKIPPED
    if not dry_run:
        ready, decision_message, _ = wait_for_today_decision_ready()
        #region debug-point C:buywatch-1450-fail-decision
        _debug_emit_event(
            'A',
            'v10_moni_trader.py:do_buy',
            '[DEBUG] decision readiness checked',
            {
                'ready': bool(ready),
                'decision_message': str(decision_message or ''),
            },
        )
        #endregion
        if not ready:
            print(f"[ERROR] 买入前 decision 未就绪: {decision_message}")
            return EXIT_CONFIG_ERROR
    scan_ctx = load_scan_context()
    if not dry_run:
        ok, message, ctx = validate_scan_freshness()
        #region debug-point D:buywatch-1450-fail-scan
        _debug_emit_event(
            'A',
            'v10_moni_trader.py:do_buy',
            '[DEBUG] scan freshness checked',
            {
                'ok': bool(ok),
                'message': str(message or ''),
                'csv_path': str((ctx or {}).get('csv_path', '')),
                'age_minutes': (ctx or {}).get('age_minutes', None),
            },
        )
        #endregion
        if not ok:
            print(f"[ERROR] 买入前扫描校验失败: {message}")
            return EXIT_STALE_SCAN
        scan_ctx = ctx
        print(
            f" 使用扫描快照: {ctx['csv_path']} "
            f"(时间 {ctx['run_time'].strftime('%Y-%m-%d %H:%M:%S')}, "
            f"{ctx.get('age_minutes', 0):.1f} 分钟前)"
        )
    signals = load_scan_signals(scan_ctx['csv_path'])
    ranking = rank_signals(signals, scan_context=scan_ctx)
    signals = ranking['ranked_signals']
    model_market = ranking['context']['market']
    model_state_summary = get_evolving_model_summary(ranking['context']['state'])
    min_model_score = ranking['min_trade_score']
    balance = get_balance()

    if not balance:
        print("[ERROR] 无法获取账户资金")
        return EXIT_RUNTIME_ERROR

    total_signals = sum(len(signals[t]) for t in signals)
    if total_signals == 0:
        print(" 今日无信号，不操作")
        return EXIT_NO_SIGNAL

    total_assets = balance['total_assets']
    records = load_track_record()
    recent_trade_memory = _build_recent_trade_memory(records)
    mode_capital_profile = _build_mode_capital_profile(records)
    learning_actions = _load_learning_actions()
    blocked_t3_modes = _blocked_t3_modes_from_learning_actions(learning_actions)
    learning_preflight = _as_report_only_learning_preflight(_resolve_learning_preflight_guard())
    print(
        f" 收盘学习闸门(汇报): {learning_preflight['status']} "
        f"| report_buy={'pass' if learning_preflight.get('reported_allow_buy') else 'block'} "
        f"| report_aggressive_add={'pass' if learning_preflight.get('reported_allow_aggressive_add') else 'hold'} "
        f"| effective_buy=pass"
    )
    for note in learning_preflight.get('notes', []):
        print(f"  - {note}")
    if not learning_preflight.get('reported_allow_buy', False):
        #region debug-point E:buywatch-1450-fail-learning-gate
        _debug_emit_event(
            'B',
            'v10_moni_trader.py:do_buy',
            '[DEBUG] learning gate downgraded to report-only for buy',
            {
                'reason': str(learning_preflight.get('reason', 'learning_gate_block')),
                'reported_allow_buy': bool(learning_preflight.get('reported_allow_buy', False)),
                'notes': list(learning_preflight.get('notes', []) or []),
            },
        )
        #endregion
        print(f" 收盘学习闸门报告为未放行，但当前配置不阻断新开仓: {learning_preflight.get('reason', 'learning_gate_block')}")
    market_regime = _normalize_market_regime(model_market.get('regime', ''))
    pm_buy_guard = _build_pm_buy_guardrails()
    print(
        f" 午盘闸门: {pm_buy_guard['pm_gate_status']} "
        f"| 风险={pm_buy_guard['risk_bias'] or 'unknown'} "
        f"| 反弹={pm_buy_guard['rebound_bias'] or 'unknown'} "
        f"| 置信={pm_buy_guard['confidence']:.2f}"
    )
    for note in pm_buy_guard.get('notes', []):
        print(f"  - {note}")
    if not pm_buy_guard.get('allow_buy', True):
        #region debug-point F:buywatch-1450-fail-pm-gate
        _debug_emit_event(
            'B',
            'v10_moni_trader.py:do_buy',
            '[DEBUG] pm guard blocked buy',
            {
                'pm_gate_status': str(pm_buy_guard.get('pm_gate_status', '')),
                'risk_bias': str(pm_buy_guard.get('risk_bias', '')),
                'rebound_bias': str(pm_buy_guard.get('rebound_bias', '')),
                'confidence': _fnum(pm_buy_guard.get('confidence', 0.0), 0.0),
                'notes': list(pm_buy_guard.get('notes', []) or []),
            },
        )
        #endregion
        print(" 午盘/尾盘仓位闸门未放行，尾盘取消新开仓。")
        return EXIT_NO_ACTION

    # 每个 tier 的每只股票满仓目标金额 = 总资产 × position_pct%
    # 首次建仓金额 = 满仓目标 × initial_build_pct%
    tier_per_stock_amount = {}
    tier_initial_amount = {}
    for tier, cfg in TIER_CONFIG.items():
        tier_per_stock_amount[tier] = total_assets * cfg['position_pct'] / 100
        tier_initial_amount[tier] = tier_per_stock_amount[tier] * cfg['initial_build_pct'] / 100

    # 检测是否超级大行情（V9_full信号存在 → T1满仓首建）
    has_v9_full = any(
        s.get('mode', '') == 'V9_full'
        for s in signals.get(1, [])
    ) and bool(pm_buy_guard.get('allow_full_v9_build'))

    # 构建买入候选：T2/T3 不再直接沿用扫描 tier，而是围绕大肉候选概率分池
    candidate_buckets = {1: [], 2: [], 3: []}
    buy_list = []
    skipped_low_model = []
    skipped_guard_modes = []
    skipped_partial_rollback = []
    limited_mode_hits = []
    skipped_recent_reentry = []
    skipped_learning_guard = []
    for original_tier in [1, 2, 3]:
        for s in signals[original_tier]:
            mode = str(s.get('mode', '')).strip()
            code = str(s.get('code', '')).zfill(6)
            if mode in set(pm_buy_guard.get('blocked_modes', [])):
                skipped_guard_modes.append(f"{code}[{mode}]")
                continue
            rollback_blocked, rollback_reason = _is_partial_rollback_blocked_buy(
                tier=original_tier,
                mode=mode,
                pm_buy_guard=pm_buy_guard,
            )
            if rollback_blocked:
                skipped_partial_rollback.append(f"{code}[{mode}:{rollback_reason}]")
                continue
            recent_adjustment = _resolve_recent_trade_selection_adjustment(code, recent_trade_memory)
            if recent_adjustment.get('block_reentry'):
                skipped_recent_reentry.append(
                    f"{code}[{mode}:{'/'.join(recent_adjustment.get('reasons', [])[:2])}]"
                )
                continue
            learning_action = _resolve_learning_trade_action(code, mode, learning_actions)
            if learning_action.get('block_new_position') and not (original_tier == 1 or mode == 'V9_full'):
                skipped_learning_guard.append(f"{code}[{mode}:{learning_action.get('reason', 'learning_block')}]")
                continue
            model_score = _fnum(s.get('model_score', 0.0), 0.0)
            if model_score < min_model_score:
                skipped_low_model.append(f"{s.get('code', '')}:{model_score:.1f}")
                continue
            entry_price = _fnum(s.get('entry_price', 0), 0.0)
            if entry_price <= 0:
                continue

            seed_profile = _build_big_meat_buy_seed_profile(
                s,
                original_tier=original_tier,
                recent_adjustment=recent_adjustment,
            )
            seed_profile = _apply_learning_action_to_seed_profile(
                seed_profile,
                learning_action,
                preserve_t1=(original_tier == 1 or mode == 'V9_full'),
            )
            effective_tier = _inum(seed_profile.get('effective_tier', original_tier), original_tier)
            if effective_tier == 3 and mode in blocked_t3_modes and not (original_tier == 1 or mode == 'V9_full'):
                skipped_learning_guard.append(f"{code}[{mode}:blocked_t3_mode]")
                continue
            tier_cfg = TIER_CONFIG.get(effective_tier, TIER_CONFIG[original_tier])
            base_target_amount = total_assets * tier_cfg['position_pct'] / 100
            base_initial_amount = base_target_amount * tier_cfg['initial_build_pct'] / 100
            capital_plan = _resolve_mode_capital_plan(
                s.get('mode', ''),
                base_target_amount=base_target_amount,
                base_initial_amount=base_initial_amount,
                mode_capital_profile=mode_capital_profile,
                market_regime=market_regime,
            )
            capital_note = str(capital_plan.get('note', '')).strip()
            ranking_bonus = _fnum(capital_plan.get('ranking_bonus', 0.0), 0.0)
            freshness_adjustment = _fnum(recent_adjustment.get('net_adjustment', 0.0), 0.0)
            seed_bonus = _fnum(seed_profile.get('ranking_bonus', 0.0), 0.0)
            target_amount_this = _fnum(capital_plan.get('target_amount', base_target_amount), base_target_amount)
            target_amount_this = round(target_amount_this * _fnum(seed_profile.get('target_amount_ratio', 1.0), 1.0), 2)

            # 当前部分回退版本下，V9_full 即使强确认也仅按普通首建执行。
            if effective_tier == 1 and has_v9_full and mode == 'V9_full':
                amount_per_stock_this = target_amount_this
                build_note = "超级大行情满仓首建"
            else:
                amount_per_stock_this = round(
                    min(
                        target_amount_this,
                        _fnum(capital_plan.get('initial_amount', base_initial_amount), base_initial_amount)
                        * _fnum(seed_profile.get('initial_amount_ratio', 1.0), 1.0),
                    ),
                    2,
                )
                first_build_pct = (amount_per_stock_this / target_amount_this * 100.0) if target_amount_this > 0 else _fnum(tier_cfg.get('initial_build_pct', 0), 0.0)
                build_note = f"首建{first_build_pct:.0f}%"
            global_amount_ratio = _fnum(pm_buy_guard.get('global_amount_ratio', 1.0), 1.0)
            if global_amount_ratio < 0.999:
                amount_per_stock_this = round(amount_per_stock_this * global_amount_ratio, 2)
                build_note = f"{build_note}; 午盘闸门{global_amount_ratio:.2f}x"
            if mode in set(pm_buy_guard.get('limited_modes', [])):
                mode_amount_ratio = _fnum(pm_buy_guard.get('mode_amount_ratio', 1.0), 1.0)
                amount_per_stock_this = round(amount_per_stock_this * mode_amount_ratio, 2)
                build_note = f"{build_note}; 模式收紧{mode_amount_ratio:.2f}x"
                limited_mode_hits.append(f"{code}[{mode}]")
            pool_note = str(seed_profile.get('pool_note', '')).strip()
            pool_label = str(seed_profile.get('pool_label', '')).strip()
            seed_reason = str(seed_profile.get('reason', '')).strip()
            if pool_label:
                build_note = f"{build_note}; {pool_label}{f'({seed_reason})' if seed_reason else ''}"
            if pool_note:
                build_note = f"{build_note}; {pool_note}"
            if capital_note:
                build_note = f"{build_note}; {capital_note}"
            if recent_adjustment.get('reasons'):
                build_note = f"{build_note}; {'/'.join(recent_adjustment.get('reasons', [])[:2])}"
            if learning_action.get('reason'):
                build_note = f"{build_note}; 学习层({learning_action['reason']})"
            amount_per_stock_this = min(amount_per_stock_this, target_amount_this)
            if amount_per_stock_this <= 0 or target_amount_this <= 0:
                continue
            candidate_buckets[effective_tier].append({
                'code': s['code'],
                'name': s['name'],
                'tier': effective_tier,
                'source_tier': original_tier,
                'entry_price': entry_price,
                'planned_build_amount': amount_per_stock_this,
                'mode': mode,
                'build_note': build_note,
                'target_amount': target_amount_this,
                'model_score': model_score,
                'ranking_score': round(model_score + ranking_bonus + freshness_adjustment + seed_bonus, 4),
                'model_industry': s.get('model_industry', 'unknown'),
                'model_market_score': _fnum(s.get('model_market_score', 0.0), 0.0),
                'model_sector_score': _fnum(s.get('model_sector_score', 0.0), 0.0),
                'model_stock_score': _fnum(s.get('model_stock_score', 0.0), 0.0),
                'model_flow_score': _fnum(s.get('model_flow_score', 0.0), 0.0),
                'capital_bias_note': capital_note,
                'big_meat_seed_score': round(_fnum(seed_profile.get('seed_score', 0.0), 0.0), 2),
                'big_meat_pool_label': pool_label,
                'big_meat_priority_rank': _inum(seed_profile.get('priority_rank', effective_tier), effective_tier),
            })
    for tier in [1, 2, 3]:
        cfg = TIER_CONFIG[tier]
        ranked_candidates = sorted(
            candidate_buckets[tier],
            key=lambda item: (
                _fnum(item.get('big_meat_seed_score', 0.0), 0.0),
                _fnum(item.get('ranking_score', 0.0), 0.0),
            ),
            reverse=True,
        )
        buy_list.extend(ranked_candidates[:cfg['max_stocks']])
    if skipped_low_model:
        print(f" 模型过滤低分候选: {', '.join(skipped_low_model[:10])}")
    if skipped_guard_modes:
        print(f" 午盘闸门拦截模式: {', '.join(sorted(set(skipped_guard_modes))[:10])}")
    if limited_mode_hits:
        print(f" 午盘闸门收紧模式: {', '.join(sorted(set(limited_mode_hits))[:10])}")
    if skipped_partial_rollback:
        print(f" 部分回退拦截候选: {', '.join(sorted(set(skipped_partial_rollback))[:10])}")
    if skipped_recent_reentry:
        print(f" 近期亏损/刚卖冷却拦截: {', '.join(sorted(set(skipped_recent_reentry))[:10])}")
    if skipped_learning_guard:
        print(f" 学习层历史剔除拦截: {', '.join(sorted(set(skipped_learning_guard))[:10])}")

    print(f"\n{'='*60}")
    print(f" V10模拟买入（分批建仓） {'[DRY RUN]' if dry_run else '[LIVE]'}")
    print(f" 总资产: ¥{total_assets:,.2f} | 可用资金: ¥{balance['avail_balance']:,.2f}")
    print(f" 信号: T1={len(signals[1])} T2={len(signals[2])} T3={len(signals[3])}")
    print(
        f" 模型: 市场{model_market.get('score', 0):.1f}分 "
        f"| 入场阈值≥{min_model_score:.1f} "
        f"| 权重 M/Sector/Stock/Flow="
        f"{model_state_summary['weights']['market']:.2f}/"
        f"{model_state_summary['weights']['sector']:.2f}/"
        f"{model_state_summary['weights']['stock']:.2f}/"
        f"{model_state_summary['weights']['flow']:.2f}"
    )
    if has_v9_full:
        print(f" 检测到V9_full强确认，但部分回退后仅按普通首建执行。")
    print(f"{'='*60}")

    if not buy_list:
        print(" 有扫描信号，但因资金/价格/仓位约束未形成可买清单")
        return EXIT_NO_ACTION
    max_new_positions = _inum(pm_buy_guard.get('max_new_positions', 0), 0)
    if max_new_positions > 0 and len(buy_list) > max_new_positions:
        ranked_buy_list = sorted(
            buy_list,
            key=lambda item: (
                _inum(item.get('big_meat_priority_rank', 0), 0),
                _fnum(item.get('big_meat_seed_score', 0.0), 0.0),
                _fnum(item.get('ranking_score', 0.0), 0.0),
            ),
            reverse=True,
        )
        skipped_due_to_gate = ranked_buy_list[max_new_positions:]
        buy_list = ranked_buy_list[:max_new_positions]
        if skipped_due_to_gate:
            print(
                " 午盘闸门压缩尾盘新开仓数量: "
                + ', '.join(f"{item['code']}[{item.get('mode', '')}]" for item in skipped_due_to_gate[:10])
            )

    # 14:50 后只保留买入短链路，不再在买入阶段插入卖单动作。
    positions = get_positions()
    orders = get_orders()
    if positions is None:
        positions = []
    if orders is None:
        orders = []
    pending_items = refresh_pending_orders(orders=orders, positions=positions)
    if not dry_run:
        cleanup_summary = cleanup_pending_orders(items=pending_items, orders=orders, positions=positions)
        if cleanup_summary.get('attempted', 0) > 0:
            pending_items = cleanup_summary.get('items', pending_items)
            print(
                f" 已自动清理 pending {cleanup_summary['attempted']} 条: "
                f"成功撤单{cleanup_summary['cancelled']} 失败{cleanup_summary['failed']}"
            )
    records, changed = sync_track_record(records, positions=positions, orders=orders, pending_items=pending_items)
    if changed and not dry_run:
        save_track_record(records)
    pending_items = refresh_pending_orders(orders=orders, positions=positions)
    pending_summary = summarize_pending_orders(pending_items)
    active_pos_map = _active_position_map(positions)
    today = _market_today()
    holding_codes = {
        str(r.get('code', '')).zfill(6)
        for r in records
        if r.get('status') == 'holding'
    }
    blocked_codes = (
        set(active_pos_map)
        | holding_codes
        | set(pending_summary.get('active_buy_codes', []))
    )
    tradability_exclusions = _load_today_tradability_exclusions()
    filtered_buy_list = []
    skipped_existing = []
    skipped_today_exclusion = []
    for item in buy_list:
        code = str(item['code']).zfill(6)
        if code in blocked_codes:
            skipped_existing.append(code)
            continue
        exclusion = tradability_exclusions.get(code)
        if exclusion:
            skipped_today_exclusion.append(f"{code}({exclusion_reason_text(exclusion)})")
            continue
        filtered_buy_list.append(item)
    buy_list = filtered_buy_list
    if skipped_existing:
        print(f" 已过滤现有持仓/重复代码: {', '.join(sorted(set(skipped_existing)))}")
    if skipped_today_exclusion:
        print(f" 已按09:31当日流动性门过滤: {', '.join(skipped_today_exclusion)}")
    if pending_summary.get('active_buy_codes'):
        print(f" 当前存在未完成买单: {', '.join(pending_summary['active_buy_codes'])}")
    if not buy_list:
        print(" 买入候选已被现有持仓或未完成买单过滤，尾盘不再重复报单")
        return EXIT_NO_ACTION
    funded_buy_list = []
    skipped_budget = []
    avail = balance['avail_balance']
    for item in buy_list:
        entry_price = _fnum(item.get('entry_price', 0.0), 0.0)
        planned_build_amount = _fnum(item.get('planned_build_amount', 0.0), 0.0)
        if entry_price <= 0 or planned_build_amount <= 0:
            continue
        qty = calc_buy_quantity(entry_price, planned_build_amount)
        if qty <= 0:
            skipped_budget.append(f"{item['code']}(too_small)")
            continue
        cost = qty * entry_price
        if cost > avail:
            qty = int(avail / entry_price / 100) * 100
            if qty < 100:
                skipped_budget.append(f"{item['code']}(cash)")
                continue
            cost = qty * entry_price
        funded_item = dict(item)
        funded_item['quantity'] = qty
        funded_item['cost'] = cost
        funded_buy_list.append(funded_item)
        avail -= cost
    buy_list = funded_buy_list
    if skipped_budget:
        print(f" 资金约束压缩候选: {', '.join(skipped_budget[:10])}")
    if not buy_list:
        print(" 候选在最终资金分配后未形成可执行买单")
        return EXIT_NO_ACTION
    if buy_list:
        run_slot = str((scan_ctx.get('meta') or {}).get('run_slot') or (scan_ctx.get('run_slot') or '')).strip()
        ranking['all_ranked'] = [_attach_decision_identity(item, run_slot) for item in (ranking.get('all_ranked') or [])]
        buy_list = [_attach_decision_identity(item, run_slot) for item in buy_list]
        selected_codes = {item['code'] for item in buy_list}
        record_model_decisions(run_slot, ranking['all_ranked'], selected_codes=selected_codes, scan_context=scan_ctx)

    # 执行买入
    success_count = 0
    success_codes = []
    trade_contexts = {}
    retry_tail_queue = []
    for item in buy_list:
        tier = item['tier']
        label = TIER_CONFIG[tier]['label']
        cost = item['cost']
        build_note = item.get('build_note', '')
        target = item.get('target_amount', 0)
        model_note = (
            f"模型{item.get('model_score', 0.0):.1f} "
            f"(市{item.get('model_market_score', 0.0):.0f}/"
            f"板{item.get('model_sector_score', 0.0):.0f}/"
            f"股{item.get('model_stock_score', 0.0):.0f}/"
            f"流{item.get('model_flow_score', 0.0):.0f}) "
            f"[{item.get('model_industry', 'unknown')}]"
        )
        build_note_full = f"{build_note} | {model_note}"

        # 信号累加加分：新建仓场景也参考（不强加分，避免误推导致过度建仓）
        # 已有30min内strength>=2的同code = scanner反复推 = 轻量+5分
        strength = _get_signal_strength(item['code'], 'buy')
        if strength >= 2:
            print(f"   {item['code']} {item['name']} 信号强度={strength} (新建仓信心加成)")

        if dry_run:
            print(f"   [DRY] {label} {item['code']} {item['name']} "
                  f"¥{item['entry_price']:.2f} × {item['quantity']}股 "
                  f"≈¥{cost:,.0f}/{target:,.0f} [{item['mode']}] {build_note_full}")
        else:
            print(f"   {label} {item['code']} {item['name']} "
                  f"¥{item['entry_price']:.2f} × {item['quantity']}股 "
                  f"≈¥{cost:,.0f}/{target:,.0f} [{item['mode']}] {build_note_full}")
            trade_result = execute_trade_action(
                'buy',
                item['code'],
                item['quantity'],
                ref_price=item['entry_price'],
                execution_phase='primary',
                strategy_action='buy',
            )
            if trade_result['success']:
                success_count += 1
                code = str(item['code']).zfill(6)
                success_codes.append(code)
                trade_contexts[code] = _build_buy_reconcile_context(
                    item,
                    today=today,
                    build_note=build_note_full,
                    tier=tier,
                )
            elif is_rate_limited_trade_result(trade_result):
                enqueue_buy_tail_retry(retry_tail_queue, item)

    if not dry_run and retry_tail_queue:
        success_count = _run_buy_tail_retry_queue(
            retry_tail_queue,
            success_codes=success_codes,
            success_count=success_count,
            trade_contexts=trade_contexts,
            today=today,
        )

    if not dry_run and success_codes:
        buy_apply = _apply_successful_buys(
            success_codes,
            records=records,
            balance=balance,
            positions=positions,
            trade_contexts=trade_contexts,
        )
        records = buy_apply['records']
        balance = buy_apply['balance']
        positions = buy_apply['positions']
        if buy_apply['changed']:
            for code in success_codes:
                buy_record_idx = _find_record_index(records, code)
                buy_record = records[buy_record_idx] if buy_record_idx is not None else {}
                buy_time = str(buy_record.get('buy_time', '')).strip()
                if buy_time:
                    print(f"   {code} 买入时点: {buy_time}")

    print(f"\n{'='*60}")
    total_buy = sum(i['cost'] for i in buy_list)
    print(f" 预计投入: ¥{total_buy:,.0f}")
    print(f" 拟买: {len(buy_list)} 只")
    print(f"{'='*60}")
    #region debug-point G:buywatch-1450-fail-summary
    _debug_emit_event(
        'C',
        'v10_moni_trader.py:do_buy',
        '[DEBUG] buy list summarized',
        {
            'buy_count': len(buy_list),
            'success_count': int(success_count),
            'total_buy': round(total_buy, 2),
            'dry_run': bool(dry_run),
        },
    )
    #endregion
    if not dry_run:
        live_state = _refresh_live_artifact_state(records)
        balance = live_state['balance']
        positions = live_state['positions']
        records = live_state['records']
        pending_items = live_state['pending_items']
        write_account_artifacts('buy', balance=balance, positions=positions, records=records, pending_items=pending_items)
        if success_count <= 0:
            return EXIT_RUNTIME_ERROR
    if dry_run:
        return EXIT_OK
    if success_count > 0:
        return EXIT_OK
    return EXIT_NO_ACTION


def do_sell(dry_run=False):
    """卖出T+5到期的持仓（兜底模式：只看持仓天数）"""
    return _do_sell_core(smart=False, dry_run=dry_run)


def do_smart_sell(dry_run=False):
    """智能卖出：信号衰减随时走人 + T+5兜底

    T+5是总框架，不是锁死持仓天数！
    - T+1赚了感觉明天要跌 → T+1卖
    - T+2有利润但信号转弱 → T+2卖
    - 信号完好 → 继续持有，T+5到期再评估
    买靠信号，卖靠判断，T+5只是兜底。
    """
    lock_owner = 'v10-smart-sell'
    lock_state = acquire_shared_phase_lock(
        'smart_sell_shared',
        owner=lock_owner,
        ttl_seconds=SMART_SELL_SHARED_LOCK_TTL_SECONDS,
    )
    if not lock_state.get('acquired'):
        holder = str(lock_state.get('owner', '')).strip() or 'unknown'
        print(f" smart-sell 共享锁占用中，当前由 {holder} 执行，本轮快速跳过。")
        _debug_report_smart_sell(
            'do_smart_sell.lock_blocked',
            holder=holder,
            holder_pid=_inum(lock_state.get('pid', 0), 0),
        )
        return EXIT_NO_ACTION
    try:
        return _do_sell_core(smart=True, dry_run=dry_run)
    finally:
        release_shared_phase_lock('smart_sell_shared', owner=lock_owner)


def _do_sell_core(smart=False, dry_run=False):
    """卖出逻辑核心：T+5兜底 + 可选信号衰减判断"""
    action = 'smart_sell' if smart else 'sell'
    #region debug-point smart-sell-core-entry
    sell_core_started_at = time.perf_counter()
    #endregion
    if smart:
        _debug_report_smart_sell("do_smart_sell.start", action=action)
    if not ensure_trade_window(action, dry_run=dry_run):
        return EXIT_WINDOW_SKIPPED
    records = load_track_record()
    today = _market_today()
    track_holding = [r for r in records if r.get('status') == 'holding']
    #region debug-point smart-sell-initial-refresh-start
    initial_refresh_started_at = time.perf_counter()
    #endregion
    positions = get_positions()
    local_pending_items = load_pending_orders()
    local_pending_summary = summarize_pending_orders(local_pending_items)
    no_live_holding = not track_holding and not positions
    no_active_pending = not local_pending_summary.get('active_buy_codes') and not local_pending_summary.get('active_sell_codes')
    if no_live_holding and no_active_pending:
        if smart:
            _debug_report_smart_sell(
                "do_smart_sell.fast_exit_no_live_holding",
                holding_records=0,
                positions=0,
                pending_count=len(local_pending_items or []),
                elapsed_ms=round((time.perf_counter() - initial_refresh_started_at) * 1000, 2),
            )
        print(" 当前无持仓")
        return EXIT_NO_ACTION
    balance = get_balance()
    orders = get_orders() if local_pending_items else []
    pending_items = refresh_pending_orders(orders=orders, positions=positions) if local_pending_items else []
    if smart:
        _debug_report_smart_sell(
            "do_smart_sell.after_initial_refresh",
            holding_records=len(track_holding),
            positions=len(positions or []),
            orders=len(orders or []),
            pending_count=len(pending_items or []),
            elapsed_ms=round((time.perf_counter() - initial_refresh_started_at) * 1000, 2),
        )
    if not dry_run:
        #region debug-point smart-sell-cleanup-start
        cleanup_started_at = time.perf_counter()
        #endregion
        cleanup_summary = cleanup_pending_orders(items=pending_items, orders=orders, positions=positions)
        if smart:
            #region debug-point smart-sell-cleanup-done
            _debug_report_smart_sell(
                "do_smart_sell.after_cleanup_pending",
                attempted=_inum(cleanup_summary.get('attempted', 0), 0),
                cancelled=_inum(cleanup_summary.get('cancelled', 0), 0),
                failed=_inum(cleanup_summary.get('failed', 0), 0),
                elapsed_ms=round((time.perf_counter() - cleanup_started_at) * 1000, 2),
            )
            #endregion
        if cleanup_summary.get('attempted', 0) > 0:
            pending_items = cleanup_summary.get('items', pending_items)
            print(
                f" 已自动清理 pending {cleanup_summary['attempted']} 条: "
                f"成功撤单{cleanup_summary['cancelled']} 失败{cleanup_summary['failed']}"
            )
    #region debug-point smart-sell-sync-start
    sync_started_at = time.perf_counter()
    #endregion
    records, changed = sync_track_record(records, positions=positions, orders=orders, pending_items=pending_items)
    if smart:
        #region debug-point smart-sell-sync-done
        _debug_report_smart_sell(
            "do_smart_sell.after_sync_track_record",
            changed=bool(changed),
            elapsed_ms=round((time.perf_counter() - sync_started_at) * 1000, 2),
        )
        #endregion
    #region debug-point smart-sell-full-reconcile-start
    full_reconcile_started_at = time.perf_counter()
    #endregion
    records, full_changed, reconcile_summary = full_reconcile_positions(
        records,
        positions=positions,
        orders=orders,
        pending_items=pending_items,
    )
    if smart:
        #region debug-point smart-sell-full-reconcile-done
        _debug_report_smart_sell(
            "do_smart_sell.after_full_reconcile",
            changed=bool(full_changed),
            imported_positions=_inum(reconcile_summary.get('imported_positions', 0), 0),
            overlaid_positions=_inum(reconcile_summary.get('overlaid_positions', 0), 0),
            elapsed_ms=round((time.perf_counter() - full_reconcile_started_at) * 1000, 2),
        )
        #endregion
    changed = changed or full_changed
    if changed and not dry_run:
        save_track_record(records)
    pending_summary = summarize_pending_orders(pending_items)
    pending_sell_ctx = _active_pending_context_by_code(pending_items, action='sell')
    if pending_summary.get('active_sell_codes'):
        print(f" 当前存在未完成卖单: {', '.join(pending_summary['active_sell_codes'])}")

    active_pos_map = _active_position_map(positions)
    pos_price_map = {code: pos.get('price', 0.0) for code, pos in active_pos_map.items()}

    holding = [r for r in records if r.get('status') == 'holding']
    if not holding:
        print(" 当前无持仓")
        return EXIT_NO_ACTION

    # 连接TDX用于信号衰减检测（smart模式）
    tdx_api = None
    if smart:
        #region debug-point smart-sell-connect-tdx-start
        tdx_started_at = time.perf_counter()
        #endregion
        tdx_api = connect_tdx()
        if smart:
            #region debug-point smart-sell-connect-tdx-done
            _debug_report_smart_sell(
                "do_smart_sell.after_connect_tdx",
                connected=bool(tdx_api),
                elapsed_ms=round((time.perf_counter() - tdx_started_at) * 1000, 2),
            )
            #endregion
        if tdx_api:
            print(f" TDX已连接（信号衰减检测模式）")
        else:
            print(f" TDX连接失败，仅执行T+5兜底卖出")
    decision_reference = _build_selected_decision_reference(_read_jsonl(MODEL_DECISIONS_FILE, limit=5000)) if smart else {}
    learning_actions = _load_learning_actions() if smart else {}

    sold_count = 0
    confirmed_count = 0
    skipped_count = 0
    hold_count = 0
    state_changed = False
    tradability_exclusions = _load_today_tradability_exclusions()
    sell_retry_queue = []

    for r in holding:
        code = r.get('code', '')
        name = r.get('name', '')
        tier = r.get('tier', '?')
        mode = r.get('mode', '')
        learning_action = _resolve_learning_trade_action(code, mode, learning_actions) if smart else {}
        buy_date = r.get('date', '')
        entry_price = _fnum(r.get('entry_price', 0), 0.0)
        #region debug-point smart-sell-loop-enter
        loop_started_at = time.perf_counter()
        #endregion

        try:
            buy_dt = datetime.strptime(buy_date, '%Y-%m-%d')
            hold_days = (datetime.now() - buy_dt).days
        except ValueError:
            hold_days = 0

        pos = active_pos_map.get(str(code).zfill(6))
        if not pos:
            if smart:
                _clear_smart_sell_retry_state(code)
            print(f"  [SKIP] {code} {name} 跳过卖出判断: 当前无真实持仓")
            continue
        if _is_legacy_holding_record(r) and _track_qty_mismatch(r, pos):
            print(
                f"  [SKIP] {code} {name} 跳过卖出判断: "
                f"账本数量{_inum(r.get('quantity', 0), 0)}与实仓{_inum(pos.get('count', 0), 0)}不一致"
            )
            continue
        exclusion = tradability_exclusions.get(str(code).zfill(6))
        if exclusion:
            skipped_count += 1
            print(
                f"  [SKIP] {code} {name} 今日不发卖单: "
                f"{exclusion_reason_text(exclusion)}"
            )
            continue

        # 当前价格（优先从持仓API获取）
        cur_price = pos_price_map.get(code, entry_price)
        retry_state = _get_smart_sell_retry_state(code) if smart and not dry_run else {}
        if retry_state:
            skipped_count += 1
            print(
                f"  [COOLDOWN] {code} {name} 上轮触发112限流，"
                f"冷却到 {retry_state.get('cooldown_until', '')} 后再尝试卖出"
            )
            continue
        pnl_pct = _fnum(pos.get('profit_pct', 0.0), 0.0)
        if pnl_pct == 0 and entry_price > 0:
            pnl_pct = (cur_price / entry_price - 1) * 100 if entry_price > 0 else 0
        pending_ctx = pending_sell_ctx.get(str(code).zfill(6))
        reserved_qty = (pending_ctx or {}).get('reserved_qty', 0)
        qty = _effective_sellable_quantity(pos, _inum(r.get('quantity', 0), 0), reserved_qty)
        if qty <= 0:
            if reserved_qty > 0:
                reprice_allowed, reprice_reason = _should_reprice_pending_sell(
                    str(code).zfill(6),
                    pending_ctx,
                    smart=smart,
                    tdx_api=tdx_api,
                    entry_price=entry_price,
                    mode=mode,
                    profit_pct=pnl_pct,
                    current_price=cur_price,
                )
                if reprice_allowed:
                    if dry_run:
                        print(
                            f"  [DRY] {code} {name} 已有未完成卖单占用{reserved_qty}股，剩余可卖0股；"
                            f"{reprice_reason}。dry-run 不执行撤单，等待下一次复核"
                        )
                    else:
                        cancel_summary = cancel_pending_context(
                            str(code).zfill(6),
                            pending_ctx,
                            reason=f"reprice:{reprice_reason}",
                        )
                        if cancel_summary.get('cancelled', 0) > 0:
                            print(
                                f"  [CANCEL] {code} {name} 已有未完成卖单占用{reserved_qty}股，剩余可卖0股；"
                                f"{reprice_reason}。已触发撤单清理{cancel_summary['cancelled']}笔，等待下一次复核重报"
                            )
                        else:
                            print(
                                f"  [WAIT] {code} {name} 已有未完成卖单占用{reserved_qty}股，剩余可卖0股；"
                                f"{reprice_reason}。撤单清理未成功，先等待下一次复核"
                            )
                else:
                    print(
                        f"  [SKIP] {code} {name} 已有未完成卖单占用{reserved_qty}股，剩余可卖0股；"
                        f"{reprice_reason}"
                    )
            else:
                print(f"  [SKIP] {code} {name} 跳过卖出判断: 当前可卖数量不足")
            continue
        if reserved_qty > 0:
            print(
                f"  [INFO] {code} {name} 已有未完成卖单占用{reserved_qty}股，"
                f"本次仅按剩余可卖{qty}股评估"
            )

        sell_reason = None
        sell_action = ''

        # ── 规则1: T+5兜底 ──
        if hold_days >= 5:
            sell_reason = f"T+5到期(持仓{hold_days}天)"
            sell_action = BIG_MEAT_ACTION_HARD_EXIT

        # ── 规则2: 信号衰减 → 提前卖出（smart模式） ──
        elif smart and tdx_api:
            #region debug-point smart-sell-decay-start
            decay_started_at = time.perf_counter()
            #endregion
            should_sell, decay_reason, decay_score = evaluate_signal_decay(
                tdx_api, code, entry_price, mode, profit_pct=pnl_pct
            )
            if smart:
                #region debug-point smart-sell-decay-done
                _debug_report_smart_sell(
                    "do_smart_sell.after_decay_eval",
                    code=str(code).zfill(6),
                    should_sell=bool(should_sell),
                    decay_score=_fnum(decay_score, 0.0),
                    elapsed_ms=round((time.perf_counter() - decay_started_at) * 1000, 2),
                )
                #endregion
            window_tag = str(r.get('big_meat_window_tag', '')).strip() or _current_add_position_window_tag()
            state_window = ADD_POSITION_WINDOW_SETTINGS.get(window_tag, {}) if window_tag else {}
            score_gate = _fnum(state_window.get('score_min', 4.5), 4.5)
            aggressive_gate = _fnum(state_window.get('aggressive_score_min', 6.0), 6.0)
            decision_row = _resolve_record_decision_row(r, decision_reference)
            big_meat_profile = _build_big_meat_add_profile(
                tdx_api,
                code,
                record=r,
                profit_pct=pnl_pct,
                decision_row=decision_row,
                window_tag=window_tag,
            )
            big_meat_transition = _resolve_big_meat_transition(
                r,
                big_meat_profile,
                score_min=score_gate,
                aggressive_score_min=aggressive_gate,
                window_tag=window_tag,
            )
            holding_big_meat_profile = _build_holding_big_meat_profile(
                r,
                profit_pct=pnl_pct,
                add_profile=big_meat_profile,
                decision_row=decision_row,
            )
            late_bloom_promote = bool(holding_big_meat_profile.get('late_bloom_eligible'))
            state_record = _normalize_record(dict(r))
            state_record = _apply_big_meat_state(
                state_record,
                state=BIG_MEAT_STATE_CONFIRMED if late_bloom_promote else str(big_meat_transition.get('state', '')).strip(),
                profile=big_meat_profile,
                reason='late_bloom_confirm' if late_bloom_promote else str(big_meat_profile.get('reason', '')).strip(),
                window_tag=window_tag,
            )
            state_record = _apply_holding_big_meat_profile(
                state_record,
                profile=holding_big_meat_profile,
                promote=late_bloom_promote,
            )
            state_action = _resolve_big_meat_state_action(
                state_record,
                should_sell=should_sell,
                decay_score=decay_score,
                decay_reason=decay_reason,
                holding_profile=holding_big_meat_profile,
                learning_action=learning_action,
            )
            current_snapshot = _big_meat_record_snapshot(r)
            r = _apply_big_meat_state(
                r,
                state=BIG_MEAT_STATE_CONFIRMED if late_bloom_promote else str(big_meat_transition.get('state', '')).strip(),
                profile=big_meat_profile,
                reason='late_bloom_confirm' if late_bloom_promote else str(big_meat_profile.get('reason', '')).strip(),
                window_tag=window_tag,
            )
            r = _apply_holding_big_meat_profile(
                r,
                profile=holding_big_meat_profile,
                promote=late_bloom_promote,
            )
            refreshed_snapshot = _big_meat_record_snapshot(r)
            if refreshed_snapshot != current_snapshot:
                state_changed = True
            if should_sell:
                sell_action = str(state_action.get('action', '')).strip()
                if sell_action == BIG_MEAT_ACTION_HOLD_CORE:
                    r = _apply_holding_big_meat_profile(
                        r,
                        profile=holding_big_meat_profile,
                        hold_state=BIG_MEAT_ACTION_HOLD_CORE,
                    )
                    state_changed = True
                    hold_count += 1
                    print(
                        f"  [HOLD-CORE] {code} {name} T{tier} | 持仓{hold_days}天 | "
                        f"收益{pnl_pct:+.1f}% | {state_action.get('reason', decay_reason)}"
                    )
                    continue
                if sell_action == BIG_MEAT_ACTION_RISK_TRIM:
                    trimmed_qty = _risk_trim_quantity_for_record(r, qty)
                    if trimmed_qty <= 0:
                        r = _apply_holding_big_meat_profile(
                            r,
                            profile=holding_big_meat_profile,
                            hold_state=BIG_MEAT_ACTION_HOLD_CORE,
                        )
                        state_changed = True
                        hold_count += 1
                        print(
                            f"  [HOLD] {code} {name} T{tier} | 持仓{hold_days}天 | "
                            f"收益{pnl_pct:+.1f}% | 仅剩 core / trade_qty 不足，继续观察"
                        )
                        continue
                    qty = trimmed_qty
                    r = _apply_holding_big_meat_profile(
                        r,
                        profile=holding_big_meat_profile,
                        hold_state=BIG_MEAT_ACTION_RISK_TRIM,
                    )
                    state_changed = True
                    sell_reason = f"risk_trim[{decay_reason}](持仓{hold_days}天)"
                elif sell_action == BIG_MEAT_ACTION_HARD_EXIT:
                    sell_reason = f"hard_exit[{decay_reason}](持仓{hold_days}天)"
                    r = _apply_big_meat_state(r, state='')
                    state_changed = True
                else:
                    sell_reason = f"信号衰减[{decay_reason}](持仓{hold_days}天)"
            else:
                if str(r.get('big_meat_state', '')).strip() == BIG_MEAT_STATE_CONFIRMED:
                    r = _apply_holding_big_meat_profile(
                        r,
                        profile=holding_big_meat_profile,
                        hold_state=BIG_MEAT_ACTION_HOLD_CORE,
                    )
                    state_changed = True
                if late_bloom_promote:
                    print(f"   {code} {name} 持仓后发走强，升级为大肉确认并进入 core 持有")
                # 信号完好，继续持有
                emoji = "" if pnl_pct > 0 else "" if pnl_pct < 0 else ""
                print(f"  {emoji} {code} {name} T{tier} | 持仓{hold_days}天 | "
                      f"收益{pnl_pct:+.1f}% | {decay_reason} → 继续持有")
                hold_count += 1
                if smart:
                    #region debug-point smart-sell-loop-hold
                    _debug_report_smart_sell(
                        "do_smart_sell.loop_hold",
                        code=str(code).zfill(6),
                        hold_days=_inum(hold_days, 0),
                        pnl_pct=_fnum(pnl_pct, 0.0),
                        elapsed_ms=round((time.perf_counter() - loop_started_at) * 1000, 2),
                    )
                    #endregion
                continue

        # ── 规则3: 浮盈+持仓时间适中+smart模式 → 可选落袋 ──
        # (已在 evaluate_signal_decay 的 decay_score>=2+浮盈>2% 中处理)

        if sell_reason:
            if dry_run:
                sold_count += 1
                emoji = "" if pnl_pct >= 0 else ""
                print(f"  {emoji} {code} {name} T{tier} | [DRY RUN] {sell_reason} | "
                      f"预计数量{qty} | 当前收益{pnl_pct:+.2f}%")
            else:
                if smart:
                    _debug_report_smart_sell(
                        "do_smart_sell.before_trade",
                        code=str(code).zfill(6),
                        qty=_inum(qty, 0),
                        reason=sell_reason,
                    )
                #region debug-point smart-sell-trade-start
                trade_started_at = time.perf_counter()
                #endregion
                trade_result = execute_trade_action(
                    'sell',
                    code,
                    qty,
                    ref_price=cur_price,
                    execution_phase='primary',
                    strategy_action=action,
                )
                if trade_result['success']:
                    if smart:
                        _debug_report_smart_sell(
                            "do_smart_sell.trade_success",
                            code=str(code).zfill(6),
                            qty=_inum(qty, 0),
                            order_id=str(trade_result.get('order_id', '') or ''),
                            elapsed_ms=round((time.perf_counter() - trade_started_at) * 1000, 2),
                        )
                    if smart:
                        _clear_smart_sell_retry_state(code)
                    sell_apply = _apply_successful_sell(
                        code,
                        quantity=qty,
                        price=cur_price,
                        close_reason=sell_reason,
                        records=records,
                        balance=balance,
                        positions=positions,
                        defer_reconcile=smart,
                    )
                    records = sell_apply['records']
                    balance = sell_apply['balance']
                    positions = sell_apply['positions']
                    updated_record = sell_apply['record'] or r
                    sold_count += 1
                    if sell_apply.get('sell_confirmed'):
                        confirmed_count += 1
                        pnl = _fnum(updated_record.get('pnl', 0.0), 0.0)
                        pnl_pct = _fnum(updated_record.get('pnl_pct', 0.0), 0.0)
                        sell_time = updated_record.get('sell_time', '')
                        emoji = "" if pnl >= 0 else ""
                        time_info = f" @{sell_time}" if sell_time else ""
                        print(f"  {emoji} {code} {name} T{tier} | {sell_reason} | "
                              f"收益{pnl_pct:+.2f}% (¥{pnl:+,.0f}){time_info}")
                    else:
                        pending_ctx = sell_apply.get('pending_ctx') or {}
                        reserved_qty = _inum(pending_ctx.get('reserved_qty', 0), qty)
                        print(
                            f"  [PENDING] {code} {name} T{tier} | {sell_reason} | "
                            f"卖单已受理 {reserved_qty} 股，等待后续成交确认"
                        )
                elif smart and is_rate_limited_trade_result(trade_result):
                    _debug_report_smart_sell(
                        "do_smart_sell.trade_rate_limited",
                        code=str(code).zfill(6),
                        qty=_inum(qty, 0),
                        elapsed_ms=round((time.perf_counter() - trade_started_at) * 1000, 2),
                    )
                    enqueue_sell_tail_retry(
                        sell_retry_queue,
                        code=code,
                        name=name,
                        tier=tier,
                        qty=qty,
                        cur_price=cur_price,
                        sell_reason=sell_reason,
                    )
                else:
                    skipped_count += 1
                    if smart:
                        #region debug-point smart-sell-trade-failed
                        _debug_report_smart_sell(
                            "do_smart_sell.trade_failed",
                            code=str(code).zfill(6),
                            qty=_inum(qty, 0),
                            elapsed_ms=round((time.perf_counter() - trade_started_at) * 1000, 2),
                            message=str(trade_result.get('message', '') or ''),
                            result_code=str(trade_result.get('code', '') or ''),
                        )
                        #endregion
        else:
            # 非smart模式且未到T+5 → 显示状态
            emoji = "" if pnl_pct > 0 else "" if pnl_pct < 0 else ""
            print(f"  {emoji} {code} {name} T{tier} | 持仓{hold_days}天 | "
                  f"收益{pnl_pct:+.1f}% → 继续持有")
            hold_count += 1
        if smart:
            #region debug-point smart-sell-loop-exit
            _debug_report_smart_sell(
                "do_smart_sell.loop_exit",
                code=str(code).zfill(6),
                hold_days=_inum(hold_days, 0),
                elapsed_ms=round((time.perf_counter() - loop_started_at) * 1000, 2),
                sold_count=_inum(sold_count, 0),
                hold_count=_inum(hold_count, 0),
                skipped_count=_inum(skipped_count, 0),
            )
            #endregion

    if smart and sell_retry_queue:
        _debug_report_smart_sell("do_smart_sell.before_tail_retry", queue_size=len(sell_retry_queue))
        records, balance, positions, sold_count, confirmed_count, skipped_count = _run_sell_tail_retry_queue(
            sell_retry_queue,
            action=action,
            records=records,
            balance=balance,
            positions=positions,
            sold_count=sold_count,
            confirmed_count=confirmed_count,
            skipped_count=skipped_count,
        )
    if state_changed and not dry_run:
        save_track_record(records)

    if tdx_api:
        try:
            tdx_api.disconnect()
        except Exception:
            pass

    if not dry_run:
        if smart:
            _debug_report_smart_sell(
                "do_smart_sell.before_write_artifacts",
                sold_count=_inum(sold_count, 0),
                confirmed_count=_inum(confirmed_count, 0),
                hold_count=_inum(hold_count, 0),
                skipped_count=_inum(skipped_count, 0),
            )
        #region debug-point smart-sell-refresh-artifacts-start
        artifact_refresh_started_at = time.perf_counter()
        #endregion
        live_state = _refresh_live_artifact_state(records)
        if smart:
            #region debug-point smart-sell-refresh-artifacts-done
            _debug_report_smart_sell(
                "do_smart_sell.after_refresh_live_artifact_state",
                elapsed_ms=round((time.perf_counter() - artifact_refresh_started_at) * 1000, 2),
            )
            #endregion
        final_balance = live_state['balance']
        final_positions = live_state['positions']
        final_pending_items = live_state['pending_items']
        records = live_state['records']
        #region debug-point smart-sell-write-artifacts-start
        write_artifacts_started_at = time.perf_counter()
        #endregion
        write_account_artifacts(
            'smart_sell' if smart else 'sell',
            balance=final_balance,
            positions=final_positions,
            records=records,
            pending_items=final_pending_items,
        )
        if smart:
            _debug_report_smart_sell(
                "do_smart_sell.after_write_artifacts",
                sold_count=_inum(sold_count, 0),
                confirmed_count=_inum(confirmed_count, 0),
                hold_count=_inum(hold_count, 0),
                skipped_count=_inum(skipped_count, 0),
                elapsed_ms=round((time.perf_counter() - write_artifacts_started_at) * 1000, 2),
            )

    # 汇总
    print(f"\n{'='*50}")
    mode_label = "智能卖出(信号衰减+T+5兜底)" if smart else "T+5兜底卖出"
    if dry_run:
        mode_label = f"{mode_label} [DRY RUN]"
    print(f" {mode_label} 结果")
    print(
        f"  卖单受理: {sold_count} 只 | 已闭合: {confirmed_count} 只 | "
        f"继续持有: {hold_count} 只 | 失败: {skipped_count} 只"
    )
    print(f"{'='*50}")
    if smart:
        #region debug-point smart-sell-core-exit
        _debug_report_smart_sell(
            "do_smart_sell.exit",
            sold_count=_inum(sold_count, 0),
            confirmed_count=_inum(confirmed_count, 0),
            hold_count=_inum(hold_count, 0),
            skipped_count=_inum(skipped_count, 0),
            elapsed_ms=round((time.perf_counter() - sell_core_started_at) * 1000, 2),
        )
        #endregion

    # 统计总战绩
    _print_stats(records)
    if dry_run:
        return EXIT_OK
    if sold_count > 0:
        return EXIT_OK
    if skipped_count > 0 and hold_count <= 0:
        return EXIT_RUNTIME_ERROR
    return EXIT_NO_ACTION


def _print_stats(records):
    """打印战绩统计（公共函数）"""
    stats = compute_track_stats(records)
    closed = [r for r in records if r.get('status') == 'closed']
    if closed:
        print(f"\n V10模拟战绩统计")
        print(f"  总交易: {len(closed)} 笔")
        print(f"  胜率: {stats['win_rate_pct']:.1f}%")
        print(f"  平均收益: {stats['avg_return_pct']:+.2f}%")
        print(f"  总盈亏: ¥{stats['realized_pnl']:+,.0f}")

        # 按tier统计
        for tier in [1, 2, 3]:
            tier_trades = stats['tier_stats'].get(tier, [])
            if tier_trades:
                tier_wins = [r for r in tier_trades if _fnum(r.get('pnl', 0.0), 0.0) > 0]
                tier_wr = len(tier_wins) / len(tier_trades) * 100 if tier_trades else 0
                tier_avg = sum(_fnum(r.get('pnl_pct', 0.0), 0.0) for r in tier_trades) / len(tier_trades)
                print(f"  T{tier}: {len(tier_trades)}笔 | 胜率{tier_wr:.0f}% | 平均{tier_avg:+.2f}%")


def do_status():
    """查看当前持仓和战绩"""
    balance = get_balance()
    positions = get_positions()
    records = load_track_record()
    orders = get_orders()
    pending_items = refresh_pending_orders(orders=orders, positions=positions)
    records, changed = sync_track_record(records, positions=positions, orders=orders, pending_items=pending_items)
    records, full_changed, _ = full_reconcile_positions(records, positions=positions, orders=orders, pending_items=pending_items)
    changed = changed or full_changed
    if changed:
        save_track_record(records)
    summary = write_account_artifacts('status', balance=balance, positions=positions, records=records)

    print(f"\n{'='*50}")
    print(f" V10模拟组合状态")
    print(f"{'='*50}")
    return EXIT_OK

    if balance:
        print(f" 总资产: ¥{balance['total_assets']:,.2f}")
        print(f" 可用资金: ¥{balance['avail_balance']:,.2f}")
        print(f" 持仓市值: ¥{balance['total_pos_value']:,.2f}")

    # 当前持仓
    holding = [r for r in records if r.get('status') == 'holding']
    if holding:
        print(f"\n 当前持仓 ({len(holding)}只):")
        for r in holding:
            buy_date = r.get('date', '')
            buy_time = r.get('buy_time', '')
            try:
                hold_days = (datetime.now() - datetime.strptime(buy_date, '%Y-%m-%d')).days
            except ValueError:
                hold_days = '?'
            time_info = f" @{buy_time}" if buy_time else ""
            print(f"  {r['code']} {r['name']} | T{r['tier']} | "
                  f"买入¥{r.get('entry_price', '?')} × {r.get('quantity', '?')}股{time_info} | "
                  f"持仓{hold_days}天 | {r.get('mode', '')}")
    else:
        print("\n 当前无持仓")

    # 已关闭交易统计
    closed = [r for r in records if r.get('status') == 'closed']
    if closed:
        print(f"\n 历史战绩 ({len(closed)}笔):")
        print(
            f"  胜率: {summary['performance']['win_rate_pct']:.1f}% | "
            f"平均收益: {summary['performance']['avg_return_pct']:+.2f}% | "
            f"总盈亏: ¥{summary['performance']['realized_pnl']:+,.0f}"
        )

        # 按tier统计
        for item in summary.get('tier_summary', []):
            if item['closed_trades'] > 0:
                print(
                    f"  T{item['tier']}: {item['closed_trades']}笔 | "
                    f"胜率{item['actual_win_rate_pct']:.0f}% | "
                    f"平均{item['actual_avg_return_pct']:+.2f}%"
                )

    print(f"\n 学习循环:")
    for note in summary.get('learning_notes', [])[:3]:
        print(f"  - {note}")
    print(f" NAV历史: {NAV_FILE}")
    print(f" 账户摘要: {SUMMARY_FILE}")

    print(f"{'='*50}")


def do_add_position(dry_run=False):
    """T+1加仓：对已有底仓但未达满仓目标的持仓加仓

    仓位管理：首建不满仓，T+1确认后加仓到满仓目标。
    加仓条件：
    1. 持仓状态为 holding
    2. 当前持仓金额 < target_amount（满仓目标）
    3. 信号未衰减（smart-sell 不建议卖出）
    """
    if not ensure_trade_window('add_position', dry_run=dry_run):
        return EXIT_WINDOW_SKIPPED
    # #region debug-point A:add-position-entry
    import json as _dbg_json, urllib.request as _dbg_urllib_request, time as _dbg_time, uuid as _dbg_uuid
    _dbg_env_path = str(Path(__file__).resolve().parent / '.dbg' / 'add-position-timeout.env')
    _dbg_url, _dbg_session = 'http://127.0.0.1:7777/event', 'add-position-timeout'
    try:
        with open(_dbg_env_path, 'r', encoding='utf-8') as _dbg_file:
            _dbg_env_content = _dbg_file.read()
        for _dbg_line in _dbg_env_content.splitlines():
            if _dbg_line.startswith('DEBUG_SERVER_URL='):
                _dbg_url = _dbg_line.split('=', 1)[1].strip() or _dbg_url
            elif _dbg_line.startswith('DEBUG_SESSION_ID='):
                _dbg_session = _dbg_line.split('=', 1)[1].strip() or _dbg_session
    except Exception:
        pass
    _dbg_trace_id = f"addpos-{int(_dbg_time.time() * 1000)}-{_dbg_uuid.uuid4().hex[:6]}"
    _dbg_t0 = _dbg_time.perf_counter()
    def _dbg_emit(_dbg_hypothesis_id, _dbg_msg, **_dbg_data):
        try:
            _dbg_payload = {
                "sessionId": _dbg_session,
                "runId": "pre-fix",
                "hypothesisId": _dbg_hypothesis_id,
                "location": "v10_moni_trader.py:do_add_position",
                "msg": _dbg_msg,
                "data": _dbg_data,
                "traceId": _dbg_trace_id,
            }
            _dbg_request = _dbg_urllib_request.Request(
                _dbg_url,
                data=_dbg_json.dumps(_dbg_payload, ensure_ascii=False).encode('utf-8'),
                headers={"Content-Type": "application/json"},
            )
            _dbg_urllib_request.urlopen(_dbg_request, timeout=1.5).read()
        except Exception:
            pass
    _dbg_emit('A', '[DEBUG] add_position entered', dry_run=bool(dry_run))
    # #endregion
    try:
        records = load_track_record()
        balance = get_balance()
        if not balance:
            print("[ERROR] 无法获取账户资金")
            # #region debug-point B:add-position-balance-missing
            _dbg_emit('B', '[DEBUG] add_position missing balance', elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1))
            # #endregion
            return EXIT_RUNTIME_ERROR

        avail = _fnum(balance.get('avail_balance', 0.0), 0.0)
        total_assets = _fnum(balance.get('total_assets', avail), avail)
        positions = get_positions()
        native_holding = [
            r for r in (records or [])
            if str((r or {}).get('status', '')).strip() == 'holding' and _is_native_strategy_record(r)
        ]
        # #region debug-point B:add-position-account-snapshot
        _dbg_emit(
            'B',
            '[DEBUG] add_position account snapshot',
            elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
            balance_total=round(_fnum(balance.get('total_assets', 0.0), 0.0), 2),
            balance_avail=round(_fnum(balance.get('avail_balance', 0.0), 0.0), 2),
            active_pos_count=len(_active_position_map(positions)),
            active_pos_codes=sorted(list(_active_position_map(positions).keys()))[:12],
            native_holding_count=len(native_holding),
            native_holding_codes=sorted([str((r or {}).get('code', '')).zfill(6) for r in native_holding])[:12],
        )
        # #endregion
        if not native_holding:
            print(" 当前无主策略持仓需要加仓")
            return EXIT_NO_ACTION
        orders = get_orders()
        pending_items = refresh_pending_orders(orders=orders, positions=positions)
        # #region debug-point B:add-position-prefetch
        _dbg_emit(
            'B',
            '[DEBUG] add_position prefetch done',
            elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
            records=len(records or []),
            positions=len(positions or []),
            orders=len(orders or []),
            pending=len(pending_items or []),
        )
        # #endregion
        if not dry_run:
        # #region debug-point D:add-position-cleanup-start
            _dbg_emit(
                'D',
                '[DEBUG] add_position cleanup pending start',
                elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
                pending=len(pending_items or []),
            )
        # #endregion
            cleanup_summary = cleanup_pending_orders(
                items=pending_items,
                orders=orders,
                positions=positions,
                max_cancel=ADD_POSITION_PENDING_CLEANUP_MAX_CANCEL,
                time_budget_seconds=ADD_POSITION_PENDING_CLEANUP_BUDGET_SECONDS,
                cancel_timeout_seconds=ADD_POSITION_PENDING_CANCEL_TIMEOUT_SECONDS,
            )
        # #region debug-point D:add-position-cleanup-done
            _dbg_emit(
                'D',
                '[DEBUG] add_position cleanup pending done',
                elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
                attempted=_inum(cleanup_summary.get('attempted', 0), 0),
                cancelled=_inum(cleanup_summary.get('cancelled', 0), 0),
                failed=_inum(cleanup_summary.get('failed', 0), 0),
                budget_exhausted=bool(cleanup_summary.get('budget_exhausted')),
                remaining_candidates=_inum(cleanup_summary.get('remaining_candidates', 0), 0),
            )
        # #endregion
            if cleanup_summary.get('attempted', 0) > 0:
                pending_items = cleanup_summary.get('items', pending_items)
                print(
                    f" 已自动清理 pending {cleanup_summary['attempted']} 条: "
                    f"成功撤单{cleanup_summary['cancelled']} 失败{cleanup_summary['failed']}"
                )
            if cleanup_summary.get('budget_exhausted'):
                print(
                    " [WARN] add_position pending 清理已触达预算上限，"
                    f"剩余候选{cleanup_summary.get('remaining_candidates', 0)}条留给后续阶段处理"
                )
        records, changed = sync_track_record(records, positions=positions, orders=orders, pending_items=pending_items)
        records, full_changed, _ = full_reconcile_positions(records, positions=positions, orders=orders, pending_items=pending_items)
        changed = changed or full_changed
        if changed and not dry_run:
            save_track_record(records)
        holding = [
            r for r in (records or [])
            if str((r or {}).get('status', '')).strip() == 'holding' and _is_native_strategy_record(r)
        ]
        # #region debug-point B:add-position-reconcile
        _dbg_emit(
            'B',
            '[DEBUG] add_position reconcile done',
            elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
            changed=bool(changed),
            records=len(records or []),
            holding_records=len(holding),
            holding_codes=sorted([str(r.get('code', '')).zfill(6) for r in holding])[:12],
        )
        # #endregion
    except Exception as exc:
        # #region debug-point E:add-position-exception
        _dbg_emit(
            'E',
            '[DEBUG] add_position top-level exception',
            elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
            exc_type=type(exc).__name__,
            exc_text=str(exc),
        )
        # #endregion
        raise
    recent_trade_memory = _build_recent_trade_memory(records)
    mode_capital_profile = _build_mode_capital_profile(records)
    learning_actions = _load_learning_actions()
    blocked_t3_modes = _blocked_t3_modes_from_learning_actions(learning_actions)
    learning_preflight = _as_report_only_learning_preflight(_resolve_learning_preflight_guard())
    print(
        f" 收盘学习闸门(汇报): {learning_preflight['status']} "
        f"| report_add={'pass' if learning_preflight.get('reported_allow_add_position') else 'block'} "
        f"| report_aggressive_add={'pass' if learning_preflight.get('reported_allow_aggressive_add') else 'hold'} "
        f"| effective_add=pass"
    )
    for note in learning_preflight.get('notes', []):
        print(f"  - {note}")
    if not learning_preflight.get('reported_allow_add_position', False):
        print(f" 收盘学习闸门报告为未放行，但当前配置不阻断加仓: {learning_preflight.get('reason', 'learning_gate_block')}")
    decision_reference = _build_selected_decision_reference(_read_jsonl(MODEL_DECISIONS_FILE, limit=5000))
    pos_map = _active_position_map(positions)
    pending_summary = summarize_pending_orders(pending_items)
    pending_buy_ctx = _active_pending_context_by_code(pending_items, action='buy')
    if pending_summary.get('active_buy_codes'):
        print(f" 当前存在未完成买单: {', '.join(pending_summary['active_buy_codes'])}")
    window_ctx = _resolve_add_position_window()
    window_tag = str(window_ctx.get('slot', '')).strip() or _current_add_position_window_tag()
    reserve_cash_ratio = _fnum(window_ctx.get('reserve_cash_ratio', ADD_POSITION_RESERVE_CASH_RATIO), ADD_POSITION_RESERVE_CASH_RATIO)
    non_aggressive_max_items = max(_inum(window_ctx.get('non_aggressive_max_items', ADD_POSITION_NON_AGGRESSIVE_MAX_ITEMS), ADD_POSITION_NON_AGGRESSIVE_MAX_ITEMS), 1)
    score_min = _fnum(window_ctx.get('score_min', 0.0), 0.0)
    aggressive_score_min = _fnum(window_ctx.get('aggressive_score_min', score_min + 1.0), score_min + 1.0)
    if window_tag:
        print(f" 加仓确认窗口: {window_tag} ({window_ctx.get('label', 'normal')})")

    if not holding:
        print(" 当前无主策略持仓需要加仓")
        return EXIT_NO_ACTION
    reserve_cash = max(
        round(avail * reserve_cash_ratio, 2),
        round(total_assets * ADD_POSITION_RESERVE_CASH_MIN_RATIO, 2),
    )
    print(f" 新机会预留现金: ¥{reserve_cash:,.0f}")
    # #region debug-point C:add-position-holding-ready
    _dbg_emit(
        'C',
        '[DEBUG] add_position holding snapshot ready',
        elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
        holding_count=len(holding),
        reserve_cash=round(reserve_cash, 2),
        avail=round(avail, 2),
    )
    # #endregion

    # 连接TDX检查信号衰减
    # #region debug-point A:add-position-connect-tdx-start
    _dbg_emit('A', '[DEBUG] add_position connect_tdx start', elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1))
    # #endregion
    tdx_api = connect_tdx()
    if tdx_api:
        print(f" TDX已连接（信号衰减检测）")
    # #region debug-point A:add-position-connect-tdx-done
    _dbg_emit(
        'A',
        '[DEBUG] add_position connect_tdx done',
        elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
        connected=bool(tdx_api),
    )
    # #endregion

    add_list = []
    skipped_yield_new = []
    non_aggressive_added = 0
    state_changed = False
    tradability_exclusions = _load_today_tradability_exclusions()
    for r in holding:
        code = r.get('code', '')
        name = r.get('name', '')
        tier = _inum(r.get('tier', 0), 0)
        entry_price = _fnum(r.get('entry_price', 0), 0.0)
        target_amount = _fnum(r.get('target_amount', 0), 0.0)
        mode = r.get('mode', '')
        learning_action = _resolve_learning_trade_action(code, mode, learning_actions)
        profit_pct = _fnum((pos_map.get(code) or {}).get('profit_pct', 0.0), 0.0)
        try:
            hold_days = (datetime.now() - datetime.strptime(r.get('date', ''), '%Y-%m-%d')).days
        except ValueError:
            hold_days = 999
        _dbg_item_t0 = _dbg_time.perf_counter()
        # #region debug-point C:add-position-loop-enter
        _dbg_emit(
            'C',
            '[DEBUG] add_position holding loop enter',
            elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
            code=str(code).zfill(6),
            mode=str(mode),
            hold_days=hold_days,
        )
        # #endregion

        # 当前持仓市值
        pos = pos_map.get(code)
        if not pos:
            print(f"  [SKIP] {code} {name} 跳过加仓: 当前无真实底仓")
            continue
        if hold_days < 1 or hold_days > ADD_POSITION_MAX_HOLD_DAYS:
            print(f"  [SKIP] {code} {name} 跳过加仓: 持仓{hold_days}天，不在T+1加仓窗口")
            continue
        if _is_legacy_holding_record(r) and _track_qty_mismatch(r, pos):
            print(
                f"  [SKIP] {code} {name} 跳过加仓: "
                f"账本数量{_inum(r.get('quantity', 0), 0)}与实仓{_inum(pos.get('count', 0), 0)}不一致"
            )
            continue
        exclusion = tradability_exclusions.get(str(code).zfill(6))
        if exclusion:
            print(f"  [SKIP] {code} {name} 今日不加仓: {exclusion_reason_text(exclusion)}")
            continue
        pending_ctx = pending_buy_ctx.get(str(code).zfill(6))
        reserved_qty = _inum((pending_ctx or {}).get('reserved_qty', 0), 0)
        if reserved_qty > 0:
            print(f"  [SKIP] {code} {name} 跳过加仓: 当前已有未完成买单占用{reserved_qty}股")
            continue
        current_value = pos['value']  # 当前持仓市值

        decision_row = _resolve_record_decision_row(r, decision_reference)
        market_regime = _normalize_market_regime(decision_row.get('market_regime', ''))
        target_plan = _resolve_add_position_target_amount(
            target_amount,
            record=r,
            mode_capital_profile=mode_capital_profile,
            market_regime=market_regime,
        )
        effective_target_amount = _fnum(target_plan.get('target_amount', target_amount), target_amount)
        capital_bias_note = str(target_plan.get('capital_note', '')).strip()
        regime_add_ratio = _fnum(learning_action.get('add_position_target_ratio', 1.0), 1.0)
        if regime_add_ratio < 0.999:
            effective_target_amount = round(effective_target_amount * regime_add_ratio, 2)
            regime_stage = str(learning_action.get('regime_stage', '')).strip()
            regime_note = f"风控正样本{regime_stage or 'defense_bias'}收缩目标仓位x{regime_add_ratio:.2f}"
            capital_bias_note = '; '.join([note for note in [capital_bias_note, regime_note] if note])
        aggressive_add_note = ''
        mode_profile = target_plan.get('mode_profile', {}) if isinstance(target_plan.get('mode_profile', {}), dict) else {}
        is_t3_observe_record = BIG_MEAT_BUY_POOL_OBSERVE_NOTE in str(r.get('build_note', '')).strip() or tier == 3
        if (
            is_t3_observe_record
            and mode in blocked_t3_modes
            and str(r.get('big_meat_state', '')).strip() != BIG_MEAT_STATE_CONFIRMED
        ):
            skipped_yield_new.append(f"{code}({mode} blocked_t3_mode)")
            print(f"  [SKIP] {code} {name} 跳过加仓: 该模式已被学习层列入 T3 证伪冷却")
            continue
        if capital_bias_note:
            print(f"   {code} {name} {capital_bias_note}")

        # 检查信号衰减——如果信号衰减就不加仓
        if tdx_api:
            # #region debug-point A:add-position-decay-start
            _dbg_emit(
                'A',
                '[DEBUG] add_position evaluate_signal_decay start',
                elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
                code=str(code).zfill(6),
            )
            # #endregion
            should_sell, decay_reason, decay_score = evaluate_signal_decay(
                tdx_api, code, entry_price, mode, profit_pct=profit_pct
            )
            # #region debug-point A:add-position-decay-done
            _dbg_emit(
                'A',
                '[DEBUG] add_position evaluate_signal_decay done',
                elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
                code=str(code).zfill(6),
                step_ms=round((_dbg_time.perf_counter() - _dbg_item_t0) * 1000, 1),
                should_sell=bool(should_sell),
                decay_reason=str(decay_reason),
                decay_score=_fnum(decay_score, 0.0),
            )
            # #endregion
            if should_sell:
                if str(r.get('big_meat_state', '')).strip():
                    r = _apply_big_meat_state(r, state='')
                    state_changed = True
                print(f"   {code} {name} 信号衰减({decay_reason})，不加仓")
                continue
            big_meat_profile = _build_big_meat_add_profile(
                tdx_api,
                code,
                record=r,
                profit_pct=profit_pct,
                decision_row=decision_row,
                window_tag=window_tag,
            )
            # #region debug-point A:add-position-big-meat-done
            _dbg_emit(
                'A',
                '[DEBUG] add_position big_meat_profile done',
                elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
                code=str(code).zfill(6),
                step_ms=round((_dbg_time.perf_counter() - _dbg_item_t0) * 1000, 1),
                enabled=bool(big_meat_profile),
            )
            # #endregion
            big_meat_transition = _resolve_big_meat_transition(
                r,
                big_meat_profile,
                score_min=score_min,
                aggressive_score_min=aggressive_score_min,
                window_tag=window_tag,
            )
            holding_big_meat_profile = _build_holding_big_meat_profile(
                r,
                profit_pct=profit_pct,
                add_profile=big_meat_profile,
                decision_row=decision_row,
            )
            profit_expansion_add_ok = bool(
                learning_action.get('allow_prelock_add')
                and _fnum(profit_pct, 0.0) >= LEARNING_BIG_MEAT_SUCCESS_PNL_PCT
            )
            late_bloom_promote = bool(holding_big_meat_profile.get('late_bloom_eligible'))
            if late_bloom_promote:
                big_meat_transition = {
                    **big_meat_transition,
                    'state': BIG_MEAT_STATE_CONFIRMED,
                    'allow_add': True,
                    'allow_aggressive_add': bool(
                        big_meat_transition.get('allow_aggressive_add')
                        or holding_big_meat_profile.get('dominant_winner')
                    ),
                    'reason': 'late_bloom_confirm',
                }
            allow_aggressive_add = bool(big_meat_transition.get('allow_aggressive_add')) and bool(
                learning_preflight.get('allow_aggressive_add', False)
            )
            prev_state_snapshot = _big_meat_record_snapshot(r)
            r = _apply_big_meat_state(
                r,
                state=str(big_meat_transition.get('state', '')).strip(),
                profile=big_meat_profile,
                reason='late_bloom_confirm' if late_bloom_promote else str(big_meat_profile.get('reason', '')).strip(),
                window_tag=window_tag,
            )
            r = _apply_holding_big_meat_profile(
                r,
                profile=holding_big_meat_profile,
                promote=late_bloom_promote and str(r.get('big_meat_state', '')).strip() == BIG_MEAT_STATE_CONFIRMED,
            )
            new_state_snapshot = _big_meat_record_snapshot(r)
            if new_state_snapshot != prev_state_snapshot:
                state_changed = True
            target_plan = _resolve_add_position_target_amount(
                target_amount,
                record=r,
                mode_capital_profile=mode_capital_profile,
                market_regime=market_regime,
                big_meat_profile=big_meat_profile if allow_aggressive_add else None,
            )
            effective_target_amount = _fnum(target_plan.get('target_amount', target_amount), target_amount)
            capital_bias_note = str(target_plan.get('capital_note', capital_bias_note)).strip()
            aggressive_add_note = str(target_plan.get('aggressive_add_note', '')).strip()
            mode_profile = target_plan.get('mode_profile', mode_profile) if isinstance(target_plan.get('mode_profile', mode_profile), dict) else mode_profile
            if bool(big_meat_transition.get('allow_aggressive_add')) and not bool(learning_preflight.get('allow_aggressive_add', False)):
                print(f"   {code} {name} 收盘学习闸门未放行激进加仓，回退为普通加仓")
            if not allow_aggressive_add:
                aggressive_add_note = ''
            if aggressive_add_note:
                print(f"   {code} {name} {aggressive_add_note}")
            elif _fnum(big_meat_profile.get('score', 0.0), 0.0) < score_min:
                skipped_yield_new.append(f"{code}({mode} score={_fnum(big_meat_profile.get('score', 0.0), 0.0):.1f})")
                print(
                    f"  [SKIP] {code} {name} 跳过加仓: "
                    f"大肉确认分{_fnum(big_meat_profile.get('score', 0.0), 0.0):.1f} < 窗口阈值{score_min:.1f}"
                )
                continue
            elif str(r.get('big_meat_state', '')).strip() == BIG_MEAT_STATE_CANDIDATE:
                if bool(holding_big_meat_profile.get('prelock_candidate')) or profit_expansion_add_ok:
                    print(
                        f"   {code} {name} 接近 late_bloom/利润扩张保护，放行保守加仓观察"
                    )
                else:
                    skipped_yield_new.append(f"{code}({mode} candidate)")
                    print(
                        f"  [WATCH] {code} {name} 进入大肉候选，等待后续窗口确认后再放大"
                    )
                    continue
            elif late_bloom_promote:
                print(f"   {code} {name} 持仓后发走强，升级为大肉确认并切入核心仓保护")
        if (
            learning_action.get('skip_add_position')
            and not bool(str(r.get('big_meat_state', '')).strip() == BIG_MEAT_STATE_CONFIRMED)
            and not bool(holding_big_meat_profile.get('prelock_candidate'))
            and not bool(profit_expansion_add_ok)
        ):
            skipped_yield_new.append(f"{code}({mode} learning_hold)")
            print(f"  [SKIP] {code} {name} 跳过加仓: {learning_action.get('reason', '学习层禁止继续培养')}")
            continue
        recent_adjustment = _resolve_recent_trade_selection_adjustment(code, recent_trade_memory, allow_block=False)
        if recent_adjustment.get('penalty', 0.0) > recent_adjustment.get('bonus', 0.0):
            extra_note = '/'.join(recent_adjustment.get('reasons', [])[:2])
            capital_bias_note = '; '.join([note for note in [capital_bias_note, extra_note] if note])
            if not aggressive_add_note:
                skipped_yield_new.append(f"{code}({mode} recent_penalty)")
                print(f"  [SKIP] {code} {name} 跳过加仓: 近期交易记忆为负，优先保留新机会")
                continue
        # 如果已达到或超过目标仓位，跳过
        if effective_target_amount <= 0 or current_value >= effective_target_amount * 0.95:
            print(
                f"  [SKIP] {code} {name} 已达目标仓位"
                f"(¥{current_value:,.0f}/¥{effective_target_amount:,.0f})"
            )
            continue

        # 计算加仓金额
        add_amount = effective_target_amount - current_value
        if add_amount <= 0:
            continue

        # 当前价格
        cur_price = pos['price']
        if cur_price <= 0:
            continue

        qty = calc_buy_quantity(cur_price, add_amount)
        if qty <= 0:
            continue

        cost = qty * cur_price
        mode_edge = _fnum((mode_profile or {}).get('edge_score', 0.0), 0.0)
        is_aggressive = bool(aggressive_add_note)
        aggressive_score = _fnum((big_meat_profile or {}).get('aggressive_score', 0.0), 0.0)
        if is_aggressive and aggressive_score < aggressive_score_min:
            is_aggressive = False
            aggressive_add_note = ''
        usable_avail = avail if is_aggressive else max(avail - reserve_cash, 0.0)
        if not is_aggressive and mode_edge <= ADD_POSITION_UNDERPERFORMING_MODE_SKIP_EDGE:
            skipped_yield_new.append(f"{code}({mode} edge={mode_edge:+.1f})")
            print(f"  [SKIP] {code} {name} 跳过加仓: 模式近期走弱，优先让位新机会")
            continue
        if not is_aggressive and non_aggressive_added >= non_aggressive_max_items:
            skipped_yield_new.append(f"{code}({mode} quota)")
            print(f"  [SKIP] {code} {name} 跳过加仓: 非激进加仓名额已满，优先保留尾盘新机会")
            continue
        if cost > usable_avail:
            qty = int(usable_avail / cur_price / 100) * 100
            if qty < 100:
                if not is_aggressive:
                    skipped_yield_new.append(f"{code}({mode} reserve)")
                    print(f"  [SKIP] {code} {name} 跳过加仓: 需保留新机会预留现金")
                continue
            cost = qty * cur_price

        add_list.append({
            'code': code,
            'name': name,
            'tier': tier,
            'price': cur_price,
            'quantity': qty,
            'cost': cost,
            'current_value': current_value,
            'target_amount': effective_target_amount,
            'base_target_amount': target_amount,
            'capital_bias_note': capital_bias_note,
            'aggressive_add_note': aggressive_add_note,
            'mode': mode,
            'is_aggressive': is_aggressive,
            'window_tag': window_tag,
            'big_meat_state': str(r.get('big_meat_state', '')).strip(),
            'big_meat_score': round(_fnum((big_meat_profile or {}).get('score', 0.0), 0.0), 2),
            'big_meat_reason': str(r.get('big_meat_reason', '')).strip(),
            'big_meat_window_tag': str(r.get('big_meat_window_tag', '')).strip(),
            'big_meat_last_eval_at': str(r.get('big_meat_last_eval_at', '')).strip(),
            'aggressive_score': round(aggressive_score, 2),
            'big_meat_aggressive_score': round(aggressive_score, 2),
        })
        avail -= cost
        if not is_aggressive:
            non_aggressive_added += 1
        # #region debug-point C:add-position-loop-candidate
        _dbg_emit(
            'C',
            '[DEBUG] add_position holding loop candidate ready',
            elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
            code=str(code).zfill(6),
            step_ms=round((_dbg_time.perf_counter() - _dbg_item_t0) * 1000, 1),
            add_count=len(add_list),
            quantity=_inum(qty, 0),
            cost=round(cost, 2),
            aggressive=bool(is_aggressive),
        )
        # #endregion

    if tdx_api:
        try:
            tdx_api.disconnect()
        except Exception:
            pass
    # #region debug-point C:add-position-loop-finished
    _dbg_emit(
        'C',
        '[DEBUG] add_position candidate build done',
        elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
        add_count=len(add_list),
        skipped_yield_new=len(skipped_yield_new),
        non_aggressive_added=non_aggressive_added,
    )
    # #endregion
    if state_changed and not dry_run:
        save_track_record(records)

    print(f"\n{'='*60}")
    print(f" V10加仓（T+1确认） {'[DRY RUN]' if dry_run else '[LIVE]'}")
    print(f" 可用资金: ¥{balance['avail_balance']:,.2f}")
    print(f"{'='*60}")

    if not add_list:
        print(" 当前持仓均无需加仓或未通过加仓条件")
        if skipped_yield_new:
            print(f" 新机会让位过滤: {', '.join(skipped_yield_new[:10])}")
        # #region debug-point D:add-position-no-action
        _dbg_emit(
            'D',
            '[DEBUG] add_position exits with no_action',
            elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
            skipped_yield_new=len(skipped_yield_new),
        )
        # #endregion
        return EXIT_NO_ACTION
    if skipped_yield_new:
        print(f" 新机会让位过滤: {', '.join(skipped_yield_new[:10])}")

    today = _market_today()
    success_count = 0
    first_pass_success_count = 0
    tail_retry_success_count = 0
    retry_tail_queue = []
    failed_add_items = []
    # #region debug-point D:add-position-order-loop-start
    _dbg_emit(
        'D',
        '[DEBUG] add_position order loop start',
        elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
        add_count=len(add_list),
        dry_run=bool(dry_run),
    )
    # #endregion
    for item in add_list:
        tier = item['tier']
        label = TIER_CONFIG[tier]['label']
        plan_notes = [str(item.get('capital_bias_note', '')).strip(), str(item.get('aggressive_add_note', '')).strip()]
        plan_suffix = f" [{' | '.join([note for note in plan_notes if note])}]" if any(plan_notes) else ''

        # 新分层下不再让短期重复信号直接改写 T2/T3 身份，只作为确认辅助信息。
        strength = _get_signal_strength(item['code'], 'buy')
        if strength >= 2:
            state = str(item.get('big_meat_state', '')).strip()
            if strength >= 4 and state == BIG_MEAT_STATE_CONFIRMED:
                print(f"   {item['code']} {item['name']} 信号强度={strength} (已确认大肉，继续按核心仓放大)")
            elif strength >= 4 and state == BIG_MEAT_STATE_CANDIDATE:
                print(f"   {item['code']} {item['name']} 信号强度={strength} (候选强化，但仍等待窗口确认)")
            else:
                print(f"   {item['code']} {item['name']} 信号强度={strength} (仅作确认辅助，不再直接升 tier)")

        if dry_run:
            print(f"   [DRY] {label} {item['code']} {item['name']} "
                  f"¥{item['price']:.2f} × {item['quantity']}股 "
                  f"≈¥{item['cost']:,.0f} (¥{item['current_value']:,.0f}→¥{item['target_amount']:,.0f}){plan_suffix}")
        else:
            print(f"   {label} {item['code']} {item['name']} "
                  f"¥{item['price']:.2f} × {item['quantity']}股 "
                  f"≈¥{item['cost']:,.0f} (¥{item['current_value']:,.0f}→¥{item['target_amount']:,.0f}){plan_suffix}")
            # #region debug-point D:add-position-execute-trade-start
            _dbg_emit(
                'D',
                '[DEBUG] add_position execute_trade_action start',
                elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
                code=str(item.get('code', '')).zfill(6),
                quantity=_inum(item.get('quantity', 0), 0),
                tier=_inum(tier, 0),
            )
            # #endregion
            trade_result = execute_trade_action(
                'buy',
                item['code'],
                item['quantity'],
                ref_price=item['price'],
                execution_phase='add_position',
                strategy_action='add_position',
            )
            # #region debug-point D:add-position-execute-trade-done
            _dbg_emit(
                'D',
                '[DEBUG] add_position execute_trade_action done',
                elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
                code=str(item.get('code', '')).zfill(6),
                success=bool(trade_result.get('success')),
                rate_limited=bool(is_rate_limited_trade_result(trade_result)),
                message=str(trade_result.get('message', ''))[:120],
            )
            # #endregion
            if trade_result['success']:
                success_count += 1
                first_pass_success_count += 1
                buy_apply = _apply_successful_buys(
                    [item['code']],
                    records=records,
                    balance=balance,
                    positions=positions,
                    trade_contexts={
                        str(item['code']).zfill(6): _build_buy_reconcile_context(
                            item,
                            today=today,
                            build_note='加仓至满仓',
                            tier=tier,
                            note_suffix='; '.join([
                                note for note in [
                                    str(item.get('capital_bias_note', '')).strip(),
                                    str(item.get('aggressive_add_note', '')).strip(),
                                    '加仓至满仓',
                                ] if note
                            ]),
                        )
                    },
                )
                records = buy_apply['records']
                balance = buy_apply['balance']
                positions = buy_apply['positions']
            elif is_rate_limited_trade_result(trade_result):
                enqueue_buy_tail_retry(retry_tail_queue, {
                    **item,
                    'entry_price': item['price'],
                })
            else:
                failed_add_items.append(item)

    if not dry_run and retry_tail_queue:
        # #region debug-point D:add-position-tail-retry-start
        _dbg_emit(
            'D',
            '[DEBUG] add_position tail retry start',
            elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
            retry_count=len(retry_tail_queue),
        )
        # #endregion
        records, balance, positions, success_count, tail_retry_success_count, tail_retry_failed_items = _run_add_position_tail_retry_queue(
            retry_tail_queue,
            records=records,
            balance=balance,
            positions=positions,
            success_count=success_count,
            today=today,
        )
        failed_add_items.extend(tail_retry_failed_items)

    total_add = sum(i['cost'] for i in add_list)
    capital_bias_items = [
        {
            'code': str(item.get('code', '')).zfill(6),
            'name': str(item.get('name', '')).strip(),
            'tier': _inum(item.get('tier', 0), 0),
            'mode': str(item.get('mode', '')).strip(),
            'base_target_amount': round(_fnum(item.get('base_target_amount', 0.0), 0.0), 2),
            'effective_target_amount': round(_fnum(item.get('target_amount', 0.0), 0.0), 2),
            'reason': str(item.get('capital_bias_note', '')).strip(),
        }
        for item in add_list
        if str(item.get('capital_bias_note', '')).strip()
    ]
    aggressive_add_items = [
        {
            'code': str(item.get('code', '')).zfill(6),
            'name': str(item.get('name', '')).strip(),
            'tier': _inum(item.get('tier', 0), 0),
            'mode': str(item.get('mode', '')).strip(),
            'base_target_amount': round(_fnum(item.get('base_target_amount', 0.0), 0.0), 2),
            'effective_target_amount': round(_fnum(item.get('target_amount', 0.0), 0.0), 2),
            'planned_add_amount': round(_fnum(item.get('cost', 0.0), 0.0), 2),
            'reason': str(item.get('aggressive_add_note', '')).strip(),
        }
        for item in add_list
        if str(item.get('aggressive_add_note', '')).strip()
    ]
    print(f"\n 预计加仓: ¥{total_add:,.0f} | 拟加仓: {len(add_list)} 只")
    print(
        f" 首轮成功: {first_pass_success_count} | "
        f"尾补成功: {tail_retry_success_count} | "
        f"未完成: {len(failed_add_items)}"
    )
    if failed_add_items:
        failed_codes = ', '.join(f"{item['code']} {item['name']}" for item in failed_add_items)
        print(f" [WARN] 加仓未完成 {len(failed_add_items)} 只: {failed_codes}")
    print(f"{'='*60}")
    if not dry_run:
        live_state = _refresh_live_artifact_state(records)
        records = live_state['records']
        write_account_artifacts(
            'add_position',
            records=records,
            balance=live_state['balance'],
            positions=live_state['positions'],
            pending_items=live_state['pending_items'],
            execution_result={
                'action': 'add_position',
                'planned_count': len(add_list),
                'planned_amount': round(total_add, 2),
                'first_pass_success_count': first_pass_success_count,
                'tail_retry_queued_count': len(retry_tail_queue),
                'tail_retry_success_count': tail_retry_success_count,
                'final_success_count': success_count,
                'failed_count': len(failed_add_items),
                'failed_codes': [str(item.get('code', '')).zfill(6) for item in failed_add_items],
                'failed_names': [str(item.get('name', '')).strip() for item in failed_add_items],
                'capital_bias_count': len(capital_bias_items),
                'capital_bias_codes': [item['code'] for item in capital_bias_items],
                'capital_bias_items': capital_bias_items,
                'aggressive_add_count': len(aggressive_add_items),
                'aggressive_add_codes': [item['code'] for item in aggressive_add_items],
                'aggressive_add_items': aggressive_add_items,
                'status': 'partial_failed' if failed_add_items else 'ok',
            },
        )
        if success_count <= 0 or failed_add_items:
            # #region debug-point D:add-position-exit-runtime-error
            _dbg_emit(
                'D',
                '[DEBUG] add_position exits with runtime_error',
                elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
                success_count=success_count,
                failed_count=len(failed_add_items),
            )
            # #endregion
            return EXIT_RUNTIME_ERROR
    # #region debug-point D:add-position-exit-ok
    _dbg_emit(
        'D',
        '[DEBUG] add_position exits',
        elapsed_ms=round((_dbg_time.perf_counter() - _dbg_t0) * 1000, 1),
        success_count=success_count,
        failed_count=len(failed_add_items),
        dry_run=bool(dry_run),
    )
    # #endregion
    return EXIT_OK if (dry_run or success_count > 0) else EXIT_NO_ACTION


def do_close_node():
    """收盘节点：全天复核复盘 + 学习放行。"""
    stage = 'start'
    started_at = time.perf_counter()
    def _emit_stage(checkpoint: str, **data):
        _main_strategy_chain_emit(
            'C',
            'v10_moni_trader.py:do_close_node',
            f'[DEBUG] close-node stage {checkpoint}',
            {
                'stage': stage,
                'elapsed_ms': round((time.perf_counter() - started_at) * 1000, 1),
                **data,
            },
        )
    # #region debug-point D:close-node-start
    _main_strategy_chain_emit(
        'C',
        'v10_moni_trader.py:do_close_node',
        '[DEBUG] close-node started',
        {'stage': stage},
    )
    # #endregion
    try:
        stage = 'collect_context'
        _emit_stage('collect_context:start')
        context = _collect_reconcile_context()
        _emit_stage(
            'collect_context:done',
            positions_count=len(context.get('positions') or []),
            records_count=len(context.get('records') or []),
            orders_count=len(context.get('orders') or []),
        )
        stage = 'write_account_artifacts'
        _emit_stage('write_account_artifacts:start')
        summary = write_account_artifacts(
            'report',
            balance=context['balance'],
            positions=context['positions'],
            records=context['records'],
        )
        summary['full_reconcile'] = context['reconcile_summary']
        _write_json_atomic(SUMMARY_FILE, summary)
        _emit_stage(
            'write_account_artifacts:done',
            account_live=bool((summary.get('account_status') or {}).get('live')),
            position_count=_inum(((summary.get('account') or {}).get('position_count', 0)), 0),
        )
        stage = 'build_daily_evolution_bundle'
        _emit_stage('build_daily_evolution_bundle:start')
        daily_evolution_bundle = _build_daily_evolution_bundle(
            summary=summary,
            records=context['records'],
            trade_date=_market_today(),
        )
        _emit_stage('build_daily_evolution_bundle:done')
        stage = 'attach_intraday_judgment_review'
        _emit_stage('attach_intraday_judgment_review:start')
        daily_evolution_bundle = _attach_intraday_judgment_review(
            daily_evolution_bundle,
            summary=summary,
            records=context['records'],
            positions=context['positions'],
        )
        _emit_stage('attach_intraday_judgment_review:done')
        stage = 'attach_regime_execution_review'
        _emit_stage('attach_regime_execution_review:start')
        daily_evolution_bundle = _attach_regime_execution_review(
            daily_evolution_bundle,
            summary=summary,
        )
        _append_regime_execution_history(
            daily_evolution_bundle.get('regime_execution_review', {}),
            source='close_node',
        )
        _emit_stage('attach_regime_execution_review:done')
        stage = 'build_learning_actions'
        _emit_stage('build_learning_actions:start')
        learning_actions = _build_learning_actions(
            daily_evolution_bundle,
            trade_date=_market_today(),
        )
        daily_evolution_bundle['learning_actions_summary'] = learning_actions.get('summary', {})
        _write_json_atomic(DAILY_EVOLUTION_BUNDLE_FILE, daily_evolution_bundle)
        _emit_stage(
            'build_learning_actions:done',
            learning_action_count=len((learning_actions.get('actions') or [])),
        )
        stage = 'build_close_payload'
        _emit_stage('build_close_payload:start')
        close_payload = _build_close_node_payload(
            summary=summary,
            reconcile_summary=context['reconcile_summary'],
            daily_evolution_bundle=daily_evolution_bundle,
        )
        close_payload['learning_actions_summary'] = learning_actions.get('summary', {})
        close_payload['learning_gate_basis']['regime_positive_sample_count'] = _inum(
            learning_actions.get('summary', {}).get('regime_positive_sample_count', 0),
            0,
        )
        close_payload['learning_gate_basis']['regime_bias_stage'] = str(
            learning_actions.get('summary', {}).get('regime_bias_stage', '')
        ).strip()
        close_payload['files'] = {
            'daily_evolution_bundle_file': DAILY_EVOLUTION_BUNDLE_FILE,
            'engineering_review_file': ENGINEERING_REVIEW_FILE,
            'trade_episode_history_file': TRADE_EPISODE_HISTORY_FILE,
            'learning_actions_file': LEARNING_ACTIONS_FILE,
            'regime_execution_history_file': REGIME_EXECUTION_HISTORY_FILE,
        }
        _write_json_atomic(TRADE_EPISODE_HISTORY_FILE, {
            'generated_at': _now_str(),
            'trade_date': datetime.now().strftime('%Y-%m-%d'),
            'summary': daily_evolution_bundle.get('history_summary', {}),
            'episodes': daily_evolution_bundle.get('trade_episode_history', []),
        })
        _write_json_atomic(LEARNING_ACTIONS_FILE, learning_actions)
        _write_json_atomic(CLOSE_NODE_FILE, close_payload)
        _write_json_atomic(ENGINEERING_REVIEW_FILE, close_payload.get('engineering_review', {}))
        _write_json_atomic(LEARNING_GATE_FILE, _build_learning_gate_payload(close_payload))
        _emit_stage(
            'build_close_payload:done',
            learning_gate_status=str(close_payload.get('learning_gate_status', '')).strip(),
            review_status=str(close_payload.get('review_status', '')).strip(),
        )
        # #region debug-point D:close-node-success
        _main_strategy_chain_emit(
            'D',
            'v10_moni_trader.py:do_close_node',
            '[DEBUG] close-node finished',
            {
                'stage': stage,
                'holding_count': _inum(((summary.get('performance') or {}).get('holding_count', 0)), 0),
                'closed_count': _inum(((summary.get('performance') or {}).get('closed_count', 0)), 0),
            },
        )
        # #endregion
        print(json.dumps(close_payload, ensure_ascii=False, indent=2))
        return EXIT_OK
    except Exception as exc:
        # #region debug-point D:close-node-failed
        _main_strategy_chain_emit(
            'D',
            'v10_moni_trader.py:do_close_node',
            '[DEBUG] close-node failed',
            {
                'stage': stage,
                'exc_type': type(exc).__name__,
                'exc_text': repr(exc),
            },
        )
        # #endregion
        raise


def do_midday_node():
    """午间节点：午盘事实复核 + 自动安全纠偏 + 形成下午放行建议。"""
    context = _collect_reconcile_context()
    review_payload = build_midday_review(
        balance=context['balance'],
        positions=context['positions'],
        orders=context['orders'],
        records=context['records'],
    )
    review_payload['full_reconcile'] = context['reconcile_summary']
    node_payload = _build_midday_node_payload(context=context, review_payload=review_payload, stage='midday_node')
    review_payload['node_status'] = {
        'review_status': node_payload['review_status'],
        'pm_gate_status': node_payload['pm_gate_status'],
        'blocked_buy_codes': node_payload['blocked_buy_codes'],
    }
    _write_json_atomic(MIDDAY_REVIEW_FILE, review_payload)
    _write_json_atomic(MIDDAY_NODE_FILE, node_payload)
    _write_pm_gate_payload(node_payload, file_path=PM_GATE_FILE)
    print(json.dumps(node_payload, ensure_ascii=False, indent=2))
    return EXIT_OK


def do_midday_gate():
    """午间节点最终放行门：13:00-13:05 快速复查并输出最终下午放行状态。"""
    context = _collect_reconcile_context()
    review_payload = build_midday_review(
        balance=context['balance'],
        positions=context['positions'],
        orders=context['orders'],
        records=context['records'],
    )
    review_payload['full_reconcile'] = context['reconcile_summary']
    gate_payload = _build_midday_node_payload(context=context, review_payload=review_payload, stage='pm_gate')
    _write_json_atomic(MIDDAY_GATE_FILE, gate_payload)
    _write_pm_gate_payload(gate_payload, file_path=PM_GATE_FILE)
    print(json.dumps(gate_payload, ensure_ascii=False, indent=2))
    return EXIT_OK


def do_report():
    """兼容旧入口：收盘节点。"""
    return do_close_node()


def do_midday_review():
    """兼容旧入口：午间节点。"""
    return do_midday_node()


def main():
    parser = argparse.ArgumentParser(description='V10 + mx-moni 模拟交易')
    parser.add_argument('--buy', action='store_true', help='按V10信号分批建仓（首仓不满仓）')
    parser.add_argument('--add-position', action='store_true',
                        help='T+1加仓：对未达满仓目标的持仓加仓')
    parser.add_argument('--sell', action='store_true', help='卖出T+5到期持仓（兜底）')
    parser.add_argument('--smart-sell', action='store_true',
                        help='智能卖出：信号衰减随时走人 + T+5兜底')
    parser.add_argument('--status', action='store_true', help='查看持仓和战绩')
    parser.add_argument('--midday-node', action='store_true', help='午间节点：事实复核、自动安全纠偏、下午放行')
    parser.add_argument('--midday-gate', action='store_true', help='午间节点最终放行门：13:00-13:05 快速复查')
    parser.add_argument('--midday-review', action='store_true', help='午间复盘：中场校准与下午观察清单')
    parser.add_argument('--close-node', action='store_true', help='收盘节点：全天复核复盘与学习放行')
    parser.add_argument('--report', action='store_true', help='生成账户摘要、NAV历史和学习循环报告')
    parser.add_argument('--dry-run', action='store_true', help='模拟运行，不实际下单')
    args = parser.parse_args()

    if not MX_APIKEY:
        print("[ERROR] MX_APIKEY 未配置")
        return EXIT_CONFIG_ERROR

    if args.buy:
        return do_buy(dry_run=args.dry_run)
    elif args.add_position:
        return do_add_position(dry_run=args.dry_run)
    elif args.smart_sell:
        return do_smart_sell(dry_run=args.dry_run)
    elif args.sell:
        return do_sell(dry_run=args.dry_run)
    elif args.midday_node:
        return do_midday_node()
    elif args.midday_gate:
        return do_midday_gate()
    elif args.midday_review:
        return do_midday_review()
    elif args.close_node:
        return do_close_node()
    elif args.report:
        return do_report()
    elif args.status:
        return do_status()
    else:
        parser.print_help()
        return EXIT_CONFIG_ERROR


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        #region debug-point H:buywatch-1450-fail-main-exc
        _debug_emit_event(
            'D',
            'v10_moni_trader.py:__main__',
            '[DEBUG] uncaught exception escaped main',
            {
                'argv': sys.argv,
                'cwd': os.getcwd(),
                'exc_type': type(exc).__name__,
                'exc_text': repr(exc),
            },
        )
        #endregion
        raise
