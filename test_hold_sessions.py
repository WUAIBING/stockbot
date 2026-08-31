#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The T+N exit must count trading sessions, not calendar days.

hold_days is `(datetime.now() - buy_dt).days`, so weekends, holidays and
suspensions all age a position that was never holdable. Real evidence from the
episode history:

    000543 皖能电力  bought Fri 2026-07-17, sold Tue 2026-07-21
                     hold_days recorded 3, actual sessions 2

That matters because MAX_HOLD_DAYS is 10 and the study that chose 10 measured
TRADING sessions - +5.60pp/yr at ten against +3.80 at five and -0.75 at three.
Ten calendar days spanning two weekends is about seven sessions, so the exit has
been firing roughly a third early on every position since long before any halt
existed.

688432 有研硅 makes it concrete. Bought 2026-08-28 and suspended from
2026-08-31 for a 重大资产重组, its calendar clock keeps running through a halt
it cannot trade out of, and the T+10 exit would come due around 2026-09-11 into
a stock that cannot fill the order.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import trading_halt as th  # noqa: E402


class WeekendTests(unittest.TestCase):
    def test_the_000543_case(self):
        """Friday to Tuesday is two sessions; calendar arithmetic said three."""
        self.assertEqual(th.sessions_held("2026-07-17", "2026-07-21"), 2)

    def test_a_weekend_adds_no_sessions(self):
        """Friday to Monday is one session, not three days."""
        self.assertEqual(th.sessions_held("2026-08-28", "2026-08-31"), 1)

    def test_a_plain_week_counts_normally(self):
        self.assertEqual(th.sessions_held("2026-08-24", "2026-08-28"), 4)

    def test_same_day_is_zero(self):
        self.assertEqual(th.sessions_held("2026-08-28", "2026-08-28"), 0)

    def test_a_date_before_the_buy_is_zero_not_negative(self):
        self.assertEqual(th.sessions_held("2026-08-28", "2026-08-27"), 0)


class HolidayTests(unittest.TestCase):
    def test_national_day_is_skipped(self):
        """30 Sep to 8 Oct 2026 is eight calendar days and one session."""
        self.assertEqual(th.sessions_held("2026-09-30", "2026-10-08"), 1)

    def test_mid_autumn_is_skipped(self):
        """25-27 Sep 2026, right before the National Day break."""
        self.assertEqual(th.sessions_held("2026-09-24", "2026-09-28"), 1)

    def test_qingming_is_skipped(self):
        """April sits in annual-report season."""
        self.assertEqual(th.sessions_held("2026-04-03", "2026-04-07"), 1)

    def test_a_weekday_holiday_is_not_a_session(self):
        self.assertFalse(th.is_trading_session("2026-10-01"))
        self.assertTrue(th.is_trading_session("2026-10-08"))


class HaltTests(unittest.TestCase):
    """688432 有研硅, suspended from 2026-08-31."""

    def test_a_halted_session_does_not_age_the_position(self):
        self.assertEqual(th.sessions_held("2026-08-28", "2026-08-31"), 1)
        self.assertEqual(
            th.sessions_held("2026-08-28", "2026-08-31",
                             halted_dates=["2026-08-31"]), 0)

    def test_a_multi_session_halt_freezes_the_clock(self):
        """The announcement expects up to five sessions."""
        halt = ["2026-08-31", "2026-09-01", "2026-09-02",
                "2026-09-03", "2026-09-04"]
        self.assertEqual(th.sessions_held("2026-08-28", "2026-09-04"), 5)
        self.assertEqual(
            th.sessions_held("2026-08-28", "2026-09-04", halted_dates=halt), 0)

    def test_sessions_resume_after_the_halt_ends(self):
        halt = ["2026-08-31", "2026-09-01"]
        self.assertEqual(
            th.sessions_held("2026-08-28", "2026-09-03", halted_dates=halt), 2)

    def test_a_halt_cannot_push_the_count_negative(self):
        halt = ["2026-08-31", "2026-09-01", "2026-09-02"]
        self.assertEqual(
            th.sessions_held("2026-08-28", "2026-08-31", halted_dates=halt), 0)

    def test_the_t10_exit_would_have_fired_early_without_this(self):
        """Bought 08-28: calendar reaches 10 well before ten sessions do."""
        import datetime as dt
        buy = dt.date(2026, 8, 28)
        as_of = buy + dt.timedelta(days=10)          # calendar T+10
        self.assertLess(th.sessions_held(buy, as_of), 10)


class RefusalTests(unittest.TestCase):
    """A wrong session count moves a real exit, so it declines to guess."""

    def test_outside_the_verified_holiday_table_returns_none(self):
        self.assertIsNone(th.sessions_held("2027-01-05", "2027-01-12"))

    def test_a_missing_date_returns_none(self):
        self.assertIsNone(th.sessions_held("", "2026-08-31"))
        self.assertIsNone(th.sessions_held("2026-08-28", None))

    def test_a_malformed_date_returns_none(self):
        self.assertIsNone(th.sessions_held("not-a-date", "2026-08-31"))

    def test_slashes_are_accepted(self):
        """Announcements write 2026/8/31."""
        self.assertEqual(th.sessions_held("2026-08-28", "2026/08/31"), 1)


class WiringTests(unittest.TestCase):
    SRC = (SKILL / "v10_moni_trader.py").read_text(encoding="utf-8")

    def test_the_exit_uses_the_session_count(self):
        self.assertIn("hold_days_for_exit >= MAX_HOLD_DAYS", self.SRC)

    def test_it_falls_back_to_calendar_days_when_unknown(self):
        """None means not knowable; the old behaviour is kept rather than guessed."""
        self.assertIn(
            "hold_days if hold_sessions is None else hold_sessions", self.SRC)

    def test_halted_sessions_are_recorded_when_the_gate_is_read(self):
        self.assertIn("_record_halted_sessions(", self.SRC)

    def test_the_exit_reason_shows_both_numbers(self):
        """So a shortened or lengthened hold is visible in the log, not silent."""
        self.assertIn("交易日", self.SRC)

    def test_only_the_exit_gate_changed(self):
        """hold_days still feeds the learning classifications unchanged; moving
        those too would shift false_selection thresholds without measurement."""
        self.assertIn("hold_days = (datetime.now() - buy_dt).days", self.SRC)


if __name__ == "__main__":
    unittest.main()
