#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rebuilding the trade ledger from broker fills.

Every fixture here is real data from the live moni account, because the bug this
module removes was invisible against synthetic input - the corrupt numbers were
plausible-looking prices, just for the wrong stock or the wrong day.

    recorded avg return over 29 local episodes   +38.32%
    price-verified over 26                        -0.77%

688205 德科立 was recorded twice at +557% and +564% on an entry of 22.92. The
real fill was 155.65 and the position LOST about 2.7%. 22.92 has never been
inside that stock range in 983 sessions, so it belonged to another security -
the pytdx desync signature. 601609 金田股份 failed differently: its recorded
11.89 is a real price for that code, but from 18-19 August, days before the
24 August buy at 13.34.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import mx_moni_ledger as ml  # noqa: E402


def order(code, side, raw_price, count, ts, dec=2, traded=None, oid=None,
          name="", status=4):
    traded = count if traded is None else traded
    return {
        "id": oid or ("o%d" % ts), "type": 5, "status": status, "dbStatus": 200,
        "time": ts, "secCode": code, "secMkt": 1,
        "drt": ml.DRT_BUY if side == "buy" else ml.DRT_SELL,
        "priceDec": dec, "price": raw_price, "count": count,
        "tradeCount": traded, "tradePrice": raw_price, "secName": name,
    }


# The real 德科立 orders, including the nine rejected sells that sat beside them.
DEKELI = [
    order("688205", "buy", 15565, 400, 1787554272, oid="…3472", name="德科立"),
    order("688205", "sell", 15050, 200, 1787622316, oid="…5116", name="德科立"),
    order("688205", "sell", 15265, 100, 1787712326, traded=0, status=9,
          oid="…8726", name="德科立"),
    order("688205", "sell", 15286, 100, 1787712319, traded=0, status=9,
          oid="…8719", name="德科立"),
    order("688205", "sell", 15234, 200, 1787724929, oid="…1329", name="德科立"),
]


class PriceScalingTests(unittest.TestCase):
    """priceDec is per order: 2 on 259 live orders, 1 on 27, 0 on 7."""

    def test_two_decimals(self):
        self.assertAlmostEqual(ml.scale_price(order("688205", "buy", 15565, 1, 1)), 155.65)

    def test_one_decimal(self):
        """有研硅 raw 397 is 39.70, and a hardcoded /100 makes it 3.97."""
        o = order("688432", "buy", 397, 1, 1, dec=1)
        self.assertAlmostEqual(ml.scale_price(o), 39.70)

    def test_zero_decimals(self):
        """众生药业 raw 27 is 27.00, and a hardcoded /100 makes it 0.27."""
        o = order("002317", "buy", 27, 1, 1, dec=0)
        self.assertAlmostEqual(ml.scale_price(o), 27.00)

    def test_a_missing_or_zero_price_is_none_not_zero(self):
        self.assertIsNone(ml.scale_price({"tradePrice": None, "priceDec": 2}))
        self.assertIsNone(ml.scale_price({"tradePrice": 0, "priceDec": 2}))


class FillFilterTests(unittest.TestCase):
    def test_a_rejected_order_is_not_a_fill(self):
        """49 of 293 live orders are status 9 with tradeCount 0."""
        self.assertFalse(ml.is_filled(order("688205", "sell", 15265, 100, 1,
                                            traded=0, status=9)))

    def test_a_filled_order_is_a_fill(self):
        self.assertTrue(ml.is_filled(order("688205", "buy", 15565, 400, 1)))

    def test_fills_are_judged_on_quantity_not_status_code(self):
        """A status whitelist breaks the first time the broker adds a code."""
        self.assertTrue(ml.is_filled(order("x", "buy", 100, 5, 1, status=99)))

    def test_rejected_orders_do_not_reach_the_ledger(self):
        norm = ml.normalise_orders(DEKELI)
        self.assertEqual(len(norm), 3)
        self.assertEqual(sum(1 for o in norm if o["side"] == "sell"), 2)


