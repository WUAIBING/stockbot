#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Market volatility regime - and the history the droplet needs to see it.

WHAT WAS MEASURED

Momentum in A-shares is switched by volatility. Over 2015-2026, splitting
sessions by the trailing 20-session volatility of the equal-weighted liquid
universe, and measuring the forward excess of strong stocks minus weak:

    next 10 sessions          strength effect        t
      calm  (bottom third)         +0.225%       +2.13
      mid                          -0.403%       -3.46
      wild  (top third)            -1.501%      -10.49

Monotonic at 5, 10 and 20 sessions. The placebo - the same split on a regime
value read 40 sessions LATER - comes out flat (-0.47 to -0.57 across all three
buckets), so the split finds regime rather than calendar.

The account's own record lands exactly on it:

    2026-06   vol 1.228 (51st pct)   +3.12%/trade   <- the only profitable month
    2026-07   vol 1.577 (74th pct)   -1.96%/trade
    2026-08   vol 1.719 (78th pct)   -0.76%/trade

Two consecutive top-tercile months running momentum entries. And nothing in
_compute_market_score looks at volatility - it is breadth, tier mix and
turnover only.

WHY THIS MODULE ARCHIVES INSTEAD OF ESTIMATING

The droplet has no market price history: no TDX dailies, no index. The two
statistics it could reach both fail:

  * the scan CSV's own vol20 column reads 4.971 where the liquid universe reads
    2.294 for 2026-08-27 - more than double, because the scan is a FILTERED set
    that selects volatile breakout names. Applying universe thresholds to it
    would mark nearly every day wild. That is the ADD_POSITION_BIG_MEAT_SECTOR_
    SCORE mistake exactly: a threshold calibrated on one population, applied to
    another.
  * cross-sectional dispersion, which one day's data can give, correlates only
    0.380 with the quantity that matters and agrees on tercile 44.9% of the
    time against a 33% coin. Classifying the regime wrong half the time is
    worse than not classifying it.

So this does not estimate the regime from what is at hand. It ARCHIVES what is
needed - the tradability gate already publishes last_close for the whole
tradable universe every session - and refuses to classify until enough sessions
have accumulated. Twenty-one closes gives the first reading; before that
`classify` returns "unknown" and says why.

Thresholds are the measured terciles of that exact statistic. Nothing here
changes scoring: it reports a regime. Gated on TLFZ_VOL_REGIME for any consumer
that later wants to act on it.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime

VOL_REGIME_ENABLED = str(
    os.environ.get("TLFZ_VOL_REGIME", "0")).strip().lower() in ("1", "true", "yes", "on")

# Terciles of the trailing 20-session volatility of the equal-weighted liquid
# universe, over 2,648 sessions from 2015-10 to 2026-08.
CALM_BELOW = 1.039
WILD_ABOVE = 1.422

WINDOW = 20
MIN_SESSIONS = WINDOW + 1        # 21 closes -> 20 returns -> one volatility

CALM, MID, WILD, UNKNOWN = "calm", "mid", "wild", "unknown"


def snapshot_universe(tradability_payload):
    """{code: last_close} for every name the 09:31 gate priced.

    last_close, not last_price: the gate runs at 09:31, so last_price is a
    partial session and a series built from it would not be daily closes. The
    previous close is complete, which is why the archive lags one session and
    that is fine - the regime is a 20-session statistic.
    """
    out = {}
    records = (tradability_payload or {}).get("records") or []
    for r in records:
        code = str(r.get("code", "")).zfill(6)
        try:
            close = float(r.get("last_close") or 0)
        except (TypeError, ValueError):
            continue
        if code and close > 0:
            out[code] = close
    return out


def append_close_snapshot(store_path, trade_date, snapshot):
    """One line per session. Re-running a session replaces it, never duplicates."""
    rows = load_close_store(store_path)
    rows = [r for r in rows if r.get("trade_date") != str(trade_date)]
    rows.append({"trade_date": str(trade_date), "n": len(snapshot),
                 "closes": snapshot,
                 "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    rows.sort(key=lambda r: r.get("trade_date") or "")
    tmp = "%s.tmp" % store_path
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, store_path)
    return len(rows)


def load_close_store(store_path):
    rows = []
    try:
        with open(store_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return sorted(rows, key=lambda r: r.get("trade_date") or "")


def universe_returns(rows):
    """Equal-weighted mean daily return, over names present in BOTH sessions.

    Intersecting matters: the tradable universe changes as listings suspend and
    resume, and a name that appears or vanishes would otherwise contribute a
    phantom return. 688432 有研硅 is the live case - suspended 2026-08-31 with
    no reopen date, so it drops out and must not read as a move.
    """
    out = []
    for prev, cur in zip(rows, rows[1:]):
        a, b = prev.get("closes") or {}, cur.get("closes") or {}
        rets = []
        for code, c1 in b.items():
            c0 = a.get(code)
            if c0 and c1 and c0 > 0:
                r = (c1 / c0 - 1.0) * 100.0
                if abs(r) <= 25:
                    rets.append(r)
        if len(rets) >= 30:
            out.append({"trade_date": cur.get("trade_date"),
                        "ret": sum(rets) / len(rets), "n": len(rets)})
    return out


def realised_vol(returns, window=WINDOW):
    vals = [r["ret"] for r in returns][-window:]
    if len(vals) < window:
        return None
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def classify(vol, calm_below=CALM_BELOW, wild_above=WILD_ABOVE):
    """(label, reason). Never guesses - "unknown" when it cannot know."""
    if vol is None:
        return UNKNOWN, ("not enough universe history yet - needs %d closes"
                         % MIN_SESSIONS)
    if vol <= calm_below:
        return CALM, ("vol %.3f <= %.3f: momentum measured +0.225%% over 10 "
                      "sessions (t+2.13)" % (vol, calm_below))
    if vol >= wild_above:
        return WILD, ("vol %.3f >= %.3f: momentum measured -1.501%% over 10 "
                      "sessions (t-10.49)" % (vol, wild_above))
    return MID, ("vol %.3f between %.3f and %.3f: momentum measured -0.403%% "
                 "(t-3.46)" % (vol, calm_below, wild_above))


def regime_from_store(store_path):
    rows = load_close_store(store_path)
    if len(rows) < MIN_SESSIONS:
        return {"regime": UNKNOWN, "vol": None, "sessions": len(rows),
                "reason": "have %d of %d closes needed" % (len(rows), MIN_SESSIONS),
                "enabled": VOL_REGIME_ENABLED}
    rets = universe_returns(rows)
    vol = realised_vol(rets)
    label, reason = classify(vol)
    return {"regime": label, "vol": (round(vol, 4) if vol is not None else None),
            "sessions": len(rows), "returns": len(rets), "reason": reason,
            "enabled": VOL_REGIME_ENABLED}
