#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The trailing exit must actually be the exit when it is switched on.

Two earlier fixes in this codebase passed their unit tests and changed nothing
live. These pin the path through _do_sell_core, not just the function.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import v10_moni_trader as mt  # noqa: E402


def sell_core_src():
    src = (SKILL / "v10_moni_trader.py").read_text(encoding="utf-8")
    return src[src.index("def _do_sell_core("):src.index("def do_add_position(")]


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        p = mock.patch.object(mt, "DATA_DIR", self.tmp)
        p.start()
        self.addCleanup(p.stop)
        e = mock.patch.dict(os.environ, {}, clear=False)
        e.start()
        self.addCleanup(e.stop)
        os.environ.pop("TLFZ_TRAIL_EXIT", None)

    def test_off_without_the_file(self):
        self.assertFalse(mt._trail_exit_enabled())

    def test_on_with_the_file(self):
        (self.tmp / "TLFZ_TRAIL_EXIT").write_text("on", encoding="utf-8")
        self.assertTrue(mt._trail_exit_enabled())

    def test_env_can_force_it_off_even_with_the_file(self):
        (self.tmp / "TLFZ_TRAIL_EXIT").write_text("on", encoding="utf-8")
        os.environ["TLFZ_TRAIL_EXIT"] = "0"
        self.assertFalse(mt._trail_exit_enabled())


class FakeApi:
    def __init__(self, bars):
        self.bars = bars

    def get_security_bars(self, category, market, code, start, count):
        return self.bars

    def get_security_quotes(self, securities):
        """A healthy server: answers with the requested code, priced at the latest bar."""
        last = sorted(self.bars, key=lambda b: b["datetime"])[-1]["close"] if self.bars else 0
        return [{"code": c, "price": last} for _m, c in securities]

    def to_df(self, rows):
        return pd.DataFrame(rows)


class VerdictTests(unittest.TestCase):
    def test_only_sessions_after_the_buy_are_used(self):
        bars = [{"datetime": "2026-09-01 15:00", "close": 200.0},   # before the buy: ignored
                {"datetime": "2026-09-02 15:00", "close": 100.0},   # buy day: ignored
                {"datetime": "2026-09-03 15:00", "close": 91.0}]    # -9%: stop
        v = mt._trail_exit_verdict(FakeApi(bars), "600000", "2026-09-02", 100.0)
        self.assertEqual(v["sessions"], 1)
        self.assertTrue(v["should_exit"])

    def test_a_bar_still_forming_is_excluded(self):
        """688432 on 09-15: the forming bar said -4.8%, finished closes said +1.1%."""
        bars = [{"datetime": "2026-09-14 15:00", "close": 46.15},
                {"datetime": "2026-09-15 15:00", "close": 43.47}]
        with mock.patch.object(mt, "_bar_is_provisional",
                               side_effect=lambda dt, period="daily": str(dt).startswith("2026-09-15")):
            v = mt._trail_exit_verdict(FakeApi(bars), "688432", "2026-08-28", 45.656)
        self.assertEqual(v["sessions"], 1)
        self.assertFalse(v["should_exit"])

    def test_a_position_bought_today_is_young_not_blind(self):
        """605296 bought 09-15: bars exist, no finished session after the buy.
        It must not be counted as a data failure."""
        bars = [{"datetime": "2026-09-14 15:00", "close": 10.0},
                {"datetime": "2026-09-15 15:00", "close": 10.1}]
        v = mt._trail_exit_verdict(FakeApi(bars), "605296", "2026-09-15", 10.0)
        self.assertFalse(v["should_exit"])
        self.assertFalse(v["data_unavailable"])
        self.assertEqual(v["sessions"], 0)

    def test_a_market_data_failure_reports_blind_instead_of_exiting(self):
        class Broken(FakeApi):
            def get_security_bars(self, *a, **k):
                raise OSError("reset")
        no_reconnect = mock.patch.object(mt._tdx_hosts, "reconnect_verified", return_value=False)
        with no_reconnect, mock.patch("builtins.print"):
            v = mt._trail_exit_verdict(Broken([]), "600000", "2026-09-02", 100.0)
        self.assertFalse(v["should_exit"])
        self.assertTrue(v["data_unavailable"])


class SellLoopWiringTests(unittest.TestCase):
    def test_the_trail_rule_sits_after_the_hard_stop_and_before_the_backstop(self):
        body = sell_core_src()
        stop = body.index("# ── 规则0: 硬止损 ──")
        trail = body.index("elif trail_verdict and trail_verdict.get('should_exit'):")
        backstop = body.index("# ── 规则1: T+N兜底 ──")
        self.assertLess(stop, trail)
        self.assertLess(trail, backstop)

    def test_the_backstop_stretches_to_the_trail_horizon(self):
        self.assertIn("_trail_exit.MAX_HOLD_SESSIONS if trail_on else MAX_HOLD_DAYS", sell_core_src())

    def test_candle_rules_cannot_sell_while_the_trail_is_on(self):
        body = sell_core_src()
        i = body.index("if trail_on:\n")
        self.assertIn("should_sell = False", body[i:i + 700])
        # and that assignment comes before the single gate every decay exit passes
        self.assertLess(i, body.index("            if should_sell:\n"))

    def test_the_switch_is_read_per_run_in_smart_mode_only(self):
        self.assertIn("trail_on = bool(smart) and _trail_exit_enabled()", sell_core_src())

    def test_the_sell_reason_is_recognisable_in_the_record(self):
        self.assertIn('sell_reason = f"trail_exit[', sell_core_src())


class BookSizeTests(unittest.TestCase):
    """Longer holds must not quietly grow the book past its designed size."""

    def buy_core_src(self):
        src = (SKILL / "v10_moni_trader.py").read_text(encoding="utf-8")
        start = src.index("def _do_buy_core(")
        return src[start:src.index("\ndef ", start + 10)]

    def test_book_slots_is_the_declared_tier_config_size(self):
        expected = sum(int(c["max_stocks"]) for c in mt.TIER_CONFIG.values())
        self.assertEqual(mt._book_slots(), expected)

    def test_the_cap_follows_tier_config_rather_than_its_own_number(self):
        with mock.patch.dict(mt.TIER_CONFIG, {1: dict(mt.TIER_CONFIG[1], max_stocks=1),
                                              2: dict(mt.TIER_CONFIG[2], max_stocks=1),
                                              3: dict(mt.TIER_CONFIG[3], max_stocks=1)}):
            self.assertEqual(mt._book_slots(), 3)

    def test_the_cap_runs_only_with_the_trailing_exit(self):
        body = self.buy_core_src()
        self.assertIn("if buy_list and _trail_exit_enabled():", body)
        self.assertIn("book_slots = _book_slots()", body)

    def test_the_cap_counts_holdings_and_orders_in_flight(self):
        body = self.buy_core_src()
        i = body.index("if buy_list and _trail_exit_enabled():")
        self.assertIn("active_pos_map", body[i:i + 400])
        self.assertIn("active_buy_codes", body[i:i + 400])

    def test_the_cap_comes_after_existing_holdings_are_filtered_and_before_cash(self):
        body = self.buy_core_src()
        cap = body.index("if buy_list and _trail_exit_enabled():")
        self.assertLess(body.index("buy_list = filtered_buy_list"), cap)
        self.assertLess(cap, body.index("funded_buy_list = []"))

    def test_a_full_book_is_logged_as_its_own_reason(self):
        self.assertIn("'concurrent_position_cap'", self.buy_core_src())


if __name__ == "__main__":
    unittest.main()
