"""
V10 Scanner: Multi-Tier + Multi-Mode Real-Time Scanner

Execution plan:
  14:30  Pre-warm: pull daily + weekly data, pre-filter
  14:50  Decision: pull 5-min tail data, compute signals
  14:53  Confirm: select top 5 candidates
  14:55  Execute: market buy or near-market limit

Output: Tiered candidates with entry_price + position + mode
"""

import sys
import io
import time
import json
import argparse
import urllib.request
import warnings
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
from pytdx.hq import TdxHq_API

from package_paths import CSI1000_SKILLS_DIR, DATA_DIR

warnings.filterwarnings("ignore")

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

CONS_FILE = CSI1000_SKILLS_DIR / "000852cons.xls"
OUTPUT_DIR = DATA_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# #region debug-point A:main-strategy-chain-helper
_DEBUG_ENV_FILE = Path(__file__).resolve().parent / ".dbg" / "main-strategy-chain.env"


def _main_strategy_debug_emit(hypothesis_id: str, location: str, msg: str, data: dict) -> None:
    url = "http://127.0.0.1:7777/event"
    session_id = "main-strategy-chain"
    try:
        content = _DEBUG_ENV_FILE.read_text(encoding="utf-8")
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if line.startswith("DEBUG_SERVER_URL="):
                url = line.split("=", 1)[1].strip() or url
            elif line.startswith("DEBUG_SESSION_ID="):
                session_id = line.split("=", 1)[1].strip() or session_id
    except Exception:
        pass
    payload = {
        "sessionId": session_id,
        "runId": "pre-fix",
        "hypothesisId": hypothesis_id,
        "location": location,
        "msg": msg,
        "data": data,
    }
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            ),
            timeout=0.8,
        ).read()
    except Exception:
        pass
# #endregion

TDX_HOSTS = [
    ("218.75.126.9", 7709),
    ("60.191.117.167", 7709),
    ("39.105.251.234", 7709),
    ("119.147.212.83", 7709),
]
# ── Dynamic scan range (not fixed Top N) ──
# 核心原则：量比质（牛市多撒网），质比量（熊市只打最确定的）
# 灵活调整依据：中证1000总成交额 + 个股成交额阈值 + 信号密度
# 注意：pytdx的amount单位是元，不是万元
SCAN_CONFIG = {
    # 成交额阈值（元）：低于此值的不扫，流动性不足
    'min_amount_yuan': 1e8,          # 默认1亿
    # 动态阈值：根据大盘冷热自动调整
    'hot_market_amount_yuan': 5e7,   # 牛市/活跃市：5千万即可（扩大搜索）
    'cold_market_amount_yuan': 3e8,  # 熊市/清淡市：3亿才扫（聚焦头部）
    # 上限：最多扫多少只（防止太慢）
    'max_stocks': 500,
    # 下限：最少扫多少只（确保覆盖）
    'min_stocks': 100,
    # 中证1000总成交额判定阈值（亿元）
    'hot_market_total_yi': 5000,     # CSI1000 >5000亿=活跃
    'cold_market_total_yi': 2000,    # CSI1000 <2000亿=清淡
}


def write_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def normalize_code(value):
    text = str(value).strip()
    if "." in text:
        text = text.split(".")[0]
    return text.zfill(6)

def market_from_exchange(exchange):
    return 0 if "深圳" in str(exchange) else 1

def connect_tdx():
    for _ in range(3):
        for host, port in TDX_HOSTS:
            api = TdxHq_API(heartbeat=True)
            try:
                if api.connect(host, port, time_out=3.0):
                    # #region debug-point A:scanner-connect-ok
                    _main_strategy_debug_emit(
                        "A",
                        "scanner_v10.py:connect_tdx",
                        "[DEBUG] scanner connected to tdx",
                        {"host": host, "port": port},
                    )
                    # #endregion
                    return api
            except Exception:
                pass
            try:
                api.disconnect()
            except Exception:
                pass
        time.sleep(0.5)
    raise RuntimeError("Cannot connect to pytdx")

def get_stock_list():
    cons = pd.read_excel(CONS_FILE)
    cons = cons.rename(columns={
        "成份券代码Constituent Code": "code_raw",
        "成份券名称Constituent Name": "name",
        "交易所Exchange": "exchange",
    })
    cons["code"] = cons["code_raw"].map(normalize_code)
    cons["market"] = cons["exchange"].map(market_from_exchange)
    cons = cons.drop_duplicates(subset=["market", "code"]).reset_index(drop=True)
    return cons[["code", "name", "market"]].copy()