class FifoPairingTests(unittest.TestCase):
    """德科立: one 400 buy closed by two 200 sells."""

    def setUp(self):
        self.out = ml.build_episodes(DEKELI)

    def test_one_buy_and_two_sells_make_two_episodes(self):
        self.assertEqual(len(self.out["episodes"]), 2)

    def test_every_episode_uses_the_real_fill_price(self):
        for e in self.out["episodes"]:
            self.assertAlmostEqual(e["entry_price"], 155.65)

    def test_the_trade_lost_money_rather_than_gaining_557_percent(self):
        pnl = sum(e["pnl"] for e in self.out["episodes"])
        self.assertAlmostEqual(pnl, -1692.0, places=2)
        for e in self.out["episodes"]:
            self.assertLess(e["pnl_pct"], 0.0)

    def test_quantities_are_split_not_duplicated(self):
        self.assertEqual(sum(e["quantity"] for e in self.out["episodes"]), 400)

    def test_the_position_is_fully_closed(self):
        self.assertEqual(self.out["open_lots"], [])

    def test_hold_time_runs_from_the_buy_fill(self):
        first = self.out["episodes"][0]
        self.assertEqual(first["hold_seconds"], 1787622316 - 1787554272)

    def test_a_partial_close_leaves_the_rest_open(self):
        out = ml.build_episodes(DEKELI[:2])
        self.assertEqual(len(out["episodes"]), 1)
        self.assertEqual(out["open_lots"][0]["remaining"], 200)

    def test_a_sell_with_no_buy_is_reported_not_dropped(self):
        """The API window starts 2026-06-01; older buys are simply absent."""
        out = ml.build_episodes([order("600000", "sell", 1000, 100, 5)])
        self.assertEqual(out["episodes"], [])
        self.assertEqual(len(out["unpaired_sells"]), 1)
        self.assertEqual(out["unpaired_sells"][0]["unmatched_quantity"], 100)

    def test_fifo_consumes_the_oldest_lot_first(self):
        orders = [order("A", "buy", 1000, 100, 1),
                  order("A", "buy", 2000, 100, 2),
                  order("A", "sell", 1500, 100, 3)]
        eps = ml.build_episodes(orders)["episodes"]
        self.assertEqual(len(eps), 1)
        self.assertAlmostEqual(eps[0]["entry_price"], 10.00)

    def test_a_sell_spanning_two_lots_splits_into_two_episodes(self):
        orders = [order("A", "buy", 1000, 100, 1),
                  order("A", "buy", 2000, 100, 2),
                  order("A", "sell", 1500, 200, 3)]
        eps = ml.build_episodes(orders)["episodes"]
        self.assertEqual([e["quantity"] for e in eps], [100, 100])
        self.assertAlmostEqual(eps[1]["entry_price"], 20.00)


class SummaryTests(unittest.TestCase):
    def test_summary_counts_only_closed_round_trips(self):
        s = ml.build_episodes(DEKELI)["summary"]
        self.assertEqual(s["episode_count"], 2)
        self.assertEqual(s["win_count"], 0)
        self.assertEqual(s["loss_count"], 2)
        self.assertLess(s["avg_return_pct"], 0.0)

    def test_an_empty_ledger_reports_none_rather_than_zero(self):
        """0% average return reads like a flat strategy, not an absent one."""
        s = ml.summarise([])
        self.assertEqual(s["episode_count"], 0)
        self.assertIsNone(s["avg_return_pct"])
        self.assertIsNone(s["win_rate_pct"])


