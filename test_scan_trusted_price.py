#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scan prices must be checked against a source outside pytdx.

On 2026-08-26 the scanner produced 116 rows and 69 of them carried prices that
were wrong - 300083 at 422.33 against a real 13.78, 600186 at 213.27 against a
real 11.55, 688630 at 30.05 against a real 415.00. Ratios ran from 0.07x to
30.65x in both directions.

_price_pair_agrees did not fire once, and could not have: it compares the
realtime quote against the last daily bar, but both arrive through the same
pytdx session. When that session is consistently wrong for a security the two
values agree with each other and disagree only with reality.

Two independent sources - the opening tradability snapshot and TDX desktop's
vipdoc files - agreed on the true price for all 116 rows. Only a source outside
pytdx can catch this, which is what these tests cover.

The order layer already had this check and was blocking three of every four buy
attempts because of it. This moves the same check upstream so the rows never
become candidates.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import scanner_v10 as s  # noqa: E402

# real rows from the 2026-08-26 scan: (code, scanned, true)
REAL_CORRUPT = [
    ("300083", 422.33, 13.78),
    ("600186", 213.27, 11.55),
    ("002192", 289.22, 72.15),
    ("688630", 30.05, 415.00),
    ("688110", 11.35, 113.97),
    ("300870", 52.48, 210.01),
    ("601208", 72.33, 44.30),
    ("300607", 72.03, 36.73),
]
REAL_CLEAN = [
    ("002396", 30.36, 30.84),
]


def reset_cache():
    s._TRUSTED_PRICE_CACHE.update({"loaded": False, "prices": None, "trade_date": ""})


class TrustedReferenceLoadTests(unittest.TestCase):
    def setUp(self):
        reset_cache()
        self.addCleanup(reset_cache)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "opening_tradability_latest.json"
        original = s.TRUSTED_PRICE_FILE
        s.TRUSTED_PRICE_FILE = self.path
        self.addCleanup(lambda: setattr(s, "TRUSTED_PRICE_FILE", original))

    def write(self, payload):
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def test_prices_are_loaded_by_code(self):
        self.write({"trade_date": "2026-08-26", "records": [
            {"code": "300083", "last_close": 13.78},
            {"code": "688630", "last_close": 415.00},
        ]})
        prices = s._load_trusted_reference_prices("2026-08-26")
        self.assertEqual(prices["300083"], 13.78)
        self.assertEqual(prices["688630"], 415.00)

    def test_a_stale_snapshot_disables_the_check(self):
        """Yesterday's closes are a legitimate reason to differ."""
        self.write({"trade_date": "2026-08-25", "records": [
            {"code": "300083", "last_close": 13.78}]})
        self.assertIsNone(s._load_trusted_reference_prices("2026-08-26"))

    def test_a_missing_snapshot_disables_the_check(self):
        self.assertIsNone(s._load_trusted_reference_prices("2026-08-26"))

    def test_an_empty_snapshot_disables_the_check(self):
        self.write({"trade_date": "2026-08-26", "records": []})
        self.assertIsNone(s._load_trusted_reference_prices("2026-08-26"))

    def test_records_without_a_usable_price_are_skipped(self):
        self.write({"trade_date": "2026-08-26", "records": [
            {"code": "300083", "last_close": 0},
            {"code": "600186", "last_close": 11.55},
        ]})
        prices = s._load_trusted_reference_prices("2026-08-26")
        self.assertNotIn("300083", prices)
        self.assertEqual(prices["600186"], 11.55)

    def test_falls_back_through_the_price_fields(self):
        self.write({"trade_date": "2026-08-26", "records": [
            {"code": "600186", "last_close": 0, "last_price": 0, "open_price": 11.40}]})
        self.assertEqual(s._load_trusted_reference_prices("2026-08-26")["600186"], 11.40)

    def test_codes_are_zero_padded(self):
        self.write({"trade_date": "2026-08-26", "records": [
            {"code": 2396, "last_close": 30.84}]})
        self.assertIn("002396", s._load_trusted_reference_prices("2026-08-26"))

    def test_result_is_cached_across_calls(self):
        self.write({"trade_date": "2026-08-26", "records": [
            {"code": "600186", "last_close": 11.55}]})
        first = s._load_trusted_reference_prices("2026-08-26")
        self.path.unlink()
        self.assertIs(s._load_trusted_reference_prices("2026-08-26"), first)


class DisagreementTests(unittest.TestCase):
    REF = {code: true for code, _scan, true in REAL_CORRUPT + REAL_CLEAN}

    def test_every_real_corrupt_row_is_caught(self):
        for code, scanned, _true in REAL_CORRUPT:
            self.assertTrue(
                s._trusted_price_disagrees(code, scanned, self.REF),
                f"{code} scanned {scanned} should be rejected")

    def test_the_one_real_clean_row_is_kept(self):
        for code, scanned, _true in REAL_CLEAN:
            self.assertFalse(
                s._trusted_price_disagrees(code, scanned, self.REF),
                f"{code} scanned {scanned} is a normal intraday move")

    def test_an_ordinary_intraday_move_is_not_rejected(self):
        """The bound is the board limit with headroom, not equality."""
        ref = {"600519": 100.0}
        for price in (100.0, 105.0, 110.0, 118.0, 90.0, 82.0):
            self.assertFalse(s._trusted_price_disagrees("600519", price, ref), price)

    def test_star_gets_the_wider_board_allowance(self):
        """688 runs +/-20%, so its tolerance must exceed the main board's."""
        ref = {"688001": 100.0, "600519": 100.0}
        self.assertTrue(s._trusted_price_disagrees("600519", 130.0, ref))
        self.assertFalse(s._trusted_price_disagrees("688001", 130.0, ref))

    def test_disabled_when_there_is_no_reference(self):
        self.assertFalse(s._trusted_price_disagrees("300083", 422.33, None))
        self.assertFalse(s._trusted_price_disagrees("300083", 422.33, {}))

    def test_a_code_absent_from_the_snapshot_is_not_judged(self):
        self.assertFalse(s._trusted_price_disagrees("999999", 1.0, {"600519": 100.0}))

    def test_non_positive_prices_are_not_judged(self):
        ref = {"600519": 100.0}
        self.assertFalse(s._trusted_price_disagrees("600519", 0, ref))
        self.assertFalse(s._trusted_price_disagrees("600519", -5, ref))

    def test_the_intra_pytdx_check_cannot_catch_these(self):
        """Why the second check exists at all.

        When the session is consistently wrong, the quote and the bar carry the
        same wrong value, so _price_pair_agrees sees perfect agreement.
        """
        for code, scanned, _true in REAL_CORRUPT:
            self.assertTrue(
                s._price_pair_agrees(code, scanned, scanned),
                f"{code}: agreeing-but-wrong pair is invisible to the old check")


if __name__ == "__main__":
    unittest.main()