def fetch_daily_bars(api, market, code, count=250):
    try:
        bars = api.get_security_bars(9, market, code, 0, count)
        if not bars:
            return None
        df = api.to_df(bars)
        if df is None or df.empty:
            return None
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.sort_values("datetime").reset_index(drop=True)
        return df
    except Exception:
        return None

def fetch_weekly_bars(api, market, code, count=100):
    try:
        bars = api.get_security_bars(5, market, code, 0, count)
        if not bars:
            return None
        df = api.to_df(bars)
        if df is None or df.empty:
            return None
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.sort_values("datetime").reset_index(drop=True)
        return df
    except Exception:
        return None

def fetch_5min_bars_today(api, market, code):
    """Get today's 5-min bars only"""
    try:
        bars = api.get_security_bars(0, market, code, 0, 50)  # last 50 bars
        if not bars:
            return None
        df = api.to_df(bars)
        if df is None or df.empty:
            return None
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.sort_values("datetime").reset_index(drop=True)
        # Filter to today only
        today = df.iloc[-1]["datetime"].date()
        df = df[df["datetime"].dt.date == today]
        return df
    except Exception:
        return None


def compute_weekly_features(wdf):
    if wdf is None or len(wdf) < 25:
        return {}
    wdf = wdf.copy()
    wdf["wma5"] = wdf["close"].rolling(5).mean()
    wdf["wma10"] = wdf["close"].rolling(10).mean()
    wdf["wma20"] = wdf["close"].rolling(20).mean()
    last = wdf.iloc[-1]
    if not all(pd.notna(last.get(k)) for k in ["wma5", "wma10", "wma20"]):
        return {}
    w5, w10, w20 = last["wma5"], last["wma10"], last["wma20"]
    return {
        "weekly_align": bool(w5 > w10 > w20),
        "weekly_slope": (w5 - w20) / w20 * 100 if w20 > 0 else 0.0,
        "weekly_close_vs_wma20": (last["close"] - w20) / w20 * 100 if w20 > 0 else 0.0,
        "weekly_ma10_slope": (w10 - wdf["wma10"].iloc[-2]) / wdf["wma10"].iloc[-2] * 100
            if len(wdf) > 1 and pd.notna(wdf["wma10"].iloc[-2]) and wdf["wma10"].iloc[-2] > 0 else 0.0,
    }


def compute_5min_signal(min5_df):
    """Compute buy-zone signals from today's 5-min data"""
    if min5_df is None or len(min5_df) < 5:
        return {}
    d = min5_df.copy()
    d["hour"] = d["datetime"].dt.hour
    d["minute"] = d["datetime"].dt.minute

    total_vol = d["vol"].sum()
    bz_full = d[(d["hour"] == 14) & (d["minute"] >= 30)]
    bz_rt = d[(d["hour"] == 14) & (d["minute"] >= 30) &
               ((d["hour"] < 14) | ((d["hour"] == 14) & (d["minute"] <= 50)))]

    feats = {}
    # Full buy-zone direction (14:30-15:00)
    if len(bz_full) >= 3:
        bz_open = bz_full.iloc[0]["open"]
        bz_close = bz_full.iloc[-1]["close"]
        feats["bz_direction"] = (bz_close - bz_open) / bz_open * 100 if bz_open > 0 else 0.0
    # Real-time direction (14:30-14:50)
    if len(bz_rt) >= 2:
        bz_rt_open = bz_rt.iloc[0]["open"]
        bz_rt_close = bz_rt.iloc[-1]["close"]
        feats["bz_rt_direction"] = (bz_rt_close - bz_rt_open) / bz_rt_open * 100 if bz_rt_open > 0 else 0.0
    # Volume ratio
    if len(bz_full) > 0 and total_vol > 0:
        bz_vol = bz_full["vol"].sum()
        avg_per_bar = total_vol / len(d)
        feats["bz_vol_ratio"] = (bz_vol / len(bz_full)) / avg_per_bar if avg_per_bar > 0 else 1.0

    return feats


