#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Replacing recorded prices with what the broker actually filled.

Checked against the broker's own orders, 19 of 29 episodes carried a wrong entry
price - two thirds - and only three were impossible against the tape. The rest
were plausible and wrong, which is why nothing noticed for months:

    688205 德科立   22.92 -> 155.65   +557.07% -> -3.31%
    601609 金田股份  11.89 ->  13.34     +9.50% -> -2.25%   a winner was a loser
    000676 智度股份   7.72 ->   7.04    -10.23% -> -1.42%

The correction runs BEFORE the summary, because the summary averages pnl_pct and
those two 德科立 rows moved the reported average from -0.77% to +38.32%.

SCOPE IS DELIBERATELY NARROW. The archive holds 122 round trips against this
file's 29, but adding the missing 93 would change what the file means to
everything downstream, and dropping an unmatched episode would change the counts
the learning layer sizes its samples by. So: fix the numbers, mark what could
not be checked, add and remove nothing.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import v10_moni_trader as trader  # noqa: E402


def broker_order(code, side, raw_price, count, ts, dec=2, oid=None, name=""):
    return {"id": oid or ("o%d" % ts), "status": 4, "time": ts,
            "secCode": code, "secMkt": 1, "drt": 1 if side == "buy" else 2,
            "priceDec": dec, "price": raw_price, "count": count,
            "tradeCount": count, "tradePrice": raw_price, "secName": name}


# The real 德科立 round trip: 400 bought at 155.65, closed 200 and 200.
DEKELI_ORDERS = [
    broker_order("688205", "buy", 15565, 400, 1787554272, name="德科立"),
    broker_order("688205", "sell", 15050, 200, 1787622316, name="德科立"),
    broker_order("688205", "sell", 15234, 200, 1787724929, name="德科立"),
]


class Harness(unittest.TestCase):
    def arm(self, orders, supplement=None):
        d = Path(tempfile.mkdtemp())
        archive = d / "orders.json"
        archive.write_text(json.dumps({"orders": orders}), encoding="utf-8")
        supp = d / "supplement.json"
        supp.write_text(json.dumps(supplement or {}), encoding="utf-8")
        self.addCleanup(setattr, trader, "MX_ORDERS_ARCHIVE_FILE",
                        trader.MX_ORDERS_ARCHIVE_FILE)
        self.addCleanup(setattr, trader, "MX_LEDGER_SUPPLEMENT_FILE",
                        trader.MX_LEDGER_SUPPLEMENT_FILE)
        trader.MX_ORDERS_ARCHIVE_FILE = str(archive)
        trader.MX_LEDGER_SUPPLEMENT_FILE = str(supp)
        return d

    def episode(self, **kw):
        base = {"code": "688205", "name": "德科立", "entry_price": 22.92,
                "sell_price": 150.60, "pnl_pct": 557.07, "quantity": 200,
                "sell_date": "2026-08-25", "buy_date": "2026-08-24",
                "close_reason": "risk_trim[连跌2日]", "mode": "pre_breakout",
                "tier": 2}
        base.update(kw)
        return base


