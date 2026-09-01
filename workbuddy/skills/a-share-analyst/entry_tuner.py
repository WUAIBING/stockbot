#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A daily-updating estimate of which ENTRIES pay, and by how much.

WHY THE ENTRY

The exits are not the problem. Forcing every exit to wait cost -5.49pp at T+10
(t=-4.35), and the flow gate earns its keep: names it refuses lose -5.98pp
(t=-2.91). The plumbing is sound.

The record is not. Rebuilt from broker fills, 124 round trips:

    2026-06   65 trades  +3.12%  t+1.83   40.0% win   ChiNext  +9.92%
    2026-07   21 trades  -1.96%  t-1.72   23.8% win   ChiNext -21.52%
    2026-08   38 trades  -0.76%  t-1.24   28.9% win   ChiNext  +4.12%
    ex-June   59 trades  -1.19%  t-2.09   27.1% win

August rose 4.12% and the book still lost. So this is not a fair-weather
strategy having bad weather - one month carries the entire record, and the
leak is at the entry.

WHY DAILY, AND WHY IT DOES NOT THRASH

Tuning daily on a day's worth of trades is how a system fits noise. Tuning
never is how it stays wrong. The middle is to re-estimate every day from ALL
history and shrink each estimate toward zero by its own reliability:

    weight = t^2 / (t^2 + K)          K = SHRINK_K = 4
    effect_used = raw_effect * weight

t=1 -> 20% of the raw effect. t=2 -> 50%. t=3 -> 69%. t=4 -> 80%.

A finding seen once barely moves anything; one that survives months converges
on its full size. The estimate approaches accuracy instead of chasing it, and
because a snapshot is appended every day, the convergence is auditable rather
than asserted.

GROUND TRUTH

Effects are measured on REALISED round trips rebuilt from broker fills - not on
price reconstructions, and not on the candidate pool. `is_filled` judges on
tradeCount, so an order that was accepted and never executed (688700 on
2026-08-31: status 2, count 340, tradeCount 0) is correctly absent. What is
measured is what the account actually did.

THIS MODULE DOES NOT TRADE. It produces numbers. Applying them to live scoring
is gated on TLFZ_ENTRY_TUNER and off by default.
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone

# Half weight at t=2. Chosen, not fitted: it is a policy about how much
# evidence is enough, and fitting it to the same data it governs would be
# circular.
SHRINK_K = 4.0

# Score points a fully-trusted effect may move a candidate. The confidence
# scaling above is statistics; this magnitude is a POLICY choice, and it is
# small on purpose - min_trade_score steps 64/58/52, so 6 points is roughly one
# regime band and cannot silently rewrite the gate.
MAX_ADJUSTMENT = 6.0

TUNER_ENABLED = str(
    os.environ.get("TLFZ_ENTRY_TUNER", "0")).strip().lower() in ("1", "true", "yes", "on")

MIN_GROUP = 8          # below this an effect is reported but never applied


def _f(v, d=0.0):
    try:
        if v is None:
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def _stats(values):
    """n, mean, t against zero."""
    vals = [v for v in values if v is not None and math.isfinite(v)]
    n = len(vals)
    if n == 0:
        return 0, 0.0, 0.0
    m = sum(vals) / n
    if n < 2:
        return n, m, 0.0
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))
    if sd <= 0:
        return n, m, 0.0
    return n, m, m / (sd / math.sqrt(n))


def _welch(a, b):
    """mean(a) - mean(b) and its t. The group against everything else."""
    na, ma, _ = _stats(a)
    nb, mb, _ = _stats(b)
    if na < 2 or nb < 2:
        return na, nb, (ma - mb if na and nb else 0.0), 0.0
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    se = math.sqrt(va / na + vb / nb)
    diff = ma - mb
    return na, nb, diff, (diff / se if se > 0 else 0.0)


def shrink_weight(t):
    """t^2 / (t^2 + K). Reliability, not significance - it never reaches 1."""
    t2 = float(t) ** 2
    return t2 / (t2 + SHRINK_K) if (t2 + SHRINK_K) > 0 else 0.0


# --------------------------------------------------------------------------
# entry attributes: each returns a label for a round trip, or None to skip
# --------------------------------------------------------------------------

