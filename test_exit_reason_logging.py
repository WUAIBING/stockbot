#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""An exit can only be judged by what the stock did after it fired.

Over 40 days the exits were 97x 冲高回落 and 45x 连跌2日 against 7 T+5 expiries,
so the decay rules - not the T+5 backstop - are what cut holds short. Neither
shows a reliable forward signal: measured across 9.1M stock-days, returns AFTER
they fire were +0.17/+0.23 vs baseline on train and -0.03/-0.20 on holdout. The
sign flips and both magnitudes sit inside the noise floor.

That test used all stock-days rather than held positions, which is exactly why
the counterfactual has to be measured live. Logging the reason on the sell is
what makes that possible.
"""
from __future__ import annotations
import sys, unittest
from pathlib import Path
SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))
import v10_moni_trader as t  # noqa: E402


class ContextTests(unittest.TestCase):
    """execute_trade_action must carry the reason into the order context."""

    def ctx(self, **kw):
        captured = {}
        real = t.sell_stock
        def fake(code, quantity, ref_price=None, order_context=None, **rest):
            captured.update(order_context or {})
            return {"success": True, "result": {}, "result_code": "200", "order_id": "x"}
        t.sell_stock = fake
        try:
            t.execute_trade_action("sell", "688205", 200, ref_price=152.33, **kw)
        finally:
            t.sell_stock = real
        return captured

    def test_reason_reaches_the_order_context(self):
        c = self.ctx(exit_reason="risk_trim[冲高回落上影线2.0% | 连跌2日]")
        self.assertIn("冲高回落", c["exit_reason"])
        self.assertIn("连跌2日", c["exit_reason"])

    def test_absent_reason_is_blank_not_missing(self):
        c = self.ctx()
        self.assertEqual(c["exit_reason"], "")

    def test_existing_context_keys_are_preserved(self):
        c = self.ctx(execution_phase="primary", strategy_action="smart_sell",
                     exit_reason="T+5到期(持仓5天)")
        self.assertEqual(c["execution_phase"], "primary")
        self.assertEqual(c["strategy_action"], "smart_sell")
        self.assertEqual(c["exit_reason"], "T+5到期(持仓5天)")

    def test_reason_is_length_capped(self):
        c = self.ctx(exit_reason="x" * 500)
        self.assertEqual(len(c["exit_reason"]), 200)

    def test_none_and_whitespace_normalise_to_blank(self):
        for bad in (None, "", "   "):
            self.assertEqual(self.ctx(exit_reason=bad)["exit_reason"], "")

    def test_buys_are_unaffected(self):
        captured = {}
        real = t.buy_stock
        def fake(code, quantity, ref_price=None, order_context=None, **rest):
            captured.update(order_context or {})
            return {"success": True, "result": {}, "result_code": "200", "order_id": "x"}
        t.buy_stock = fake
        try:
            t.execute_trade_action("buy", "002396", 700, ref_price=30.36,
                                   strategy_action="buy")
        finally:
            t.buy_stock = real
        self.assertEqual(captured["strategy_action"], "buy")
        self.assertEqual(captured["exit_reason"], "")


class BucketTests(unittest.TestCase):
    """The analyser must collapse free text onto the rule that produced it."""

    def setUp(self):
        sys.path.insert(0, str(SKILL))
        import evaluate_exit_rules as e
        self.e = e

    def test_real_reason_strings_map_to_their_rule(self):
        cases = [
            ("risk_trim[冲高回落上影线2.0% | 连跌2日](持仓2天)", "冲高回落"),
            ("T+5到期(持仓5天)", "T+5到期"),
            ("浮盈+2.4%落袋为安", "落袋为安"),
            ("中高盈利+9.2%且信号转弱", "中高盈利"),
            ("高盈利+16.1%转弱优先兑现", "高盈利"),
        ]
        for text, want in cases:
            self.assertEqual(self.e.bucket_label(text, ""), want, text)

    def test_unlogged_exits_are_named_not_dropped(self):
        """Sells recorded before this change have no reason; they must stay visible."""
        self.assertIn("unlogged", self.e.bucket_label("", "smart_sell"))
        self.assertIn("smart_sell", self.e.bucket_label("", "smart_sell"))

    def test_unknown_reason_is_truncated_not_discarded(self):
        out = self.e.bucket_label("some brand new reason text here", "")
        self.assertTrue(out)
        self.assertLessEqual(len(out), 24)


if __name__ == "__main__":
    unittest.main()


class RankingBuilderResyncTests(unittest.TestCase):
    """The ranking builder had the same swallowed-socket bug as the scanner.

    It walks ~5,200 stocks per trade date - far more requests than any other
    caller - and its output feeds the template search, the distill pool and
    every backtest built on them. The damage is visible in the artifacts: the
    2026-08-25 ranking carries 002396 at 7.03 against a real 30.84, and 300083
    at 3.45 against 13.78.
    """

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent
                               / "workbuddy_distill" / "scripts"))
        import build_tdx_rankings as b
        self.b = b
        b.RESYNC_LOG.clear()
        self.addCleanup(b.RESYNC_LOG.clear)

    class FakeApi:
        def __init__(self, fail=False, connect_ok=True):
            self.fail, self.connect_ok = fail, connect_ok
            self.connects = self.disconnects = 0

        def get_security_bars(self, cat, market, code, start, count):
            if self.fail:
                raise ConnectionResetError("short read")
            return [{"datetime": "2026-08-26", "close": 10.0, "open": 9.9,
                     "high": 10.1, "low": 9.8, "amount": 1e8, "vol": 1000}]

        def to_df(self, bars):
            import pandas as pd
            return pd.DataFrame(bars)

        def connect(self, host, port, time_out=None):
            self.connects += 1
            return self.connect_ok

        def disconnect(self):
            self.disconnects += 1

    def test_socket_error_triggers_a_reconnect(self):
        api = self.FakeApi(fail=True)
        self.assertIsNone(self.b.fetch_daily_frame(api, 0, "300083", 60))
        self.assertEqual(len(self.b.RESYNC_LOG), 1)
        self.assertEqual(self.b.RESYNC_LOG[0]["code"], "300083")
        self.assertGreaterEqual(api.connects, 1)

    def test_healthy_fetch_never_reconnects(self):
        api = self.FakeApi()
        self.assertIsNotNone(self.b.fetch_daily_frame(api, 0, "002396", 60))
        self.assertEqual(self.b.RESYNC_LOG, [])
        self.assertEqual(api.disconnects, 0)

    def test_empty_response_is_not_a_protocol_error(self):
        api = self.FakeApi()
        api.get_security_bars = lambda *a, **k: []
        self.assertIsNone(self.b.fetch_daily_frame(api, 0, "002396", 60))
        self.assertEqual(self.b.RESYNC_LOG, [])

    def test_reports_failure_when_no_host_answers(self):
        api = self.FakeApi(connect_ok=False)
        self.assertFalse(self.b.resync_after_protocol_error(api, "002396"))
        self.assertEqual(api.connects, len(self.b.TDX_HOSTS))

    def test_the_next_stock_still_works_after_a_resync(self):
        """The whole point: one bad code must not poison the remaining 5,200."""
        api = self.FakeApi(fail=True)
        self.assertIsNone(self.b.fetch_daily_frame(api, 0, "300083", 60))
        api.fail = False
        self.assertIsNotNone(self.b.fetch_daily_frame(api, 0, "002396", 60))
        self.assertEqual(len(self.b.RESYNC_LOG), 1)
