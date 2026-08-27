#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The fetch side of the disclosure calendar.

The logic lives in disclosure_calendar.py and is tested separately. What can go
wrong HERE is different, and quieter:

  a session miscounted across a holiday puts the entry in the wrong window, and
  which window you are in IS the edge (+0.91% at three sessions, -0.21% at ten)

  a sector rank taken from only the names reporting that day would call almost
  everyone a leader, because the band is defined against the WHOLE sector

  a screener call that fails returns no rows, and an empty candidate file is
  indistinguishable from a season with nothing in it
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import disclosure_calendar as dc  # noqa: E402
import disclosure_calendar_runner as r  # noqa: E402


class TradingDateTests(unittest.TestCase):
    def test_weekends_are_not_sessions(self):
        # 2026-08-29 is a Saturday, 2026-08-30 a Sunday.
        got = r.trading_dates(20260828, 20260831)
        self.assertEqual(got, [20260828, 20260831])

    def test_national_day_is_closed(self):
        """1-7 October 2026 sits squarely inside Q3 reporting season."""
        got = r.trading_dates(20260930, 20261008)
        self.assertEqual(got, [20260930, 20261008])

    def test_mid_autumn_is_closed(self):
        """25-27 September 2026, immediately before the National Day break."""
        self.assertNotIn(20260925, r.trading_dates(20260921, 20260930))

    def test_qingming_is_closed(self):
        """April is annual-report season; a missing 清明 would misfire there."""
        got = r.trading_dates(20260403, 20260407)
        self.assertEqual(got, [20260403, 20260407])

    def test_sessions_across_national_day_are_counted_not_days(self):
        """30 Sep to 8 Oct is EIGHT calendar days and ONE session."""
        cal = r.trading_dates(20260901, 20261031)
        self.assertEqual(dc.sessions_until(20261008, 20260930, cal), 1)

    def test_a_normal_week_counts_normally(self):
        # 31 Aug 2026 is a Monday: Mon, Tue, Wed, Thu is three sessions on.
        cal = r.trading_dates(20260801, 20260930)
        self.assertEqual(dc.sessions_until(20260903, 20260831, cal), 3)
        self.assertEqual(dc.sessions_until(20260901, 20260831, cal), 1)


class CoverageTests(unittest.TestCase):
    """Guessing a weekday calendar is worse than declining to score."""

    def test_a_covered_year_is_accepted(self):
        self.assertTrue(r.coverage_ok(20260827, 20260910))

    def test_an_uncovered_year_is_refused(self):
        self.assertFalse(r.coverage_ok(20270102, 20270115))

    def test_a_window_straddling_coverage_is_refused(self):
        self.assertFalse(r.coverage_ok(20261228, 20270106))

    def test_main_declines_rather_than_guessing(self):
        rc = r.main(["--today", "20270102", "--skill-dir", "/nonexistent"])
        self.assertEqual(rc, 2)


class SectorColumnTests(unittest.TestCase):
    def test_the_column_the_screener_actually_returns(self):
        self.assertEqual(r.sector_of({"东财行业分类二级": "半导体"}), "半导体")

    def test_falls_back_to_shenwan(self):
        self.assertEqual(r.sector_of({"申万行业分类": "电子"}), "电子")

    def test_a_blank_sector_is_none_not_empty_string(self):
        """An empty label would collide every unclassified name into one bucket."""
        self.assertIsNone(r.sector_of({"东财行业分类二级": "  "}))
        self.assertIsNone(r.sector_of({"代码": "600000"}))