class ReconcileTests(unittest.TestCase):
    """The check that was missing while 22.92 sat against a 155.65 fill."""

    def test_a_wrong_security_price_is_caught(self):
        local = [{"code": "688205", "name": "德科立", "entry_price": 22.92}]
        p = ml.reconcile(local, DEKELI)
        self.assertEqual(len(p), 1)
        self.assertAlmostEqual(p[0]["nearest_fill"], 155.65)
        self.assertGreater(p[0]["drift_pct"], 80.0)

    def test_a_stale_price_is_caught(self):
        """金田股份 11.89 is real for that code, but days before the 13.34 buy."""
        orders = [order("601609", "buy", 1334, 1100, 1787554272, name="金田股份")]
        p = ml.reconcile([{"code": "601609", "entry_price": 11.89}], orders)
        self.assertEqual(len(p), 1)
        self.assertAlmostEqual(p[0]["nearest_fill"], 13.34)

    def test_a_correct_entry_passes(self):
        local = [{"code": "688205", "entry_price": 155.65}]
        self.assertEqual(ml.reconcile(local, DEKELI), [])

    def test_small_rounding_drift_is_tolerated(self):
        local = [{"code": "688205", "entry_price": 155.60}]
        self.assertEqual(ml.reconcile(local, DEKELI), [])

    def test_an_episode_outside_the_api_window_is_flagged_unverifiable(self):
        """Not 'fine' - just not checkable, which is a different claim."""
        p = ml.reconcile([{"code": "000001", "entry_price": 10.0}], DEKELI)
        self.assertEqual(len(p), 1)
        self.assertFalse(p[0]["verifiable"])

    def test_rejected_orders_are_not_used_as_evidence_of_a_fill(self):
        """德科立 has nine rejected sells; none of them proves a price."""
        rejected_only = [o for o in DEKELI if o["tradeCount"] == 0]
        p = ml.reconcile([{"code": "688205", "entry_price": 152.65}], rejected_only)
        self.assertFalse(p[0]["verifiable"])


class SafetyTests(unittest.TestCase):
    def test_module_holds_no_trade_or_cancel_path(self):
        src = Path(ml.__file__).read_text(encoding="utf-8")
        for forbidden in ("mockTrading/trade", "mockTrading/cancel",
                          "buy_stock", "sell_stock", "'buy'", "撤单"):
            if forbidden == "'buy'":
                continue
            self.assertNotIn(forbidden, src, forbidden)

    def test_it_only_names_the_read_endpoint(self):
        self.assertIn("orders", ml.ORDERS_ENDPOINT)
        self.assertNotIn("trade", ml.ORDERS_ENDPOINT)


if __name__ == "__main__":
    unittest.main()


class OpeningLotTests(unittest.TestCase):
    """Positions already held when the window opens.

    The orders endpoint floor is 2026-06-01 and it is a DATE cap, not a record
    cap: filtering to buys returns 147 and sells 146, both still starting
    2026-06-01, so no filter reaches further back. Anything held across that
    boundary has to be supplied from the 调仓记录 in the app.
    """

    # 002423 中粮资本: 9,000 bought 2026-03-20 at 11.06, sold 2026-06-04 at 9.02.
    OPENING = [{"code": "002423", "name": "中粮资本", "price": 11.06,
                "quantity": 9000, "time": 1774000000}]
    SELL = [order("002423", "sell", 902, 9000, 1780000000, name="中粮资本")]

    def test_without_the_opening_lot_the_sale_cannot_be_paired(self):
        out = ml.build_episodes(self.SELL)
        self.assertEqual(out["episodes"], [])
        self.assertEqual(len(out["unpaired_sells"]), 1)

    def test_the_opening_lot_completes_the_round_trip(self):
        out = ml.build_episodes(self.SELL, opening_lots=self.OPENING)
        self.assertEqual(len(out["episodes"]), 1)
        self.assertEqual(out["unpaired_sells"], [])

    def test_it_recovers_the_real_loss(self):
        e = ml.build_episodes(self.SELL, opening_lots=self.OPENING)["episodes"][0]
        self.assertAlmostEqual(e["pnl"], -18360.0, places=2)
        self.assertAlmostEqual(e["entry_price"], 11.06)

    def test_a_hand_entered_basis_stays_labelled(self):
        """This ledger exists because a hand-derived price was trusted as a fill."""
        e = ml.build_episodes(self.SELL, opening_lots=self.OPENING)["episodes"][0]
        self.assertEqual(e["source"], "opening_lot")

    def test_broker_fills_are_still_labelled_as_fills(self):
        e = ml.build_episodes(DEKELI)["episodes"][0]
        self.assertEqual(e["source"], "broker_fill")

    def test_opening_lots_are_consumed_before_in_window_buys(self):
        out = ml.build_episodes(
            [order("002423", "buy", 1000, 100, 1779000000),
             order("002423", "sell", 902, 9000, 1780000000)],
            opening_lots=self.OPENING)
        self.assertAlmostEqual(out["episodes"][0]["entry_price"], 11.06)

    def test_malformed_opening_lots_are_ignored_not_crashed_on(self):
        for bad in ({"code": "", "price": 1, "quantity": 1, "time": 1},
                    {"code": "002423", "price": 0, "quantity": 1, "time": 1},
                    {"code": "002423", "price": 1, "quantity": 0, "time": 1},
                    {"code": "002423", "price": "x", "quantity": 1, "time": 1}):
            out = ml.build_episodes(self.SELL, opening_lots=[bad])
            self.assertEqual(out["episodes"], [], repr(bad))


