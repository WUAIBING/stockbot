#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Market volatility regime - measured on the universe we actually archive.

WHAT WAS MEASURED

Momentum in A-shares is switched by volatility. Splitting 2015-2026 by the
trailing 20-session volatility of the archived universe's equal-weighted return,
and measuring the forward excess of strong stocks minus weak:

    next 10 sessions        strength effect        t
      calm  (bottom third)       +0.152%       +1.47
      mid                        -0.284%       -2.41
      wild  (top third)          -1.596%      -11.02

    next 20 sessions             +0.127% / -0.722% / -2.563%   (t-14.47 wild)

Monotonic at 5, 10 and 20 sessions. A placebo - the same split on a regime value
read 40 sessions LATER - goes flat, so this finds regime and not calendar.

The account's record lands on it: 2026-06 was its only profitable month
(+3.12%/trade) at the 51st volatility percentile, while 2026-07 (-1.96%) and
2026-08 (-0.76%) sat at the 74th and 78th. Nothing in _compute_market_score
looks at volatility - it is breadth, tier mix and turnover only.

THE UNIVERSE IS CSI 1000, NOT THE MARKET

This matters enough to state twice. The 09:31 tradability gate publishes
`scanner_exchange_universe`, which is the CSI 1000 constituent list from
csi1000-skills/000852cons.xls - 1,042 names with EVERY large cap excluded by
construction. 600519, 601398, 300750 are all absent.

So the archive stores small/mid caps, and thresholds calibrated on the liquid
whole market do not belong to it. The first cut of this module shipped
1.0390/1.4220 from a whole-market calibration; applied to the archived series
those read CALM 28% / mid 34% / WILD 38% of days instead of 33/33/33 - a real
skew toward "wild", from measuring one population and deploying on another. The
constants below are recalibrated on the gate universe itself.

That miscalibration was mild only by luck: the two series correlate 0.996 and
agree on tercile 94.5% of the time, because CSI 1000 volatility tracks the
market's closely. The same mistake with the scan CSV's own vol20 column would
NOT have been mild - it reads 4.971 where the liquid universe reads 2.294,
correlating 0.380 and agreeing 44.9% of the time against a 33% coin. Population
before threshold, every time.

Breadth is where the difference bites hardest and is NOT interpolatable: on the
panel, top-300-by-turnover breadth minus next-1000 breadth ranges from -18.9 to
+23.1 points day to day. A CSI 1000 reading is not a market reading, and no
constant corrects it.

WHY THIS ARCHIVES INSTEAD OF ESTIMATING

The droplet has no market price history - no TDX dailies, no index - so it
cannot compute this at all today. Rather than estimate the regime from what is
at hand and be wrong, this stores the universe closes the gate already
publishes and refuses to classify until 21 sessions exist.

If the gate is ever widened to the whole market, these thresholds stop applying.
`regime_from_store` therefore checks the archived universe size against the size
it was calibrated on and refuses rather than silently reading the wrong scale.

READING TODAY INSTEAD OF NEXT MONTH

The archive needs 21 gate sessions, which put the first reading a month out.
That wait was an artefact of where the data lived: the same universe's history
was already on the panel. vol_regime_seed.json carries 160 sessions of
panel-derived universe returns, so the regime reads now and the archive takes
over as it fills. Archived sessions always win an overlap, and every result
reports how many of its returns were seeded versus archived.

The seed is checked against the calibration universe size like everything else,
and refused if it does not match.

WHY THIS SURVIVES T+1, WHEN THE ENTRY SCORE DOES NOT

The entry score is sharpest on a move already spent by the 14:50 decision -
same-day +2.15% (t+2.45), then T+1 +0.40% and T+2 -0.42%. Knowing it today does
not help, because the first tradeable session is tomorrow.

Volatility is the opposite: it clusters. Today's regime is tomorrow's regime
93.0% of the time overall, and 95.0% of the time when today is wild:

    horizon   same regime   wild -> still wild
    T+1           93.0%          95.0%
    T+2           88.6%          91.6%
    T+5           77.4%          83.2%
    T+10          65.5%          72.6%      (random baseline ~33%)

And the effect was always measured forward - conditioner from trailing data at
t, outcome from t onward. Buying into a wild reading and holding: -0.049% at 1
session (t-0.93), -0.280% at 2 (t-3.99), -0.749% at 5 (t-6.39), -1.499% at 10
(t-10.19). Note the first session is nil; this protects holds of two sessions
or more, which is most of them but not all.

WHAT IT DOES, ONCE ENABLED

