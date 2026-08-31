#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Recognise a suspended stock, and stop firing orders it cannot fill.

On 2026-08-31 有研硅 (688432) halted from the open for a 重大资产重组 - issuing
shares and cash to buy 山东有研艾斯 and 山东有研半导体. The announcement gives a
start date and NOTHING ELSE:

    停牌起始日   2026/8/31
    停牌期间     (blank)
    停牌终止日   (blank)
    复牌日       (blank)
    body         预计停牌时间不超过 5 个交易日

"Expected to be no more than 5 trading days" is not a reopen date, and a
restructuring can extend. So nothing can be scheduled against a resume; the only
safe design is to notice the halt from the tape each session and keep noticing
until it ends.

WHY THIS MATTERS TO THE SELL PATH

The book held 471 shares when the halt began. Smart-sell runs roughly every
thirty minutes - the NAV log shows 01:45, 02:15, 02:45, 03:15, 05:15, 05:45,
06:15, 06:45 in one day - and every attempt against a suspended stock is
rejected. Five sessions of that is about forty dead orders, each an API call and
each landing in the broker record as a 废单.

It also collides with MAX_HOLD_DAYS. 688432 was bought 2026-08-28, so the
ten-day exit falls due around 2026-09-11. If the halt is still running, that
exit fires into a stock that cannot trade, fails, and fires again.

THE BROKER DOES NOT TELL YOU

Checked against the live position feed while 688432 was actually suspended:

    688432 halted    dayProfit 0.0    dayProfitPct 0.0     delist 0
    000811 trading   dayProfit 380.0  dayProfitPct 1.8953  delist 0

There is no halt field. `delist` is for delisting and reads 0 for both. The only
signal in the data is that the price has not moved from the previous close at
all - dayProfitPct exactly 0.

THAT SIGNAL IS NOT PROOF ON ITS OWN, which is the whole difficulty. A stock can
legitimately close unchanged, and one sitting at its limit also stops moving. So
a single zero reading is treated as a SUSPICION, and confirmation requires
either the reading persisting across separate observations in a session - a
trading stock ticks, a suspended one cannot - or an actual order coming back
unfilled, which is ground truth.

WHAT IT DELIBERATELY WILL NOT DO

It decides whether an order is worth sending. It does not sell, cancel or size,
and a test asserts it holds no execution path. It also never CANCELS a pending
exit: a position the strategy wanted out of is still wanted out of, and the
intent is carried until the stock trades again. Forgetting the exit would turn a
halt into an accidental long-term hold, which is the opposite of the fix.
"""

from __future__ import annotations

import datetime as _dt
from typing import Mapping, Sequence

# A price that has not moved at all since the previous close. Exact zero rather
# than a tolerance: a real quote almost never lands exactly flat, and widening
# this would swallow genuinely flat sessions.
FLAT_EPSILON = 1e-9

# One flat reading is a suspicion. A trading stock ticks between observations;
# a suspended one cannot, so repeated readings across a session are what
# separates the two.
MIN_FLAT_OBSERVATIONS = 2

# Rejected orders that, together with a flat price, confirm the halt. Ground
# truth beats inference, so this is deliberately small.
REJECTIONS_TO_CONFIRM = 1

# Once confirmed, stop sending orders for the rest of the session. The halt is
# a property of the day, not of the minute, so re-testing every thirty minutes
# just reproduces the dead orders this module exists to prevent.
RECHECK_NEXT_SESSION_ONLY = True


def day_move_pct(position: Mapping) -> float | None:
    """The broker's own day move for a holding, or None when absent."""
    for key in ("dayProfitPct", "day_profit_pct", "change_pct"):
        if key in position:
            try:
                return float(position[key])
            except (TypeError, ValueError):
                return None
    return None


def looks_suspended(position: Mapping) -> dict:
    """Judge one position from a single observation.

    Returns `suspicious` rather than `halted`, because one flat reading cannot
    tell a suspended stock from one that happens to be unchanged. The caller
    accumulates observations; this only reads the tape.
    """
    move = day_move_pct(position)
    code = str(position.get("secCode") or position.get("code") or "").strip()
    if move is None:
        return {"code": code, "suspicious": False,
                "reason": "no day-move field in the position feed"}
    if abs(move) > FLAT_EPSILON:
        return {"code": code, "suspicious": False, "day_move_pct": move,
                "reason": "price moved %.4f%% today" % move}
    return {
        "code": code, "suspicious": True, "day_move_pct": move,
        "reason": ("price has not moved from the previous close; suspended, "
                   "limit-locked or genuinely flat - not yet distinguishable"),
    }