def classify_signal(bz_dir, bz_rt, weekly_align, weekly_slope, ma20_off,
                    vol_expand, rsi, is_green, amt_ratio):
    """
    Multi-tier + multi-mode signal classification.
    Returns (tier, mode, position, description)
    """
    bz = bz_dir if not np.isnan(bz_dir) else 0.0
    bz_rt = bz_rt if not np.isnan(bz_rt) else bz

    bz_kill = bz < -0.3
    bz_mild = -0.3 <= bz < 0
    ma20_pull = -5.0 <= ma20_off <= 2.0
    ma20_near = -3.0 <= ma20_off <= 3.0
    weekly_strong = weekly_align and weekly_slope > 5.0

    # ── TIER 1: V9 full pass ──
    if bz_kill and weekly_align and ma20_pull:
        return (1, "V9_full", 1.0, f"bz={bz:+.2f}%+weekly+MA20")

    # ── TIER 2: Two strong conditions ──
    # bz_kill + weekly + nearMA20 (V9-like but wider MA20 band)
    if bz_kill and weekly_align and ma20_near:
        return (2, "kill+weekly+nearMA20", 0.6, f"bz={bz:+.2f}%+weekly+MA20_near")
    # bz_mild + weekly + MA20 pull
    if bz_mild and weekly_align and ma20_pull:
        return (2, "near_kill+weekly+MA20", 0.6, f"bz_mild={bz:+.2f}%+weekly+MA20")
    # bz_kill + MA20 pull (no weekly)
    if bz_kill and ma20_pull:
        return (2, "kill+MA20_pull", 0.5, f"bz={bz:+.2f}%+MA20")

    # ── MODE 2: Trend-Riding (no bz_kill needed) ──
    if weekly_strong and ma20_pull and vol_expand:
        return (2, "trend_ride+vol", 0.6, f"slope={weekly_slope:.1f}%+MA20+vol_expand")
    if weekly_strong and ma20_pull and is_green:
        return (2, "trend_ride+green", 0.5, f"slope={weekly_slope:.1f}%+MA20+green")

    # ── MODE 3: Volume-Breakout (cap MA20 offset to +15%) ──
    if vol_expand and is_green and weekly_align and rsi < 70 and ma20_off <= 15.0:
        return (2, "vol_breakout", 0.5, f"vol*{amt_ratio:.1f}+green+weekly")

    # ── TIER 3: One strong condition ──
    # kill_only: only if weekly_slope > 0 (avoid downtrend falling knives)
    if bz_kill and weekly_slope > 0:
        return (3, "kill_only", 0.3, f"bz={bz:+.2f}%")
    if weekly_strong and ma20_near:
        return (3, "trend_only", 0.3, f"slope={weekly_slope:.1f}%+MA20_near")
    # Remove vol_green — EV too low (1.44%)

    return (0, "no_signal", 0.0, "")


