#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Merging to master deploys; it must never deploy into a trading session."""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts" / "github-actions"))

import deploy_window as dw  # noqa: E402

CHINA = timezone(timedelta(hours=8))


def at(y, m, d, hh, mm, tz=CHINA):
    return datetime(y, m, d, hh, mm, tzinfo=tz)


def always(_day):
    return True


def never(_day):
    return False


class WindowTests(unittest.TestCase):
    def test_mid_session_waits_until_1505(self):
        self.assertEqual(dw.seconds_until_safe(at(2026, 10, 8, 10, 30), always), 4 * 3600 + 35 * 60 + 1)

    def test_lunch_is_still_mid_session(self):
        """Afternoon phases import fresh modules at 13:00 - lunch is not a gap."""
        self.assertGreater(dw.seconds_until_safe(at(2026, 10, 8, 12, 0), always), 0)

    def test_after_the_close_deploys_now(self):
        self.assertEqual(dw.seconds_until_safe(at(2026, 10, 8, 15, 5), always), 0)
        self.assertEqual(dw.seconds_until_safe(at(2026, 10, 8, 21, 31), always), 0)

    def test_before_the_open_deploys_now(self):
        self.assertEqual(dw.seconds_until_safe(at(2026, 10, 8, 8, 59), always), 0)

    def test_0900_is_already_closed(self):
        self.assertGreater(dw.seconds_until_safe(at(2026, 10, 8, 9, 0), always), 0)

    def test_a_holiday_deploys_now(self):
        self.assertEqual(dw.seconds_until_safe(at(2026, 10, 1, 10, 30), never), 0)

    def test_utc_input_is_judged_in_china_time(self):
        """The runner's clock is UTC. 02:30 UTC is 10:30 China - mid-session."""
        utc = datetime(2026, 10, 8, 2, 30, tzinfo=timezone.utc)
        self.assertGreater(dw.seconds_until_safe(utc, always), 0)

    def test_a_broken_calendar_assumes_a_weekday_trades(self):
        def broken(_day):
            raise RuntimeError("no table")
        self.assertGreater(dw.seconds_until_safe(at(2026, 10, 8, 10, 30), broken), 0)   # Thursday
        self.assertEqual(dw.seconds_until_safe(at(2026, 10, 10, 10, 30), broken), 0)     # Saturday


class RealCalendarTests(unittest.TestCase):
    """The checkout's own calendar, as the workflow loads it."""

    def setUp(self):
        self.is_trading_day, self.source = dw.load_calendar()

    def test_the_real_calendar_loads(self):
        self.assertEqual(self.source, "trading_calendar")

    def test_first_day_back_from_national_day_waits(self):
        self.assertTrue(self.is_trading_day(date(2026, 10, 8)))
        self.assertGreater(dw.seconds_until_safe(at(2026, 10, 8, 10, 30), self.is_trading_day), 0)

    def test_national_day_deploys_now(self):
        self.assertEqual(dw.seconds_until_safe(at(2026, 10, 5, 10, 30), self.is_trading_day), 0)


class WorkflowTests(unittest.TestCase):
    def src(self):
        return (ROOT / ".github" / "workflows" / "sync-do-repo.yml").read_text(encoding="utf-8")

    def test_the_wait_comes_before_the_sync(self):
        s = self.src()
        self.assertLess(s.index("Wait for the market to close"), s.index("Sync checkout to /opt/stockbot"))

    def test_the_push_trigger_cannot_skip_the_wait(self):
        s = self.src()
        i = s.index("Wait for the market to close")
        guard = s[i:i + 200]
        self.assertIn("dry_run != 'true'", guard)
        self.assertIn("deploy_now != 'true'", guard)

    def test_the_job_can_outlast_a_full_session(self):
        import re
        minutes = int(re.search(r"timeout-minutes:\s*(\d+)", self.src()).group(1))
        self.assertGreaterEqual(minutes, 380)


if __name__ == "__main__":
    unittest.main()