def update_halt_state(state: Mapping, position: Mapping, *, trade_date: str,
                      order_rejected: bool = False) -> dict:
    """Fold one observation into what is known about a code.

    state carries: trade_date, flat_observations, rejections, confirmed.
    It resets when the date changes, because a halt that ended overnight must
    not keep suppressing orders on the strength of yesterday's readings.
    """
    state = dict(state or {})
    code = str(position.get("secCode") or position.get("code") or "").strip()
    if str(state.get("trade_date") or "") != str(trade_date):
        state = {"trade_date": str(trade_date), "flat_observations": 0,
                 "rejections": 0, "confirmed": False}
    state["code"] = code

    look = looks_suspended(position)
    if look["suspicious"]:
        state["flat_observations"] = int(state.get("flat_observations", 0)) + 1
    else:
        # A single tick proves it is trading. Everything resets, including a
        # confirmation: the stock is live and orders should flow again.
        state["flat_observations"] = 0
        state["rejections"] = 0
        state["confirmed"] = False
        state["reason"] = look["reason"]
        return state

    if order_rejected:
        state["rejections"] = int(state.get("rejections", 0)) + 1

    flat = int(state.get("flat_observations", 0))
    rej = int(state.get("rejections", 0))
    if rej >= REJECTIONS_TO_CONFIRM and flat >= 1:
        state["confirmed"] = True
        state["reason"] = ("an order came back unfilled while the price has not "
                           "moved: suspended")
    elif flat >= MIN_FLAT_OBSERVATIONS:
        state["confirmed"] = True
        state["reason"] = ("price unchanged across %d separate observations; a "
                           "trading stock ticks" % flat)
    else:
        state["confirmed"] = False
        state["reason"] = ("one flat reading, not yet distinguishable from a "
                           "genuinely unchanged price")
    return state


def should_attempt_sell(state: Mapping) -> dict:
    """Is an order worth sending, or would it only produce another 废单?

    Declining is NOT the same as cancelling the exit. The caller keeps the
    intent and acts on it when the stock trades again; this only answers whether
    the wire is worth using right now.
    """
    state = state or {}
    if state.get("confirmed"):
        return {
            "attempt": False, "hold_intent": True,
            "reason": "suspended: %s" % state.get("reason", ""),
        }
    return {"attempt": True, "hold_intent": True, "reason": "tradeable"}


def hold_days_should_count(state: Mapping) -> bool:
    """Suspended sessions must not age a position toward MAX_HOLD_DAYS.

    The ten-day exit exists because the measured edge decays over ten TRADING
    sessions. Days a stock could not be traded carry no decay and no
    opportunity, so counting them would force an exit whose reason never
    happened - and force it into a stock that cannot fill it.
    """
    return not bool((state or {}).get("confirmed"))


def summarise(states: Sequence[Mapping]) -> dict:
    """What is suspended right now, for the daily report."""
    confirmed = [s for s in (states or []) if s.get("confirmed")]
    return {
        "suspended_count": len(confirmed),
        "suspended_codes": sorted(str(s.get("code", "")) for s in confirmed),
        "watching_count": len([s for s in (states or [])
                               if not s.get("confirmed")
                               and int(s.get("flat_observations", 0)) > 0]),
    }


# === trading-session arithmetic ===
#
# hold_days was computed as CALENDAR days - (datetime.now() - buy_dt).days - so
# a weekend inflated it. 000543 皖能电力 bought Friday 2026-07-17 and sold
# Tuesday 2026-07-21 recorded 3 days against 2 actual sessions.
#
# That matters because MAX_HOLD_DAYS is 10 and the hold-length study that chose
# 10 measured TRADING SESSIONS: +5.60pp/yr at ten against +3.80 at five and
# -0.75 at three. Ten calendar days spanning two weekends is about seven
# sessions, so the exit has been firing roughly a third early on every position
# since long before any halt existed.
#
# The calendar is NOT duplicated here. trading_calendar carries the official
# SSE/SZSE closures for 2025-2026 and the trader already imports it; a second
# copy would drift, and this one already did - it was missing nothing but the
# canonical table held a wrong 2026-09-28 that this module disagreed with.
# One table, one place.
from trading_calendar import (
    MARKET_HOLIDAYS as _CANON_HOLIDAYS,
    is_trading_day as _is_trading_day,
)

# Derived from the canonical table rather than restated, so extending the
# calendar to a new year automatically extends what this will answer for.
# Outside these years sessions_held returns None instead of guessing a
# weekday calendar, because a wrong session count moves a real exit.
HOLIDAY_COVERAGE_YEARS = tuple(sorted({int(str(d)[:4]) for d in _CANON_HOLIDAYS}))


def _as_date(value):
    if isinstance(value, _dt.date):
        return value
    s = str(value or "").strip()[:10].replace("/", "-")
    try:
        return _dt.date(*map(int, s.split("-")))
    except (TypeError, ValueError):
        return None


def is_trading_session(day) -> bool:
    d = _as_date(day)
    return bool(d) and bool(_is_trading_day(d))


def sessions_held(buy_date, as_of, *, halted_dates=()) -> int | None:
    """Trading sessions a position has actually been holdable.

    Counts sessions AFTER the buy up to and including as_of, skipping weekends,
    exchange holidays, and any session the stock was suspended.

    A halted session carries no decay and no opportunity, so ageing a position
    through one would fire an exit whose reason never happened - and fire it
    into a stock that cannot fill the order.

    Returns None outside the verified holiday table rather than guessing a
    weekday calendar, because a wrong session count moves a real exit.
    """
    a, b = _as_date(buy_date), _as_date(as_of)
    if a is None or b is None:
        return None
    if a.year not in HOLIDAY_COVERAGE_YEARS or b.year not in HOLIDAY_COVERAGE_YEARS:
        return None
    if b < a:
        return 0
    skip = {d for d in (_as_date(x) for x in (halted_dates or [])) if d}
    n = 0
    cur = a + _dt.timedelta(days=1)
    while cur <= b:
        if is_trading_session(cur) and cur not in skip:
            n += 1
        cur += _dt.timedelta(days=1)
    return n
