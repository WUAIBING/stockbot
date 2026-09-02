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

# +20%, not +5%. The +5% trigger came from a boat measurement that credited a
# good exit rule to the entry. Measured properly - forward over a FIXED horizon
# from the moment one of OUR positions first reaches the level, excess over the
# same-session liquid universe:
#
#     our positions up +5%    n=36   +1.08% at 5 sessions   t+0.59   nothing
#     our positions up +10%   n=21   +4.08% at 5 sessions   t+1.36   suggestive
#     our positions up +20%   n= 9  +14.14% at 5 sessions   t+2.34   the signal
#                                    +11.64% at 3 sessions   t+3.11
#
# against an ocean benchmark of -2.007% (t-19.24) for ANY stock up +10%. So the
# selection IS different at the big-meat stage - but only there. Setting the
# trigger at +5% would open the door where the evidence is absent and the market
# is against us.
MIN_PROFIT_PCT = 20.0

# The added slice is a SHORT trade, not a doubling-down. The edge is +11.64% at
# 3 sessions and +14.14% at 5, then gone by 10 (-1.33%, t-0.18). An add held to
# the core position's exit would give back what it earned.
ADD_MAX_HOLD_SESSIONS = 5

# The grind, not the spike. Four sessions was the old CEILING and it selected
# the worst group (-2.007% at 10 sessions, t-19.24, against -0.980% for the
# slower one). Here it is a floor. That is a repair to which group gets picked
# if adds are ever enabled - it does not make the group profitable.
MIN_HOLD_SESSIONS = 5

# Top third of sectors. Replaces the fixed 75, which was 23% of days on the
# broken scale and would be 0% of days on a correct one.
SECTOR_PERCENTILE = 0.67

# A confirmed big meat is taken to 10% of book. Ordinary positions stay near
# 2%, so the slice actually at risk is the ~7.3% increment - and it lands on a
# position already carrying +20%, which is why this is not the reckless number
# it first looks like:
#
#     continuation real   +10.12% x 7.3% = +0.74% of NAV per event
#     continuation wrong   -2.01% x 7.3% = -0.15%
#     worst observed       -9.82% x 7.3% = -0.72%   (combined position still green)
#
# Across the 9 big meat of one quarter: +6.65% of NAV if real, -1.32% if not,
# EV +3.62% at P=0.62. Raw Kelly on the measured continuation said 8%, so this
# is just above full Kelly rather than wildly beyond it.
#
# A first pass set this at 4%, pricing the add as a fresh bet. It is not one -
# only the increment can be wrong, and the cushion absorbs the worst case.
BIG_MEAT_TARGET_PCT = 10.0

# What does NOT shrink with the cushion is being unable to get out. 688432 has
# been suspended since 2026-08-31 with no reopen date; 10% of book in a halted
# name is 10% that cannot be exited at any price. And a -10% open on 10% is
# -1.0% of NAV before anyone can act, with fills landing 1.9% below the open.
# So total big-meat exposure is capped as well as per-name.
TOTAL_BIG_MEAT_CAP_PCT = 20.0


def _f(v, d=None):
    try:
        if v is None:
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def sector_rank_ok(sector_code, sector_scores, percentile=SECTOR_PERCENTILE):
    """Is this sector in the top (1-percentile) of today's sectors?

    NOT USED AS A GATE, AND DELIBERATELY SO.

    Replacing the fixed 75 with a percentile made this scale-proof, which was
    the right repair to the wrong question. The question never asked was
    whether sector rank survives to the session we can actually trade. Under
    T+1 the add executes today but everything it earns starts tomorrow, so a
    same-day ranking has to persist to be worth anything.

    It does not:

        top-third membership repeated next session   36%   (random 33%)

        buying at T+1 on a top-third sector, then holding
            1 session   +0.009%   t+0.57
            3 sessions  +0.021%   t+0.73
            5 sessions  +0.000%   t+0.01
           10 sessions  -0.018%   t-0.38

    Nothing, at every horizon. Set against a signal that does survive - the
    volatility regime is 93% persistent at T+1 with an effect of -1.596%
    (t-11.02) - the contrast is the whole point. Same test, opposite verdicts.

    So the gate is removed rather than repaired. A filter that costs
    opportunities and buys nothing is worse than no filter, and the percentile
    fix was making a decoration scale-proof.

    Kept as a function because the percentile-versus-constant lesson still
    applies elsewhere - min_trade_score steps 64/58/52 on a score whose scale
    can drift, which is the same disease untreated.
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


def add_size_pct(position_pct, book_big_meat_pct,
                 target=BIG_MEAT_TARGET_PCT, cap=TOTAL_BIG_MEAT_CAP_PCT):
    """(pct_of_book_to_add, reason). Never exceeds the per-name or total cap.

    Sizing up happens only AFTER confirmation, which is what makes 10%
    defensible: the position already carries +20%, so the increment is the only
    thing that can be wrong, and the worst observed continuation (-9.82%) still
    leaves the combined position green.

    The total cap is the guard that the cushion does not provide. A halted name
    cannot be exited at any price - 688432 is the live example - so two
    concurrent big meat at 10% is the most the book should have frozen.
    """
    try:
        cur = float(position_pct)
        held = float(book_big_meat_pct)
    except (TypeError, ValueError):
        return 0.0, "position or exposure not measurable"
    if cur >= target:
        return 0.0, "already at %.1f%% of book, target %.1f%%" % (cur, target)
    room_name = target - cur
    room_total = cap - held
    if room_total <= 0:
        return 0.0, ("big-meat exposure already %.1f%% of book, cap %.1f%%"
                     % (held, cap))
    add = min(room_name, room_total)
    if add < room_name:
        return round(add, 2), ("capped by total exposure: %.1f%% of a possible "
                               "%.1f%% (held %.1f%%, cap %.1f%%)"
                               % (add, room_name, held, cap))
    return round(add, 2), ("to %.1f%% of book from %.1f%%" % (target, cur))


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
    # NO SECTOR GATE. See sector_rank_ok for why it was removed rather than
    # fixed: it ranks a thing that does not survive to the session we can trade.
    return all(c[1] for c in checks), checks
