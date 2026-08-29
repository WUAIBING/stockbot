#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rebuild the trade ledger from broker fills instead of quote lookups.

v10_trade_episode_history records what the system BELIEVED it paid, taken from a
quote resolved around order time. The broker knows what it actually paid. Those
two disagreed badly enough to invert the strategy's reported performance:

    recorded avg return over 29 episodes    +38.32%
    price-verified over 26 episodes          -0.77%
    win rate, verified                        34.6%   (9 of 26)

Three entries were impossible against the real tape, and they failed in two
different ways:

    688205 德科立   ledger 22.92, real fill 155.65. 22.92 has never been inside
                    this stock range in 983 sessions - its all-time low close is
                    24.10 - so that number belongs to a DIFFERENT SECURITY. Same
                    signature as the pytdx socket desync that corrupted the
                    scanner: a response read against the wrong request.

    601609 金田股份  ledger 11.89, real fill 13.34. Here 11.89 IS a real price
                    for this code, but from 18-19 August, four to six sessions
                    before the 24 August buy. A STALE quote, not a wrong one.

Those two 德科立 episodes were booked at +557% and +564% on a trade that in fact
LOST about 2.7% (400 bought at 155.65, sold 200 at 150.50 and 200 at 152.34).
They alone drag the average from -0.77% to +38.32%, and the learning layer reads
this file to decide which patterns worked - so it has been studying a fabricated
triumph.

THE FIX IS NOT A BETTER QUOTE LOOKUP

Both failures come from the same decision: deriving the entry price rather than
reading it. A fill price is a fact the broker reports; a quote is a guess about
what a fill might have cost. This module never guesses.

TWO DETAILS THAT WILL CORRUPT THIS FILE IF IGNORED

priceDec is PER ORDER and is not always 2. Across 293 orders it is 2 on 259, 1
on 27 and 0 on 7. A hardcoded divide-by-100 turns 众生药业 raw 27 into 0.27 and
有研硅 raw 397 into 3.97 - the same class of error this module exists to remove.

Rejected orders carry a price but no fill. Of 293 orders, 49 are status 9 and 7
are status 8, all with tradeCount 0. Counting them as trades would inflate the
episode count and score decisions that never happened - 德科立 alone has nine
rejected sells sitting next to its two real ones.

WHAT CANNOT BE REBUILT, AND WHY IT IS TRUNCATION RATHER THAN AN EMPTY PAST

The orders endpoint holds 293 records reaching back to 2026-06-01 while the
account has run 163 days, and it accepts no date range and no pagination -
fltOrderDrt and fltOrderStatus are its only parameters, and totalNum equals the
number returned. So the first ten weeks are simply not retrievable.

That this is a rolling window rather than a quiet start was settled by the
arithmetic. Rebuilt realised P&L of +92,910.68 plus floating +3,907.30 stands
against an account lifetime of +78,266.82, a gap of 18,551.16. Two things fill
it exactly:

    002423 中粮资本 sold 2026-06-04, 9,000 shares at 9.02, with NO buy anywhere
    in the window. An entry of 10.52 closes the gap, and 10.52 was tradeable on
    seven sessions in late March and early April - when this account was two
    weeks old.        9,000 x (10.52 - 9.02)  ~ 13,483

    commission at 0.025% each way on 10,140,643 of turnover plus stamp duty at
    0.05% on 5,066,974 of sales                ~  5,069
                                               ---------
                                                  18,552   against 18,551 observed

So: from 2026-06-01 the record is complete and authoritative. Before it, only
positions still open INTO the window are visible at all, and they appear here as
unpaired sells rather than being dropped. Anything bought and closed entirely
before June is gone.

THE WINDOW IS THE REASON TO ARCHIVE. Because it rolls, history exists only if
something writes it down. A daily snapshot of the raw orders response costs one
call and makes this limitation stop growing.

READ ONLY

