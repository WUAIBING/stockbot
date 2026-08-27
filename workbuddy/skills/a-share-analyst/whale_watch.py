#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Find where capital is piling up, and whether it is still piling or already gone.

A different selection principle from the rest of this system. The mainline picks
from a fixed CSI1000 universe by technical pattern. This ranks by WHERE MONEY IS
CONCENTRATING RIGHT NOW, using 东方财富's order-size-decomposed flow via the MX
API, and asks whether the concentration is ongoing or finished.

WHY EACH RULE EXISTS - all three came from screens that caught the wrong thing:

  Deep water, not small caps.
      A first screen filtered for 流通市值 < 300亿 on the theory that small caps
      show conviction. It returned ST汇洲 and other thin names where one block
      trade dominates an illiquid tape. A whale cannot build a position where
      there is nothing to buy. Rank by TURNOVER: that is where large money can
      actually transact.

  Flow relative to turnover, not absolute yuan.
      A second screen used 超大单净流入 > 3000万 outright. It returned 建设银行,
      中国银行, 南京银行, 中信证券, 中金公司, 华能国际 - index flow. 30M into a
      trillion-yuan bank is noise; the same figure in a mid-cap is conviction.

  Early in the campaign, not late.
      佰仁医疗 passed a one-day screen on 4,029万 of 超大单 inflow after nine
      sessions of noise. One session cannot tell a whale from a splash - but the
      first fix was worse than the problem. Requiring flow positive on 6 of 10
      sessions rejects block trades and SELECTS campaigns already six days along.
      A large position takes days or weeks to build; six positive days means the
      whale is most of the way done, and the supply it still needs is whoever
      buys now. That slowness is the opportunity only if you are early, so the
      test is the TURN - flow crossing from neutral or negative into positive on
      more than one session, while the prior window was NOT already accumulating
      and price has not moved.

WHAT IT DELIBERATELY WILL NOT DO

It selects and records. It does not size, order or trade, and a test asserts it
holds no execution path. Every candidate is written with the evidence behind it
so forward returns can score the idea later. Nothing here is validated: unlike
the price-based work in this repo, there is no 11-year backtest behind these
thresholds, because per-stock flow history is only retrievable ten sessions at a
time. Treat the output as a hypothesis under measurement.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, Mapping, Sequence

# Deep water. Below this, large money cannot operate without moving the tape.
MIN_TURNOVER_YUAN = 5.0e9          # 50亿

# 超大单 net inflow as a share of the session's turnover. Absolute yuan selects
# by size and finds index flow instead of conviction.
MIN_FLOW_RATIO = 0.05

# A large position takes days or weeks to build, and that slowness is the whole
# opportunity - but only if you are early in the campaign rather than late.
#
# The first version of this module required flow positive on 6 of 10 sessions.
# That rejects block trades, but look at what it SELECTS: a campaign already six
# days along. If a whale needs two weeks to build, six positive days means they
# are most of the way done, and the supply they still need is whoever buys now.
# It was structurally a LATE detector, which is exactly the moment you become
# their exit liquidity.
#
# So the test is the TURN, not the level: flow crossing from neutral or negative
# into positive, while price has not moved yet. Day one to three of a campaign,
# not day seven to ten.
SUSTAIN_WINDOW = 10
RECENT_DAYS = 3           # the possible turn
PRIOR_DAYS = 7            # what it is turning away from
MIN_RECENT_POSITIVE = 2   # of RECENT_DAYS, so one spike is not a campaign
MAX_PRIOR_POSITIVE = 0.50 # if the prior window was already accumulating, we are late

# If the move has already happened, the whale's wake is what is being bought.
MAX_PRICE_RUN_PCT = 15.0

_UNITS = (("万亿", 1.0e12), ("亿", 1.0e8), ("万", 1.0e4))