def _to_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _write_outputs(*, df, run_time, total_amt_yi, market_regime, amount_threshold, scanned_count):
    df_sig = df[df["tier"] > 0].copy()

    run_slot = datetime.now().strftime("%Y-%m-%d_%H%M")
    latest_scan_csv = OUTPUT_DIR / "v10_scan_full.csv"
    snapshot_scan_csv = OUTPUT_DIR / f"v10_scan_full.{run_slot}.csv"
    latest_scan_meta = OUTPUT_DIR / "v10_scan_meta.json"
    snapshot_scan_meta = OUTPUT_DIR / f"v10_scan_meta.{run_slot}.json"
    latest_scan_pointer = OUTPUT_DIR / "v10_scan_latest.json"

    df.to_csv(latest_scan_csv, index=False, encoding="utf-8-sig")
    df.to_csv(snapshot_scan_csv, index=False, encoding="utf-8-sig")

    scan_meta = {
        'run_time': run_time,
        'run_slot': run_slot,
        'total_csi1000_amt_yi': round(total_amt_yi, 0),
        'market_regime': market_regime,
        'amount_threshold_yi': round(amount_threshold / 1e8, 1),
        'stocks_scanned': scanned_count,
        'stocks_with_signal': len(df_sig),
        'signals_by_tier': {f'T{t}': len(df_sig[df_sig['tier'] == t]) for t in [1, 2, 3]},
        'latest_scan_csv': str(latest_scan_csv),
        'snapshot_scan_csv': str(snapshot_scan_csv),
        'latest_scan_meta': str(latest_scan_meta),
        'snapshot_scan_meta': str(snapshot_scan_meta),
    }
    write_json_atomic(latest_scan_meta, scan_meta)
    write_json_atomic(snapshot_scan_meta, scan_meta)
    write_json_atomic(
        latest_scan_pointer,
        {
            'run_time': run_time,
            'run_slot': run_slot,
            'scan_csv': str(snapshot_scan_csv),
            'scan_meta': str(snapshot_scan_meta),
            'signals_by_tier': scan_meta['signals_by_tier'],
            'stocks_with_signal': scan_meta['stocks_with_signal'],
        },
    )

    print("=" * 70)
    print("SCAN RESULTS")
    print("=" * 70)
    for tier in [1, 2, 3]:
        t = df_sig[df_sig["tier"] == tier]
        if len(t) == 0:
            print(f"\n  Tier {tier} (大肉/中肉/小肉): 0 signals")
            continue
        tier_name = {1: "大肉", 2: "中肉", 3: "小肉"}[tier]
        print(f"\n  Tier {tier} ({tier_name}): {len(t)} signals")
        print(f"  {'Code':<8s} {'Name':<10s} {'Entry':>8s} {'Pos':>5s} {'Mode':<25s} {'bz_dir':>8s} {'Slope':>7s} {'MA20':>7s}")
        print(f"  {'----':<8s} {'----':<10s} {'-----':>8s} {'---':>5s} {'----':<25s} {'------':>8s} {'-----':>7s} {'-----':>7s}")
        for _, r in t.sort_values(["mode", "weekly_slope"], ascending=[True, False]).iterrows():
            code_text = str(r.get("code", "") or "")
            name_text = str(r.get("name", "") or "")
            mode_text = str(r.get("mode", "") or "")
            bz_s = f"{r['bz_direction']:+.2f}%" if not np.isnan(r['bz_direction']) else "N/A"
            print(f"  {code_text:<8s} {name_text:<10.10s} {r['entry_price']:>8.2f} {r['position']:>5.0%} "
                  f"{mode_text:<25.25s} {bz_s:>8s} {r['weekly_slope']:>6.1f}% {r['close_vs_ma20_pct']:>+6.1f}%")

    print("\n" + "=" * 70)
    print("TOP 5 PICKS (by tier then weekly_slope)")
    print("=" * 70)
    if len(df_sig) > 0:
        top5 = df_sig.sort_values(["tier", "weekly_slope"], ascending=[True, False]).head(5)
        for rank, (_, r) in enumerate(top5.iterrows(), 1):
            tier_name = {1: "大肉", 2: "中肉", 3: "小肉"}[r['tier']]
            code_text = str(r.get("code", "") or "")
            name_text = str(r.get("name", "") or "")
            mode_text = str(r.get("mode", "") or "")
            signal_desc_text = str(r.get("signal_desc", "") or "")
            print(f"  #{rank} [{tier_name}] {code_text} {name_text}")
            print(f"       Entry: {r['entry_price']:.2f}  Position: {r['position']:.0%}")
            print(f"       Mode: {mode_text}  {signal_desc_text}")
            bz_s = f"{r['bz_direction']:+.2f}%" if not np.isnan(r['bz_direction']) else "N/A"
            bz_rt_s = f"{r['bz_rt_direction']:+.2f}%" if not np.isnan(r['bz_rt_direction']) else "N/A"
            print(f"       bz(14:30-15:00)={bz_s}  bz_rt(14:30-14:50)={bz_rt_s}")
            print(f"       Weekly slope={r['weekly_slope']:.1f}%  MA20 offset={r['close_vs_ma20_pct']:+.1f}%  RSI={r['rsi14']:.0f}")
    else:
        print("  No signals today!")

    print("\n" + "=" * 70)
    print("EXECUTION PLAN")
    print("=" * 70)
    print("  14:30  Pre-warm: scanner pulls daily + weekly data")
    print("  14:50  Decision: check 5-min tail for bz_rt_direction")
    print("  14:53  Confirm: select candidates from tiered list")
    print("  14:55  EXECUTE: market buy or current price +1-2 tick limit")
    print("  Note:   Use entry_price as reference, actual fill may differ by ~0.2%")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    n_t1 = len(df_sig[df_sig["tier"] == 1])
    n_t2 = len(df_sig[df_sig["tier"] == 2])
    n_t3 = len(df_sig[df_sig["tier"] == 3])
    print(f"  Total scanned: {len(df)}")
    print(f"  Tier 1 (大肉 100%pos): {n_t1}")
    print(f"  Tier 2 (中肉 50-60%pos): {n_t2}")
    print(f"  Tier 3 (小肉 30%pos): {n_t3}")
    print(f"  Total signals: {n_t1 + n_t2 + n_t3}")

    if len(df_sig) > 0:
        print(f"\n  By mode:")
        for mode in df_sig["mode"].unique():
            n = len(df_sig[df_sig["mode"] == mode])
            tier = df_sig[df_sig["mode"] == mode]["tier"].iloc[0]
            print(f"    T{tier} {mode}: {n}")

    print(f"\n  Full data saved: {OUTPUT_DIR / 'v10_scan_full.csv'}")
    print("=" * 70)


