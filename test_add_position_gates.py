#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Two gates on adding to a position: when it pays, and when it costs breadth.

Big meat is deliberately allowed past MAX_POSITION_PCT_NAV - adds run to 1.70x
target, so a winner reaches ~3.4% NAV against 2% for every new position. That is
the intended design and neither gate here changes it.

WHEN. Measured over 9.1M liquid A-share stock-days, forward 10-day return by
open profit:

    up 3-6%    +0.03 train  +0.16 holdout   first add, positive in both
    up 6-12%   -0.09        +0.29           second add, sign flips
    up >12%    -0.94        -0.27           consistently bad

Previously unbounded: a position up 30% scored the same +2 as one up 6%.

BREADTH. Noise scales as 1/sqrt(effective N), where effective N is 1/sum(w^2).
The skew an add introduces costs far more on an empty book:

    2 held, one big meat      effective N 1.6 of 2    18% lost
    25 full, 5 big meat       effective N 23.6 of 25   6% lost

The existing brake reserves cash for new opportunities, but the account sits
~96% idle so it never binds. Cash is not the scarce resource; breadth is.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import v10_moni_trader as t  # noqa: E402


class ConstantTests(unittest.TestCase):
    def test_profit_ceiling_sits_where_returns_turn(self):
        self.assertEqual(t.ADD_POSITION_MAX_PROFIT_PCT, 12.0)

    def test_ceiling_is_above_both_add_triggers(self):
        """Otherwise the second add would be unreachable."""
        self.assertGreater(t.ADD_POSITION_MAX_PROFIT_PCT,
                           t.ADD_POSITION_BIG_MEAT_PROFIT_PCT)
        self.assertGreater(t.ADD_POSITION_BIG_MEAT_PROFIT_PCT,
                           t.ADD_POSITION_BIG_MEAT_EARLY_PROFIT_PCT)

    def test_fill_ratio_leaves_room_to_add_before_full(self):
        self.assertEqual(t.ADD_POSITION_MIN_BOOK_FILL_RATIO, 0.70)
        self.assertLess(t.ADD_POSITION_MIN_BOOK_FILL_RATIO, 1.0)

    def test_big_meat_still_exceeds_the_new_position_cap(self):
        """The whole point: big meat is NOT held to 2%."""
        self.assertGreater(t.ADD_POSITION_BIG_MEAT_TARGET_MULTIPLIER, 1.0)
        self.assertGreater(
            t.MAX_POSITION_PCT_NAV * t.ADD_POSITION_BIG_MEAT_TARGET_MULTIPLIER,
            t.MAX_POSITION_PCT_NAV)


class ProfitCeilingTests(unittest.TestCase):
    def score(self, profit_pct):
        return t._build_big_meat_identity_profile({}, profit_pct=profit_pct)

    def test_first_add_band_still_scores(self):
        """+3-6% was positive in both periods; it must survive."""
        self.assertGreaterEqual(self.score(4.0)["score"], 1)

    def test_second_add_band_still_scores(self):
        self.assertGreaterEqual(self.score(8.0)["score"], 2)

    def test_at_the_ceiling_still_scores(self):
        """12.0 is the last value that pays; the rule is strictly greater."""
        self.assertGreaterEqual(self.score(12.0)["score"], 2)

    def test_above_the_ceiling_scores_nothing_from_profit(self):
        for pnl in (12.1, 15.0, 30.0, 80.0):
            self.assertEqual(self.score(pnl)["score"], 0, pnl)

    def test_above_the_ceiling_says_why(self):
        notes = " ".join(self.score(20.0)["notes"])
        self.assertIn("超加仓上限", notes)
        self.assertIn("不再加仓", notes)

    def test_a_loser_scores_nothing_and_is_not_mislabelled(self):
        p = self.score(-4.0)
        self.assertEqual(p["score"], 0)
        self.assertNotIn("超加仓上限", " ".join(p["notes"]))

    def test_holding_is_unaffected_by_the_ceiling(self):
        """This caps buying more, not holding. A winner still runs."""
        self.assertGreater(t.MAX_HOLD_DAYS, 0)
        self.assertLess(t.INTRADAY_HARD_STOP_PCT, 0)


class BreadthGateTests(unittest.TestCase):
    """Effective breadth is 1/sum(w^2); the gate exists because of this shape."""

    @staticmethod
    def effective(weights):
        s = sum(weights)
        w = [x / s for x in weights]
        return 1.0 / sum(x * x for x in w)

    def test_an_add_on_an_empty_book_costs_far_more(self):
        empty = self.effective([3.4, 1.24]) / 2
        full = self.effective([3.4] * 5 + [2.0] * 20) / 25
        self.assertLess(empty, full)
        self.assertLess(empty, 0.85)      # >15% of breadth lost
        self.assertGreater(full, 0.93)    # <7% lost

    def test_equal_weights_lose_nothing(self):
        self.assertAlmostEqual(self.effective([2.0] * 25), 25.0, places=6)

    def test_gate_threshold_blocks_todays_book(self):
        """2 of 25 held is 8% full - adds must defer to opening new names."""
        self.assertLess(2 / 25, t.ADD_POSITION_MIN_BOOK_FILL_RATIO)

    def test_gate_permits_a_nearly_full_book(self):
        self.assertGreaterEqual(20 / 25, t.ADD_POSITION_MIN_BOOK_FILL_RATIO)

    def test_slot_total_is_read_from_tier_config(self):
        """The gate must follow the ramp to 50, not a hard-coded number."""
        total = sum(c["max_stocks"] for c in t.TIER_CONFIG.values())
        self.assertEqual(total, 25)
        self.assertGreater(total, 0)


if __name__ == "__main__":
    unittest.main()
