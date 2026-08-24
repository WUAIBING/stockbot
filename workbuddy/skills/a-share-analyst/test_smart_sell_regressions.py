#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import unittest
from datetime import timedelta
from unittest.mock import patch

import pandas as pd

import v10_moni_trader as trader


class SmartSellRegressionTests(unittest.TestCase):
    def test_returns_runtime_error_when_empty_cache_conflicts_with_holding(self) -> None:
        records = [{"code": "000001", "name": "平安银行", "status": "holding", "date": "2026-07-15"}]
        with (
            patch.object(trader, "ensure_trade_window", return_value=True),
            patch.object(trader, "load_track_record", return_value=records),
            patch.object(trader, "get_positions", return_value=[]),
            patch.object(
                trader,
                "_get_last_positions_fetch_status",
                return_value={"source": "cache", "ok": False, "message": "cache fallback", "cache_age_seconds": 12},
            ),
            patch.object(trader, "load_pending_orders", return_value=[]),
            patch.object(
                trader,
                "summarize_pending_orders",
                return_value={"active_buy_codes": [], "active_sell_codes": [], "counts": {}},
            ),
            patch.object(trader, "get_balance", return_value={"avail_balance": 0}),
            patch.object(trader, "sync_track_record", return_value=(records, False)),
            patch.object(trader, "full_reconcile_positions", return_value=(records, False, {"imported_positions": 0, "overlaid_positions": 0})),
            patch.object(trader, "_debug_report_smart_sell"),
        ):
            code = trader.do_smart_sell(dry_run=False)

        self.assertEqual(code, trader.EXIT_RUNTIME_ERROR)


class SignalDecayEvidenceTests(unittest.TestCase):
    class FakeApi:
        def __init__(self, daily_rows):
            self.daily_rows = daily_rows

        def get_security_bars(self, category, market, code, start, count):
            if category == 9:
                return self.daily_rows
            return []

        @staticmethod
        def to_df(rows):
            return pd.DataFrame(rows)

    def _daily_rows(self):
        now = trader._market_now()
        return [
            {
                "datetime": now - timedelta(days=2),
                "open": 44.0,
                "high": 44.5,
                "close": 44.2,
                "amount": 100.0,
            },
            {
                "datetime": now - timedelta(days=1),
                "open": 44.2,
                "high": 45.2,
                "close": 45.0,
                "amount": 100.0,
            },
            {
                "datetime": now.replace(hour=10, minute=45, second=0, microsecond=0),
                "open": 46.0,
                "high": 46.28,
                "close": 45.0,
                "amount": 100.0,
            },
        ]

    def test_unfinished_daily_candle_deduplicates_shadow_and_open_drop(self) -> None:
        api = self.FakeApi(self._daily_rows())
        before_close = trader._market_now().replace(hour=10, minute=45, second=0, microsecond=0)
        with patch.object(trader, "_market_now", return_value=before_close):
            detail = trader.evaluate_signal_decay_detail(
                api,
                "688000",
                42.0,
                "vol_breakout",
                profit_pct=7.0,
            )

        self.assertEqual(detail["score"], 3.0)
        self.assertEqual(detail["provisional_score"], 3.0)
        self.assertEqual(detail["confirmed_score"], 0.0)
        self.assertTrue(detail["provisional_only"])
        candle_family = [item for item in detail["families"] if item["family"] == "daily_candle_reversal"]
        self.assertEqual(len(candle_family), 1)
        self.assertEqual(candle_family[0]["score"], 3.0)
        self.assertIn("冲高回落上影线", detail["reason"])
        self.assertIn("大阴线", detail["reason"])

    def test_completed_daily_candle_keeps_confirmed_score(self) -> None:
        api = self.FakeApi(self._daily_rows())
        after_close = trader._market_now().replace(hour=15, minute=1, second=0, microsecond=0)
        with patch.object(trader, "_market_now", return_value=after_close):
            detail = trader.evaluate_signal_decay_detail(
                api,
                "688000",
                42.0,
                "vol_breakout",
                profit_pct=7.0,
            )

        self.assertEqual(detail["score"], 3.0)
        self.assertEqual(detail["confirmed_score"], 3.0)
        self.assertEqual(detail["provisional_score"], 0.0)
        self.assertFalse(detail["provisional_only"])

    def test_legacy_decay_tuple_return_is_preserved(self) -> None:
        api = self.FakeApi(self._daily_rows())
        before_close = trader._market_now().replace(hour=10, minute=45, second=0, microsecond=0)
        with patch.object(trader, "_market_now", return_value=before_close):
            result = trader.evaluate_signal_decay(api, "688000", 42.0, "vol_breakout", profit_pct=7.0)

        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[2], 3.0)


