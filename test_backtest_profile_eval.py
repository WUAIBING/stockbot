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




class ExitStudyTests(unittest.TestCase):
    """Measures what the take-profit caps cost, instead of guessing.

    The live policy caps gains at 8/15% and defines no stop. Episodes carry a
    "profit_truncation" verdict, but nothing had ever measured whether the peak
    was actually reachable. gap% answers that: large means the caps truncate,
    small means the peak was never holdable and letting winners run is wrong.
    """

    def test_empty_input_reports_nothing_not_zero(self):
        self.assertEqual(ev.exit_study([]), {"n": 0})

    def test_rows_missing_forward_data_are_skipped(self):
        """A NaN horizon must not be counted as a 0% outcome."""
        self.assertEqual(ev.exit_study([(None, None, None), (1.0, None, -1.0)]), {"n": 0})

    def test_truncation_gap_is_peak_minus_held(self):
        # held +2%, but +10% was available along the way
        out = ev.exit_study([(2.0, 10.0, -1.0)])
        self.assertAlmostEqual(out["avg_return_pct"], 2.0)
        self.assertAlmostEqual(out["avg_mfe_pct"], 10.0)
        self.assertAlmostEqual(out["truncation_gap_pct"], 8.0)

    def test_no_gap_when_the_peak_is_where_it_closed(self):
        out = ev.exit_study([(5.0, 5.0, -2.0)])
        self.assertAlmostEqual(out["truncation_gap_pct"], 0.0)

    def test_cap_helps_when_price_peaks_then_gives_it_back(self):
        """Peak +12%, closed -3%: capping at +8% would have banked the move."""
        out = ev.exit_study([(-3.0, 12.0, -5.0)], take_profit_pct=8.0)
        self.assertAlmostEqual(out["avg_return_if_capped_pct"], 8.0)
        self.assertAlmostEqual(out["cap_vs_hold_pct"], 11.0)
        self.assertAlmostEqual(out["reached_take_profit_pct"], 100.0)

    def test_cap_hurts_when_the_move_keeps_going(self):
        """Peak +30%, closed +25%: the cap exits at 8 and forfeits 17."""
        out = ev.exit_study([(25.0, 30.0, -1.0)], take_profit_pct=8.0)
        self.assertAlmostEqual(out["avg_return_if_capped_pct"], 8.0)
        self.assertAlmostEqual(out["cap_vs_hold_pct"], -17.0)

    def test_cap_is_inert_when_the_peak_never_reaches_it(self):
        out = ev.exit_study([(3.0, 6.0, -2.0)], take_profit_pct=8.0)
        self.assertAlmostEqual(out["avg_return_if_capped_pct"], 3.0)
        self.assertAlmostEqual(out["cap_vs_hold_pct"], 0.0)
        self.assertAlmostEqual(out["reached_take_profit_pct"], 0.0)

    def test_mae_tracks_the_worst_drawdown(self):
        out = ev.exit_study([(1.0, 2.0, -9.0), (1.0, 2.0, -1.0)])
        self.assertAlmostEqual(out["avg_mae_pct"], -5.0)

    def test_evaluate_attaches_an_exit_study_per_rule(self):
        rows = [{
            "date": "2026-02-01", "ret_5d": 2.0, "mfe_5d": 9.0, "mae_5d": -3.0,
            "bz_direction": -0.5, "bz_rt_direction": -0.5, "close_vs_ma20": 0.0,
            "weekly_slope": 6.0, "weekly_align": True, "rsi14": 50.0,
            "amt_ratio": 1.0, "vol_expand": True, "is_green": True,
        }]
        out = ev.evaluate(rows, split_date="2026-01-01")
        study = out["rules"]["v9_full"]["exit_study"]
        self.assertEqual(study["n"], 1)
        self.assertAlmostEqual(study["truncation_gap_pct"], 7.0)
        self.assertEqual(out["baseline_exit_study"]["n"], 1)




class MissingFieldTests(unittest.TestCase):
    """A condition cannot be met by data that does not exist.

    bz is absent for ~99.7% of a multi-year run - pytdx serves only about 25
    sessions of 5-minute history. The extractor used to emit 0.0 for those, and
    the evaluator used to default missing numerics to 0.0, so:

        bz_rt_min: -0.2   ->  0.0 >= -0.2  ->  PASSES on a value never observed

    pre_breakout appeared to match 6,070 holdout records on that basis, and the
    resulting per-rule table was used to claim its win rate underperformed the
    baseline. It was measuring a default.

    The mirror case is just as wrong in the other direction: bz_lt: -0.3 fails
    against 0.0, so V9_full looked vanishingly rare rather than unevaluable.
    """

    def _rec(self, **over):
        base = {
            "date": "2026-03-02", "ret_5d": 1.0,
            "close_vs_ma20": 0.0, "weekly_slope": 6.0, "weekly_align": True,
            "rsi14": 50.0, "amt_ratio": 1.0, "vol_expand": True, "is_green": True,
        }
        base.update(over)
        return base

    def test_absent_bz_does_not_satisfy_bz_rt_min(self):
        rule = ev.load_profile()["rules"]["pre_breakout"]
        self.assertFalse(ev.rule_matches(rule, self._rec()))

    def test_empty_string_bz_does_not_satisfy_bz_rt_min(self):
        """CSV round-trips a None as an empty field."""
        rule = ev.load_profile()["rules"]["pre_breakout"]
        self.assertFalse(ev.rule_matches(rule, self._rec(bz_rt_direction="")))

    def test_present_bz_is_evaluated_normally(self):
        rule = ev.load_profile()["rules"]["pre_breakout"]
        ok = self._rec(bz_rt_direction=-0.1, weekly_slope=0.0, close_vs_ma20=0.0,
                       amt_ratio=1.0, rsi14=50.0)
        self.assertTrue(ev.rule_matches(rule, ok))
        bad = dict(ok, bz_rt_direction=-0.9)   # below bz_rt_min
        self.assertFalse(ev.rule_matches(bad and rule, bad))

    def test_absent_bz_also_fails_bz_lt(self):
        rule = ev.load_profile()["rules"]["v9_full"]
        self.assertFalse(ev.rule_matches(rule, self._rec()))
        self.assertTrue(ev.rule_matches(rule, self._rec(bz_direction=-0.5)))

    def test_rules_without_bz_are_unaffected(self):
        """vol_breakout has no bz condition and must still match."""
        rule = ev.load_profile()["rules"]["vol_breakout"]
        self.assertTrue(ev.rule_matches(rule, self._rec(
            vol_expand=True, is_green=True, weekly_align=True,
            rsi14=60.0, close_vs_ma20=5.0)))

    def test_missing_non_bz_field_also_fails(self):
        """The rule is general: any absent field fails its condition."""
        rule = ev.load_profile()["rules"]["vol_breakout"]
        rec = self._rec(vol_expand=True, is_green=True, weekly_align=True,
                        close_vs_ma20=5.0)
        rec.pop("rsi14")
        self.assertFalse(ev.rule_matches(rule, rec))


if __name__ == "__main__":
    unittest.main()
