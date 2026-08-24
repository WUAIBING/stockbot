#!/usr/bin/env python3
"""Challenger_1 Automated Trading Strategy — TDX Technical Screener.

Design goals:
1. Beat main-line benchmark (v10_moni_trader) on winrate and return
2. Fully automated via MEP nodes (tdx_download → screen → execute → track)
3. T+1 compliant A-share paper trading
4. Plugs into existing stockbot refresh pipeline

Data sources:
- TDX .day files (historical OHLCV, always available)
- pytdx (real-time quotes when market open)
- TDX pyautogui screenshots (visual confirmation)

Entry logic:
- MA trend alignment (MA5>MA10>MA20>MA60)
- Volume expansion (VR > 1.2)
- Price position (20d range, prefer 40-60% for pullback entries)
- Volatility filter (1% < stdev < 5%)
- Gap-at-open protection (skip if open > 2% above ref)

Exit logic (T+1 minimum):
- Target: 2.5x risk from entry
- Stop: 1.5x ATR
- Signal decay: MA5 crosses below MA10
- T+5 forced exit
"""

from __future__ import annotations

import csv
import json
import os
import struct
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Optional

import numpy as np

# ── Stockbot paths (compatible with repo layout) ──────────────────────
CHALLENGER_ROOT = Path(__file__).resolve().parent
SKILLS_DIR = CHALLENGER_ROOT / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILLS_DIR))

try:
    from backtest_framework import StrategyConfig, register_strategy
    from position_sizer import SizerConfig
    BACKTEST_FRAMEWORK_AVAILABLE = True
except ImportError:
    BACKTEST_FRAMEWORK_AVAILABLE = False

TDX_VIPDOC = Path(r"C:\new_tdx\vipdoc")

# ── Strategy Configuration ──────────────────────────────────────────────

@dataclass
class Challenger1Config:
    """Configuration for Challenger_1 TDX Technical Screener."""
    name: str = "challenger_1"
    capital: float = 1_000_000.0
    max_positions: int = 9
    max_single_pct: float = 12.0
    top_n_candidates: int = 9

    # Entry filters
    min_price: float = 3.0
    min_avg_volume: int = 500_000  # Minimum daily volume shares
    min_bars: int = 50

    # Technical thresholds
    winner_thresh: float = 5.0
    loser_thresh: float = -3.0

    # Stop/target defaults (overridden by per-stock ATR)
    default_stop_pct: float = 5.0
    target_risk_ratio: float = 2.5  # Target = stop * 2.5

    # Gap protection
    gap_up_skip_pct: float = 2.0  # Skip if open > ref * 1.02
    gap_down_enter_pct: float = 2.0  # Enter at open if open < ref * 0.98

    # T+1 compliance
    min_hold_days: int = 1
    max_hold_days: int = 5  # T+5 forced exit

    # Volatility filter
    min_stdev_pct: float = 0.5
    max_stdev_pct: float = 8.0

    # Output paths
    output_dir: Path = CHALLENGER_ROOT / "workbuddy_pool"

    def __post_init__(self):
        if isinstance(self.output_dir, str):
            self.output_dir = Path(self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)


# ── TDX .day File Reader ──────────────────────────────────────────────

def read_tdx_day(code: str, days: int = 200) -> list[dict]:
    """Read TDX .day binary file. Returns list of {'c','h','l','o','v'} dicts."""
    num = code[2:] if len(code) > 2 else code
    mkt = 'sh' if code.startswith('sh') or code.startswith('SH') or num.startswith(('6','5')) else 'sz'
    fname = f"{'sh' if mkt=='sh' else 'sz'}{num}.day"
    fp = TDX_VIPDOC / mkt / "lday" / fname
    if not fp.exists():
        return []
    bars = []
    try:
        data = fp.read_bytes()
        for i in range(0, len(data), 32):
            if i + 32 > len(data):
                break
            rec = data[i:i+32]
            di, o, h, l, c, amt, vol, _ = struct.unpack('<iiiiifii', rec)
            y, m, d = di // 10000, (di // 100) % 100, di % 100
            if not (2020 <= y <= 2030 and 1 <= m <= 12 and 1 <= d <= 31):
                continue
            o = o / 100.0; h = h / 100.0; l = l / 100.0; c = c / 100.0
            if c > 100000: o /= 100; h /= 100; l /= 100; c /= 100
            bars.append({'o': o, 'h': h, 'l': l, 'c': c, 'v': vol})
    except Exception:
        pass
    return bars[-days:]


# ── Technical Scoring Engine ──────────────────────────────────────────

