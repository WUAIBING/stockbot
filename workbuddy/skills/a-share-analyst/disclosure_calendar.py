#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Position ahead of a date the exchange published in advance.

Every other selection rule in this repo reads price and volume. This one reads a
CALENDAR: 定期报告预计披露日期, the date each listed company has told the
exchange it will report. It is public, it is forward, and nothing in
v10_moni_trader.py or scanner_v10.py has ever looked at it - a grep for 披露,
业绩预告, 财报 or earnings across both files returns zero.

WHAT WAS MEASURED

24,773 disclosure events over 272 distinct dates, 2017-2026, excess over the
same sessions equal-weighted universe, aggregated per DATE because 200 companies
reporting on one deadline day are not 200 independent draws:

    window     mean excess      t
    -3            +0.451%    +2.95
    -5            +0.541%    +2.79
    -10           +0.326%    +1.26
    -20           +0.031%    +0.10
    event day     -0.106%    -1.31

The drift is real and it is SMALL, and it lives entirely in the last three to
five sessions. The event day itself is flat to negative: whatever gets priced,
gets priced before the report lands, which is why this module aims at the run-up
and has nothing to say about holding through the announcement.

WHY THE PLACEBO IS THE WHOLE ARGUMENT

Same stocks, same window length, anchor moved 40-60 sessions off the event:

    shift        K=3        K=5       K=10       K=20
    true     +0.45/2.9  +0.54/2.8  +0.33/1.3  +0.03/0.1
    -60      +0.12/0.5  +0.24/0.8  -0.01/0.0  -0.06/0.1
    -40      -0.00/0.0  -0.09/0.8  -0.19/1.0  -0.49/1.7
    +40      -0.10/0.9  -0.18/1.2  -0.36/1.7  -0.87/2.5
    +60      +0.09/0.7  +0.06/0.3  +0.16/0.7  +0.33/0.9

The true anchor beats every placebo at every window. That test is here because
it KILLED the neighbouring ideas the same day it passed this one. Earnings
pre-announcements (业绩预告, 12,803 events) looked tradeable at face value -
预减 ran -1.34% over five sessions with t=-4.14 - until the placebo showed the
same cohort falling -1.87% (t=-5.93) sixty sessions EARLIER. Those were simply
declining companies; the announcement added nothing. A scheduled industry
conference failed the same way: four World Robot Conferences averaged +0.63%
(t=0.95) into the event while the -60 placebo returned +2.16% (t=3.16).

So: measure against a shifted anchor, or you are measuring what the stock was
already doing.

WHAT THIS IS WORTH, STATED PLAINLY

+0.45% over three sessions is roughly +0.30% net of a 0.15% round trip. That is
a TILT, not a book. Sized as a standalone strategy it would be swamped by the
noise it trades in; used to break ties between candidates the mainline already
likes, it is free money on information nothing else in the system reads. The
reporting calendar is also seasonal - April, July-August and October - so this
sits idle most of the year by construction.

    board       mean%      t    dates
    STAR 688   +1.242   +1.57      87
    ChiNext    +0.566   +1.65     189
    SH main    +0.344   +1.28     190
    SZ main    +0.335   +1.26     217

The tech boards read stronger, which is what a +/-20% daily band would predict,
but 87 dates cannot carry that claim. Do not gate on it.

THE ONE THING THAT CANNOT BE BACKTESTED

预计披露日期 is a LIVE SNAPSHOT. Query it for a past period and the API returns
stockCount: 0 - the field rolls forward the moment a company reports. So the
measurement above necessarily used 实际披露日期, the date companies actually
filed, while live trading can only use the scheduled one. Companies that defer
do so because the numbers are bad, and that difference is not in the backtest.
It is why _schedule_slipped exists and why a stale scheduled date is dropped
rather than carried: a date that has come and gone without a report is evidence,
and the evidence is bad.

WHAT IT DELIBERATELY WILL NOT DO

It selects. It does not size, order or trade, and a test asserts it holds no
execution path.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

# The measured window. -3 and -5 both clear their placebos; -10 does not, and
# -20 is indistinguishable from zero. Entering earlier than this is not "more
# exposure to the same edge", it is exposure to a window where the edge is not
# there.
ENTRY_SESSIONS_BEFORE = 5
EXIT_SESSIONS_BEFORE = 1          # out the session before the report lands

# The event day ran -0.106% (t=-1.31) across 241 dates. Holding through the
# announcement is a different trade with a worse measured outcome, so the
# module refuses to describe one.
HOLD_THROUGH_EVENT = False

# Matches the liquidity floor the measurement used. Below this the excess is not
# reachable: you cannot fill into it.
MIN_TURNOVER_YUAN = 2.0e7         # 2000万

# A scheduled date already this many sessions in the past, with no report filed,
# is a deferral rather than a calendar entry.
MAX_SCHEDULE_SLIP_SESSIONS = 1


def _as_int_date(value) -> int | None:
    """'2026-08-31' or '2026.08.31' or 20260831 -> 20260831."""
    if value is None:
        return None
    if isinstance(value, int):
        return value if 19900101 <= value <= 21001231 else None
    s = str(value).strip()
    if not s or s in {"-", "--", "None"}:
        return None
    s = s.split("|")[0].split(" ")[0]
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        d = int(digits[:8])
    except ValueError:
        return None
    return d if 19900101 <= d <= 21001231 else None


