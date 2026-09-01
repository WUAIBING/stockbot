#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Adding to a winner: refuted on the market, and why the boat said otherwise.

OUR BOOK SAID YES. THE MARKET SAYS NO.

Measured on our own 124 round trips, money added to a position already up +5%
returned +4.83% (t+2.38) against +1.07% for a fresh entry - about four times.
Two cells of that result held 8 and 13 trades.

Asked of every liquid stock on every session from 2015 to 2026 - 2.0 million
observations, excess over the same session's liquid universe, t taken across
entry sessions rather than stock-days because stocks move together:

    from the moment a stock is first up +10%      next 10 sess   next 60 sess
      reached within 4 sessions                       -2.007%       -5.325%
                                                     t-19.24       t-39.60
      reached in 5 or more                            -0.980%       -2.785%
                                                     t-18.26       t-29.52
      baseline (any liquid stock)                     -0.000%       +0.000%

Buying a stock that has just risen UNDERPERFORMS, at every horizon out to 60
sessions, and it gets worse with time rather than reverting to momentum. The
baseline lands on zero, so the construction is sound and the rest is readable.

The boat said the opposite for two reasons. It measured forward to the ACTUAL
exit, so a good exit rule was being credited to the entry; and its only
profitable month was June, when ChiNext rose 9.92%. Three months of one book in
one regime cannot see a reversal effect that needs a decade to measure.

SO ADDS STAY SHUT. This module exists to hold that finding where the next
person to reach for "big meat" will read it, and to keep the two structural
repairs that are true regardless.

WHAT SURVIVES: SPEED

Slow beats fast at every horizon and the gap widens - +0.348% at 5 sessions
(t+7.29), +1.027% at 10 (t+9.95), +2.540% at 60 (t+17.68). A fast spike is a
move already spent, the same T+1 problem that makes the entry score describe an
untradeable day. So ADD_POSITION_MAX_HOLD_DAYS = 4 is not merely miscalibrated,
it selects the WORST group - but the fix is not to invert it and add anyway,
because the better group is still negative. Less bad is not good.

WHAT SURVIVES: THE SECTOR GATE IS A THRESHOLD ON A DEAD SCALE

ADD_POSITION_BIG_MEAT_SECTOR_SCORE = 75.0 was compared against the broken
single-bucket sector score - one number for the whole market each day:

    days whose sector score reached 75:    5 of 22  (23%)
    days it blocked EVERY possible add:   17 of 22  (77%)
    days with more than one sector value:  0 of 22

In scoring that constant was inert - the same number added to every candidate
cannot reorder them, which is why fixing the map moved rank IC by -0.023
(t=-0.52). Against a THRESHOLD it is not inert: it became a market-wide on/off
switch, shut three days in four for reasons unrelated to the stock. Fixing the
map makes it worse - real per-sector scores reach 55.29 raw and 72.88
recentred, so neither touches 75 and a correct map would shut adds forever. A
percentile is the only form that survives a change of scale, which this score
has already undergone once.

That repair matters even with adds shut, because the same constant-against-a-
moving-scale mistake is what min_trade_score makes, and this is the worked
example of it.

Gated on TLFZ_ADD_GATES, off by default, and the measurement above is the
reason to leave it off. Places no orders.
"""

from __future__ import annotations

import os

ADD_GATES_ENABLED = str(
    os.environ.get("TLFZ_ADD_GATES", "0")).strip().lower() in ("1", "true", "yes", "on")

# Retained from the boat measurement (+5% was its lowest significant trigger).
# The market says every bucket above this line is NEGATIVE, so this is the
# floor of a door that should stay closed, not a recommendation to open it.
MIN_PROFIT_PCT = 5.0

# The grind, not the spike. Four sessions was the old CEILING and it selected
# the worst group (-2.007% at 10 sessions, t-19.24, against -0.980% for the
# slower one). Here it is a floor. That is a repair to which group gets picked
# if adds are ever enabled - it does not make the group profitable.
MIN_HOLD_SESSIONS = 5

# Top third of sectors. Replaces the fixed 75, which was 23% of days on the
# broken scale and would be 0% of days on a correct one.
SECTOR_PERCENTILE = 0.67


def _f(v, d=None):
    try:
        if v is None:
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def sector_rank_ok(sector_code, sector_scores, percentile=SECTOR_PERCENTILE):
    """Is this sector in the top (1-percentile) of today's sectors?

    Scale-free on purpose. A constant threshold cannot survive a change in how
    the score is computed, and this score has already changed once - the whole
    reason this gate silently blocked 77% of days.

    With one bucket there is no ranking to do, so this returns False rather
    than True: a single global number carries no information about whether THIS
    sector is strong, and passing everything on no information is how the old
    gate came to be a market-wide switch.
    """
    if not sector_code or not sector_scores:
        return False, "no sector scores"
    scores = {k: _f(v, None) for k, v in sector_scores.items()}
    scores = {k: v for k, v in scores.items() if v is not None}
    if len(scores) < 2:
        return False, "only %d sector bucket(s) - no ranking possible" % len(scores)
    mine = scores.get(sector_code)
    if mine is None:
        return False, "sector %s not scored today" % sector_code
    ordered = sorted(scores.values())
    below = sum(1 for v in ordered if v < mine)
    pct = below / float(len(ordered) - 1) if len(ordered) > 1 else 0.0
    if pct >= percentile:
        return True, "sector at %.0f%% of today's sectors" % (pct * 100)
    return False, "sector only at %.0f%%, needs %.0f%%" % (pct * 100, percentile * 100)


def hold_window_ok(hold_sessions, min_sessions=MIN_HOLD_SESSIONS):
    """A floor, not a ceiling - the inversion this module exists to correct."""
    h = _f(hold_sessions, None)
    if h is None:
        return False, "hold length unknown"
    if h < min_sessions:
        return False, ("held %d sessions, needs %d - a fast move is a spent one "
                       "(-2.007%% over the next 10 sessions, t-19.24, against "
                       "-0.980%% for the slower group)" % (int(h), min_sessions))
    return True, "held %d sessions" % int(h)


def profit_ok(profit_pct, minimum=MIN_PROFIT_PCT):
    p = _f(profit_pct, None)
    if p is None:
        return False, "no profit figure"
    if p < minimum:
        return False, "up %.2f%%, needs %.1f%%" % (p, minimum)
    return True, "up %.2f%%" % p


def evaluate_add(position, sector_code=None, sector_scores=None,
                 hold_sessions=None, tradable_codes=None):
    """(allowed, reasons). Every gate is evaluated so the trail is complete.

    Reasons are returned for passes as well as refusals: a gate that only
    explains itself when it says no leaves no way to tell "allowed on strong
    evidence" from "allowed because a check silently did nothing".
    """
    code = str((position or {}).get("code", "")).zfill(6)
    checks = []
    if not ADD_GATES_ENABLED:
        return False, [("enabled", False, "add gates disabled")]
    if tradable_codes is not None and code not in {
            str(c).zfill(6) for c in tradable_codes}:
        return False, [("tradable", False, "not tradable today")]
    ok_p, why_p = profit_ok((position or {}).get("profit_pct"))
    checks.append(("profit", ok_p, why_p))
    ok_h, why_h = hold_window_ok(hold_sessions)
    checks.append(("hold_window", ok_h, why_h))
    ok_s, why_s = sector_rank_ok(sector_code, sector_scores)
    checks.append(("sector_rank", ok_s, why_s))
    return all(c[1] for c in checks), checks
