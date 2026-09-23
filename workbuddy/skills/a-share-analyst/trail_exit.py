#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Let the fish run: exit on a trailing stop, not on a single red candle.

WHAT THE HISTORY SHOWED (70 closed mainline trades, 2026-07-15 .. 09-07)

    29 ran +10% within 20 sessions of entry; we held long enough to see +10% on
    6 and booked it on 1. 5 ran +20%; we booked 1. 77% of trades were closed
    within 3 sessions.

The exit that did the damage was the candle rule. A close more than 2% below the
day's open scored 3 on its own and sold the whole position - and during the
session "the day's open" is fifteen minutes old. Split by what the stock did
AFTER we sold, excess over CSI 1000 across 10 sessions:

    signal decay   n=31   -1.55%   (those sells were right)
    risk_trim      n=19   +4.57%   (the stocks we sold went on to outrun the index)

risk_trim is the same candle rule firing on positions already labelled big-fish
candidates - the ones with the best runs ahead (+13.3% average within 20
sessions). 688205 德科立 ran +50.9% within 20 sessions of entry and was sold
twice by it, at -3.3% and -2.1%.

WHAT THE WHOLE MARKET SHOWED (CSI 1000, ~2 years, 54,281 pool-like entries:
turnover 1-7亿, bought at the close, filled at the next open, 0.536% cost)

    exit                 mean     booked +20%   median   hold
    live candle rules   -0.06%       2.3%       -1.8%     6
    trailing            +1.42%      10.2%       -2.6%    25

The candle rules sell before a fish can become one. The parameters here were
chosen from a robustness grid as a setting that held in BOTH halves of the
sample, not as the single best cell. See exit_grid in the progress log.

WHAT THIS DOES NOT FIX

Our entries have no edge at finding big fish: 7.1% of them touched +20% within
20 sessions against 9.2% for random CSI 1000 names on the same dates. In the
most recent year every exit loses money on pool-like entries; the trailing exit
loses least and books five times the fish, but its median trade is a stop-out.
Exits decide whether we catch fish. Entries decide whether catching them pays.

Pure module: prices in, verdict out. No market data access, no orders.
"""

from __future__ import annotations

# Chosen from the robustness grid; see the module docstring.
STOP_PCT = -8.0          # close this far below entry -> exit
TRAIL_WIDTH_PCT = 10.0   # once trailing, close this far below the best close -> exit
ACTIVATE_PCT = 10.0      # trailing starts once any close is this far above entry
MAX_HOLD_SESSIONS = 40   # backstop; the trail, not the clock, should end a runner


def evaluate(closes_after_entry, entry_price, *, stop_pct=STOP_PCT,
             width_pct=TRAIL_WIDTH_PCT, activate_pct=ACTIVATE_PCT):
    """Judge an open position on COMPLETED closes since the entry session.

    closes_after_entry: closes of the sessions after the buy, oldest first,
    excluding any bar still forming. The entry price stands in for the entry
    session's close, as in the study (the 14:50 buy sits at the close).

    Returns a dict with should_exit, reason, and the state that produced it.
    An empty or unusable input never exits - it reports data_unavailable so the
    caller can say so, because silence here is how exits went blind before.
    """
    try:
        entry = float(entry_price)
    except (TypeError, ValueError):
        entry = 0.0
    closes = []
    for value in closes_after_entry or []:
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if v > 0:
            closes.append(v)
    verdict = {
        "should_exit": False, "reason": "", "trailing": False, "sessions": len(closes),
        "best_close": entry, "best_ret_pct": 0.0, "last_close": entry, "last_ret_pct": 0.0,
        "data_unavailable": entry <= 0 or not closes,
    }
    if verdict["data_unavailable"]:
        verdict["reason"] = "无完成K线，跟踪止盈未评估" if entry > 0 else "无有效成本价"
        return verdict

    best = entry
    trailing = False
    for close in closes:
        best = max(best, close)
        if best >= entry * (1 + activate_pct / 100.0):
            trailing = True
    last = closes[-1]
    last_ret = (last / entry - 1) * 100.0
    best_ret = (best / entry - 1) * 100.0
    from_best = (last / best - 1) * 100.0
    verdict.update(trailing=trailing, best_close=best, best_ret_pct=best_ret,
                   last_close=last, last_ret_pct=last_ret)

    if last_ret <= stop_pct:
        verdict["should_exit"] = True
        verdict["reason"] = "收盘止损%+.1f%%(线%.0f%%)" % (last_ret, stop_pct)
    # Same comparison, same form, as the study's loop - a rounding difference at
    # the boundary would make the deployed rule a different rule.
    elif trailing and last < best * (1 - width_pct / 100.0):
        verdict["should_exit"] = True
        verdict["reason"] = "跟踪止盈: 最高收盘%+.1f%% 回撤%.1f%% 现%+.1f%%" % (best_ret, from_best, last_ret)
    elif trailing:
        verdict["reason"] = "跟踪中: 最高收盘%+.1f%% 回撤%.1f%%(线-%.0f%%)" % (best_ret, from_best, width_pct)
    else:
        verdict["reason"] = "未启动跟踪: 最高收盘%+.1f%%(启动+%.0f%%)" % (best_ret, activate_pct)
    return verdict
