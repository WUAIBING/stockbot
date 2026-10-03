#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The scoreboard must be settled against the broker where records are loaded.

For Q3 2026 the printed scoreboard claimed +31,591 on an account that lost
35,887. The episode history had been corrected in August; the records that feed
the scoreboard, the tier and mode tables, the NAV log and the model never were.
These tests pin the settlement at load, the daily books refresh, and the
history correction's sell-order matching.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import v10_moni_trader as trader  # noqa: E402

NOW = int(time.time())
DAY = 86400


def order(oid, code, side, qty, price, ts, name=""):
    return {"id": str(oid), "secCode": code, "secName": name or code, "drt": 1 if side == "buy" else 2,
            "count": qty, "tradeCount": qty, "price": int(round(price * 100)),
            "tradePrice": int(round(price * 100)), "priceDec": 2, "status": 4, "time": ts}


# The quarter's real failures, as broker orders.
ORDERS = [
    order("b1", "688205", "buy", 400, 155.65, NOW - 9 * DAY, "德科立"),
    order("s1", "688205", "sell", 200, 150.50, NOW - 8 * DAY, "德科立"),
    order("s2", "688205", "sell", 200, 152.34, NOW - 7 * DAY, "德科立"),
    order("b2", "000676", "buy", 4000, 7.20, NOW - 9 * DAY, "智度股份"),
    order("b3", "000676", "buy", 4000, 7.49, NOW - 8 * DAY, "智度股份"),
    order("s3", "000676", "sell", 8000, 7.01, NOW - 6 * DAY, "智度股份"),
    order("b4", "600649", "buy", 5300, 4.01, NOW - 6 * DAY, "城投控股"),
    order("s4", "600649", "sell", 5300, 3.76, NOW - 5 * DAY, "城投控股"),      # no local record
]


def dead_order(oid, code, qty, price, ts, name=""):
    """A sell order the broker accepted and never filled."""
    o = order(oid, code, "sell", qty, price, ts, name)
    o.update({"tradeCount": 0, "tradePrice": 0, "status": 9})
    return o


def china_day(ts):
    return datetime.fromtimestamp(ts, trader.MARKET_TZ).strftime("%Y-%m-%d")


def record(code, sell_order_id, entry, sell, qty, **extra):
    r = {"code": code, "status": "closed", "sell_order_id": sell_order_id,
         "entry_price": str(entry), "sell_price": str(sell), "quantity": str(qty),
         "pnl": f"{(sell - entry) * qty:.2f}", "pnl_pct": f"{(sell / entry - 1) * 100:.2f}",
         "mode": "pre_breakout", "tier": "2", "target_amount": "20000",
         "date": "2026-08-24", "close_reason": "信号衰减[大阴线-3.3%]"}
    r.update(extra)
    return r


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.archive = self.tmp / "orders.json"
        self.archive.write_text(json.dumps({"orders": ORDERS}), encoding="utf-8")
        for name, value in (("MX_ORDERS_ARCHIVE_FILE", str(self.archive)),
                            ("MX_LEDGER_SUPPLEMENT_FILE", str(self.tmp / "no-supplement.json")),
                            ("MX_LEDGER_SUPPLEMENT_DEFAULT", str(self.tmp / "no-default.json")),
                            ("NAV_FILE", str(self.tmp / "nav.csv")),
                            ("BOOKS_FILE", str(self.tmp / "books.json"))):
            p = mock.patch.object(trader, name, value)
            p.start()
            self.addCleanup(p.stop)
        trader._BROKER_LEDGER_CACHE.clear()
        self.addCleanup(trader._BROKER_LEDGER_CACHE.clear)
        quiet = mock.patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)


