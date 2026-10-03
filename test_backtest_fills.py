#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backtests may only fill at prices an order could really have got."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import backtest_fills as bf  # noqa: E402


def bar(o, h, lo, c, day="2026-09-15"):
    return (day, o, h, lo, c)


class LimitPriceTests(unittest.TestCase):
    def test_boards(self):
        self.assertEqual(bf.limit_pct("600623"), 0.10)
        self.assertEqual(bf.limit_pct("002145"), 0.10)
        self.assertEqual(bf.limit_pct("688432"), 0.20)
        self.assertEqual(bf.limit_pct("300185"), 0.20)
        self.assertEqual(bf.limit_pct("830799"), 0.30)
        self.assertEqual(bf.limit_pct("600623", is_st=True), 0.05)

    def test_limits_round_half_up_like_the_exchange(self):
        """10.05 x 1.1 = 11.055 -> 11.06. Python's round() can say 11.05."""
        up, down = bf.limit_prices(10.05, 0.10)
        self.assertEqual(up, 11.06)
        self.assertEqual(down, 9.05)

    def test_688432_on_09_16(self):
        """Prev close 45.05, STAR 20%: limit 54.06 - the price it locked at."""
        self.assertEqual(bf.limit_prices(45.05, 0.20)[0], 54.06)


class BuyFillTests(unittest.TestCase):
    def test_an_open_below_the_limit_fills_at_the_open(self):
        self.assertEqual(bf.buy_fill(bar(10.5, 11.0, 10.4, 10.9), 10.0, 0.10), 10.5)

    def test_a_board_that_breaks_fills_at_the_limit(self):
        self.assertEqual(bf.buy_fill(bar(11.0, 11.0, 10.7, 10.9), 10.0, 0.10), 11.0)

    def test_a_one_word_limit_up_never_fills(self):
        """THE EVENT-STUDY BUG: this bar was being bought at its open."""
        self.assertIsNone(bf.buy_fill(bar(11.0, 11.0, 11.0, 11.0), 10.0, 0.10))

    def test_a_star_stock_is_not_judged_by_the_main_board_limit(self):
        """+10% on STAR is an ordinary day, not a seal."""
        self.assertEqual(bf.buy_fill(bar(11.0, 11.0, 11.0, 11.0), 10.0, 0.20), 11.0)


class SellFillTests(unittest.TestCase):
    def test_an_open_above_limit_down_fills_at_the_open(self):
        self.assertEqual(bf.sell_fill(bar(9.5, 9.7, 9.2, 9.3), 10.0, 0.10), 9.5)

    def test_a_sealed_limit_down_never_fills(self):
        """A stop at -8% cannot get out of a one-word limit-down either."""
        self.assertIsNone(bf.sell_fill(bar(9.0, 9.0, 9.0, 9.0), 10.0, 0.10))

    def test_a_limit_down_that_opens_up_fills_at_the_limit(self):
        self.assertEqual(bf.sell_fill(bar(9.0, 9.3, 9.0, 9.1), 10.0, 0.10), 9.0)


class FirstFillTests(unittest.TestCase):
    def test_a_resumption_run_of_seals_is_waited_out(self):
        """Resumes locked, stays locked two more days, opens on the third."""
        bars = [bar(10.0, 10.0, 10.0, 10.0, "d0"),
                bar(11.0, 11.0, 11.0, 11.0, "d1"),    # resumption, sealed
                bar(12.1, 12.1, 12.1, 12.1, "d2"),    # sealed
                bar(13.31, 13.31, 13.31, 13.31, "d3"),  # sealed
                bar(14.2, 14.5, 13.6, 13.9, "d4")]    # opens below 14.64: fill
        j, px = bf.first_buy_fill(bars, 2, 0.10)
        self.assertEqual((j, px), (4, 14.2))
        self.assertGreater(px / bars[2][1] - 1, 0.17)   # what the naive entry hid

    def test_never_fillable_says_none(self):
        bars = [bar(10.0, 10.0, 10.0, 10.0)] + [bar(10.0 * 1.1 ** k, 10.0 * 1.1 ** k,
                                                   10.0 * 1.1 ** k, 10.0 * 1.1 ** k) for k in range(1, 6)]
        self.assertIsNone(bf.first_buy_fill(bars, 1, 0.10, max_wait=5))

    def test_dict_bars_work_too(self):
        bars = [{"open": 10, "high": 10, "low": 10, "close": 10},
                {"open": 10.2, "high": 10.4, "low": 10.1, "close": 10.3}]
        self.assertEqual(bf.first_buy_fill(bars, 1, 0.10), (1, 10.2))

    def test_sell_side_waits_out_a_limit_down_run(self):
        bars = [bar(10.0, 10.0, 10.0, 10.0), bar(9.0, 9.0, 9.0, 9.0), bar(8.3, 8.6, 8.1, 8.4)]
        self.assertEqual(bf.first_sell_fill(bars, 1, 0.10), (2, 8.3))


class SafetyTests(unittest.TestCase):
    def test_no_market_access_and_no_orders(self):
        src = (SKILL / "backtest_fills.py").read_text(encoding="utf-8")
        for bad in ("buy_stock", "sell_stock", "execute_trade_action", "mockTrading",
                    "TdxHq_API", "import tdx_hosts", "requests."):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
