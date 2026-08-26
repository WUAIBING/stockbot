#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A failed pytdx call must not leave the connection in use.

pytdx speaks a request/response protocol over one socket: write a request, read
a header, read a body. When the read raises, the bytes for that response are
still in the buffer, so the next request reads THOSE bytes as its own response.
Code N+1 receives code N's data and every code after it is shifted by one.
Nothing raises again, so the corruption is silent and permanent for the run.

Measured on the 2026-08-26 14:49 decision scan:

    rows   0-33   34 rows,   0 corrupt
    rows  34-115  82 rows,  75 corrupt  (91%)

300083 was written as 422.33 against a real 13.78; 688630 as 30.05 against a
real 415.00. The 14:31 prewarm scan of the same session was clean, because
prewarm passes include_5min=False and issues one fewer request per code - the
only structural difference between the two runs.

pytdx itself was exonerated first: queried directly it returned correct closes
for 16/16 codes at counts 3, 50, 120 and 250, and both reachable hosts agreed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import scanner_v10 as s  # noqa: E402


class FakeApi:
    """Minimal pytdx stand-in that can fail a call and record reconnects."""

    def __init__(self, fail_on=(), connect_ok=True):
        self.fail_on = set(fail_on)
        self.connect_ok = connect_ok
        self.calls = 0
        self.disconnects = 0
        self.connects = 0

    def get_security_bars(self, category, market, code, start, count):
        self.calls += 1
        if code in self.fail_on:
            raise ConnectionResetError("short read")
        return [{"datetime": "2026-08-26 15:00", "close": 10.0,
                 "open": 9.9, "high": 10.1, "low": 9.8, "amount": 1e8, "vol": 1000}]

    def to_df(self, bars):
        import pandas as pd
        return pd.DataFrame(bars)

    def disconnect(self):
        self.disconnects += 1

    def connect(self, host, port):
        self.connects += 1
        return self.connect_ok


def reset_log():
    s._TDX_RESYNC_LOG.clear()


class ResyncHelperTests(unittest.TestCase):
    def setUp(self):
        reset_log()
        self.addCleanup(reset_log)

    def test_reconnects_and_reports_success(self):
        api = FakeApi()
        self.assertTrue(s._resync_after_protocol_error(api, "test", "600519"))
        self.assertGreaterEqual(api.disconnects, 1)
        self.assertGreaterEqual(api.connects, 1)

    def test_returns_false_when_every_host_refuses(self):
        api = FakeApi(connect_ok=False)
        self.assertFalse(s._resync_after_protocol_error(api, "test", "600519"))
        self.assertEqual(api.connects, len(s.TDX_HOSTS))

    def test_each_resync_is_recorded(self):
        api = FakeApi()
        s._resync_after_protocol_error(api, "fetch_daily_bars", "600519")
        s._resync_after_protocol_error(api, "fetch_5min_bars_today", "300750")
        self.assertEqual(len(s._TDX_RESYNC_LOG), 2)
        self.assertEqual(s._TDX_RESYNC_LOG[0]["where"], "fetch_daily_bars")
        self.assertEqual(s._TDX_RESYNC_LOG[1]["code"], "300750")

    def test_a_disconnect_that_throws_does_not_stop_recovery(self):
        api = FakeApi()
        api.disconnect = lambda: (_ for _ in ()).throw(OSError("already closed"))
        self.assertTrue(s._resync_after_protocol_error(api, "test", "600519"))


class FetchResyncTests(unittest.TestCase):
    """Every socket-level failure must trigger a resync before returning."""

    def setUp(self):
        reset_log()
        self.addCleanup(reset_log)

    def test_daily_bars_resync_on_socket_error(self):
        api = FakeApi(fail_on={"300083"})
        self.assertIsNone(s.fetch_daily_bars(api, 0, "300083", count=80))
        self.assertEqual(len(s._TDX_RESYNC_LOG), 1)
        self.assertEqual(s._TDX_RESYNC_LOG[0]["where"], "fetch_daily_bars")

    def test_weekly_bars_resync_on_socket_error(self):
        api = FakeApi(fail_on={"300083"})
        self.assertIsNone(s.fetch_weekly_bars(api, 0, "300083", count=100))
        self.assertEqual(len(s._TDX_RESYNC_LOG), 1)
        self.assertEqual(s._TDX_RESYNC_LOG[0]["where"], "fetch_weekly_bars")

    def test_5min_bars_resync_on_socket_error(self):
        """The extra request only the decision scan makes - the actual trigger."""
        api = FakeApi(fail_on={"300083"})
        self.assertIsNone(s.fetch_5min_bars_today(api, 0, "300083"))
        self.assertEqual(len(s._TDX_RESYNC_LOG), 1)
        self.assertEqual(s._TDX_RESYNC_LOG[0]["where"], "fetch_5min_bars_today")

    def test_a_healthy_fetch_never_resyncs(self):
        api = FakeApi()
        self.assertIsNotNone(s.fetch_daily_bars(api, 0, "600519", count=80))
        self.assertIsNotNone(s.fetch_weekly_bars(api, 0, "600519", count=100))
        self.assertEqual(s._TDX_RESYNC_LOG, [])
        self.assertEqual(api.disconnects, 0)

    def test_an_empty_response_is_not_a_protocol_error(self):
        """No exception was raised, so the socket is still in step."""
        api = FakeApi()
        api.get_security_bars = lambda *a, **k: []
        self.assertIsNone(s.fetch_daily_bars(api, 0, "600519", count=80))
        self.assertEqual(s._TDX_RESYNC_LOG, [])

    def test_a_parse_failure_is_not_a_protocol_error(self):
        """The body was read in full; only our own conversion failed.

        Reconnecting here would be harmless but pointless, and it would hide
        how often the socket is genuinely breaking.
        """
        api = FakeApi()
        api.to_df = lambda bars: (_ for _ in ()).throw(ValueError("bad frame"))
        self.assertIsNone(s.fetch_daily_bars(api, 0, "600519", count=80))
        self.assertEqual(s._TDX_RESYNC_LOG, [])

    def test_the_next_code_still_works_after_a_resync(self):
        """The whole point: one bad code must not poison the rest of the run."""
        api = FakeApi(fail_on={"300083"})
        self.assertIsNone(s.fetch_daily_bars(api, 0, "300083", count=80))
        self.assertIsNotNone(s.fetch_daily_bars(api, 0, "688630", count=80))
        self.assertEqual(len(s._TDX_RESYNC_LOG), 1)


if __name__ == "__main__":
    unittest.main()
