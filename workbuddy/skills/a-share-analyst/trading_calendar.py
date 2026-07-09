#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A-share trading calendar helpers for automation and observatory updates.

Current project reality:
- `pytdx` is used for行情/指数抓取, not as an authoritative trading calendar.
- The current `MX` integration in this repo points to mock trading endpoints, not
  the Eastmoney/EMQuant calendar API.

So this module uses the official SSE/SZSE holiday schedule as the stable default
calendar source for 2025-2026, which covers the current operating window.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Iterable


MARKET_CLOSE_TIME = time(hour=15, minute=0)
WORKBUDDY_SOURCE_READY_TIME = time(hour=15, minute=20)
CALENDAR_SOURCE = "official_sse_szse_schedule_embedded_2025_2026"

# SSE/SZSE are closed on weekends and the following public-holiday market
# closures. Weekend make-up workdays are still closed for the stock market, so
# they do not need special "open weekend" handling here.
MARKET_HOLIDAYS = {
    "2025-01-01",
    "2025-01-28",
    "2025-01-29",
    "2025-01-30",
    "2025-01-31",
    "2025-02-03",
    "2025-02-04",
    "2025-04-04",
    "2025-05-01",
    "2025-05-02",
    "2025-05-05",
    "2025-06-02",
    "2025-10-01",
    "2025-10-02",
    "2025-10-03",
    "2025-10-06",
    "2025-10-07",
    "2025-10-08",
    "2026-01-01",
    "2026-01-02",
    "2026-02-16",
    "2026-02-17",
    "2026-02-18",
    "2026-02-19",
    "2026-02-20",
    "2026-02-23",
    "2026-04-06",
    "2026-05-01",
    "2026-05-04",
    "2026-05-05",
    "2026-06-19",
    "2026-09-25",
    "2026-09-28",
    "2026-10-01",
    "2026-10-02",
    "2026-10-05",
    "2026-10-06",
    "2026-10-07",
}


def _coerce_date(value: date | datetime | str | None) -> date:
    if value is None:
        return datetime.now().date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return datetime.now().date()
    return datetime.strptime(text[:10], "%Y-%m-%d").date()


def iter_trading_days(start_date: date | datetime | str, end_date: date | datetime | str) -> Iterable[date]:
    current = _coerce_date(start_date)
    end = _coerce_date(end_date)
    while current <= end:
        if is_trading_day(current):
            yield current
        current += timedelta(days=1)


def is_trading_day(value: date | datetime | str | None = None) -> bool:
    current = _coerce_date(value)
    return current.weekday() < 5 and current.isoformat() not in MARKET_HOLIDAYS


def previous_trading_day(value: date | datetime | str | None = None, *, include_self: bool = False) -> date:
    current = _coerce_date(value)
    if include_self and is_trading_day(current):
        return current
    probe = current - timedelta(days=0 if not include_self else 1)
    if not include_self:
        probe = current - timedelta(days=1)
    while not is_trading_day(probe):
        probe -= timedelta(days=1)
    return probe


def next_trading_day(value: date | datetime | str | None = None, *, include_self: bool = False) -> date:
    current = _coerce_date(value)
    if include_self and is_trading_day(current):
        return current
    probe = current + timedelta(days=0 if not include_self else 1)
    if not include_self:
        probe = current + timedelta(days=1)
    while not is_trading_day(probe):
        probe += timedelta(days=1)
    return probe


def latest_completed_trading_day(value: datetime | date | str | None = None) -> date:
    if value is None:
        current_dt = datetime.now()
    elif isinstance(value, datetime):
        current_dt = value
    else:
        current_dt = datetime.combine(_coerce_date(value), MARKET_CLOSE_TIME)

    today = current_dt.date()
    if is_trading_day(today) and current_dt.time() >= MARKET_CLOSE_TIME:
        return today
    return previous_trading_day(today)


def latest_workbuddy_source_trade_date(value: datetime | date | str | None = None) -> date:
    if value is None:
        current_dt = datetime.now()
    elif isinstance(value, datetime):
        current_dt = value
    else:
        current_dt = datetime.combine(_coerce_date(value), WORKBUDDY_SOURCE_READY_TIME)

    today = current_dt.date()
    if is_trading_day(today) and current_dt.time() >= WORKBUDDY_SOURCE_READY_TIME:
        return today
    return previous_trading_day(today)


def trading_week_id(value: date | datetime | str | None = None) -> tuple[int, int]:
    current = _coerce_date(value)
    iso_year, iso_week, _ = current.isocalendar()
    return iso_year, iso_week


def calendar_context(value: datetime | date | str | None = None) -> dict:
    latest_trade_day = latest_completed_trading_day(value)
    latest_workbuddy_day = latest_workbuddy_source_trade_date(value)
    iso_year, iso_week = trading_week_id(latest_trade_day)
    return {
        "calendar_source": CALENDAR_SOURCE,
        "latest_completed_trading_day": latest_trade_day.isoformat(),
        "latest_workbuddy_source_trade_date": latest_workbuddy_day.isoformat(),
        "archive_iso_year": iso_year,
        "archive_iso_week": iso_week,
        "is_today_trading_day": is_trading_day(_coerce_date(value)),
    }