It scales NEW entry exposure and nothing else - selection, exits and existing
positions are untouched. Wild takes 40% size; calm and mid are unchanged; an
unknown regime takes full size so that a data outage cannot quietly halt the
book. See entry_exposure_multiplier for why exposure rather than the score bar.

Gated on TLFZ_VOL_REGIME. With it off, the multiplier is always 1.0.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime

VOL_REGIME_ENABLED = str(
    os.environ.get("TLFZ_VOL_REGIME", "0")).strip().lower() in ("1", "true", "yes", "on")

# Terciles of the trailing 20-session volatility of the equal-weighted GATE
# universe (CSI 1000), over 2,648 sessions from 2015-10 to 2026-08. Calibrated
# on the same population the archive stores - see the header for why the
# previous whole-market figures (1.0390/1.4220) were wrong here.
CALM_BELOW = 1.0969
WILD_ABOVE = 1.4802

# Names the calibration ran on: 1,042 gate codes, 1,037 present in the panel.
# A universe that has drifted far from this is a different population and these
# thresholds no longer describe it.
CALIBRATION_UNIVERSE_SIZE = 1037
UNIVERSE_TOLERANCE = 0.25

WINDOW = 20
MIN_SESSIONS = WINDOW + 1        # 21 closes -> 20 returns -> one volatility

CALM, MID, WILD, UNKNOWN = "calm", "mid", "wild", "unknown"

# Panel-derived history, shipped beside this module. The archive needs 21 gate
# sessions before it can speak, which put the first reading a month out - but
# that wait was an artefact of where the data lived, not of the measurement.
# The same universe's history was already on the panel.
SEED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "vol_regime_seed.json")


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


# How much NEW entry exposure each regime gets. A policy choice, stated as one
# - the statistics say the wild edge is negative, they do not say by how much a
# book should shrink. Calm and mid are left alone deliberately: calm measured
# +0.152% (t+1.47, not significant) and mid -0.284% (t-2.41, real but small),
# and neither justifies touching a book already deployed at only 18-20% against
# a 50% design.
ENTRY_EXPOSURE = {CALM: 1.0, MID: 1.0, WILD: 0.4, UNKNOWN: 1.0}


def entry_exposure_multiplier(regime):
    """(multiplier, reason) for NEW entries. Existing positions are untouched.

    WHY EXPOSURE AND NOT THE SCORE BAR

    The obvious lever is min_trade_score - it already steps 64/58/52, so a wild
    regime could simply demand one band more. That lever is wrong here, and the
    reason is measured rather than aesthetic: raising the bar concentrates
    selection into the HIGHEST-scoring names, and on realised P&L this score
    runs backwards. The 76+ band returned -2.16% while the sub-64 band returned
    +0.45% (effect +3.38pp, t+2.92, n=10 each). Raising the bar in a bad regime
    would therefore push money toward the band that measured worst - buying
    less, but worse.

    (That band comparison is confounded: sub-64 entries only exist when the
    regime lowers the bar to 52 or 58, so it partly measures "bought in a strong
    tape". It is not strong enough to invert the score on - it IS strong enough
    to avoid leaning on the score harder.)

    Scaling exposure keeps selection exactly as it is and simply takes a smaller
    bet when the edge is negative. It is also the only form that degrades
    safely: an unknown regime returns 1.0, so a missing seed or a short archive
    changes nothing rather than silently halting the book.

    UNKNOWN IS FULL SIZE ON PURPOSE

    The alternative - treating unknown as wild - would mean any data outage
    quietly stops trading, which is a failure mode that hides. This module has
    already produced two population mistakes; it should not also be able to shut
    the book off by breaking.
    """
    if not VOL_REGIME_ENABLED:
        return 1.0, "vol regime disabled"
    key = str(regime or UNKNOWN)
    mult = ENTRY_EXPOSURE.get(key)
    if mult is None:
        return 1.0, "unrecognised regime %r, treated as full size" % key
    if key == WILD:
        return mult, ("wild regime: momentum entries measured -1.596%% over 10 "
                      "sessions (t-11.02), so new entries take %.0f%% size"
                      % (mult * 100))
    if key == UNKNOWN:
        return mult, "regime unknown, full size rather than silently halting"
    return mult, "%s regime: entries unchanged" % key


