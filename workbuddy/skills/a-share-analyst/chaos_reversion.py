#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cast in churning water, on the beaten-down side.

Measured over 9,102,435 liquid A-share stock-days, 2015-09 to 2026-08, split
train (<2024-01-01) and holdout (>=2024-01-01). Excess is over the SAME
session's universe mean, so a regime that simply floats or sinks everything
cannot masquerade as skill. Top 25 per session, held 10 sessions, cost netted.

                       regime   train   holdout   verdict
    reversion          chaos    +0.99     +2.03   REAL / REAL
                       calm     -0.76     -0.01   neg  / noise
    momentum           chaos    -2.07     -2.65   neg  / neg
                       calm     -1.60     -0.94   neg  / neg

Two things follow, and the second is the uncomfortable one:

1. The edge is REGIME-CONDITIONAL. Measured unconditionally it is -0.03 train /
   +0.45 holdout - which is the zero that every earlier study of this system
   kept finding, because chaos and calm were averaged together.

2. Momentum selection loses in all four cells. There is no market condition in
   this sample where buying strength worked. That is a direction problem, not a
   timing problem: running a momentum book harder during chaos is the worst cell
   of the four.

WHAT THIS MODULE IS NOT

It selects. It does not size, order, or trade, and it deliberately holds no
reference to any execution path. Wiring it to real money is a separate decision
that should follow paper evaluation, not precede it.

WHAT IT NEEDS THAT DOES NOT EXIST YET

`ma60_off` (close vs the 60-session mean, in percent). scanner_v10 computes ma60
internally but never emits it - it publishes close_vs_ma20_pct only. Adding
close_vs_ma60_pct to the scan row is the one upstream change required.

KNOWN LIMITS, stated because they bound how far the numbers can be trusted

- Survivorship. vipdoc retains 101 delisted files, so names that fell and never
  recovered are largely absent. That biases every reversion result UPWARD by an
  amount I cannot quantify from this data.
- The `model_score` that drives live selection remains unmeasured. If it is
  doing something genuinely different from the momentum proxy tested here, these
  comparisons do not describe it.
- Chaos is ~14% of sessions when both conditions are required. A book trading
  only then is idle most of the year, which is a different business from the one
  currently running.
