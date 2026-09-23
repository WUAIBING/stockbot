#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The monitor reports. It must never trade, and never dress a band as a signal."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import fish_monitor as fm  # noqa: E402


def bars(closes, amount=2e8):
    """Daily bars with a small range around each close."""
    return [{"date": "2026-09-%02d" % (i % 28 + 1), "open": c, "high": c * 1.02,
             "low": c * 0.98, "close": c, "amount": amount} for i, c in enumerate(closes)]


class MeasureTests(unittest.TestCase):
    def test_a_pullback_into_the_band_is_reported_as_in_band(self):
        # 13.2 is under MA5 (13.44) and over MA20 (10.91) - inside the band.
        closes = [10] * 60 + [11, 12, 13, 14, 15] + [13.2]
        m = fm.measure(bars(closes))
        self.assertEqual(m["state"], fm.IN_BAND)
        self.assertLessEqual(m["band_low"], m["close"])
        self.assertLessEqual(m["close"], m["band_high"])

    def test_a_stock_at_its_high_is_above_the_band(self):
        m = fm.measure(bars([10] * 60 + [11, 12, 13, 14, 20]))
        self.assertEqual(m["state"], fm.ABOVE)
        self.assertLess(m["to_band_top_pct"], 0)

    def test_a_broken_stock_is_below_the_band(self):
        m = fm.measure(bars([20] * 60 + [18, 16, 14, 12, 9]))
        self.assertEqual(m["state"], fm.BELOW)
        self.assertGreater(m["to_band_bottom_pct"], 0)

    def test_the_band_orders_itself_when_ma5_is_under_ma20(self):
        """In a downtrend MA5 < MA20; the band must still be low..high."""
        m = fm.measure(bars([20] * 60 + [18, 17, 16, 15, 14]))
        self.assertLessEqual(m["band_low"], m["band_high"])

    def test_run_and_drawdown_describe_the_last_20_sessions(self):
        m = fm.measure(bars([10] * 50 + [10] * 10 + [12, 14, 16, 18, 20][:5]))
        self.assertGreater(m["run20_pct"], 0)
        self.assertLessEqual(m["off_high20_pct"], 0)

    def test_too_little_history_returns_nothing(self):
        self.assertIsNone(fm.measure(bars([10] * 30)))


class WatchlistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name, path in (("WATCHLIST_FILE", "fish_watchlist.json"),
                           ("LATEST_FILE", "fish_monitor_latest.json"),
                           ("HISTORY_FILE", "fish_monitor_history.jsonl")):
            p = mock.patch.object(fm, name, self.tmp / path)
            p.start()
            self.addCleanup(p.stop)

    def test_add_then_list_round_trips(self):
        fm.main(["--add", "688432", "--name", "有研硅", "--note", "deal"])
        items = fm.load_watchlist()
        self.assertEqual(items[0]["code"], "688432")
        self.assertEqual(items[0]["name"], "有研硅")

    def test_adding_twice_does_not_duplicate(self):
        fm.main(["--add", "688432"])
        fm.main(["--add", "688432", "--name", "updated"])
        self.assertEqual(len(fm.load_watchlist()), 1)

    def test_remove(self):
        fm.main(["--add", "688432"])
        fm.main(["--remove", "688432"])
        self.assertEqual(fm.load_watchlist(), [])

    def test_a_missing_watchlist_is_not_an_error(self):
        self.assertEqual(fm.load_watchlist(), [])

    def test_entering_the_band_is_flagged_once_against_last_state(self):
        (self.tmp / "fish_monitor_history.jsonl").write_text(
            json.dumps({"items": [{"code": "688432", "state": fm.ABOVE}]}) + "\n", encoding="utf-8")
        self.assertEqual(fm.previous_states()["688432"], fm.ABOVE)


class SafetyTests(unittest.TestCase):
    def test_it_holds_no_execution_path(self):
        src = (SKILL / "fish_monitor.py").read_text(encoding="utf-8")
        for bad in ("buy_stock", "sell_stock", "execute_trade_action", "mockTrading",
                    "v10_moni_trader", "place_order", "requests.post"):
            self.assertNotIn(bad, src)

    def test_timestamps_are_market_time_not_droplet_utc(self):
        """The droplet runs UTC; a monitor stamping 08:13 for a 16:13 session
        is a monitor nobody can line up against the market."""
        src = (SKILL / "fish_monitor.py").read_text(encoding="utf-8")
        self.assertNotIn("datetime.now().strftime", src)
        self.assertIn("CHINA = timezone(timedelta(hours=8))", src)
        self.assertIn("CST", fm.now_china().strftime("%Y-%m-%d %H:%M:%S CST"))

    def test_it_says_a_band_is_not_a_signal(self):
        """The measured numbers must travel with the tool that shows the band."""
        src = " ".join((SKILL / "fish_monitor.py").read_text(encoding="utf-8").split())
        self.assertIn("not an edge", src)
        self.assertIn("26.1%", src)
        self.assertIn("A band is not a signal", src)


if __name__ == "__main__":
    unittest.main()
