#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Two gates on adding to a winner, both currently calibrated backwards.

Adding to an already-winning position was the best-paying thing the account
did, measured on its own realised round trips:

    already up +5%    n=31   then returned +4.83%   t+2.38   61.3% win
    already up +10%   n=19   then returned +5.80%   t+2.37   68.4% win
    already up +20%   n= 7   then returned +6.43%   t+1.35   85.7% win
    a fresh entry     n=124                  +1.07%  t+1.13   33.9% win

About four times a new entry's expectation. Two gates stop it happening.

GATE 1: THE HOLD WINDOW IS INVERTED

ADD_POSITION_MAX_HOLD_DAYS = 4 admits only positions that got there fast, and
those are the ones that do not continue:

    reached +5%   within 4 sessions  n=23   then  +2.07%   t+0.87
                  after  4 sessions  n= 8   then +12.78%   t+5.34
    reached +10%  within 4 sessions  n= 6   then  -0.46%   t-0.26
                  after  4 sessions  n=13   then  +8.69%   t+2.70

A fast spike to +5% is a move already spent - the same T+1 problem that makes
the entry score describe an untradeable day. A position that grinds up over
more than four sessions is in something that is still going. The window admits
the spike and excludes the grind, which is the wrong way round. 002396 星网锐捷
is the excluded kind: 17+ sessions held, +31.57%.

GATE 2: A THRESHOLD AGAINST A SCALE THAT DOES NOT EXIST

ADD_POSITION_BIG_MEAT_SECTOR_SCORE = 75.0 was compared against the broken
single-bucket sector score - ONE number for the entire market each day, from a
map that had collapsed 5,586 lines into three entries:

    days whose sector score reached 75:    5 of 22  (23%)
    days it blocked EVERY possible add:   17 of 22  (77%)
    days with more than one sector value:  0 of 22

In scoring, that constant was inert: adding the same number to every candidate
cannot reorder them, which is why fixing the map moved within-day rank IC by
-0.023 (t=-0.52). Against a THRESHOLD it is not inert at all - it became a
market-wide on/off switch for adding to winners, off on three days in four,
with nothing to do with the stock being added to.

Fixing the map does not fix this. Real per-sector scores run 6.02-55.29 raw
and 23.61-72.88 recentred; neither reaches 75, so a correct map with this
constant would block adds permanently. The threshold has to become a
PERCENTILE - "is this sector strong relative to today's sectors" - which is
what the constant was trying to express and can only express on a fixed scale.

Gated on TLFZ_ADD_GATES, off by default. Places no orders.
"""

from __future__ import annotations

import os

ADD_GATES_ENABLED = str(
    os.environ.get("TLFZ_ADD_GATES", "0")).strip().lower() in ("1", "true", "yes", "on")

# Where the evidence is, not where it is prettiest. +5% is the lowest trigger
# that measured significantly (t+2.38) and it carries the largest sample.
MIN_PROFIT_PCT = 5.0

# The grind, not the spike. Four sessions is the OLD ceiling; here it is a
# floor, because that is what the measurement says.
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
                       "(+2.07%%, t+0.87)" % (int(h), min_sessions))
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