"""

from __future__ import annotations

import statistics as st
from typing import Iterable, Mapping, Sequence

# Cross-sectional dispersion at or above this percentile of its own history
# counts as chaos. p67 over 2,596 sessions is 2.90; the p10/p90 range is
# 2.10/3.37, so the measure is tight and a fixed constant would drift.
DISPERSION_CHAOS_PERCENTILE = 0.67

# Trailing sessions used for the market-trend leg of the regime test.
TREND_LOOKBACK_SESSIONS = 20

# Top N by reversion score. 25 measured better than 10 in both periods
# (+0.99/+2.03 against +0.81/+1.30) - wider is better while the edge holds,
# because noise falls as 1/sqrt(N) faster than the score decays.
DEFAULT_TOP_N = 25

# Hold length. Net of a 0.15% round trip the 10-session hold returned +5.60pp/yr
# of excess against +3.80 at 5 and -0.75 at 3: a fixed cost needs time to
# amortise, and beyond 20 sessions the independent-draw count falls too far.
HOLD_SESSIONS = 10

# Daily limits make anything beyond this a data error rather than a return.
_IMPLAUSIBLE_PCT = 21.0


def session_dispersion(pct_changes: Iterable[float]) -> float | None:
    """Cross-sectional spread of one session's returns.

    This is the "how much does selection matter today" measure. When every stock
    moves together there is nothing to select between, and the edge collapses to
    +0.41 (calm) from +2.62 (chaos).
    """
    vals = [
        float(v) for v in pct_changes
        if v is not None and -_IMPLAUSIBLE_PCT < float(v) < _IMPLAUSIBLE_PCT
    ]
    if len(vals) < 50:
        return None
    return st.pstdev(vals)


def session_median(pct_changes: Iterable[float]) -> float | None:
    """Median session return - the market leg, robust to the tails."""
    vals = [
        float(v) for v in pct_changes
        if v is not None and -_IMPLAUSIBLE_PCT < float(v) < _IMPLAUSIBLE_PCT
    ]
    if len(vals) < 50:
        return None
    return st.median(vals)


def dispersion_threshold(history: Sequence[float],
                         percentile: float = DISPERSION_CHAOS_PERCENTILE) -> float | None:
    """The chaos cut, taken from dispersion's own distribution.

    Needs enough history to be stable: 33 sessions put the cut at 3.46 while
    2,596 sessions put it at 2.90, and that gap is the difference between
    calling a day calm and calling it chaos.
    """
    vals = sorted(v for v in history if v is not None and v > 0)
    if len(vals) < 250:
        return None
    idx = min(len(vals) - 1, int(len(vals) * percentile))
    return vals[idx]


def classify_regime(dispersion_prior: float | None,
                    trend_prior: float | None,
                    dispersion_cut: float | None) -> dict:
    """Label the session from data that ends BEFORE it.

    Both inputs must come from sessions up to D-1. Using D's own dispersion
    would be look-ahead: you cannot know how scattered a day was until it ends,
    and by then the entry is gone.
    """
    if dispersion_prior is None or trend_prior is None or dispersion_cut is None:
        return {"regime": "unknown", "high_dispersion": False,
                "downtrend": False, "tradeable": False,
                "reason": "insufficient history"}
    high = dispersion_prior >= dispersion_cut
    down = trend_prior <= 0.0
    if high and down:
        regime, tradeable = "chaos", True
    elif high or down:
        regime, tradeable = "partial", False
    else:
        regime, tradeable = "calm", False
    return {
        "regime": regime,
        "high_dispersion": high,
        "downtrend": down,
        "tradeable": tradeable,
        "dispersion": dispersion_prior,
        "dispersion_cut": dispersion_cut,
        "trend": trend_prior,
        "reason": (
            f"dispersion {dispersion_prior:.2f} "
            f"{'>=' if high else '<'} {dispersion_cut:.2f}; "
            f"trend {trend_prior:+.2f}%"
        ),
    }


def _percentile_ranks(values: Sequence[float | None]) -> list[float | None]:
    """Rank within the session, so the score is scale-free across regimes."""
    pairs = [(i, float(v)) for i, v in enumerate(values) if v is not None]
    out: list[float | None] = [None] * len(values)
    if len(pairs) < 2:
        return out
    pairs.sort(key=lambda t: t[1])
    last = len(pairs) - 1
    for rank, (i, _) in enumerate(pairs):
        out[i] = rank / last
    return out


def reversion_scores(rows: Sequence[Mapping]) -> list[float | None]:
    """Lower is more beaten-down. Mean of two within-session percentile ranks.

    ma60_off and r20 were the two most monotone separators of the twelve tested
    (rank correlation -0.86 and -0.80 against forward return). Averaging ranks
    rather than raw values keeps one feature's scale from dominating when
    volatility shifts.
    """
    ma60 = _percentile_ranks([r.get("ma60_off") for r in rows])
    r20 = _percentile_ranks([r.get("r20") for r in rows])
    scores: list[float | None] = []
    for a, b in zip(ma60, r20):
        scores.append(None if a is None or b is None else (a + b) / 2.0)
    return scores


def select_candidates(rows: Sequence[Mapping],
                      regime: Mapping,
                      top_n: int = DEFAULT_TOP_N) -> dict:
    """Rank a session's universe and return the picks, or none if not tradeable.

    Returns the reason when it declines, because a strategy that silently does
    nothing is indistinguishable from one that is broken - which is how a
    candidate pool in this system stayed frozen for 37 days unnoticed.
    """
    if not regime.get("tradeable"):
        return {
            "picks": [],
            "traded": False,
            "reason": f"regime={regime.get('regime')} ({regime.get('reason', '')})",
        }
    scores = reversion_scores(rows)
    scored = [(s, r) for s, r in zip(scores, rows) if s is not None]
    if len(scored) < top_n:
        return {
            "picks": [],
            "traded": False,
            "reason": f"only {len(scored)} scoreable rows, need {top_n}",
        }
    scored.sort(key=lambda t: t[0])
    picks = [dict(r, reversion_score=round(s, 4)) for s, r in scored[:top_n]]
    return {
        "picks": picks,
        "traded": True,
        "reason": f"chaos: {regime.get('reason', '')}",
        "hold_sessions": HOLD_SESSIONS,
    }
