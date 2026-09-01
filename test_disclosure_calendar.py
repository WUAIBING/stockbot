#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Selection from the exchange disclosure calendar.

The measurement behind the module: 24,773 disclosure events over 272 dates,
2017-2026, excess over the same sessions universe, aggregated per date.

    window     mean excess      t        placebo range (4 shifts)
    -3            +0.451%    +2.95       -0.00 to +0.12,  |t| <= 0.5
    -5            +0.541%    +2.79       -0.18 to +0.24,  |t| <= 1.2
    -10           +0.326%    +1.26
    -20           +0.031%    +0.10
    event day     -0.106%    -1.31

These tests pin the boundaries of that window, because every neighbouring idea
measured the same day FAILED its placebo and the difference is the only thing
separating this from those:

  业绩预告 预减    -1.34% over 5 sessions, t=-4.14, and the -60 placebo on the
                  same cohort ran -1.87% with t=-5.93 - already falling
  世界机器人大会   +0.63% into the event over 4 years, t=0.95, and the -60
                  placebo returned +2.16% with t=3.16 - already a hot theme
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import disclosure_calendar as dc  # noqa: E402


# A calendar with a gap: 20260501-20260505 is the Labour Day break. Counting in
# calendar days across it would put an entry most of a week early.
CAL = [20260427, 20260428, 20260429, 20260430,
       20260506, 20260507, 20260508, 20260511, 20260512]


class DateParsingTests(unittest.TestCase):
    def test_the_forms_the_screener_actually_returns(self):
        for raw in ("2026-08-31", "2026.08.31", "20260831",
                    "2026-08-31|2026半年报", "2026-08-31 00:00:00"):
            self.assertEqual(dc._as_int_date(raw), 20260831, repr(raw))

    def test_integers_pass_through(self):
        self.assertEqual(dc._as_int_date(20260831), 20260831)

    def test_missing_values_are_none_not_zero(self):
        """A zero date sorts before everything and would select the whole book."""
        for bad in (None, "", "-", "--", "None", "abc", "2026"):
            self.assertIsNone(dc._as_int_date(bad), repr(bad))

    def test_out_of_range_is_rejected(self):
        self.assertIsNone(dc._as_int_date(18000101))


class NumberParsingTests(unittest.TestCase):
    def test_chinese_units(self):
        self.assertAlmostEqual(dc.parse_cn_number("11.31亿"), 1.131e9)
        self.assertAlmostEqual(dc.parse_cn_number("9757万"), 9.757e7)

    def test_missing_values(self):
        for bad in (None, "", "-", "--"):
            self.assertIsNone(dc.parse_cn_number(bad), repr(bad))


class CalendarRowTests(unittest.TestCase):
    def test_a_real_forward_row(self):
        rows = [{
            "代码": "688048", "名称": "长光华芯",
            "定期报告预计披露日期 2026.06.30": "2026-08-31",
            "成交额(元) 2026.08.27": "24.2亿",
        }]
        out = dc.parse_calendar_rows(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["code"], "688048")
        self.assertEqual(out[0]["scheduled"], 20260831)
        self.assertAlmostEqual(out[0]["turnover"], 2.42e9)

    def test_column_names_carry_a_period_suffix(self):
        """The suffix changes every reporting period; matching must be by fragment."""
        rows = [{"代码": "000001", "名称": "X",
                 "定期报告预计披露日期 2099.12.31": "2099-12-30"}]
        self.assertEqual(len(dc.parse_calendar_rows(rows)), 1)

    def test_rows_without_a_date_are_dropped_not_defaulted(self):
        rows = [{"代码": "000001", "名称": "X", "定期报告预计披露日期 x": "-"},
                {"代码": "notacode", "定期报告预计披露日期 x": "2026-08-31"}]
        self.assertEqual(dc.parse_calendar_rows(rows), [])

    def test_query_names_the_scheduled_field_not_the_actual_one(self):
        """实际披露日期 is history; only 预计 is knowable in advance."""
        q = dc.calendar_query(20260828, 20260910)
        self.assertIn("预计披露日期", q)
        self.assertNotIn("实际", q)
        self.assertIn("2026年8月28日", q)


class SessionCountingTests(unittest.TestCase):
    def test_counts_sessions_not_calendar_days(self):
        """20260430 -> 20260506 is one session across the holiday, not six days."""
        self.assertEqual(dc.sessions_until(20260506, 20260430, CAL), 1)

    def test_same_day_is_zero(self):
        self.assertEqual(dc.sessions_until(20260428, 20260428, CAL), 0)

    def test_a_past_date_is_negative(self):
        self.assertEqual(dc.sessions_until(20260427, 20260430, CAL), -3)

    def test_a_non_trading_date_resolves_to_the_next_session(self):
        """A report scheduled on a Saturday is read as the next open session."""
        self.assertEqual(dc.sessions_until(20260502, 20260430, CAL), 1)

    def test_a_date_past_the_calendar_end_is_none(self):
        self.assertIsNone(dc.sessions_until(20270101, 20260430, CAL))

    def test_an_empty_calendar_declines_rather_than_guesses(self):
        self.assertIsNone(dc.sessions_until(20260506, 20260430, []))