It fetches orders, positions and balance. It holds no trade or cancel path, and
a test asserts it.
"""

from __future__ import annotations

import collections
from typing import Iterable, Mapping, Sequence

ORDERS_ENDPOINT = "/api/claw/mockTrading/orders"
ORDERS_PAYLOAD = {"fltOrderDrt": 0, "fltOrderStatus": 0}

DRT_BUY = 1
DRT_SELL = 2

# A fill is proven by tradeCount, not by status. Statuses seen in live data are
# 4 (filled), 9 and 8 (both rejected, tradeCount 0); trusting a status whitelist
# would break the first time the broker adds a code, whereas "it filled some
# quantity" is the actual question being asked.
def is_filled(order: Mapping) -> bool:
    try:
        return int(order.get("tradeCount") or 0) > 0
    except (TypeError, ValueError):
        return False


def scale_price(order: Mapping, field: str = "tradePrice") -> float | None:
    """Raw integer price -> yuan, using THIS order's priceDec.

    priceDec varies per order (2, 1 and 0 all appear in live data). Hardcoding
    100 here would reintroduce exactly the kind of silent 10x error this module
    was written to eliminate.
    """
    raw = order.get(field)
    if raw is None:
        return None
    try:
        dec = int(order.get("priceDec", 2))
        value = float(raw) / (10.0 ** dec)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def normalise_orders(orders: Iterable[Mapping]) -> list[dict]:
    """Filled orders only, priced in yuan, oldest first."""
    out = []
    for o in orders:
        if not is_filled(o):
            continue
        price = scale_price(o)
        if price is None:
            continue
        code = str(o.get("secCode") or "").strip()
        drt = o.get("drt")
        if not code or drt not in (DRT_BUY, DRT_SELL):
            continue
        try:
            qty = int(o.get("tradeCount") or 0)
            ts = int(o.get("time") or 0)
        except (TypeError, ValueError):
            continue
        out.append({
            "order_id": str(o.get("id") or ""),
            "code": code,
            "name": str(o.get("secName") or "").strip(),
            "side": "buy" if drt == DRT_BUY else "sell",
            "price": price,
            "quantity": qty,
            "amount": price * qty,
            "time": ts,
        })
    out.sort(key=lambda r: (r["time"], r["order_id"]))
    return out


def build_episodes(orders: Iterable[Mapping]) -> dict:
    """FIFO-pair fills into round trips, per code.

    FIFO because partial fills are normal here: 德科立 was one 400-share buy
    closed by two 200-share sells, and pairing whole orders would have thrown
    away half the position or double counted it.

    A sell with no open lot means the buy predates the API window. That is
    reported as an unpaired sell rather than dropped, because a silently short
    ledger is what made the old one believable.
    """
    lots: dict[str, collections.deque] = collections.defaultdict(collections.deque)
    episodes: list[dict] = []
    unpaired_sells: list[dict] = []

    for o in normalise_orders(orders):
        code = o["code"]
        if o["side"] == "buy":
            lots[code].append(dict(o, remaining=o["quantity"]))
            continue
        remaining = o["quantity"]
        while remaining > 0 and lots[code]:
            lot = lots[code][0]
            matched = min(remaining, lot["remaining"])
            entry, exit_ = lot["price"], o["price"]
            episodes.append({
                "code": code,
                "name": o["name"] or lot["name"],
                "quantity": matched,
                "entry_price": round(entry, 4),
                "exit_price": round(exit_, 4),
                "buy_time": lot["time"],
                "sell_time": o["time"],
                "hold_seconds": max(0, o["time"] - lot["time"]),
                "buy_order_id": lot["order_id"],
                "sell_order_id": o["order_id"],
                "pnl": round((exit_ - entry) * matched, 2),
                "pnl_pct": round((exit_ / entry - 1.0) * 100.0, 4) if entry else None,
                "source": "broker_fill",
            })
            lot["remaining"] -= matched
            remaining -= matched
            if lot["remaining"] <= 0:
                lots[code].popleft()
        if remaining > 0:
            unpaired_sells.append(dict(o, unmatched_quantity=remaining))

    open_lots = [dict(l) for code in lots for l in lots[code] if l["remaining"] > 0]
    return {
        "episodes": episodes,
        "unpaired_sells": unpaired_sells,
        "open_lots": open_lots,
        "summary": summarise(episodes),
    }


def summarise(episodes: Sequence[Mapping]) -> dict:
    """Plain arithmetic on realised round trips. No weighting, no exclusions."""
    rets = [e["pnl_pct"] for e in episodes if e.get("pnl_pct") is not None]
    pnl = sum(e.get("pnl") or 0.0 for e in episodes)
    wins = sum(1 for r in rets if r > 0)
    return {
        "episode_count": len(episodes),
        "realised_pnl": round(pnl, 2),
        "avg_return_pct": round(sum(rets) / len(rets), 4) if rets else None,
        "win_rate_pct": round(100.0 * wins / len(rets), 2) if rets else None,
        "win_count": wins,
        "loss_count": len(rets) - wins,
    }


def reconcile(local_episodes: Iterable[Mapping],
              broker_orders: Iterable[Mapping],
              tolerance_pct: float = 1.0) -> list[dict]:
    """Flag local entry prices that no broker fill supports.

    This is the check that was missing. 德科立 sat in the ledger at 22.92 against
    a 155.65 fill for days, and nothing in the system was positioned to notice,
    because nothing ever compared the two.
    """
    fills: dict[str, list[float]] = collections.defaultdict(list)
    for o in normalise_orders(broker_orders):
        if o["side"] == "buy":
            fills[o["code"]].append(o["price"])

    problems = []
    for e in local_episodes:
        code = str(e.get("code") or "").strip()
        try:
            entry = float(e.get("entry_price") or 0)
        except (TypeError, ValueError):
            entry = 0.0
        known = fills.get(code) or []
        if not known:
            problems.append({
                "code": code, "name": e.get("name"), "entry_price": entry,
                "issue": "no broker fill in the available window",
                "verifiable": False,
            })
            continue
        if entry <= 0:
            problems.append({"code": code, "name": e.get("name"),
                             "entry_price": entry, "issue": "missing entry price",
                             "verifiable": True})
            continue
        nearest = min(known, key=lambda p: abs(p - entry))
        drift = abs(nearest - entry) / nearest * 100.0 if nearest else 0.0
        if drift > tolerance_pct:
            problems.append({
                "code": code, "name": e.get("name"), "entry_price": entry,
                "nearest_fill": round(nearest, 4),
                "drift_pct": round(drift, 2),
                "issue": "entry price matches no broker fill",
                "verifiable": True,
            })
    return problems
