#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ask the market what a price is, right now, and say when you asked.

WHY THIS EXISTS

The position cache only refreshes when a trading node runs. Between nodes -
and through the whole 11:30-13:00 lunch break - it is frozen, which is correct
behaviour and not a bug. The bug was mine: on 2026-09-02 I read a cache stamped
11:15:30 and reported it as the lunch close. The price had moved 7.26 -> 7.18 in
those fifteen minutes, so the number was not merely old, it was wrong for the
question asked.

That was the third time in one session. I called the 09:31 gate "the whole
market" when it is CSI 1000; I quoted 09:31 breadth at 09:46 as current; then
this. The common fault is not bad data - it is reporting the freshest number I
have as though it were the current number.

So every quote here carries `fetched_at` and `age_seconds`, and the formatter
refuses to print a price without them. A number whose age is invisible will
eventually be read as live by someone, and that someone was me.

READ-ONLY. This opens a market-data socket and asks for prices. It cannot place,
amend or cancel an order, and a test asserts the module holds no execution path.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

# The droplet runs UTC. Services that care pin TZ=Asia/Shanghai, but an ad-hoc
# call does not, so datetime.now() there returns 04:26 for a 12:26 market. In a
# module whose whole point is an unambiguous timestamp, a naive local clock is
# the same bug in smaller print - the stamp must say which market minute it is.
CHINA = timezone(timedelta(hours=8))

TDX_HOSTS = [
    ("119.147.212.81", 7709),
    ("119.147.212.83", 7709),
    ("114.80.63.12", 7709),
    ("180.153.18.170", 7709),
]

CONNECT_TIMEOUT = 3.0
RETRIES = 2

# pytdx market ids. Getting this wrong returns a plausible price for the WRONG
# security rather than an error, which is the pytdx desync signature that put
# 22.92 in the ledger for a stock trading at 155.65.
MARKET_SZ, MARKET_SH, MARKET_BJ = 0, 1, 2


def market_of(code):
    """Exchange for a 6-digit A-share code, or None if it is not one.

    The padding exists because codes arrive from JSON as ints (636 -> 000636),
    but it must not manufacture a code out of nothing: "".zfill(6) is "000000",
    a syntactically perfect Shenzhen code for a security that does not exist.
    So the input is validated BEFORE padding, not after.
    """
    raw = str(code if code is not None else "").strip()
    if not raw or not raw.isdigit() or len(raw) > 6:
        return None
    c = raw.zfill(6)
    if c[0] == "6":
        return MARKET_SH
    if c[0] in ("0", "1", "2", "3"):
        return MARKET_SZ
    if c[0] in ("4", "8", "9"):
        return MARKET_BJ
    return None


def _connect():
    from pytdx.hq import TdxHq_API
    for _ in range(RETRIES):
        for host, port in TDX_HOSTS:
            api = TdxHq_API(heartbeat=True)
            try:
                if api.connect(host, port, time_out=CONNECT_TIMEOUT):
                    return api, "%s:%d" % (host, port)
            except Exception:
                pass
            try:
                api.disconnect()
            except Exception:
                pass
        time.sleep(0.3)
    return None, None


def get_quotes(codes):
    """{code: quote} for A-share codes. Missing codes are simply absent.

    Each quote carries the moment it was fetched. Callers that want to know
    whether a price is current must read `age_seconds` rather than assume.
    """
    wanted = []
    for c in codes or []:
        c = str(c).strip().zfill(6)
        m = market_of(c)
        if m is not None:
            wanted.append((m, c))
    if not wanted:
        return {}
    api, endpoint = _connect()
    if api is None:
        return {}
    out = {}
    try:
        now = time.time()
        stamp = datetime.now(CHINA).strftime("%Y-%m-%d %H:%M:%S CST")
        for i in range(0, len(wanted), 80):        # pytdx caps a batch at 80
            rows = api.get_security_quotes(wanted[i:i + 80]) or []
            for r in rows:
                code = str(r.get("code") or "").zfill(6)
                if not code:
                    continue
                last = float(r.get("price") or 0)
                prev = float(r.get("last_close") or 0)
                out[code] = {
                    "code": code,
                    "price": last,
                    "last_close": prev,
                    "open": float(r.get("open") or 0),
                    "high": float(r.get("high") or 0),
                    "low": float(r.get("low") or 0),
                    "volume": int(r.get("vol") or 0),
                    "change_pct": (round((last / prev - 1) * 100, 3)
                                   if last > 0 and prev > 0 else None),
                    "fetched_at": stamp,
                    "fetched_ts": now,
                    "source": "pytdx %s" % endpoint,
                }
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    return out


def age_seconds(quote, as_of=None):
    try:
        return max(0.0, (as_of or time.time()) - float(quote["fetched_ts"]))
    except (KeyError, TypeError, ValueError):
        return None


def format_quote(quote, name=""):
    """One line, and it will not print a price without its age.

    Deliberately strict: the whole reason this module exists is a price that
    was reported without its timestamp and therefore read as current.
    """
    if not isinstance(quote, dict) or "fetched_ts" not in quote:
        return "no quote (and no timestamp, so nothing is printable)"
    age = age_seconds(quote)
    chg = quote.get("change_pct")
    return ("%-8s %-10.10s %8.2f  %+7.2f%%  prev %.2f  open %.2f  "
            "[fetched %s, %.0fs ago]"
            % (quote.get("code", ""), name, quote.get("price", 0.0),
               chg if chg is not None else float("nan"),
               quote.get("last_close", 0.0), quote.get("open", 0.0),
               quote.get("fetched_at", "?"), age if age is not None else -1))
