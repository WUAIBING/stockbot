#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Moving an exit's execution, never its decision.

On exit days the account fills -1.90% below that day's open (n=122, t=-8.15),
and the loss is flat across the day - 09:45 fills are already down 1.94%, so
running the sweep earlier recovers nothing. The gap closes in the first minutes.

The tempting rule - take the gap-up open whenever above cost - was measured and
loses: -1.85pp at any gap (t=-1.68), -6.35pp at a +5% trigger (t=-3.47) where
every such trade had been a winner. 64% of gap-ups do fade, giving back -2.66pp,
but the 36% that run gain +10.00pp and carry the whole 34.7%/3.4:1 arithmetic.

So this module only executes exits already determined at yesterday's close. The
clean slice measures on its own: exits at T+10 or beyond fill -2.31% below the
open (n=19, t=-4.29), and MAX_HOLD_DAYS is known a day ahead.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import open_exit as oe  # noqa: E402

POS = [
    {"code": "002396", "name": "星网锐捷", "count": 700},
    {"code": "688432", "name": "有研硅", "count": 471},
    {"code": "600403", "name": "大有能源", "count": 3000},
]


def precommit(codes_reasons, trade_date="2026-09-02"):
    return {"trade_date": trade_date, "max_hold_days": 10,
            "entries": [{"code": c, "quantity": 100, "reason": r}
                        for c, r in codes_reasons]}


class PredeterminedTests(unittest.TestCase):
    """Known at yesterday's close, or it does not qualify."""

    def test_the_session_before_the_limit_qualifies(self):
        self.assertTrue(oe.exit_is_predetermined(9, 10))

    def test_already_past_the_limit_qualifies(self):
        self.assertTrue(oe.exit_is_predetermined(12, 10))

    def test_a_position_with_room_left_does_not(self):
        self.assertFalse(oe.exit_is_predetermined(8, 10))
        self.assertFalse(oe.exit_is_predetermined(2, 10))

    def test_unknown_hold_length_does_not_qualify(self):
        """A halted name can have no session count; absence is not a reason."""
        self.assertFalse(oe.exit_is_predetermined(None, 10))
        self.assertFalse(oe.exit_is_predetermined("", 10))

    def test_a_nonsense_limit_qualifies_nothing(self):
        self.assertFalse(oe.exit_is_predetermined(9, 0))
        self.assertFalse(oe.exit_is_predetermined(9, -1))


class BuildTests(unittest.TestCase):
    def test_only_positions_at_the_limit_are_listed(self):
        p = oe.build_precommit(POS, {"002396": 9, "688432": 2, "600403": 3},
                               10, "2026-09-02")
        self.assertEqual([e["code"] for e in p["entries"]], ["002396"])
        self.assertEqual(p["entries"][0]["reason"], oe.REASON_MAX_HOLD_DUE)

    def test_a_close_flag_also_precommits(self):
        p = oe.build_precommit(POS, {}, 10, "2026-09-02",
                               flagged_codes=["600403"])
        self.assertEqual([e["code"] for e in p["entries"]], ["600403"])
        self.assertEqual(p["entries"][0]["reason"], oe.REASON_FLAGGED_AT_CLOSE)

    def test_a_zero_quantity_holding_is_skipped(self):
        p = oe.build_precommit([{"code": "002396", "count": 0}],
                               {"002396": 9}, 10, "2026-09-02")
        self.assertEqual(p["entries"], [])

    def test_the_list_is_stamped_for_the_session_it_is_for(self):
        p = oe.build_precommit(POS, {"002396": 9}, 10, "2026-09-02")
        self.assertEqual(p["trade_date"], "2026-09-02")

    def test_an_empty_book_builds_an_empty_list_not_an_error(self):
        self.assertEqual(oe.build_precommit([], {}, 10, "2026-09-02")["entries"], [])


class StaleListTests(unittest.TestCase):
    """The dangerous failure: acting on yesterday's reasoning."""

    def setUp(self):
        self.p = Path(tempfile.mkdtemp()) / "precommit.json"

    def test_a_list_for_another_session_is_refused(self):
        oe.save_precommit(oe.build_precommit(POS, {"002396": 9}, 10, "2026-09-01"),
                          str(self.p))
        self.assertIsNone(oe.load_precommit(str(self.p), "2026-09-02"))

    def test_the_list_for_this_session_loads(self):
        oe.save_precommit(oe.build_precommit(POS, {"002396": 9}, 10, "2026-09-02"),
                          str(self.p))
        got = oe.load_precommit(str(self.p), "2026-09-02")
        self.assertEqual([e["code"] for e in got["entries"]], ["002396"])

    def test_a_missing_file_is_none_not_an_error(self):
        self.assertIsNone(oe.load_precommit("/nonexistent/p.json", "2026-09-02"))

    def test_unreadable_content_is_none_not_a_guess(self):
        self.p.write_text("{ not json", encoding="utf-8")
        self.assertIsNone(oe.load_precommit(str(self.p), "2026-09-02"))


class GateTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, oe, "OPEN_EXIT_ENABLED", oe.OPEN_EXIT_ENABLED)

    def test_the_gate_is_off_by_default(self):
        import os
        self.assertNotIn(os.environ.get("TLFZ_OPEN_EXIT", "0").lower(),
                         ("1", "true", "yes", "on"))

    def test_nothing_sells_while_the_gate_is_off(self):
        oe.OPEN_EXIT_ENABLED = False
        ok, why = oe.should_sell_at_open(
            "002396", precommit([("002396", oe.REASON_MAX_HOLD_DUE)]),
            tradable_codes=["002396"])
        self.assertFalse(ok)
        self.assertIn("disabled", why)

    def test_a_precommitted_tradable_name_sells(self):
        oe.OPEN_EXIT_ENABLED = True
        ok, why = oe.should_sell_at_open(
            "002396", precommit([("002396", oe.REASON_MAX_HOLD_DUE)]),
            tradable_codes=["002396"])
        self.assertTrue(ok)
        self.assertEqual(why, oe.REASON_MAX_HOLD_DUE)

    def test_a_halted_name_is_never_ordered(self):
        """688432 有研硅: suspended since 2026-08-31, no reopen date, sits in
        excluded_today_codes. A standing market-on-open order against it would
        be an order that cannot fill."""
        oe.OPEN_EXIT_ENABLED = True
        ok, why = oe.should_sell_at_open(
            "688432", precommit([("688432", oe.REASON_MAX_HOLD_DUE)]),
            tradable_codes=["002396", "600403"])
        self.assertFalse(ok)
        self.assertIn("not tradable", why)

    def test_a_name_not_on_the_list_does_not_sell(self):
        oe.OPEN_EXIT_ENABLED = True
        ok, _ = oe.should_sell_at_open(
            "600403", precommit([("002396", oe.REASON_MAX_HOLD_DUE)]),
            tradable_codes=["600403"])
        self.assertFalse(ok)

    def test_an_unrecognised_reason_is_refused(self):
        """The list is a file on disk; a reason nobody wrote must not execute."""
        oe.OPEN_EXIT_ENABLED = True
        ok, why = oe.should_sell_at_open(
            "002396", precommit([("002396", "because_i_said_so")]),
            tradable_codes=["002396"])
        self.assertFalse(ok)
        self.assertIn("unrecognised", why)

    def test_no_list_means_no_sale(self):
        oe.OPEN_EXIT_ENABLED = True
        self.assertFalse(oe.should_sell_at_open("002396", None)[0])
        self.assertFalse(oe.should_sell_at_open("002396", {})[0])

    def test_a_missing_tradability_verdict_does_not_block(self):
        """None means the gate was not supplied, not that nothing is tradable.
        Callers that have the gate must pass it; this is the documented default
        so a caller without it is not silently disarmed."""
        oe.OPEN_EXIT_ENABLED = True
        ok, _ = oe.should_sell_at_open(
            "002396", precommit([("002396", oe.REASON_MAX_HOLD_DUE)]),
            tradable_codes=None)
        self.assertTrue(ok)


class SafetyTests(unittest.TestCase):
    def test_the_module_places_no_orders(self):
        src = Path(oe.__file__).read_text(encoding="utf-8")
        for bad in ("buy_stock", "sell_stock", "execute_trade_action",
                    "mockTrading/trade", "mockTrading/cancel", "requests.post"):
            self.assertNotIn(bad, src)

    def test_it_never_decides_to_exit_only_when_to_execute_one(self):
        """No market data reaches the decision path.

        If a price, indicator or score could steer these functions, this would
        be an exit RULE rather than an execution move - and exit rules of that
        shape measured badly: taking the gap-up open cost -1.85pp overall and
        -6.35pp at a +5% trigger.

        Checked over the parsed AST rather than the text, because the prose
        legitimately says "yesterday's close" and one reason is literally
        named REASON_FLAGGED_AT_CLOSE. Grepping the source would fail on its
        own documentation.
        """
        import ast
        tree = ast.parse(Path(oe.__file__).read_text(encoding="utf-8"))
        targets = {"exit_is_predetermined", "build_precommit",
                   "should_sell_at_open", "load_precommit"}
        banned = {"price", "last_price", "cost_price", "profit_pct", "rsi14",
                  "ma20", "close_vs_ma20_pct", "score", "model_score",
                  "amt_ratio", "weekly_slope"}
        seen = set()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef) and node.name in targets):
                continue
            body = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                                     and isinstance(node.body[0].value, ast.Constant)
                                     and isinstance(node.body[0].value.value, str)
                                     ) else node.body
            for sub in body:
                for n in ast.walk(sub):
                    if isinstance(n, ast.Name):
                        seen.add(n.id)
                    elif isinstance(n, ast.Attribute):
                        seen.add(n.attr)
                    elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                        seen.add(n.value)
        self.assertEqual(seen & banned, set(),
                         "market data reached the decision path: %s" % (seen & banned))


if __name__ == "__main__":
    unittest.main()