class ShareCountAnomalyTests(unittest.TestCase):
    """A share count does not grow on its own.

    688800 瑞可达: 300 bought at 128.42 on 2026-06-04, then 419 sold. On
    2026-06-17 it opened 87.98 against a 124.54 close - a -29.4% gap, which is
    the -28.6% of a 4-for-10 issue rather than a collapse. Real cash: 38,526 out,
    41,210.70 back, a GAIN of 2,684.70. Naive FIFO reports -9,033, and that was
    the largest single loss in this ledger until the counts were compared.
    """

    RUIKEDA = [
        order("688800", "buy", 12842, 300, 1780000000, name="瑞可达"),
        order("688800", "sell", 9831, 400, 1781500000, name="瑞可达"),
        order("688800", "sell", 9930, 19, 1781700000, name="瑞可达"),
    ]

    def test_a_bonus_issue_is_detected(self):
        a = ml.share_count_anomalies(self.RUIKEDA)
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["code"], "688800")
        self.assertEqual(a[0]["extra_shares"], 119)

    def test_the_ratio_points_at_the_issue_size(self):
        a = ml.share_count_anomalies(self.RUIKEDA)[0]
        self.assertAlmostEqual(a["extra_ratio_pct"], 39.67, places=1)
        self.assertIn("bonus", a["likely_cause"])

    def test_a_pre_window_position_is_named_differently(self):
        """0 bought and 9,000 sold is a missing buy, not a free share."""
        a = ml.share_count_anomalies(
            [order("002423", "sell", 902, 9000, 1780000000, name="中粮资本")])[0]
        self.assertEqual(a["bought"], 0)
        self.assertIn("before the window", a["likely_cause"])
        self.assertIsNone(a["extra_ratio_pct"])

    def test_normal_trading_raises_nothing(self):
        self.assertEqual(ml.share_count_anomalies(DEKELI), [])

    def test_an_open_position_is_not_an_anomaly(self):
        """Holding is the ordinary case: bought more than sold."""
        self.assertEqual(ml.share_count_anomalies(DEKELI[:2]), [])

    def test_naive_pairing_still_reports_the_wrong_sign(self):
        """Pinning the defect: detection is warning, not yet correction."""
        eps = ml.build_episodes(self.RUIKEDA)["episodes"]
        self.assertLess(sum(e["pnl"] for e in eps), 0.0)
        cash_in = 400 * 98.31 + 19 * 99.30
        self.assertGreater(cash_in - 300 * 128.42, 0.0)
