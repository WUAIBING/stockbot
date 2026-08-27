#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The selection logic behind the chaos-reversion finding.

Measured over 9,102,435 liquid A-share stock-days, top 25 per session, 10-session
hold, excess over the same session's universe mean:

                       regime   train   holdout
    reversion          chaos    +0.99     +2.03
                       calm     -0.76     -0.01
    momentum           chaos    -2.07     -2.65
                       calm     -1.60     -0.94

Unconditionally the same reversion book measures -0.03 / +0.45. Averaging chaos
and calm together is what hid the edge, and it is why these tests care so much
about the regime gate refusing to fire in calm water.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import chaos_reversion as cr  # noqa: E402


def rows(spec):
    """spec: list of (code, ma60_off, r20)."""
    return [{"code": c, "ma60_off": m, "r20": r} for c, m, r in spec]


class DispersionTests(unittest.TestCase):
    def test_needs_a_real_cross_section(self):
        self.assertIsNone(cr.session_dispersion([1.0] * 49))
        self.assertIsNotNone(cr.session_dispersion([1.0, -1.0] * 40))

    def test_implausible_moves_are_discarded(self):
        """Daily limits cap A-shares; 3761% was a corrupt price, not a return."""
        clean = [1.0, -1.0] * 40
        self.assertAlmostEqual(
            cr.session_dispersion(clean),
            cr.session_dispersion(clean + [3761.0, -500.0]),
            places=6)

    def test_none_values_are_tolerated(self):
        self.assertIsNotNone(cr.session_dispersion([1.0, None, -1.0] * 40))

    def test_a_uniform_session_has_no_dispersion(self):
        """Nothing to select between - the calm case, where the edge vanishes."""
        self.assertAlmostEqual(cr.session_dispersion([2.0] * 100), 0.0, places=9)

    def test_median_is_robust_to_the_tails(self):
        vals = [0.0] * 100 + [20.0] * 3
        self.assertAlmostEqual(cr.session_median(vals), 0.0, places=6)


class ThresholdTests(unittest.TestCase):
    def test_short_history_refuses_to_guess(self):
        """33 sessions put the cut at 3.46; 2,596 put it at 2.90."""
        self.assertIsNone(cr.dispersion_threshold([2.5] * 33))

    def test_long_history_returns_the_percentile(self):
        hist = [float(i) / 100 for i in range(1, 1001)]
        cut = cr.dispersion_threshold(hist, percentile=0.67)
        self.assertAlmostEqual(cut, 6.70, places=1)

    def test_default_percentile_matches_the_measurement(self):
        self.assertAlmostEqual(cr.DISPERSION_CHAOS_PERCENTILE, 0.67, places=2)


class RegimeTests(unittest.TestCase):
    def test_chaos_needs_both_legs(self):
        r = cr.classify_regime(3.2, -4.0, 2.90)
        self.assertEqual(r["regime"], "chaos")
        self.assertTrue(r["tradeable"])

    def test_high_dispersion_alone_is_not_enough(self):
        r = cr.classify_regime(3.2, +6.0, 2.90)
        self.assertEqual(r["regime"], "partial")
        self.assertFalse(r["tradeable"])

    def test_downtrend_alone_is_not_enough(self):
        r = cr.classify_regime(2.1, -4.0, 2.90)
        self.assertEqual(r["regime"], "partial")
        self.assertFalse(r["tradeable"])

    def test_todays_actual_market_is_calm(self):
        """2026-08-26: dispersion 2.37 (26th pctile of 11y), trend +6.21%."""
        r = cr.classify_regime(2.37, 6.21, 2.90)
        self.assertEqual(r["regime"], "calm")
        self.assertFalse(r["tradeable"])

    def test_missing_history_is_not_treated_as_calm(self):
        """Unknown must not silently look like a decision not to trade."""
        r = cr.classify_regime(None, -4.0, 2.90)
        self.assertEqual(r["regime"], "unknown")
        self.assertFalse(r["tradeable"])

    def test_the_reason_is_always_populated(self):
        for args in ((3.2, -4.0, 2.90), (2.1, 6.0, 2.90), (None, None, None)):
            self.assertTrue(cr.classify_regime(*args)["reason"])