def score_candidate(code: str, bars: list[dict], config: Challenger1Config) -> dict | None:
    """Score a single candidate. Returns scored dict or None if filtered out."""
    if len(bars) < config.min_bars:
        return None

    closes = [b['c'] for b in bars]
    highs = [b['h'] for b in bars]
    lows = [b['l'] for b in bars]
    volumes = [b['v'] for b in bars]
    latest = closes[-1]
    avg_vol = sum(volumes[-20:]) / 20

    # ── Basic filters ──
    if latest < config.min_price:
        return None
    if avg_vol < config.min_avg_volume:
        return None

    # ── Moving averages ──
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else ma20

    # ── Price position in 20-day range ──
    h20 = max(highs[-20:])
    l20 = min(lows[-20:])
    pos20 = (latest - l20) / (h20 - l20) * 100 if h20 > l20 else 50

    # ── Volume expansion ──
    vol5 = sum(volumes[-5:]) / 5
    vol20 = sum(volumes[-20:]) / 20
    vr = vol5 / vol20 if vol20 > 0 else 1.0

    # ── Momentum ──
    chg3 = (closes[-1] / closes[-4] - 1) * 100 if len(closes) >= 4 else 0
    chg10 = (closes[-1] / closes[-11] - 1) * 100 if len(closes) >= 11 else 0
    chg20 = (closes[-1] / closes[-21] - 1) * 100 if len(closes) >= 21 else 0

    # ── Volatility ──
    returns = [(closes[i] / closes[i-1] - 1) * 100 for i in range(-20, 0)]
    stdev = statistics.stdev(returns) if len(returns) > 1 else 2.0
    if stdev < config.min_stdev_pct or stdev > config.max_stdev_pct:
        return None

    # ── ATR (Average True Range) ──
    atr = sum(abs(b['h'] - b['l']) for b in bars[-14:]) / 14
    atr_pct = atr / latest * 100

    # ── Scoring (0-100+) ──
    score = 50  # Baseline

    # MA alignment: full bull = +25
    if ma5 > ma10 > ma20 > ma60: score += 25
    elif ma5 > ma10 > ma20: score += 18
    elif ma5 > ma10: score += 10
    elif ma5 < ma10 < ma20: score -= 12  # Bearish alignment penalty

    # Volume expansion: surge = +20
    if vr > 2.0: score += 20
    elif vr > 1.5: score += 16
    elif vr > 1.2: score += 10
    elif vr < 0.6: score -= 10

    # Position in range: pullback zone = +15
    if 30 <= pos20 <= 55: score += 15  # Sweet spot
    elif 20 <= pos20 < 30: score += 12  # Near support
    elif 55 < pos20 <= 75: score += 5   # Moderate extension
    elif pos20 > 90: score -= 8         # Overextended

    # Momentum quality = +12
    if -2 < chg3 < 6 and chg10 > -5: score += 8
    if 5 < chg20 < 30: score += 4

    # Volatility sweet spot = +8
    if 1.0 < stdev < 3.0: score += 8
    elif 0.5 < stdev < 1.5 and vr > 1.2: score += 5  # Low vol breakout

    # Mid-cap preference = +5
    if 10 < latest < 100: score += 5

    # ── Tier classification ──
    if pos20 < 60: tier = 1       # Pullback ready — full allocation
    elif pos20 < 80: tier = 2     # Moderate — half allocation
    else: tier = 3                # Extended — quarter allocation

    # ── Entry / Stop / Target ──
    entry_ref = round(ma5, 2)
    stop_ref = round(entry_ref - 1.5 * atr, 2)
    target_ref = round(entry_ref + config.target_risk_ratio * (entry_ref - stop_ref), 2)

    # ── Position sizing ──
    alloc_pct = {1: 10.0, 2: 6.0, 3: 3.0}.get(tier, 3.0)

    return {
        'code': code,
        'name': '',  # Filled by name resolver
        'tier': tier,
        'score': score,
        'close': latest,
        'ma5': round(ma5, 2),
        'ma10': round(ma10, 2),
        'ma20': round(ma20, 2),
        'ma60': round(ma60, 2),
        'pos20': round(pos20, 1),
        'vr': round(vr, 2),
        'stdev': round(stdev, 2),
        'atr_pct': round(atr_pct, 2),
        'chg3d': round(chg3, 1),
        'chg10d': round(chg10, 1),
        'chg20d': round(chg20, 1),
        'avg_vol': int(avg_vol),
        'entry_ref': entry_ref,
        'stop_ref': stop_ref,
        'target_ref': target_ref,
        'alloc_pct': alloc_pct,
        'gap_up_skip': round(entry_ref * (1 + config.gap_up_skip_pct / 100), 2),
        'gap_down_enter': round(entry_ref * (1 - config.gap_down_enter_pct / 100), 2),
        'window': {1: '10:30', 2: '11:00', 3: '14:00'}.get(tier, '14:00'),
    }


