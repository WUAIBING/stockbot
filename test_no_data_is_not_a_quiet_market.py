#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Missing data must never read as a market signal.

Two silent conversions ran the account blind from 2026-09-10:

    scanner      [] quotes -> 0亿 -> 清淡市 -> floor 3亿 -> 0 names scanned
    smart-sell   [] bars   -> no evidence -> "信号完好" -> hold

Each is pinned here at the point it happened.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import scanner_v10 as sv  # noqa: E402
import tdx_hosts as th  # noqa: E402
import v10_moni_trader as mt  # noqa: E402


def stocks(n):
    return pd.DataFrame([{"code": "%06d" % i, "name": "s%d" % i, "market": 0}
                         for i in range(n)])


class EmptyApi:
    def get_security_quotes(self, securities):
        return []

    def get_security_bars(self, *a, **k):
        return []

    def to_df(self, rows):
        return pd.DataFrame(rows)


class PricedApi(EmptyApi):
    def get_security_quotes(self, securities):
        return [{"code": c, "price": 10.0, "amount": 2e8} for _m, c in securities]


class ScannerTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(sv, "_resync_after_protocol_error", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def test_an_empty_snapshot_refuses_instead_of_calling_the_market_cold(self):
        """09-10 exactly: every batch empty."""
        with self.assertRaises(th.TdxDataUnavailable):
            sv._collect_amount_snapshot(EmptyApi(), stocks(200))

    def test_a_mostly_empty_snapshot_also_refuses(self):
        """Half the batches dying is not a market fact either."""
        class Partial(PricedApi):
            calls = 0

            def get_security_quotes(self, securities):
                Partial.calls += 1
                return super().get_security_quotes(securities) if Partial.calls == 1 else []
        with self.assertRaises(th.TdxDataUnavailable):
            sv._collect_amount_snapshot(Partial(), stocks(400))

    def test_a_priced_snapshot_still_passes(self):
        """The guard must not refuse a real market."""
        rows = sv._collect_amount_snapshot(PricedApi(), stocks(200))
        self.assertEqual(len(rows), 200)

    def test_the_refusal_is_printed_in_words_an_operator_reads(self):
        with mock.patch("builtins.print") as out:
            with self.assertRaises(th.TdxDataUnavailable):
                sv._collect_amount_snapshot(EmptyApi(), stocks(80))
        printed = " ".join(str(c.args[0]) for c in out.call_args_list if c.args)
        self.assertIn("不是清淡市", printed)


class SmartSellTests(unittest.TestCase):
    def test_no_bars_is_reported_as_no_data_not_intact(self):
        """09-10 exactly: 信号完好 printed over empty charts."""
        detail = mt.evaluate_signal_decay_detail(EmptyApi(), "600057", 6.5, "pre_breakout",
                                                 profit_pct=-6.86)
        self.assertTrue(detail["data_unavailable"])
        self.assertNotEqual(detail["reason"], "信号完好")
        self.assertIn("行情数据缺失", detail["reason"])

    def test_no_bars_never_manufactures_a_sell(self):
        """Blind is not a reason to sell either - it is a reason to say so."""
        detail = mt.evaluate_signal_decay_detail(EmptyApi(), "600057", 6.5, "pre_breakout",
                                                 profit_pct=-6.86)
        self.assertFalse(detail["should_sell"])

    def test_the_sell_loop_counts_blind_positions(self):
        src = (SKILL / "v10_moni_trader.py").read_text(encoding="utf-8")
        body = src[src.index("def _do_sell_core"):src.index("def do_add_position")]
        self.assertIn("data_blind_count += 1", body)
        self.assertIn("行情缺失", body)

    def test_a_missing_api_is_announced_before_the_loop(self):
        src = (SKILL / "v10_moni_trader.py").read_text(encoding="utf-8")
        body = src[src.index("def _do_sell_core"):src.index("def do_add_position")]
        i = body.index("tdx_api = connect_tdx()")
        self.assertIn("if smart and not tdx_api:", body[i:i + 300])

    def test_connect_tdx_returns_none_when_no_host_prices(self):
        with mock.patch.object(th, "connect_verified",
                               side_effect=th.TdxDataUnavailable("none")):
            self.assertIsNone(mt.connect_tdx())


class ProbeTests(unittest.TestCase):
    def test_missing_prices_rank_as_degraded_not_warning(self):
        """33 probe runs said 'warning' through the outage. Nothing reads warnings."""
        src = (SKILL / "data_freshness_probe.py").read_text(encoding="utf-8")
        i = src.index('payload["status"] = "degraded"', src.index("tdx_quote_missing"))
        block = src[i - 400:i]
        self.assertIn('"tdx_daily_missing"', block)
        self.assertIn('"tdx_quote_missing"', block)


if __name__ == "__main__":
    unittest.main()