def _attr_sector_crowded(ep):
    n = ep.get("sector_same_day_buys")
    if n is None:
        return None
    return "crowded" if int(n) >= 2 else "solo"


def _attr_tier(ep):
    t = ep.get("tier")
    return ("tier%d" % int(t)) if t else None


def _attr_mode(ep):
    m = str(ep.get("mode") or "").strip()
    return m or None


def _attr_score_band(ep):
    s = ep.get("entry_score")
    if s is None:
        return None
    s = _f(s)
    if s >= 76:
        return "score76+"
    if s >= 70:
        return "score70-75"
    if s >= 64:
        return "score64-69"
    return "score<64"


ATTRIBUTES = {
    "sector_crowding": _attr_sector_crowded,
    "tier": _attr_tier,
    "mode": _attr_mode,
    "score_band": _attr_score_band,
}


def estimate_effects(trips):
    """For every attribute value: its trades against all other trades.

    Reported for each: raw effect in percentage points, its t, the shrink
    weight, and the shrunk effect actually eligible to be applied.
    """
    out = {}
    for attr, fn in ATTRIBUTES.items():
        labelled = []
        for ep in trips:
            lab = fn(ep)
            if lab is not None and ep.get("pnl_pct") is not None:
                labelled.append((lab, _f(ep["pnl_pct"])))
        if not labelled:
            continue
        groups = defaultdict(list)
        for lab, v in labelled:
            groups[lab].append(v)
        entries = {}
        for lab, vals in sorted(groups.items()):
            others = [v for l2, v in labelled if l2 != lab]
            n_in, n_out, diff, t = _welch(vals, others)
            w = shrink_weight(t)
            entries[lab] = {
                "n": n_in,
                "n_other": n_out,
                "mean_pct": round(sum(vals) / len(vals), 4),
                "effect_pp": round(diff, 4),
                "t": round(t, 4),
                "shrink_weight": round(w, 4),
                "shrunk_pp": round(diff * w, 4),
                "eligible": bool(n_in >= MIN_GROUP and n_out >= MIN_GROUP),
            }
        out[attr] = entries
    return out


def score_adjustment(label_values, effects):
    """Score points to add for a candidate, from its own attribute labels.

    Sign convention: a NEGATIVE effect (this kind of entry lost money) yields a
    negative adjustment. The effect is in percentage points of trade return and
    the score is in points of a 0-100 scale, so the conversion is a policy
    scaling capped at MAX_ADJUSTMENT - stated plainly rather than dressed up as
    a calibration, because nothing in the data fixes that exchange rate.
    """
    if not TUNER_ENABLED:
        return 0.0, []
    total = 0.0
    used = []
    for attr, lab in (label_values or {}).items():
        entry = ((effects or {}).get(attr) or {}).get(lab)
        if not entry or not entry.get("eligible"):
            continue
        total += _f(entry.get("shrunk_pp"))
        used.append({"attribute": attr, "label": lab,
                     "shrunk_pp": entry.get("shrunk_pp"),
                     "t": entry.get("t")})
    if total > MAX_ADJUSTMENT:
        total = MAX_ADJUSTMENT
    elif total < -MAX_ADJUSTMENT:
        total = -MAX_ADJUSTMENT
    return round(total, 3), used


def build_snapshot(trips, as_of=None):
    eff = estimate_effects(trips)
    n, m, t = _stats([_f(e.get("pnl_pct")) for e in trips
                      if e.get("pnl_pct") is not None])
    return {
        "as_of": as_of or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "trips": n,
        "overall_mean_pct": round(m, 4),
        "overall_t": round(t, 4),
        "shrink_k": SHRINK_K,
        "max_adjustment": MAX_ADJUSTMENT,
        "tuner_enabled": TUNER_ENABLED,
        "effects": eff,
    }


def append_snapshot(snapshot, path):
    """One line per day, so convergence can be read off the file.

    Appended rather than replaced: the point of tuning daily is to be able to
    see an estimate settle, and a file that only holds today's answer cannot
    show that.
    """
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(snapshot, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def load_latest_snapshot(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            last = None
            for line in fh:
                line = line.strip()
                if line:
                    last = line
        return json.loads(last) if last else None
    except (OSError, ValueError):
        return None
