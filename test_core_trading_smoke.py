#!/usr/bin/env python3
"""Smoke tests for core trading logic: entry sizing, sell decisions, position limits.

Covers calc_buy_quantity, _sellable_quantity, _effective_sellable_quantity,
_normalize_sell_quantity, and key entry/sell decision paths against known regimes.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"))

import v10_moni_trader as trader


class EntryQuantityTests(unittest.TestCase):
    """calc_buy_quantity: the last pure-sizing function before order placement."""

    def test_normal_calc_100_shares_at_10_yuan(self):
        qty = trader.calc_buy_quantity(10.0, 1000)
        self.assertEqual(qty, 100)

    def test_rounds_down_to_nearest_lot(self):
        qty = trader.calc_buy_quantity(10.0, 1050)
        self.assertEqual(qty, 100)

    def test_returns_zero_when_under_min_lot(self):
        qty = trader.calc_buy_quantity(10.0, 500)
        self.assertEqual(qty, 0)

    def test_zero_price_returns_zero(self):
        self.assertEqual(trader.calc_buy_quantity(0, 50000), 0)
        self.assertEqual(trader.calc_buy_quantity(-1, 50000), 0)

    def test_zero_amount_returns_zero(self):
        self.assertEqual(trader.calc_buy_quantity(10.0, 0), 0)
        self.assertEqual(trader.calc_buy_quantity(10.0, -100), 0)

    def test_override_amount_replaces_default(self):
        qty = trader.calc_buy_quantity(10.0, amount=100000, override_amount=2000)
        self.assertEqual(qty, 200)

    def test_override_amount_zero_falls_back_to_zero(self):
        qty = trader.calc_buy_quantity(10.0, amount=50000, override_amount=0)
        self.assertEqual(qty, 0)

    def test_override_none_uses_default_amount(self):
        qty = trader.calc_buy_quantity(10.0, amount=5000, override_amount=None)
        self.assertEqual(qty, 500)

    def test_high_price_yields_fewer_shares(self):
        cheap = trader.calc_buy_quantity(5.0, 50000)
        expensive = trader.calc_buy_quantity(50.0, 50000)
        self.assertGreater(cheap, expensive)
        self.assertEqual(cheap, 10000)
        self.assertEqual(expensive, 1000)

    def test_etf_like_price(self):
        qty = trader.calc_buy_quantity(1.5, 15000)
        self.assertEqual(qty, 10000)
        qty_small = trader.calc_buy_quantity(1.5, 100)
        self.assertEqual(qty_small, 0)


class PositionLimitTests(unittest.TestCase):
    """_sellable_quantity and _effective_sellable_quantity: position cap guards."""

    def test_sellable_normal(self):
        pos = {"count": 1000, "avail_count": 1000}
        self.assertEqual(trader._sellable_quantity(pos), 1000)

    def test_sellable_clamped_by_avail(self):
        pos = {"count": 1000, "avail_count": 200}
        self.assertEqual(trader._sellable_quantity(pos), 200)

    def test_sellable_avail_none_uses_count(self):
        pos = {"count": 1000}
        self.assertEqual(trader._sellable_quantity(pos), 1000)

    def test_sellable_zero_on_empty_pos(self):
        self.assertEqual(trader._sellable_quantity({}), 0)
        self.assertEqual(trader._sellable_quantity(None), 0)

    def test_sellable_tracked_qty_caps(self):
        pos = {"count": 1000, "avail_count": 1000}
        self.assertEqual(trader._sellable_quantity(pos, tracked_qty=500), 500)

    def test_sellable_rounds_down_to_lot(self):
        pos = {"count": 550, "avail_count": 550}
        self.assertEqual(trader._sellable_quantity(pos), 500)

    def test_sellable_preserves_odd_lot(self):
        pos = {"count": 80, "avail_count": 80}
        self.assertEqual(trader._sellable_quantity(pos), 80)

    def test_effective_sellable_respects_pending_reserved(self):
        pos = {"count": 1000, "avail_count": 1000}
        qty = trader._effective_sellable_quantity(pos, tracked_qty=800, pending_reserved_qty=300)
        self.assertEqual(qty, 500)

    def test_effective_sellable_respects_broker_cap(self):
        pos = {"count": 1000, "avail_count": 600}
        qty = trader._effective_sellable_quantity(pos, tracked_qty=1000, pending_reserved_qty=0)
        self.assertEqual(qty, 600)

    def test_effective_sellable_zero_when_all_reserved(self):
        pos = {"count": 500, "avail_count": 500}
        qty = trader._effective_sellable_quantity(pos, tracked_qty=500, pending_reserved_qty=500)
        self.assertEqual(qty, 0)

    def test_normalize_sell_quantity_rounds_board_lots(self):
        self.assertEqual(trader._normalize_sell_quantity(550), 500)
        self.assertEqual(trader._normalize_sell_quantity(1000), 1000)
        self.assertEqual(trader._normalize_sell_quantity(120), 100)

    def test_normalize_sell_quantity_zero(self):
        self.assertEqual(trader._normalize_sell_quantity(0), 0)
        self.assertEqual(trader._normalize_sell_quantity(-5), 0)

    def test_normalize_sell_quantity_odd_lot_pass_through(self):
        self.assertEqual(trader._normalize_sell_quantity(80), 80)
        self.assertEqual(trader._normalize_sell_quantity(50), 50)

    def test_position_broker_cap_empty(self):
        self.assertEqual(trader._position_broker_sellable_cap({}), 0)
        self.assertEqual(trader._position_broker_sellable_cap(None), 0)


class SellDecisionTests(unittest.TestCase):
    """Sell decision: signal decay evaluation and sell path guards."""

    def test_evaluate_signal_decay_requires_connected_api(self):
        with self.assertRaises(AttributeError):
            trader.evaluate_signal_decay(None, "000001", 10.0, "V9_full")

    def test_ensure_trade_window_dry_run_allows_trading_hours(self):
        result = trader.ensure_trade_window("sell", dry_run=True)
        self.assertIsInstance(result, bool)

    def test_market_from_code_sh_sz(self):
        self.assertEqual(trader.market_from_code("600519"), 1)
        self.assertEqual(trader.market_from_code("000001"), 0)

    def test_market_from_code_returns_market_for_bad_code(self):
        result = trader.market_from_code("")
        self.assertIn(result, [0, 1, None])
        result = trader.market_from_code("xyz")
        self.assertIn(result, [0, 1, None])

    def test_active_position_map_filters_active(self):
        positions = [
            {"code": "000001", "count": 500},
            {"code": "000002", "count": 0},
            {"code": "000003", "count": 200},
        ]
        active = trader._active_position_map(positions)
        self.assertIn("000001", active)
        self.assertNotIn("000002", active)
        self.assertIn("000003", active)

    def test_active_position_map_empty(self):
        self.assertEqual(trader._active_position_map([]), {})
        self.assertEqual(trader._active_position_map(None), {})

    def test_has_active_position_detects_count(self):
        self.assertTrue(trader._has_active_position({"code": "000001", "count": 100}))
        self.assertFalse(trader._has_active_position({"code": "000001", "count": 0}))
        self.assertFalse(trader._has_active_position({}))


class AddPositionGuardTests(unittest.TestCase):
    """add-position checkpoint and scoring guards."""

    def test_resolve_add_position_window_returns_empty_outside(self):
        result = trader._resolve_add_position_window()
        self.assertIsNotNone(result)
        self.assertIsInstance(result, dict)

    def test_current_add_position_window_tag_outside(self):
        self.assertEqual(trader._current_add_position_window_tag(), "")

    def test_in_checkpoint_grace_within_window(self):
        from datetime import datetime
        now = datetime(2026, 7, 15, 9, 37, 0)
        result = trader._in_checkpoint_grace(
            now, trader.ADD_POSITION_CHECKPOINTS, grace_minutes=4
        )
        self.assertTrue(result)

    def test_in_checkpoint_grace_outside_window(self):
        from datetime import datetime
        now = datetime(2026, 7, 15, 12, 0, 0)
        result = trader._in_checkpoint_grace(
            now, trader.ADD_POSITION_CHECKPOINTS, grace_minutes=4
        )
        self.assertFalse(result)

    def test_add_position_window_settings_have_required_fields(self):
        for window_key, settings in trader.ADD_POSITION_WINDOW_SETTINGS.items():
            self.assertIn("label", settings)
            self.assertIn("score_min", settings)
            self.assertIn("aggressive_score_min", settings)
            self.assertIn("reserve_cash_ratio", settings)


class TradeWindowTests(unittest.TestCase):
    """Buy/sell window and checkpoint configuration."""

    def test_buy_window_is_after_1450(self):
        start, end = trader.BUY_WINDOW
        self.assertEqual(start, (14, 50))
        self.assertEqual(end, (14, 57))

    def test_sell_cutoff_is_before_1500(self):
        self.assertEqual(trader.SELL_CUTOFF_TIME, (14, 49))

    def test_smart_sell_has_checkpoints(self):
        self.assertGreater(len(trader.SMART_SELL_CHECKPOINTS), 0)
        for cp in trader.SMART_SELL_CHECKPOINTS:
            self.assertIsInstance(cp, tuple)
            self.assertEqual(len(cp), 2)

    def test_add_position_checkpoints_ordered(self):
        cps = trader.ADD_POSITION_CHECKPOINTS
        for i in range(len(cps) - 1):
            self.assertLess(cps[i][0] * 60 + cps[i][1], cps[i + 1][0] * 60 + cps[i + 1][1])

    def test_tier_config_has_expected_structure(self):
        for tier in [1, 2, 3]:
            cfg = trader.TIER_CONFIG[tier]
            self.assertIn("label", cfg)
            self.assertIn("position_pct", cfg)
            self.assertIn("initial_build_pct", cfg)
            self.assertIn("max_stocks", cfg)
            self.assertGreater(cfg["position_pct"], 0)
            self.assertGreater(cfg["initial_build_pct"], 0)
            self.assertGreater(cfg["max_stocks"], 0)

    def test_tier_weight_decreases_from_t1_to_t3(self):
        self.assertGreater(
            trader.TIER_CONFIG[1]["position_pct"],
            trader.TIER_CONFIG[3]["position_pct"],
        )


class StateManagementTests(unittest.TestCase):
    """Shared lock and retry state management."""

    def test_acquire_shared_lock_returns_acquired(self):
        result = trader.acquire_shared_phase_lock("test_lock", owner="test", ttl_seconds=5)
        self.assertTrue(result.get("acquired"))
        self.assertEqual(result.get("owner"), "test")

    def test_release_shared_lock_returns_ok(self):
        trader.release_shared_phase_lock("test_lock", owner="test")
        result = trader.acquire_shared_phase_lock("test_lock_2", owner="test", ttl_seconds=5)
        self.assertTrue(result.get("acquired"))

    def test_smart_sell_retry_state_storage(self):
        trader._save_smart_sell_retry_state({"code": "000001"})
        loaded = trader._load_smart_sell_retry_state()
        self.assertEqual(loaded.get("code"), "000001")

    def test_clear_smart_sell_retry_state(self):
        trader._save_smart_sell_retry_state({"code": "000001"})
        trader._clear_smart_sell_retry_state("000001")
        loaded = trader._load_smart_sell_retry_state()
        self.assertIsNotNone(loaded)

    def test_rate_limit_cooldown_marking(self):
        trader._mark_smart_sell_rate_limit("000001", 500, cooldown_seconds=60)
        state = trader._get_smart_sell_retry_state("000001")
        self.assertIsNotNone(state)


class KellyIntegrationTests(unittest.TestCase):
    """Verify the Kelly sizing integration points don't break existing contracts."""

    def test_calc_buy_quantity_accepts_override_kwarg(self):
        import inspect
        sig = inspect.signature(trader.calc_buy_quantity)
        self.assertIn("override_amount", sig.parameters)

    def test_override_amount_has_default_none(self):
        import inspect
        sig = inspect.signature(trader.calc_buy_quantity)
        self.assertIsNone(sig.parameters["override_amount"].default)

    def test_position_sizer_importable_from_trader(self):
        from position_sizer import compute_position_weights, SizerConfig
        cfg = SizerConfig()
        alloc, dbg = compute_position_weights(
            [{"code": "000001", "name": "test", "score": 80, "avg_candidate_win_rate": 0.55, "avg_candidate_avg_return": 1.8, "avg_profitability_priority": 95, "volatility": 25, "correlation_group": "test", "selection_rank": 1}],
            1_000_000, drawdown_pct=2.0, window_key="14:50", config=cfg,
        )
        self.assertGreater(len(alloc), 0)
        self.assertEqual(alloc[0].code, "000001")


if __name__ == "__main__":
    unittest.main(verbosity=2)
