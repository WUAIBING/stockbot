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
import os
import time
import json
import errno
import uuid
import ctypes
import hashlib
import argparse
import urllib.request
import warnings
from pathlib import Path
from datetime import datetime, time as datetime_time, timedelta, timezone

import numpy as np
import pandas as pd
from pytdx.hq import TdxHq_API

from package_paths import CSI1000_SKILLS_DIR, DATA_DIR
from strategy_profile import classify_signal as classify_profile_signal, load_strategy_profile, profile_fingerprint

warnings.filterwarnings("ignore")

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

CONS_FILE = CSI1000_SKILLS_DIR / "000852cons.xls"
OUTPUT_DIR = DATA_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
WALK_FORWARD_SNAPSHOT_FILE = OUTPUT_DIR / "v10_walk_forward_snapshots.csv"

MARKET_TZ = timezone(timedelta(hours=8), name="UTC+08")
MARKET_TIMEZONE = "UTC+08"
SCANNER_ARTIFACT_SCHEMA_VERSION = 1
DECISION_LOCK_STALE_SECONDS = 15 * 60
DECISION_MAX_WAIT_SECONDS = 3 * 60


def _market_now():
    return datetime.now(MARKET_TZ)


def _market_timestamp(value=None):
    current = _market_now() if value is None else value
    if current.tzinfo is None:
        current = current.replace(tzinfo=MARKET_TZ)
    else:
        current = current.astimezone(MARKET_TZ)
    return current.isoformat(sep=" ", timespec="microseconds")


def _as_market_datetime(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=MARKET_TZ)
    return parsed.astimezone(MARKET_TZ)


def _decision_cutoff_for(trade_date):
    return datetime.combine(trade_date, datetime_time(14, 50), tzinfo=MARKET_TZ)


def _new_run_slot():
    market_now = _market_now()
    return (
        f"{market_now:%Y-%m-%d_%H%M%S_%f}_"
        f"{os.getpid()}_{time.time_ns()}"
    )


def _protocol_identity(*, trade_date, phase, producer_run_id=None, run_slot=None):
    resolved_run_slot = str(run_slot or _new_run_slot())
    resolved_producer_run_id = str(producer_run_id or "").strip() or resolved_run_slot
    artifact_id = f"{trade_date}:{phase}:{resolved_producer_run_id}"
    return resolved_run_slot, resolved_producer_run_id, artifact_id


def _decision_protocol_context(*, run_time, producer_run_id=None):
    run_datetime = _as_market_datetime(run_time)
    trade_date = run_datetime.date().isoformat()
    run_slot, resolved_producer_run_id, artifact_id = _protocol_identity(
        trade_date=trade_date,
        phase="decision",
        producer_run_id=producer_run_id,
    )
    return {
        "trade_date": trade_date,
        "decision_cutoff_at": _decision_cutoff_for(run_datetime.date()),
        "run_slot": run_slot,
        "producer_run_id": resolved_producer_run_id,
        "artifact_id": artifact_id,
        "started_at": _market_timestamp(),
    }


def _decision_lock_path():
    return OUTPUT_DIR / "v10_decision.lock"


def _windows_pid_is_alive(pid):
    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        return ctypes.get_last_error() == error_access_denied
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _pid_is_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_pid_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def _lock_snapshot(path):
    try:
        stat = path.stat()
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    return (stat.st_ino, stat.st_mtime_ns, stat.st_size, raw)


def _reclaim_abandoned_decision_lock(path, *, stale_after_seconds=DECISION_LOCK_STALE_SECONDS):
    snapshot = _lock_snapshot(path)
    if snapshot is None:
        return True
    _inode, modified_ns, _size, raw = snapshot
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}

    now_epoch = time.time()
    modified_age = max(0.0, now_epoch - (modified_ns / 1_000_000_000))
    try:
        created_epoch = float(payload.get("created_at_epoch", 0.0))
    except (TypeError, ValueError):
        created_epoch = 0.0
    created_age = max(0.0, now_epoch - created_epoch) if created_epoch > 0 else 0.0
    stale = modified_age > stale_after_seconds or created_age > stale_after_seconds
    pid = payload.get("pid")
    owner_dead = pid is not None and not _pid_is_alive(pid)
    if not stale and not owner_dead:
        return False

    if _lock_snapshot(path) != snapshot:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return True


def _acquire_decision_lock(*, stale_after_seconds=DECISION_LOCK_STALE_SECONDS):
    path = _decision_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    owner_token = f"{os.getpid()}-{time.time_ns()}-{uuid.uuid4().hex}"
    payload = {
        "pid": os.getpid(),
        "owner_token": owner_token,
        "created_at": _market_timestamp(),
        "created_at_epoch": time.time(),
        "market_timezone": MARKET_TIMEZONE,
    }
    for _attempt in range(3):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            if _reclaim_abandoned_decision_lock(
                path,
                stale_after_seconds=stale_after_seconds,
            ):
                continue
            raise RuntimeError(f"decision producer lock is already held: {path}")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
                json.dump(payload, lock_file, ensure_ascii=False)
                lock_file.flush()
                os.fsync(lock_file.fileno())
        except Exception:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise
        return {
            "path": path,
            "owner_token": owner_token,
            "pid": os.getpid(),
        }
    raise RuntimeError(f"unable to acquire decision producer lock: {path}")


def _release_decision_lock(lock):
    if not lock:
        return False
    path = Path(lock["path"])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError, OSError):
        return False
    try:
        payload_pid = int(payload.get("pid", -1))
        owner_pid = int(lock.get("pid", -2))
    except (TypeError, ValueError):
        return False
    if (
        payload.get("owner_token") != lock.get("owner_token")
        or payload_pid != owner_pid
    ):
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _publish_decision_running_marker(context):
    published_at = _market_timestamp()
    marker = {
        "schema_version": SCANNER_ARTIFACT_SCHEMA_VERSION,
        "artifact_id": context["artifact_id"],
        "producer_run_id": context["producer_run_id"],
        "phase": "decision",
        "trade_date": context["trade_date"],
        "complete": False,
        "decision_cutoff_at": context["decision_cutoff_at"].strftime("%Y-%m-%d %H:%M:%S"),
        "started_at": context["started_at"],
        "published_at": published_at,
        "market_timezone": MARKET_TIMEZONE,
    }
    write_json_atomic(OUTPUT_DIR / "v10_decision_latest.json", marker)
    return marker


