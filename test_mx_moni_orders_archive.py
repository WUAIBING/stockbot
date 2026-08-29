#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Archiving the broker order window before it rolls past.

The orders endpoint held 293 records back to 2026-06-01 on 2026-08-29, against
an account 163 days old, and it takes no date range and no pagination. The first
ten weeks are gone along with about 12,090 of realised P&L. Nothing recovers
that; this stops it recurring.

The failure worth guarding is not a crash but a QUIET SHRINK: a short response
merged over a good archive would destroy history exactly the way the window
does, only faster.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import mx_moni_orders_archive as ar  # noqa: E402


def order(oid, code="600000", ts=1780000000, traded=100, price=1000):
    return {"id": oid, "secCode": code, "secName": "X", "drt": 1, "status": 4,
            "time": ts, "priceDec": 2, "price": price, "count": traded,
            "tradeCount": traded, "tradePrice": price}


class ExtractTests(unittest.TestCase):
    """The skill has nested its result differently across versions."""

    def test_data_orders(self):
        self.assertEqual(len(ar.extract_orders({"data": {"orders": [order("1")]}})), 1)

    def test_data_data_orders(self):
        payload = {"data": {"data": {"orders": [order("1"), order("2")]}}}
        self.assertEqual(len(ar.extract_orders(payload)), 2)

    def test_a_bare_list(self):
        self.assertEqual(len(ar.extract_orders([order("1")])), 1)

    def test_nothing_found_is_empty_not_an_error(self):
        self.assertEqual(ar.extract_orders({"data": {"rc": 0}}), [])
        self.assertEqual(ar.extract_orders(None), [])


class MergeTests(unittest.TestCase):
    def test_new_ids_accumulate(self):
        merged = ar.merge_orders([order("1")], [order("2")])
        self.assertEqual(len(merged), 2)

    def test_the_same_id_is_updated_not_duplicated(self):
        """An order legitimately changes from reported to filled."""
        old = order("1", traded=0)
        new = order("1", traded=100)
        merged = ar.merge_orders([old], [new])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["tradeCount"], 100)

    def test_orders_that_aged_out_of_the_window_are_kept(self):
        """This is the entire purpose: the API forgets, the archive does not."""
        merged = ar.merge_orders([order("old", ts=1)], [order("new", ts=2)])
        self.assertEqual([r["id"] for r in merged], ["old", "new"])

    def test_output_is_ordered_by_time(self):
        merged = ar.merge_orders([], [order("b", ts=20), order("a", ts=10)])
        self.assertEqual([r["id"] for r in merged], ["a", "b"])

    def test_rows_without_an_id_still_dedupe(self):
        a = {"secCode": "600000", "time": 5, "tradeCount": 100}
        merged = ar.merge_orders([a], [dict(a)])
        self.assertEqual(len(merged), 1)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_first_run_writes_both_files(self):
        res = ar.archive([order("1")], self.dir, today="2026-08-29")
        self.assertTrue((self.dir / "mx_moni_orders_2026-08-29.json").exists())
        self.assertTrue((self.dir / "mx_moni_orders_merged.json").exists())
        self.assertEqual(res["new_orders"], 1)

    def test_history_accumulates_across_days(self):
        ar.archive([order("1", ts=1)], self.dir, today="2026-08-29")
        res = ar.archive([order("2", ts=2)], self.dir, today="2026-08-30")
        self.assertEqual(res["known_before"], 1)
        self.assertEqual(res["known_after"], 2)

    def test_an_order_that_left_the_window_survives(self):
        """Day two no longer returns order 1; the merge must still hold it."""
        ar.archive([order("1", ts=1), order("2", ts=2)], self.dir, today="2026-08-29")
        ar.archive([order("2", ts=2), order("3", ts=3)], self.dir, today="2026-08-30")
        merged = json.loads((self.dir / "mx_moni_orders_merged.json").read_text(
            encoding="utf-8"))
        self.assertEqual([r["id"] for r in merged["orders"]], ["1", "2", "3"])

    def test_the_daily_snapshot_is_not_rewritten_by_the_merge(self):
        ar.archive([order("1", ts=1)], self.dir, today="2026-08-29")
        snap = json.loads((self.dir / "mx_moni_orders_2026-08-29.json").read_text(
            encoding="utf-8"))
        self.assertEqual(snap["order_count"], 1)
        self.assertEqual(snap["trade_date"], "2026-08-29")

    def test_an_unreadable_archive_is_never_overwritten(self):
        """The merge is a union and can only grow, so the ONLY way to lose
        history here is to treat an unreadable archive as an empty one and
        write a single window over it."""
        ar.archive([order("1"), order("2")], self.dir, today="2026-08-29")
        merged = self.dir / "mx_moni_orders_merged.json"
        merged.write_text("not json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            ar.archive([order("3")], self.dir, today="2026-08-30")
        self.assertEqual(merged.read_text(encoding="utf-8"), "not json")

    def test_the_refusal_says_what_to_do(self):
        merged = self.dir / "mx_moni_orders_merged.json"
        merged.write_text("not json", encoding="utf-8")
        with self.assertRaises(RuntimeError) as ctx:
            ar.archive([order("1")], self.dir, today="2026-08-30")
        self.assertIn("move it aside", str(ctx.exception))

    def test_starting_over_can_be_forced_for_repair(self):
        merged = self.dir / "mx_moni_orders_merged.json"
        merged.write_text("not json", encoding="utf-8")
        res = ar.archive([order("1")], self.dir, today="2026-08-30",
                         allow_shrink=True)
        self.assertEqual(res["known_after"], 1)

    def test_a_missing_archive_is_simply_the_first_run(self):
        """Absent is not the same as unreadable."""
        res = ar.archive([order("1")], self.dir, today="2026-08-29")
        self.assertEqual(res["known_before"], 0)
        self.assertEqual(res["known_after"], 1)


class SafetyTests(unittest.TestCase):
    def test_it_holds_no_trade_or_cancel_path(self):
        src = Path(ar.__file__).read_text(encoding="utf-8")
        for forbidden in ("mockTrading/trade", "mockTrading/cancel", "买入", "卖出"):
            self.assertNotIn(forbidden, src, forbidden)

    def test_it_names_only_the_orders_endpoint(self):
        self.assertEqual(ar.ORDERS_ENDPOINT, "/api/claw/mockTrading/orders")

    def test_an_empty_fetch_never_reaches_the_archive(self):
        """main() returns before archiving when the endpoint gives nothing."""
        src = Path(ar.__file__).read_text(encoding="utf-8")
        self.assertIn("not touching the archive", src)


if __name__ == "__main__":
    unittest.main()
