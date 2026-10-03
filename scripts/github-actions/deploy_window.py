#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""How long to wait before deploying into the live runtime. Prints seconds; 0 = now.

WHY THIS EXISTS

Every push to master runs sync-do-repo.yml on the droplet's self-hosted runner,
which rsyncs the repository over /opt/stockbot. Merging is deploying - and
nothing on that path looked at the clock. A merge at 10:30 China time would
have swapped code under a running trading day with nobody touching the server.
"Deploy after the close" was a rule in a notes file; this makes it the path.

On a trading day the window 09:00-15:05 China time is closed: the trading-day
service starts at 09:25 and runs phases until 15:06, each phase importing the
modules fresh. Outside it, or on a non-trading day, the answer is 0.

The calendar is the one in the checkout being deployed, so a newly published
holiday table takes effect with the same push. If it cannot be loaded, every
weekday is treated as a trading day - waiting a few hours on a holiday is cheap,
deploying into a session is not.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

CHINA = timezone(timedelta(hours=8))
CLOSED_FROM = time(9, 0)
CLOSED_UNTIL = time(15, 5)


def _weekday_calendar(day: date) -> bool:
    return day.weekday() < 5


def load_calendar(skill_dir=None):
    """The checkout's is_trading_day, or a conservative weekday rule."""
    skill_dir = Path(skill_dir) if skill_dir else (
        Path(__file__).resolve().parents[2] / "workbuddy" / "skills" / "a-share-analyst")
    try:
        sys.path.insert(0, str(skill_dir))
        import trading_calendar  # noqa: WPS433
        return trading_calendar.is_trading_day, "trading_calendar"
    except Exception:
        return _weekday_calendar, "weekday fallback"


def seconds_until_safe(now: datetime, is_trading_day) -> int:
    """0 if a deploy is safe now, else seconds until 15:05 China time."""
    now = now.astimezone(CHINA)
    try:
        trading = bool(is_trading_day(now.date()))
    except Exception:
        trading = _weekday_calendar(now.date())
    if not trading:
        return 0
    start = datetime.combine(now.date(), CLOSED_FROM, tzinfo=CHINA)
    end = datetime.combine(now.date(), CLOSED_UNTIL, tzinfo=CHINA)
    if start <= now < end:
        return int((end - now).total_seconds()) + 1
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    is_trading_day, source = load_calendar(argv[0] if argv else None)
    wait = seconds_until_safe(datetime.now(CHINA), is_trading_day)
    print(wait)
    print("deploy window: %s, calendar=%s, wait=%ds" % (
        "open" if wait == 0 else "closed until 15:05 China", source, wait), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
