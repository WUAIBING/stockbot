#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Take-profit thresholds must mean the same thing on every board.

HIGH_PROFIT_TAKE_PROFIT_PCT (15) and MEDIUM_PROFIT_TAKE_PROFIT_PCT (8) were
calibrated on the +/-10% main boards and then applied everywhere. Measured over
9,567 TDX daily files (2023-01 onward, 10-day forward, turnover >= 20M), among
winning moves only:

    board       +8% cuts at   +15% cuts at   median win
    SH main         73rd pct       89th pct       +4.29%
    STAR 688        59th           81st           +6.38%

The same number is a tighter leash exactly where the moves are biggest, which
is backwards for a system whose edge is holding winners: STAR returns >+20% over
10 days on 5.6% of stock-days vs the main board's 2.8%, at an identical win rate
(47.3% vs 47.4%). It does not bite more often, it bites bigger.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import v10_moni_trader as t  # noqa: E402


class BoardScaleTests(unittest.TestCase):
    def test_star_scales_up(self):
        for code in ("688205", "688596", "688131"):
            self.assertEqual(t._board_take_profit_scale(code),
                             t.BOARD_TAKE_PROFIT_SCALE_STAR, code)

    def test_chinext_scales_up_less(self):
        for code in ("300750", "301162"):
            self.assertEqual(t._board_take_profit_scale(code),
                             t.BOARD_TAKE_PROFIT_SCALE_CHINEXT, code)

    def test_main_boards_are_unscaled(self):
        for code in ("600519", "601398", "000001", "002487"):
            self.assertEqual(t._board_take_profit_scale(code), 1.0, code)

    def test_star_scales_more_than_chinext(self):
        """Both run a +/-20% limit, but STAR realises the larger move."""
        self.assertGreater(t._board_take_profit_scale("688205"),
                           t._board_take_profit_scale("300750"))

    def test_scale_is_well_below_the_daily_limit_ratio(self):
        """The limit ratio is 2.0x; realised volatility scales far less.

        Scaling by the limit would overshoot badly - it would push STAR's
        medium threshold to 16%, above the main board's HIGH threshold.
        """
        self.assertLess(t._board_take_profit_scale("688205"), 2.0)
        self.assertGreater(t._board_take_profit_scale("688205"), 1.0)


class TakeProfitLevelTests(unittest.TestCase):
    def test_main_board_levels_are_exactly_the_constants(self):
        """No behaviour change on the boards the constants were calibrated on."""
        high, medium = t._take_profit_levels("600519")
        self.assertEqual(high, t.HIGH_PROFIT_TAKE_PROFIT_PCT)
        self.assertEqual(medium, t.MEDIUM_PROFIT_TAKE_PROFIT_PCT)

    def test_unknown_code_keeps_main_board_behaviour(self):
        """Any caller that cannot supply a code is unaffected."""
        for code in ("", None, "   ", "999999"):
            self.assertEqual(t._take_profit_levels(code),
                             (t.HIGH_PROFIT_TAKE_PROFIT_PCT,
                              t.MEDIUM_PROFIT_TAKE_PROFIT_PCT))

    def test_star_levels_match_the_measurement(self):
        """Measured main-board-equivalent for STAR was 11.7% / 21.2%."""
        high, medium = t._take_profit_levels("688205")
        self.assertAlmostEqual(medium, 11.6, places=1)
        self.assertAlmostEqual(high, 21.75, places=2)

    def test_chinext_levels_match_the_measurement(self):
        """Measured main-board-equivalent for ChiNext was 10.3% / 19.6%."""
        high, medium = t._take_profit_levels("300750")
        self.assertAlmostEqual(medium, 10.4, places=1)
        self.assertAlmostEqual(high, 19.5, places=1)

    def test_high_always_exceeds_medium(self):
        """Inverting these would make the medium branch unreachable."""
        for code in ("688205", "300750", "600519", ""):
            high, medium = t._take_profit_levels(code)
            self.assertGreater(high, medium, code)

    def test_star_medium_stays_below_main_board_high(self):
        """The scaled medium tier must not overtake the unscaled high tier.

        If it did, a STAR position would need a larger gain to trigger the
        LENIENT decay condition (decay>=2) than a main-board position needs for
        the STRICT one (decay>=1) - an ordering the exit logic does not expect.
        """
        _, star_medium = t._take_profit_levels("688205")
        self.assertLess(star_medium, t.HIGH_PROFIT_TAKE_PROFIT_PCT)


class ExitOrderingTests(unittest.TestCase):
    """The gains at which each board actually releases a winner."""

    def test_a_star_winner_is_held_where_a_main_board_one_is_sold(self):
        _, star_medium = t._take_profit_levels("688205")
        _, main_medium = t._take_profit_levels("600519")
        gain = 9.0  # a real 688205-style move
        self.assertGreaterEqual(gain, main_medium)   # main board: eligible
        self.assertLess(gain, star_medium)           # STAR: keep holding

    def test_star_still_releases_once_the_move_is_genuinely_large(self):
        """Scaling defers the exit; it does not remove it."""
        high, medium = t._take_profit_levels("688205")
        self.assertGreaterEqual(12.0, medium)
        self.assertGreaterEqual(22.0, high)


if __name__ == "__main__":
    unittest.main()