def _run_decision_fast(api, *, run_time):
    latest_scan_csv = OUTPUT_DIR / "v10_scan_full.csv"
    if not latest_scan_csv.exists():
        raise RuntimeError(f"missing prewarm scan file: {latest_scan_csv}")
    df = pd.read_csv(latest_scan_csv, encoding="utf-8-sig")
    if "market" not in df.columns:
        market_df = get_stock_list()[["code", "market"]].copy()
        market_df["code"] = market_df["code"].map(normalize_code)
        df["code"] = df["code"].map(normalize_code)
        df = df.merge(market_df, on="code", how="left")
    refreshed_rows = []
    total = len(df)
    for idx, row in enumerate(df.to_dict(orient="records")):
        code = normalize_code(row.get("code", ""))
        market = int(_to_float(row.get("market", 1), 1))
        min5 = fetch_5min_bars_today(api, market, code)
        m5feats = compute_5min_signal(min5)
        bz_dir = m5feats.get("bz_direction", np.nan)
        bz_rt = m5feats.get("bz_rt_direction", np.nan)
        bz_vol_r = m5feats.get("bz_vol_ratio", np.nan)
        tier, mode, position, desc = classify_signal(
            bz_dir,
            bz_rt,
            _to_bool(row.get("weekly_align", False)),
            _to_float(row.get("weekly_slope", 0.0), 0.0),
            _to_float(row.get("close_vs_ma20_pct", 0.0), 0.0),
            _to_bool(row.get("vol_expand", False)),
            _to_float(row.get("rsi14", 50.0), 50.0),
            _to_bool(row.get("is_green", False)),
            _to_float(row.get("amt_ratio", 1.0), 1.0),
        )
        row["tier"] = tier
        row["mode"] = mode
        row["position"] = position
        row["signal_desc"] = desc
        row["bz_direction"] = bz_dir
        row["bz_rt_direction"] = bz_rt
        row["bz_vol_ratio"] = bz_vol_r
        refreshed_rows.append(row)
        if (idx + 1) % 100 == 0:
            # #region debug-point C:scanner-decision-refresh-progress
            _main_strategy_debug_emit(
                "B",
                "scanner_v10.py:_run_decision_fast",
                "[DEBUG] scanner decision refresh progress",
                {"processed": idx + 1, "total_to_scan": total, "last_code": code},
            )
            # #endregion
    refreshed = pd.DataFrame(refreshed_rows)
    # #region debug-point B:scanner-decision-refresh-done
    _main_strategy_debug_emit(
        "B",
        "scanner_v10.py:_run_decision_fast",
        "[DEBUG] scanner decision refresh completed",
        {
            "row_count": len(refreshed),
            "signal_count": int((refreshed["tier"].fillna(0) > 0).sum()) if not refreshed.empty else 0,
        },
    )
    # #endregion
    _write_outputs(
        df=refreshed,
        run_time=run_time,
        total_amt_yi=_to_float((OUTPUT_DIR / "v10_scan_meta.json").exists() and json.loads((OUTPUT_DIR / "v10_scan_meta.json").read_text(encoding="utf-8")).get("total_csi1000_amt_yi", 0.0), 0.0),
        market_regime=str(json.loads((OUTPUT_DIR / "v10_scan_meta.json").read_text(encoding="utf-8")).get("market_regime", "cached")) if (OUTPUT_DIR / "v10_scan_meta.json").exists() else "cached",
        amount_threshold=_to_float(json.loads((OUTPUT_DIR / "v10_scan_meta.json").read_text(encoding="utf-8")).get("amount_threshold_yi", 0.0), 0.0) * 1e8 if (OUTPUT_DIR / "v10_scan_meta.json").exists() else 0.0,
        scanned_count=len(refreshed),
    )


