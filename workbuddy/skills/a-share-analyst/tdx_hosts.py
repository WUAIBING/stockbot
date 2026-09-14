#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pick a pytdx server that actually returns prices, not one that merely answers.

THE OUTAGE THIS EXISTS FOR

From 2026-09-10 every server in our lists kept accepting connections and kept
serving reference data - security counts, security lists - while returning
EMPTY quotes, daily bars and index bars. Every connection routine in the
system judged a host by `api.connect()` alone, so each one happily chose a dead
server, and nothing downstream could tell "no data" from "a quiet market":

    scanner      total turnover 0 -> "清淡市" -> floor 3亿 -> scanned 0 names
    smart-sell   no bars -> no evidence -> "信号完好" -> held all 13 positions
    freshness    "connected" -> status ok, every phase, every day

Zero entries on 09-10, 09-11 and 09-14; zero exits in the same three sessions
against 8, 11, 2 and 5 a session before. Silent throughout.

A sweep of pytdx's 103 known hosts on 09-14 found 71 refusing connections, 24
connecting but returning nothing, and 8 serving real prices. The four hosts we
had configured were all in the 24.

So a host is accepted here only after it returns a priced quote AND a daily
bar. The last host that passed is remembered, so the next process tries it
first rather than walking a list of servers already known to be empty.

Six modules each carried their own copy of the host list. This is the one list.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Verified 2026-09-14 by a full sweep: these returned priced quotes and daily
# bars. Order is preference. Seven share a subnet, so they probably share an
# operator and could fail together - which is why the sweep fallback exists.
VERIFIED_HOSTS = [
    ("117.34.114.14", 7709),
    ("117.34.114.15", 7709),
    ("117.34.114.16", 7709),
    ("117.34.114.17", 7709),
    ("117.34.114.18", 7709),
    ("117.34.114.20", 7709),
    ("117.34.114.27", 7709),
    ("59.36.5.11", 7709),
]

# The previous configured lists. Kept, not deleted: servers recover, and a list
# that only ever shrinks eventually runs dry. They sit behind the verified ones.
LEGACY_HOSTS = [
    ("218.75.126.9", 7709),
    ("60.191.117.167", 7709),
    ("39.105.251.234", 7709),
    ("119.147.212.83", 7709),
    ("119.147.212.81", 7709),
    ("114.80.63.12", 7709),
    ("180.153.18.170", 7709),
    ("112.74.214.43", 7727),
    ("221.231.141.60", 7709),
]

TDX_HOSTS = VERIFIED_HOSTS + [h for h in LEGACY_HOSTS if h not in VERIFIED_HOSTS]

# A liquid name on each exchange. Both must price: a server can serve one
# market and not the other.
PROBE_SECURITIES = [(0, "000001"), (1, "600000")]
PROBE_BAR_SECURITY = (1, "600000")

CACHE_FILE = Path(os.environ.get(
    "TDX_HOST_CACHE_FILE",
    str(Path(__file__).resolve().parent.parent.parent / "a-share-analyst" / "tdx_host_last_good.json"),
))


class TdxDataUnavailable(RuntimeError):
    """No reachable server returned prices. Callers must not treat this as a
    quiet market or an intact signal - it is the absence of evidence."""


def serves_prices(api):
    """True only when the connected server returns real prices and bars."""
    try:
        quotes = api.get_security_quotes(PROBE_SECURITIES) or []
    except Exception:
        return False
    priced = 0
    for quote in quotes:
        try:
            if float((quote or {}).get("price") or 0) > 0:
                priced += 1
        except (TypeError, ValueError, AttributeError):
            continue
    if priced < len(PROBE_SECURITIES):
        return False
    try:
        market, code = PROBE_BAR_SECURITY
        bars = api.get_security_bars(9, market, code, 0, 1) or []
    except Exception:
        return False
    return len(bars) > 0


def _read_cache():
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        host, port = str(payload["host"]), int(payload["port"])
        return (host, port)
    except Exception:
        return None


def _write_cache(host, port):
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "host": host, "port": int(port),
            "verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }), encoding="utf-8")
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass


def _ordered_candidates(extra=None):
    seen, ordered = set(), []
    cached = _read_cache()
    for host in ([cached] if cached else []) + list(extra or []) + TDX_HOSTS:
        key = (str(host[0]), int(host[1]))
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def _try(api_factory, host, port, time_out, heartbeat):
    api = api_factory(heartbeat=heartbeat) if heartbeat else api_factory()
    try:
        if api.connect(host, port, time_out=time_out) and serves_prices(api):
            return api
    except Exception:
        pass
    try:
        api.disconnect()
    except Exception:
        pass
    return None


def _builtin_hosts():
    try:
        from pytdx.config.hosts import hq_hosts
        return [(str(h[1]), int(h[2])) for h in hq_hosts]
    except Exception:
        return []


def connect_verified(api_factory=None, *, time_out=3.0, heartbeat=True,
                     sweep=True, extra_hosts=None, log=print):
    """(api, "host:port") for a server proven to return prices.

    Tries the last good host, then the curated list, one at a time. Only if all
    of those fail does it sweep pytdx's built-in host list in parallel, which is
    how the eight working servers were found in the first place.

    Raises TdxDataUnavailable when nothing serves prices. It never returns an
    api that merely connected - that is the exact failure being removed.
    """
    if api_factory is None:
        from pytdx.hq import TdxHq_API as api_factory
    tried = 0
    for host, port in _ordered_candidates(extra_hosts):
        tried += 1
        api = _try(api_factory, host, port, time_out, heartbeat)
        if api is not None:
            _write_cache(host, port)
            return api, "%s:%d" % (host, port)

    if sweep:
        curated = set(_ordered_candidates(extra_hosts))
        pool = [h for h in _builtin_hosts() if h not in curated]
        if pool:
            if log:
                log("[WARN] tdx: none of %d curated hosts serve prices - sweeping %d more"
                    % (tried, len(pool)))

            def probe(hp):
                api = _try(api_factory, hp[0], hp[1], min(time_out, 2.5), False)
                if api is not None:
                    try:
                        api.disconnect()
                    except Exception:
                        pass
                    return hp
                return None

            with ThreadPoolExecutor(max_workers=24) as ex:
                found = [hp for hp in ex.map(probe, pool) if hp]
            for host, port in found:
                api = _try(api_factory, host, port, time_out, heartbeat)
                if api is not None:
                    _write_cache(host, port)
                    if log:
                        log("[WARN] tdx: recovered via sweep on %s:%d "
                            "(add it to VERIFIED_HOSTS)" % (host, port))
                    return api, "%s:%d" % (host, port)
            tried += len(pool)

    raise TdxDataUnavailable(
        "no pytdx server returned prices (%d hosts tried) - market data is "
        "unavailable, which is not the same as a quiet market" % tried)


def reconnect_verified(api, *, time_out=3.0, log=print):
    """Re-point an existing api object at a server that serves prices.

    For mid-run resyncs that must keep the same object. True on success.
    """
    try:
        api.disconnect()
    except Exception:
        pass
    for host, port in _ordered_candidates():
        try:
            if api.connect(host, port, time_out=time_out) and serves_prices(api):
                _write_cache(host, port)
                return True
        except Exception:
            pass
        try:
            api.disconnect()
        except Exception:
            pass
    if log:
        log("[ERROR] tdx: reconnect found no host serving prices")
    return False
