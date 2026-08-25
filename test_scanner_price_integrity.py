#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the scanner's price-integrity guards.

The scan CSV was emitting entry_price values that never occurred for the stock
they were attached to:

    688205  scan 2026-08-24  entry_price  22.92   real ~156
    688596  scan 2026-08-25  entry_price  36.79   real  ~71
    688131  scan 2026-07-15  entry_price  30.92   real  ~91

Across 71 trading days of 5-minute bars, none of those prices ever occurred for
those securities. TDX desktop, pytdx and the MX broker all agreed with each
other and disagreed with the scanner, so the fault was inside the scan.

entry_price comes from the realtime quote batch. That batch had a fallback that
matched responses to requests by list position, which misattributes every price
in the batch if the server reorders or omits one - and produces values that look
entirely plausible.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import scanner_v10 as s  # noqa: E402


class BoardLimitTests(unittest.TestCase):
    def test_star_and_chinext_get_20(self):
        for code in ("688205", "300750", "301162"):
            self.assertEqual(s._board_daily_limit_pct(code), 20.0, code)

    def test_beijing_gets_30(self):
        for code in ("430047", "830799", "871981", "920002"):
            self.assertEqual(s._board_daily_limit_pct(code), 30.0, code)

    def test_main_board_gets_10(self):
        for code in ("600519", "002487", "000001", "601318"):
            self.assertEqual(s._board_daily_limit_pct(code), 10.0, code)


class PricePairAgreementTests(unittest.TestCase):
    """The real incidents must be rejected; real intraday moves must not be."""

    def test_rejects_the_688205_incident(self):
        # quote said 22.92, the daily bar said 156.00
        self.assertFalse(s._price_pair_agrees("688205", 22.92, 156.00))

    def test_rejects_the_688596_incident(self):
        self.assertFalse(s._price_pair_agrees("688596", 36.79, 70.60))

    def test_rejects_the_688131_incident(self):
        self.assertFalse(s._price_pair_agrees("688131", 30.92, 91.40))

    def test_rejects_the_002487_incident(self):
        self.assertFalse(s._price_pair_agrees("002487", 7.42, 42.10))

    def test_allows_a_normal_intraday_gap(self):
        """Quote is live, the bar may be the prior close - a few % is expected."""
        self.assertTrue(s._price_pair_agrees("600519", 1304.0, 1298.0))
        self.assertTrue(s._price_pair_agrees("002487", 35.16, 34.90))

    def test_allows_a_full_limit_move_on_the_main_board(self):
        """+10% is the most a main-board stock can move; must not trip."""
        self.assertTrue(s._price_pair_agrees("600519", 110.0, 100.0))

    def test_allows_a_full_limit_move_on_star(self):
        self.assertTrue(s._price_pair_agrees("688205", 120.0, 100.0))

    def test_rejects_beyond_headroom(self):
        """Main board tolerance is max(2x10, 25) = 25%."""
        self.assertTrue(s._price_pair_agrees("600519", 124.0, 100.0))
        self.assertFalse(s._price_pair_agrees("600519", 126.0, 100.0))

    def test_star_headroom_is_wider(self):
        """STAR tolerance is max(2x20, 25) = 40%."""
        self.assertTrue(s._price_pair_agrees("688205", 139.0, 100.0))
        self.assertFalse(s._price_pair_agrees("688205", 141.0, 100.0))

    def test_missing_values_do_not_reject(self):
        """A detector, not a completeness check - must not drop the universe."""
        self.assertTrue(s._price_pair_agrees("600519", 0, 100.0))
        self.assertTrue(s._price_pair_agrees("600519", 100.0, 0))
        self.assertTrue(s._price_pair_agrees("600519", None, None))

    def test_symmetric_enough_in_both_directions(self):
        """Whichever source is wrong, the pair must be rejected."""
        self.assertFalse(s._price_pair_agrees("688205", 156.00, 22.92))
        self.assertFalse(s._price_pair_agrees("688205", 22.92, 156.00))


class MismatchLogTests(unittest.TestCase):
    """The positional-matching fallback is deliberate and stays.

    An earlier version of this change removed it, on a theory that the batch
    quote response could be reordered. That broke five existing tests which
    encode positional matching as intended behaviour for uncoded responses -
    and the theory was never confirmed. The TDX wire protocol returns quotes in
    request order, so the fallback is defensible.

    The cross-check below is mechanism-agnostic: whatever produced the bad
    price, two independent sources disagreeing is enough to reject the row.
    """

    def test_mismatch_log_exists(self):
        self.assertIsInstance(s._PRICE_MISMATCH_LOG, list)


if __name__ == "__main__":
    unittest.main()