class SettlementTests(Harness):
    def test_the_fabricated_dekeli_profit_becomes_the_real_loss(self):
        """Booked at 22.92 against a 155.65 fill: +25,516 that never happened."""
        r = record("688205", "s1", 22.92, 150.50, 200)
        self.assertGreater(float(r["pnl"]), 25000)
        out = trader._settle_closed_records_from_broker([r])
        self.assertEqual(float(r["entry_price"]), 155.65)
        self.assertEqual(float(r["pnl"]), -1030.00)
        self.assertAlmostEqual(float(r["pnl_pct"]), -3.3087, places=3)
        self.assertEqual(r["price_source"], "broker_fill")
        self.assertEqual(out["settled"], 1)
        self.assertEqual(out["changed"], 1)

    def test_a_sale_that_closed_two_buys_is_settled_whole(self):
        """智度股份: the history kept one lot (-550); the sale lost 2,680."""
        r = record("000676", "s3", 7.72, 7.01, 4000)
        trader._settle_closed_records_from_broker([r])
        self.assertEqual(r["quantity"], "8000")
        self.assertAlmostEqual(float(r["entry_price"]), 7.345, places=4)
        self.assertAlmostEqual(float(r["pnl"]), -2680.00, places=2)

    def test_strategy_fields_are_not_touched(self):
        r = record("688205", "s2", 22.92, 152.34, 200)
        trader._settle_closed_records_from_broker([r])
        self.assertEqual((r["mode"], r["tier"], r["date"]), ("pre_breakout", "2", "2026-08-24"))
        self.assertEqual(r["close_reason"], "信号衰减[大阴线-3.3%]")

    def test_a_record_with_no_sell_order_is_kept_and_labelled(self):
        r = record("688205", "", 22.92, 150.50, 200)
        before = dict(r)
        out = trader._settle_closed_records_from_broker([r])
        self.assertEqual(r["price_source"], "local_unverified")
        self.assertEqual(r["pnl"], before["pnl"])
        self.assertEqual(out["unverified"], 1)

    def test_a_sale_not_yet_in_the_archive_is_left_until_it_is(self):
        """Today's sales reach the archive at 15:02; until then they stand."""
        r = record("600001", "not-archived-yet", 10.0, 9.5, 1000)
        trader._settle_closed_records_from_broker([r])
        self.assertEqual(float(r["pnl"]), -500.00)
        self.assertEqual(r["price_source"], "local_unverified")

    def test_two_records_claiming_one_sale_are_both_left_alone(self):
        a, b = record("688205", "s1", 22.92, 150.50, 100), record("688205", "s1", 22.92, 150.50, 100)
        out = trader._settle_closed_records_from_broker([a, b])
        self.assertEqual(out["settled"], 0)
        self.assertEqual(float(a["entry_price"]), 22.92)

    def test_settling_twice_changes_nothing_more(self):
        r = record("688205", "s1", 22.92, 150.50, 200)
        trader._settle_closed_records_from_broker([r])
        snapshot = dict(r)
        out = trader._settle_closed_records_from_broker([r])
        self.assertEqual(r, snapshot)
        self.assertEqual(out["changed"], 0)

    def test_no_archive_changes_nothing_and_says_so(self):
        self.archive.unlink()
        trader._BROKER_LEDGER_CACHE.clear()
        r = record("688205", "s1", 22.92, 150.50, 200)
        out = trader._settle_closed_records_from_broker([r])
        self.assertEqual(float(r["entry_price"]), 22.92)
        self.assertEqual(out["reason"], "no broker ledger")

    def test_a_corrupt_archive_does_not_raise(self):
        self.archive.write_text("{not json", encoding="utf-8")
        trader._BROKER_LEDGER_CACHE.clear()
        self.assertEqual(trader._settle_closed_records_from_broker([record("688205", "s1", 22.92, 150.5, 200)])["settled"], 0)

    def test_the_scoreboard_then_sums_to_the_broker(self):
        recs = [record("688205", "s1", 22.92, 150.50, 200), record("688205", "s2", 22.92, 152.34, 200),
                record("000676", "s3", 7.72, 7.01, 4000)]
        self.assertGreater(trader.compute_track_stats(recs)["realized_pnl"], 45000)
        trader._settle_closed_records_from_broker(recs)
        self.assertAlmostEqual(trader.compute_track_stats(recs)["realized_pnl"], -1030 - 662 - 2680, places=2)

    def test_the_ledger_is_rebuilt_when_the_archive_changes(self):
        r = record("600001", "s9", 10.0, 9.5, 1000)
        trader._settle_closed_records_from_broker([r])
        self.assertEqual(r["price_source"], "local_unverified")
        more = ORDERS + [order("b9", "600001", "buy", 1000, 10.2, NOW - 3 * DAY),
                         order("s9", "600001", "sell", 1000, 9.5, NOW - 2 * DAY)]
        self.archive.write_text(json.dumps({"orders": more}), encoding="utf-8")
        trader._settle_closed_records_from_broker([r])
        self.assertEqual(r["price_source"], "broker_fill")
        self.assertAlmostEqual(float(r["pnl"]), -700.00, places=2)