class CorrectionTests(Harness):
    def test_the_fabricated_557_percent_is_replaced_by_the_real_loss(self):
        self.arm(DEKELI_ORDERS)
        eps = [self.episode()]
        res = trader._correct_episodes_from_broker_fills(eps)
        self.assertEqual(res["reason"], "ok")
        self.assertEqual(res["corrected"], 1)
        self.assertAlmostEqual(eps[0]["entry_price"], 155.65)
        self.assertLess(eps[0]["pnl_pct"], 0.0)

    def test_the_strategy_reason_is_not_disturbed(self):
        """The broker has no idea why anything was sold."""
        self.arm(DEKELI_ORDERS)
        eps = [self.episode()]
        trader._correct_episodes_from_broker_fills(eps)
        self.assertEqual(eps[0]["close_reason"], "risk_trim[连跌2日]")
        self.assertEqual(eps[0]["mode"], "pre_breakout")
        self.assertEqual(eps[0]["tier"], 2)

    def test_a_corrected_episode_is_marked_verified(self):
        self.arm(DEKELI_ORDERS)
        eps = [self.episode()]
        trader._correct_episodes_from_broker_fills(eps)
        self.assertTrue(eps[0]["price_verified"])
        self.assertEqual(eps[0]["price_source"], "broker_fill")

    def test_an_episode_with_no_fill_is_flagged_not_dropped(self):
        """Dropping would change counts the learning layer sizes samples by."""
        self.arm(DEKELI_ORDERS)
        eps = [self.episode(code="999999", name="不存在")]
        res = trader._correct_episodes_from_broker_fills(eps)
        self.assertEqual(len(eps), 1)
        self.assertFalse(eps[0]["price_verified"])
        self.assertEqual(res["unverified"], 1)

    def test_nothing_is_added_even_though_the_archive_holds_more(self):
        """122 round trips in the archive against 29 here; scope is correction."""
        self.arm(DEKELI_ORDERS)
        eps = [self.episode()]
        trader._correct_episodes_from_broker_fills(eps)
        self.assertEqual(len(eps), 1)

    def test_an_already_correct_price_is_not_counted_as_corrected(self):
        self.arm(DEKELI_ORDERS)
        eps = [self.episode(entry_price=155.65)]
        res = trader._correct_episodes_from_broker_fills(eps)
        self.assertEqual(res["corrected"], 0)
        self.assertTrue(eps[0]["price_verified"])


class SupplementTests(Harness):
    """Facts the rolling window cannot supply, kept as data not code."""

    def test_a_corporate_action_turns_the_loss_into_a_gain(self):
        orders = [
            broker_order("688800", "buy", 12842, 300, 1780617000, name="瑞可达"),
            broker_order("688800", "sell", 9831, 400, 1782000000, name="瑞可达"),
        ]
        self.arm(orders, supplement={"corporate_actions": [
            {"code": "688800", "time": 1781654400,
             "per_10_bonus": 4, "per_10_cash": 3}]})
        eps = [{"code": "688800", "name": "瑞可达", "entry_price": 128.42,
                "pnl_pct": -23.4, "quantity": 400, "sell_date": "2026-06-22"}]
        trader._correct_episodes_from_broker_fills(eps)
        self.assertAlmostEqual(eps[0]["entry_price"], 91.7286, places=3)
        self.assertGreater(eps[0]["pnl_pct"], 0.0)

    def test_a_missing_supplement_is_not_an_error(self):
        self.arm(DEKELI_ORDERS)
        trader.MX_LEDGER_SUPPLEMENT_FILE = "/nonexistent/supplement.json"
        res = trader._correct_episodes_from_broker_fills([self.episode()])
        self.assertEqual(res["reason"], "ok")


class SafetyTests(Harness):
    """A wrong price is bad. A close-node phase that dies is worse."""

    def test_a_missing_archive_leaves_prices_untouched(self):
        d = self.arm(DEKELI_ORDERS)
        trader.MX_ORDERS_ARCHIVE_FILE = str(d / "nope.json")
        eps = [self.episode()]
        res = trader._correct_episodes_from_broker_fills(eps)
        self.assertEqual(eps[0]["entry_price"], 22.92)
        self.assertFalse(eps[0]["price_verified"])
        self.assertIn("no order archive", res["reason"])

    def test_a_corrupt_archive_does_not_raise(self):
        d = self.arm(DEKELI_ORDERS)
        (d / "orders.json").write_text("not json", encoding="utf-8")
        eps = [self.episode()]
        res = trader._correct_episodes_from_broker_fills(eps)
        self.assertEqual(eps[0]["entry_price"], 22.92)
        self.assertEqual(res["corrected"], 0)

    def test_an_empty_episode_list_is_handled(self):
        self.arm(DEKELI_ORDERS)
        self.assertEqual(
            trader._correct_episodes_from_broker_fills([])["corrected"], 0)

    def test_correction_runs_before_the_summary(self):
        """The summary averages pnl_pct; +557% rows moved it to +38.32%."""
        src = Path(trader.__file__).read_text(encoding="utf-8")
        fix = src.index("_correct_episodes_from_broker_fills(trade_episode_history)")
        summ = src.index("history_summary = _summarize_trade_episode_history")
        self.assertLess(fix, summ)