class WindowTests(unittest.TestCase):
    """The window IS the claim: -3 and -5 cleared their placebos, -10 did not."""

    def entry(self, scheduled, turnover=1e9):
        return {"code": "600000", "name": "X",
                "scheduled": scheduled, "turnover": turnover}

    def test_five_sessions_out_is_the_far_edge_and_is_selected(self):
        # 20260428 is CAL[1] and 20260508 is CAL[6], so exactly five sessions -
        # counted across the Labour Day gap, which is the whole point.
        r = dc.evaluate(self.entry(20260508), 20260428, CAL)
        self.assertTrue(r["selected"], r["reason"])
        self.assertEqual(r["sessions_until"], 5)

    def test_six_sessions_out_is_just_outside(self):
        r = dc.evaluate(self.entry(20260511), 20260428, CAL)
        self.assertFalse(r["selected"])
        self.assertIn("beyond", r["reason"])

    def test_one_session_out_is_the_near_edge_and_is_selected(self):
        r = dc.evaluate(self.entry(20260429), 20260428, CAL)
        self.assertTrue(r["selected"], r["reason"])

    def test_too_far_out_is_rejected(self):
        """-10 measured +0.33 with t=1.26 and did not beat its placebo."""
        r = dc.evaluate(self.entry(20260512), 20260427, CAL)
        self.assertFalse(r["selected"])
        self.assertIn("beyond", r["reason"])

    def test_the_event_day_itself_is_not_this_trade(self):
        """The disclosure session measured -0.106%, t=-1.31."""
        r = dc.evaluate(self.entry(20260428), 20260428, CAL)
        self.assertFalse(r["selected"])
        self.assertIn("event day", r["reason"])

    def test_a_deferred_report_is_dropped_not_carried(self):
        """The one failure mode the backtest could not see: it used ACTUAL filing
        dates, while a live book only ever has the scheduled one."""
        r = dc.evaluate(self.entry(20260427), 20260430, CAL)
        self.assertFalse(r["selected"])
        self.assertIn("defer", r["reason"])

    def test_illiquid_names_are_rejected(self):
        r = dc.evaluate(self.entry(20260429, turnover=1e6), 20260428, CAL)
        self.assertFalse(r["selected"])
        self.assertIn("turnover", r["reason"])

    def test_missing_turnover_does_not_reject(self):
        """The calendar query need not carry turnover; absence is not illiquidity."""
        e = {"code": "600000", "scheduled": 20260429, "turnover": None}
        self.assertTrue(dc.evaluate(e, 20260428, CAL)["selected"])

    def test_every_outcome_carries_a_reason(self):
        for scheduled, today in ((20260507, 20260428), (20260512, 20260427),
                                 (20260428, 20260428), (20260427, 20260430),
                                 (20270101, 20260428)):
            r = dc.evaluate(self.entry(scheduled), today, CAL)
            self.assertTrue(r["reason"], (scheduled, today))


class RankTests(unittest.TestCase):
    def sel(self, code, scheduled, turnover):
        return dc.evaluate({"code": code, "scheduled": scheduled,
                            "turnover": turnover}, 20260427, CAL)

    def test_nearest_report_ranks_first(self):
        """The drift concentrates in the last three sessions."""
        far = self.sel("000001", 20260506, 1e10)
        near = self.sel("000002", 20260429, 1e9)
        self.assertEqual([c["code"] for c in dc.rank([far, near])],
                         ["000002", "000001"])

    def test_turnover_breaks_ties_at_equal_distance(self):
        a = self.sel("000001", 20260429, 1e9)
        b = self.sel("000002", 20260429, 5e9)
        self.assertEqual([c["code"] for c in dc.rank([a, b])],
                         ["000002", "000001"])

    def test_rejected_candidates_never_rank(self):
        bad = self.sel("000003", 20260512, 1e9)
        self.assertEqual(dc.rank([bad]), [])