class BigMeatCoreProtectionTests(unittest.TestCase):
    def _confirmed_record(self):
        return {
            "code": "688000",
            "status": "holding",
            "quantity": "1200",
            "big_meat_state": trader.BIG_MEAT_STATE_CONFIRMED,
            "big_meat_core_qty": "600",
            "big_meat_trade_qty": "600",
            "big_meat_hold_lock_until": "",
            "big_meat_last_eval_at": "2026-08-12 14:15:00",
        }

    def test_provisional_only_decay_can_trim_trade_qty_but_not_core(self) -> None:
        action = trader._resolve_big_meat_state_action(
            self._confirmed_record(),
            should_sell=True,
            decay_score=5.0,
            decay_reason="冲高回落上影线2.7% | 大阴线-2.0%",
            decay_detail={
                "provisional_only": True,
                "confirmed_score": 0.0,
                "has_confirmed_structural_break": False,
            },
            holding_profile={"profit_pct": 6.0},
        )

        self.assertEqual(action["action"], trader.BIG_MEAT_ACTION_RISK_TRIM)

    def test_same_day_provisional_decay_does_not_trim_twice(self) -> None:
        record = self._confirmed_record()
        record["big_meat_last_risk_trim_at"] = f"{trader._market_today()} 10:18:00"
        action = trader._resolve_big_meat_state_action(
            record,
            should_sell=True,
            decay_score=3.0,
            decay_reason="大阴线-2.1%",
            decay_detail={
                "provisional_only": True,
                "confirmed_score": 0.0,
                "has_confirmed_structural_break": False,
            },
            holding_profile={"profit_pct": 6.0},
        )

        self.assertEqual(action["action"], trader.BIG_MEAT_ACTION_HOLD_CORE)
        self.assertIn("不重复减仓", action["reason"])

    def test_completed_structural_break_can_bypass_core_lock(self) -> None:
        record = self._confirmed_record()
        record["big_meat_hold_lock_until"] = trader._market_today()
        action = trader._resolve_big_meat_state_action(
            record,
            should_sell=True,
            decay_score=3.0,
            decay_reason="周线slope转负(-1.0%)趋势终结",
            decay_detail={
                "provisional_only": False,
                "confirmed_score": 3.0,
                "has_confirmed_structural_break": True,
            },
            holding_profile={"profit_pct": 6.0},
        )

        self.assertEqual(action["action"], trader.BIG_MEAT_ACTION_HARD_EXIT)

    def test_mixed_provisional_and_soft_confirmed_evidence_does_not_clear_core(self) -> None:
        action = trader._resolve_big_meat_state_action(
            self._confirmed_record(),
            should_sell=True,
            decay_score=5.0,
            decay_reason="大阴线-2.3% | 放量滞涨",
            decay_detail={
                "provisional_only": False,
                "confirmed_score": 2.0,
                "provisional_score": 3.0,
                "has_confirmed_structural_break": False,
            },
            holding_profile={"profit_pct": 6.0},
        )

        self.assertEqual(action["action"], trader.BIG_MEAT_ACTION_RISK_TRIM)

    def test_holding_profile_updates_original_record(self) -> None:
        record = self._confirmed_record()
        returned = trader._apply_holding_big_meat_profile(
            record,
            profile={"holding_score": 4.0, "core_ratio": 0.5, "profit_pct": 6.0},
            hold_state=trader.BIG_MEAT_ACTION_RISK_TRIM,
        )

        self.assertIs(returned, record)
        self.assertEqual(record["big_meat_hold_state"], trader.BIG_MEAT_ACTION_RISK_TRIM)
        self.assertEqual(record["big_meat_core_qty"], "600")
        self.assertEqual(record["big_meat_trade_qty"], "600")


if __name__ == "__main__":
    unittest.main()
