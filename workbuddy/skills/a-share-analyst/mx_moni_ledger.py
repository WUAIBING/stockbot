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

That this is a rolling window rather than a quiet start was settled by a sale
with no purchase: 002423 中粮资本, 9,000 shares at 9.02 on 2026-06-04, bought
2026-03-20 at 11.06 when the account was two weeks old.

With every known fact supplied - that opening lot, and 瑞可达 10转4派3元 credited
2026-06-17 - the window accounts for:

    122 round trips, realised +86,450.11 including 90.00 of dividend
    expected realised is 78,266.82 lifetime less 3,907.30 floating = 74,359.52
    residual                                                        +12,090.59

The residual is POSITIVE, meaning the rebuild claims more than the account made,
and the explanation that fits is the one thing the API structurally cannot show:
round trips that both opened AND closed before 2026-06-01. A position carried
across the boundary at least leaves its sale behind as evidence. One completed
entirely inside the missing ten weeks leaves nothing at all, and about 12,090 of
losses there would close the books.

That figure is bounded, not proven, and it is quoted as an unknown rather than
folded into any performance number. An earlier version of this file claimed the
books closed to 191.16 by attributing about 5,069 to commission and stamp duty
and inferring a 10.52 entry from the remainder. The real entry was 11.06 and the
simulator charges no meaningful fees; that reconciliation only looked clean
because a -11,717.70 error on 瑞可达 was cancelling it. Reasoning backwards from
a gap produces a number that fits and is wrong.

So: from 2026-06-01 the record is complete and authoritative. Before it, only
positions still open INTO the window are visible at all.

THE WINDOW IS THE REASON TO ARCHIVE. Because it rolls, history exists only if
something writes it down. A daily snapshot of the raw orders response costs one
call and makes this limitation stop growing.

READ ONLY

