#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sell at the open the positions whose exit was already decided yesterday.

WHAT WAS MEASURED

On the day an exit happens, the price the account actually gets is -1.90%
below that day's OPEN (n=122, t=-8.15). The value does not bleed away slowly:

    filled 09:45          n=39   -1.94%   t=-6.91
    filled 10:00-11:30    n=59   -2.15%   t=-6.90
    filled 13:00+         n=24   -1.24%   t=-1.57

Flat. Fills fifteen minutes after the open are already down 1.94%, so running
the sweep earlier recovers nothing - the gap closes in the first minutes.

WHY THIS IS NARROW ON PURPOSE

The tempting version of this - "take the gap-up open whenever we are above
cost" - was measured and LOSES: -1.85pp at any gap (t=-1.68), worsening to
-6.35pp at a +5% trigger (t=-3.47), where every single such trade had been a
winner. 64% of gap-up opens do fade, giving back -2.66pp, but the 36% that run
gain +10.00pp and carry the whole 34.7%-win / 3.4:1 arithmetic. Capping them is
how a profitable book is turned into a losing one while being right most days.

So this module never decides to exit. It only moves the EXECUTION of an exit
that is already determined, where "already determined" means determined by
information available at yesterday's close - no signal from today, no hindsight.
Today's exits fire on intraday shapes (冲高回落上影线4.6%), and a signal cannot
be acted on before the thing it measures exists.

The predetermined slice measures on its own:

    exits at T+10 or beyond   n=19   fill -2.31% below open   t=-4.29

Those are the MAX_HOLD_DAYS exits. At yesterday's close the system already
knows the limit trips today, so a market-on-open order carries no hindsight.

THIS MODULE PLACES NO ORDERS. It returns a list; the executor acts on it.
Gated on TLFZ_OPEN_EXIT, off by default.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

OPEN_EXIT_ENABLED = str(
    os.environ.get("TLFZ_OPEN_EXIT", "0")).strip().lower() in ("1", "true", "yes", "on")

# Reasons an exit can be known before the open. Every one of these must be
# derivable from data that exists at yesterday's close.
REASON_MAX_HOLD_DUE = "max_hold_due_today"
REASON_FLAGGED_AT_CLOSE = "exit_flagged_at_close"

VALID_REASONS = (REASON_MAX_HOLD_DUE, REASON_FLAGGED_AT_CLOSE)


def exit_is_predetermined(hold_sessions, max_hold_days):
    """True when today's session will trip the hold limit.

    Evaluated at yesterday's close: a position that has held max_hold-1
    sessions reaches the limit on the next one, so the exit is already
    decided and does not depend on anything today does.
    """
    try:
        h = int(hold_sessions)
        m = int(max_hold_days)
    except (TypeError, ValueError):
        return False
    if m <= 0:
        return False
    return h >= m - 1


def build_precommit(positions, hold_sessions_by_code, max_hold_days,
                    trade_date, flagged_codes=()):
    """The list to sell at tomorrow's open, built at tonight's close.

    `trade_date` is the session the list is FOR - tomorrow - not today. A list
    is only ever valid for one session (see load_precommit), because a position
    that should have been sold at yesterday's open must be re-decided rather
    than sold a day late on stale reasoning.
    """
    flagged = {str(c).zfill(6) for c in (flagged_codes or ())}
    out = []
    for pos in positions or []:
        code = str(pos.get("code", "")).zfill(6)
        if not code:
            continue
        qty = pos.get("count")
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        reason = None
        if code in flagged:
            reason = REASON_FLAGGED_AT_CLOSE
        elif exit_is_predetermined(hold_sessions_by_code.get(code), max_hold_days):
            reason = REASON_MAX_HOLD_DUE
        if reason:
            out.append({
                "code": code,
                "name": pos.get("name", ""),
                "quantity": qty,
                "reason": reason,
                "hold_sessions_at_close": hold_sessions_by_code.get(code),
            })
    return {
        "trade_date": str(trade_date or "").strip(),
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "max_hold_days": max_hold_days,
        "entries": out,
    }


def save_precommit(payload, path):
    tmp = "%s.tmp" % path
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)
    return path


def load_precommit(path, trade_date):
    """The list, but only if it was built FOR this session.

    A stale list is the dangerous failure here: if close-node does not run, or
    the process dies overnight, yesterday's file is still sitting there. Selling
    from it at today's open would execute a decision whose reasoning has since
    expired - the position may have been sold already, or the hold count may no
    longer be what it was. So the date must match exactly; a mismatch yields
    nothing rather than something plausible.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("trade_date") or "").strip() != str(trade_date or "").strip():
        return None
    return payload


def should_sell_at_open(code, precommit, tradable_codes=None):
    """(sell?, reason). Every gate that can refuse is checked here.

    tradable_codes is the 09:31 gate's verdict. A halted name must never be
    ordered - 688432 有研硅 has been suspended since 2026-08-31 with no reopen
    date, sits in excluded_today_codes, and would otherwise be a standing
    market-on-open order against a stock that cannot trade.
    """
    if not OPEN_EXIT_ENABLED:
        return False, "open exit disabled"
    if not precommit:
        return False, "no precommit list for this session"
    code = str(code or "").zfill(6)
    entry = None
    for e in precommit.get("entries") or []:
        if str(e.get("code", "")).zfill(6) == code:
            entry = e
            break
    if entry is None:
        return False, "not precommitted"
    if entry.get("reason") not in VALID_REASONS:
        return False, "unrecognised reason %r" % entry.get("reason")
    if tradable_codes is not None and code not in {
            str(c).zfill(6) for c in tradable_codes}:
        return False, "not tradable today"
    return True, entry["reason"]


def precommitted_codes(precommit):
    if not precommit:
        return []
    return [str(e.get("code", "")).zfill(6) for e in (precommit.get("entries") or [])]
