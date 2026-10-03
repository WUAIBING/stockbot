#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The books must close from broker facts, and must say when they cannot.

For Q3 2026 the printed scoreboard said +31,591, the episode history -19,458,
and the account had lost 35,887. These tests drive a small simulated account
with known fees through account_books and check that it recovers every term -
and that a missing fill, a phantom share or an unrecorded dividend is reported
rather than absorbed.
"""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import account_books as ab  # noqa: E402
import mx_moni_ledger as ml  # noqa: E402

CN = dt.timezone(dt.timedelta(hours=8))
BUY_FEE, SELL_FEE = 0.001, 0.0015


def at(day, hh=15, mm=6):
    return int(dt.datetime(2026, 9, day, hh, mm, tzinfo=CN).timestamp())


def order(oid, code, side, qty, price, ts, filled=True):
    """A raw broker order, in the orders endpoint's own field names."""
    return {"id": str(oid), "secCode": code, "secName": "n" + code, "drt": 1 if side == "buy" else 2,
            "count": qty, "tradeCount": qty if filled else 0, "price": int(round(price * 100)),
            "tradePrice": int(round(price * 100)) if filled else 0, "priceDec": 2,
            "status": 4 if filled else 9, "time": ts}


class Account:
    """Cash and holdings that move the way the simulator's do, fees included."""

    def __init__(self, cash):
        self.cash, self.holdings, self.orders, self.snaps = cash, {}, [], []
        self._id = 0

    def trade(self, code, side, qty, price, ts):
        self._id += 1
        self.orders.append(order(self._id, code, side, qty, price, ts))
        amount = qty * price
        if side == "buy":
            self.cash -= amount * (1 + BUY_FEE)
            self.holdings[code] = self.holdings.get(code, 0) + qty
        else:
            self.cash += amount * (1 - SELL_FEE)
            self.holdings[code] = self.holdings.get(code, 0) - qty
        return self._id

    def snapshot(self, ts, marks, frozen=0.0):
        pos = sum(q * marks[c] for c, q in self.holdings.items() if q)
        # frozen cash is still the account's money: in total_assets, out of avail
        self.snaps.append({"ts": ts, "total_assets": self.cash + pos, "total_pos_value": pos,
                           "avail_balance": self.cash - frozen})

    def positions(self):
        return [{"code": c, "count": q} for c, q in self.holdings.items() if q]


def quarter():
    """Three days: two round trips (one win, one loss) and a position left open."""
    a = Account(1_000_000.0)
    a.snapshot(at(1), {})
    a.trade("600001", "buy", 1000, 10.00, at(2, 14, 53))
    a.trade("600002", "buy", 2000, 5.00, at(2, 14, 53))
    a.snapshot(at(2), {"600001": 10.10, "600002": 4.95})
    a.trade("600001", "sell", 1000, 11.00, at(3, 9, 45))      # +1,000
    a.trade("600003", "buy", 500, 20.00, at(3, 14, 53))
    a.snapshot(at(3), {"600002": 4.90, "600003": 20.20})
    a.trade("600002", "sell", 2000, 4.80, at(4, 9, 45))        # -400
    a.snapshot(at(4), {"600003": 21.00})
    return a


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.a = quarter()
        self.b = ab.books(self.a.orders, self.a.snaps, positions=self.a.positions())

    def test_every_term_is_recovered(self):
        b = self.b
        self.assertEqual(b["realised_pnl"], 600.00)
        self.assertEqual(b["closes"]["count"], 2)
        self.assertEqual(b["closes"]["win_count"], 1)
        self.assertEqual(b["unrealised_end"], 500.00)            # 500 x (21.00 - 20.00)
        fees = (10000 + 10000 + 10000) * BUY_FEE + (11000 + 9600) * SELL_FEE
        self.assertAlmostEqual(b["costs"], fees, places=2)
        self.assertAlmostEqual(b["nav_change"], 600 + 500 - fees, places=2)

    def test_the_books_close_and_are_verified(self):
        self.assertEqual(self.b["residual"], 0.0)
        self.assertEqual(self.b["problems"], [])
        self.assertTrue(self.b["verified"])

    def test_costs_are_measured_not_assumed(self):
        """The fitted rates recover what the account was actually charged."""
        a = Account(1_000_000.0)
        a.snapshot(at(1), {})
        for d, (bq, sq) in enumerate([(1000, 0), (3000, 1000), (500, 2500), (2000, 0), (0, 2000)], start=2):
            if sq:
                a.trade("600001", "sell", sq, 10.0, at(d, 9, 45))
            if bq:
                a.trade("600001", "buy", bq, 10.0, at(d, 14, 53))
            a.snapshot(at(d), {"600001": 10.0})
        b = ab.books(a.orders, a.snaps, positions=a.positions())
        self.assertAlmostEqual(b["fitted_rates_pct"]["buy"], 100 * BUY_FEE, places=3)
        self.assertAlmostEqual(b["fitted_rates_pct"]["sell"], 100 * SELL_FEE, places=3)

    def test_realised_after_costs_is_what_the_account_kept(self):
        b = self.b
        self.assertAlmostEqual(b["closes"]["realised_after_costs"], b["realised_pnl"] - b["costs"], places=2)