class DeadSellOrderTests(Harness):
    """城投控股: recorded sold on 09-09 at 3.97 (-212) by an order that died
    unfilled; really sold on 09-15 at 3.76 (-1,325) by an order no record had."""

    TODAY = china_day(NOW)

    def setUp(self):
        super().setUp()
        self.orders = [
            order("b4", "600649", "buy", 5300, 4.01, NOW - 9 * DAY, "城投控股"),
            dead_order("x4", "600649", 5300, 3.97, NOW - 7 * DAY, "城投控股"),
            order("s4", "600649", "sell", 5300, 3.76, NOW - 3 * DAY, "城投控股"),
        ]
        self.write(self.orders)

    def write(self, orders):
        self.archive.write_text(json.dumps({"orders": orders}), encoding="utf-8")
        trader._BROKER_LEDGER_CACHE.clear()

    def phantom(self, sell_order_id="x4", qty=5300, **extra):
        fields = {"buy_order_ids": "b4", "date": china_day(NOW - 9 * DAY),
                  "sell_date": china_day(NOW - 7 * DAY), "sell_time": "13:15:26", "hold_days": "2"}
        fields.update(extra)
        return record("600649", sell_order_id, 4.01, 3.97, qty, **fields)

    def settle(self, records, today=None):
        return trader._settle_closed_records_from_broker(records, today=today or self.TODAY)

    def assertLeftAlone(self, r, sell_order_id="x4"):
        self.assertEqual(r["sell_order_id"], sell_order_id)
        self.assertEqual(r["price_source"], "local_unverified")
        self.assertEqual(float(r["sell_price"]), 3.97)
        self.assertNotIn("sell_order_id_unfilled", r)

    def test_the_record_takes_the_sale_that_really_closed_it(self):
        r = self.phantom()
        self.assertEqual(float(r["pnl"]), -212.00)
        out = self.settle([r])
        self.assertEqual(r["sell_order_id"], "s4")
        self.assertEqual(r["sell_order_id_unfilled"], "x4")
        self.assertEqual(float(r["sell_price"]), 3.76)
        self.assertEqual(float(r["pnl"]), -1325.00)
        self.assertEqual(r["sell_date"], china_day(NOW - 3 * DAY))
        self.assertEqual(r["hold_days"], "6")
        self.assertEqual(r["price_source"], "broker_fill")
        self.assertEqual((out["settled"], out["repointed"], out["unverified"], out["changed"]), (1, 1, 0, 1))

    def test_strategy_fields_survive_the_re_pointing(self):
        r = self.phantom()
        self.settle([r])
        self.assertEqual((r["mode"], r["tier"], r["date"]), ("pre_breakout", "2", china_day(NOW - 9 * DAY)))
        self.assertEqual(r["close_reason"], "信号衰减[大阴线-3.3%]")

    def test_an_order_from_todays_session_is_not_dead_yet(self):
        r = self.phantom()
        out = self.settle([r], today=china_day(NOW - 7 * DAY))
        self.assertLeftAlone(r)
        self.assertEqual(out["repointed"], 0)

    def test_an_order_the_archive_has_never_seen_is_not_dead(self):
        """Today's real sale before the 15:02 archive must not be handed an old one."""
        r = self.phantom(sell_order_id="placed-today")
        self.settle([r])
        self.assertLeftAlone(r, "placed-today")

    def test_a_filled_order_two_records_claim_is_not_dead(self):
        a, b = self.phantom(sell_order_id="s4"), self.phantom(sell_order_id="s4")
        out = self.settle([a, b])
        self.assertEqual((out["settled"], out["repointed"], out["unverified"]), (0, 0, 2))

    def test_the_sale_must_come_out_of_the_records_own_buy(self):
        r = self.phantom(buy_order_ids="some-other-buy")
        self.settle([r])
        self.assertLeftAlone(r)

    def test_a_record_that_names_no_buy_order_is_left(self):
        r = self.phantom(buy_order_ids="")
        self.settle([r])
        self.assertLeftAlone(r)

    def test_the_quantity_must_be_the_same(self):
        r = self.phantom(qty=5000)
        self.settle([r])
        self.assertLeftAlone(r)

    def test_a_sale_another_record_cites_is_never_handed_out(self):
        r, owner = self.phantom(), record("600649", "s4", 4.01, 3.76, 5300, buy_order_ids="b4")
        out = self.settle([r, owner])
        self.assertLeftAlone(r)
        self.assertEqual(owner["price_source"], "broker_fill")
        self.assertEqual((out["settled"], out["repointed"], out["unverified"]), (1, 0, 1))

    def test_two_records_cannot_share_one_real_sale(self):
        self.write(self.orders + [dead_order("x5", "600649", 5300, 3.90, NOW - 6 * DAY)])
        a, b = self.phantom(), self.phantom(sell_order_id="x5")
        out = self.settle([a, b])
        self.assertLeftAlone(a)
        self.assertLeftAlone(b, "x5")
        self.assertEqual(out["repointed"], 0)

    def test_two_candidate_sales_leave_the_record_alone(self):
        self.write([order("b4", "600649", "buy", 10600, 4.01, NOW - 9 * DAY),
                    dead_order("x4", "600649", 5300, 3.97, NOW - 7 * DAY),
                    order("s4", "600649", "sell", 5300, 3.76, NOW - 3 * DAY),
                    order("s5", "600649", "sell", 5300, 3.70, NOW - 2 * DAY)])
        r = self.phantom()
        self.settle([r])
        self.assertLeftAlone(r)

    def test_a_sale_of_another_stock_is_never_taken(self):
        self.write([order("b4", "600001", "buy", 5300, 4.01, NOW - 9 * DAY),
                    dead_order("x4", "600649", 5300, 3.97, NOW - 7 * DAY),
                    order("s4", "600001", "sell", 5300, 3.76, NOW - 3 * DAY)])
        r = self.phantom()
        self.settle([r])
        self.assertLeftAlone(r)

    def test_the_august_half_sale(self):
        """利尔化学: 7,400 bought, half sold with no record, a sell order died the
        next day, the rest sold the day after. Two records, one on the dead order."""
        self.write([order("b1", "002258", "buy", 7400, 14.38, NOW - 9 * DAY),
                    order("sa", "002258", "sell", 3700, 14.15, NOW - 7 * DAY),
                    dead_order("xd", "002258", 3700, 14.04, NOW - 6 * DAY),
                    order("sb", "002258", "sell", 3700, 14.00, NOW - 5 * DAY)])
        phantom = record("002258", "xd", 14.48, 14.04, 3700, buy_order_ids="b1")
        real = record("002258", "sb", 14.48, 14.00, 3700, buy_order_ids="b1")
        out = self.settle([phantom, real])
        self.assertEqual((phantom["sell_order_id"], real["sell_order_id"]), ("sa", "sb"))
        self.assertEqual(float(phantom["pnl"]), -851.00)
        self.assertEqual(float(real["pnl"]), -1406.00)
        self.assertEqual((out["settled"], out["repointed"], out["unverified"]), (2, 1, 0))

    def test_before_the_archive_has_the_second_sale_neither_record_guesses(self):
        """The same stock on the day of its last sale: that order is not in the
        archive yet, the dead-order record's match is still the only free sale -
        and it is the right one, because the record on today's order never asks."""
        self.write([order("b1", "002258", "buy", 7400, 14.38, NOW - 9 * DAY),
                    order("sa", "002258", "sell", 3700, 14.15, NOW - 7 * DAY),
                    dead_order("xd", "002258", 3700, 14.04, NOW - 6 * DAY)])
        phantom = record("002258", "xd", 14.48, 14.04, 3700, buy_order_ids="b1")
        today = record("002258", "sb", 14.48, 14.00, 3700, buy_order_ids="b1")
        self.settle([phantom, today])
        self.assertEqual(phantom["sell_order_id"], "sa")
        self.assertEqual((today["sell_order_id"], today["price_source"]), ("sb", "local_unverified"))
        self.assertEqual(float(today["pnl"]), -1776.00)

    def test_settling_again_changes_nothing_more(self):
        r = self.phantom()
        self.settle([r])
        snapshot = dict(r)
        out = self.settle([r])
        self.assertEqual(r, snapshot)
        self.assertEqual((out["settled"], out["repointed"], out["changed"]), (1, 0, 0))

    def test_a_broker_order_with_a_garbage_time_is_ignored(self):
        bad = dead_order("x4", "600649", 5300, 3.97, NOW - 7 * DAY)
        bad["time"] = "not-a-time"
        self.write([self.orders[0], bad, self.orders[2]])
        r = self.phantom()
        self.settle([r])
        self.assertLeftAlone(r)