def _field(row: Mapping, *fragments: str):
    """Column names carry a period suffix, so match on a fragment."""
    for key in row:
        for frag in fragments:
            if frag in str(key):
                return row[key]
    return None


def parse_cn_number(text) -> float | None:
    """'11.31亿' -> 1.131e9. The screener returns human units."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().replace(",", "").replace("元", "")
    if not s or s in {"-", "--", "None"}:
        return None
    for suffix, mult in (("万亿", 1.0e12), ("亿", 1.0e8), ("万", 1.0e4)):
        if s.endswith(suffix):
            try:
                return float(s[: -len(suffix)]) * mult
            except ValueError:
                return None
    try:
        return float(s)
    except ValueError:
        return None


def calendar_query(start: int, end: int) -> str:
    """The forward screen. Dates are ints like 20260828."""
    def fmt(d):
        return "%d年%d月%d日" % (d // 10000, (d // 100) % 100, d % 100)
    return ("定期报告预计披露日期在%s到%s之间的A股" % (fmt(start), fmt(end)))


def parse_calendar_rows(rows: Iterable[Mapping]) -> list[dict]:
    """Screener output -> typed calendar entries, dropping what cannot be used."""
    out = []
    for r in rows:
        code = str(_field(r, "代码") or "").strip().zfill(6)
        if not code.isdigit() or len(code) != 6:
            continue
        scheduled = _as_int_date(_field(r, "预计披露日期", "披露日期"))
        if scheduled is None:
            continue
        out.append({
            "code": code,
            "name": str(_field(r, "名称") or "").strip(),
            "scheduled": scheduled,
            "turnover": parse_cn_number(_field(r, "成交额")),
        })
    return out


def sessions_until(scheduled: int, today: int,
                   trading_dates: Sequence[int]) -> int | None:
    """Trading sessions from today to the scheduled date.

    Counted in SESSIONS, not calendar days. A report scheduled the Tuesday after
    a week-long holiday is two sessions away, not nine days, and an entry rule
    written in calendar days would be early by most of a week.

    Negative means the scheduled date has already passed.
    """
    if not trading_dates:
        return None
    i = _index_at_or_after(trading_dates, today)
    j = _index_at_or_after(trading_dates, scheduled)
    if i is None or j is None:
        return None
    return j - i


def _index_at_or_after(dates: Sequence[int], d: int) -> int | None:
    lo, hi = 0, len(dates)
    while lo < hi:
        mid = (lo + hi) // 2
        if dates[mid] < d:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(dates) else None


def _schedule_slipped(sessions: int) -> bool:
    """The date came and went with no report: a deferral, not a calendar entry.

    This is the one failure mode the backtest could not see, because it measured
    actual filing dates while live trading only has scheduled ones.
    """
    return sessions < -MAX_SCHEDULE_SLIP_SESSIONS


def evaluate(entry: Mapping, today: int,
             trading_dates: Sequence[int]) -> dict:
    """Judge one calendar entry. Always returns a reason, including when it declines."""
    result = dict(entry)
    scheduled = entry.get("scheduled")
    sessions = (sessions_until(scheduled, today, trading_dates)
                if scheduled is not None else None)
    result["sessions_until"] = sessions

    turnover = entry.get("turnover")
    if sessions is None:
        result.update(selected=False, reason="scheduled date not on the trading calendar")
    elif _schedule_slipped(sessions):
        result.update(selected=False,
                      reason="scheduled %d sessions ago with no report - deferred, "
                             "and companies defer when the numbers are bad" % (-sessions))
    elif sessions > ENTRY_SESSIONS_BEFORE:
        result.update(selected=False,
                      reason="%d sessions out, beyond the %d-session window where the "
                             "drift was measured" % (sessions, ENTRY_SESSIONS_BEFORE))
    elif sessions < EXIT_SESSIONS_BEFORE:
        result.update(selected=False,
                      reason="inside %d sessions - the event day itself measured "
                             "-0.11%% and is not this trade" % EXIT_SESSIONS_BEFORE)
    elif turnover is not None and turnover < MIN_TURNOVER_YUAN:
        result.update(selected=False,
                      reason="turnover below the %.0f万 floor the measurement used"
                             % (MIN_TURNOVER_YUAN / 1e4))
    else:
        result.update(selected=True,
                      reason="reports in %d sessions; measured +0.45%% to +0.54%% "
                             "excess over this window" % sessions)
    return result


def rank(candidates: Iterable[Mapping]) -> list[dict]:
    """Selected only, nearest report first, then by turnover.

    Nearest-first because the drift concentrates in the last three sessions: a
    name reporting in two is further into the measured window than one reporting
    in five.
    """
    sel = [c for c in candidates if c.get("selected")]
    sel.sort(key=lambda c: (c.get("sessions_until", 99),
                            -(c.get("turnover") or 0.0)))
    return sel


def tilt_weight(sessions_until_report: int | None) -> float:
    """A multiplier for an existing candidate score, never a standalone signal.

    +0.45% over three sessions cannot carry a book of its own. It can break a tie
    between two names the mainline already likes, which is the only use the
    measured size supports.
    """
    if sessions_until_report is None:
        return 1.0
    if _schedule_slipped(sessions_until_report):
        return 1.0
    if EXIT_SESSIONS_BEFORE <= sessions_until_report <= ENTRY_SESSIONS_BEFORE:
        return 1.05
    return 1.0
