#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sell quantities must respect the board's minimum lot.

688205 failed six exits on 2026-08-26. The position was 200 shares of a STAR
stock, the exit logic wanted half, rounded it to 100, and the broker rejected
every attempt:

    sell 688205 qty=100 @ 151.96  ->  委托卖出失败，碎股
    sell 688205 qty=100 @ 150.00  ->  碎股
    sell 688205 qty=100 @ 152.82  ->  碎股

STAR (688) trades in 200-share lots. _buy_min_lot already knew this; the sell
path took no code at all and always rounded to 100, so a STAR position could be
entered and then not partially exited - it retried and failed every 30 minutes
for as long as it was held.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import v10_moni_trader as t  # noqa: E402


class SellMinLotTests(unittest.TestCase):
    def test_star_needs_200(self):
        for code in ("688205", "688596", "688131"):
            self.assertEqual(t._sell_min_lot(code), 200, code)

    def test_everything_else_is_100(self):
        for code in ("600519", "002487", "000001", "300750", "301162"):
            self.assertEqual(t._sell_min_lot(code), 100, code)

    def test_matches_the_buy_side(self):
        """Buy and sell must agree, or a position can be entered and not exited."""
        for code in ("688205", "600519", "002487", "300750"):
            self.assertEqual(t._sell_min_lot(code), t._buy_min_lot(code), code)


class NormalizeSellQuantityTests(unittest.TestCase):
    def test_the_688205_incident(self):
        """Half of a 200-share STAR position is not a placeable order.

        Needs position context: without it the function cannot distinguish an
        invalid partial from a genuine sub-lot remnant, and keeps the original
        pass-through so existing callers are unaffected.
        """
        self.assertEqual(
            t._normalize_sell_quantity(100, code="688205", position_qty=200), 0)

    def test_no_position_context_keeps_the_original_pass_through(self):
        """test_core_trading_smoke asserts _normalize_sell_quantity(80) == 80."""
        self.assertEqual(t._normalize_sell_quantity(80), 80)
        self.assertEqual(t._normalize_sell_quantity(100, code="688205"), 100)

    def test_full_star_position_still_sells(self):
        """A complete exit was always valid and must stay valid."""
        self.assertEqual(t._normalize_sell_quantity(200, code="688205"), 200)
        self.assertEqual(t._normalize_sell_quantity(400, code="688205"), 400)

    def test_star_keeps_sizes_at_or_above_the_minimum(self):
        """Superseded an earlier assertion that these round to 200-multiples.

        STAR increments by 1 share above its 200 minimum, so 300 and 599 are
        both placeable orders and must not be rounded down.
        """
        self.assertEqual(t._normalize_sell_quantity(300, code="688205"), 300)
        self.assertEqual(t._normalize_sell_quantity(599, code="688205"), 599)

    def test_main_board_unchanged(self):
        self.assertEqual(t._normalize_sell_quantity(100, code="600519"), 100)
        self.assertEqual(t._normalize_sell_quantity(350, code="600519"), 300)

    def test_default_code_preserves_old_behaviour(self):
        """Callers not yet updated must behave exactly as before."""
        self.assertEqual(t._normalize_sell_quantity(100), 100)
        self.assertEqual(t._normalize_sell_quantity(350), 300)
        self.assertEqual(t._normalize_sell_quantity(0), 0)
        self.assertEqual(t._normalize_sell_quantity(-5), 0)

    def test_true_odd_lot_can_still_be_liquidated(self):
        """A remainder below one lot is sellable, but only as a full exit.

        Otherwise a 100-share STAR remnant becomes permanently unsellable -
        the opposite trap from the one being fixed.
        """
        self.assertEqual(
            t._normalize_sell_quantity(100, code="688205", position_qty=100), 100)
        self.assertEqual(
            t._normalize_sell_quantity(50, code="600519", position_qty=50), 50)

    def test_sub_lot_is_refused_when_the_position_exceeds_a_lot(self):
        """200 held, 100 requested: not an odd-lot liquidation, so refuse."""
        self.assertEqual(
            t._normalize_sell_quantity(100, code="688205", position_qty=200), 0)


class SellableQuantityTests(unittest.TestCase):
    def test_star_position_of_200_is_fully_sellable(self):
        pos = {"code": "688205", "count": 200, "avail_count": 200}
        self.assertEqual(t._sellable_quantity(pos), 200)

    def test_star_position_of_300_is_fully_sellable(self):
        """300 >= the 200 minimum, so the whole position is one legal order."""
        pos = {"code": "688205", "count": 300, "avail_count": 300}
        self.assertEqual(t._sellable_quantity(pos), 300)

    def test_star_remnant_below_a_lot_is_liquidatable(self):
        pos = {"code": "688205", "count": 100, "avail_count": 100}
        self.assertEqual(t._sellable_quantity(pos), 100)

    def test_main_board_is_unaffected(self):
        pos = {"code": "600664", "count": 1600, "avail_count": 1600}
        self.assertEqual(t._sellable_quantity(pos), 1600)

    def test_effective_sellable_respects_the_board(self):
        pos = {"code": "688205", "count": 200, "avail_count": 200}
        self.assertEqual(t._effective_sellable_quantity(pos), 200)
        # 100 already reserved by a pending order -> remainder is a sub-lot
        self.assertEqual(
            t._effective_sellable_quantity(pos, pending_reserved_qty=100), 0)