class BooksRefreshTests(Harness):
    """A small account: cash moves only by fills, fees ignored, so costs are 0."""

    def setUp(self):
        super().setUp()
        self.cash0 = 1_000_000.0
        flow = sum((o["tradePrice"] / 100) * o["tradeCount"] * (1 if o["drt"] == 2 else -1) for o in ORDERS)
        self.account = {"total_assets": self.cash0 + flow, "total_pos_value": 0.0}
        self.write_nav([(NOW - 10 * DAY, self.cash0, 0.0)])

    def write_nav(self, rows):
        with open(trader.NAV_FILE, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "time", "tag", "total_assets", "avail_balance", "total_pos_value"])
            for ts, total, pos in rows:
                stamp = datetime.fromtimestamp(ts)
                w.writerow([stamp.strftime("%Y-%m-%d"), stamp.strftime("%H:%M:%S"), "report", total, total - pos, pos])

    def test_the_books_close_and_are_written(self):
        books = trader._refresh_account_books(self.account, [], records=[])
        self.assertEqual(books["residual"], 0.0)
        self.assertAlmostEqual(books["realised_pnl"], -1030 - 662 - 2680 - 1325, places=2)
        self.assertEqual(books["closes"]["count"], 4)
        self.assertTrue(books["verified"])
        self.assertEqual(json.loads(Path(trader.BOOKS_FILE).read_text(encoding="utf-8"))["realised_pnl"],
                         books["realised_pnl"])

    def test_sales_with_no_local_record_are_counted_and_named(self):
        """城投控股 lost 1,325 on 09-15 and no record of it existed."""
        recs = [record("688205", "s1", 155.65, 150.50, 200), record("688205", "s2", 155.65, 152.34, 200),
                record("000676", "s3", 7.345, 7.01, 8000)]
        books = trader._refresh_account_books(self.account, [], records=recs)
        self.assertEqual(books["unattributed"]["count"], 1)
        self.assertAlmostEqual(books["unattributed"]["pnl"], -1325.00, places=2)
        self.assertEqual(books["unattributed"]["items"][0]["code"], "600649")

    def test_a_re_pointed_record_covers_its_real_sale(self):
        """Settled first, the 城投控股 sale is a recorded trade, not a stray loss."""
        self.archive.write_text(json.dumps(
            {"orders": ORDERS + [dead_order("x4", "600649", 5300, 3.97, NOW - 5 * DAY - 3600)]}), encoding="utf-8")
        trader._BROKER_LEDGER_CACHE.clear()
        recs = [record("688205", "s1", 22.92, 150.50, 200), record("688205", "s2", 22.92, 152.34, 200),
                record("000676", "s3", 7.72, 7.01, 4000),
                record("600649", "x4", 4.01, 3.97, 5300, buy_order_ids="b4")]
        trader._settle_closed_records_from_broker(recs, today=china_day(NOW))
        books = trader._refresh_account_books(self.account, [], records=recs)
        self.assertEqual(books["unattributed"]["count"], 0)
        self.assertEqual(books["local_records"], {"closed": 4, "broker_fill": 4, "repointed": 1, "unverified": 0})
        self.assertAlmostEqual(trader.compute_track_stats(recs)["realized_pnl"], books["realised_pnl"], places=2)
        text = "\n".join(trader._books_lines(books))
        self.assertIn("1 笔原卖单未成交", text)
        self.assertNotIn("未能核实", text)
        self.assertNotIn("本地无记录", text)

    def test_fully_settled_records_add_no_line(self):
        recs = [record("688205", "s1", 22.92, 150.50, 200)]
        trader._settle_closed_records_from_broker(recs)
        text = "\n".join(trader._books_lines(trader._refresh_account_books(self.account, [], records=recs)))
        self.assertNotIn("本地记录", text)

    def test_unverified_records_are_counted_in_the_printout(self):
        recs = [record("688205", "never-archived", 22.92, 150.50, 200)]
        trader._settle_closed_records_from_broker(recs)
        text = "\n".join(trader._books_lines(trader._refresh_account_books(self.account, [], records=recs)))
        self.assertIn("本地记录 1 笔: 0 笔按券商成交结算，1 笔未能核实", text)

    def test_a_phantom_position_fails_verification(self):
        books = trader._refresh_account_books(self.account, [{"code": "600649", "count": 100}], records=[])
        self.assertFalse(books["verified"])
        self.assertTrue(any("600649" in p for p in books["problems"]))

    def test_a_stale_archive_is_reported_not_trusted(self):
        old = time.time() - 5 * 3600
        os.utime(self.archive, (old, old))
        trader._BROKER_LEDGER_CACHE.clear()
        books = trader._refresh_account_books(self.account, [], records=[])
        self.assertFalse(books["verified"])
        self.assertFalse(books["position_check"]["checked"])
        self.assertTrue(any("归档" in p for p in books["problems"]))

    def test_a_broken_account_never_raises(self):
        self.assertEqual(trader._refresh_account_books(None, None), trader._refresh_account_books(None, None))

    def test_the_printed_lines_carry_the_identity_and_the_verdict(self):
        recs = [record("688205", "s1", 155.65, 150.50, 200)]
        text = "\n".join(trader._books_lines(trader._refresh_account_books(self.account, [], records=recs)))
        self.assertIn("通过", text)
        self.assertIn("已实现 -5,697", text)
        self.assertIn("平仓 4 笔", text)
        self.assertIn("本地无记录", text)

    def test_no_books_prints_nothing(self):
        self.assertEqual(trader._books_lines({}), [])
        self.assertEqual(trader._books_lines({"verified": False, "problems": ["x"]}), [])