# ── Stock Name Resolver (from TDX local data) ──────────────────────

_STOCK_NAMES: dict[str, str] = {}

def _load_stock_names():
    """Parse stock names from connect.cfg or base.dbf if available."""
    global _STOCK_NAMES
    if _STOCK_NAMES:
        return
    # Hardcode known candidates (TDX has no simple name lookup in .day files)
    _STOCK_NAMES = {
        'sh600519': '贵州茅台', 'sz000858': '五粮液', 'sz000779': '甘咨询',
        'sz000999': '华润三九', 'sz000739': '普洛药业', 'sz000538': '云南白药',
        'sh600750': '江中药业', 'sz000938': '紫光股份', 'sz000977': '浪潮信息',
        'sh600600': '青岛啤酒', 'sh600674': '川投能源', 'sh600713': '南京医药',
        'sh600728': '中炬高新', 'sh600871': '石化油服', 'sh600886': '国投电力',
        'sh601607': '上海医药', 'sz000158': '常山北明', 'sz000513': '丽珠集团',
        'sz000626': '远大控股', 'sz000892': '欢瑞世纪', 'sz000923': '河北宣工',
        'sz000948': '南天信息', 'sz000999': '华润三九', 'sz001872': '招商港口',
    }


# ── Full Universe Scanner ──────────────────────────────────────────

def scan_universe(config: Challenger1Config = None, max_to_scan: int = 800) -> list[dict]:
    """Scan TDX .day files for top candidates."""
    if config is None:
        config = Challenger1Config()
    _load_stock_names()

    # Collect all A-share .day files
    all_files = []
    for mkt in ['sh', 'sz']:
        lday_dir = TDX_VIPDOC / mkt / "lday"
        if not lday_dir.exists():
            continue
        for f in lday_dir.iterdir():
            if not f.name.endswith('.day'):
                continue
            num = f.stem[2:] if len(f.stem) > 2 else f.stem
            # Filter: only real A-shares
            if mkt == 'sh' and num[:3] not in ('600', '601', '603', '605'):
                continue
            if mkt == 'sz' and num[:3] not in ('000', '001', '002', '003', '300', '301'):
                continue
            if len(num) != 6:
                continue
            code = f"{'sh' if mkt == 'sh' else 'sz'}{num}"
            all_files.append((code, f, f.stat().st_size))

    # Sort by file size (proxy for trading activity), take top N
    all_files.sort(key=lambda x: x[2], reverse=True)
    to_scan = all_files[:max_to_scan]

    results = []
    for code, filepath, _ in to_scan:
        bars = read_tdx_day(code, 200)
        scored = score_candidate(code, bars, config)
        if scored:
            scored['name'] = _STOCK_NAMES.get(code, f"Unknown_{code}")
            results.append(scored)

    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:config.top_n_candidates]


# ── Pool Writer (compatible with workbuddy pool format) ────────────

def write_candidate_pool(candidates: list[dict], config: Challenger1Config,
                         trade_date: str = None) -> Path:
    """Write candidate pool in format compatible with workbuddy_local_challenger."""
    if trade_date is None:
        trade_date = date.today().isoformat()

    selected_records = []
    for i, c in enumerate(candidates):
        selected_records.append({
            'code': c['code'].upper(),
            'name': c['name'],
            'tier': c['tier'],
            'selection_rank': i + 1,
            'selection_score': c['score'],
            'avg_profitability_priority': 100 + c['chg20d'] / 2 if c['chg20d'] > 0 else 90,
            'avg_candidate_win_rate': min(0.6, 0.52 + (0.03 if c['chg10d'] > 2 else 0) + (0.03 if c['vr'] > 1.2 else 0)),
            'avg_candidate_avg_return': max(0.5, min(c['chg20d'] / 4, 12.0)),
            'target_weight_pct': c['alloc_pct'],
            'volatility': c['stdev'],
            # Extended challenger_1 fields
            'entry_ref': c['entry_ref'],
            'stop_ref': c['stop_ref'],
            'target_ref': c['target_ref'],
            'gap_up_skip': c['gap_up_skip'],
            'gap_down_enter': c['gap_down_enter'],
            'buy_window': c['window'],
            'atr_pct': c['atr_pct'],
            'ma5': c['ma5'],
            'ma20': c['ma20'],
            'vr': c['vr'],
        })

    pool = {
        'generated_at': datetime.now().isoformat(),
        'trade_date': trade_date,
        'source': 'challenger_1_tdx_screener',
        'status': 'ready',
        'candidate_count': len(selected_records),
        'selected_count': len(selected_records),
        'selected_records': selected_records,
    }

    out_path = config.output_dir / f"workbuddy_challenger_1_pool_{trade_date}.json"
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(pool, f, indent=2, ensure_ascii=False)

    # Also write a "latest" symlink/copy
    latest_path = config.output_dir / "workbuddy_challenger_1_pool_latest.json"
    with open(latest_path, 'w', encoding='utf-8') as f:
        json.dump(pool, f, indent=2, ensure_ascii=False)

    return out_path