class CashTests(unittest.TestCase):
    def test_frozen_cash_does_not_look_like_a_cost(self):
        """2026-09-09 14:53: avail fell 168,009 on 125,654 of fills, because two
        unfilled orders were frozen. true_cash must not see that as spending."""
        a = Account(1_000_000.0)
        a.snapshot(at(1), {})
        a.trade("600001", "buy", 1000, 10.0, at(2, 14, 53))
        a.snapshot(at(2), {"600001": 10.0}, frozen=42_000.0)
        self.assertAlmostEqual(ab.true_cash(a.snaps[-1]), a.cash, places=2)
        self.assertLess(a.snaps[-1]["avail_balance"], a.cash - 40_000)
        b = ab.books(a.orders, a.snaps, positions=a.positions())
        self.assertAlmostEqual(b["costs"], 10.0, places=2)

    def test_a_zero_nav_row_is_an_api_failure_not_a_wipeout(self):
        a = quarter()
        snaps = a.snaps + [{"ts": at(3, 13, 0), "total_assets": 0, "total_pos_value": 0}]
        self.assertEqual(ab.books(a.orders, snaps, positions=a.positions())["residual"], 0.0)

    def test_one_snapshot_is_not_enough(self):
        b = ab.books([], [{"ts": at(1), "total_assets": 1.0, "total_pos_value": 0.0}])
        self.assertFalse(b["verified"])
        self.assertIn("need two", b["problems"][0])


class WhatCannotHideTests(unittest.TestCase):
    def test_a_missing_fill_breaks_the_position_check(self):
        """The archive lost the 600003 buy. Arithmetic still 'closes' - which is
        exactly why the share-for-share check exists."""
        a = quarter()
        orders = [o for o in a.orders if o["secCode"] != "600003"]
        b = ab.books(orders, a.snaps, positions=a.positions())
        self.assertFalse(b["verified"])
        self.assertTrue(any("600003" in p for p in b["problems"]))

    def test_a_missing_fill_also_shows_as_an_impossible_cost(self):
        a = quarter()
        orders = [o for o in a.orders if o["secCode"] != "600003"]
        b = ab.books(orders, a.snaps, positions=a.positions())
        self.assertTrue(any("not a fee" in p for p in b["problems"]))

    def test_a_phantom_share_is_reported(self):
        a = quarter()
        positions = [{"code": "600003", "count": 499}]
        b = ab.books(a.orders, a.snaps, positions=positions)
        self.assertEqual(b["position_check"]["mismatches"], [{"code": "600003", "ledger": 500, "broker": 499}])
        self.assertFalse(b["verified"])

    def test_without_positions_nothing_is_called_verified(self):
        a = quarter()
        b = ab.books(a.orders, a.snaps)
        self.assertEqual(b["problems"], [])
        self.assertFalse(b["verified"])      # closes, but nobody checked the shares

    def test_a_sell_with_no_buy_on_record_is_a_problem(self):
        a = quarter()
        a.orders.append(order(99, "600009", "sell", 100, 9.0, at(4, 10, 0)))
        b = ab.books(a.orders, a.snaps, positions=a.positions())
        self.assertTrue(any("no buy on record" in p for p in b["problems"]))

    def test_rejected_orders_are_not_trades(self):
        a = quarter()
        a.orders.append(order(98, "600001", "buy", 9000, 10.0, at(3, 14, 53), filled=False))
        self.assertTrue(ab.books(a.orders, a.snaps, positions=a.positions())["verified"])


