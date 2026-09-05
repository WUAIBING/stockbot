#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""What the BROKER fills, not what the exchange calendar says.

The first version of this module refused everything outside exchange hours.
The account's own record says that is wrong. Of 20 orders placed after 15:00:

    15:04 - 15:27   10 orders   status 9    0 filled
    17:54 - 21:49   10 orders   status 8/4  6 filled

Evening orders QUEUE and fill - standard 盘后委托, and the only route we have to
an opening fill, which is exactly what open_exit.py needs. Blocking them was
the error.

The dead window is narrow and certain: 15:00 to ~17:00, zero of ten.

Lunch stays UNTESTED: two orders ever, one filled. Two observations decide
nothing, so it is a caller flag defaulting to the cautious side - not a finding.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import market_hours as mh  # noqa: E402


def at(h, m, tz=True):
    d = datetime(2026, 9, 3, h, m)
    return d.replace(tzinfo=mh.CHINA) if tz else d


class SessionTests(unittest.TestCase):
    def test_the_call_auction_accepts_orders(self):
        """09:15-09:25 orders are matched at 09:25 - this is not 'closed'."""
        self.assertEqual(mh.session_state(at(9, 20))[0], mh.AUCTION)

    def test_the_gap_after_the_auction_is_closed(self):
        self.assertEqual(mh.session_state(at(9, 27))[0], mh.CLOSED)

    def test_the_morning_session(self):
        for h, m in ((9, 30), (10, 15), (11, 30)):
            self.assertEqual(mh.session_state(at(h, m))[0], mh.CONTINUOUS, (h, m))

    def test_lunch_is_marked_untested_not_closed(self):
        """Two orders ever placed at lunch, one filled. That decides nothing,
        so the state says so rather than pretending to know."""
        state, why = mh.session_state(at(12, 0))
        self.assertEqual(state, mh.LUNCH_UNTESTED)
        self.assertIn("too few to call", why)

    def test_the_afternoon_session(self):
        for h, m in ((13, 0), (14, 51), (15, 0)):
            self.assertEqual(mh.session_state(at(h, m))[0], mh.CONTINUOUS, (h, m))

    def test_the_dead_window_is_just_after_the_close(self):
        """15:04-15:27: ten orders, status 9, none filled."""
        for h, m in ((15, 4), (15, 27), (16, 0)):
            self.assertEqual(mh.session_state(at(h, m))[0], mh.DEAD, (h, m))

    def test_the_evening_queues_rather_than_dies(self):
        """THE CORRECTION. 17:54-21:49 filled 6 of 10 - they queue to the next
        session, which is the mechanism open_exit needs."""
        for h, m in ((17, 54), (19, 30), (21, 49)):
            state, why = mh.session_state(at(h, m))
            self.assertEqual(state, mh.QUEUED, (h, m))
            self.assertIn("queue", why)

    def test_before_the_auction_is_closed(self):
        self.assertEqual(mh.session_state(at(8, 0))[0], mh.CLOSED)

    def test_a_non_trading_day_is_closed_at_any_hour(self):
        self.assertEqual(mh.session_state(at(10, 0), trading_day=False)[0], mh.CLOSED)

    def test_every_state_carries_a_reason(self):
        for h, m in ((8, 0), (9, 20), (9, 27), (10, 0), (12, 0), (14, 0), (16, 0)):
            self.assertTrue(mh.session_state(at(h, m))[1], (h, m))


class PlacementTests(unittest.TestCase):
    """The guard itself."""

    def test_orders_are_allowed_in_session(self):
        self.assertTrue(mh.can_place_order(at(10, 0))[0])
        self.assertTrue(mh.can_place_order(at(14, 51))[0])

    def test_orders_are_allowed_in_the_auction(self):
        self.assertTrue(mh.can_place_order(at(9, 20))[0])

    def test_orders_are_refused_in_the_dead_window(self):
        """The one unambiguous refusal: zero fills from ten orders."""
        ok, why = mh.can_place_order(at(15, 4))
        self.assertFalse(ok)
        self.assertIn("none filled", why)

    def test_evening_orders_are_allowed_by_default(self):
        """They fill. Refusing them would remove our only opening-fill route."""
        ok, why = mh.can_place_order(at(19, 30))
        self.assertTrue(ok)
        self.assertIn("queue", why)

    def test_evening_can_be_refused_by_the_caller(self):
        self.assertFalse(mh.can_place_order(at(19, 30), allow_queued=False)[0])

    def test_lunch_is_a_caller_choice_not_a_finding(self):
        """Cautious default, but the flag exists because the data is silent."""
        self.assertFalse(mh.can_place_order(at(12, 0))[0])
        self.assertTrue(mh.can_place_order(at(12, 0), allow_lunch=True)[0])

    def test_orders_are_refused_on_a_holiday(self):
        self.assertFalse(mh.can_place_order(at(10, 0), trading_day=False)[0])

    def test_the_exact_failing_timestamps_are_refused(self):
        """06-04 15:04 and 06-05 15:27 - real orders, real zero fills."""
        self.assertFalse(mh.can_place_order(at(15, 4))[0])
        self.assertFalse(mh.can_place_order(at(15, 27))[0])

    def test_the_exact_filling_timestamps_are_allowed(self):
        """08-10 19:30 and 19:31 - real orders that really filled."""
        self.assertTrue(mh.can_place_order(at(19, 30))[0])
        self.assertTrue(mh.can_place_order(at(19, 31))[0])