class RiskTrimTests(unittest.TestCase):
    def test_trimming_a_single_star_lot_exits_it(self):
        """One lot cannot be halved, so a risk-off signal exits it entirely.

        This matches the pre-existing main-board convention: _risk_trim_quantity(100)
        on a 100-share main-board position already returned 100. STAR now follows
        the same rule instead of emitting an unplaceable 100-share order.

        It also unsticks 688205: 200 shares held, risk trim now exits all 200
        rather than failing every 30 minutes.
        """
        self.assertEqual(t._risk_trim_quantity(200, code="688205"), 200)
        self.assertEqual(t._risk_trim_quantity(100, code="600519"), 100)

    def test_trimming_two_star_lots_leaves_one(self):
        trimmed = t._risk_trim_quantity(400, code="688205")
        self.assertGreater(trimmed, 0)
        self.assertEqual(trimmed % 200, 0)
        self.assertLess(trimmed, 400)

    def test_main_board_trim_unchanged(self):
        trimmed = t._risk_trim_quantity(1000, code="600519")
        self.assertGreater(trimmed, 0)
        self.assertEqual(trimmed % 100, 0)
        self.assertLess(trimmed, 1000)

    def test_zero_and_negative(self):
        self.assertEqual(t._risk_trim_quantity(0, code="688205"), 0)
        self.assertEqual(t._risk_trim_quantity(-10, code="688205"), 0)




class StarIncrementTests(unittest.TestCase):
    """STAR requires >=200 shares, then increments by 1 - not by 100 or 200.

    The first version of this fix rounded STAR quantities down to multiples of
    200. That never produced an illegal order, which is why it stopped the six
    rejections, but it discarded granularity the exchange allows. The rounding
    compounds with the 2% NAV cap, both pushing size downward, and it bites
    hardest on STAR: at 150 a share one 200-lot is 30,000, already 2.8% of a
    1.07M account.
    """

    def test_star_uses_single_share_increments(self):
        self.assertTrue(t._uses_single_share_increment("688205"))
        for code in ("600519", "002487", "000001", "300750", "301162"):
            self.assertFalse(t._uses_single_share_increment(code), code)

    def test_star_sell_keeps_odd_sizes_above_the_minimum(self):
        """300 of 500 held is a legal STAR order and must not become 200."""
        self.assertEqual(
            t._normalize_sell_quantity(300, code="688205", position_qty=500), 300)
        self.assertEqual(
            t._normalize_sell_quantity(237, code="688205", position_qty=500), 237)
        self.assertEqual(
            t._normalize_sell_quantity(499, code="688205", position_qty=500), 499)

    def test_star_sell_still_refuses_below_the_minimum(self):
        """The rule that caused the six rejections is unchanged."""
        self.assertEqual(
            t._normalize_sell_quantity(199, code="688205", position_qty=500), 0)
        self.assertEqual(
            t._normalize_sell_quantity(100, code="688205", position_qty=200), 0)

    def test_star_sell_never_exceeds_the_position(self):
        self.assertEqual(
            t._normalize_sell_quantity(900, code="688205", position_qty=500), 500)

    def test_main_board_still_rounds_to_100(self):
        self.assertEqual(
            t._normalize_sell_quantity(350, code="600519", position_qty=1000), 300)
        self.assertEqual(
            t._normalize_sell_quantity(237, code="002487", position_qty=1000), 200)

    def test_star_buy_keeps_odd_sizes(self):
        """30,000 at 137.5 is 218 shares - not 200."""
        self.assertEqual(t.calc_buy_quantity(137.5, 30000, code="688205"), 218)

    def test_star_buy_refuses_below_the_minimum(self):
        """Under one lot's worth of cash buys nothing, rather than a bad order."""
        self.assertEqual(t.calc_buy_quantity(150.0, 20000, code="688205"), 0)

    def test_star_buy_at_exactly_the_minimum(self):
        self.assertEqual(t.calc_buy_quantity(150.0, 30000, code="688205"), 200)

    def test_main_board_buy_still_rounds_to_100(self):
        self.assertEqual(t.calc_buy_quantity(10.0, 12500, code="600519"), 1200)
        self.assertEqual(t.calc_buy_quantity(10.0, 1050, code="600519"), 100)


if __name__ == "__main__":
    unittest.main()