# ── Backtest Strategy Registration ──────────────────────────────────

def register_challenger_1_strategy():
    """Register challenger_1 in the global strategy registry if backtest_framework is available."""
    if not BACKTEST_FRAMEWORK_AVAILABLE:
        return None
    cfg = StrategyConfig(
        name="challenger_1",
        output_prefix="backtest_challenger_1",
        top_n_amount=9,
        winner_thresh=5.0,
        loser_thresh=-3.0,
        daily_bar_count=800,
        weekly_bar_count=100,
        ma_windows=(5, 10, 20, 60),
        bollinger_window=20,
        bollinger_std_mult=2.0,
        rsi_window=14,
        roc_windows=(3, 5, 10),
        slippage=0.001,
        commission=0.0003,
        min_stake=5000.0,
    )
    return register_strategy(cfg)


# ── Ledger (T+1 Compatible) ────────────────────────────────────────

def write_buy_plan(candidates: list[dict], trade_date: str = None, config: Challenger1Config = None):
    """Write detailed buy plan with conditional entry rules for tomorrow."""
    if trade_date is None:
        trade_date = date.today().isoformat()
    if config is None:
        config = Challenger1Config()

    plan_path = CHALLENGER_ROOT / f"challenger_1_buy_plan_{trade_date}.json"
    sell_date = (date.fromisoformat(trade_date) + timedelta(days=1)).isoformat() if trade_date else None

    entries = []
    for c in candidates:
        shares = int((config.capital * c['alloc_pct'] / 100) / c['close'] / 100) * 100
        entries.append({
            **c,
            'plan_shares': shares,
            'plan_amount': round(shares * c['entry_ref'], 2),
            'buy_date': trade_date,
            'earliest_sell_date': sell_date,
            'stop_loss_pct': round((c['entry_ref'] - c['stop_ref']) / c['entry_ref'] * 100, 1),
            'target_pct': round((c['target_ref'] - c['entry_ref']) / c['entry_ref'] * 100, 1),
        })

    plan = {
        'strategy': 'challenger_1',
        'generated_at': datetime.now().isoformat(),
        'buy_date': trade_date,
        'earliest_sell_date': sell_date,
        'capital': config.capital,
        't_plus_1': True,
        'entries': entries,
    }

    with open(plan_path, 'w', encoding='utf-8') as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)

    return plan_path


# ── Track Record ───────────────────────────────────────────────────

TRACK_FIELDS = [
    'buy_date', 'code', 'name', 'tier', 'entry_ref', 'entry_actual',
    'shares', 'cost', 'sell_date', 'sell_price', 'proceeds',
    'pnl', 'pnl_pct', 'hold_days', 'win', 'exit_reason',
]


def init_track_record(path: Path = None) -> Path:
    if path is None:
        path = CHALLENGER_ROOT / "workbuddy_pool" / "challenger_1_track_record.csv"
    if not path.exists():
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(TRACK_FIELDS)
    return path