def parse_cn_number(text) -> float | None:
    """'25.04亿' -> 2.504e9. The API returns human units, not raw floats."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().replace(",", "").replace("元", "")
    if not s or s in {"-", "--", "None"}:
        return None
    for suffix, mult in _UNITS:
        if s.endswith(suffix):
            body = s[: -len(suffix)]
            try:
                return float(body) * mult
            except ValueError:
                return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_pct(text) -> float | None:
    """'8.43%' or '8.43' -> 8.43."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().replace("%", "")
    if not s or s in {"-", "--"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def screen_query(min_turnover_yuan: float = MIN_TURNOVER_YUAN,
                 min_flow_yuan: float = 1.0e8) -> str:
    """The natural-language screen. Ordered by turnover: deep water first."""
    return (
        f"成交额大于{min_turnover_yuan/1.0e8:.0f}亿 并且 "
        f"超大单净流入资金大于{min_flow_yuan/1.0e8:.0f}亿 "
        f"按成交额从大到小排列"
    )


def _field(row: Mapping, *fragments: str):
    """Column names carry the trade date, so match on a fragment."""
    for key in row:
        for frag in fragments:
            if frag in str(key):
                return row[key]
    return None


def parse_screen_rows(rows: Iterable[Mapping]) -> list[dict]:
    """Normalise screener output into typed candidates."""
    out = []
    for r in rows:
        code = str(_field(r, "代码") or "").strip().zfill(6)
        if not code.isdigit():
            continue
        turnover = parse_cn_number(_field(r, "成交额"))
        flow = parse_cn_number(_field(r, "超大单净额", "超大单净流入"))
        chg = parse_pct(_field(r, "涨跌幅"))
        if turnover is None or flow is None or turnover <= 0:
            continue
        out.append({
            "code": code,
            "name": str(_field(r, "名称") or "").strip(),
            "turnover": turnover,
            "whale_flow": flow,
            "flow_ratio": flow / turnover,
            "change_pct": chg if chg is not None else 0.0,
        })
    return out


def early_turn(history: Sequence[Mapping], window: int = SUSTAIN_WINDOW) -> dict:
    """Has money just STARTED arriving, or has it been arriving for a while?

    history: newest-first rows carrying 'whale_flow' and optionally 'change_pct'.

    Three conditions, all necessary:

      the recent window turned positive   - money is arriving now
      on more than one session            - a campaign, not a block trade
      the prior window was not already    - we are near the start of it,
        accumulating                        not near the end

    The last condition is what the previous version had backwards. Requiring a
    long run of positive sessions selected campaigns that were nearly complete.
    """
    rows = [h for h in history if h.get("whale_flow") is not None][:window]
    need = RECENT_DAYS + MIN_RECENT_POSITIVE
    if len(rows) < need:
        return {"early": False, "sessions": len(rows), "recent_positive": 0,
                "reason": f"only {len(rows)} sessions, need {need}"}

    flows = [float(h["whale_flow"]) for h in rows]
    recent, prior = flows[:RECENT_DAYS], flows[RECENT_DAYS:RECENT_DAYS + PRIOR_DAYS]
    recent_pos = sum(1 for f in recent if f > 0)
    prior_pos_ratio = (sum(1 for f in prior if f > 0) / len(prior)) if prior else 0.0
    recent_mean = sum(recent) / len(recent)
    prior_mean = (sum(prior) / len(prior)) if prior else 0.0
    turn = recent_mean - prior_mean

    if recent_pos < MIN_RECENT_POSITIVE:
        ok, why = False, (f"only {recent_pos}/{len(recent)} recent sessions positive"
                          f" - a single print, not a campaign")
    elif prior_pos_ratio > MAX_PRIOR_POSITIVE:
        ok, why = False, (f"prior window already {prior_pos_ratio:.0%} positive"
                          f" - the campaign is mature, this is its exit liquidity")
    elif turn <= 0:
        ok, why = False, "recent flow is not above the prior window - no turn"
    else:
        ok, why = True, (f"turn: {recent_pos}/{len(recent)} recent positive against"
                         f" a prior window {prior_pos_ratio:.0%} positive")

    return {
        "early": ok,
        "sessions": len(rows),
        "recent_positive": recent_pos,
        "prior_positive_ratio": round(prior_pos_ratio, 3),
        "turn": turn,
        "reason": why,
    }


def price_run_pct(history: Sequence[Mapping], window: int = SUSTAIN_WINDOW) -> float:
    """Cumulative move over the window. A big run means the wake, not the whale."""
    rows = [h for h in history if h.get("change_pct") is not None][:window]
    total = 0.0
    for h in rows:
        total += float(h["change_pct"])
    return total


def evaluate(candidate: Mapping, history: Sequence[Mapping]) -> dict:
    """Judge one candidate. Always returns a reason, including when it declines."""
    result = dict(candidate)
    acc = early_turn(history)
    run = price_run_pct(history)
    result.update({
        "recent_positive": acc["recent_positive"],
        "prior_positive_ratio": acc.get("prior_positive_ratio"),
        "sessions": acc["sessions"],
        "price_run_pct": round(run, 2),
    })
    if candidate.get("turnover", 0) < MIN_TURNOVER_YUAN:
        result.update(selected=False, reason="turnover below the deep-water floor")
    elif candidate.get("flow_ratio", 0) < MIN_FLOW_RATIO:
        result.update(selected=False,
                      reason=f"flow ratio {candidate.get('flow_ratio', 0):.3f} "
                             f"below {MIN_FLOW_RATIO}")
    elif not acc["early"]:
        result.update(selected=False, reason=acc["reason"])
    elif run > MAX_PRICE_RUN_PCT:
        result.update(selected=False,
                      reason=f"already run {run:+.1f}% over the window - this is "
                             f"the wake, not the whale")
    else:
        result.update(selected=True, reason=acc["reason"])
    return result


def rank(candidates: Iterable[Mapping]) -> list[dict]:
    """Selected first, then by sustained flow relative to turnover."""
    sel = [c for c in candidates if c.get("selected")]
    sel.sort(key=lambda c: (-c.get("flow_ratio", 0.0), -c.get("turnover", 0.0)))
    return sel


def theme_concentration(candidates: Sequence[Mapping],
                        theme_of: Callable[[str], str | None]) -> list[tuple]:
    """How concentrated is the money? One theme dominating is the school.

    theme_of maps a code to a concept or industry label. On 2026-08-27 the top
    thirteen names by turnover were all AI hardware - optical/CPO, memory,
    PCB-CCL, AI chips, AI servers - which is the signal itself, not a curiosity.
    """
    tally: dict[str, float] = {}
    for c in candidates:
        t = theme_of(c.get("code", "")) or "unknown"
        tally[t] = tally.get(t, 0.0) + float(c.get("turnover", 0.0))
    total = sum(tally.values()) or 1.0
    return sorted(((t, v, v / total) for t, v in tally.items()),
                  key=lambda x: -x[1])