class HistoryCorrectionTests(Harness):
    def test_an_episode_takes_its_own_sale_across_both_lots(self):
        ep = {"code": "000676", "sell_order_id": "s3", "entry_price": 7.72, "sell_price": 7.01,
              "quantity": 4000, "pnl": -550.0, "pnl_pct": -1.42, "sell_date": "2026-08-17", "mode": "m"}
        out = trader._correct_episodes_from_broker_fills([ep])
        self.assertAlmostEqual(ep["pnl"], -2680.00, places=2)
        self.assertEqual(ep["quantity"], 8000)
        self.assertTrue(ep["price_verified"])
        self.assertEqual(ep["mode"], "m")
        self.assertEqual(out["reason"], "ok")

    def test_two_episodes_of_one_stock_cannot_swap_sales(self):
        """Matching by code alone could hand s2's numbers to the s1 episode."""
        e2 = {"code": "688205", "sell_order_id": "s2", "entry_price": 22.92, "pnl": 1.0, "sell_date": "2026-08-26"}
        e1 = {"code": "688205", "sell_order_id": "s1", "entry_price": 22.92, "pnl": 1.0, "sell_date": "2026-08-25"}
        trader._correct_episodes_from_broker_fills([e2, e1])
        self.assertAlmostEqual(e1["pnl"], -1030.00, places=2)
        self.assertAlmostEqual(e2["pnl"], -662.00, places=2)

    def test_sales_no_episode_covers_are_counted(self):
        """Counted from the history's first REAL sale date - the corrected one."""
        ep = {"code": "688205", "sell_order_id": "s1", "entry_price": 22.92, "pnl": 1.0, "sell_date": "2026-01-01"}
        out = trader._correct_episodes_from_broker_fills([ep])
        self.assertEqual(out["unattributed"]["count"], 3)          # s2, s3, s4
        self.assertAlmostEqual(out["unattributed"]["pnl"], -662 - 2680 - 1325, places=2)

    def test_sales_before_the_history_began_are_not_counted(self):
        ep = {"code": "600649", "sell_order_id": "s4", "entry_price": 4.01, "pnl": 1.0, "sell_date": "2026-01-01"}
        out = trader._correct_episodes_from_broker_fills([ep])
        self.assertEqual(out["unattributed"]["count"], 0)          # s1-s3 were all earlier

    def test_an_episode_without_an_id_still_matches_loosely(self):
        """The August behaviour, kept for records that carry no sell order."""
        ep = {"code": "600649", "entry_price": 3.00, "sell_price": 3.76, "quantity": 5300,
              "pnl": 4028.0, "pnl_pct": 25.3, "sell_date": datetime.utcfromtimestamp(NOW - 5 * DAY).strftime("%Y-%m-%d")}
        trader._correct_episodes_from_broker_fills([ep])
        self.assertAlmostEqual(ep["pnl"], -1325.00, places=2)
        self.assertTrue(ep["price_verified"])


