#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V10 自动化阶段执行器。

把 scanner / trader / email 串成可调度的阶段命令，便于 Windows Task Scheduler 使用。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from package_paths import DATA_DIR
from trading_calendar import CALENDAR_SOURCE, is_trading_day
from workbuddy_runtime import (
    BUILD_WORKBUDDY_DISTILL_DAILY_REVIEW_SCRIPT,
    REFRESH_DISTILL_PIPELINE_SCRIPT,
    RuntimeValidationError,
    WORKBUDDY_CANDIDATE_POOL_FILE,
    WORKBUDDY_DISTILL_DAILY_REVIEW_FILE,
    validate_candidate_pool_artifact,
    validate_opening_tradability_artifact,
    preflight_phase,
)


ROOT = Path(__file__).resolve().parent
PYTHON = os.environ.get('TLFZ_PYTHON_EXE') or sys.executable
STATUS_DIR = DATA_DIR / 'automation_status'
STATUS_LATEST = STATUS_DIR / 'latest_phase_status.json'
STATUS_HISTORY = STATUS_DIR / 'phase_history.csv'
STATUS_HISTORY_DETAILED = STATUS_DIR / 'phase_history_detailed.csv'
PREWARM_TIMING_SIGNAL = STATUS_DIR / 'prewarm_timing_signal.json'
OPENING_NODE_FILE = DATA_DIR / 'v10_opening_node_latest.json'
MIDDAY_INSPECTION_FILE = DATA_DIR / 'v10_midday_inspection_latest.json'
CLOSE_NODE_FILE = DATA_DIR / 'v10_close_node_latest.json'
CLOSE_NODE_MX_REVIEW_FILE = DATA_DIR / 'v10_close_node_mx_review_latest.json'
SECURITY_MASTER_FILE = DATA_DIR / 'security_master_latest.json'
OPENING_TRADABILITY_FILE = DATA_DIR / 'opening_tradability_latest.json'
EXTERNAL_MARKET_REVIEW_FILE = DATA_DIR / 'v10_external_market_review_latest.json'
MIDDAY_REVIEW_FILE = DATA_DIR / 'v10_midday_review_latest.json'
MIDDAY_NODE_FILE = DATA_DIR / 'v10_midday_node_latest.json'
MIDDAY_GATE_FILE = DATA_DIR / 'v10_midday_gate_latest.json'
PM_GATE_FILE = DATA_DIR / 'v10_pm_gate_status.json'
WORKBUDDY_LOCAL_REVIEW_FILE = DATA_DIR / 'workbuddy_local_review_latest.json'
WORKBUDDY_LEARNING_ADVICE_FILE = DATA_DIR / 'workbuddy_learning_advice_latest.json'
ACCOUNT_SUMMARY_FILE = DATA_DIR / 'v10_account_summary_latest.json'
DISTILL_DAILY_REVIEW_SCRIPT = BUILD_WORKBUDDY_DISTILL_DAILY_REVIEW_SCRIPT
DISTILL_DAILY_REVIEW_FILE = WORKBUDDY_DISTILL_DAILY_REVIEW_FILE
WATCH_RETRYABLE_CODES = {2, 3, 4, 124}
STEP_TIMEOUT_DEFAULT = 600
EXIT_WINDOW_SKIPPED = 2
EXIT_NO_SIGNAL = 10
EXIT_NO_ACTION = 11
LEGACY_HISTORY_FIELDS = [
    'generated_at', 'phase', 'step', 'attempt', 'status',
    'exit_code', 'detail', 'command', 'root', 'data_dir',
]
DETAILED_HISTORY_FIELDS = [
    'generated_at', 'run_id', 'task_name', 'trigger_slot',
    'phase', 'step', 'attempt', 'status', 'exit_code',
    'started_at', 'finished_at', 'duration_seconds',
    'detail', 'command', 'learning_action', 'learning_note',
    'decision_buffer_seconds', 'root', 'data_dir',
]
PREWARM_SLOW_THRESHOLD_SECONDS = 600
PREWARM_DECISION_BUFFER_SECONDS = 9 * 60
PREWARM_RECOMMENDED_START_SLOT = '14:25'
DECISION_TRIGGER_SLOT = '14:49'
STEP_TIMEOUTS = {
    ('opening-data', 'security_master_refresh.py'): 480,
    ('opening-data', 'external_market_review.py'): 360,
    ('workbuddy-refresh', 'refresh_distill_pipeline.py'): 180,
    ('workbuddy-refresh', 'mx_enrich_candidates.py'): 120,
    ('workbuddy-refresh', 'mx_event_review.py'): 120,
    ('workbuddy-refresh', 'mx_challenger_pool.py'): 120,
    ('workbuddy-refresh', 'mx_workbuddy_portfolio.py'): 120,
    ('workbuddy-buy', 'refresh_distill_pipeline.py'): 180,
    ('workbuddy-buy', 'workbuddy_local_challenger.py'): 180,
    ('workbuddy-sell', 'workbuddy_local_challenger.py'): 120,
    ('workbuddy-smart-sell', 'workbuddy_local_challenger.py'): 180,
    ('workbuddy-status', 'workbuddy_local_challenger.py'): 45,
    ('smart-sell', 'data_freshness_probe.py'): 20,
    ('prewarm', 'scanner_v10.py'): 900,
    ('prewarm', 'data_freshness_probe.py'): 20,
    ('decision', 'scanner_v10.py'): 300,
    ('decision', 'data_freshness_probe.py'): 20,
    ('buy', 'data_freshness_probe.py'): 20,
    ('buy', 'v10_moni_trader.py'): 180,
    ('add-position', 'v10_moni_trader.py'): 240,
    ('smart-sell', 'v10_moni_trader.py'): 300,
    ('sell', 'v10_moni_trader.py'): 240,
    ('status', 'v10_moni_trader.py'): 180,
    ('midday-node', 'v10_moni_trader.py'): 240,
    ('midday-gate', 'v10_moni_trader.py'): 300,
    ('close-node', 'v10_moni_trader.py'): 420,
    ('close-node', 'external_market_review.py'): 360,
    ('close-node', 'mx_enrich_candidates.py'): 300,
    ('close-node', 'mx_event_review.py'): 300,
    ('close-node', 'mx_challenger_pool.py'): 300,
    ('close-node', 'mx_workbuddy_portfolio.py'): 300,
    ('close-node', 'workbuddy_local_review.py'): 120,
    ('close-node', 'build_workbuddy_distill_daily_review.py'): 120,
    ('close-node', 'workbuddy_learning_bridge.py'): 120,
    ('close-node', 'update_curve_observatory.py'): 180,
    ('close-node', 'send_email.py'): 120,
    ('prewarm', 'send_email.py'): 120,
    ('decision', 'send_email.py'): 120,
}
TRADING_DAY_ONLY_PHASES = {
    'opening-data',
    'workbuddy-refresh',
    'workbuddy-buy',
    'workbuddy-sell',
    'workbuddy-smart-sell',
    'workbuddy-status',
    'prewarm',
    'decision',
    'buy',
    'add-position',
    'smart-sell',
    'sell',
    'midday-node',
    'midday-gate',
    'close-node',
}
PHASE_HARD_DEADLINES = {
    'midday-gate': (13, 5),
}
CLOSE_NODE_OPTIONAL_STEPS = {
    'mx_enrich_candidates.py',
    'mx_event_review.py',
    'mx_challenger_pool.py',
    'mx_workbuddy_portfolio.py',
    'workbuddy_local_review.py',
    'build_workbuddy_distill_daily_review.py',
    'workbuddy_learning_bridge.py',
}
WORKBUDDY_REFRESH_OPTIONAL_STEPS = {
    'mx_enrich_candidates.py',
    'mx_event_review.py',
}
OBSERVE_ONLY_OPTIONAL_STEPS = {
    'data_freshness_probe.py',
}
RUN_META_ARG_STEPS = {
    'data_freshness_probe.py',
    'workbuddy_local_challenger.py',
    'workbuddy_local_review.py',
    'workbuddy_learning_bridge.py',
    'build_workbuddy_distill_daily_review.py',
    'external_market_review.py',
}


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + '.tmp')
    with tmp_path.open('w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open('r', encoding='utf-8') as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def normalize_step_name(step_path: str) -> str:
    return Path(step_path).name


def sanitize_token(value: str) -> str:
    cleaned = re.sub(r'[^A-Za-z0-9]+', '-', value.strip()).strip('-')
    return cleaned.lower() or 'manual'


def format_timestamp(value: datetime | None) -> str:
    return value.strftime('%Y-%m-%d %H:%M:%S') if value else ''


def seconds_between(started_at: datetime | None, finished_at: datetime | None) -> int | None:
    if not started_at or not finished_at:
        return None
    return max(0, int((finished_at - started_at).total_seconds()))


def latest_phase_path(phase: str) -> Path:
    return STATUS_DIR / f'latest_{sanitize_token(phase)}_status.json'


def payload_date(payload: dict, *keys: str) -> str:
    if not isinstance(payload, dict):
        return ''
    for key in keys:
        text = str(payload.get(key, '')).strip()
        if text:
            return text[:10]
    return ''


def summarize_detail_file(path: Path, *, date_keys: tuple[str, ...], extra_keys: tuple[str, ...] = ()) -> dict[str, object]:
    payload = read_json(path)
    date_value = payload_date(payload, *date_keys)
    summary: dict[str, object] = {
        'exists': bool(payload),
        'trade_date': date_value,
        'generated_at': str(payload.get('generated_at', '')).strip() if isinstance(payload, dict) else '',
        'is_today': bool(date_value and date_value == datetime.now().strftime('%Y-%m-%d')),
        'detail_file': str(path),
    }
    for key in extra_keys:
        summary[key] = payload.get(key) if isinstance(payload, dict) else None
    return summary


def build_opening_node_snapshot(
    *,
    run_meta: dict[str, str],
    phase_status: str,
    phase_exit_code: int,
) -> dict[str, object]:
    opening = summarize_detail_file(
        OPENING_TRADABILITY_FILE,
        date_keys=('trade_date', 'date'),
        extra_keys=('record_count', 'excluded_today_count', 'review_only_count'),
    )
    security_master = summarize_detail_file(
        SECURITY_MASTER_FILE,
        date_keys=('trade_date', 'date'),
        extra_keys=('record_count',),
    )
    external_market = summarize_detail_file(
        EXTERNAL_MARKET_REVIEW_FILE,
        date_keys=('trade_date', 'date'),
        extra_keys=('window_tag', 'risk_level', 'a_share_bias', 'impact_summary'),
    )
    checklist = {
        'opening_data_phase_finished': phase_status in {'ok', 'no_action', 'no_signal', 'skipped'},
        'opening_tradability_today': bool(opening['is_today']),
        'security_master_today': bool(security_master['is_today']),
        'external_market_review_today': bool(external_market['is_today']),
    }
    notes: list[str] = []
    if checklist['opening_tradability_today']:
        notes.append('09:31 opening-data 已落盘，可直接复核晨间流动性门。')
    else:
        notes.append('opening_tradability_latest.json 不是今日版本或缺失。')
    if not checklist['security_master_today']:
        notes.append('security_master_latest.json 未确认是今日版本。')
    if not checklist['external_market_review_today']:
        notes.append('v10_external_market_review_latest.json 未确认是今日版本。')
    if phase_status == 'ok' and all(checklist.values()):
        node_status = 'ok'
    elif phase_status in {'ok', 'no_action', 'no_signal', 'skipped'}:
        node_status = 'warning'
    else:
        node_status = 'failed'
    return {
        'generated_at': format_timestamp(datetime.now()),
        'trade_date': datetime.now().strftime('%Y-%m-%d'),
        'node': 'opening_node',
        'run_id': run_meta['run_id'],
        'task_name': run_meta['task_name'],
        'trigger_slot': run_meta['trigger_slot'],
        'phase_status': phase_status,
        'phase_exit_code': phase_exit_code,
        'node_status': node_status,
        'checklist': checklist,
        'opening_tradability': opening,
        'security_master': security_master,
        'external_market_review': external_market,
        'summary': {
            'record_count': int(opening.get('record_count', 0) or 0),
            'excluded_today_count': int(opening.get('excluded_today_count', 0) or 0),
            'review_only_count': int(opening.get('review_only_count', 0) or 0),
            'window_tag': str(external_market.get('window_tag', '') or ''),
            'risk_level': str(external_market.get('risk_level', '') or ''),
            'a_share_bias': str(external_market.get('a_share_bias', '') or ''),
        },
        'notes': notes,
    }


def build_midday_inspection_snapshot(
    *,
    run_meta: dict[str, str],
    phase: str,
    phase_status: str,
    phase_exit_code: int,
) -> dict[str, object]:
    midday_review = summarize_detail_file(
        MIDDAY_REVIEW_FILE,
        date_keys=('trade_date', 'date'),
        extra_keys=('market_temperature',),
    )
    midday_node = summarize_detail_file(
        MIDDAY_NODE_FILE,
        date_keys=('trade_date', 'date'),
        extra_keys=('stage', 'review_status', 'pm_gate_status', 'blocked_buy_codes'),
    )
    midday_gate = summarize_detail_file(
        MIDDAY_GATE_FILE,
        date_keys=('trade_date', 'date'),
        extra_keys=('stage', 'review_status', 'pm_gate_status', 'blocked_buy_codes'),
    )
    pm_gate = summarize_detail_file(
        PM_GATE_FILE,
        date_keys=('trade_date', 'date'),
        extra_keys=('stage', 'review_status', 'pm_gate_status', 'blocked_buy_codes', 'reason_codes'),
    )
    account_summary_payload = read_json(ACCOUNT_SUMMARY_FILE)
    latest_execution_result = account_summary_payload.get('latest_execution_result', {}) if isinstance(account_summary_payload, dict) else {}
    account_summary = summarize_detail_file(
        ACCOUNT_SUMMARY_FILE,
        date_keys=('trade_date', 'date'),
    )
    account_summary['latest_execution_action'] = str(latest_execution_result.get('action', '')).strip() if isinstance(latest_execution_result, dict) else ''
    account_summary['latest_execution_status'] = str(latest_execution_result.get('status', '')).strip() if isinstance(latest_execution_result, dict) else ''
    current_stage_ready = bool(midday_node['is_today']) if phase == 'midday-node' else bool(midday_gate['is_today'] and pm_gate['is_today'])
    checklist = {
        'midday_review_today': bool(midday_review['is_today']),
        'midday_node_today': bool(midday_node['is_today']),
        'midday_gate_today': bool(midday_gate['is_today']),
        'pm_gate_today': bool(pm_gate['is_today']),
        'current_stage_ready': current_stage_ready,
    }
    notes: list[str] = []
    if phase == 'midday-node':
        notes.append('11:35 午间节点已落盘，当前巡检用于事实复核与下午放行建议。')
    else:
        notes.append('13:00 午盘闸门已落盘，当前巡检用于最终下午放行确认。')
    if not checklist['midday_review_today']:
        notes.append('v10_midday_review_latest.json 未确认是今日版本。')
    if phase == 'midday-gate' and not checklist['pm_gate_today']:
        notes.append('v10_pm_gate_status.json 未确认是今日版本。')
    if phase_status == 'ok' and current_stage_ready:
        inspection_status = 'ok'
    elif phase_status in {'ok', 'no_action', 'no_signal', 'skipped'}:
        inspection_status = 'warning'
    else:
        inspection_status = 'failed'
    return {
        'generated_at': format_timestamp(datetime.now()),
        'trade_date': datetime.now().strftime('%Y-%m-%d'),
        'node': 'midday_inspection',
        'stage': phase,
        'run_id': run_meta['run_id'],
        'task_name': run_meta['task_name'],
        'trigger_slot': run_meta['trigger_slot'],
        'phase_status': phase_status,
        'phase_exit_code': phase_exit_code,
        'inspection_status': inspection_status,
        'checklist': checklist,
        'midday_review': midday_review,
        'midday_node': midday_node,
        'midday_gate': midday_gate,
        'pm_gate': pm_gate,
        'account_summary': account_summary,
        'notes': notes,
    }


def write_phase_inspection_snapshot(
    *,
    run_meta: dict[str, str],
    phase: str,
    phase_status: str,
    phase_exit_code: int,
) -> Path | None:
    if phase == 'opening-data':
        payload = build_opening_node_snapshot(
            run_meta=run_meta,
            phase_status=phase_status,
            phase_exit_code=phase_exit_code,
        )
        write_json_atomic(OPENING_NODE_FILE, payload)
        return OPENING_NODE_FILE
    if phase in {'midday-node', 'midday-gate'}:
        payload = build_midday_inspection_snapshot(
            run_meta=run_meta,
            phase=phase,
            phase_status=phase_status,
            phase_exit_code=phase_exit_code,
        )
        write_json_atomic(MIDDAY_INSPECTION_FILE, payload)
        return MIDDAY_INSPECTION_FILE
    return None


def is_soft_fail_step(phase: str, step_name: str) -> bool:
    if step_name in OBSERVE_ONLY_OPTIONAL_STEPS:
        return True
    if phase == 'workbuddy-refresh' and step_name in WORKBUDDY_REFRESH_OPTIONAL_STEPS:
        return True
    return phase == 'close-node' and step_name in CLOSE_NODE_OPTIONAL_STEPS


def write_close_node_mx_review(
    *,
    run_meta: dict[str, str],
    mx_step_results: list[dict[str, object]],
    phase_status: str,
) -> dict[str, object]:
    if not mx_step_results:
        return {}
    failed_steps = [item for item in mx_step_results if item.get('status') != 'ok']
    success_steps = [item for item in mx_step_results if item.get('status') == 'ok']
    if not failed_steps:
        review_status = 'ok'
        note = 'MX 收盘增强链路运行正常，未出现容错降级。'
    elif success_steps:
        review_status = 'warning'
        note = '部分 MX 步骤失败但已按 soft-fail 降级处理，未阻断收盘节点后续复核。'
    else:
        review_status = 'degraded'
        note = 'MX 收盘增强步骤全部失败，但收盘主复核与后续观测更新已继续执行。'
    payload = {
        'generated_at': format_timestamp(datetime.now()),
        'date': datetime.now().strftime('%Y-%m-%d'),
        'node': 'close_node',
        'run_id': run_meta['run_id'],
        'task_name': run_meta['task_name'],
        'trigger_slot': run_meta['trigger_slot'],
        'mx_review_status': review_status,
        'phase_status': phase_status,
        'mx_error_count': len(failed_steps),
        'mx_success_count': len(success_steps),
        'mx_failed_steps': [
            {
                'step': item.get('step', ''),
                'exit_code': item.get('exit_code', ''),
                'detail': item.get('detail', ''),
                'started_at': item.get('started_at', ''),
                'finished_at': item.get('finished_at', ''),
                'duration_seconds': item.get('duration_seconds', ''),
            }
            for item in failed_steps
        ],
        'mx_step_results': mx_step_results,
        'mx_observation_note': note,
        'notes': [
            '该文件属于收盘节点复核层观察产物，用于记录 MX 收盘增强链路的容错表现。',
            'MX 脚本失败不会阻断 close-node 后续步骤，但会在这里留下失败证据供复核层审阅。',
        ],
    }
    write_json_atomic(CLOSE_NODE_MX_REVIEW_FILE, payload)
    return payload


def merge_close_node_mx_review(mx_review_payload: dict[str, object]) -> None:
    if not mx_review_payload:
        return
    close_payload = read_json(CLOSE_NODE_FILE)
    if not close_payload:
        return
    notes = close_payload.get('notes', [])
    if not isinstance(notes, list):
        notes = []
    mx_note = 'MX 收盘增强链路摘要已并入收盘节点总入口，详细失败证据见 v10_close_node_mx_review_latest.json。'
    if mx_note not in notes:
        notes.append(mx_note)
    close_payload['notes'] = notes
    close_payload['mx_review'] = {
        'mx_review_status': mx_review_payload.get('mx_review_status', ''),
        'phase_status': mx_review_payload.get('phase_status', ''),
        'mx_error_count': mx_review_payload.get('mx_error_count', 0),
        'mx_success_count': mx_review_payload.get('mx_success_count', 0),
        'mx_failed_steps': mx_review_payload.get('mx_failed_steps', []),
        'mx_observation_note': mx_review_payload.get('mx_observation_note', ''),
        'detail_file': str(CLOSE_NODE_MX_REVIEW_FILE),
    }
    write_json_atomic(CLOSE_NODE_FILE, close_payload)


def merge_close_node_opening_data_review() -> None:
    close_payload = read_json(CLOSE_NODE_FILE)
    if not close_payload:
        return
    opening_payload = read_json(OPENING_TRADABILITY_FILE)
    notes = close_payload.get('notes', [])
    if not isinstance(notes, list):
        notes = []
    opening_note = '09:31 晨间数据任务摘要已并入收盘节点总入口，详细结果见 opening_tradability_latest.json。'
    if opening_note not in notes:
        notes.append(opening_note)
    close_payload['notes'] = notes

    if not opening_payload:
        close_payload['opening_data_review'] = {
            'opening_data_status': 'missing',
            'trade_date': '',
            'generated_at': '',
            'record_count': 0,
            'excluded_today_count': 0,
            'review_only_count': 0,
            'excluded_today_codes': [],
            'unsupported_market_codes': [],
            'today_gate_effective': False,
            'opening_observation_note': '未找到 09:31 晨间数据任务结果，复核层暂无法引用当天流动性门摘要。',
            'detail_file': str(OPENING_TRADABILITY_FILE),
        }
        write_json_atomic(CLOSE_NODE_FILE, close_payload)
        return

    records = opening_payload.get('records', [])
    if not isinstance(records, list):
        records = []
    unsupported_market_codes = [
        str(item.get('code', '')).strip()
        for item in records
        if str(item.get('tradability_status', '')).strip() == 'review_today_unsupported_market'
        and str(item.get('code', '')).strip()
    ]
    trade_date = str(opening_payload.get('trade_date', '')).strip()
    today = datetime.now().strftime('%Y-%m-%d')
    is_current = trade_date == today
    status = 'ok' if is_current else 'stale'
    note = (
        '晨间数据任务结果已并入收盘复核，可回看当日 09:31 的市场映射与流动性门结论。'
        if is_current else
        '晨间数据任务结果不是今日版本，收盘复核仅保留为历史参考。'
    )
    close_payload['opening_data_review'] = {
        'opening_data_status': status,
        'trade_date': trade_date,
        'generated_at': opening_payload.get('generated_at', ''),
        'record_count': opening_payload.get('record_count', 0),
        'excluded_today_count': opening_payload.get('excluded_today_count', 0),
        'review_only_count': opening_payload.get('review_only_count', 0),
        'excluded_today_codes': opening_payload.get('excluded_today_codes', []),
        'unsupported_market_codes': unsupported_market_codes,
        'today_gate_effective': is_current,
        'opening_observation_note': note,
        'detail_file': str(OPENING_TRADABILITY_FILE),
    }
    write_json_atomic(CLOSE_NODE_FILE, close_payload)


def merge_close_node_external_market_review() -> None:
    close_payload = read_json(CLOSE_NODE_FILE)
    if not close_payload:
        return
    review_payload = read_json(EXTERNAL_MARKET_REVIEW_FILE)
    notes = close_payload.get('notes', [])
    if not isinstance(notes, list):
        notes = []
    review_note = '盘前多源资讯复核摘要已并入收盘节点总入口，详细结果见 v10_external_market_review_latest.json。'
    if review_note not in notes:
        notes.append(review_note)
    close_payload['notes'] = notes
    if not review_payload:
        close_payload['external_market_review'] = {
            'status': 'missing',
            'trade_date': '',
            'generated_at': '',
            'window_tag': '',
            'risk_level': '',
            'a_share_bias': '',
            'negative_sectors': [],
            'positive_sectors': [],
            'impact_summary': '未找到盘前多源资讯复核文件，收盘节点无法引用隔夜/新闻联播/政策产业情报。',
            'detail_file': str(EXTERNAL_MARKET_REVIEW_FILE),
        }
        write_json_atomic(CLOSE_NODE_FILE, close_payload)
        return
    close_payload['external_market_review'] = {
        'status': 'ok',
        'trade_date': str(review_payload.get('trade_date', '')).strip(),
        'generated_at': str(review_payload.get('generated_at', '')).strip(),
        'window_tag': str(review_payload.get('window_tag', '')).strip(),
        'risk_level': str(review_payload.get('risk_level', '')).strip(),
        'a_share_bias': str(review_payload.get('a_share_bias', '')).strip(),
        'confidence': float(review_payload.get('confidence', 0.0) or 0.0),
        'negative_sectors': review_payload.get('negative_sectors', []),
        'positive_sectors': review_payload.get('positive_sectors', []),
        'negative_flags': review_payload.get('negative_flags', []),
        'positive_flags': review_payload.get('positive_flags', []),
        'impact_summary': str(review_payload.get('impact_summary', '')).strip(),
        'detail_file': str(EXTERNAL_MARKET_REVIEW_FILE),
    }
    write_json_atomic(CLOSE_NODE_FILE, close_payload)


def merge_close_node_workbuddy_local_review() -> None:
    close_payload = read_json(CLOSE_NODE_FILE)
    if not close_payload:
        return
    review_payload = read_json(WORKBUDDY_LOCAL_REVIEW_FILE)
    learning_payload = read_json(WORKBUDDY_LEARNING_ADVICE_FILE)
    notes = close_payload.get('notes', [])
    if not isinstance(notes, list):
        notes = []
    review_note = 'Workbuddy 本地 challenger 复核与学习桥接摘要已并入收盘节点总入口。'
    if review_note not in notes:
        notes.append(review_note)
    close_payload['notes'] = notes
    close_payload['workbuddy_local_review'] = {
        'review_verdict': str(review_payload.get('review_verdict', '')).strip(),
        'learning_sample_ready': bool(review_payload.get('learning_sample_ready', False)),
        'source_trade_date': str((review_payload.get('source_alignment') or {}).get('source_trade_date', '')).strip(),
        'today_order_count': int((review_payload.get('execution_health') or {}).get('today_order_count', 0) or 0),
        'closed_trade_count': int((review_payload.get('trade_quality') or {}).get('closed_trade_count', 0) or 0),
        'closed_trade_win_rate_pct': float((review_payload.get('trade_quality') or {}).get('closed_trade_win_rate_pct', 0.0) or 0.0),
        'detail_file': str(WORKBUDDY_LOCAL_REVIEW_FILE),
    }
    close_payload['workbuddy_learning_bridge'] = {
        'adoption_verdict': str(learning_payload.get('adoption_verdict', '')).strip(),
        'adoption_reason': str(learning_payload.get('adoption_reason', '')).strip(),
        'recommended_action': str(learning_payload.get('recommended_action', '')).strip(),
        'detail_file': str(WORKBUDDY_LEARNING_ADVICE_FILE),
    }
    write_json_atomic(CLOSE_NODE_FILE, close_payload)


def merge_close_node_distill_daily_review() -> None:
    close_payload = read_json(CLOSE_NODE_FILE)
    if not close_payload:
        return
    review_payload = read_json(DISTILL_DAILY_REVIEW_FILE)
    notes = close_payload.get('notes', [])
    if not isinstance(notes, list):
        notes = []
    review_note = '蒸馏每日自动复核报告摘要已并入收盘节点总入口。'
    if review_note not in notes:
        notes.append(review_note)
    close_payload['notes'] = notes
    if not review_payload:
        close_payload['workbuddy_distill_daily_review'] = {
            'review_status': 'missing',
            'template_name': '',
            'pool_trade_date': '',
            'forward_available': False,
            'execution_available': False,
            'detail_file': str(DISTILL_DAILY_REVIEW_FILE),
            'observation_note': '未找到蒸馏每日自动复核报告，收盘节点暂无法引用模板值/前向验证/实际执行三栏对照。',
        }
        write_json_atomic(CLOSE_NODE_FILE, close_payload)
        return

    template_section = review_payload.get('template_metrics', {})
    forward_section = review_payload.get('forward_validation', {})
    execution_section = review_payload.get('actual_execution', {})
    gaps = review_payload.get('gaps', {})
    alignment = review_payload.get('alignment', {})
    close_payload['workbuddy_distill_daily_review'] = {
        'review_status': 'ok',
        'template_name': str(template_section.get('template_name', '')).strip(),
        'pool_trade_date': str(alignment.get('pool_trade_date', '')).strip(),
        'forward_available': bool(forward_section.get('available', False)),
        'forward_source_trade_date': str(forward_section.get('source_trade_date', '')).strip(),
        'forward_evaluation_trade_date': str(forward_section.get('evaluation_trade_date', '')).strip(),
        'forward_win_rate_pct': float(forward_section.get('win_rate_pct', 0.0) or 0.0),
        'forward_avg_return_pct': float(forward_section.get('avg_return_pct', 0.0) or 0.0),
        'execution_available': bool(execution_section.get('available', False)),
        'execution_closed_trade_count': int(execution_section.get('closed_trade_count', 0) or 0),
        'execution_closed_trade_win_rate_pct': float(execution_section.get('closed_trade_win_rate_pct', 0.0) or 0.0),
        'execution_avg_closed_return_pct': float(execution_section.get('avg_closed_return_pct', 0.0) or 0.0),
        'template_vs_forward_win_rate_pct_gap': gaps.get('template_vs_forward_win_rate_pct_gap'),
        'template_vs_forward_avg_return_pct_gap': gaps.get('template_vs_forward_avg_return_pct_gap'),
        'forward_vs_execution_win_rate_pct_gap': gaps.get('forward_vs_execution_win_rate_pct_gap'),
        'forward_vs_execution_avg_return_pct_gap': gaps.get('forward_vs_execution_avg_return_pct_gap'),
        'alignment_notes': alignment.get('notes', []),
        'detail_file': str(DISTILL_DAILY_REVIEW_FILE),
    }
    write_json_atomic(CLOSE_NODE_FILE, close_payload)


def build_run_id(*, phase: str, task_name: str, trigger_slot: str) -> str:
    date_part = datetime.now().strftime('%Y%m%d')
    slot_part = sanitize_token(trigger_slot.replace(':', '') if trigger_slot else datetime.now().strftime('%H%M%S'))
    task_part = sanitize_token(task_name or 'manual-task')
    phase_part = sanitize_token(phase)
    return f'{date_part}-{slot_part}-{task_part}-{phase_part}'


def canonical_phase_name(phase: str) -> str:
    mapping = {
        'buy-watch': 'buy',
        'midday-review': 'midday-node',
        'report': 'close-node',
    }
    return mapping.get(phase, phase)


def parse_hhmm(slot: str) -> tuple[int, int] | None:
    try:
        hour, minute = slot.split(':', 1)
        return int(hour), int(minute)
    except (AttributeError, ValueError):
        return None


def evaluate_prewarm_timing(
    *,
    run_meta: dict[str, str],
    status: str,
    started_at: datetime | None,
    finished_at: datetime | None,
) -> dict[str, str | int | bool]:
    duration_seconds = seconds_between(started_at, finished_at)
    decision_slot = parse_hhmm(DECISION_TRIGGER_SLOT)
    buffer_seconds = None
    if status == 'ok' and finished_at and decision_slot:
        decision_at = finished_at.replace(hour=decision_slot[0], minute=decision_slot[1], second=0, microsecond=0)
        buffer_seconds = int((decision_at - finished_at).total_seconds())
    should_move_earlier = (
        status == 'ok'
        and (
            (duration_seconds is not None and duration_seconds >= PREWARM_SLOW_THRESHOLD_SECONDS)
            or (buffer_seconds is not None and buffer_seconds < PREWARM_DECISION_BUFFER_SECONDS)
        )
    )
    if status == 'skipped':
        note = 'non-trading day skipped'
        action = 'keep'
    elif status != 'ok':
        note = 'prewarm did not finish cleanly, manual review required'
        action = 'review'
    elif should_move_earlier:
        note = (
            f'prewarm completed too close to decision or ran too long; '
            f'suggest moving start from {run_meta["trigger_slot"] or "14:30"} to {PREWARM_RECOMMENDED_START_SLOT}'
        )
        action = 'suggest_move_to_14_25'
    else:
        note = 'current prewarm window is sufficient'
        action = 'keep'
    payload = {
        'generated_at': format_timestamp(datetime.now()),
        'run_id': run_meta['run_id'],
        'task_name': run_meta['task_name'],
        'phase': 'prewarm',
        'status': status,
        'current_trigger_slot': run_meta['trigger_slot'],
        'recommended_trigger_slot': PREWARM_RECOMMENDED_START_SLOT if should_move_earlier else (run_meta['trigger_slot'] or '14:30'),
        'started_at': format_timestamp(started_at),
        'finished_at': format_timestamp(finished_at),
        'duration_seconds': duration_seconds if duration_seconds is not None else '',
        'decision_trigger_slot': DECISION_TRIGGER_SLOT,
        'decision_buffer_seconds': buffer_seconds if buffer_seconds is not None else '',
        'should_move_earlier': should_move_earlier,
        'learning_action': action,
        'learning_note': note,
    }
    write_json_atomic(PREWARM_TIMING_SIGNAL, payload)
    return payload


def append_history(path: Path, fieldnames: list[str], row: dict) -> None:
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    target_path = path
    exists = target_path.exists()
    if exists:
        try:
            header = target_path.read_text(encoding='utf-8-sig').splitlines()[:1]
            current = header[0].split(',') if header else []
            if current != fieldnames:
                target_path = target_path.with_name(f'{target_path.stem}_v2{target_path.suffix}')
                exists = target_path.exists()
        except OSError:
            exists = False
    mode = 'a' if exists else 'w'
    with target_path.open(mode, encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def record_status(
    *,
    run_meta: dict[str, str],
    phase: str,
    step: str,
    attempt: int,
    status: str,
    exit_code: int,
    detail: str,
    command: str = '',
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    learning_action: str = '',
    learning_note: str = '',
    decision_buffer_seconds: int | None = None,
) -> None:
    duration_seconds = seconds_between(started_at, finished_at)
    payload = {
        'generated_at': format_timestamp(datetime.now()),
        'run_id': run_meta['run_id'],
        'task_name': run_meta['task_name'],
        'trigger_slot': run_meta['trigger_slot'],
        'phase': phase,
        'step': step,
        'attempt': attempt,
        'status': status,
        'exit_code': exit_code,
        'started_at': format_timestamp(started_at),
        'finished_at': format_timestamp(finished_at),
        'duration_seconds': duration_seconds if duration_seconds is not None else '',
        'detail': detail,
        'command': command,
        'learning_action': learning_action,
        'learning_note': learning_note,
        'decision_buffer_seconds': decision_buffer_seconds if decision_buffer_seconds is not None else '',
        'root': str(ROOT),
        'data_dir': str(DATA_DIR),
    }
    write_json_atomic(STATUS_LATEST, payload)
    write_json_atomic(latest_phase_path(phase), payload)
    append_history(STATUS_HISTORY, LEGACY_HISTORY_FIELDS, {key: payload.get(key, '') for key in LEGACY_HISTORY_FIELDS})
    append_history(STATUS_HISTORY_DETAILED, DETAILED_HISTORY_FIELDS, {key: payload.get(key, '') for key in DETAILED_HISTORY_FIELDS})


def get_step_timeout(phase: str, step_name: str) -> int:
    return STEP_TIMEOUTS.get((phase, step_name), STEP_TIMEOUT_DEFAULT)


def classify_step_status(phase: str, step_name: str, exit_code: int) -> str:
    if exit_code == 0:
        return 'ok'
    semantic_map = {
        'v10_moni_trader.py': {
            EXIT_WINDOW_SKIPPED: 'skipped',
            EXIT_NO_SIGNAL: 'no_signal',
            EXIT_NO_ACTION: 'no_action',
        },
        'workbuddy_local_challenger.py': {
            EXIT_WINDOW_SKIPPED: 'skipped',
            EXIT_NO_ACTION: 'no_action',
        },
    }
    step_semantics = semantic_map.get(step_name, {})
    if exit_code in step_semantics:
        return step_semantics[exit_code]
    return 'warning' if is_soft_fail_step(phase, step_name) else 'failed'


def is_nonfatal_step_status(phase: str, step_name: str, status: str) -> bool:
    if status in {'ok', 'no_action', 'no_signal', 'skipped'}:
        return True
    return status == 'warning' and is_soft_fail_step(phase, step_name)


def phase_completion_detail(status: str) -> str:
    detail_map = {
        'ok': 'phase finished',
        'no_action': 'phase finished with no action',
        'no_signal': 'phase finished with no signal',
        'skipped': 'phase finished with window skipped',
    }
    return detail_map.get(status, 'phase finished')


def enrich_add_position_detail(phase: str, step_name: str, detail: str) -> str:
    if phase != 'add-position' or step_name != 'v10_moni_trader.py':
        return detail
    summary = read_json(ACCOUNT_SUMMARY_FILE)
    execution_result = summary.get('latest_execution_result')
    if not isinstance(execution_result, dict):
        return detail
    if str(execution_result.get('action', '')).strip() != 'add_position':
        return detail
    status = str(execution_result.get('status', '')).strip() or 'unknown'
    planned_count = int(execution_result.get('planned_count', 0) or 0)
    first_pass_success_count = int(execution_result.get('first_pass_success_count', 0) or 0)
    tail_retry_queued_count = int(execution_result.get('tail_retry_queued_count', 0) or 0)
    tail_retry_success_count = int(execution_result.get('tail_retry_success_count', 0) or 0)
    failed_count = int(execution_result.get('failed_count', 0) or 0)
    failed_codes = execution_result.get('failed_codes', [])
    if not isinstance(failed_codes, list):
        failed_codes = []
    failed_codes = [str(code).strip() for code in failed_codes if str(code).strip()]
    summary_text = (
        f"planned={planned_count}, first_pass={first_pass_success_count}, "
        f"tail_retry_queued={tail_retry_queued_count}, tail_retry_success={tail_retry_success_count}, "
        f"failed={failed_count}, status={status}"
    )
    if failed_codes:
        summary_text += f", failed_codes={','.join(failed_codes)}"
    return f"{detail} | {summary_text}"


def run_step(phase: str, args: list[str]) -> tuple[int, str, datetime, datetime]:
    cmd = [PYTHON, *args]
    timeout_seconds = get_step_timeout(phase, args[0])
    started_at = datetime.now()
    print(f"[RUN] {' '.join(cmd)} | timeout={timeout_seconds}s")
    try:
        result = subprocess.run(cmd, cwd=ROOT, timeout=timeout_seconds)
        finished_at = datetime.now()
        if result.returncode == 0:
            return 0, 'step finished', started_at, finished_at
        step_name = normalize_step_name(args[0])
        semantic_status = classify_step_status(phase, step_name, result.returncode)
        semantic_detail_map = {
            'no_action': f'step finished with no action: {args[0]}',
            'no_signal': f'step finished with no signal: {args[0]}',
            'skipped': f'step finished with window skipped: {args[0]}',
        }
        return result.returncode, semantic_detail_map.get(semantic_status, f'step failed: {args[0]}'), started_at, finished_at
    except subprocess.TimeoutExpired:
        finished_at = datetime.now()
        detail = f'step timeout after {timeout_seconds}s: {args[0]}'
        print(f"[TIMEOUT] {detail}")
        return 124, detail, started_at, finished_at


def validate_step_outputs(phase: str, step: list[str]) -> list[str]:
    step_name = normalize_step_name(step[0])
    messages: list[str] = []
    if step_name == 'security_master_refresh.py':
        report = validate_opening_tradability_artifact(expected_trade_date=datetime.now().strftime('%Y-%m-%d'))
        messages.append(
            f"{report.name} ok trade_date={report.details['trade_date']} record_count={report.details['record_count']}"
        )
    elif step_name == 'refresh_distill_pipeline.py':
        report = validate_candidate_pool_artifact(path=WORKBUDDY_CANDIDATE_POOL_FILE)
        messages.append(
            f"{report.name} ok trade_date={report.details['trade_date']} selected_count={report.details['selected_count']}"
        )
    elif step_name == 'build_workbuddy_distill_daily_review.py':
        if not DISTILL_DAILY_REVIEW_FILE.exists():
            raise RuntimeValidationError(f"distill daily review 文件未生成: {DISTILL_DAILY_REVIEW_FILE}")
        payload = read_json(DISTILL_DAILY_REVIEW_FILE)
        if not payload or 'generated_at' not in payload or 'trade_date' not in payload:
            raise RuntimeValidationError(f"distill daily review 文件缺少关键字段: {DISTILL_DAILY_REVIEW_FILE}")
        messages.append(
            f"workbuddy_distill_daily_review_latest.json ok trade_date={payload.get('trade_date', '')}"
        )
    return messages


def enrich_run_meta_args(step: list[str], *, phase: str, run_meta: dict[str, str]) -> list[str]:
    if not step:
        return step
    step_name = normalize_step_name(step[0])
    if step_name not in RUN_META_ARG_STEPS:
        return step
    enriched = list(step)
    if step_name == 'data_freshness_probe.py' and '--phase' not in enriched:
        enriched.extend(['--phase', phase])
    if '--task-name' not in enriched:
        enriched.extend(['--task-name', run_meta['task_name']])
    if '--trigger-slot' not in enriched:
        enriched.extend(['--trigger-slot', run_meta['trigger_slot']])
    if '--run-id' not in enriched:
        enriched.extend(['--run-id', run_meta['run_id']])
    return enriched


def build_steps(phase: str, *, with_email: bool) -> list[list[str] | None]:
    if phase == 'opening-data':
        return [['security_master_refresh.py'], ['external_market_review.py']]
    elif phase == 'workbuddy-refresh':
        return [
            [str(REFRESH_DISTILL_PIPELINE_SCRIPT)],
            ['mx_enrich_candidates.py'],
            ['mx_event_review.py'],
            ['mx_challenger_pool.py'],
            ['mx_workbuddy_portfolio.py'],
        ]
    elif phase == 'workbuddy-buy':
        return [['workbuddy_local_challenger.py', '--buy']]
    elif phase == 'workbuddy-sell':
        return [['workbuddy_local_challenger.py', '--sell']]
    elif phase == 'workbuddy-smart-sell':
        return [['workbuddy_local_challenger.py', '--smart-sell']]
    elif phase == 'workbuddy-status':
        return [['workbuddy_local_challenger.py', '--status']]
    elif phase == 'prewarm':
        return [
            ['data_freshness_probe.py'],
            ['scanner_v10.py', '--prewarm-fast'],
            ['send_email.py', '--type', 'prewarm'] if with_email else None,
        ]
    elif phase == 'decision':
        return [
            ['data_freshness_probe.py'],
            ['scanner_v10.py', '--decision-fast'],
            ['send_email.py', '--type', 'decision'] if with_email else None,
        ]
    elif phase == 'buy':
        return [
            ['data_freshness_probe.py'],
            ['v10_moni_trader.py', '--buy'],
        ]
    elif phase == 'add-position':
        return [['v10_moni_trader.py', '--add-position']]
    elif phase == 'smart-sell':
        return [
            ['data_freshness_probe.py'],
            ['v10_moni_trader.py', '--smart-sell'],
        ]
    elif phase == 'sell':
        return [['v10_moni_trader.py', '--sell']]
    elif phase == 'status':
        return [['v10_moni_trader.py', '--status']]
    elif phase == 'midday-node':
        return [['v10_moni_trader.py', '--midday-node']]
    elif phase == 'midday-gate':
        return [['v10_moni_trader.py', '--midday-gate']]
    elif phase == 'close-node':
        return [
            ['v10_moni_trader.py', '--close-node'],
            ['external_market_review.py'],
            ['mx_enrich_candidates.py'],
            ['mx_event_review.py'],
            ['mx_challenger_pool.py'],
            ['mx_workbuddy_portfolio.py'],
            ['workbuddy_local_review.py'],
            [str(DISTILL_DAILY_REVIEW_SCRIPT)],
            ['workbuddy_learning_bridge.py'],
            ['update_curve_observatory.py'],
            ['send_email.py', '--type', 'review'] if with_email else None,
        ]
    else:
        raise ValueError(f'Unsupported phase: {phase}')


def should_skip_phase_for_calendar(phase: str) -> tuple[bool, str]:
    if phase not in TRADING_DAY_ONLY_PHASES:
        return False, ''
    today = datetime.now().date()
    if is_trading_day(today):
        return False, ''
    detail = f'non-trading day skipped by calendar ({CALENDAR_SOURCE})'
    return True, detail


def should_stop_phase_for_deadline(phase: str) -> tuple[bool, str]:
    deadline = PHASE_HARD_DEADLINES.get(phase)
    if not deadline:
        return False, ''
    now = datetime.now()
    deadline_at = now.replace(hour=deadline[0], minute=deadline[1], second=0, microsecond=0)
    if now <= deadline_at:
        return False, ''
    detail = f'phase hard deadline reached at {deadline_at:%H:%M:%S}'
    return True, detail


def run_phase_once(phase: str, *, run_meta: dict[str, str], with_email: bool, attempt: int = 1) -> int:
    phase_started_at = datetime.now()
    close_node_mx_results: list[dict[str, object]] = []
    phase_status = 'ok'
    phase_exit_code = 0
    skip, skip_detail = should_skip_phase_for_calendar(phase)
    if skip:
        learning = evaluate_prewarm_timing(
            run_meta=run_meta,
            status='skipped',
            started_at=phase_started_at,
            finished_at=phase_started_at,
        ) if phase == 'prewarm' else {}
        record_status(
            run_meta=run_meta,
            phase=phase,
            step='phase',
            attempt=attempt,
            status='skipped',
            exit_code=0,
            detail=skip_detail,
            started_at=phase_started_at,
            finished_at=phase_started_at,
            learning_action=str(learning.get('learning_action', '')),
            learning_note=str(learning.get('learning_note', '')),
            decision_buffer_seconds=learning.get('decision_buffer_seconds') if learning else None,
        )
        write_phase_inspection_snapshot(
            run_meta=run_meta,
            phase=phase,
            phase_status='skipped',
            phase_exit_code=0,
        )
        print(f"[SKIP] {skip_detail}")
        return 0
    deadline_stop, deadline_detail = should_stop_phase_for_deadline(phase)
    if deadline_stop:
        record_status(
            run_meta=run_meta,
            phase=phase,
            step='phase',
            attempt=attempt,
            status='deadline',
            exit_code=2,
            detail=deadline_detail,
            started_at=phase_started_at,
            finished_at=phase_started_at,
        )
        write_phase_inspection_snapshot(
            run_meta=run_meta,
            phase=phase,
            phase_status='deadline',
            phase_exit_code=2,
        )
        print(f"[STOP] {deadline_detail}")
        return 2
    try:
        preflight_reports = preflight_phase(phase, expected_trade_date=datetime.now().strftime('%Y-%m-%d'))
    except RuntimeValidationError as exc:
        preflight_finished_at = datetime.now()
        record_status(
            run_meta=run_meta,
            phase=phase,
            step='preflight',
            attempt=attempt,
            status='failed',
            exit_code=3,
            detail=str(exc),
            started_at=phase_started_at,
            finished_at=preflight_finished_at,
        )
        write_phase_inspection_snapshot(
            run_meta=run_meta,
            phase=phase,
            phase_status='failed',
            phase_exit_code=3,
        )
        print(f"[PREFLIGHT-FAIL] {exc}")
        return 3
    if preflight_reports:
        for report in preflight_reports:
            print(f"[PREFLIGHT] {report.name}: {json.dumps(report.details, ensure_ascii=False)}")
    steps = build_steps(phase, with_email=with_email)
    record_status(
        run_meta=run_meta,
        phase=phase,
        step='phase',
        attempt=attempt,
        status='running',
        exit_code=0,
        detail='phase started',
        started_at=phase_started_at,
    )
    try:
        for step in steps:
            if not step:
                continue
            effective_step = enrich_run_meta_args(step, phase=phase, run_meta=run_meta)
            step_name = normalize_step_name(effective_step[0])
            command = ' '.join([PYTHON, *effective_step])
            step_mark_started_at = datetime.now()
            record_status(
                run_meta=run_meta,
                phase=phase,
                step=step_name,
                attempt=attempt,
                status='running',
                exit_code=0,
                detail='step started',
                command=command,
                started_at=step_mark_started_at,
            )
            code, step_detail, step_started_at, step_finished_at = run_step(phase, effective_step)
            if code == 0:
                try:
                    validation_messages = validate_step_outputs(phase, effective_step)
                    if validation_messages:
                        suffix = ' | '.join(validation_messages)
                        step_detail = f"{step_detail} | {suffix}"
                except RuntimeValidationError as exc:
                    code = 3
                    step_detail = f"step output validation failed: {exc}"
            step_detail = enrich_add_position_detail(phase, step_name, step_detail)
            step_status = classify_step_status(phase, step_name, code)
            record_status(
                run_meta=run_meta,
                phase=phase,
                step=step_name,
                attempt=attempt,
                status=step_status,
                exit_code=code,
                detail=step_detail if code == 0 else step_detail,
                command=command,
                started_at=step_started_at,
                finished_at=step_finished_at,
            )
            if phase == 'close-node' and step_name in CLOSE_NODE_OPTIONAL_STEPS:
                close_node_mx_results.append({
                    'step': step_name,
                    'status': step_status,
                    'exit_code': code,
                    'detail': step_detail,
                    'started_at': format_timestamp(step_started_at),
                    'finished_at': format_timestamp(step_finished_at),
                    'duration_seconds': seconds_between(step_started_at, step_finished_at) or 0,
                })
            if code != 0:
                if step_status in {'no_action', 'no_signal', 'skipped'}:
                    phase_status = step_status
                    phase_exit_code = code
                    print(f"[INFO] semantic non-fatal step: {step_name} -> {step_detail} ({step_status})")
                    continue
                if is_soft_fail_step(phase, step_name):
                    print(f"[WARN] soft-fail step tolerated: {step_name} -> {step_detail}")
                    continue
                phase_finished_at = datetime.now()
                learning = evaluate_prewarm_timing(
                    run_meta=run_meta,
                    status='failed',
                    started_at=phase_started_at,
                    finished_at=phase_finished_at,
                ) if phase == 'prewarm' else {}
                record_status(
                    run_meta=run_meta,
                    phase=phase,
                    step='phase',
                    attempt=attempt,
                    status='failed',
                    exit_code=code,
                    detail=f'phase stopped at {step_name}: {step_detail}',
                    started_at=phase_started_at,
                    finished_at=phase_finished_at,
                    learning_action=str(learning.get('learning_action', '')),
                    learning_note=str(learning.get('learning_note', '')),
                    decision_buffer_seconds=learning.get('decision_buffer_seconds') if learning else None,
                )
                write_phase_inspection_snapshot(
                    run_meta=run_meta,
                    phase=phase,
                    phase_status='failed',
                    phase_exit_code=code,
                )
                if phase == 'close-node':
                    mx_review_payload = write_close_node_mx_review(
                        run_meta=run_meta,
                        mx_step_results=close_node_mx_results,
                        phase_status='failed',
                    )
                    merge_close_node_mx_review(mx_review_payload)
                    merge_close_node_opening_data_review()
                    merge_close_node_external_market_review()
                    merge_close_node_workbuddy_local_review()
                    merge_close_node_distill_daily_review()
                return code
            if not is_nonfatal_step_status(phase, step_name, step_status):
                phase_status = step_status
                phase_exit_code = code
        phase_finished_at = datetime.now()
        learning = evaluate_prewarm_timing(
            run_meta=run_meta,
            status='ok' if phase_status == 'ok' else 'review',
            started_at=phase_started_at,
            finished_at=phase_finished_at,
        ) if phase == 'prewarm' else {}
        record_status(
            run_meta=run_meta,
            phase=phase,
            step='phase',
            attempt=attempt,
            status=phase_status,
            exit_code=phase_exit_code,
            detail=phase_completion_detail(phase_status),
            started_at=phase_started_at,
            finished_at=phase_finished_at,
            learning_action=str(learning.get('learning_action', '')),
            learning_note=str(learning.get('learning_note', '')),
            decision_buffer_seconds=learning.get('decision_buffer_seconds') if learning else None,
        )
        write_phase_inspection_snapshot(
            run_meta=run_meta,
            phase=phase,
            phase_status=phase_status,
            phase_exit_code=phase_exit_code,
        )
        if phase == 'close-node':
            close_node_phase_status = 'warning' if any(item.get('status') != 'ok' for item in close_node_mx_results) else phase_status
            mx_review_payload = write_close_node_mx_review(
                run_meta=run_meta,
                mx_step_results=close_node_mx_results,
                phase_status=close_node_phase_status,
            )
            merge_close_node_mx_review(mx_review_payload)
            merge_close_node_opening_data_review()
            merge_close_node_external_market_review()
            merge_close_node_workbuddy_local_review()
            merge_close_node_distill_daily_review()
        return phase_exit_code
    except Exception as exc:
        phase_finished_at = datetime.now()
        record_status(
            run_meta=run_meta,
            phase=phase,
            step='phase',
            attempt=attempt,
            status='failed',
            exit_code=3,
            detail=f'unhandled runner exception: {type(exc).__name__}: {exc}',
            started_at=phase_started_at,
            finished_at=phase_finished_at,
        )
        write_phase_inspection_snapshot(
            run_meta=run_meta,
            phase=phase,
            phase_status='failed',
            phase_exit_code=3,
        )
        print(f"[RUNNER-FAIL] {type(exc).__name__}: {exc}")
        return 3


def run_phase_watch(
    phase: str,
    *,
    run_meta: dict[str, str],
    with_email: bool,
    max_attempts: int,
    interval_seconds: int,
) -> int:
    deadline = datetime.now().replace(hour=14, minute=57, second=0, microsecond=0)
    last_code = 11
    retryable_codes = set(WATCH_RETRYABLE_CODES)
    # buy-watch exists specifically to wait for scanner/decision artifacts to become ready.
    if phase == 'buy':
        retryable_codes.add(1)
    for attempt in range(1, max_attempts + 1):
        now = datetime.now()
        if now > deadline:
            detail = f'watch deadline reached at {deadline:%H:%M:%S}'
            record_status(
                run_meta=run_meta,
                phase=phase,
                step='phase',
                attempt=attempt,
                status='deadline',
                exit_code=last_code,
                detail=detail,
                started_at=now,
                finished_at=now,
            )
            print(f"[STOP] {detail}")
            return last_code
        code = run_phase_once(phase, run_meta=run_meta, with_email=with_email, attempt=attempt)
        last_code = code
        if code == 0:
            return 0
        if code not in retryable_codes or attempt == max_attempts:
            return code
        sleep_seconds = min(interval_seconds, max(1, int((deadline - datetime.now()).total_seconds())))
        detail = f'retry in {sleep_seconds}s after exit code {code}'
        retry_at = datetime.now()
        record_status(
            run_meta=run_meta,
            phase=phase,
            step='phase',
            attempt=attempt,
            status='retrying',
            exit_code=code,
            detail=detail,
            started_at=retry_at,
            finished_at=retry_at,
        )
        print(f"[WAIT] {detail}")
        time.sleep(sleep_seconds)
    return last_code


def main() -> int:
    parser = argparse.ArgumentParser(description='V10 自动化阶段执行器')
    parser.add_argument(
        '--phase',
        required=True,
        choices=['opening-data', 'workbuddy-refresh', 'workbuddy-buy', 'workbuddy-sell', 'workbuddy-smart-sell', 'workbuddy-status', 'prewarm', 'decision', 'buy', 'buy-watch', 'add-position', 'smart-sell', 'sell', 'status', 'midday-node', 'midday-gate', 'midday-review', 'close-node', 'report'],
        help='要执行的自动化阶段',
    )
    parser.add_argument('--with-email', action='store_true', help='阶段完成后发送对应邮件')
    parser.add_argument('--max-attempts', type=int, default=12, help='watch 模式最大尝试次数')
    parser.add_argument('--interval-seconds', type=int, default=30, help='watch 模式重试间隔秒数')
    parser.add_argument('--task-name', default='', help='触发该阶段的计划任务名')
    parser.add_argument('--trigger-slot', default='', help='计划任务时间槽，例如 14:30')
    parser.add_argument('--run-id', default='', help='可选的外部 run id；未传时自动生成')
    args = parser.parse_args()
    phase_name = canonical_phase_name(args.phase)
    run_meta = {
        'task_name': args.task_name or f'TLFZ-WorkBuddy-{phase_name}',
        'trigger_slot': args.trigger_slot,
        'run_id': args.run_id or build_run_id(
            phase=phase_name,
            task_name=args.task_name or f'TLFZ-WorkBuddy-{phase_name}',
            trigger_slot=args.trigger_slot,
        ),
    }
    if args.phase == 'buy-watch':
        return run_phase_watch(
            'buy',
            run_meta=run_meta,
            with_email=args.with_email,
            max_attempts=max(args.max_attempts, 1),
            interval_seconds=max(args.interval_seconds, 5),
        )
    return run_phase_once(phase_name, run_meta=run_meta, with_email=args.with_email)


if __name__ == '__main__':
    raise SystemExit(main())
