#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""注册/移除 TLFZ workbuddy 的 Windows 计划任务。"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PYTHON = Path(sys.executable).resolve()
RUNNER = ROOT / 'v10_auto_runner.py'
WRAPPER_DIR = ROOT / 'task_wrappers'
TASK_PREFIX = 'TLFZ-WorkBuddy-'
WEEK_DAYS = 'MON,TUE,WED,THU,FRI'
LEGACY_TASK_NAMES = [
    f'{TASK_PREFIX}SmartSell',
    f'{TASK_PREFIX}SmartSell1445',
    f'{TASK_PREFIX}MiddayReview',
    f'{TASK_PREFIX}Report',
    f'{TASK_PREFIX}AddPosition',
    f'{TASK_PREFIX}AddPosition1028',
    f'{TASK_PREFIX}AddPosition1328',
    f'{TASK_PREFIX}SmartSell0945',
    f'{TASK_PREFIX}SmartSell1015',
    f'{TASK_PREFIX}SmartSell1045',
    f'{TASK_PREFIX}SmartSell1115',
    f'{TASK_PREFIX}MiddayNode',
    f'{TASK_PREFIX}MiddayGate',
    f'{TASK_PREFIX}SmartSell1315',
    f'{TASK_PREFIX}SmartSell1345',
    f'{TASK_PREFIX}SmartSell1415',
    f'{TASK_PREFIX}Prewarm',
    f'{TASK_PREFIX}Decision',
    f'{TASK_PREFIX}BuyWatch',
    f'{TASK_PREFIX}SmartSell0947',
    f'{TASK_PREFIX}SmartSell1017',
    f'{TASK_PREFIX}SmartSell1047',
    f'{TASK_PREFIX}SmartSell1117',
    f'{TASK_PREFIX}SmartSell1347',
    f'{TASK_PREFIX}SmartSell1417',
    f'{TASK_PREFIX}SmartSell1447',
    f'{TASK_PREFIX}Buy1454',
    f'{TASK_PREFIX}ChallengerBuy1032',
]


@dataclass(frozen=True)
class TaskSpec:
    suffix: str
    time_hhmm: str
    phase: str
    trigger_slot: str | None = None
    with_email: bool = False
    max_attempts: int | None = None
    interval_seconds: int | None = None

    @property
    def task_name(self) -> str:
        return f'{TASK_PREFIX}{self.suffix}'

    @property
    def effective_trigger_slot(self) -> str:
        return self.trigger_slot or self.time_hhmm


TASK_SPECS = [
    TaskSpec('OpeningData', '09:31', 'opening-data'),
    TaskSpec('Status0933', '09:33', 'workbuddy-status'),
    TaskSpec('AddPosition', '09:36', 'add-position'),
    TaskSpec('AddPosition1028', '10:28', 'add-position'),
    TaskSpec('SmartSell0945', '09:45', 'smart-sell'),
    TaskSpec('ChallengerSell0947', '09:47', 'workbuddy-smart-sell', trigger_slot='09:45'),
    TaskSpec('ChallengerBuy1002', '10:02', 'workbuddy-buy', trigger_slot='10:00'),
    TaskSpec('SmartSell1015', '10:15', 'smart-sell'),
    TaskSpec('ChallengerSell1032', '10:32', 'workbuddy-smart-sell', trigger_slot='10:30'),
    TaskSpec('ChallengerBuy1034', '10:34', 'workbuddy-buy', trigger_slot='10:30'),
    TaskSpec('SmartSell1045', '10:45', 'smart-sell'),
    TaskSpec('ChallengerBuy1102', '11:02', 'workbuddy-buy', trigger_slot='11:00'),
    TaskSpec('SmartSell1115', '11:15', 'smart-sell'),
    TaskSpec('MiddayNode', '11:35', 'midday-node'),
    TaskSpec('MiddayGate', '13:00', 'midday-gate'),
    TaskSpec('SmartSell1315', '13:15', 'smart-sell'),
    TaskSpec('AddPosition1328', '13:28', 'add-position'),
    TaskSpec('ChallengerBuy1332', '13:32', 'workbuddy-buy', trigger_slot='13:30'),
    TaskSpec('WorkBuddyRefresh', '13:38', 'workbuddy-refresh'),
    TaskSpec('SmartSell1345', '13:45', 'smart-sell'),
    TaskSpec('ChallengerBuy1402', '14:02', 'workbuddy-buy', trigger_slot='14:00'),
    TaskSpec('SmartSell1415', '14:15', 'smart-sell'),
    TaskSpec('Prewarm', '14:30', 'prewarm'),
    TaskSpec('ChallengerBuy1432', '14:32', 'workbuddy-buy', trigger_slot='14:30'),
    TaskSpec('SmartSell1445', '14:45', 'smart-sell'),
    TaskSpec('Decision', '14:49', 'decision'),
    TaskSpec('BuyWatch', '14:50', 'buy-watch', max_attempts=12, interval_seconds=30),
    TaskSpec('ChallengerSell1452', '14:52', 'workbuddy-smart-sell', trigger_slot='14:50'),
    TaskSpec('ChallengerBuy1454', '14:54', 'workbuddy-buy', trigger_slot='14:50'),
    TaskSpec('Status1503', '15:03', 'workbuddy-status'),
    TaskSpec('CloseNode', '15:06', 'close-node'),
]

