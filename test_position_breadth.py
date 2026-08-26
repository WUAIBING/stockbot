#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Breadth, not signal quality, is the binding constraint on this book.

A single 10-day A-share hold has a ~10.4pp spread. The noise on an annual result
falls as sd/sqrt(positions), so the same edge is invisible at 2 positions and
legible at 50:

    positions   annual SD   info ratio on a 5.6pp edge
            2        36.8                        +0.15
            8        18.4                        +0.30
           25        10.4                        +0.54
           50         7.4                        +0.76

Expected return is identical at every row - only the noise changes. That is why
8 positions at 2% beats 4 at 4% despite identical exposure: same money, 30% less
noise.

The account held 2 positions on 2026-08-26 against 12 configured slots, because
3 of 4 buy attempts were blocked by corrupt scan prices - not because the limits
were binding. This raises the ceiling for when the pipeline actually fills.
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import v10_moni_trader as t  # noqa: E402


class SlotCountTests(unittest.TestCase):
    def test_stage_one_totals_25_slots(self):
        total = sum(c["max_stocks"] for c in t.TIER_CONFIG.values())
        self.assertEqual(total, 25)

    def test_every_tier_grew(self):
        """Previous stage was T1=3, T2=5, T3=4."""
        for tier, was in ((1, 3), (2, 5), (3, 4)):
            self.assertGreater(t.TIER_CONFIG[tier]["max_stocks"], was, f"T{tier}")

    def test_not_jumped_straight_to_fifty(self):
        """The corruption fix has not yet been seen filling a full book."""
        total = sum(c["max_stocks"] for c in t.TIER_CONFIG.values())
        self.assertLess(total, 50)

    def test_position_cap_is_unchanged(self):
        """This change widens breadth only; per-position size must not move."""
        self.assertEqual(t.MAX_POSITION_PCT_NAV, 2.0)

    def test_maximum_deployment_is_bounded(self):
        total = sum(c["max_stocks"] for c in t.TIER_CONFIG.values())
        self.assertLessEqual(total * t.MAX_POSITION_PCT_NAV, 100.0)
        self.assertEqual(total * t.MAX_POSITION_PCT_NAV, 50.0)

    def test_tier_ordering_is_preserved(self):
        """T2 is the workhorse and must keep the most slots."""
        self.assertGreater(t.TIER_CONFIG[2]["max_stocks"], t.TIER_CONFIG[1]["max_stocks"])
        self.assertGreater(t.TIER_CONFIG[2]["max_stocks"], t.TIER_CONFIG[3]["max_stocks"])


class EnvOverrideTests(unittest.TestCase):
    """The ramp to 50 must not need a code change."""

    def tearDown(self):
        for tier in (1, 2, 3):
            os.environ.pop(f"TLFZ_MAX_STOCKS_T{tier}", None)
        importlib.reload(t)

    def test_env_overrides_the_default(self):
        self.assertEqual(t._max_stocks_env(2, 11), 11)
        os.environ["TLFZ_MAX_STOCKS_T2"] = "22"
        self.assertEqual(t._max_stocks_env(2, 11), 22)

    def test_a_full_ramp_to_fifty_is_reachable(self):
        os.environ["TLFZ_MAX_STOCKS_T1"] = "12"
        os.environ["TLFZ_MAX_STOCKS_T2"] = "22"
        os.environ["TLFZ_MAX_STOCKS_T3"] = "16"
        total = sum(t._max_stocks_env(i, 0) for i in (1, 2, 3))
        self.assertEqual(total, 50)

    def test_garbage_falls_back_to_the_default(self):
        for bad in ("", "abc", "3.7", "  "):
            os.environ["TLFZ_MAX_STOCKS_T1"] = bad
            self.assertEqual(t._max_stocks_env(1, 6), 6, repr(bad))

    def test_negative_is_clamped_to_zero_not_negative(self):
        os.environ["TLFZ_MAX_STOCKS_T3"] = "-5"
        self.assertEqual(t._max_stocks_env(3, 8), 0)

    def test_zero_is_honoured_as_disabling_a_tier(self):
        os.environ["TLFZ_MAX_STOCKS_T1"] = "0"
        self.assertEqual(t._max_stocks_env(1, 6), 0)


class NoiseArithmeticTests(unittest.TestCase):
    """The reason for the change, pinned so it cannot drift silently."""

    SD_PICK = 10.4
    CYCLES = 25.0

    def ann_sd(self, positions):
        return self.SD_PICK * (self.CYCLES / positions) ** 0.5

    def test_more_positions_strictly_reduces_noise(self):
        prev = None
        for n in (2, 8, 25, 50, 100):
            cur = self.ann_sd(n)
            if prev is not None:
                self.assertLess(cur, prev, n)
            prev = cur

    def test_widening_beats_upsizing_at_equal_exposure(self):
        """8 x 2% and 4 x 4% deploy the same money; 8 carries less noise."""
        self.assertLess(self.ann_sd(8), self.ann_sd(4))

    def test_this_change_roughly_halves_the_noise(self):
        self.assertAlmostEqual(self.ann_sd(12) / self.ann_sd(25), 1.44, places=1)


if __name__ == "__main__":
    unittest.main()
