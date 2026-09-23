#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A host is accepted for the prices it returns, not for answering the phone.

From 2026-09-10 every configured pytdx server kept connecting and kept serving
reference data while returning empty quotes and bars. Every connection routine
chose one anyway. The scanner called the empty market 清淡市 and scanned
nothing; smart-sell called 13 blind evaluations 信号完好 and sold nothing. Three
sessions, no entries, no exits, no error.

These tests use fake servers shaped exactly like that outage.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import tdx_hosts as th  # noqa: E402


class FakeServer:
    """What a pytdx host looks like from the client."""

    def __init__(self, *, connects=True, prices=True, bars=True):
        self.connects, self.prices, self.bars = connects, prices, bars


def factory_for(servers):
    """A TdxHq_API stand-in whose behaviour depends on the host it dials."""
    made = []

    class FakeApi:
        def __init__(self, heartbeat=False):
            self.server = None
            made.append(self)

        def connect(self, host, port, time_out=None):
            srv = servers.get((host, port))
            if srv is None or not srv.connects:
                return False
            self.server = srv
            self.host = (host, port)
            return self

        def disconnect(self):
            self.server = None

        def get_security_quotes(self, securities):
            if not self.server or not self.server.prices:
                return []
            return [{"code": c, "price": 10.0} for _m, c in securities]

        def get_security_bars(self, category, market, code, start, count):
            if not self.server or not self.server.bars:
                return []
            return [{"close": 10.0}]

    FakeApi.made = made
    return FakeApi


class HostPickerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cache = Path(self.tmp) / "last_good.json"
        patches = [
            mock.patch.object(th, "CACHE_FILE", cache),
            mock.patch.object(th, "TDX_HOSTS", [("dead", 1), ("empty", 1), ("good", 1)]),
            mock.patch.object(th, "_builtin_hosts", return_value=[]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_a_connected_but_empty_server_is_rejected(self):
        """THE OUTAGE. Connecting was all the old code checked."""
        api_cls = factory_for({("empty", 1): FakeServer(prices=False, bars=False),
                               ("good", 1): FakeServer()})
        api, where = th.connect_verified(api_cls, log=None)
        self.assertEqual(where, "good:1")

    def test_a_server_with_quotes_but_no_bars_is_rejected(self):
        """Smart-sell reads daily bars. Quotes alone would still leave it blind."""
        api_cls = factory_for({("empty", 1): FakeServer(bars=False),
                               ("good", 1): FakeServer()})
        _api, where = th.connect_verified(api_cls, log=None)
        self.assertEqual(where, "good:1")

    def test_when_nothing_serves_prices_it_raises_rather_than_returning(self):
        """A returned api is a promise of data. None available must be loud."""
        api_cls = factory_for({("empty", 1): FakeServer(prices=False, bars=False),
                               ("good", 1): FakeServer(prices=False, bars=False)})
        with self.assertRaises(th.TdxDataUnavailable):
            th.connect_verified(api_cls, log=None)

    def test_the_failure_says_it_is_not_a_quiet_market(self):
        api_cls = factory_for({})
        with self.assertRaises(th.TdxDataUnavailable) as ctx:
            th.connect_verified(api_cls, log=None)
        self.assertIn("not the same as a quiet market", str(ctx.exception))

    def test_it_is_still_a_runtime_error_for_old_callers(self):
        self.assertTrue(issubclass(th.TdxDataUnavailable, RuntimeError))

    def test_the_winner_is_remembered_and_tried_first(self):
        api_cls = factory_for({("good", 1): FakeServer()})
        th.connect_verified(api_cls, log=None)
        self.assertEqual(th._ordered_candidates()[0], ("good", 1))

    def test_a_remembered_host_that_dies_is_skipped(self):
        """The cache is a hint, not a verdict - it is re-verified every time."""
        th._write_cache("empty", 1)
        api_cls = factory_for({("empty", 1): FakeServer(prices=False),
                               ("good", 1): FakeServer()})
        _api, where = th.connect_verified(api_cls, log=None)
        self.assertEqual(where, "good:1")

    def test_a_corrupt_cache_is_ignored(self):
        th.CACHE_FILE.write_text("not json", encoding="utf-8")
        api_cls = factory_for({("good", 1): FakeServer()})
        _api, where = th.connect_verified(api_cls, log=None)
        self.assertEqual(where, "good:1")

    def test_the_sweep_finds_a_server_outside_the_curated_list(self):
        """How the eight working hosts were found on 09-14."""
        api_cls = factory_for({("empty", 1): FakeServer(prices=False),
                               ("far", 9): FakeServer()})
        with mock.patch.object(th, "_builtin_hosts", return_value=[("far", 9)]):
            _api, where = th.connect_verified(api_cls, log=None)
        self.assertEqual(where, "far:9")

    def test_rejected_connections_are_closed(self):
        """Walking 100 hosts must not leave 100 sockets open."""
        api_cls = factory_for({("empty", 1): FakeServer(prices=False),
                               ("good", 1): FakeServer()})
        th.connect_verified(api_cls, log=None)
        open_empty = [a for a in api_cls.made
                      if a.server is not None and getattr(a, "host", None) == ("empty", 1)]
        self.assertEqual(open_empty, [])

    def test_reconnect_keeps_the_same_object(self):
        api_cls = factory_for({("empty", 1): FakeServer(prices=False),
                               ("good", 1): FakeServer()})
        api = api_cls()
        self.assertTrue(th.reconnect_verified(api, log=None))
        self.assertEqual(api.host, ("good", 1))

    def test_reconnect_reports_failure(self):
        api_cls = factory_for({})
        self.assertFalse(th.reconnect_verified(api_cls(), log=None))


class ServesPricesTests(unittest.TestCase):
    def test_one_exchange_priced_is_not_enough(self):
        class Half:
            def get_security_quotes(self, s):
                return [{"price": 10.0}, {"price": 0}]

            def get_security_bars(self, *a):
                return [{}]
        self.assertFalse(th.serves_prices(Half()))

    def test_an_exception_means_no(self):
        class Boom:
            def get_security_quotes(self, s):
                raise OSError("reset")
        self.assertFalse(th.serves_prices(Boom()))

    def test_none_responses_mean_no(self):
        class Nones:
            def get_security_quotes(self, s):
                return None

            def get_security_bars(self, *a):
                return None
        self.assertFalse(th.serves_prices(Nones()))


class OneListTests(unittest.TestCase):
    """Six modules each carried a stale copy. Now they must share one."""

    def test_every_live_module_uses_the_shared_list(self):
        for name in ("scanner_v10.py", "v10_moni_trader.py", "data_freshness_probe.py",
                     "live_quote.py", "v10_watchdog.py"):
            src = (SKILL / name).read_text(encoding="utf-8")
            self.assertIn("TDX_HOSTS = _tdx_hosts.TDX_HOSTS", src, name)
            self.assertNotIn('("218.75.126.9", 7709),\n    ("60.191', src, name)

    def test_no_live_module_accepts_a_bare_connect(self):
        """The pattern that caused it: `if api.connect(...): return api`."""
        for name in ("scanner_v10.py", "v10_moni_trader.py", "live_quote.py", "v10_watchdog.py"):
            src = (SKILL / name).read_text(encoding="utf-8")
            start = src.index("def connect_tdx") if "def connect_tdx" in src else src.index("def _connect")
            body = src[start:start + 900]
            self.assertIn("connect_verified", body, name)

    def test_the_verified_hosts_lead_the_list(self):
        self.assertEqual(th.TDX_HOSTS[:len(th.VERIFIED_HOSTS)], th.VERIFIED_HOSTS)


class SafetyTests(unittest.TestCase):
    def test_it_holds_no_execution_path(self):
        src = (SKILL / "tdx_hosts.py").read_text(encoding="utf-8")
        for bad in ("buy_stock", "sell_stock", "execute_trade_action",
                    "mockTrading/trade", "mockTrading/cancel", "requests."):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