def _run_prewarm_fast(api, *, run_time):
    stocks = get_stock_list()
    print(f"CSI1000: {len(stocks)} stocks, pytdx connected\n")
    print("Phase 1: Dynamic amount filter (市场冷热自适应)...")
    amt_list = []
    for _, row in stocks.iterrows():
        bars = api.get_security_bars(9, row["market"], row["code"], 0, 3)
        if bars:
            try:
                df = api.to_df(bars)
                if df is not None and not df.empty:
                    amt_list.append({
                        "code": row["code"], "name": row["name"],
                        "market": row["market"],
                        "latest_amt": df.iloc[-1]["amount"],
                        "last_close": df.iloc[-1]["close"],
                    })
            except Exception:
                pass

    total_amt_yuan = sum(r["latest_amt"] for r in amt_list)
    total_amt_yi = total_amt_yuan / 1e8
    if total_amt_yi > SCAN_CONFIG["hot_market_total_yi"]:
        market_regime = "活跃市"
        amount_threshold = SCAN_CONFIG["hot_market_amount_yuan"]
    elif total_amt_yi < SCAN_CONFIG["cold_market_total_yi"]:
        market_regime = "清淡市"
        amount_threshold = SCAN_CONFIG["cold_market_amount_yuan"]
    else:
        market_regime = "正常市"
        amount_threshold = SCAN_CONFIG["min_amount_yuan"]

    print(f"  中证1000总成交额: {total_amt_yi:.0f}亿 | 市场状态: {market_regime}")
    print(f"  个股成交额阈值: {amount_threshold/1e8:.1f}亿")
    filtered = [r for r in amt_list if r["latest_amt"] >= amount_threshold]
    filtered.sort(key=lambda x: x["latest_amt"], reverse=True)
    if len(filtered) > SCAN_CONFIG["max_stocks"]:
        filtered = filtered[:SCAN_CONFIG["max_stocks"]]
    elif len(filtered) < SCAN_CONFIG["min_stocks"]:
        all_sorted = sorted(amt_list, key=lambda x: x["latest_amt"], reverse=True)
        filtered = all_sorted[:SCAN_CONFIG["min_stocks"]]

    amt_df = pd.DataFrame(filtered)
    print(f"  -> 预热扫描{len(amt_df)}只 (阈值>={amount_threshold/1e8:.1f}亿, 范围{SCAN_CONFIG['min_stocks']}-{SCAN_CONFIG['max_stocks']})\n")
    print("Phase 2: Compute daily + weekly features only...")
    results = []
    total = len(amt_df)
    for i, (_, row) in enumerate(amt_df.iterrows()):
        code = row["code"]
        name = row["name"]
        market = row["market"]
        last_close = row["last_close"]
        daily = fetch_daily_bars(api, market, code, count=250)
        if daily is None or len(daily) < 60:
            continue
        d = daily.copy()
        for w in [5, 10, 20, 60]:
            d[f"ma{w}"] = d["close"].rolling(w).mean()
        d["avg_amt_5d"] = d["amount"].rolling(5).mean()
        d["amt_ratio"] = d["amount"] / d["avg_amt_5d"]
        d["close_vs_ma20"] = (d["close"] - d["ma20"]) / d["ma20"] * 100
        delta = d["close"].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        d["rsi14"] = 100 - (100 / (1 + rs))
        last = d.iloc[-1]
        if pd.isna(last.get("ma20")) or pd.isna(last.get("amt_ratio")):
            continue
        ma20_off = last["close_vs_ma20"] if pd.notna(last["close_vs_ma20"]) else 0.0
        amt_r = last["amt_ratio"] if pd.notna(last["amt_ratio"]) else 1.0
        vol_exp = bool(1.3 <= amt_r <= 2.5)
        rsi = last["rsi14"] if pd.notna(last["rsi14"]) else 50.0
        is_green = last["close"] > last["open"]
        weekly = fetch_weekly_bars(api, market, code, count=100)
        wfeats = compute_weekly_features(weekly)
        weekly_align = wfeats.get("weekly_align", False)
        weekly_slope = wfeats.get("weekly_slope", 0.0)

        # Prewarm only prepares reusable base features; final 5-min classification stays in decision-fast.
        results.append({
            "code": code,
            "name": name,
            "close": last_close,
            "entry_price": last_close,
            "market": market,
            "tier": 0,
            "mode": "prewarm_pending_decision",
            "position": 0.0,
            "signal_desc": "",
            "bz_direction": np.nan,
            "bz_rt_direction": np.nan,
            "bz_vol_ratio": np.nan,
            "weekly_align": weekly_align,
            "weekly_slope": weekly_slope,
            "close_vs_ma20_pct": ma20_off,
            "amt_ratio": amt_r,
            "rsi14": rsi,
            "is_green": is_green,
            "vol_expand": vol_exp,
        })
        if (i + 1) % 50 == 0:
            print(f"  Processed {i+1}/{total}")

    print(f"  -> Prewarm cached base features for {len(results)} stocks\n")
    _write_outputs(
        df=pd.DataFrame(results),
        run_time=run_time,
        total_amt_yi=total_amt_yi,
        market_regime=market_regime,
        amount_threshold=amount_threshold,
        scanned_count=len(amt_df),
    )