class NavLogReadingTests(Harness):
    def test_the_nav_log_is_read_despite_its_byte_order_mark(self):
        """Every CSV here is written utf-8-sig. Read as plain utf-8 the first
        column is '<U+FEFF>date', every row is dropped, and the books would have
        reported 'need two snapshots' forever."""
        with open(trader.NAV_FILE, "w", encoding="utf-8-sig", newline="") as f:
            f.write("date,time,tag,total_assets,avail_balance,total_pos_value\n")
            f.write("2026-09-30,07:06:10,report,1057160.31,432376.07,624784.24\n")
        snaps = trader._nav_snapshots()
        self.assertEqual(len(snaps), 1)
        self.assertEqual(float(snaps[0]["total_assets"]), 1057160.31)

    def test_the_old_default_is_unchanged_until_its_effect_is_measured(self):
        with open(trader.NAV_FILE, "w", encoding="utf-8-sig", newline="") as f:
            f.write("date,time\n2026-09-30,07:06:10\n")
        self.assertIsNone(trader._read_csv_rows(trader.NAV_FILE)[0].get("date"))
        self.assertEqual(trader._read_csv_rows(trader.NAV_FILE, strip_bom=True)[0].get("date"), "2026-09-30")


class WiringTests(unittest.TestCase):
    """Two earlier fixes here passed their unit tests and did nothing live."""

    SRC = (SKILL / "v10_moni_trader.py").read_text(encoding="utf-8")

    def body(self, name):
        start = self.SRC.index("def %s(" % name)
        return self.SRC[start:self.SRC.index("\ndef ", start + 10)]

    def test_records_are_settled_where_everything_loads_them(self):
        body = self.body("load_track_record")
        self.assertLess(body.index("_build_runtime_trade_records("),
                        body.index("_settle_closed_records_from_broker(closed_records)"))

    def test_the_books_are_refreshed_only_at_the_close_report(self):
        body = self.body("write_account_artifacts")
        self.assertIn("if (account_live and tag == 'report')", body)
        self.assertIn("'books': books,", body)

    def test_the_status_printout_shows_the_books(self):
        self.assertIn("_books_lines(_read_json(BOOKS_FILE)", self.body("_print_stats"))

    def test_both_history_writers_carry_the_unattributed_count(self):
        self.assertEqual(self.SRC.count("unattributed_broker_closes"), 2)

    def test_re_pointing_looks_at_every_records_citation(self):
        """Handing it only the unsettled ones would let it give away a sale that
        a settled record already owns."""
        self.assertIn("_real_sales_for_dead_orders(closed_records, ledger,",
                      self.body("_settle_closed_records_from_broker"))

    def test_the_books_module_cannot_trade(self):
        src = (SKILL / "account_books.py").read_text(encoding="utf-8")
        for bad in ("buy_stock", "sell_stock", "execute_trade_action", "mockTrading"):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