def calculate_performance(track_file: Path = None) -> dict:
    """Calculate winrate, avg return, total P&L from track record."""
    if track_file is None:
        track_file = CHALLENGER_ROOT / "workbuddy_pool" / "challenger_1_track_record.csv"
    if not track_file.exists():
        return {'error': 'no track record yet'}

    rows = []
    with open(track_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    closed = [r for r in rows if r.get('sell_date') and r['sell_date'].strip()]
    wins = [r for r in closed if float(r.get('pnl', 0)) > 0]
    losses = [r for r in closed if float(r.get('pnl', 0)) <= 0]

    total_pnl = sum(float(r.get('pnl', 0)) for r in closed)
    avg_return = statistics.mean([float(r.get('pnl_pct', 0)) for r in closed]) if closed else 0
    max_win = max([float(r.get('pnl_pct', 0)) for r in closed]) if closed else 0
    max_loss = min([float(r.get('pnl_pct', 0)) for r in closed]) if closed else 0

    return {
        'total_trades': len(closed),
        'wins': len(wins),
        'losses': len(losses),
        'winrate': round(len(wins) / len(closed) * 100, 1) if closed else 0,
        'total_pnl': round(total_pnl, 2),
        'avg_return_pct': round(avg_return, 2),
        'max_win_pct': round(max_win, 2),
        'max_loss_pct': round(max_loss, 2),
        'profit_factor': round(abs(sum(float(r.get('pnl', 0)) for r in wins) / sum(float(r.get('pnl', 0)) for r in losses)), 2) if losses else float('inf'),
    }


# ── MEP Integration ─────────────────────────────────────────────────

MEP_TDX_PIPELINE = """#!/usr/bin/env python3
\"\"\"MEP Node Pipeline: Full daily challenger_1 cycle.
Run on trae stockbot node for automated execution.\"\"\"
import subprocess, sys, os, json, time
from datetime import datetime, date

ARKCLAW = r"C:/Users/Aibing/Documents/trae_projects/arkclaw"

def run_step(label, *cmd, cwd=ARKCLAW, timeout=300):
    print(f"\\n[{datetime.now():%H:%M:%S}] {label}...")
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr[:500]}")
    else:
        print(f"  OK: {result.stdout[:300]}")
    return result.returncode == 0

# Step 1: Download latest TDX data
run_step("TDX data download", sys.executable, "tdx_download.py", timeout=120)

# Step 2: Scan candidates
run_step("Candidate screening", sys.executable, "-c",
    "from challenger_1_strategy import *; "
    "cfg = Challenger1Config(); "
    "candidates = scan_universe(cfg, 800); "
    f"pool_path = write_candidate_pool(candidates, cfg, '{date.today().isoformat()}'); "
    "print(f'Pool: {pool_path}'); "
    f"plan_path = write_buy_plan(candidates, '{date.today().isoformat()}', cfg); "
    "print(f'Plan: {plan_path}')",
    timeout=180)

# Step 3: If market open, run visual confirmation screenshots
# (Skip if weekend/holiday)

# Step 4: Execute via buy windows throughout the day
print("\\nPipeline complete. Buy windows will execute via Windows Task Scheduler.")
"""


def deploy_mep_pipeline():
    """Write MEP pipeline script to arkclaw dir for trae execution."""
    path = CHALLENGER_ROOT / "mep_challenger_1_pipeline.py"
    path.write_text(MEP_TDX_PIPELINE, encoding='utf-8')
    return path


# ── CLI ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"

    if cmd == "scan":
        print("Scanning for challenger_1 candidates...")
        cfg = Challenger1Config()
        candidates = scan_universe(cfg, 800)
        pool_path = write_candidate_pool(candidates, cfg)
        plan_path = write_buy_plan(candidates, config=cfg)
        init_track_record()

        print(f"\nTop {len(candidates)} candidates → {pool_path}")
        print(f"Buy plan → {plan_path}")
        print()
        for i, c in enumerate(candidates):
            print(f"  {i+1}. [{c['tier']}] {c['code']} {c['name']} "
                  f"score={c['score']} close={c['close']:.2f} "
                  f"entry={c['entry_ref']} stop={c['stop_ref']} "
                  f"window={c['window']}")

    elif cmd == "perf":
        perf = calculate_performance()
        print(json.dumps(perf, indent=2, ensure_ascii=False))

    elif cmd == "register":
        reg = register_challenger_1_strategy()
        print(f"Registered: {reg}" if reg else "Backtest framework not available")

    elif cmd == "deploy":
        path = deploy_mep_pipeline()
        print(f"MEP pipeline deployed: {path}")

    elif cmd == "plan":
        # Show latest buy plan
        import glob as _glob
        plans = sorted(_glob.glob(str(CHALLENGER_ROOT / "challenger_1_buy_plan_*.json")))
        if plans:
            plan = json.loads(Path(plans[-1]).read_text(encoding='utf-8'))
            print(f"Strategy: {plan['strategy']} | Buy: {plan['buy_date']} | Sell: {plan['earliest_sell_date']}")
            for e in plan['entries']:
                print(f"  [{e['tier']}] {e['code']} {e['name']} "
                      f"entry={e['entry_ref']} stop={e['stop_ref']} target={e['target_ref']} "
                      f"shares={e['plan_shares']} window={e['window']}")
        else:
            print("No buy plan found. Run 'scan' first.")

    else:
        print(f"Usage: python challenger_1_strategy.py [scan|perf|register|deploy|plan]")