It fetches orders, positions and balance. It holds no trade or cancel path, and
a test asserts it.
"""

from __future__ import annotations

import collections
import datetime as _dt
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


def build_episodes(orders: Iterable[Mapping],
                   opening_lots: Iterable[Mapping] | None = None,
                   corporate_actions: Iterable[Mapping] | None = None) -> dict:
    """FIFO-pair fills into round trips, per code.

    FIFO because partial fills are normal here: 德科立 was one 400-share buy
    closed by two 200-share sells, and pairing whole orders would have thrown
    away half the position or double counted it.

    opening_lots carries positions already held when the window opens, which the
    orders endpoint cannot supply and which are therefore supplied by hand from
    the 调仓记录 in the 妙想 app. Each needs code, price, quantity and time.
    Seeding them lets the matching sell find its real cost basis:

        002423 中粮资本, 9,000 bought 2026-03-20 at 11.06, sold 2026-06-04 at
        9.02. That single position is a 18,360 loss, and it accounts for 18,360
        of the 18,551 that otherwise separates rebuilt P&L from the account
        lifetime figure - leaving 191, which also settles that this simulator
        charges no meaningful commission or stamp duty.

    A sell still lacking an open lot is reported as unpaired rather than
    dropped, because a silently short ledger is what made the old one
    believable.
    """
    lots: dict[str, collections.deque] = collections.defaultdict(collections.deque)
    episodes: list[dict] = []
    unpaired_sells: list[dict] = []

    for seed in (opening_lots or []):
        code = str(seed.get("code") or "").strip()
        try:
            price = float(seed.get("price") or 0)
            qty = int(seed.get("quantity") or 0)
            ts = int(seed.get("time") or 0)
        except (TypeError, ValueError):
            continue
        if not code or price <= 0 or qty <= 0:
            continue
        lots[code].append({
            "order_id": str(seed.get("order_id") or "opening"),
            "code": code, "name": str(seed.get("name") or "").strip(),
            "side": "buy", "price": price, "quantity": qty,
            "amount": price * qty, "time": ts, "remaining": qty,
            "opening": True,
        })

    # Ex dates are applied in time order against the orders, because an action
    # restates whatever is open AT that moment. 瑞可达 credited its 10转4派3元 on
    # 2026-06-17, between the 06-04 buy and the 06-22 sale, so applying it late
    # or early prices the wrong number of shares.
    pending = sorted(
        (dict(a) for a in (corporate_actions or []) if a.get("code")),
        key=lambda a: int(a.get("time") or 0))
    dividends: list[dict] = []

    def _apply_due(now: int) -> None:
        while pending and int(pending[0].get("time") or 0) <= now:
            act = pending.pop(0)
            code = str(act["code"]).strip()
            if not lots.get(code):
                continue
            res = apply_corporate_action(
                list(lots[code]),
                float(act.get("per_10_bonus") or 0.0),
                float(act.get("per_10_cash") or 0.0))
            lots[code] = collections.deque(res["lots"])
            if res["cash_dividend"]:
                dividends.append({"code": code, "time": act.get("time"),
                                  "cash": res["cash_dividend"]})

    for o in normalise_orders(orders):
        _apply_due(o["time"])
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
                # An opening lot was typed in from the app rather than read from
                # a fill, so it is kept labelled: this ledger exists because an
                # unlabelled hand-derived price was trusted as a fill.
                "source": "opening_lot" if lot.get("opening") else "broker_fill",
            })
            lot["remaining"] -= matched
            remaining -= matched
            if lot["remaining"] <= 0:
                lots[code].popleft()
        if remaining > 0:
            unpaired_sells.append(dict(o, unmatched_quantity=remaining))

    _apply_due(2 ** 62)   # anything credited after the last order still counts
    open_lots = [dict(l) for code in lots for l in lots[code] if l["remaining"] > 0]
    cash_total = round(sum(d["cash"] for d in dividends), 2)
    out = {
        "episodes": episodes,
        "unpaired_sells": unpaired_sells,
        "open_lots": open_lots,
        "dividends": dividends,
        "cash_dividend_total": cash_total,
        "summary": summarise(episodes),
    }
    # A dividend is realised the moment it is paid, so it belongs in realised
    # P&L even when the position is never closed.
    out["summary"]["cash_dividend_total"] = cash_total
    out["summary"]["realised_pnl"] = round(
        out["summary"]["realised_pnl"] + cash_total, 2)
    return out


def apply_corporate_action(lots: Sequence[Mapping], per_10_bonus: float,
                           per_10_cash: float) -> dict:
    """Restate open lots across an ex date. A-share terms read 每10股转X派Y元.

    688800 瑞可达 was 10转4派3元: every ten shares became fourteen, and every ten
    paid 3 yuan. On 300 shares that is 120 free shares and 90 yuan.

    The free shares are NOT profit and the lower price is NOT a loss - they are
    the same holding renumbered, which is why the stock opened 87.98 against a
    124.54 close on 2026-06-17. What must move is the BASIS: 38,526 paid, spread
    over 420 shares instead of 300, is 91.73 a share rather than 128.42. Selling
    at 98.31 is then a gain, and the -9,033 that naive pairing reported was an
    artefact of comparing a pre-adjustment cost against a post-adjustment price.

    Cash is returned separately rather than folded into the basis, because a
    dividend is realised money the moment it is paid, whether or not the position
    is ever closed.
    """
    if per_10_bonus < 0 or per_10_cash < 0:
        raise ValueError("corporate action terms cannot be negative")
    factor = 1.0 + (per_10_bonus / 10.0)
    cash = 0.0
    out = []
    for lot in lots:
        qty = int(lot.get("remaining", lot.get("quantity", 0)) or 0)
        price = float(lot.get("price") or 0)
        if qty <= 0 or price <= 0:
            out.append(dict(lot))
            continue
        cash += qty / 10.0 * per_10_cash
        new_qty = int(round(qty * factor))
        adjusted = dict(lot)
        adjusted["remaining"] = new_qty
        adjusted["quantity"] = new_qty
        # Cost is preserved, not the per-share price: the holding did not change
        # value, only how many pieces it is divided into.
        adjusted["price"] = round(price * qty / new_qty, 6) if new_qty else price
        adjusted["corporate_action"] = "10转%g派%g" % (per_10_bonus, per_10_cash)
        out.append(adjusted)
    return {"lots": out, "cash_dividend": round(cash, 2)}


def share_count_anomalies(orders: Iterable[Mapping]) -> list[dict]:
    """Codes where more was sold than bought. A share count does not grow by itself.

    Two causes, and they need opposite treatment:

      a position opened before the window - the buy is simply not retrievable,
      and an opening lot supplies its basis

      a bonus or capitalisation issue - the shares are real and free, and the
      price was cut to match on the ex date, so pairing the ORIGINAL entry price
      against the POST-adjustment sale invents a loss that never happened

    688800 瑞可达 is the second kind. 300 bought at 128.42 on 2026-06-04; on
    2026-06-17 it opened at 87.98 against a 124.54 close, a -29.4% gap that is
    the -28.6% of a 4-for-10 issue, not a collapse. 419 shares were then sold for
    41,210.70 against 38,526 paid - a GAIN of 2,684.70. Pairing 300 at 128.42
    into a 98.31 sale reports -9,033 instead, and that was this ledger largest
    single loss until the share counts were checked against each other.

    Reporting the anomaly is the point. A ledger that silently books -9,033 for a
    profitable position is the failure this module was written to end, and doing
    it with fill prices rather than quotes would be no improvement at all.
    """
    tally: dict[str, dict] = {}
    for o in normalise_orders(orders):
        e = tally.setdefault(o["code"], {"code": o["code"], "name": o["name"],
                                         "bought": 0, "sold": 0})
        e["bought" if o["side"] == "buy" else "sold"] += o["quantity"]
        if o["name"]:
            e["name"] = o["name"]
    out = []
    for e in tally.values():
        if e["sold"] > e["bought"]:
            extra = e["sold"] - e["bought"]
            out.append(dict(
                e, extra_shares=extra,
                extra_ratio_pct=(round(100.0 * extra / e["bought"], 2)
                                 if e["bought"] else None),
                likely_cause=("opened before the window" if e["bought"] == 0
                              else "bonus or capitalisation issue"),
            ))
    return sorted(out, key=lambda x: -x["extra_shares"])


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


# Fields the broker settles and the strategy may not overwrite. These are the
# ones that were wrong: entry_price carried 22.92 against a 155.65 fill.
# The local sell date can be a day out, so matching tolerates drift rather
# than trusting it. 002258 was recorded a day late at a price never traded.
MATCH_DATE_TOLERANCE_DAYS = 3

EXECUTION_FIELDS = (
    "entry_price", "sell_price", "quantity", "buy_amount",
    "pnl", "pnl_pct", "buy_date", "sell_date", "hold_days",
    "buy_order_id", "sell_order_id",
)

# Fields only the strategy knows. The broker has no idea why a position was
# opened or what rule closed it, so a rebuild that dropped these would fix the
# prices and destroy every reason the learning layer reads.
STRATEGY_FIELDS = (
    "mode", "tier", "decision_id", "decision_run_slot", "selected_reason_hash",
    "market_regime", "build_note", "close_reason", "big_meat_state",
    "big_meat_confirmed_at", "big_meat_success_flag", "false_selection_flag",
    "falsify_level", "falsify_reason_codes", "t3_observe_flag",
    "candidate_pool_flag", "execution_damaged", "execution_damage_score",
    "execution_damage_reasons", "profit_truncation", "decision_match",
    "selection_verdict", "sample_quality_score", "blocked_reasons",
)


def _day(ts: int) -> str:
    import datetime as _d
    return _d.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")


def rebuild_episode_history(broker_episodes: Sequence[Mapping],
                            local_episodes: Iterable[Mapping]) -> dict:
    """Broker fills for the facts, the local file for the intent.

    The corrupt history is not simply wrong, it is wrong in one direction: every
    price came from a quote and every REASON came from the strategy itself.
    Rebuilding from fills alone would fix 22.92 and throw away close_reason,
    mode and tier - the only record of why anything was done, and the whole
    reason the learning layer reads this file.

    So execution fields are taken from the broker and strategy fields are
    carried across, matched on code and sell date.

    Three outcomes, all reported, because the counts are the finding:

      matched     a local record backed by a real fill
      unrecorded  a real round trip the local file never held - there are far
                  more of these than matches, since it held 29 against 120
      orphaned    a local record no fill supports. 德科立 at 22.92 is one, and
                  an orphan is never carried forward: it describes a trade that
                  did not happen at the price claimed.
    """
    # Matching must NOT key on the local sell date, because that field is
    # corrupt too. 002258 利尔化学 was really sold 2026-08-12 at 14.15 and the
    # local file records 2026-08-13 at 14.04 - wrong day and wrong price. An
    # exact-date key silently orphans the record and loses its close_reason.
    # So: match within a code by quantity first, then nearest date.
    by_code: dict[str, list] = collections.defaultdict(list)
    for le in local_episodes:
        by_code[str(le.get("code") or "").strip()].append(le)

    rebuilt, unrecorded = [], []
    used: set[tuple] = set()

    def _pick(code: str, sell_day: str, qty: int):
        pool = by_code.get(code) or []
        best, best_i, best_cost = None, None, None
        for i, le in enumerate(pool):
            if (code, i) in used:
                continue
            lq = le.get("quantity")
            try:
                same_qty = int(lq) == int(qty)
            except (TypeError, ValueError):
                same_qty = False
            try:
                drift = abs((_dt.date(*map(int, str(le.get("sell_date"))[:10].split("-")))
                             - _dt.date(*map(int, sell_day.split("-")))).days)
            except (TypeError, ValueError):
                drift = 99
            if drift > MATCH_DATE_TOLERANCE_DAYS:
                continue
            cost = (0 if same_qty else 100) + drift
            if best_cost is None or cost < best_cost:
                best, best_i, best_cost = le, i, cost
        if best is not None:
            used.add((code, best_i))
        return best

    for be in broker_episodes:
        match = _pick(be["code"], _day(be["sell_time"]), be["quantity"])
        row = {
            "code": be["code"], "name": be.get("name"),
            "entry_price": be["entry_price"], "sell_price": be["exit_price"],
            "quantity": be["quantity"],
            "buy_amount": round(be["entry_price"] * be["quantity"], 2),
            "pnl": be["pnl"], "pnl_pct": be["pnl_pct"],
            "buy_date": _day(be["buy_time"]), "sell_date": _day(be["sell_time"]),
            "hold_days": max(0, be["hold_seconds"] // 86400),
            "buy_order_id": be.get("buy_order_id"),
            "sell_order_id": be.get("sell_order_id"),
            "price_source": be.get("source", "broker_fill"),
        }
        if match:
            for f in STRATEGY_FIELDS:
                if f in match:
                    row[f] = match[f]
            row["metadata_source"] = "local_episode"
        else:
            row["metadata_source"] = "none"
            unrecorded.append(row)
        rebuilt.append(row)

    orphaned = []
    for code, pool in by_code.items():
        for i, le in enumerate(pool):
            if (code, i) not in used:
                orphaned.append({"code": code,
                                 "sell_date": str(le.get("sell_date"))[:10],
                                 "entry_price": le.get("entry_price"),
                                 "pnl_pct": le.get("pnl_pct"),
                                 "name": le.get("name")})

    return {
        "episodes": rebuilt,
        "unrecorded": unrecorded,
        "orphaned": orphaned,
        "summary": dict(summarise(rebuilt),
                        matched=len(rebuilt) - len(unrecorded),
                        unrecorded_count=len(unrecorded),
                        orphaned_count=len(orphaned)),
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
