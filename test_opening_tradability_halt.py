#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The 09:31 gate must catch a suspended stock.

On 2026-08-31 有研硅 (688432) halted from the open for a 重大资产重组. The gate
ran at 09:31, saw the stock, and published it as tradable:

    688432  open 0.0  last 0.0  volume 0  amount 0.0  -> allow_today

The rule to catch exactly this already existed and is even documented in the
file's own notes - 早盘无竞价且 09:31 仍为 0 成交的标的，今日剔除自动买卖 - but
it did not fire, because pytdx does not return zero turnover for a halted stock.
It returns a DENORMALIZED FLOAT:

    688432  amount = 5.877471754111438e-39
    600929  amount = 5.877471754111438e-39     (雪天盐业, also halted)
    000811  amount = 1831799680.0              (trading, for contrast)

That is uninitialised bytes read as a float32, and the identical constant on
both suspended names proves it is a fixed pattern rather than a real figure.

It defeated `amount <= 0`. Three of the four zero checks passed and the fourth
did not, so the gate waved a suspended stock through for both buying AND
selling. Worse, the evidence was invisible in the published file: round(5.877e-39,
2) is 0.0, so the record displayed a zero that the comparison never saw.

The consequence downstream is real. smart-sell runs about every thirty minutes,
and every order against a suspended stock is rejected - roughly forty dead
orders over a five-session halt, each landing in the broker record as a 废单.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import security_master_refresh as smr  # noqa: E402

# The exact value pytdx returned for both halted stocks.
HALTED_AMOUNT = 5.877471754111438e-39


class DenormalizedAmountTests(unittest.TestCase):
    def test_the_artefact_is_not_zero(self):
        """Which is precisely why `amount <= 0` failed to catch it."""
        self.assertGreater(HALTED_AMOUNT, 0.0)
        self.assertFalse(HALTED_AMOUNT <= 0)

    def test_rounding_hid_it_in_the_published_file(self):
        """The record showed 0.0 while the comparison saw a positive number."""
        self.assertEqual(round(HALTED_AMOUNT, 2), 0.0)

    def test_the_floor_is_below_any_real_trade(self):
        """One yuan is far under an A-share round lot at any plausible price."""
        self.assertLessEqual(smr.MIN_MEANINGFUL_AMOUNT, 100.0)

    def test_the_floor_is_above_the_artefact(self):
        self.assertGreater(smr.MIN_MEANINGFUL_AMOUNT, HALTED_AMOUNT)

    def test_a_real_turnover_clears_the_floor(self):
        """000811 冰轮环境 on the same morning."""
        self.assertGreater(1831799680.0, smr.MIN_MEANINGFUL_AMOUNT)


class ClassificationTests(unittest.TestCase):
    """The four-way check as the gate applies it, with the fix in place."""

    @staticmethod
    def classify(open_price, last_price, amount, volume):
        amount = 0.0 if amount < smr.MIN_MEANINGFUL_AMOUNT else amount
        if open_price <= 0 and last_price <= 0 and amount <= 0 and volume <= 0:
            return "exclude_today_halt_or_no_open"
        if amount <= 0 and volume <= 0:
            return "exclude_today_zero_turnover_0931"
        return "tradable_today"

    def test_the_halted_stock_is_now_excluded(self):
        self.assertEqual(
            self.classify(0.0, 0.0, HALTED_AMOUNT, 0),
            "exclude_today_halt_or_no_open")

    def test_the_second_halted_stock_too(self):
        """600929 雪天盐业 carried the identical artefact."""
        self.assertEqual(
            self.classify(0.0, 0.0, HALTED_AMOUNT, 0),
            "exclude_today_halt_or_no_open")

    def test_without_the_fix_it_would_pass(self):
        """Pinning the defect: the raw comparison lets a halt through."""
        raw = (0.0 <= 0 and 0.0 <= 0 and HALTED_AMOUNT <= 0 and 0 <= 0)
        self.assertFalse(raw)

    def test_a_trading_stock_is_untouched(self):
        self.assertEqual(
            self.classify(39.13, 41.75, 1831799680.0, 447096),
            "tradable_today")

    def test_a_stock_that_opened_but_never_traded_is_still_caught(self):
        """An auction price with no turnover is not tradeable either."""
        self.assertEqual(
            self.classify(10.0, 10.0, HALTED_AMOUNT, 0),
            "exclude_today_zero_turnover_0931")

    def test_a_genuinely_tiny_turnover_is_treated_as_none(self):
        """Half a yuan of turnover is not a market to sell into."""
        self.assertNotEqual(self.classify(0.0, 0.0, 0.5, 0), "tradable_today")


class WiringTests(unittest.TestCase):
    """The gate already reaches the trader; only the classification was wrong."""

    def test_the_floor_is_applied_before_the_check(self):
        src = Path(smr.__file__).read_text(encoding="utf-8")
        norm = src.index("amount = 0.0 if amount < MIN_MEANINGFUL_AMOUNT")
        check = src.index("if open_price <= 0 and last_price <= 0")
        self.assertLess(norm, check)

    def test_the_exclusion_action_is_the_one_the_trader_reads(self):
        import market_resolver as mr
        payload = {"trade_date": mr._today_str(), "records": [
            {"code": "688432", "executor_action": "exclude_today_buy_sell",
             "tradability_status": "exclude_today_halt_or_no_open",
             "summary": "09:31 仍无有效开盘与成交"},
            {"code": "000811", "executor_action": "allow_today"},
        ]}
        excl = mr.build_today_exclusion_map(payload)
        self.assertIn("688432", excl)
        self.assertNotIn("000811", excl)
        self.assertTrue(mr.exclusion_reason_text(excl["688432"]))


if __name__ == "__main__":
    unittest.main()