class SectorRankTests(unittest.TestCase):
    """Splitting by sector rank is what separated +0.91% from +0.37%.

        band        K=3            K=5            K=10          stocks
        top 1-3   +0.91 t=3.03   +0.49 t=1.30   -0.21 t=-0.47    1,084
        4-10      +0.53 t=2.73   +0.71 t=2.64   +0.94 t=2.45     2,342
        11-30     +0.49 t=3.42   +0.69 t=3.79   +0.74 t=3.00     5,082
        31+       +0.37 t=2.18   +0.54 t=2.33   +0.50 t=1.70    11,679
    """

    PEERS = [100.0, 90.0, 80.0, 70.0, 60.0, 50.0, 40.0, 30.0]

    def test_largest_turnover_ranks_first(self):
        self.assertEqual(dc.sector_rank_from_turnovers(100.0, self.PEERS), 1)

    def test_smallest_ranks_last(self):
        self.assertEqual(dc.sector_rank_from_turnovers(30.0, self.PEERS), 8)

    def test_a_thin_sector_declines_rather_than_calling_everyone_a_leader(self):
        self.assertIsNone(dc.sector_rank_from_turnovers(10.0, [1.0, 2.0]))

    def test_missing_turnover_is_none_not_rank_one(self):
        self.assertIsNone(dc.sector_rank_from_turnovers(None, self.PEERS))

    def test_the_tail_band_is_rejected(self):
        e = {"code": "600000", "scheduled": 20260429,
             "turnover": 1e9, "sector_rank": 90}
        r = dc.evaluate(e, 20260428, CAL)
        self.assertFalse(r["selected"])
        self.assertIn("sector", r["reason"])

    def test_a_leader_uses_the_shorter_window(self):
        """Top 1-3 measured +0.91% at three sessions and -0.21% at ten."""
        self.assertEqual(dc.entry_window_for(1), dc.ENTRY_SESSIONS_BEFORE_LEADER)
        self.assertEqual(dc.entry_window_for(20), dc.ENTRY_SESSIONS_BEFORE)
        self.assertEqual(dc.entry_window_for(None), dc.ENTRY_SESSIONS_BEFORE)

    def test_a_leader_five_sessions_out_is_too_early(self):
        e = {"code": "600000", "scheduled": 20260508,
             "turnover": 1e9, "sector_rank": 1}
        r = dc.evaluate(e, 20260428, CAL)
        self.assertFalse(r["selected"])
        self.assertIn("beyond", r["reason"])

    def test_a_mid_band_name_five_sessions_out_is_fine(self):
        e = {"code": "600000", "scheduled": 20260508,
             "turnover": 1e9, "sector_rank": 20}
        self.assertTrue(dc.evaluate(e, 20260428, CAL)["selected"])

    def test_leaders_rank_ahead_of_the_mid_band(self):
        lead = dc.evaluate({"code": "000001", "scheduled": 20260429,
                            "turnover": 1e9, "sector_rank": 2}, 20260428, CAL)
        mid = dc.evaluate({"code": "000002", "scheduled": 20260429,
                           "turnover": 9e9, "sector_rank": 25}, 20260428, CAL)
        self.assertEqual([c["code"] for c in dc.rank([mid, lead])],
                         ["000001", "000002"])


class TiltTests(unittest.TestCase):
    """Even the best band is +0.91% over three sessions: a tiebreaker, not a book."""

    def test_inside_the_window_tilts_up(self):
        self.assertGreater(dc.tilt_weight(3), 1.0)

    def test_outside_the_window_is_neutral(self):
        for s in (0, 6, 20, None):
            self.assertEqual(dc.tilt_weight(s), 1.0, repr(s))

    def test_a_deferred_schedule_is_neutral_not_negative(self):
        """Deferral is bad news, but this module measured a run-up, not a short."""
        self.assertEqual(dc.tilt_weight(-9), 1.0)

    def test_a_leader_tilts_harder_than_the_mid_band(self):
        self.assertGreater(dc.tilt_weight(2, sector_rank=1),
                           dc.tilt_weight(2, sector_rank=20))

    def test_the_tail_gets_no_tilt(self):
        self.assertEqual(dc.tilt_weight(2, sector_rank=90), 1.0)

    def test_a_leader_outside_its_shorter_window_is_neutral(self):
        """Rank alone does not earn a tilt; the window still has to hold."""
        self.assertEqual(dc.tilt_weight(5, sector_rank=1), 1.0)
        self.assertGreater(dc.tilt_weight(5, sector_rank=20), 1.0)

    def test_the_tilt_stays_small(self):
        """A measured +0.91% cannot justify a large multiplier."""
        for rk in (1, 20, None):
            self.assertLessEqual(dc.tilt_weight(2, sector_rank=rk), 1.10)


class SafetyTests(unittest.TestCase):
    def test_module_holds_no_execution_path(self):
        src = Path(dc.__file__).read_text(encoding="utf-8")
        for forbidden in ("buy_stock", "sell_stock", "execute_trade_action",
                          "requests", "urllib", "socket"):
            self.assertNotIn(forbidden, src, forbidden)

    def test_the_measured_window_is_what_the_module_uses(self):
        self.assertEqual(dc.ENTRY_SESSIONS_BEFORE, 5)
        self.assertEqual(dc.EXIT_SESSIONS_BEFORE, 1)
        self.assertFalse(dc.HOLD_THROUGH_EVENT)


if __name__ == "__main__":
    unittest.main()
