#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Recognising a suspended stock from a feed that does not say so.

Every fixture is the live position feed taken while 688432 有研硅 was actually
halted on 2026-08-31, alongside a stock that was trading the same minute:

    688432 halted    dayProfit 0.0    dayProfitPct 0.0     delist 0
    000811 trading   dayProfit 380.0  dayProfitPct 1.8953  delist 0

There is no halt field. `delist` reads 0 for both. The only signal is a price
that has not moved at all, and that signal is ambiguous on its own - a stock can
close unchanged, and one locked at its limit also stops moving. So the tests
care most about the boundary between suspicion and confirmation.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import trading_halt as th  # noqa: E402


# Straight from the live feed, 2026-08-31.
HALTED = {"secCode": "688432", "secName": "有研硅", "count": 471,
          "availCount": 471, "value": 21298.62, "price": 4522, "priceDec": 2,
          "dayProfit": 0.0, "dayProfitPct": 0.0, "profit": -205.387,
          "profitPct": -0.9551, "delist": 0}

TRADING = {"secCode": "000811", "secName": "冰轮环境", "count": 500,
           "availCount": 500, "value": 20430.0, "price": 4086, "priceDec": 2,
           "dayProfit": 380.0, "dayProfitPct": 1.8953, "profit": 374.765,
           "profitPct": 1.8687, "delist": 0}


class SignalTests(unittest.TestCase):
    def test_the_halted_position_reads_flat(self):
        self.assertTrue(th.looks_suspended(HALTED)["suspicious"])

    def test_a_trading_position_does_not(self):
        self.assertFalse(th.looks_suspended(TRADING)["suspicious"])

    def test_delist_is_not_the_halt_flag(self):
        """Both read delist 0; using it would catch nothing."""
        self.assertEqual(HALTED["delist"], TRADING["delist"])

    def test_a_tiny_move_is_still_trading(self):
        p = dict(HALTED, dayProfitPct=0.01)
        self.assertFalse(th.looks_suspended(p)["suspicious"])

    def test_a_negative_move_is_still_trading(self):
        p = dict(HALTED, dayProfitPct=-2.5)
        self.assertFalse(th.looks_suspended(p)["suspicious"])

    def test_a_missing_field_is_not_read_as_flat(self):
        """Absent data must not be mistaken for a zero move."""
        p = {k: v for k, v in HALTED.items() if k != "dayProfitPct"}
        r = th.looks_suspended(p)
        self.assertFalse(r["suspicious"])
        self.assertIn("no day-move field", r["reason"])

    def test_every_verdict_explains_itself(self):
        for p in (HALTED, TRADING):
            self.assertTrue(th.looks_suspended(p)["reason"])


class ConfirmationTests(unittest.TestCase):
    """One flat reading is a suspicion. Two, or a dead order, is a halt."""

    def test_one_reading_is_not_enough(self):
        s = th.update_halt_state({}, HALTED, trade_date="2026-08-31")
        self.assertFalse(s["confirmed"])
        self.assertEqual(s["flat_observations"], 1)

    def test_a_second_flat_reading_confirms(self):
        s = th.update_halt_state({}, HALTED, trade_date="2026-08-31")
        s = th.update_halt_state(s, HALTED, trade_date="2026-08-31")
        self.assertTrue(s["confirmed"])
        self.assertIn("ticks", s["reason"])

    def test_a_rejected_order_confirms_immediately(self):
        """Ground truth beats inference: the order actually failed."""
        s = th.update_halt_state({}, HALTED, trade_date="2026-08-31",
                                 order_rejected=True)
        self.assertTrue(s["confirmed"])
        self.assertIn("unfilled", s["reason"])

    def test_a_single_tick_clears_everything(self):
        """A stock that moved is trading, whatever was believed a minute ago."""
        s = th.update_halt_state({}, HALTED, trade_date="2026-08-31")
        s = th.update_halt_state(s, HALTED, trade_date="2026-08-31")
        self.assertTrue(s["confirmed"])
        s = th.update_halt_state(s, TRADING, trade_date="2026-08-31")
        self.assertFalse(s["confirmed"])
        self.assertEqual(s["flat_observations"], 0)

    def test_state_resets_on_a_new_session(self):
        """A halt that ended overnight must not suppress today's orders."""
        s = th.update_halt_state({}, HALTED, trade_date="2026-08-31")
        s = th.update_halt_state(s, HALTED, trade_date="2026-08-31")
        self.assertTrue(s["confirmed"])
        s2 = th.update_halt_state(s, HALTED, trade_date="2026-09-01")
        self.assertEqual(s2["flat_observations"], 1)
        self.assertFalse(s2["confirmed"])

    def test_a_genuinely_unchanged_close_is_not_condemned_on_one_look(self):
        flat = dict(TRADING, dayProfitPct=0.0)
        s = th.update_halt_state({}, flat, trade_date="2026-08-31")
        self.assertFalse(s["confirmed"])