if __name__ == "__main__":
    unittest.main()


class EveryWriteSiteTests(unittest.TestCase):
    """Both producers of the episode file must correct.

    The file is written in two places - the close-node phase and the MX fill
    repair path. Correcting only one means a repair run silently republishes
    uncorrected prices, and the learning layer goes straight back to believing
    vol_breakout averages +161% when it averages +0.11%.
    """

    SRC = Path(trader.__file__).read_text(encoding="utf-8")

    def test_every_producer_of_episodes_is_corrected(self):
        """The invariant is per PRODUCER, not per write.

        A first version of this asserted the correction appeared near each
        _write_json_atomic(TRADE_EPISODE_HISTORY_FILE...) call, and failed on
        the close-node write - which publishes a bundle corrected four thousand
        lines earlier. Proximity to the write is not the property that matters;
        what matters is that nothing builds an episode list without correcting
        it.
        """
        calls = [i for i in range(len(self.SRC))
                 if self.SRC.startswith("_build_trade_episode_history(", i)]
        # one definition plus every call site
        self.assertGreaterEqual(len(calls), 3)
        for at in calls:
            if self.SRC[max(0, at - 4):at].strip().endswith("def"):
                continue
            following = self.SRC[at:at + 900]
            self.assertIn("_correct_episodes_from_broker_fills", following,
                          "episodes are built at offset %d and never corrected"
                          % at)

    def test_the_correction_is_defined_once(self):
        self.assertEqual(
            self.SRC.count("def _correct_episodes_from_broker_fills"), 1)


class SupplementFallbackTests(unittest.TestCase):
    """The pre-window facts must survive a rebuilt box.

    DATA_DIR is excluded from the deploy sync, so an operator copy there
    persists across deploys and rightly wins. But that same exclusion means
    facts living ONLY there would be lost if the box were rebuilt - and they are
    irreplaceable: the orders window cannot reach the 2026-03-20 中粮资本
    purchase, and no endpoint records 瑞可达's 10转4派3元.
    """

    def test_a_shipped_default_travels_with_the_repo(self):
        self.assertTrue(Path(trader.MX_LEDGER_SUPPLEMENT_DEFAULT).exists())

    def test_the_shipped_default_carries_both_known_facts(self):
        payload = json.loads(
            Path(trader.MX_LEDGER_SUPPLEMENT_DEFAULT).read_text(encoding="utf-8"))
        codes = {str(x.get("code")) for x in payload.get("opening_lots", [])}
        actions = {str(x.get("code")) for x in payload.get("corporate_actions", [])}
        self.assertIn("002423", codes)
        self.assertIn("688800", actions)

    def test_the_default_is_used_when_no_operator_copy_exists(self):
        self.addCleanup(setattr, trader, "MX_LEDGER_SUPPLEMENT_FILE",
                        trader.MX_LEDGER_SUPPLEMENT_FILE)
        trader.MX_LEDGER_SUPPLEMENT_FILE = "/nonexistent/supplement.json"
        opening, actions = trader._load_ledger_supplement()
        self.assertTrue(opening)
        self.assertTrue(actions)

    def test_an_operator_copy_wins_over_the_default(self):
        d = Path(tempfile.mkdtemp()) / "supp.json"
        d.write_text(json.dumps({"opening_lots": [
            {"code": "999999", "price": 1.0, "quantity": 1, "time": 1}]}),
            encoding="utf-8")
        self.addCleanup(setattr, trader, "MX_LEDGER_SUPPLEMENT_FILE",
                        trader.MX_LEDGER_SUPPLEMENT_FILE)
        trader.MX_LEDGER_SUPPLEMENT_FILE = str(d)
        opening, _ = trader._load_ledger_supplement()
        self.assertEqual([x["code"] for x in opening], ["999999"])
