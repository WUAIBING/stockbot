"""Tests for backtest_framework.py — shared plumbing for all T5 strategies."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"))

from backtest_framework import (
    BacktestEngine,
    StrategyConfig,
    STRATEGY_REGISTRY,
    normalize_code,
    register_strategy,
)
from backtest_strategies import ALL_STRATEGIES


class StrategyRegistryTests(unittest.TestCase):
    def test_all_eight_strategies_registered(self):
        self.assertGreaterEqual(len(STRATEGY_REGISTRY), 8)
        for name in ALL_STRATEGIES:
            self.assertIn(name, STRATEGY_REGISTRY)
            cfg = STRATEGY_REGISTRY[name]
            self.assertEqual(cfg.name, name)

    def test_v6_uses_500_top_n(self):
        self.assertEqual(STRATEGY_REGISTRY["v6"].top_n_amount, 500)

    def test_v7_uses_150_top_n(self):
        self.assertEqual(STRATEGY_REGISTRY["v7"].top_n_amount, 150)

    def test_v8_relaxed_thresholds(self):
        self.assertEqual(STRATEGY_REGISTRY["v8"].winner_thresh, 4.0)
        self.assertEqual(STRATEGY_REGISTRY["v8"].loser_thresh, -3.5)

    def test_v7_tighter_bollinger(self):
        self.assertEqual(STRATEGY_REGISTRY["v7"].bollinger_std_mult, 2.5)

    def test_v3_original_parameters(self):
        cfg = STRATEGY_REGISTRY["v3"]
        self.assertEqual(cfg.daily_bar_count, 600)
        self.assertEqual(cfg.weekly_bar_count, 80)
        self.assertEqual(cfg.ma_windows, (5, 10, 20))


class ConfigTests(unittest.TestCase):
    def test_default_config(self):
        cfg = StrategyConfig()
        self.assertEqual(cfg.name, "backtest")  # defaults from output_prefix
        self.assertEqual(cfg.top_n_amount, 200)

    def test_post_init_sets_name_from_output_prefix(self):
        cfg = StrategyConfig(output_prefix="test")
        self.assertEqual(cfg.name, "test")

    def test_explicit_name_overrides(self):
        cfg = StrategyConfig(name="explicit", output_prefix="ignored")
        self.assertEqual(cfg.name, "explicit")

    def test_register_strategy(self):
        cfg = register_strategy(StrategyConfig(name="test_cfg", top_n_amount=999))
        self.assertEqual(STRATEGY_REGISTRY["test_cfg"].top_n_amount, 999)


class FeatureComputationTests(unittest.TestCase):
    def setUp(self):
        np.random.seed(42)
        dates = pd.date_range("2025-01-01", periods=200, freq="B")
        self.mock_df = pd.DataFrame({
            "datetime": dates,
            "open": np.abs(np.random.randn(200).cumsum() + 100),
            "high": np.abs(np.random.randn(200).cumsum() + 102),
            "low": np.abs(np.random.randn(200).cumsum() + 98),
            "close": np.abs(np.random.randn(200).cumsum() + 100),
            "amount": np.abs(np.random.randn(200)) * 1e8 + 1e7,
            "volume": np.abs(np.random.randn(200)) * 1e6 + 1e5,
        })
        self.mock_df["close"] = self.mock_df["close"] + 50
        self.engine = BacktestEngine(STRATEGY_REGISTRY["v10"])

    def test_weekly_features_returns_dict(self):
        wfeats = BacktestEngine.compute_weekly_features(self.mock_df.head(60))
        self.assertIsInstance(wfeats, dict)
        self.assertIn("weekly_align", wfeats)
        self.assertIn("weekly_slope", wfeats)

    def test_weekly_features_empty_input(self):
        wfeats = BacktestEngine.compute_weekly_features(None)
        self.assertFalse(wfeats["weekly_align"])

    def test_daily_features_all_columns(self):
        wfeats = {"weekly_align": True, "weekly_slope": 1.5, "weekly_close_vs_wma20": 2.0, "weekly_ma10_slope": 0.5}
        result = self.engine.compute_daily_features(self.mock_df, wfeats)
        expected = ["ma5", "ma20", "rsi14", "amt_ratio", "bb_pct", "roc_5", "roc_10", "vol_expand", "ret_5d"]
        for col in expected:
            self.assertIn(col, result.columns, f"Missing: {col}")

    def test_daily_features_row_count_preserved(self):
        wfeats = {"weekly_align": False, "weekly_slope": 0.0, "weekly_close_vs_wma20": 0.0, "weekly_ma10_slope": 0.0}
        result = self.engine.compute_daily_features(self.mock_df, wfeats)
        self.assertEqual(len(result), 200)

    def test_v3_config_fewer_mas(self):
        engine_v3 = BacktestEngine(STRATEGY_REGISTRY["v3"])
        wfeats = {"weekly_align": False, "weekly_slope": 0.0, "weekly_close_vs_wma20": 0.0, "weekly_ma10_slope": 0.0}
        result = engine_v3.compute_daily_features(self.mock_df, wfeats)
        self.assertIn("ma5", result.columns)
        self.assertIn("ma20", result.columns)
        self.assertNotIn("ma60", result.columns)


class UtilityTests(unittest.TestCase):
    def test_cohens_d_large_effect(self):
        d = BacktestEngine.cohens_d(
            np.array([1.0, 2, 3, 4, 5]),
            np.array([6.0, 7, 8, 9, 10]),
        )
        self.assertLess(d, 0)

    def test_cohens_d_identical(self):
        d = BacktestEngine.cohens_d(np.array([1.0] * 10), np.array([1.0] * 10))
        self.assertEqual(d, 0.0)

    def test_cohens_d_small_sample(self):
        d = BacktestEngine.cohens_d(np.array([1.0, 2.0]), np.array([3.0, 4.0]))
        self.assertEqual(d, 0.0)

    def test_normalize_code_zfill(self):
        self.assertEqual(normalize_code("1"), "000001")

    def test_normalize_code_already_6_digit(self):
        self.assertEqual(normalize_code("600519"), "600519")

    def test_normalize_code_strips_market_suffix(self):
        self.assertEqual(normalize_code("000001.SZ"), "000001")

    def test_normalize_code_empty(self):
        self.assertEqual(normalize_code(""), "000000")


class EngineLifecycleTests(unittest.TestCase):
    def test_init_with_config(self):
        cfg = StrategyConfig(name="test", output_prefix="test")
        engine = BacktestEngine(cfg)
        self.assertEqual(engine.config.name, "test")
        self.assertIsNone(engine.api)

    def test_init_without_config(self):
        engine = BacktestEngine()
        self.assertIsNotNone(engine.config)

    def test_process_stock_no_api_returns_empty(self):
        engine = BacktestEngine()
        result = engine.process_stock("000001", "test", 0)
        self.assertEqual(result, [])




class WeeklyFeatureFrameTests(unittest.TestCase):
    """Weekly features must vary per week, not be one end-of-history snapshot.

    Broadcasting the snapshot gave every 2023 row the 2026 weekly state:
    look-ahead bias, and weekly_align constant per stock - which silently
    zeroed the seven v10 rules that gate on it.
    """

    def _weekly(self, n=60, start=10.0, step=0.5):
        return pd.DataFrame({
            "datetime": pd.date_range("2024-01-05", periods=n, freq="7D"),
            "close": [start + i * step for i in range(n)],
        })

    def test_returns_one_row_per_week(self):
        frame = BacktestEngine.compute_weekly_feature_frame(self._weekly(60))
        self.assertEqual(len(frame), 60)
        self.assertIn("week_end", frame.columns)

    def test_short_history_returns_empty_frame(self):
        self.assertTrue(BacktestEngine.compute_weekly_feature_frame(self._weekly(5)).empty)
        self.assertTrue(BacktestEngine.compute_weekly_feature_frame(None).empty)

    def test_align_is_true_in_a_steady_uptrend(self):
        frame = BacktestEngine.compute_weekly_feature_frame(self._weekly(60, step=0.5))
        self.assertTrue(bool(frame["weekly_align"].iloc[-1]))

    def test_align_is_false_in_a_steady_downtrend(self):
        frame = BacktestEngine.compute_weekly_feature_frame(
            self._weekly(60, start=50.0, step=-0.5))
        self.assertFalse(bool(frame["weekly_align"].iloc[-1]))

    def test_align_actually_varies_across_a_reversal(self):
        up = [10.0 + i * 0.5 for i in range(40)]
        down = [up[-1] - i * 0.5 for i in range(1, 41)]
        wdf = pd.DataFrame({
            "datetime": pd.date_range("2024-01-05", periods=80, freq="7D"),
            "close": up + down,
        })
        frame = BacktestEngine.compute_weekly_feature_frame(wdf)
        values = set(bool(x) for x in frame["weekly_align"])
        self.assertEqual(values, {True, False},
                         "weekly_align must change across a trend reversal")

    def test_daily_rows_get_the_last_completed_week_not_their_own(self):
        """A given day must not see weekly state from its own unfinished week."""
        wdf = self._weekly(60)
        engine = BacktestEngine()
        wfeats = engine.compute_weekly_features(wdf)
        wfeats["_frame"] = engine.compute_weekly_feature_frame(wdf)

        daily = pd.DataFrame({
            "datetime": pd.date_range("2024-06-03", periods=40, freq="D"),
            "open": [20.0] * 40, "high": [21.0] * 40,
            "low": [19.0] * 40, "close": [20.0] * 40,
            "vol": [1000.0] * 40, "amount": [20000.0] * 40,
        })
        out = engine.compute_daily_features(daily, wfeats)

        frame = wfeats["_frame"]
        checked = 0
        for i in range(len(out)):
            day = pd.Timestamp(out["datetime"].iloc[i])
            eligible = frame[frame["week_end"] < day]
            if eligible.empty:
                continue
            expected = eligible.iloc[-1]
            self.assertEqual(bool(out["weekly_align"].iloc[i]),
                             bool(expected["weekly_align"]),
                             "row %d (%s) took the wrong week" % (i, day.date()))
            checked += 1
        self.assertGreater(checked, 0, "test asserted nothing")

    def test_daily_features_no_longer_constant_per_stock(self):
        up = [10.0 + i * 0.5 for i in range(40)]
        down = [up[-1] - i * 0.5 for i in range(1, 41)]
        wdf = pd.DataFrame({
            "datetime": pd.date_range("2023-01-06", periods=80, freq="7D"),
            "close": up + down,
        })
        engine = BacktestEngine()
        wfeats = engine.compute_weekly_features(wdf)
        wfeats["_frame"] = engine.compute_weekly_feature_frame(wdf)
        daily = pd.DataFrame({
            "datetime": pd.date_range("2023-03-01", periods=400, freq="D"),
            "open": [20.0] * 400, "high": [21.0] * 400,
            "low": [19.0] * 400, "close": [20.0] * 400,
            "vol": [1000.0] * 400, "amount": [20000.0] * 400,
        })
        out = engine.compute_daily_features(daily, wfeats)
        self.assertGreater(out["weekly_slope"].nunique(), 1,
                           "weekly_slope must vary across a daily history")


class IntradayBuyZoneTests(unittest.TestCase):
    """bz_direction / bz_rt_direction gate seven of the eleven v10 rules."""

    def _bars(self, closes, hours, minutes):
        return pd.DataFrame({
            "datetime": pd.to_datetime(["2026-08-14 09:30:00"] * len(closes)),
            "hour": hours, "minute": minutes,
            "open": closes, "close": closes, "vol": [100.0] * len(closes),
        })

    def test_empty_input_is_missing_not_neutral(self):
        """Absent 5-minute data must be None, never 0.0.

        This test previously asserted 0.0 - encoding the bug. A 0.0 default
        silently satisfies bz_rt_min: -0.2 (0.0 >= -0.2), so rules gating on
        the buy zone matched thousands of records where it was never observed.
        pytdx serves ~25 sessions of 5-minute history against 3+ years of daily
        bars, so this is the common case, not an edge case.
        """
        feats = BacktestEngine.compute_intraday_buy_zone(None)
        self.assertIsNone(feats["bz_direction"])
        self.assertIsNone(feats["bz_rt_direction"])

    def test_too_few_bars_is_also_missing(self):
        """One bar in the window is not enough to compute a direction."""
        bars = self._bars([100.0], [14], [30])
        feats = BacktestEngine.compute_intraday_buy_zone(bars)
        self.assertIsNone(feats["bz_direction"])

    def test_rising_buy_zone_is_positive(self):
        bars = self._bars([100.0, 101.0, 102.0, 103.0],
                          [14, 14, 14, 14], [30, 40, 50, 55])
        self.assertGreater(BacktestEngine.compute_intraday_buy_zone(bars)["bz_direction"], 0)

    def test_falling_buy_zone_is_negative(self):
        bars = self._bars([100.0, 99.0, 98.0, 97.0],
                          [14, 14, 14, 14], [30, 40, 50, 55])
        self.assertLess(BacktestEngine.compute_intraday_buy_zone(bars)["bz_direction"], 0)

    def test_rt_window_stops_at_1450(self):
        """bz_rt covers 14:30-14:50; a 14:55 bar must not affect it."""
        bars = self._bars([100.0, 102.0, 104.0, 200.0],
                          [14, 14, 14, 14], [30, 40, 50, 55])
        feats = BacktestEngine.compute_intraday_buy_zone(bars)
        self.assertAlmostEqual(feats["bz_rt_direction"], 4.0, places=6)

    def test_bars_outside_the_buy_zone_are_ignored(self):
        bars = self._bars([50.0, 100.0, 101.0, 102.0],
                          [10, 14, 14, 14], [15, 30, 40, 50])
        feats = BacktestEngine.compute_intraday_buy_zone(bars)
        self.assertAlmostEqual(feats["bz_direction"], 2.0, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