class CandidateBuildTests(unittest.TestCase):
    ENTRIES = [
        {"code": "688825", "name": "长鑫科技", "scheduled": 20260831,
         "turnover": 1.88e10, "sector": "半导体"},
        {"code": "600183", "name": "生益科技", "scheduled": 20260831,
         "turnover": 1.43e10, "sector": "元件"},
        {"code": "000001", "name": "小盘股", "scheduled": 20260831,
         "turnover": 5.0e8, "sector": "半导体"},
    ]
    RANKS = {"半导体": {"688825": 1, "000001": 88},
             "元件": {"600183": 4}}

    def build(self, today=20260828):
        return r.build_candidates(today, self.ENTRIES, self.RANKS, 12)

    def test_ranks_are_attached_from_the_whole_sector(self):
        got = {c["code"]: c["sector_rank"] for c in self.build()}
        self.assertEqual(got["688825"], 1)
        self.assertEqual(got["000001"], 88)

    def test_the_sector_leader_is_selected(self):
        sel = {c["code"] for c in self.build() if c["selected"]}
        self.assertIn("688825", sel)

    def test_the_tail_name_is_rejected_with_its_rank(self):
        tail = [c for c in self.build() if c["code"] == "000001"][0]
        self.assertFalse(tail["selected"])
        self.assertIn("88", tail["reason"])

    def test_everything_scored_is_returned_not_just_the_winners(self):
        """A silent empty result reads exactly like a broken one."""
        self.assertEqual(len(self.build()), 3)
        for c in self.build():
            self.assertTrue(c["reason"])

    def test_an_unranked_sector_still_scores(self):
        entries = [dict(self.ENTRIES[0], sector="未知行业")]
        out = r.build_candidates(20260828, entries, {}, 12)
        self.assertIsNone(out[0]["sector_rank"])
        self.assertTrue(out[0]["reason"])


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        # Restore the real one: leaving a stub on the module leaks into every
        # later test, and the retry tests silently passed against it.
        self.addCleanup(setattr, r, "run_screener", r.run_screener)

    def fake(self, rows_by_query):
        def _run(query, skill_dir, python_exe, timeout=0):
            self.calls.append(query)
            for frag, rows in rows_by_query.items():
                if frag in query:
                    return rows
            return []
        return _run

    def test_calendar_carries_the_sector_through(self):
        rows = [{"代码": "688825", "名称": "长鑫科技",
                 "定期报告预计披露日期 2026.06.30": "2026-08-31",
                 "东财行业分类二级": "半导体",
                 "成交额(元) 2026.08.27": "188.39亿"}]
        r.run_screener = self.fake({"预计披露日期": rows})
        out = r.fetch_calendar(20260828, 20260910, "/x", "python")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["sector"], "半导体")
        self.assertEqual(out[0]["scheduled"], 20260831)
        self.assertAlmostEqual(out[0]["turnover"], 1.8839e10, places=2)

    def test_a_dropped_row_does_not_shift_every_sector_after_it(self):
        """parse_calendar_rows drops unusable rows. Pairing its output against
        the raw rows by POSITION files later names under the wrong industry."""
        rows = [
            {"代码": "notacode", "名称": "junk",
             "定期报告预计披露日期 2026.06.30": "2026-08-31",
             "东财行业分类二级": "房地产开发"},
            {"代码": "688825", "名称": "长鑫科技",
             "定期报告预计披露日期 2026.06.30": "2026-08-31",
             "东财行业分类二级": "半导体"},
        ]
        r.run_screener = self.fake({"预计披露日期": rows})
        out = r.fetch_calendar(20260828, 20260910, "/x", "python")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["code"], "688825")
        self.assertEqual(out[0]["sector"], "半导体")

    def test_the_calendar_query_asks_for_the_scheduled_field(self):
        r.run_screener = self.fake({})
        r.fetch_calendar(20260828, 20260910, "/x", "python")
        self.assertIn("预计披露日期", self.calls[0])
        self.assertNotIn("实际", self.calls[0])

    def test_sector_ranking_is_row_position(self):
        rows = [{"代码": "688825"}, {"代码": "603986"}, {"代码": "688008"}]
        r.run_screener = self.fake({"按成交额": rows})
        got, ok = r.fetch_sector_ranking("半导体", "/x", "python")
        self.assertEqual(got, {"688825": 1, "603986": 2, "688008": 3})
        self.assertTrue(ok)

    def test_sector_query_orders_by_turnover_descending(self):
        r.run_screener = self.fake({})
        r.fetch_sector_ranking("半导体", "/x", "python")
        self.assertIn("成交额从大到小", self.calls[0])

    def test_an_empty_sector_reply_is_reported_as_a_failure(self):
        """This is only called for a sector that appeared on the calendar, so it
        has a member by construction: no rows means the query failed, and
        reporting it as an empty ranking would quietly demote every name in it.
        The first live run lost 房地产开发, 电力 and 化学制药 exactly this way."""
        r.run_screener = self.fake({})
        ranks, ok = r.fetch_sector_ranking("半导体", "/x", "python")
        self.assertEqual(ranks, {})
        self.assertFalse(ok)

    def test_duplicate_codes_keep_the_best_rank(self):
        rows = [{"代码": "688825"}, {"代码": "688825"}]
        r.run_screener = self.fake({"按成交额": rows})
        self.assertEqual(r.fetch_sector_ranking("x", "/x", "python")[0]["688825"], 1)


class RetryTests(unittest.TestCase):
    """A throttled call returns no rows and raises nothing."""

    def setUp(self):
        self.addCleanup(setattr, r.time, "sleep", r.time.sleep)
        self.addCleanup(setattr, r, "_screener_once", r._screener_once)
        r.time.sleep = lambda *_: None
        self.calls = []

    def stub(self, results):
        def _once(query, skill_dir, python_exe, timeout):
            self.calls.append(query)
            return results[min(len(self.calls) - 1, len(results) - 1)]
        r._screener_once = _once

    def test_a_transient_empty_reply_is_retried(self):
        self.stub([[], [], [{"代码": "688825"}]])
        rows = r.run_screener("q", "/x", "python")
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(self.calls), 3)

    def test_a_first_time_success_does_not_retry(self):
        self.stub([[{"代码": "688825"}]])
        r.run_screener("q", "/x", "python")
        self.assertEqual(len(self.calls), 1)

    def test_retries_are_bounded(self):
        self.stub([[]])
        self.assertEqual(r.run_screener("q", "/x", "python"), [])
        self.assertEqual(len(self.calls), r.SCREENER_ATTEMPTS)