def load_seed(path=SEED_FILE):
    """Historical universe returns, or None.

    Returns rather than closes, deliberately: writing panel-derived closes into
    the archive would blur which numbers came from the 09:31 gate and which
    were reconstructed. Provenance has mattered repeatedly here - this whole
    module exists because a threshold calibrated on one population was deployed
    on another - so a seed stays visibly a seed.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    rows = payload.get("returns")
    if not isinstance(rows, list) or not rows:
        return None
    size = payload.get("universe_size")
    try:
        size = int(size)
    except (TypeError, ValueError):
        return None
    lo = CALIBRATION_UNIVERSE_SIZE * (1 - UNIVERSE_TOLERANCE)
    hi = CALIBRATION_UNIVERSE_SIZE * (1 + UNIVERSE_TOLERANCE)
    if not (lo <= size <= hi):
        return None
    return payload


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
        return CALM, ("vol %.3f <= %.3f: momentum measured +0.152%% over 10 "
                      "sessions (t+1.47)" % (vol, calm_below))
    if vol >= wild_above:
        return WILD, ("vol %.3f >= %.3f: momentum measured -1.596%% over 10 "
                      "sessions (t-11.02)" % (vol, wild_above))
    return MID, ("vol %.3f between %.3f and %.3f: momentum measured -0.284%% "
                 "(t-2.41)" % (vol, calm_below, wild_above))


def universe_matches_calibration(rows, expected=CALIBRATION_UNIVERSE_SIZE,
                                 tolerance=UNIVERSE_TOLERANCE):
    """Is the archived universe still the one the thresholds were built on?

    The gate publishes CSI 1000 today. If it is ever widened to the whole
    market - which is the obvious fix for breadth - the archive keeps filling
    and the thresholds silently start describing a different population. That
    is the failure this whole module was written after, so it refuses instead.
    """
    if not rows:
        return False, "no sessions archived"
    recent = [int(r.get("n") or 0) for r in rows[-5:]]
    recent = [n for n in recent if n > 0]
    if not recent:
        return False, "archived sessions carry no universe size"
    avg = sum(recent) / len(recent)
    lo, hi = expected * (1 - tolerance), expected * (1 + tolerance)
    if not (lo <= avg <= hi):
        return False, ("universe is %d names, thresholds calibrated on %d - "
                       "recalibrate before classifying" % (int(avg), expected))
    return True, "universe %d names, as calibrated" % int(avg)


def combined_returns(rows, seed=None):
    """Seed returns first, then archived ones, archive winning any overlap.

    The archive is the live measurement and the seed is reconstruction, so where
    both cover a session the archive is kept. They are otherwise concatenated in
    date order - a volatility estimate over a 20-session window does not care
    which side of the join a session came from, only that the sequence is right.
    """
    archived = universe_returns(rows)
    have = {r.get("trade_date") for r in archived}
    seeded = []
    if seed:
        for r in seed.get("returns") or []:
            day = str(r.get("trade_date") or "")
            if not day or day in have:
                continue
            try:
                seeded.append({"trade_date": day, "ret": float(r["ret"]),
                               "n": int(r.get("n") or 0), "source": "seed"})
            except (KeyError, TypeError, ValueError):
                continue
    for r in archived:
        r["source"] = "archive"
    return sorted(seeded + archived, key=lambda r: r["trade_date"])


def regime_from_store(store_path, seed_path=SEED_FILE):
    rows = load_close_store(store_path)
    seed = load_seed(seed_path) if seed_path else None
    if rows:
        ok, why = universe_matches_calibration(rows)
        if not ok:
            return {"regime": UNKNOWN, "vol": None, "sessions": len(rows),
                    "reason": why, "enabled": VOL_REGIME_ENABLED}
    rets = combined_returns(rows, seed)
    n_seed = sum(1 for r in rets if r.get("source") == "seed")
    n_arch = len(rets) - n_seed
    if len(rets) < WINDOW:
        return {"regime": UNKNOWN, "vol": None, "sessions": len(rows),
                "returns": len(rets), "seeded": n_seed, "archived": n_arch,
                "reason": ("have %d of %d returns needed (seed %d, archive %d)"
                           % (len(rets), WINDOW, n_seed, n_arch)),
                "enabled": VOL_REGIME_ENABLED}
    vol = realised_vol(rets)
    label, reason = classify(vol)
    return {"regime": label, "vol": (round(vol, 4) if vol is not None else None),
            "sessions": len(rows), "returns": len(rets),
            "seeded": n_seed, "archived": n_arch,
            "window_from": rets[-WINDOW]["trade_date"],
            "window_to": rets[-1]["trade_date"],
            "reason": reason, "enabled": VOL_REGIME_ENABLED}
