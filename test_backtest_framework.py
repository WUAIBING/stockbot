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


if __name__ == "__main__":
    unittest.main(verbosity=2)