class SectorSkipTests(unittest.TestCase):
    """One screener call per sector, and a day spans about 83 of them."""

    def test_an_all_illiquid_sector_is_skipped(self):
        entries = [{"code": "000001", "sector": "冷门行业", "turnover": 1e6}]
        self.assertEqual(r.sectors_worth_ranking(entries), set())

    def test_one_liquid_name_keeps_the_sector(self):
        entries = [{"code": "000001", "sector": "半导体", "turnover": 1e6},
                   {"code": "688825", "sector": "半导体", "turnover": 1e10}]
        self.assertEqual(r.sectors_worth_ranking(entries), {"半导体"})

    def test_unknown_turnover_is_not_treated_as_illiquid(self):
        """The calendar query need not carry the column; a missing value must
        not silently empty the book."""
        entries = [{"code": "000001", "sector": "半导体", "turnover": None}]
        self.assertEqual(r.sectors_worth_ranking(entries), {"半导体"})

    def test_entries_without_a_sector_are_ignored(self):
        entries = [{"code": "000001", "sector": None, "turnover": 1e10}]
        self.assertEqual(r.sectors_worth_ranking(entries), set())


class QuotaTests(unittest.TestCase):
    """The account allows 500 screener calls a day and then answers 状态码 113.

    A run costs about 84 calls, so the budget is workable - but only if nothing
    retries into an exhausted quota, and only if the run stops rather than
    silently unranking every remaining sector.
    """

    class _Proc:
        def __init__(self, out):
            self.stdout = out.encode("utf-8")
            self.stderr = b""

    def stub_subprocess(self, output):
        import subprocess as sp
        self.addCleanup(setattr, r.subprocess, "run", r.subprocess.run)
        self.addCleanup(setattr, r.os.path, "exists", r.os.path.exists)
        r.os.path.exists = lambda *_: True
        r.subprocess.run = lambda *a, **k: self._Proc(output)

    def test_the_real_quota_message_is_detected(self):
        self.stub_subprocess(
            "错误: 顶层错误: 状态码 113 - 您为妙想skils进阶版用户，"
            "今日调用次数已达上线500次，可以明日再来，感谢您的认可。")
        with self.assertRaises(r.ScreenerQuotaExhausted):
            r._screener_once("q", "/x", "python", 10)

    def test_quota_is_not_retried(self):
        """Three blind attempts spend three real calls to learn the same thing."""
        self.addCleanup(setattr, r, "_screener_once", r._screener_once)
        calls = []

        def _once(*a, **k):
            calls.append(1)
            raise r.ScreenerQuotaExhausted("spent")

        r._screener_once = _once
        with self.assertRaises(r.ScreenerQuotaExhausted):
            r.run_screener("q", "/x", "python")
        self.assertEqual(len(calls), 1)

    def test_an_ordinary_empty_reply_is_not_mistaken_for_quota(self):
        self.stub_subprocess("📊 行数: 0")
        self.assertEqual(r._screener_once("q", "/x", "python", 10), [])


class SectorCacheTests(unittest.TestCase):
    """Rankings are reused because the measurement used a trailing 20-session
    mean turnover: one day snapshot is noisier than the validated construct."""

    def setUp(self):
        import tempfile, os
        self.path = os.path.join(tempfile.mkdtemp(), "ranks.json")

    def test_a_fresh_entry_is_reused(self):
        r.save_sector_cache(self.path,
                            {"半导体": {"ranks": {"688825": 1}, "as_of": 20260826}})
        got = r.load_sector_cache(self.path, 20260827)
        self.assertEqual(got["半导体"]["ranks"]["688825"], 1)

    def test_a_stale_entry_is_dropped(self):
        r.save_sector_cache(self.path,
                            {"半导体": {"ranks": {"688825": 1}, "as_of": 20260101}})
        self.assertEqual(r.load_sector_cache(self.path, 20260827), {})

    def test_a_missing_file_is_not_an_error(self):
        self.assertEqual(r.load_sector_cache("/nonexistent/x.json", 20260827), {})

    def test_a_corrupt_file_is_not_an_error(self):
        Path(self.path).write_text("not json", encoding="utf-8")
        self.assertEqual(r.load_sector_cache(self.path, 20260827), {})

    def test_an_empty_ranking_is_never_cached_as_valid(self):
        """A throttled call produced {}; caching it would freeze the failure in."""
        r.save_sector_cache(self.path, {"电力": {"ranks": {}, "as_of": 20260827}})
        self.assertEqual(r.load_sector_cache(self.path, 20260827), {})


class SafetyTests(unittest.TestCase):
    def test_runner_holds_no_execution_path(self):
        src = Path(r.__file__).read_text(encoding="utf-8")
        for forbidden in ("buy_stock", "sell_stock", "execute_trade_action",
                          "place_order", "v10_moni_trader"):
            self.assertNotIn(forbidden, src, forbidden)

    def test_the_logic_module_stays_pure(self):
        """The runner does the I/O so disclosure_calendar.py does not have to."""
        src = Path(dc.__file__).read_text(encoding="utf-8")
        for forbidden in ("subprocess", "requests", "urllib", "socket", "open("):
            self.assertNotIn(forbidden, src, forbidden)


if __name__ == "__main__":
    unittest.main()
