#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Every mainline add must pass add_gates, and add_gates is off.

Buying a stock that has just risen +10% underperforms at every horizon across
2.0M liquid stock-sessions (-2.007% at 10 sessions, t-19.24). The live
大肉激进加仓 path added at about +4.5% and took three positions to 5-6.5% of
NAV in September. These tests pin the gate in front of the order list.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import add_gates  # noqa: E402
import v10_moni_trader as mt  # noqa: E402


def add_body():
    src = (SKILL / "v10_moni_trader.py").read_text(encoding="utf-8")
    start = src.index("def do_add_position(")
    return src[start:src.index("\ndef ", start + 10)]


class WiringTests(unittest.TestCase):
    def test_the_trader_imports_the_gate(self):
        self.assertIs(mt._add_gates, add_gates)

    def test_every_append_to_the_order_list_is_behind_the_gate(self):
        body = add_body()
        appends = [i for i in range(len(body)) if body.startswith("add_list.append(", i)]
        self.assertEqual(len(appends), 1, "a second route into add_list would bypass the gate")
        gate = body.index("_add_gates.evaluate_add(")
        refuse = body.index("if not gate_ok:")
        self.assertLess(gate, refuse)
        self.assertLess(refuse, appends[0])
        self.assertIn("continue", body[refuse:appends[0]])

    def test_the_gate_sees_broker_profit_and_trading_sessions(self):
        body = add_body()
        i = body.index("_add_gates.evaluate_add(")
        call = body[i:i + 200]
        self.assertIn("'profit_pct': profit_pct", call)
        self.assertIn("hold_sessions=_hold_sessions(code", call)

    def test_a_refusal_is_logged_with_its_reason(self):
        self.assertIn("跳过加仓: add_gates(", add_body())


class GateBehaviourTests(unittest.TestCase):
    def test_off_by_default_refuses_everything(self):
        with mock.patch.object(add_gates, "ADD_GATES_ENABLED", False):
            ok, checks = add_gates.evaluate_add({"code": "600657", "profit_pct": 30.0}, hold_sessions=10)
        self.assertFalse(ok)
        self.assertIn("disabled", checks[0][2])

    def test_the_september_adds_would_be_refused_even_if_enabled(self):
        """信达地产 was added at +4.5% on day 4 - both gates say no."""
        with mock.patch.object(add_gates, "ADD_GATES_ENABLED", True):
            ok, checks = add_gates.evaluate_add({"code": "600657", "profit_pct": 4.5}, hold_sessions=4)
        self.assertFalse(ok)
        failed = {c[0] for c in checks if not c[1]}
        self.assertEqual(failed, {"profit", "hold_window"})

    def test_the_enabled_exception_is_a_confirmed_runner_only(self):
        with mock.patch.object(add_gates, "ADD_GATES_ENABLED", True):
            ok, _ = add_gates.evaluate_add({"code": "600657", "profit_pct": 22.0}, hold_sessions=6)
        self.assertTrue(ok)

    def test_unknown_hold_length_is_refused(self):
        with mock.patch.object(add_gates, "ADD_GATES_ENABLED", True):
            ok, _ = add_gates.evaluate_add({"code": "600657", "profit_pct": 30.0}, hold_sessions=None)
        self.assertFalse(ok)


class SafetyTests(unittest.TestCase):
    def test_the_gate_module_holds_no_execution_path(self):
        src = (SKILL / "add_gates.py").read_text(encoding="utf-8")
        for bad in ("buy_stock", "sell_stock", "execute_trade_action", "mockTrading"):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
