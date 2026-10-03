#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Close the account's books from broker facts, and say whether they close.

WHY THIS EXISTS

For the third quarter of 2026 the system carried three scoreboards and none of
them agreed with the account:

    printed scoreboard        +31,591   92 closes   (local records)
    episode history           -19,458   92 closes   (the learning layer's file)
    the account itself        -35,887   change in net asset value

The printed figure summed local records whose entry prices came from quotes:
德科立 booked at 22.92 against a 155.65 fill is 51,418 of profit that never
happened. The history had been corrected for that, but 14 broker sales matched
no local record (-7,315): 12 were never recorded (-5,139) and 2 were recorded
against sell orders that never filled. It also counted only the first lot of
sells that closed several buys (3,293). And nothing accounted for fees, because an
earlier note had concluded the simulator charges none. It charges 0.094% on
buys and 0.157% on sells: 6,575 on 5.85M of turnover.

From broker facts alone the quarter closes to the cent:

    change in NAV                      -35,887.35
    realised P&L, 107 closes           -29,002.50
    change in unrealised                  -309.56
    costs, measured from cash           -6,575.29
    residual                                 0.00

THE IDENTITY

    change in NAV = realised + change in unrealised + dividends - costs

    NAV, position value   balance endpoint snapshots
    fills                 orders endpoint, FIFO per code (mx_moni_ledger)
    costs                 (sells - buys + known dividends) - change in true cash

true cash = total_assets - total_pos_value. Not avail_balance: that drops when
an order is PLACED, so while orders are pending it understates cash by whatever
is frozen (2026-09-09 14:53: avail fell 168,009 on 125,654 of fills).

Costs are measured, never assumed, so the identity closes by construction. What
makes it worth trusting is the two checks that cannot be satisfied by
arithmetic:

  * the ledger's open lots must equal the broker's positions share for share.
    A fill missing from the archive cannot hide there.
  * the measured cost must be a plausible fraction of turnover. A missing fill
    would show up as a "cost" the size of a whole trade.

Dividends the supplement file does not list arrive as cash and are therefore
netted into costs; days whose cost is far from the fitted rate are reported so
they can be identified and recorded.

Pure functions. No network, no file access, no orders.
"""

from __future__ import annotations

import datetime as _dt
from collections import defaultdict
from typing import Iterable, Mapping, Sequence

import mx_moni_ledger as _ledger

CHINA = _dt.timezone(_dt.timedelta(hours=8))

# Commission plus stamp duty runs about 0.25% a round trip here. A measured cost
# outside this band is not a fee - it is a missing fill or an unrecorded cash
# movement, and the books must say so instead of absorbing it.
MAX_PLAUSIBLE_COST_RATE = 0.004      # of turnover
MIN_PLAUSIBLE_COST_RATE = -0.001     # small dividends can push a quiet window negative


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def true_cash(snapshot: Mapping) -> float:
    """Cash including what is frozen for unfilled orders."""
    return _num(snapshot.get("total_assets")) - _num(snapshot.get("total_pos_value"))


def clean_snapshots(snapshots: Iterable[Mapping]) -> list[dict]:
    """Usable balance snapshots, oldest first. A zero NAV is an API failure
    that was written down, not a wipe-out."""
    out = []
    for s in snapshots or []:
        try:
            ts = int(s.get("ts"))
        except (TypeError, ValueError):
            continue
        if _num(s.get("total_assets")) <= 0:
            continue
        out.append({"ts": ts, "total_assets": _num(s.get("total_assets")),
                    "total_pos_value": _num(s.get("total_pos_value"))})
    out.sort(key=lambda s: s["ts"])
    return out


def _china_day(ts: int) -> str:
    return _dt.datetime.fromtimestamp(int(ts), CHINA).strftime("%Y-%m-%d")


def daily_costs(snapshots: Sequence[Mapping], fills: Sequence[Mapping]) -> list[dict]:
    """Per China trading day: what was bought, sold, and what it cost in cash.

    Measured between the LAST snapshot of consecutive days. Order timestamps
    are placement times and a fill can land minutes later, so finer intervals
    would book a 14:53 buy against cash that has not moved yet; by the last
    snapshot of the day every fill of that day has settled into cash.
    """
    last = {}
    for s in snapshots:
        last[_china_day(s["ts"])] = s
    days = sorted(last)
    rows = []
    for a, b in zip(days, days[1:]):
        sa, sb = last[a], last[b]
        buys = sum(f["amount"] for f in fills if sa["ts"] < f["time"] <= sb["ts"] and f["side"] == "buy")
        sells = sum(f["amount"] for f in fills if sa["ts"] < f["time"] <= sb["ts"] and f["side"] == "sell")
        rows.append({"day": b, "buys": round(buys, 2), "sells": round(sells, 2),
                     "cost": round((sells - buys) - (true_cash(sb) - true_cash(sa)), 2)})
    return rows


def fit_cost_rates(days: Sequence[Mapping]) -> dict:
    """Least squares, no intercept: cost = buy_rate * buys + sell_rate * sells."""
    obs = [d for d in days if d["buys"] or d["sells"]]
    sbb = sum(d["buys"] ** 2 for d in obs)
    sss = sum(d["sells"] ** 2 for d in obs)
    sbs = sum(d["buys"] * d["sells"] for d in obs)
    sby = sum(d["buys"] * d["cost"] for d in obs)
    ssy = sum(d["sells"] * d["cost"] for d in obs)
    det = sbb * sss - sbs * sbs
    if len(obs) < 3 or abs(det) < 1e-6:
        return {"buy_rate": None, "sell_rate": None, "days": len(obs)}
    return {"buy_rate": (sby * sss - ssy * sbs) / det,
            "sell_rate": (ssy * sbb - sby * sbs) / det, "days": len(obs)}


def unusual_days(days: Sequence[Mapping], rates: Mapping, tolerance: float = 100.0) -> list[dict]:
    """Days whose cash did not behave like fees: dividends, or something missing.

    A day with no fills must not move cash at all. A day with fills should cost
    about the fitted rate. Anything else is listed, with its size, so it can be
    identified and written into the supplement rather than left inside "costs".
    """
    out = []
    for d in days:
        expected = 0.0
        if (d["buys"] or d["sells"]) and rates.get("buy_rate") is not None:
            expected = rates["buy_rate"] * d["buys"] + rates["sell_rate"] * d["sells"]
        gap = d["cost"] - expected
        if abs(gap) > tolerance:
            out.append({"day": d["day"], "cash_gap": round(-gap, 2),
                        "note": "cash in" if gap < 0 else "cash out",
                        "had_fills": bool(d["buys"] or d["sells"])})
    return out


def position_mismatches(open_lots: Iterable[Mapping], positions: Iterable[Mapping]) -> list[dict]:
    """Codes where the ledger and the broker hold different share counts."""
    mine = defaultdict(int)
    for lot in open_lots or []:
        mine[str(lot.get("code") or "").zfill(6)] += int(lot.get("remaining") or 0)
    theirs = defaultdict(int)
    for p in positions or []:
        theirs[str(p.get("code") or "").zfill(6)] += int(_num(p.get("count")))
    return [{"code": c, "ledger": mine.get(c, 0), "broker": theirs.get(c, 0)}
            for c in sorted(set(mine) | set(theirs))
            if mine.get(c, 0) != theirs.get(c, 0)]


def _state(orders, opening_lots, corporate_actions, ts):
    upto = [o for o in orders if int(_num(o.get("time"))) <= ts]
    acts = [a for a in (corporate_actions or []) if int(_num(a.get("time"))) <= ts]
    res = _ledger.build_episodes(upto, opening_lots=opening_lots, corporate_actions=acts)
    cost = sum(l["remaining"] * l["price"] for l in res["open_lots"])
    return res, cost


def books(orders: Iterable[Mapping], snapshots: Iterable[Mapping], *,
          opening_lots: Iterable[Mapping] | None = None,
          corporate_actions: Iterable[Mapping] | None = None,
          positions: Iterable[Mapping] | None = None,
          since_ts: int | None = None) -> dict:
    """The reconciled books between the first and last usable snapshot.

    `positions`, when given, must be the broker's positions AS OF the last
    snapshot; the share-for-share check is only as good as that.
    """
    orders = list(orders or [])
    snaps = clean_snapshots(snapshots)
    if since_ts is not None:
        snaps = [s for s in snaps if s["ts"] >= since_ts]
    if len(snaps) < 2:
        return {"verified": False, "problems": ["need two balance snapshots, have %d" % len(snaps)]}
    s0, s1 = snaps[0], snaps[-1]
    t0, t1 = s0["ts"], s1["ts"]

    fills = _ledger.normalise_orders(orders)
    full = _ledger.build_episodes(orders, opening_lots=opening_lots, corporate_actions=corporate_actions)
    window = [c for c in _ledger.closes(full["episodes"]) if t0 < c["sell_time"] <= t1]
    realised = round(sum(c["pnl"] for c in window), 2)
    rets = [c["pnl_pct"] for c in window if c.get("pnl_pct") is not None]
    wins = sum(1 for r in rets if r > 0)

    buys = sum(f["amount"] for f in fills if t0 < f["time"] <= t1 and f["side"] == "buy")
    sells = sum(f["amount"] for f in fills if t0 < f["time"] <= t1 and f["side"] == "sell")
    dividends = round(sum(_num(d.get("cash")) for d in full["dividends"]
                          if t0 < int(_num(d.get("time"))) <= t1), 2)
    costs = round((sells - buys + dividends) - (true_cash(s1) - true_cash(s0)), 2)
    turnover = buys + sells

    state0, cost0 = _state(orders, opening_lots, corporate_actions, t0)
    state1, cost1 = _state(orders, opening_lots, corporate_actions, t1)
    unreal0 = round(s0["total_pos_value"] - cost0, 2)
    unreal1 = round(s1["total_pos_value"] - cost1, 2)
    nav_change = round(s1["total_assets"] - s0["total_assets"], 2)
    parts = round(realised + (unreal1 - unreal0) + dividends - costs, 2)

    days = daily_costs(snaps, fills)
    rates = fit_cost_rates(days)

    problems = []
    unpaired = [u for u in full["unpaired_sells"] if t0 < u["time"] <= t1]
    if unpaired:
        problems.append("%d sells in the window have no buy on record" % len(unpaired))
    cost_rate = (costs / turnover) if turnover else 0.0
    if turnover and not (MIN_PLAUSIBLE_COST_RATE <= cost_rate <= MAX_PLAUSIBLE_COST_RATE):
        problems.append("measured cost is %.3f%% of turnover - not a fee; a fill or a cash movement "
                        "is missing" % (100 * cost_rate))
    position_check = {"checked": False, "mismatches": []}
    if positions is not None:
        mismatches = position_mismatches(state1["open_lots"], positions)
        position_check = {"checked": True, "mismatches": mismatches}
        if mismatches:
            problems.append("ledger and broker disagree on %d position(s): %s" % (
                len(mismatches), ", ".join("%s %d vs %d" % (m["code"], m["ledger"], m["broker"])
                                            for m in mismatches[:5])))
    if abs(nav_change - parts) > 0.05:
        problems.append("identity off by %.2f" % (nav_change - parts))

    return {
        "window": {"from_ts": t0, "to_ts": t1, "from_day": _china_day(t0), "to_day": _china_day(t1)},
        "nav_start": s0["total_assets"], "nav_end": s1["total_assets"], "nav_change": nav_change,
        "realised_pnl": realised,
        "unrealised_start": unreal0, "unrealised_end": unreal1,
        "unrealised_change": round(unreal1 - unreal0, 2),
        "dividends": dividends,
        "costs": costs, "turnover": round(turnover, 2),
        "cost_rate_pct": round(100 * cost_rate, 4),
        "fitted_rates_pct": {
            "buy": None if rates["buy_rate"] is None else round(100 * rates["buy_rate"], 4),
            "sell": None if rates["sell_rate"] is None else round(100 * rates["sell_rate"], 4),
            "days": rates["days"]},
        "unusual_days": unusual_days(days, rates),
        "residual": round(nav_change - parts, 2),
        "closes": {
            "count": len(window), "win_count": wins,
            "win_rate_pct": round(100.0 * wins / len(rets), 2) if rets else None,
            "avg_return_pct": round(sum(rets) / len(rets), 4) if rets else None,
            "realised_after_costs": round(realised - costs, 2)},
        "position_check": position_check,
        "verified": not problems and position_check["checked"],
        "problems": problems,
    }
