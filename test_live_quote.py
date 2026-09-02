#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A price without its timestamp is a wrong answer waiting to happen.

On 2026-09-02 the position cache was stamped 11:15:30 and I reported it as the
lunch close. The price had moved 7.26 -> 7.18 in those fifteen minutes, so the
number was not merely stale - it was wrong for the question asked. 002396 was
worse: cache 40.41, actual 41.51, so the book was reported nearly 1.1 points
per share light on its largest winner.

Third time in one session. The 09:31 gate got called "the whole market" when it
is CSI 1000; 09:31 breadth got quoted at 09:46 as current; then this. The fault
is never bad data - it is reporting the freshest number available as though it
were the current number.

Hence: every quote carries fetched_at and age, and the formatter refuses to
print a price that has neither.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import live_quote as lq  # noqa: E402


class MarketRoutingTests(unittest.TestCase):
    """Wrong market id returns a plausible price for the WRONG security.

    That is the pytdx desync signature: it put 22.92 in the ledger for a stock
    trading at 155.65, and nothing raised.
    """

    def test_shanghai_codes(self):
        for c in ("600403", "603039", "601609", "688432"):
            self.assertEqual(lq.market_of(c), lq.MARKET_SH, c)

    def test_shenzhen_codes(self):
        for c in ("000636", "002396", "300083", "301217"):
            self.assertEqual(lq.market_of(c), lq.MARKET_SZ, c)

    def test_beijing_codes(self):
        for c in ("830799", "920123"):
            self.assertEqual(lq.market_of(c), lq.MARKET_BJ, c)

    def test_a_non_code_is_none_not_a_guess(self):
        for bad in ("", "   ", None, "abc", "1234567", "60040x", "-1"):
            self.assertIsNone(lq.market_of(bad), repr(bad))

    def test_empty_input_does_not_become_a_real_looking_code(self):
        """THE BUG. "".zfill(6) is "000000" - a syntactically perfect Shenzhen
        code for a security that does not exist. Validate before padding."""
        self.assertIsNone(lq.market_of(""))
        self.assertIsNone(lq.market_of(None))

    def test_short_numeric_input_still_pads(self):
        """Codes arrive from JSON as ints; 636 must still reach 000636."""
        self.assertEqual(lq.market_of(636), lq.MARKET_SZ)
        self.assertEqual(lq.market_of("636"), lq.MARKET_SZ)

    def test_codes_are_zero_padded(self):
        self.assertEqual(lq.market_of(636), lq.market_of("000636"))


class TimestampTests(unittest.TestCase):
    """The whole point of the module."""

    def quote(self, **kw):
        q = {"code": "600403", "price": 7.18, "last_close": 7.50, "open": 7.90,
             "change_pct": -4.27, "fetched_at": "2026-09-02 12:27:39 CST",
             "fetched_ts": time.time()}
        q.update(kw)
        return q

    def test_a_fresh_quote_reads_as_fresh(self):
        self.assertLess(lq.age_seconds(self.quote()), 2.0)

    def test_an_old_quote_reports_its_age(self):
        q = self.quote(fetched_ts=time.time() - 900)
        self.assertGreater(lq.age_seconds(q), 800)

    def test_age_is_never_negative(self):
        q = self.quote(fetched_ts=time.time() + 60)
        self.assertEqual(lq.age_seconds(q), 0.0)

    def test_a_quote_without_a_stamp_has_no_age(self):
        self.assertIsNone(lq.age_seconds({"price": 7.18}))
        self.assertIsNone(lq.age_seconds(None))

    def test_the_formatter_refuses_a_price_with_no_timestamp(self):
        """This is the guard. A price that prints without its age will be read
        as live by someone, and that someone was me."""
        out = lq.format_quote({"code": "600403", "price": 7.18})
        self.assertNotIn("7.18", out)
        self.assertIn("no timestamp", out)

    def test_the_formatter_shows_the_age(self):
        out = lq.format_quote(self.quote(), "大有能源")
        self.assertIn("7.18", out)
        self.assertIn("ago", out)
        self.assertIn("CST", out)

    def test_the_stamp_is_market_time_not_droplet_time(self):
        """The droplet runs UTC. An ad-hoc call there stamped 04:26 for a 12:26
        market - the same fault in smaller print."""
        src = Path(lq.__file__).read_text(encoding="utf-8")
        self.assertIn("CHINA = timezone(timedelta(hours=8))", src)
        self.assertIn("datetime.now(CHINA)", src)
        self.assertNotIn("datetime.now().strftime", src)


class DegradationTests(unittest.TestCase):
    def test_no_codes_asks_nothing(self):
        self.assertEqual(lq.get_quotes([]), {})
        self.assertEqual(lq.get_quotes(None), {})

    def test_only_junk_codes_asks_nothing(self):
        """Must not open a socket to ask about nonsense."""
        self.assertEqual(lq.get_quotes(["abc", "", "999"]), {})


class SafetyTests(unittest.TestCase):
    def test_the_module_cannot_trade(self):
        src = Path(lq.__file__).read_text(encoding="utf-8")
        for bad in ("buy_stock", "sell_stock", "execute_trade_action",
                    "mockTrading/trade", "mockTrading/cancel", "requests.post",
                    "TdxTradeApi"):
            self.assertNotIn(bad, src)

    def test_it_only_reads_quotes(self):
        src = Path(lq.__file__).read_text(encoding="utf-8")
        self.assertIn("get_security_quotes", src)
        self.assertNotIn("send_order", src)


class LiveTests(unittest.TestCase):
    """Against the real market data servers, if reachable."""

    def test_a_real_quote_comes_back_stamped(self):
        q = lq.get_quotes(["600403"])
        if not q:
            self.skipTest("no TDX host reachable from here")
        got = q["600403"]
        self.assertGreater(got["last_close"], 0)
        self.assertIn("CST", got["fetched_at"])
        self.assertLess(lq.age_seconds(got), 30)

    def test_a_halted_name_reports_zero_not_a_stale_price(self):
        """688432 有研硅 has been suspended since 2026-08-31. A halted name must
        come back as no-trade rather than as its last price dressed as live."""
        q = lq.get_quotes(["688432"])
        if not q:
            self.skipTest("no TDX host reachable from here")
        self.assertEqual(q["688432"]["price"], 0.0)


if __name__ == "__main__":
    unittest.main()
