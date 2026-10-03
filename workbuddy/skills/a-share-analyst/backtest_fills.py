#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prices you could actually have traded at. Use this in every backtest.

WHY THIS EXISTS

On 2026-09-23 a study of 970 trading-halt resumptions across 5,219 A-shares
showed stocks that resumed locked at limit-up returning +12.78% over the next
20 sessions. It assumed a buy at the next day's open. Those stocks usually
OPEN locked again - no sellers, no fill - and the run of sealed limit-ups is
where the whole gain sat. Priced the way an order could really fill:

    the first fillable session came 1.4 sessions later on average,
    at +17.4% above the assumed open, and returned -7.83% (about t-7),
    negative in every year 2020-2026.

Same events, opposite conclusion. A rule in a notes file had not prevented it,
so the rule lives here as the one function research uses for fills.

THE RULES

  buy   open below the limit-up price          -> fill at the open
        opens at the limit but trades below it -> fill AT the limit price
        trades only at the limit (one-word)    -> no fill
  sell  the mirror image against limit-down

"Fill at the limit when the board breaks" is still optimistic - it assumes our
order is at the front of the queue - so treat results near that case as an
upper bound. Limit prices round half-up to the cent, as the exchanges do;
Python's round() rounds half-to-even and gets some of them wrong.

Pure functions over bars. No market data access, no orders.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

TICK = 0.005   # half a cent: anything closer than this to the limit IS the limit


def limit_pct(code, is_st=False):
    """Daily price limit as a fraction. ST status is not knowable from the
    code, so the caller says; the default is the board's normal limit."""
    if is_st:
        return 0.05
    code = str(code).zfill(6)
    if code.startswith(("688", "689", "300", "301")):
        return 0.20
    if code.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def limit_prices(prev_close, pct):
    """(limit_up, limit_down), rounded half-up to the cent."""
    base = Decimal(str(prev_close))
    step = Decimal(str(pct))
    cent = Decimal("0.01")
    up = (base * (Decimal(1) + step)).quantize(cent, rounding=ROUND_HALF_UP)
    down = (base * (Decimal(1) - step)).quantize(cent, rounding=ROUND_HALF_UP)
    return float(up), float(down)


def _ohlc(bar):
    if isinstance(bar, dict):
        return float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
    # (date, open, high, low, close, ...) - the tuple shape the research scripts use
    return float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4])


def buy_fill(bar, prev_close, pct):
    """Price a buy could fill at on this bar, or None if it was sealed limit-up."""
    o, _h, lo, _c = _ohlc(bar)
    up, _down = limit_prices(prev_close, pct)
    if o < up - TICK:
        return o
    if lo < up - TICK:
        return up
    return None


def sell_fill(bar, prev_close, pct):
    """Price a sell could fill at on this bar, or None if it was sealed limit-down."""
    o, h, _lo, _c = _ohlc(bar)
    _up, down = limit_prices(prev_close, pct)
    if o > down + TICK:
        return o
    if h > down + TICK:
        return down
    return None


def first_buy_fill(bars, start, pct, max_wait=10):
    """(index, price) of the first session from `start` a buy could fill, or None.

    `bars` is oldest-first; bars[start - 1] must exist (its close is the limit
    base). Gives up after `max_wait` sessions and says so with None - an entry
    you never got is not an entry.
    """
    for j in range(max(start, 1), min(start + max_wait, len(bars))):
        px = buy_fill(bars[j], _ohlc(bars[j - 1])[3], pct)
        if px is not None:
            return j, px
    return None


def first_sell_fill(bars, start, pct, max_wait=10):
    """(index, price) of the first session from `start` a sell could fill, or None."""
    for j in range(max(start, 1), min(start + max_wait, len(bars))):
        px = sell_fill(bars[j], _ohlc(bars[j - 1])[3], pct)
        if px is not None:
            return j, px
    return None