class OrderDecisionTests(unittest.TestCase):
    def test_a_confirmed_halt_stops_the_order(self):
        s = th.update_halt_state({}, HALTED, trade_date="2026-08-31",
                                 order_rejected=True)
        d = th.should_attempt_sell(s)
        self.assertFalse(d["attempt"])
        self.assertIn("suspended", d["reason"])

    def test_the_exit_intent_is_kept_not_cancelled(self):
        """Forgetting the exit turns a halt into an accidental long-term hold."""
        s = th.update_halt_state({}, HALTED, trade_date="2026-08-31",
                                 order_rejected=True)
        self.assertTrue(th.should_attempt_sell(s)["hold_intent"])

    def test_an_unconfirmed_suspicion_still_lets_the_order_through(self):
        """The first attempt is how a halt gets confirmed in the first place."""
        s = th.update_halt_state({}, HALTED, trade_date="2026-08-31")
        self.assertTrue(th.should_attempt_sell(s)["attempt"])

    def test_a_trading_stock_is_never_blocked(self):
        s = th.update_halt_state({}, TRADING, trade_date="2026-08-31")
        self.assertTrue(th.should_attempt_sell(s)["attempt"])

    def test_an_empty_state_permits_trading(self):
        self.assertTrue(th.should_attempt_sell({})["attempt"])
        self.assertTrue(th.should_attempt_sell(None)["attempt"])


class HoldDaysTests(unittest.TestCase):
    """MAX_HOLD_DAYS measures decay over TRADING sessions."""

    def test_a_suspended_session_does_not_age_the_position(self):
        s = th.update_halt_state({}, HALTED, trade_date="2026-08-31",
                                 order_rejected=True)
        self.assertFalse(th.hold_days_should_count(s))

    def test_a_trading_session_does(self):
        s = th.update_halt_state({}, TRADING, trade_date="2026-08-31")
        self.assertTrue(th.hold_days_should_count(s))

    def test_an_unknown_state_counts_normally(self):
        """Absent knowledge must not silently freeze the exit clock."""
        self.assertTrue(th.hold_days_should_count({}))
        self.assertTrue(th.hold_days_should_count(None))


class SummaryTests(unittest.TestCase):
    def test_it_names_what_is_suspended(self):
        a = th.update_halt_state({}, HALTED, trade_date="2026-08-31",
                                 order_rejected=True)
        b = th.update_halt_state({}, TRADING, trade_date="2026-08-31")
        out = th.summarise([a, b])
        self.assertEqual(out["suspended_count"], 1)
        self.assertEqual(out["suspended_codes"], ["688432"])

    def test_a_single_flat_reading_shows_as_watching(self):
        a = th.update_halt_state({}, HALTED, trade_date="2026-08-31")
        out = th.summarise([a])
        self.assertEqual(out["suspended_count"], 0)
        self.assertEqual(out["watching_count"], 1)


class SafetyTests(unittest.TestCase):
    def test_module_holds_no_execution_path(self):
        src = Path(th.__file__).read_text(encoding="utf-8")
        for forbidden in ("sell_stock", "buy_stock", "execute_trade_action",
                          "mockTrading", "requests", "urllib"):
            self.assertNotIn(forbidden, src, forbidden)


if __name__ == "__main__":
    unittest.main()
