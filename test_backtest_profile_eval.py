#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for scoring the live strategy profile against backtest signal records."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import backtest_profile_eval as ev  # noqa: E402


def record(**over):
    base = {
        "date": "2026-03-02",
        "code": "600000",
        "ret_5d": 1.0,
        "bz_direction": -1.0,
        "bz_rt_direction": -1.0,
        "close_vs_ma20": 0.0,
        "weekly_slope": 6.0,
        "weekly_align": True,
        "rsi14": 50.0,
        "amt_ratio": 1.0,
        "vol_expand": True,
        "is_green": True,
    }
    base.update(over)
    return base


class ConditionCoverageTests(unittest.TestCase):
    """Every condition the shipped profile uses must be expressible."""

    def test_live_profile_is_fully_supported(self):
        profile = ev.load_profile()
        unsupported = {}
        for name, rule in profile["rules"].items():
            missing = ev.unsupported_conditions(rule)
            if missing:
                unsupported[name] = missing
        self.assertEqual(unsupported, {},
                         "profile uses conditions the evaluator cannot express")

    def test_all_eleven_rules_present(self):
        self.assertEqual(len(ev.load_profile()["rules"]), 11)


class RuleMatchTests(unittest.TestCase):
    def test_v9_full_matches_the_legacy_tier1_condition(self):
        """backtest_t5_v10.compute_signal_tier tier 1:
        bz < -0.3 and weekly_align and -5.0 <= ma20_off <= 2.0
        """
        rule = ev.load_profile()["rules"]["v9_full"]
        self.assertTrue(ev.rule_matches(rule, record(
            bz_direction=-0.5, weekly_align=True, close_vs_ma20=-1.0)))
        # bz not deep enough
        self.assertFalse(ev.rule_matches(rule, record(
            bz_direction=-0.2, weekly_align=True, close_vs_ma20=-1.0)))
        # weekly not aligned
        self.assertFalse(ev.rule_matches(rule, record(
            bz_direction=-0.5, weekly_align=False, close_vs_ma20=-1.0)))
        # outside the MA20 band
        self.assertFalse(ev.rule_matches(rule, record(
            bz_direction=-0.5, weekly_align=True, close_vs_ma20=3.0)))

    def test_ma20_band_is_inclusive_at_both_edges(self):
        rule = ev.load_profile()["rules"]["v9_full"]
        for edge in (-5.0, 2.0):
            self.assertTrue(ev.rule_matches(rule, record(
                bz_direction=-0.5, weekly_align=True, close_vs_ma20=edge)), edge)

    def test_boolean_conditions_must_match_exactly(self):
        rule = ev.load_profile()["rules"]["vol_breakout"]
        self.assertTrue(ev.rule_matches(rule, record(
            vol_expand=True, is_green=True, weekly_align=True,
            rsi14=60.0, close_vs_ma20=5.0)))
        self.assertFalse(ev.rule_matches(rule, record(
            vol_expand=False, is_green=True, weekly_align=True,
            rsi14=60.0, close_vs_ma20=5.0)))

    def test_rsi_upper_bound_is_strict(self):
        rule = ev.load_profile()["rules"]["vol_breakout"]
        common = dict(vol_expand=True, is_green=True, weekly_align=True, close_vs_ma20=5.0)
        self.assertTrue(ev.rule_matches(rule, record(rsi14=69.9, **common)))
        self.assertFalse(ev.rule_matches(rule, record(rsi14=70.0, **common)))

    def test_bz_rules_read_bz_direction_not_bz_rt(self):
        """Seven rules gate on bz; they are unevaluable without it."""
        rule = ev.load_profile()["rules"]["v9_full"]
        self.assertTrue(ev.rule_matches(rule, record(
            bz_direction=-0.5, bz_rt_direction=99.0,
            weekly_align=True, close_vs_ma20=0.0)))

    def test_unknown_condition_refuses_to_match(self):
        self.assertFalse(ev.rule_matches({"mode": "x", "no_such_field": 1}, record()))

    def test_missing_field_does_not_crash(self):
        rule = ev.load_profile()["rules"]["v9_full"]
        self.assertFalse(ev.rule_matches(rule, {"date": "2026-01-01", "ret_5d": 0.0}))


class SplitTests(unittest.TestCase):
    def test_split_is_required(self):
        with self.assertRaises(ValueError):
            ev.evaluate([], split_date="")

    def test_records_land_in_the_right_period(self):
        rows = [
            record(date="2025-12-31", ret_5d=10.0, bz_direction=-0.5,
                   weekly_align=True, close_vs_ma20=0.0),
            record(date="2026-01-01", ret_5d=-4.0, bz_direction=-0.5,
                   weekly_align=True, close_vs_ma20=0.0),
        ]
        out = ev.evaluate(rows, split_date="2026-01-01")
        v9 = out["rules"]["v9_full"]
        self.assertEqual(v9["train"]["n"], 1)
        self.assertEqual(v9["holdout"]["n"], 1)
        self.assertAlmostEqual(v9["train"]["avg_return_pct"], 10.0)
        self.assertAlmostEqual(v9["holdout"]["avg_return_pct"], -4.0)

    def test_split_date_itself_is_holdout(self):
        rows = [record(date="2026-01-01", ret_5d=2.0, bz_direction=-0.5,
                       weekly_align=True, close_vs_ma20=0.0)]
        out = ev.evaluate(rows, split_date="2026-01-01")
        self.assertEqual(out["rules"]["v9_full"]["holdout"]["n"], 1)
        self.assertEqual(out["rules"]["v9_full"]["train"]["n"], 0)

    def test_baseline_counts_every_record(self):
        rows = [record(date="2025-06-01"), record(date="2026-06-01")]
        out = ev.evaluate(rows, split_date="2026-01-01")
        self.assertEqual(out["records_scored"], 2)
        self.assertEqual(out["baseline"]["train"]["n"], 1)
        self.assertEqual(out["baseline"]["holdout"]["n"], 1)


class SummaryTests(unittest.TestCase):
    def test_empty_summary_reports_none_not_zero(self):
        """A rule with no matches must not look like a 0% win rate."""
        s = ev.summarize([])
        self.assertEqual(s["n"], 0)
        self.assertIsNone(s["win_rate_pct"])
        self.assertIsNone(s["avg_return_pct"])

    def test_win_rate_counts_strictly_positive(self):
        s = ev.summarize([1.0, 0.0, -1.0, 2.0])
        self.assertEqual(s["n"], 4)
        self.assertAlmostEqual(s["win_rate_pct"], 50.0)
        self.assertAlmostEqual(s["avg_return_pct"], 0.5)

    def test_median_of_even_sample(self):
        self.assertAlmostEqual(ev.summarize([1.0, 3.0])["median_return_pct"], 2.0)


class ReportTests(unittest.TestCase):
    def test_report_renders_and_flags_the_holdout(self):
        rows = [record(date="2026-02-01", ret_5d=1.5, bz_direction=-0.5,
                       weekly_align=True, close_vs_ma20=0.0)]
        text = ev.format_report(ev.evaluate(rows, split_date="2026-01-01"))
        self.assertIn("HOLDOUT", text)
        self.assertIn("V9_full", text)
        self.assertIn("baseline", text.lower())


if __name__ == "__main__":
    unittest.main()
