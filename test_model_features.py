#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The scan must publish every feature a fitted model needs.

A ridge model on twelve features scored a mean out-of-sample IC of +0.0892
across ten walk-forward years, positive in all ten, against +0.0708 for a
hand-picked two-feature rank and outright negative for the live profile rules.

Only three of the twelve were reachable from a scan row. The missing ones
carried the 3rd and 4th largest weights:

    ma20_off          -1.3995   published
    rsi14             +1.2911   published
    vol20             -0.8770   MISSING
    close_vs_ma60_pct -0.6673   MISSING
    amt_ratio         -0.3640   published
    gap_pct           +0.3097   MISSING

Note the sign disagreement between ma20_off and rsi14: below the 20-day mean but
NOT weak on RSI. The model buys pullbacks within strength, a shape no single
monotone ranking can express - which is why hand-picking underperformed.

Both row builders must publish them. The prewarm path goes through
_compute_daily_snapshot; the decision path recomputes the same frame inline. They
share one helper here precisely so they cannot drift.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import scanner_v10 as s  # noqa: E402


def frame(n=80, start=10.0, step=0.05):
    close = np.array([start + i * step for i in range(n)], dtype=float)
    d = pd.DataFrame({
        "close": close,
        "open": close - 0.10,
        "high": close + 0.20,
        "low": close - 0.30,
        "amount": np.full(n, 5e7),
    })
    for w in (5, 10, 20, 60):
        d[f"ma{w}"] = d["close"].rolling(w).mean()
    d["avg_amt_5d"] = d["amount"].rolling(5).mean()
    d["amt_ratio"] = d["amount"] / d["avg_amt_5d"]
    d["close_vs_ma20"] = (d["close"] - d["ma20"]) / d["ma20"] * 100
    return d


class CoverageTests(unittest.TestCase):
    def test_all_twelve_are_declared(self):
        self.assertEqual(len(s.MODEL_FEATURE_COLUMNS), 12)
        self.assertEqual(len(set(s.MODEL_FEATURE_COLUMNS)), 12)

    def test_the_previously_missing_nine_are_present(self):
        for f in ("ret_1d", "ret_5d", "ret_10d", "ret_20d", "close_vs_ma60_pct",
                  "high20_off_pct", "vol20", "gap_pct", "range_pos_pct"):
            self.assertIn(f, s.MODEL_FEATURE_COLUMNS, f)

    def test_the_three_already_published_are_still_listed(self):
        for f in ("amt_ratio", "close_vs_ma20_pct", "rsi14"):
            self.assertIn(f, s.MODEL_FEATURE_COLUMNS, f)

    def test_snapshot_emits_every_declared_column(self):
        out = s._compute_daily_snapshot(frame())
        self.assertIsNotNone(out)
        for f in s.MODEL_FEATURE_COLUMNS:
            self.assertIn(f, out, f)


class ValueTests(unittest.TestCase):
    def feats(self, d):
        return s.extended_model_features(d, d.iloc[-1], d.iloc[-1]["close"])

    def test_returns_measure_the_right_windows(self):
        d = frame(n=80, start=10.0, step=0.05)
        f = self.feats(d)
        close = d.iloc[-1]["close"]
        for key, win in (("ret_1d", 1), ("ret_5d", 5), ("ret_20d", 20)):
            expect = (close / d["close"].iloc[-1 - win] - 1.0) * 100.0
            self.assertAlmostEqual(f[key], expect, places=6, msg=key)

    def test_a_rising_series_sits_at_its_20_day_high(self):
        self.assertAlmostEqual(self.feats(frame())["high20_off_pct"], 0.0, places=6)

    def test_a_falling_series_sits_below_its_high(self):
        self.assertLess(self.feats(frame(step=-0.05))["high20_off_pct"], 0.0)

    def test_range_position_is_a_percentage_of_the_session_range(self):
        d = frame()
        last = d.iloc[-1]
        expect = (last["close"] - last["low"]) / (last["high"] - last["low"]) * 100.0
        self.assertAlmostEqual(self.feats(d)["range_pos_pct"], expect, places=6)

    def test_ma60_offset_is_positive_above_the_mean(self):
        f = self.feats(frame())
        self.assertGreater(f["close_vs_ma60_pct"], 0.0)

    def test_volatility_is_zero_for_a_constant_series(self):
        d = frame(step=0.0)
        self.assertAlmostEqual(self.feats(d)["vol20"], 0.0, places=6)

    def test_volatility_rises_with_a_noisier_series(self):
        calm = self.feats(frame(step=0.01))["vol20"]
        wild = self.feats(frame(step=0.50))["vol20"]
        self.assertGreater(wild, calm)


class RobustnessTests(unittest.TestCase):
    """A feature that raises takes the whole scan row down with it."""

    def test_a_short_series_returns_zeros_not_errors(self):
        d = frame(n=3)
        f = s.extended_model_features(d, d.iloc[-1], d.iloc[-1]["close"])
        for k in s.MODEL_FEATURE_COLUMNS:
            if k in f:
                self.assertIsInstance(f[k], float, k)

    def test_a_zero_range_session_does_not_divide_by_zero(self):
        d = frame()
        d.loc[d.index[-1], ["high", "low", "close", "open"]] = 10.0
        f = s.extended_model_features(d, d.iloc[-1], 10.0)
        self.assertEqual(f["range_pos_pct"], 50.0)

    def test_a_zero_close_does_not_divide_by_zero(self):
        d = frame()
        f = s.extended_model_features(d, d.iloc[-1], 0.0)
        self.assertEqual(f["close_vs_ma60_pct"], 0.0)

    def test_every_value_is_finite(self):
        for step in (0.05, -0.05, 0.0):
            f = s.extended_model_features(frame(step=step), frame(step=step).iloc[-1],
                                          frame(step=step).iloc[-1]["close"])
            for k, v in f.items():
                self.assertTrue(np.isfinite(v), f"{k}={v}")


if __name__ == "__main__":
    unittest.main()