class DividendTests(unittest.TestCase):
    def test_an_unrecorded_dividend_is_listed_not_hidden(self):
        """佐力药业 paid about 517 on 2026-09-23 and nothing knew."""
        marks = {"600001": 10.0}
        a = Account(1_000_000.0)
        a.snapshot(at(1), {})
        a.trade("600001", "buy", 1000, 10.0, at(2, 14, 53))
        a.snapshot(at(2), marks)
        for d in (3, 4, 5):                               # sell the morning, rebuy the close
            a.trade("600001", "sell", 1000, 10.0, at(d, 9, 45))
            a.trade("600001", "buy", 1000, 10.0, at(d, 14, 53))
            a.snapshot(at(d), marks)
        a.snapshot(at(6), marks)                          # a quiet day: cash must not move
        a.cash += 517.0                                   # the dividend, on a day with no fills
        a.snapshot(at(7), marks)
        b = ab.books(a.orders, a.snaps, positions=a.positions())
        days = [u for u in b["unusual_days"] if u["day"] == "2026-09-07"]
        self.assertEqual(len(days), 1)
        self.assertEqual(days[0]["note"], "cash in")
        self.assertAlmostEqual(days[0]["cash_gap"], 517.0, places=2)
        self.assertFalse(days[0]["had_fills"])

    def test_a_recorded_dividend_is_its_own_line(self):
        a = Account(1_000_000.0)
        a.snapshot(at(1), {})
        a.trade("600001", "buy", 1000, 10.0, at(2, 14, 53))
        a.snapshot(at(2), {"600001": 10.0})
        a.cash += 300.0                                   # 10派3 on 1000 shares
        a.snapshot(at(3), {"600001": 10.0})
        actions = [{"code": "600001", "per_10_bonus": 0, "per_10_cash": 3, "time": at(3, 9, 0)}]
        b = ab.books(a.orders, a.snaps, corporate_actions=actions, positions=a.positions())
        self.assertEqual(b["dividends"], 300.0)
        self.assertAlmostEqual(b["costs"], 10.0, places=2)
        self.assertEqual(b["residual"], 0.0)


class LedgerClosesTests(unittest.TestCase):
    def test_a_sell_across_two_lots_is_one_close(self):
        """智度股份 08-17: one sell of two lots, -2,680; the history kept -550."""
        orders = [order(1, "000676", "buy", 4000, 7.20, at(1)), order(2, "000676", "buy", 4000, 7.49, at(2)),
                  order(3, "000676", "sell", 8000, 7.01, at(3))]
        eps = ml.build_episodes(orders)["episodes"]
        self.assertEqual(len(eps), 2)
        c = ml.closes(eps)
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]["quantity"], 8000)
        self.assertEqual(c[0]["lots"], 2)
        self.assertAlmostEqual(c[0]["entry_price"], 7.345, places=4)
        self.assertAlmostEqual(c[0]["pnl"], (7.01 - 7.345) * 8000, places=2)
        self.assertAlmostEqual(c[0]["pnl_pct"], (7.01 / 7.345 - 1) * 100, places=3)

    def test_two_sells_of_one_lot_stay_two_closes(self):
        orders = [order(1, "688205", "buy", 400, 155.65, at(1)), order(2, "688205", "sell", 200, 150.50, at(2)),
                  order(3, "688205", "sell", 200, 152.34, at(3))]
        c = ml.closes(ml.build_episodes(orders)["episodes"])
        self.assertEqual([x["sell_order_id"] for x in c], ["2", "3"])
        self.assertAlmostEqual(c[0]["pnl"], -1030.0, places=2)
        self.assertAlmostEqual(c[1]["pnl"], -662.0, places=2)

    def test_the_brokers_share_count_wins_over_the_formula(self):
        """瑞可达 10转4 on 300 is 420 by formula; the broker credited 419."""
        orders = [order(1, "688800", "buy", 300, 128.42, at(1)), order(2, "688800", "sell", 400, 98.31, at(5)),
                  order(3, "688800", "sell", 19, 99.30, at(6))]
        action = {"code": "688800", "per_10_bonus": 4, "per_10_cash": 3, "time": at(3, 9, 0)}
        loose = ml.build_episodes(orders, corporate_actions=[action])
        self.assertEqual(sum(l["remaining"] for l in loose["open_lots"]), 1)       # the phantom share
        exact = ml.build_episodes(orders, corporate_actions=[dict(action, resulting_quantity=419)])
        self.assertEqual(exact["open_lots"], [])
        self.assertAlmostEqual(sum(e["pnl"] for e in exact["episodes"]),
                               400 * 98.31 + 19 * 99.30 - 300 * 128.42, places=1)


class SafetyTests(unittest.TestCase):
    def test_pure_no_network_no_files_no_orders(self):
        src = (SKILL / "account_books.py").read_text(encoding="utf-8")
        for bad in ("requests", "urllib", "open(", "buy_stock", "sell_stock", "execute_trade_action",
                    "mockTrading", "TdxHq_API"):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