def main():
    parser = argparse.ArgumentParser(description="V10 scanner")
    parser.add_argument("--decision-fast", action="store_true", help="reuse latest scan and refresh 5-min tail only")
    parser.add_argument("--prewarm-fast", action="store_true", help="prewarm only daily/weekly base features and defer 5-min classification to decision")
    args = parser.parse_args()
    # #region debug-point A:scanner-main-start
    _main_strategy_debug_emit(
        "A",
        "scanner_v10.py:main",
        "[DEBUG] scanner main started",
        {
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "decision_fast": bool(args.decision_fast),
            "prewarm_fast": bool(args.prewarm_fast),
        },
    )
    # #endregion
    print("=" * 70)
    print("V10 Scanner: Multi-Tier + Multi-Mode Real-Time Scanner")
    print("Philosophy: 大肉小肉都是肉 — every day is a trading day")
    print("=" * 70)
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"Run time: {run_time}\n")
    api = connect_tdx()
    try:
        if args.decision_fast:
            print("Decision fast mode: reuse latest prewarm scan and refresh 5-min tail only...")
            _run_decision_fast(api, run_time=run_time)
            return
        if args.prewarm_fast:
            print("Prewarm fast mode: cache daily/weekly base features and defer 5-min refresh to decision...")
            _run_prewarm_fast(api, run_time=run_time)
            return

        stocks = get_stock_list()
        print(f"CSI1000: {len(stocks)} stocks, pytdx connected\n")
        print("Phase 1: Dynamic amount filter (市场冷热自适应)...")
        amt_list = []
        for _, row in stocks.iterrows():
            bars = api.get_security_bars(9, row["market"], row["code"], 0, 3)
            if bars:
                try:
                    df = api.to_df(bars)
                    if df is not None and not df.empty:
                        amt_list.append({
                            "code": row["code"], "name": row["name"],
                            "market": row["market"],
                            "latest_amt": df.iloc[-1]["amount"],
                            "last_close": df.iloc[-1]["close"],
                        })
                except Exception:
                    pass

        total_amt_yuan = sum(r['latest_amt'] for r in amt_list)
        total_amt_yi = total_amt_yuan / 1e8
        if total_amt_yi > SCAN_CONFIG['hot_market_total_yi']:
            market_regime = '活跃市'
            amount_threshold = SCAN_CONFIG['hot_market_amount_yuan']
        elif total_amt_yi < SCAN_CONFIG['cold_market_total_yi']:
            market_regime = '清淡市'
            amount_threshold = SCAN_CONFIG['cold_market_amount_yuan']
        else:
            market_regime = '正常市'
            amount_threshold = SCAN_CONFIG['min_amount_yuan']

        print(f"  中证1000总成交额: {total_amt_yi:.0f}亿 | 市场状态: {market_regime}")
        print(f"  个股成交额阈值: {amount_threshold/1e8:.1f}亿")
        filtered = [r for r in amt_list if r['latest_amt'] >= amount_threshold]
        filtered.sort(key=lambda x: x['latest_amt'], reverse=True)
        if len(filtered) > SCAN_CONFIG['max_stocks']:
            filtered = filtered[:SCAN_CONFIG['max_stocks']]
        elif len(filtered) < SCAN_CONFIG['min_stocks']:
            all_sorted = sorted(amt_list, key=lambda x: x['latest_amt'], reverse=True)
            filtered = all_sorted[:SCAN_CONFIG['min_stocks']]

        amt_df = pd.DataFrame(filtered)
        # #region debug-point B:scanner-phase1-done
        _main_strategy_debug_emit(
            "B",
            "scanner_v10.py:main",
            "[DEBUG] scanner phase1 completed",
            {
                "stock_count": len(stocks),
                "amt_list_count": len(amt_list),
                "filtered_count": len(amt_df),
                "market_regime": market_regime,
                "amount_threshold_yi": round(amount_threshold / 1e8, 4),
            },
        )
        # #endregion
        print(f"  -> 扫描{len(amt_df)}只 (阈值>={amount_threshold/1e8:.1f}亿, 范围{SCAN_CONFIG['min_stocks']}-{SCAN_CONFIG['max_stocks']})\n")
        print("Phase 2: Compute daily + weekly + 5min features...")
        results = []
        for i, (_, row) in enumerate(amt_df.iterrows()):
            code = row["code"]
            name = row["name"]
            market = row["market"]
            last_close = row["last_close"]
            daily = fetch_daily_bars(api, market, code, count=250)
            if daily is None or len(daily) < 60:
                continue
            d = daily.copy()
            for w in [5, 10, 20, 60]:
                d[f"ma{w}"] = d["close"].rolling(w).mean()
            d["avg_amt_5d"] = d["amount"].rolling(5).mean()
            d["amt_ratio"] = d["amount"] / d["avg_amt_5d"]
            d["close_vs_ma20"] = (d["close"] - d["ma20"]) / d["ma20"] * 100
            delta = d["close"].diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss
            d["rsi14"] = 100 - (100 / (1 + rs))
            last = d.iloc[-1]
            if pd.isna(last.get("ma20")) or pd.isna(last.get("amt_ratio")):
                continue
            ma20_off = last["close_vs_ma20"] if pd.notna(last["close_vs_ma20"]) else 0.0
            amt_r = last["amt_ratio"] if pd.notna(last["amt_ratio"]) else 1.0
            vol_exp = bool(1.3 <= amt_r <= 2.5)
            rsi = last["rsi14"] if pd.notna(last["rsi14"]) else 50.0
            is_green = last["close"] > last["open"]
            weekly = fetch_weekly_bars(api, market, code, count=100)
            wfeats = compute_weekly_features(weekly)
            weekly_align = wfeats.get("weekly_align", False)
            weekly_slope = wfeats.get("weekly_slope", 0.0)
            min5 = fetch_5min_bars_today(api, market, code)
            m5feats = compute_5min_signal(min5)
            bz_dir = m5feats.get("bz_direction", np.nan)
            bz_rt = m5feats.get("bz_rt_direction", np.nan)
            bz_vol_r = m5feats.get("bz_vol_ratio", np.nan)
            tier, mode, position, desc = classify_signal(
                bz_dir, bz_rt, weekly_align, weekly_slope, ma20_off,
                vol_exp, rsi, is_green, amt_r
            )
            results.append({
                "code": code,
                "name": name,
                "close": last_close,
                "entry_price": last_close,
                "market": market,
                "tier": tier,
                "mode": mode,
                "position": position,
                "signal_desc": desc,
                "bz_direction": bz_dir,
                "bz_rt_direction": bz_rt,
                "bz_vol_ratio": bz_vol_r,
                "weekly_align": weekly_align,
                "weekly_slope": weekly_slope,
                "close_vs_ma20_pct": ma20_off,
                "amt_ratio": amt_r,
                "rsi14": rsi,
                "is_green": is_green,
                "vol_expand": vol_exp,
            })
            if (i + 1) % 50 == 0:
                print(f"  Processed {i+1}/{len(amt_df)}")
            if (i + 1) % 100 == 0:
                # #region debug-point C:scanner-progress
                _main_strategy_debug_emit(
                    "A",
                    "scanner_v10.py:main",
                    "[DEBUG] scanner phase2 progress",
                    {
                        "processed": i + 1,
                        "candidate_count": len(results),
                        "total_to_scan": len(amt_df),
                        "last_code": code,
                    },
                )
                # #endregion

        # #region debug-point D:scanner-finished
        _main_strategy_debug_emit(
            "B",
            "scanner_v10.py:main",
            "[DEBUG] scanner finished",
            {
                "result_count": len(results),
                "signal_count": len([row for row in results if int(row.get("tier", 0) or 0) > 0]),
            },
        )
        # #endregion
        print(f"  -> Total: {len(results)} stocks scanned\n")
        _write_outputs(
            df=pd.DataFrame(results),
            run_time=run_time,
            total_amt_yi=total_amt_yi,
            market_regime=market_regime,
            amount_threshold=amount_threshold,
            scanned_count=len(amt_df),
        )
    finally:
        api.disconnect()


if __name__ == "__main__":
    main()
