#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The trailing exit may only sell on bars proven to belong to the stock.

In the 14:45 sell runs of 2026-09-21 and 09-22 a pytdx request timed out,
pytdx returned None instead of raising, and the late reply stayed in the socket.
Every later request read the answer to the one before, so each position was
scored on the previous position's prices. 华谊集团 (cost 8.49, broker 8.52) read
通裕重工's 2.78 and logged a -67.3% stop. Nine positions were sold on prices that
were not theirs; real fills were -1.2% to +2.8%.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import v10_moni_trader as mt  # noqa: E402


def daily(closes, start_day=10):
    return [{"datetime": "2026-09-%02d 15:00" % (start_day + i), "open": c, "high": c,
             "low": c, "close": c} for i, c in enumerate(closes)]


class Conn:
    """A pytdx stand-in. `replies` is a list of (bars, quote) per attempt, so a
    test can serve a shifted reply first and a healthy one after a reconnect."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.attempt = 0

    def get_security_bars(self, category, market, code, start, count):
        bars, _ = self.replies[min(self.attempt, len(self.replies) - 1)]
        if isinstance(bars, Exception):
            raise bars
        return bars

    def get_security_quotes(self, securities):
        _, quote = self.replies[min(self.attempt, len(self.replies) - 1)]
        self.attempt += 1
        return [quote] if quote is not None else []

    def to_df(self, rows):
        return pd.DataFrame(rows)


def verdict(conn, code, buy_date, entry, live):
    with mock.patch.object(mt._tdx_hosts, "reconnect_verified", return_value=True) as rc, \
            mock.patch.object(mt, "_bar_is_provisional", return_value=False), \
            mock.patch("builtins.print"):
        v = mt._trail_exit_verdict(conn, code, buy_date, entry, live_price=live)
    return v, rc.call_count


HUAYI_OWN = daily([8.30, 8.41, 8.53])            # 华谊集团's real closes
TONGYU = daily([2.74, 2.78, 2.80])              # 通裕重工's - what it read on 09-21


class TheIncidentTests(unittest.TestCase):
    def test_the_09_21_mixup_does_not_sell(self):
        """Shifted bars, and the quote on the same connection is shifted too."""
        conn = Conn([(TONGYU, {"code": "300185", "price": 2.80})] * 2)
        v, reconnects = verdict(conn, "600623", "2026-09-07", 8.489, 8.52)
        self.assertFalse(v["should_exit"])
        self.assertTrue(v["data_unavailable"])
        self.assertIn("行情校验失败", v["reason"])
        self.assertEqual(reconnects, 1)

    def test_a_shift_that_only_the_broker_can_see_still_does_not_sell(self):
        """Even if the server's quote agreed with the wrong bars, the broker's
        own price - which never touches pytdx - does not."""
        conn = Conn([(TONGYU, {"code": "600623", "price": 2.80})] * 2)
        v, _ = verdict(conn, "600623", "2026-09-07", 8.489, 8.52)
        self.assertFalse(v["should_exit"])
        self.assertTrue(v["data_unavailable"])
        self.assertIn("broker", v["reason"])

    def test_a_reconnect_that_clears_the_shift_gives_a_normal_verdict(self):
        conn = Conn([(TONGYU, {"code": "300185", "price": 2.80}),
                     (HUAYI_OWN, {"code": "600623", "price": 8.52})])
        v, reconnects = verdict(conn, "600623", "2026-09-07", 8.489, 8.52)
        self.assertFalse(v["data_unavailable"])
        self.assertFalse(v["should_exit"])
        self.assertEqual(reconnects, 1)
        self.assertEqual(v["sessions"], 3)


class GenuineMovesStillCountTests(unittest.TestCase):
    def test_a_real_stop_still_sells(self):
        """The check must not become a way to never exit."""
        own = daily([10.0, 9.4, 9.1])
        conn = Conn([(own, {"code": "600000", "price": 9.1})])
        v, reconnects = verdict(conn, "600000", "2026-09-09", 10.0, 9.1)
        self.assertTrue(v["should_exit"])
        self.assertEqual(reconnects, 0)

    def test_a_main_board_limit_move_is_not_mistaken_for_a_mixup(self):
        own = daily([10.0, 10.0, 10.0])
        conn = Conn([(own, {"code": "600000", "price": 10.99})])
        v, _ = verdict(conn, "600000", "2026-09-09", 10.0, 10.99)
        self.assertFalse(v["data_unavailable"])

    def test_a_star_limit_move_is_not_mistaken_for_a_mixup(self):
        """688432 went +20% in a session; the tolerance follows the board."""
        own = daily([45.05, 45.05, 45.05])
        conn = Conn([(own, {"code": "688432", "price": 54.06})])
        v, _ = verdict(conn, "688432", "2026-09-09", 45.656, 54.06)
        self.assertFalse(v["data_unavailable"])

    def test_the_same_move_on_the_main_board_is_rejected(self):
        own = daily([45.05, 45.05, 45.05])
        conn = Conn([(own, {"code": "600000", "price": 54.06})] * 2)
        v, _ = verdict(conn, "600000", "2026-09-09", 45.0, 54.06)
        self.assertTrue(v["data_unavailable"])


class FailureShapesTests(unittest.TestCase):
    def test_a_quote_for_another_code_is_a_shift(self):
        conn = Conn([(HUAYI_OWN, {"code": "600624", "price": 8.52})] * 2)
        v, _ = verdict(conn, "600623", "2026-09-07", 8.489, 8.52)
        self.assertTrue(v["data_unavailable"])
        self.assertIn("answered as 600624", v["reason"])

    def test_no_quote_is_not_trusted(self):
        conn = Conn([(HUAYI_OWN, None)] * 2)
        v, _ = verdict(conn, "600623", "2026-09-07", 8.489, 8.52)
        self.assertTrue(v["data_unavailable"])

    def test_a_request_that_raises_holds_rather_than_sells(self):
        conn = Conn([(OSError("reset"), None)] * 2)
        v, _ = verdict(conn, "600623", "2026-09-07", 8.489, 8.52)
        self.assertFalse(v["should_exit"])
        self.assertTrue(v["data_unavailable"])

    def test_no_bars_at_all_holds(self):
        conn = Conn([([], {"code": "600623", "price": 8.52})] * 2)
        v, _ = verdict(conn, "600623", "2026-09-07", 8.489, 8.52)
        self.assertTrue(v["data_unavailable"])


class BoardLimitTests(unittest.TestCase):
    def test_limits_by_board(self):
        self.assertEqual(mt._board_limit_pct("600623"), 10.0)
        self.assertEqual(mt._board_limit_pct("000029"), 10.0)
        self.assertEqual(mt._board_limit_pct("300185"), 20.0)
        self.assertEqual(mt._board_limit_pct("688432"), 20.0)
        self.assertEqual(mt._board_limit_pct("830799"), 30.0)


class WiringTests(unittest.TestCase):
    def src(self):
        return (SKILL / "v10_moni_trader.py").read_text(encoding="utf-8")

    def test_the_sell_loop_passes_the_broker_price(self):
        self.assertIn("_trail_exit_verdict(tdx_api, code, buy_date, entry_price, live_price=cur_price)",
                      self.src())

    def test_the_trader_connection_runs_without_the_heartbeat_thread(self):
        src = self.src()
        body = src[src.index("def connect_tdx():"):src.index("def _board_limit_pct(")]
        self.assertIn("heartbeat=False", body)
        self.assertNotIn("heartbeat=True", body)

    def test_the_monitor_connection_runs_without_the_heartbeat_thread(self):
        src = (SKILL / "fish_monitor.py").read_text(encoding="utf-8")
        self.assertIn("connect_verified(log=None, heartbeat=False)", src)


if __name__ == "__main__":
    unittest.main()