REQUIRED_TASK_SUFFIXES = {
    'OpeningData',
    'AddPosition',
    'SmartSell0945',
    'ChallengerSell0947',
    'ChallengerBuy1002',
    'SmartSell1015',
    'ChallengerSell1032',
    'ChallengerBuy1034',
    'SmartSell1045',
    'ChallengerBuy1102',
    'SmartSell1115',
    'MiddayNode',
    'MiddayGate',
    'SmartSell1315',
    'ChallengerBuy1332',
    'SmartSell1345',
    'ChallengerBuy1402',
    'SmartSell1415',
    'Prewarm',
    'ChallengerBuy1432',
    'SmartSell1445',
    'Decision',
    'BuyWatch',
    'ChallengerSell1452',
    'ChallengerBuy1454',
    'CloseNode',
}


def ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def safe_print(text: str) -> None:
    message = str(text)
    try:
        print(message)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((message + '\n').encode(sys.stdout.encoding or 'utf-8', errors='replace'))


def build_task_args(spec: TaskSpec) -> list[str]:
    args = [
        str(PYTHON),
        str(RUNNER),
        '--phase',
        spec.phase,
        '--task-name',
        spec.task_name,
        '--trigger-slot',
        spec.effective_trigger_slot,
    ]
    if spec.with_email:
        args.append('--with-email')
    if spec.max_attempts is not None:
        args.extend(['--max-attempts', str(spec.max_attempts)])
    if spec.interval_seconds is not None:
        args.extend(['--interval-seconds', str(spec.interval_seconds)])
    return args


def validate_task_specs(specs: list[TaskSpec]) -> None:
    suffixes = [spec.suffix for spec in specs]
    missing = sorted(REQUIRED_TASK_SUFFIXES - set(suffixes))
    if missing:
        raise ValueError(f'缺少关键任务定义: {", ".join(missing)}')

    seen_suffixes: set[str] = set()
    duplicate_suffixes: list[str] = []
    seen_slots: set[str] = set()
    duplicate_slots: list[str] = []
    for spec in specs:
        if spec.suffix in seen_suffixes and spec.suffix not in duplicate_suffixes:
            duplicate_suffixes.append(spec.suffix)
        seen_suffixes.add(spec.suffix)
        slot_key = f'{spec.time_hhmm}|{spec.phase}'
        if slot_key in seen_slots and slot_key not in duplicate_slots:
            duplicate_slots.append(slot_key)
        seen_slots.add(slot_key)

    problems = []
    if duplicate_suffixes:
        problems.append(f'任务后缀重复: {", ".join(sorted(duplicate_suffixes))}')
    if duplicate_slots:
        problems.append(f'时间槽重复: {", ".join(sorted(duplicate_slots))}')
    if problems:
        raise ValueError(' ; '.join(problems))


def ensure_wrapper_script(spec: TaskSpec) -> Path:
    WRAPPER_DIR.mkdir(parents=True, exist_ok=True)
    wrapper = WRAPPER_DIR / f'{spec.suffix}.cmd'
    args = build_task_args(spec)
    command = ' '.join(f'"{arg}"' for arg in args)
    lines = [
        '@echo off',
        f'cd /d "{ROOT}"',
        f'set "TLFZ_WORKBUDDY_ROOT={ROOT.parent.parent}"',
        f'set "TLFZ_ARKCLAW_ROOT={ROOT.parent.parent.parent}"',
        f'set "TLFZ_WORKBUDDY_DATA_DIR={ROOT.parent.parent / "a-share-analyst"}"',
        f'call {command}',
        'exit /b %ERRORLEVEL%',
    ]
    wrapper.write_text('\r\n'.join(lines) + '\r\n', encoding='utf-8')
    return wrapper


def create_task(spec: TaskSpec, *, dry_run: bool) -> int:
    wrapper = ensure_wrapper_script(spec)
    command = [
        'schtasks',
        '/Create',
        '/F',
        '/SC',
        'WEEKLY',
        '/D',
        WEEK_DAYS,
        '/TN',
        spec.task_name,
        '/ST',
        spec.time_hhmm,
        '/TR',
        str(wrapper),
    ]
    print(' '.join(command))
    if dry_run:
        return 0
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.stdout.strip():
        safe_print(result.stdout.strip())
    if result.stderr.strip():
        safe_print(result.stderr.strip())
    return result.returncode


def delete_task(spec: TaskSpec, *, dry_run: bool) -> int:
    command = ['schtasks', '/Delete', '/F', '/TN', spec.task_name]
    print(' '.join(command))
    if dry_run:
        return 0
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.stdout.strip():
        safe_print(result.stdout.strip())
    if result.stderr.strip():
        safe_print(result.stderr.strip())
    return result.returncode


def delete_named_task(task_name: str, *, dry_run: bool) -> int:
    command = ['schtasks', '/Delete', '/F', '/TN', task_name]
    print(' '.join(command))
    if dry_run:
        return 0
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.stdout.strip():
        safe_print(result.stdout.strip())
    if result.stderr.strip():
        safe_print(result.stderr.strip())
    return 0 if result.returncode == 0 else result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description='注册/移除 TLFZ workbuddy 自动化计划任务')
    parser.add_argument('--remove', action='store_true', help='移除而不是创建任务')
    parser.add_argument('--dry-run', action='store_true', help='只打印 schtasks 命令')
    args = parser.parse_args()

    if not args.remove:
        validate_task_specs(TASK_SPECS)

    exit_code = 0
    for legacy_name in LEGACY_TASK_NAMES:
        code = delete_named_task(legacy_name, dry_run=args.dry_run)
        if code not in (0, 1):
            exit_code = code
    for spec in TASK_SPECS:
        code = delete_task(spec, dry_run=args.dry_run) if args.remove else create_task(spec, dry_run=args.dry_run)
        if code != 0:
            exit_code = code
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
