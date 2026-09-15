#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The trailing exit must behave exactly like the one that was measured.

A rule deployed on different terms from the one tested is a different rule -
which is precisely how the candle exit drifted: studied on completed daily bars,
run live on a candle fifteen minutes old.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import trail_exit as te  # noqa: E402


def backtest_exit_index(entry, closes, stop, width, act):
    """The loop used in the full-market study, verbatim in behaviour."""
    best = entry
    trailing = False
    for k, c in enumerate(closes):
        best = max(best, c)
        if best >= entry * (1 + act / 100):
            trailing = True
        if (c / entry - 1) * 100 <= stop or (trailing and c < best * (1 - width / 100)):
            return k
    return None


def live_exit_index(entry, closes, **kw):
    """Run evaluate() once per session, as the daily sell loop does."""
    for k in range(len(closes)):
        if te.evaluate(closes[:k + 1], entry, **kw)["should_exit"]:
            return k
    return None


class MatchesTheStudyTests(unittest.TestCase):
    PATHS = [
        [101, 104, 108, 112, 115, 109, 103.4, 100],     # runs, then gives back 10%
        [99, 97, 95, 93, 91.9, 95],                        # grinds into the stop
        [102, 105, 109.9, 106, 99],                        # never activates
        [111, 125, 138, 150, 141, 136, 134.9, 120],       # the big fish
        [100, 100, 100],                                   # nothing happens
        [108, 110, 99.1, 99],                              # activates, drops exactly 10%
    ]

    def test_same_exit_session_as_the_backtest(self):
        for path in self.PATHS:
            for stop, width, act in ((-8, 10, 10), (-6, 8, 5), (-10, 15, 10)):
                with self.subTest(path=path, prm=(stop, width, act)):
                    self.assertEqual(
                        live_exit_index(100, path, stop_pct=stop, width_pct=width, activate_pct=act),
                        backtest_exit_index(100, path, stop, width, act))


class BehaviourTests(unittest.TestCase):
    def test_a_runner_is_not_sold_on_a_pullback_inside_the_trail(self):
        """The whole point: 150 -> 141 is a 6% dip, not an exit."""
        v = te.evaluate([111, 125, 138, 150, 141], 100)
        self.assertFalse(v["should_exit"])
        self.assertTrue(v["trailing"])

    def test_the_trail_takes_the_fish_when_it_turns(self):
        v = te.evaluate([111, 125, 138, 150, 141, 136, 134.9], 100)
        self.assertTrue(v["should_exit"])
        self.assertIn("跟踪止盈", v["reason"])
        self.assertAlmostEqual(v["best_ret_pct"], 50.0)

    def test_the_stop_cuts_a_loser(self):
        v = te.evaluate([97, 94, 91.9], 100)
        self.assertTrue(v["should_exit"])
        self.assertIn("止损", v["reason"])

    def test_before_activation_a_drawdown_from_the_best_does_not_exit(self):
        """+9.9% then back to +0.5% is noise until the trail has started."""
        v = te.evaluate([105, 109.9, 100.5], 100)
        self.assertFalse(v["should_exit"])
        self.assertFalse(v["trailing"])

    def test_it_judges_closes_not_intraday_highs(self):
        """The study used closes. An intraday spike must not arm the trail."""
        v = te.evaluate([109.0], 100)
        self.assertFalse(v["trailing"])

    def test_no_data_never_exits_and_says_so(self):
        for closes in ([], None, [0, None, "x"]):
            v = te.evaluate(closes, 100)
            self.assertFalse(v["should_exit"])
            self.assertTrue(v["data_unavailable"])

    def test_no_entry_price_never_exits(self):
        v = te.evaluate([90, 80], 0)
        self.assertFalse(v["should_exit"])
        self.assertTrue(v["data_unavailable"])

    def test_the_red_candle_that_sold_688432_would_not_sell(self):
        """09-15: cost 45.66, prior closes 45.22 (halted) and 46.15, sold at 43.47
        on a fifteen-minute candle. On completed closes: +1.1%, no stop, no trail."""
        v = te.evaluate([46.15], 45.656)
        self.assertFalse(v["should_exit"])


class SafetyTests(unittest.TestCase):
    def test_it_holds_no_execution_path_and_no_market_access(self):
        src = (SKILL / "trail_exit.py").read_text(encoding="utf-8")
        for bad in ("buy_stock", "sell_stock", "execute_trade_action", "mockTrading",
                    "requests.", "TdxHq_API", "import tdx_hosts", "open("):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
