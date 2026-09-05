#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""When will the broker actually fill an order? Measured, not assumed.

THE FIRST VERSION OF THIS MODULE WAS WRONG

It refused everything outside exchange hours, reasoning that a shut market
cannot fill an order. The account's own record says otherwise, and the mistake
was assuming the exchange calendar describes the broker.

Of 334 archived orders, 20 were placed after 15:00, and they split cleanly:

    15:04 - 15:27   10 orders   status 9    0 filled
    17:54 - 21:49   10 orders   status 8/4  6 filled
        19:30  002487  4100 @ 42.10   filled
        19:30  002123 12400 @  8.71   filled
        19:31  300079 23700 @  4.55   filled

Evening orders QUEUE and fill - that is the standard 盘后委托 behaviour, and it
is precisely the market-on-open mechanism open_exit.py needs. A guard that
blocked them would have removed the one route we have to an opening fill.

The dead window is narrow: 15:00 to roughly 17:00, status 9, zero of ten.

THE EXCHANGE RULE EXISTS. THIS BROKER DOES NOT IMPLEMENT IT.

盘后固定价格交易 allows trading at the closing price from 15:05 to 15:30 on STAR
and ChiNext. So an order at 15:09 on a 688 name should be legal. Ours were not
filled:

    15:00-15:30 orders   eligible boards (688/300/301)  4   0 filled
                         main board                     7   0 filled
        15:09  688726  STAR     status 9   inside the 15:05-15:30 window
        15:27  688313  STAR     status 9   inside the window

Four eligible-board orders inside the exact window, all status 9. The market
permits it; the simulated platform returns nothing. Recorded here because
anyone reading the exchange rulebook would reasonably assume otherwise, and
would be wrong for this account.

WHAT IS NOT KNOWN, AND IS NOT GUESSED HERE

  * LUNCH: only 2 orders have ever been placed between 11:30 and 13:00, one of
    which filled. Two observations decide nothing, so lunch is reported as
    UNTESTED rather than allowed or refused. The caller decides.
  * The exact evening cutoff. 17:54 through 21:49 all reached the broker; we
    have never tried past 21:49 or between 15:27 and 17:54.
  * The broker has NEVER rejected an order for "当前时间不可交易" - 0 of 360
    logged results. The documented error exists; we have not provoked it. So
    even the dead window is an observed OUTCOME, not a stated rule we obey.

WHY AN UNFILLED ORDER IS NOT A NEUTRAL EVENT

It is a decision the system made and did not get, and the two examined went
opposite ways: 002396's entry failed three times in July at 28.26 and was
finally bought a month later at 30.38, 7.5% worse. 002428's exit failed twice
and accidentally earned +22,000. Unfilled orders inject noise into the record
everything else is measured on - the 124 round trips are the trades that
happened to execute.

This module answers only "would an order placed now be likely to trade". It
does not decide what to trade, and holds no execution path.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

CHINA = timezone(timedelta(hours=8))

AUCTION_OPEN = time(9, 15)
AUCTION_CUTOFF = time(9, 25)
MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)
AFTERNOON_CLOSE = time(15, 0)

# Measured, not assumed. 15:04-15:27 produced status 9 and zero fills across ten
# orders; 17:54 onward produced six fills across ten. The boundary between them
# is untested - nothing has ever been sent between 15:27 and 17:54 - so 17:00 is
# a judgement inside an unobserved gap and is marked as such.
DEAD_WINDOW_END = time(17, 0)

CONTINUOUS = "continuous"
AUCTION = "auction"
LUNCH_UNTESTED = "lunch_untested"
DEAD = "dead_after_close"
QUEUED = "queued_to_next_session"
CLOSED = "closed"


def now_china():
    return datetime.now(CHINA)


def _as_china(when):
    w = when or now_china()
    if w.tzinfo is None:
        return w.replace(tzinfo=CHINA)
    return w.astimezone(CHINA)


def session_state(when=None, trading_day=True):
    """(state, reason). Six states, because the outcomes genuinely differ.

    `trading_day` is supplied by the caller: whether a date is a trading day is
    trading_calendar's job. Duplicating a holiday table is how this project
    ended up with two, one of which was wrong about 2026-09-28.
    """
    w = _as_china(when)
    if not trading_day:
        return CLOSED, "not a trading day"
    t = w.time()
    if t < AUCTION_OPEN:
        return CLOSED, "before the 09:15 auction"
    if t < AUCTION_CUTOFF:
        return AUCTION, "call auction, matched at 09:25"
    if t < MORNING_OPEN:
        return CLOSED, "auction shut, continuous not yet open"
    if t <= MORNING_CLOSE:
        return CONTINUOUS, "morning session"
    if t < AFTERNOON_OPEN:
        return LUNCH_UNTESTED, ("lunch break - only 2 orders ever placed here, "
                                "1 filled; too few to call")
    if t <= AFTERNOON_CLOSE:
        return CONTINUOUS, "afternoon session"
    if t < DEAD_WINDOW_END:
        return DEAD, ("just after the close - 10 orders placed 15:04-15:27, "
                      "status 9, none filled")
    return QUEUED, ("evening - orders queue to the next session; 6 of 10 filled "
                    "between 17:54 and 21:49")


def can_place_order(when=None, trading_day=True, allow_queued=True,
                    allow_lunch=False):
    """(bool, reason). Refuses only where the evidence is unambiguous.

    The dead window is the one clear finding: zero fills from ten orders. Every
    other refusal here is a caller's choice, exposed as a flag rather than
    buried, because the data does not settle them:

      allow_queued  evening orders DO fill and are the only route to an opening
                    fill. Default True - blocking them was the first version's
                    error.
      allow_lunch   two observations. Default False as the cautious side of a
                    coin, not as a finding.
    """
    state, why = session_state(when, trading_day)
    if state in (CONTINUOUS, AUCTION):
        return True, why
    if state == QUEUED:
        return (allow_queued, why if allow_queued
                else why + " (refused by caller preference)")
    if state == LUNCH_UNTESTED:
        return (allow_lunch, why if allow_lunch
                else why + " (refused as the cautious default)")
    return False, why


def minutes_left(when=None, trading_day=True):
    """Continuous-trading minutes left today, or 0 when there are none.

    A limit placed with nine minutes to live is a different proposition from
    one with three hours, and nothing in the system currently knows the
    difference. The close-node buys at 14:51 - nine minutes - and the in-session
    non-fills are concentrated in orders priced away from the market, which is
    a limit that needed more time than it had.
    """
    w = _as_china(when)
    state, _ = session_state(w, trading_day)
    if state in (CLOSED, DEAD, QUEUED):
        return 0
    if state == AUCTION:
        return 240
    if state == LUNCH_UNTESTED:
        return 120
    t = w.time()
    mins = w.hour * 60 + w.minute
    if t <= MORNING_CLOSE:
        return (11 * 60 + 30 - mins) + 120
    return max(0, 15 * 60 - mins)