def _wait_for_decision_fetch_window(trade_date, *, max_wait_seconds=DECISION_MAX_WAIT_SECONDS):
    if isinstance(trade_date, str):
        trade_date = datetime.strptime(trade_date, "%Y-%m-%d").date()
    now = _market_now()
    if trade_date < now.date():
        return 0.0
    if trade_date > now.date():
        raise RuntimeError(f"decision trade date {trade_date} is in the future")

    fetch_not_before = datetime.combine(
        trade_date,
        datetime_time(14, 50, 2),
        tzinfo=MARKET_TZ,
    )
    remaining = (fetch_not_before - now).total_seconds()
    if remaining <= 0:
        return 0.0
    if remaining > max_wait_seconds:
        raise RuntimeError(
            f"decision cutoff wait {remaining:.3f}s exceeds "
            f"maximum {max_wait_seconds}s"
        )

    waited = 0.0
    deadline = time.monotonic() + max_wait_seconds
    while remaining > 0:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise RuntimeError("decision cutoff wait exceeded its deadline")
        sleep_for = min(remaining, budget)
        time.sleep(sleep_for)
        waited += sleep_for
        now = _market_now()
        remaining = (fetch_not_before - now).total_seconds()
    return waited

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
def _positive_int_env(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


DECISION_MAX_CANDIDATES = _positive_int_env("TLFZ_DECISION_MAX_CANDIDATES", 120)
DECISION_DAILY_COUNT = 80
QUOTE_BATCH_SIZE = 80

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
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _write_csv_atomic(df, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        df.to_csv(tmp_path, index=False, encoding="utf-8-sig")
        tmp_path.replace(path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


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

_PRICE_MISMATCH_LOG = []
_TRUSTED_MISMATCH_LOG = []

# Written at the open by the tradability pass, which does not go through pytdx.
TRUSTED_PRICE_FILE = DATA_DIR / "opening_tradability_latest.json"
_TRUSTED_PRICE_CACHE = {"loaded": False, "prices": None, "trade_date": ""}


def _load_trusted_reference_prices(expected_trade_date=None):
    """last_close per code from the opening tradability snapshot.

    _price_pair_agrees compares the realtime quote against the last daily bar,
    but both arrive through the same pytdx session. When that session returns
    wrong-but-consistent data for a security the two agree, the check passes,
    and the row becomes a tradable candidate.

    That is not hypothetical. On 2026-08-26 it let 69 of 116 scanned rows
    through carrying prices up to 30x wrong - 300083 at 422.33 against a real
    13.78, 688630 at 30.05 against a real 415.00 - while the mismatch log stayed
    empty. Two independent sources agreed on the true prices for all 116 rows:
    this snapshot, and TDX desktop's own vipdoc files.

    Only a source outside pytdx can catch that class of fault, so this is
    deliberately read from disk rather than fetched.

    Returns None when the snapshot is unusable, which disables the check rather
    than dropping the universe.
    """
    if _TRUSTED_PRICE_CACHE["loaded"]:
        return _TRUSTED_PRICE_CACHE["prices"]
    _TRUSTED_PRICE_CACHE["loaded"] = True
    payload = _read_json_object(TRUSTED_PRICE_FILE)
    if not isinstance(payload, dict) or not payload:
        print(f"[WARN] trusted price snapshot unavailable: {TRUSTED_PRICE_FILE} "
              "- scan price validation disabled this run")
        return None
    trade_date = str(payload.get("trade_date", "")).strip()
    if expected_trade_date and trade_date != str(expected_trade_date):
        # A previous session's closes are a legitimate reason to differ, so a
        # stale snapshot must not be used to reject today's rows.
        print(f"[WARN] trusted price snapshot is for {trade_date or 'unknown'}, "
              f"expected {expected_trade_date} - scan price validation disabled")
        return None
    records = payload.get("records", [])
    if not isinstance(records, list) or not records:
        print("[WARN] trusted price snapshot has no records "
              "- scan price validation disabled this run")
        return None
    prices = {}
    for item in records:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip().zfill(6)
        if not code:
            continue
        for field in ("last_close", "last_price", "open_price"):
            value = _to_float(item.get(field), 0.0)
            if value > 0:
                prices[code] = value
                break
    if not prices:
        print("[WARN] trusted price snapshot carried no usable prices "
              "- scan price validation disabled this run")
        return None
    _TRUSTED_PRICE_CACHE["prices"] = prices
    _TRUSTED_PRICE_CACHE["trade_date"] = trade_date
    print(f"[INFO] trusted price reference loaded: {len(prices)} codes "
          f"(trade_date={trade_date})")
    return prices


def _trusted_price_disagrees(code, price, reference_prices):
    """True when a scanned price is impossible against the trusted reference.

    The bound is the board's daily limit with headroom - the same shape the
    order layer uses - so an ordinary intraday move never trips it. A code the
    snapshot does not cover is not judged.
    """
    if not reference_prices:
        return False
    px = _to_float(price, 0.0)
    if px <= 0:
        return False
    reference = _to_float(reference_prices.get(str(code).strip().zfill(6)), 0.0)
    if reference <= 0:
        return False
    tolerance = max(_board_daily_limit_pct(code) * 2.0, 25.0)
    return abs(px - reference) / reference * 100.0 > tolerance


def _board_daily_limit_pct(code):
    """Daily price-limit ceiling per board, used to bound a plausible gap."""
    c = str(code or "").zfill(6)
    if c.startswith(("688", "300", "301")):
        return 20.0
    if c.startswith(("43", "83", "87", "920")):
        return 30.0
    return 10.0


def _price_pair_agrees(code, quote_price, bar_close):
    """True when two independent price sources are close enough to trust.

    The realtime quote and the last daily bar can legitimately differ - the bar
    may be the previous session's close while the quote is live - so the bound
    is the board's daily limit with headroom, not equality. What it catches is
    an order-of-magnitude disagreement, which means one source is describing a
    different security.

    Missing or non-positive values return True: this is a mismatch detector,
    not a completeness check, and it must not silently drop the whole universe
    if a field is absent.
    """
    q = _to_float(quote_price, 0.0)
    b = _to_float(bar_close, 0.0)
    if q <= 0 or b <= 0:
        return True
    tolerance = max(_board_daily_limit_pct(code) * 2.0, 25.0)
    return abs(q - b) / b * 100.0 <= tolerance


_TDX_RESYNC_LOG = []


def _resync_after_protocol_error(api, where, code=""):
    """Rebuild the connection after a failed pytdx call.

    pytdx speaks a request/response protocol over a single socket: write a
    request, read a header, read a body. If the read raises - a timeout, a
    short read, a parse error - the bytes for that response are still sitting
    in the buffer. The next request then reads THOSE bytes as its own response,
    so code N+1 receives code N's data, and every subsequent code is shifted by
    one. Nothing raises again, so the corruption is silent and permanent for
    the rest of the run.

    That is not hypothetical. In the 2026-08-26 14:49 decision scan, rows 0-33
    were correct and rows 34-115 were 91% wrong - 300083 written as 422.33
    against a real 13.78, 688630 as 30.05 against a real 415.00. The 14:31
    prewarm scan of the same session was clean, because prewarm passes
    include_5min=False and therefore issues one fewer request per code.

    Reconnecting costs a round trip and loses one row. Continuing costs every
    row after it.
    """
    _TDX_RESYNC_LOG.append({"where": where, "code": str(code or "")})
    print(f"[WARN] pytdx protocol error in {where}"
          f"{f' at {code}' if code else ''} - reconnecting to avoid "
          f"desynchronised responses (resync #{len(_TDX_RESYNC_LOG)})")
    try:
        api.disconnect()
    except Exception:
        pass
    for host, port in TDX_HOSTS:
        try:
            if api.connect(host, port):
                return True
        except Exception:
            continue
        try:
            api.disconnect()
        except Exception:
            pass
    print("[ERROR] pytdx reconnect failed on every host - "
          "remaining rows this run cannot be trusted")
    return False


def fetch_daily_bars(api, market, code, count=250):
    try:
        bars = api.get_security_bars(9, market, code, 0, count)
    except Exception:
        # socket-level failure: the connection is now unsafe to reuse
        _resync_after_protocol_error(api, "fetch_daily_bars", code)
        return None
    if not bars:
        return None
    try:
        df = api.to_df(bars)
        if df is None or df.empty:
            return None
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.sort_values("datetime").reset_index(drop=True)
        return df
    except Exception:
        # the response was read in full; only our own parsing failed
        return None

def fetch_weekly_bars(api, market, code, count=100):
    try:
        bars = api.get_security_bars(5, market, code, 0, count)
    except Exception:
        _resync_after_protocol_error(api, "fetch_weekly_bars", code)
        return None
    if not bars:
        return None
    try:
        df = api.to_df(bars)
        if df is None or df.empty:
            return None
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.sort_values("datetime").reset_index(drop=True)
        return df
    except Exception:
        return None

def fetch_5min_bars_today(api, market, code, *, cutoff_at=None):
    """Get today's 5-min bars only"""
    try:
        bars = api.get_security_bars(0, market, code, 0, 50)  # last 50 bars
    except Exception:
        # The decision scan is the only caller that reaches here, which is why
        # it corrupts and the prewarm scan does not.
        _resync_after_protocol_error(api, "fetch_5min_bars_today", code)
        return None
    if not bars:
        return None
    try:
        df = api.to_df(bars)
        if df is None or df.empty:
            return None
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.sort_values("datetime").reset_index(drop=True)
        # A decision cutoff owns the trading date. Never infer that date from a
        # provider response, which may already contain bars from another day.
        if cutoff_at is not None:
            cutoff_timestamp = pd.Timestamp(cutoff_at)
            if cutoff_timestamp.tzinfo is not None:
                cutoff_timestamp = (
                    cutoff_timestamp.tz_convert(MARKET_TZ).tz_localize(None)
                )
            target_date = cutoff_timestamp.date()
        else:
            cutoff_timestamp = None
            target_date = df.iloc[-1]["datetime"].date()
        df = df[df["datetime"].dt.date == target_date]
        if cutoff_timestamp is not None:
            df = df[df["datetime"] <= cutoff_timestamp]
        return df.reset_index(drop=True)
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


_SCANNER_STRATEGY_PROFILE = load_strategy_profile()
_SCANNER_STRATEGY_PROFILE_ID = str(_SCANNER_STRATEGY_PROFILE["profile_id"])
_SCANNER_STRATEGY_PROFILE_HASH = profile_fingerprint(_SCANNER_STRATEGY_PROFILE)


def classify_signal(bz_dir, bz_rt, weekly_align, weekly_slope, ma20_off,
                    vol_expand, rsi, is_green, amt_ratio):
    """Classify a 14:50 snapshot with the versioned live strategy profile."""
    decision = classify_profile_signal({
        "bz_dir": bz_dir,
        "bz_rt": bz_rt,
        "weekly_align": weekly_align,
        "weekly_slope": weekly_slope,
        "ma20_off": ma20_off,
        "vol_expand": vol_expand,
        "rsi": rsi,
        "is_green": is_green,
        "amt_ratio": amt_ratio,
    }, _SCANNER_STRATEGY_PROFILE)
    return (
        decision["tier"],
        decision["mode"],
        decision["position"],
        decision["signal_desc"],
    )


def _to_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _quote_code(quote):
    if not isinstance(quote, dict):
        return ""
    value = quote.get("code")
    if value is None:
        return ""
    try:
        if bool(pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    return normalize_code(value) if str(value).strip() else ""


def _quote_records(api, raw_quotes):
    if raw_quotes is None:
        return []
    if isinstance(raw_quotes, list):
        return raw_quotes
    if isinstance(raw_quotes, pd.DataFrame):
        quote_df = raw_quotes
    else:
        response_to_df = getattr(raw_quotes, "to_df", None)
        if callable(response_to_df):
            quote_df = response_to_df()
        else:
            api_to_df = getattr(api, "to_df", None)
            if not callable(api_to_df):
                raise TypeError("quote response is neither a list nor convertible with to_df")
            quote_df = api_to_df(raw_quotes)
    if quote_df is None:
        return []
    if isinstance(quote_df, list):
        return quote_df
    if isinstance(quote_df, pd.DataFrame):
        return quote_df.to_dict(orient="records")
    raise TypeError("to_df did not return a DataFrame or list")


def _collect_amount_snapshot(api, stocks):
    amt_list = []
    stock_rows = stocks.to_dict(orient="records")
    for offset in range(0, len(stock_rows), QUOTE_BATCH_SIZE):
        batch = stock_rows[offset:offset + QUOTE_BATCH_SIZE]
        batch_number = offset // QUOTE_BATCH_SIZE + 1
        try:
            securities = [
                (int(_to_float(row["market"], 1)), normalize_code(row["code"]))
                for row in batch
            ]
            quotes = _quote_records(api, api.get_security_quotes(securities))
            coded_quotes = [(_quote_code(quote), quote) for quote in quotes]
            quotes_have_codes = any(code for code, _quote in coded_quotes)
            if quotes_have_codes:
                quotes_by_code = {
                    code: quote
                    for code, quote in coded_quotes
                    if code
                }
                row_quotes = (
                    (row, quotes_by_code.get(normalize_code(row["code"])))
                    for row in batch
                )
            elif len(quotes) == len(batch):
                row_quotes = zip(batch, quotes)
            else:
                print(
                    f"[WARN] quote batch {batch_number} skipped: "
                    f"uncoded response count {len(quotes)} != request count {len(batch)}"
                )
                continue

            batch_snapshot = []
            for row, quote in row_quotes:
                if not isinstance(quote, dict):
                    continue
                batch_snapshot.append({
                    "code": normalize_code(row["code"]),
                    "name": row["name"],
                    "market": row["market"],
                    "latest_amt": _to_float(quote.get("amount"), 0.0),
                    "last_close": _to_float(quote.get("price"), 0.0),
                })
        except Exception as exc:
            print(f"[WARN] quote batch {batch_number} skipped: {exc}")
            _resync_after_protocol_error(api, "quote_batch", f"batch{batch_number}")
            continue
        amt_list.extend(batch_snapshot)
    return amt_list


DATA_DIR_FOR_FLAG = str(DATA_DIR)


def _select_amount_candidates(amt_list):
    total_amt_yuan = sum(_to_float(row.get("latest_amt"), 0.0) for row in amt_list)
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

    # WHICH END OF THE TURNOVER RANKING THE POOL COMES FROM.
    #
    # This sorted descending and kept the top - the highest-turnover names above
    # the floor. Turnover is the single strongest predictor in the whole feature
    # set, and it is NEGATIVE: rank IC -0.0968 at ten sessions, t-17.96, over
    # 868 sessions of the gate universe with a shuffled control at zero.
    #
    # So the pool was chosen from the wrong end of the strongest factor, before
    # any score, gate or sizing touched it. Measured as forward excess of the
    # pool against the universe it is drawn from, entered T+1:
    #
    #     held 10 sessions   top 120 by turnover   -0.893%  t -9.35
    #                        bottom 120            +0.068%  t +1.68
    #     held 20 sessions   top 120               -1.723%  t-13.82
    #                        bottom 120            +0.119%  t +2.14
    #
    # Same universe, same floor, same count - one sort direction apart. That is
    # why every gate tuned downstream of it measured flat or negative: they were
    # ranking inside a pool already 1.7 points in the hole.
    #
    # The gain is almost entirely in NOT bleeding rather than a new edge (+0.068%
    # is barely above zero, and t+1.68 is weak on its own). The comparison that
    # matters is +0.068% against -0.893%.
    #
    # Off by default. TLFZ_LOW_TURNOVER_POOL=1 turns it around.
    # Read at CALL time, from a file as well as the environment.
    #
    # The env alone cannot turn this on during a session: stockbot-trading-day
    # starts at 09:25 and every phase subprocess inherits the environment it had
    # then, so editing trading-day.env changes nothing until the service
    # restarts - and restarting mid-day disrupts the running trading day.
    #
    # A flag FILE is checked on every call, so the pool can be reverted between
    # the scan and the buy node without touching the service. Deleting the file
    # is the kill switch.
    ascending = str(os.environ.get("TLFZ_LOW_TURNOVER_POOL", "0")).strip().lower() \
        in ("1", "true", "yes", "on")
    if not ascending:
        try:
            ascending = os.path.exists(os.path.join(
                DATA_DIR_FOR_FLAG, "TLFZ_LOW_TURNOVER_POOL"))
        except Exception:
            ascending = False
    filtered = [row for row in amt_list if _to_float(row.get("latest_amt"), 0.0) >= amount_threshold]
    filtered.sort(key=lambda row: _to_float(row.get("latest_amt"), 0.0),
                  reverse=not ascending)
    if len(filtered) > SCAN_CONFIG["max_stocks"]:
        filtered = filtered[:SCAN_CONFIG["max_stocks"]]
    elif len(filtered) < SCAN_CONFIG["min_stocks"]:
        # THIS BRANCH IS THE ONE THAT USUALLY FIRES, and the first version of
        # this change left it untouched - which made the whole change inert.
        #
        # On 2026-09-04 the market read 清淡市, so the floor was 3亿, fewer than
        # min_stocks passed it, and this ran. The resulting pool had a median of
        # 1.1亿 and a minimum of 0.60亿 - far below its own threshold - because
        # the old code abandoned the floor entirely and took the top 100 by
        # turnover from the whole universe. The most aggressive possible
        # selection from the wrong end of the strongest factor, on exactly the
        # days the market is thin.
        #
        # So relax the floor instead of abandoning it: step it down until enough
        # names qualify, then apply the same direction as above. A liquidity
        # floor still exists; it just is not "whatever the busiest 100 names
        # happen to be".
        # Relax until min_stocks qualify, and do NOT pin a higher floor to
        # "protect" the measured population. An earlier attempt pinned this at
        # 1亿 and the pool collapsed from 100 names to 60 - which throttles the
        # engine rather than turning it, and is not what was asked for.
        #
        # The pin was also unnecessary on the evidence. The liquidity sweep
        # tested floors from 20M upward and the effect held at every one:
        #
        #     floor    excess @10 sessions        t
        #      20M          +0.573%            +9.43
        #      50M          +0.429%            +7.16
        #     100M          +0.427%            +6.41
        #     200M          +0.530%            +6.53
        #
        # So names at 0.39亿 are INSIDE the tested population, not outside it.
        # And a 20k position against 39M of daily turnover is 0.05% of volume -
        # the fill concern was invented, not measured.
        floor = amount_threshold
        for _ in range(6):
            floor *= 0.6
            widened = [row for row in amt_list
                       if _to_float(row.get("latest_amt"), 0.0) >= floor]
            if len(widened) >= SCAN_CONFIG["min_stocks"]:
                break
        else:
            widened = list(amt_list)
        widened.sort(key=lambda row: _to_float(row.get("latest_amt"), 0.0),
                     reverse=not ascending)
        filtered = widened[:SCAN_CONFIG["min_stocks"]]
    return filtered, total_amt_yi, market_regime, amount_threshold


MODEL_FEATURE_COLUMNS = (
    "ret_1d", "ret_5d", "ret_10d", "ret_20d", "amt_ratio", "close_vs_ma20_pct",
    "close_vs_ma60_pct", "rsi14", "high20_off_pct", "vol20", "gap_pct",
    "range_pos_pct",
)


def extended_model_features(d, last, close):
    """The features a fitted model needs, all already present in `d`.

    They were being discarded at the row boundary. A ridge model fitted on these
    twelve scored a mean out-of-sample IC of +0.0892 across ten walk-forward
    years - positive in all ten - against +0.0708 for a hand-picked two-feature
    rank. Only three of the twelve were reachable from a scan row, and the
    missing ones carried the 3rd and 4th largest weights (vol20 -0.88,
    close_vs_ma60_pct -0.67), so a scan-based model was discarding most of its
    signal before it began.

    Shared by both row builders: the prewarm path via _compute_daily_snapshot,
    and the decision path, which recomputes the same frame inline. Duplicating
    the arithmetic in two places is how the two paths drift apart.
    """
    close = _to_float(close, 0.0)

    def _pct_off(key):
        v = _to_float(last.get(key), 0.0)
        return ((close / v - 1.0) * 100.0) if v > 0 and close > 0 else 0.0

    def _ret(win):
        if len(d) <= win:
            return 0.0
        prior = _to_float(d["close"].iloc[-1 - win], 0.0)
        return ((close / prior - 1.0) * 100.0) if prior > 0 else 0.0

    high = _to_float(last.get("high"), close)
    low = _to_float(last.get("low"), close)
    open_ = _to_float(last.get("open"), close)
    prev_close = _to_float(d["close"].iloc[-2], close) if len(d) >= 2 else close
    rng = high - low
    hi20 = _to_float(d["close"].tail(20).max(), 0.0) if len(d) >= 20 else 0.0
    ret_series = d["close"].pct_change().mul(100.0)

    return {
        "close_vs_ma60_pct": _pct_off("ma60"),
        "ret_1d": _ret(1),
        "ret_5d": _ret(5),
        "ret_10d": _ret(10),
        "ret_20d": _ret(20),
        # distance below the 20-session high: 0 at a new high, negative under it
        "high20_off_pct": ((close / hi20 - 1.0) * 100.0) if hi20 > 0 else 0.0,
        "vol20": _to_float(ret_series.tail(20).std(), 0.0) if len(d) >= 21 else 0.0,
        "gap_pct": ((open_ / prev_close - 1.0) * 100.0) if prev_close > 0 else 0.0,
        # where the close sits in the session range: 0 at the low, 100 at the high
        "range_pos_pct": ((close - low) / rng * 100.0) if rng > 0 else 50.0,
    }


def _compute_daily_snapshot(daily):
    if daily is None or len(daily) < 60:
        return None
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
        return None
    close = _to_float(last.get("close"), 0.0)
    ma20_off = _to_float(last.get("close_vs_ma20"), 0.0) if pd.notna(last.get("close_vs_ma20")) else 0.0
    amt_r = _to_float(last.get("amt_ratio"), 1.0) if pd.notna(last.get("amt_ratio")) else 1.0
    rsi = _to_float(last.get("rsi14"), 50.0) if pd.notna(last.get("rsi14")) else 50.0

    return {
        "close": close,
        "entry_price": close,
        "latest_amt": _to_float(last.get("amount"), 0.0),
        "close_vs_ma20_pct": ma20_off,
        "amt_ratio": amt_r,
        "rsi14": rsi,
        "is_green": bool(close > _to_float(last.get("open"), close)),
        "vol_expand": bool(1.3 <= amt_r <= 2.5),
        **extended_model_features(d, last, close),
    }


def _resolve_weekly_features(api, market, code, cached_row=None):
    if cached_row is not None:
        cached_align = cached_row.get("weekly_align")
        cached_slope = cached_row.get("weekly_slope")
        if pd.notna(cached_align) and pd.notna(cached_slope):
            return {
                "weekly_align": _to_bool(cached_align),
                "weekly_slope": _to_float(cached_slope, 0.0),
            }
    weekly = fetch_weekly_bars(api, market, code, count=100)
    wfeats = compute_weekly_features(weekly)
    return {
        "weekly_align": _to_bool(wfeats.get("weekly_align", False)),
        "weekly_slope": _to_float(wfeats.get("weekly_slope", 0.0), 0.0),
    }


def _build_signal_row(api, *, code, name, market, latest_snapshot=None, cached_row=None, include_5min, intraday_cutoff=None, daily_count=250):
    daily = fetch_daily_bars(api, market, code, count=daily_count)
    daily_fields = _compute_daily_snapshot(daily)
    if daily_fields is None:
        return None
    weekly_fields = _resolve_weekly_features(api, market, code, cached_row=cached_row)
    latest_amt = daily_fields["latest_amt"]
    if latest_snapshot is not None:
        latest_amt = _to_float(latest_snapshot.get("latest_amt"), latest_amt)
    elif cached_row is not None:
        latest_amt = _to_float(cached_row.get("latest_amt"), latest_amt)

    row = {
        "code": normalize_code(code),
        "name": name,
        "close": daily_fields["close"],
        "entry_price": daily_fields["entry_price"],
        "market": market,
        "latest_amt": latest_amt,
        "tier": 0,
        "mode": "prewarm_pending_decision",
        "position": 0.0,
        "signal_desc": "",
        "bz_direction": np.nan,
        "bz_rt_direction": np.nan,
        "bz_vol_ratio": np.nan,
        "weekly_align": weekly_fields["weekly_align"],
        "weekly_slope": weekly_fields["weekly_slope"],
        "close_vs_ma20_pct": daily_fields["close_vs_ma20_pct"],
        "amt_ratio": daily_fields["amt_ratio"],
        "rsi14": daily_fields["rsi14"],
        "is_green": daily_fields["is_green"],
        "vol_expand": daily_fields["vol_expand"],
        **{k: daily_fields[k] for k in MODEL_FEATURE_COLUMNS if k in daily_fields},
        "strategy_profile_id": _SCANNER_STRATEGY_PROFILE_ID,
        "strategy_profile_hash": _SCANNER_STRATEGY_PROFILE_HASH,
    }
    if not include_5min:
        return row

    min5 = fetch_5min_bars_today(api, market, code, cutoff_at=intraday_cutoff)
    m5feats = compute_5min_signal(min5)
    bz_dir = m5feats.get("bz_direction", np.nan)
    bz_rt = m5feats.get("bz_rt_direction", np.nan)
    bz_vol_r = m5feats.get("bz_vol_ratio", np.nan)
    source_bar_end_at = (
        min5["datetime"].max().strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(min5, pd.DataFrame) and not min5.empty else ""
    )
    decision_cutoff_text = (
        pd.Timestamp(intraday_cutoff).strftime("%Y-%m-%d %H:%M:%S")
        if intraday_cutoff is not None else ""
    )
    decision_cutoff_ready = (
        not decision_cutoff_text or source_bar_end_at == decision_cutoff_text
    )

    row["bz_direction"] = bz_dir
    row["bz_rt_direction"] = bz_rt
    row["bz_vol_ratio"] = bz_vol_r
    row["source_bar_end_at"] = source_bar_end_at
    row["decision_cutoff_at"] = decision_cutoff_text
    row["decision_cutoff_ready"] = bool(decision_cutoff_ready)
    if not decision_cutoff_ready:
        row["tier"] = 0
        row["mode"] = "cutoff_not_ready"
        row["position"] = 0.0
        row["signal_desc"] = "decision cutoff bar not ready"
        return row

    tier, mode, position, desc = classify_signal(
        bz_dir,
        bz_rt,
        weekly_fields["weekly_align"],
        weekly_fields["weekly_slope"],
        daily_fields["close_vs_ma20_pct"],
        daily_fields["vol_expand"],
        daily_fields["rsi14"],
        daily_fields["is_green"],
        daily_fields["amt_ratio"],
    )
    row["tier"] = tier
    row["mode"] = mode
    row["position"] = position
    row["signal_desc"] = desc
    return row


def _append_walk_forward_snapshot(df, *, decision_cutoff_at):
    """Append only rows sourced from the exact 14:50 decision bar."""
    if df.empty:
        return
    cutoff_text = pd.Timestamp(decision_cutoff_at).strftime("%Y-%m-%d %H:%M:%S")
    if "source_bar_end_at" not in df:
        return
    df = df[df["source_bar_end_at"].astype(str) == cutoff_text].copy()
    if df.empty:
        return
    aliases = {
        "bz_direction": "bz_dir",
        "bz_rt_direction": "bz_rt",
        "close_vs_ma20_pct": "ma20_off",
        "rsi14": "rsi",
    }
    snapshot = df.copy()
    for source, target in aliases.items():
        snapshot[target] = snapshot[source] if source in snapshot else np.nan
    snapshot["signal_date"] = cutoff_text[:10]
    snapshot["as_of"] = cutoff_text
    snapshot["observed_at"] = _market_now().strftime("%Y-%m-%d %H:%M:%S")
    snapshot["snapshot_id"] = f"{cutoff_text}|{_SCANNER_STRATEGY_PROFILE_HASH}"
    snapshot["forward_return_pct"] = ""
    snapshot["label_available_date"] = ""
    snapshot["walk_forward_eligible"] = 1
    try:
        if WALK_FORWARD_SNAPSHOT_FILE.exists():
            existing = pd.read_csv(WALK_FORWARD_SNAPSHOT_FILE, dtype=str)
            snapshot = pd.concat([existing, snapshot], ignore_index=True, sort=False)
            snapshot = snapshot.drop_duplicates(subset=["snapshot_id", "code"], keep="first")
        _write_csv_atomic(snapshot, WALK_FORWARD_SNAPSHOT_FILE)
    except OSError as exc:
        print(f"[WARN] unable to persist walk-forward snapshot: {exc}")


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _enforce_decision_cutoff_readiness(df, *, phase, decision_cutoff_text):
    df = df.copy()
    if phase != "decision" or not decision_cutoff_text:
        if "decision_cutoff_ready" in df.columns:
            not_ready_count = int(
                (~df["decision_cutoff_ready"].map(_to_bool)).sum()
            )
        else:
            not_ready_count = 0
        return df, not_ready_count

    if "source_bar_end_at" in df.columns:
        cutoff_ready = (
            df["source_bar_end_at"].fillna("").astype(str)
            == decision_cutoff_text
        )
    else:
        cutoff_ready = pd.Series(False, index=df.index, dtype=bool)
    df["decision_cutoff_at"] = decision_cutoff_text
    df["decision_cutoff_ready"] = cutoff_ready.astype(bool)
    if bool((~cutoff_ready).any()):
        df.loc[~cutoff_ready, "tier"] = 0
        df.loc[~cutoff_ready, "mode"] = "cutoff_not_ready"
        df.loc[~cutoff_ready, "position"] = 0.0
        df.loc[~cutoff_ready, "signal_desc"] = "decision cutoff bar not ready"
    return df, int((~cutoff_ready).sum())


def _write_outputs(
    *,
    df,
    run_time,
    total_amt_yi,
    market_regime,
    amount_threshold,
    scanned_count,
    phase="legacy",
    decision_cutoff_at=None,
    producer_run_id=None,
    run_slot=None,
    started_at=None,
):
    if "tier" not in df.columns:
        df = df.copy()
        df["tier"] = pd.Series(dtype="int64")

    decision_cutoff_text = (
        pd.Timestamp(decision_cutoff_at).strftime("%Y-%m-%d %H:%M:%S")
        if decision_cutoff_at is not None else ""
    )
    trade_date = decision_cutoff_text[:10] or str(run_time)[:10]
    df, cutoff_not_ready_count = _enforce_decision_cutoff_readiness(
        df,
        phase=phase,
        decision_cutoff_text=decision_cutoff_text,
    )
    df_sig = df[df["tier"] > 0].copy()
    run_slot, producer_run_id, artifact_id = _protocol_identity(
        trade_date=trade_date,
        phase=phase,
        producer_run_id=producer_run_id,
        run_slot=run_slot,
    )

    latest_scan_csv = OUTPUT_DIR / "v10_scan_full.csv"
    snapshot_scan_csv = OUTPUT_DIR / f"v10_scan_full.{run_slot}.csv"
    latest_scan_meta = OUTPUT_DIR / "v10_scan_meta.json"
    snapshot_scan_meta = OUTPUT_DIR / f"v10_scan_meta.{run_slot}.json"
    latest_scan_pointer = OUTPUT_DIR / "v10_scan_latest.json"
    latest_prewarm_pointer = OUTPUT_DIR / "v10_prewarm_latest.json"
    latest_decision_pointer = OUTPUT_DIR / "v10_decision_latest.json"

    _write_csv_atomic(df, latest_scan_csv)
    _write_csv_atomic(df, snapshot_scan_csv)
    scan_csv_sha256 = _sha256_file(snapshot_scan_csv)
    published_at = _market_timestamp()
    started_at = str(started_at or published_at)
    signals_by_tier = {
        f"T{tier}": len(df_sig[df_sig["tier"] == tier])
        for tier in [1, 2, 3]
    }
    protocol_fields = {
        "schema_version": SCANNER_ARTIFACT_SCHEMA_VERSION,
        "artifact_id": artifact_id,
        "producer_run_id": producer_run_id,
        "started_at": started_at,
        "published_at": published_at,
        "market_timezone": MARKET_TIMEZONE,
        "scan_csv_sha256": scan_csv_sha256,
        "cutoff_not_ready_count": cutoff_not_ready_count,
        "run_time": str(run_time),
        "run_slot": run_slot,
        "phase": phase,
        "trade_date": trade_date,
        "decision_cutoff_at": decision_cutoff_text,
        "complete": True,
        "requested_count": int(scanned_count),
        "refreshed_count": len(df),
        "strategy_profile_id": _SCANNER_STRATEGY_PROFILE_ID,
        "strategy_profile_hash": _SCANNER_STRATEGY_PROFILE_HASH,
        "stocks_with_signal": len(df_sig),
        "signals_by_tier": signals_by_tier,
    }
    path_fields = {
        "scan_csv": str(snapshot_scan_csv),
        "scan_meta": str(snapshot_scan_meta),
        "latest_scan_csv": str(latest_scan_csv),
        "latest_scan_meta": str(latest_scan_meta),
        "snapshot_scan_csv": str(snapshot_scan_csv),
        "snapshot_scan_meta": str(snapshot_scan_meta),
    }
    scan_meta = {
        **protocol_fields,
        **path_fields,
        "total_csi1000_amt_yi": round(total_amt_yi, 0),
        "market_regime": market_regime,
        "amount_threshold_yi": round(amount_threshold / 1e8, 1),
        "stocks_scanned": int(scanned_count),
    }
    write_json_atomic(snapshot_scan_meta, scan_meta)
    write_json_atomic(latest_scan_meta, scan_meta)

    pointer_payload = {
        **protocol_fields,
        **path_fields,
    }
    write_json_atomic(latest_scan_pointer, pointer_payload)
    if phase == "prewarm":
        write_json_atomic(latest_prewarm_pointer, pointer_payload)
    if phase == "decision":
        # Publish the executable decision before optional research persistence.
        # This final atomic replace transitions the dedicated marker from
        # running/complete=false to the completed immutable artifact pointer.
        write_json_atomic(latest_decision_pointer, pointer_payload)

    if phase == "decision" and decision_cutoff_at is not None:
        try:
            _append_walk_forward_snapshot(df, decision_cutoff_at=decision_cutoff_at)
        except Exception as exc:
            print(f"[WARN] unable to persist walk-forward snapshot: {exc}")

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


def _read_json_object(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_output_snapshot_path(raw_path, *, prefix, forbidden_name):
    text = str(raw_path or "").strip()
    if not text:
        raise RuntimeError("prewarm pointer is missing an artifact path")
    try:
        path = Path(text).resolve()
        path.relative_to(OUTPUT_DIR.resolve())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"prewarm artifact is outside OUTPUT_DIR: {text}") from exc
    if path.name == forbidden_name or not path.name.startswith(prefix):
        raise RuntimeError(f"prewarm artifact is not an immutable snapshot: {path.name}")
    return path


def _load_valid_prewarm_dataframe(expected_trade_date):
    pointer_path = OUTPUT_DIR / "v10_prewarm_latest.json"
    pointer = _read_json_object(pointer_path)
    if not pointer:
        raise RuntimeError(f"missing dedicated prewarm pointer: {pointer_path}")
    expected_trade_date = str(expected_trade_date)
    producer_run_id = str(pointer.get("producer_run_id", "")).strip()
    if pointer.get("complete") is not True:
        raise RuntimeError("prewarm pointer is incomplete")
    if str(pointer.get("phase", "")).strip() != "prewarm":
        raise RuntimeError("prewarm pointer phase mismatch")
    if str(pointer.get("trade_date", "")).strip() != expected_trade_date:
        raise RuntimeError("prewarm pointer trade_date mismatch")
    if str(pointer.get("artifact_id", "")).strip() != f"{expected_trade_date}:prewarm:{producer_run_id}":
        raise RuntimeError("prewarm artifact identity mismatch")
    if str(pointer.get("strategy_profile_id", "")).strip() != _SCANNER_STRATEGY_PROFILE_ID:
        raise RuntimeError("prewarm strategy profile id mismatch")
    if str(pointer.get("strategy_profile_hash", "")).strip() != _SCANNER_STRATEGY_PROFILE_HASH:
        raise RuntimeError("prewarm strategy profile hash mismatch")
    try:
        published_at = _as_market_datetime(pointer.get("published_at"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("prewarm published_at is invalid") from exc
    if published_at.date().isoformat() != expected_trade_date:
        raise RuntimeError("prewarm published_at trade_date mismatch")

    csv_path = _resolve_output_snapshot_path(
        pointer.get("scan_csv"),
        prefix="v10_scan_full.",
        forbidden_name="v10_scan_full.csv",
    )
    meta_path = _resolve_output_snapshot_path(
        pointer.get("scan_meta"),
        prefix="v10_scan_meta.",
        forbidden_name="v10_scan_meta.json",
    )
    if not csv_path.is_file() or not meta_path.is_file():
        raise RuntimeError("prewarm immutable snapshot is missing")
    meta = _read_json_object(meta_path)
    comparable_fields = (
        "schema_version", "artifact_id", "producer_run_id", "run_time", "run_slot",
        "phase", "trade_date", "complete", "published_at", "market_timezone",
        "requested_count", "refreshed_count", "strategy_profile_id",
        "strategy_profile_hash", "scan_csv_sha256",
    )
    mismatched = [field for field in comparable_fields if meta.get(field) != pointer.get(field)]
    if mismatched:
        raise RuntimeError(f"prewarm pointer/meta mismatch: {','.join(mismatched)}")
    actual_hash = _sha256_file(csv_path)
    if actual_hash != str(pointer.get("scan_csv_sha256", "")).strip().lower():
        raise RuntimeError("prewarm CSV SHA-256 mismatch")
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    if len(df) != int(pointer.get("refreshed_count", -1)):
        raise RuntimeError("prewarm CSV row count mismatch")
    if not df.empty:
        if "strategy_profile_id" not in df or "strategy_profile_hash" not in df:
            raise RuntimeError("prewarm CSV is missing strategy profile provenance")
        if not df["strategy_profile_id"].astype(str).eq(_SCANNER_STRATEGY_PROFILE_ID).all():
            raise RuntimeError("prewarm CSV strategy profile id mismatch")
        if not df["strategy_profile_hash"].astype(str).eq(_SCANNER_STRATEGY_PROFILE_HASH).all():
            raise RuntimeError("prewarm CSV strategy profile hash mismatch")
    return df


def _run_decision_fast(api, *, run_time, producer_run_id=None):
    decision_lock = _acquire_decision_lock()
    try:
        context = _decision_protocol_context(
            run_time=run_time,
            producer_run_id=producer_run_id,
        )
        _publish_decision_running_marker(context)
        return _run_decision_fast_impl(
            api,
            run_time=run_time,
            context=context,
        )
    finally:
        _release_decision_lock(decision_lock)


def _run_decision_fast_impl(api, *, run_time, context):
    df = _load_valid_prewarm_dataframe(context["trade_date"])
    if "code" in df.columns:
        df["code"] = df["code"].map(normalize_code)
    if "market" not in df.columns:
        market_df = get_stock_list()[["code", "market"]].copy()
        market_df["code"] = market_df["code"].map(normalize_code)
        df["code"] = df["code"].map(normalize_code)
        df = df.merge(market_df, on="code", how="left")

    stocks = get_stock_list()
    amt_list = _collect_amount_snapshot(api, stocks)
    filtered, total_amt_yi, market_regime, amount_threshold = _select_amount_candidates(amt_list)
    print(f"Decision fast mode: {len(filtered)} live amount candidates from {market_regime}")
    print(f"  当前成交额阈值: {amount_threshold/1e8:.1f}亿")

    cached_rows = {
        normalize_code(row.get("code", "")): row
        for row in df.to_dict(orient="records")
        if str(row.get("code", "")).strip()
    }
    candidate_specs = []
    seen_codes = set()
    for snapshot in filtered:
        code = normalize_code(snapshot.get("code", ""))
        cached_row = cached_rows.get(code)
        name = snapshot.get("name") or (cached_row.get("name") if cached_row else "")
        market = int(_to_float(snapshot.get("market"), _to_float(cached_row.get("market") if cached_row else 1, 1)))
        candidate_specs.append({
            "code": code,
            "name": name,
            "market": market,
            "latest_snapshot": snapshot,
            "cached_row": cached_row,
        })
        seen_codes.add(code)

    for code, cached_row in cached_rows.items():
        if code in seen_codes:
            continue
        candidate_specs.append({
            "code": code,
            "name": cached_row.get("name", ""),
            "market": int(_to_float(cached_row.get("market", 1), 1)),
            "latest_snapshot": None,
            "cached_row": cached_row,
        })

    total_candidates = len(candidate_specs)
    if total_candidates > DECISION_MAX_CANDIDATES:
        candidate_specs = candidate_specs[:DECISION_MAX_CANDIDATES]
        print(
            f"  -> 决策阶段按成交额优先刷新 {len(candidate_specs)}/{total_candidates}只 "
            f"(上限={DECISION_MAX_CANDIDATES})，避免占用尾盘买入窗口"
        )
    else:
        print(f"  -> 决策阶段混合刷新 {total_candidates}只 (实时候选{len(filtered)} + 预热缓存补集{total_candidates - len(filtered)})")
    decision_cutoff_at = context["decision_cutoff_at"]
    _wait_for_decision_fetch_window(context["trade_date"])
    refreshed_rows = []
    total = len(candidate_specs)
    for idx, spec in enumerate(candidate_specs):
        code = spec["code"]
        row = _build_signal_row(
            api,
            code=code,
            name=spec["name"],
            market=spec["market"],
            latest_snapshot=spec["latest_snapshot"],
            cached_row=spec["cached_row"],
            include_5min=True,
            intraday_cutoff=decision_cutoff_at,
            daily_count=DECISION_DAILY_COUNT,
        )
        if row is None:
            continue
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
        total_amt_yi=total_amt_yi,
        market_regime=market_regime,
        amount_threshold=amount_threshold,
        scanned_count=len(candidate_specs),
        phase="decision",
        decision_cutoff_at=decision_cutoff_at,
        producer_run_id=context["producer_run_id"],
        run_slot=context["run_slot"],
        started_at=context["started_at"],
    )


def _run_prewarm_fast(api, *, run_time, producer_run_id=None):
    stocks = get_stock_list()
    print(f"CSI1000: {len(stocks)} stocks, pytdx connected\n")
    print("Phase 1: Dynamic amount filter (市场冷热自适应)...")
    amt_list = _collect_amount_snapshot(api, stocks)
    filtered, total_amt_yi, market_regime, amount_threshold = _select_amount_candidates(amt_list)

    print(f"  中证1000总成交额: {total_amt_yi:.0f}亿 | 市场状态: {market_regime}")
    print(f"  个股成交额阈值: {amount_threshold/1e8:.1f}亿")
    amt_df = pd.DataFrame(filtered)
    print(f"  -> 预热扫描{len(amt_df)}只 (阈值>={amount_threshold/1e8:.1f}亿, 范围{SCAN_CONFIG['min_stocks']}-{SCAN_CONFIG['max_stocks']})\n")
    print("Phase 2: Compute daily + weekly features only...")
    results = []
    total = len(amt_df)
    for i, (_, row) in enumerate(amt_df.iterrows()):
        built = _build_signal_row(
            api,
            code=row["code"],
            name=row["name"],
            market=row["market"],
            latest_snapshot=row.to_dict(),
            cached_row=None,
            include_5min=False,
        )
        if built is None:
            continue
        results.append(built)
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
        phase="prewarm",
        producer_run_id=producer_run_id,
    )


def _build_arg_parser():
    parser = argparse.ArgumentParser(description="V10 scanner")
    parser.add_argument("--decision-fast", action="store_true", help="reuse latest scan and refresh 5-min tail only")
    parser.add_argument("--prewarm-fast", action="store_true", help="prewarm only daily/weekly base features and defer 5-min classification to decision")
    parser.add_argument("--task-name", default="", help="scheduler task name")
    parser.add_argument("--trigger-slot", default="", help="scheduler trigger slot")
    parser.add_argument("--run-id", default="", help="producer run identifier")
    return parser


def main(argv=None):
    args = _build_arg_parser().parse_args(argv)
    # #region debug-point A:scanner-main-start
    _main_strategy_debug_emit(
        "A",
        "scanner_v10.py:main",
        "[DEBUG] scanner main started",
        {
            "started_at": _market_now().strftime("%Y-%m-%d %H:%M:%S"),
            "decision_fast": bool(args.decision_fast),
            "prewarm_fast": bool(args.prewarm_fast),
        },
    )
    # #endregion
    print("=" * 70)
    print("V10 Scanner: Multi-Tier + Multi-Mode Real-Time Scanner")
    print("Philosophy: 大肉小肉都是肉 — every day is a trading day")
    print("=" * 70)
    run_time = _market_now().strftime("%Y-%m-%d %H:%M:%S")
    decision_cutoff_at = _decision_cutoff_for(_as_market_datetime(run_time).date())
    print(f"Run time: {run_time}\n")
    api = connect_tdx()
    try:
        if args.decision_fast:
            print("Decision fast mode: reuse latest prewarm scan and refresh 5-min tail only...")
            _run_decision_fast(
                api,
                run_time=run_time,
                producer_run_id=args.run_id,
            )
            return
        if args.prewarm_fast:
            print("Prewarm fast mode: cache daily/weekly base features and defer 5-min refresh to decision...")
            _run_prewarm_fast(
                api,
                run_time=run_time,
                producer_run_id=args.run_id,
            )
            return

        stocks = get_stock_list()
        print(f"CSI1000: {len(stocks)} stocks, pytdx connected\n")
        # Loaded once per run, from disk, before any pytdx data is trusted.
        trusted_prices = _load_trusted_reference_prices(
            _as_market_datetime(run_time).date().isoformat()
        )
        print("Phase 1: Dynamic amount filter (市场冷热自适应)...")
        amt_list = _collect_amount_snapshot(api, stocks)
        filtered, total_amt_yi, market_regime, amount_threshold = _select_amount_candidates(amt_list)

        print(f"  中证1000总成交额: {total_amt_yi:.0f}亿 | 市场状态: {market_regime}")
        print(f"  个股成交额阈值: {amount_threshold/1e8:.1f}亿")

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
            min5 = fetch_5min_bars_today(api, market, code, cutoff_at=decision_cutoff_at)
            m5feats = compute_5min_signal(min5)
            bz_dir = m5feats.get("bz_direction", np.nan)
            bz_rt = m5feats.get("bz_rt_direction", np.nan)
            bz_vol_r = m5feats.get("bz_vol_ratio", np.nan)
            tier, mode, position, desc = classify_signal(
                bz_dir, bz_rt, weekly_align, weekly_slope, ma20_off,
                vol_exp, rsi, is_green, amt_r
            )
            # entry_price comes from the realtime quote; d.iloc[-1] comes from a
            # separate daily-bar request. Two independent round trips - if they
            # disagree by more than the board can move in a day, one is wrong and
            # this row must not become a tradable candidate.
            bar_close = _to_float(last.get("close"), 0.0)
            if not _price_pair_agrees(code, last_close, bar_close):
                _PRICE_MISMATCH_LOG.append({
                    "code": code, "name": name,
                    "quote_price": round(_to_float(last_close, 0.0), 4),
                    "bar_close": round(bar_close, 4),
                })
                continue
            # Both values above come from the same pytdx session, so they agree
            # whenever that session is consistently wrong. This second check is
            # against a source outside pytdx and is what actually catches it.
            if _trusted_price_disagrees(code, last_close, trusted_prices):
                _TRUSTED_MISMATCH_LOG.append({
                    "code": code, "name": name,
                    "scan_price": round(_to_float(last_close, 0.0), 4),
                    "trusted_price": round(
                        _to_float(trusted_prices.get(str(code).zfill(6)), 0.0), 4),
                })
                continue

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
                **extended_model_features(d, last, last_close),
                "strategy_profile_id": _SCANNER_STRATEGY_PROFILE_ID,
                "strategy_profile_hash": _SCANNER_STRATEGY_PROFILE_HASH,
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
            producer_run_id=args.run_id,
        )
    finally:
        api.disconnect()


if __name__ == "__main__":
    main()