class TimeRemainingTests(unittest.TestCase):
    """A limit with nine minutes to live is not a limit with three hours."""

    def test_none_left_when_shut(self):
        self.assertEqual(mh.minutes_left(at(15, 30)), 0)
        self.assertEqual(mh.minutes_left(at(19, 30)), 0)   # queued, not trading
        self.assertEqual(mh.minutes_left(at(10, 0), trading_day=False), 0)

    def test_the_close_node_has_nine_minutes(self):
        """14:51 is when the system buys. Nine minutes, not a day."""
        self.assertEqual(mh.minutes_left(at(14, 51)), 9)

    def test_the_morning_sweep_has_most_of_the_day(self):
        self.assertEqual(mh.minutes_left(at(9, 45)), 105 + 120)

    def test_morning_time_includes_the_afternoon(self):
        """A morning order can still fill after lunch, so the count spans it."""
        self.assertGreater(mh.minutes_left(at(11, 0)), 120)

    def test_it_never_goes_negative(self):
        for h, m in ((15, 0), (14, 59), (11, 30)):
            self.assertGreaterEqual(mh.minutes_left(at(h, m)), 0, (h, m))


class NaiveDatetimeTests(unittest.TestCase):
    """The droplet runs UTC; a naive datetime there is eight hours out."""

    def test_a_naive_time_is_read_as_china_time(self):
        self.assertEqual(mh.session_state(at(10, 0, tz=False))[0], mh.CONTINUOUS)

    def test_a_utc_time_is_converted_not_assumed(self):
        utc = datetime(2026, 9, 3, 2, 0, tzinfo=timezone.utc)   # 10:00 China
        self.assertEqual(mh.session_state(utc)[0], mh.CONTINUOUS)

    def test_a_utc_time_after_the_china_close_is_dead(self):
        utc = datetime(2026, 9, 3, 7, 30, tzinfo=timezone.utc)  # 15:30 China
        self.assertEqual(mh.session_state(utc)[0], mh.DEAD)


class ExchangeRuleTests(unittest.TestCase):
    """The rule is real. The platform does not honour it.

    盘后固定价格交易 permits 15:05-15:30 trading at the close on STAR and
    ChiNext. Four eligible-board orders inside that exact window - 688726 at
    15:09, 688313 at 15:27 - all returned status 9 with zero fills.
    """

    def test_an_eligible_board_is_still_refused_in_the_window(self):
        """A 688 name at 15:09 is legal under the exchange rule and still does
        not fill here. The guard follows the platform, not the rulebook."""
        self.assertFalse(mh.can_place_order(at(15, 9))[0])
        self.assertFalse(mh.can_place_order(at(15, 27))[0])

    def test_the_module_records_why_the_rule_does_not_apply(self):
        src = Path(mh.__file__).read_text(encoding="utf-8")
        self.assertIn("盘后固定价格交易", src)
        self.assertIn("688726", src)
        self.assertIn("does not implement", src.lower())


class SafetyTests(unittest.TestCase):
    def test_it_holds_no_execution_path(self):
        src = Path(mh.__file__).read_text(encoding="utf-8")
        for bad in ("buy_stock", "sell_stock", "execute_trade_action",
                    "mockTrading/trade", "mockTrading/cancel", "requests.post"):
            self.assertNotIn(bad, src)

    def test_it_does_not_fork_the_holiday_calendar(self):
        """trading_day is passed in, because trading_calendar owns holidays and
        this project already shipped two tables, one wrong about 2026-09-28."""
        src = Path(mh.__file__).read_text(encoding="utf-8")
        self.assertIn("trading_day", src)
        self.assertNotIn("MARKET_HOLIDAYS", src)


if __name__ == "__main__":
    unittest.main()