class ScoreTests(unittest.TestCase):
    def test_lower_score_is_more_beaten_down(self):
        r = rows([("A", -30.0, -25.0), ("B", 0.0, 0.0), ("C", +30.0, +25.0)])
        s = cr.reversion_scores(r)
        self.assertLess(s[0], s[1])
        self.assertLess(s[1], s[2])

    def test_both_features_are_required(self):
        r = [{"code": "A", "ma60_off": -10.0}, {"code": "B", "ma60_off": 1.0, "r20": 1.0},
             {"code": "C", "ma60_off": 2.0, "r20": 2.0}]
        self.assertIsNone(cr.reversion_scores(r)[0])

    def test_ranks_are_scale_free(self):
        """Doubling every value must not change the ordering."""
        base = rows([("A", -10.0, -8.0), ("B", 0.0, 0.0), ("C", 10.0, 8.0)])
        doubled = rows([("A", -20.0, -16.0), ("B", 0.0, 0.0), ("C", 20.0, 16.0)])
        self.assertEqual(cr.reversion_scores(base), cr.reversion_scores(doubled))

    def test_a_single_row_cannot_be_ranked(self):
        self.assertEqual(cr.reversion_scores(rows([("A", 1.0, 1.0)])), [None])


class SelectionTests(unittest.TestCase):
    CHAOS = {"tradeable": True, "regime": "chaos", "reason": "test"}
    CALM = {"tradeable": False, "regime": "calm", "reason": "dispersion 2.37 < 2.90"}

    def universe(self, n=40):
        return rows([(f"{i:06d}", float(i) - n / 2, float(i) - n / 2) for i in range(n)])

    def test_chaos_selects_the_most_beaten_down(self):
        out = cr.select_candidates(self.universe(), self.CHAOS, top_n=5)
        self.assertTrue(out["traded"])
        self.assertEqual([p["code"] for p in out["picks"]],
                         ["000000", "000001", "000002", "000003", "000004"])

    def test_calm_declines_and_says_why(self):
        out = cr.select_candidates(self.universe(), self.CALM, top_n=5)
        self.assertFalse(out["traded"])
        self.assertEqual(out["picks"], [])
        self.assertIn("calm", out["reason"])

    def test_a_thin_universe_declines_rather_than_under_filling(self):
        """Fewer names than intended is a smaller book, not a better one."""
        out = cr.select_candidates(self.universe(10), self.CHAOS, top_n=25)
        self.assertFalse(out["traded"])
        self.assertIn("need 25", out["reason"])

    def test_the_default_book_is_the_measured_one(self):
        self.assertEqual(cr.DEFAULT_TOP_N, 25)
        self.assertEqual(cr.HOLD_SESSIONS, 10)

    def test_picks_carry_their_score(self):
        out = cr.select_candidates(self.universe(), self.CHAOS, top_n=3)
        for p in out["picks"]:
            self.assertIn("reversion_score", p)

    def test_declining_always_explains_itself(self):
        """A silent no-op is how a frozen pool went unnoticed for 37 days."""
        for regime in (self.CALM, {"tradeable": False, "regime": "unknown"}):
            out = cr.select_candidates(self.universe(), regime, top_n=5)
            self.assertTrue(out["reason"])

    def test_module_holds_no_execution_path(self):
        """It selects. It must not be able to trade."""
        src = Path(cr.__file__).read_text(encoding="utf-8")
        for forbidden in ("buy_stock", "sell_stock", "execute_trade_action",
                          "requests", "urllib"):
            self.assertNotIn(forbidden, src, forbidden)


if __name__ == "__main__":
    unittest.main()
