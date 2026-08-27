#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Selection by where capital concentrates, not by price pattern.

Every threshold in whale_watch.py exists because a screen without it caught the
wrong thing. These tests pin those failures with the real data that produced
them, so the rules cannot quietly be relaxed back:

  absolute yuan          returned 建设银行, 中国银行, 中信证券 - index flow
  small-cap filter       returned ST汇洲 - one block trade in a thin tape
  one-day snapshot       returned 佰仁医疗 on a 4,029万 spike after nine
                         sessions of noise around zero

And the case it must accept: 2026-08-27, where the top thirteen names by
turnover were all AI hardware - 中际旭创 227.8亿, 亨通光电 191.2亿, 长鑫科技
188.4亿 - one theme occupying the entire deep end of the pool.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import whale_watch as w  # noqa: E402


def hist(flows, changes=None):
    """Newest-first sessions."""
    changes = changes or [0.0] * len(flows)
    return [{"whale_flow": f, "change_pct": c} for f, c in zip(flows, changes)]


class NumberParsingTests(unittest.TestCase):
    """The API returns human units; misreading them silently corrupts every rule."""

    def test_chinese_units(self):
        self.assertAlmostEqual(w.parse_cn_number("25.04亿"), 2.504e9)
        self.assertAlmostEqual(w.parse_cn_number("4029万"), 4.029e7)
        self.assertAlmostEqual(w.parse_cn_number("1.27万亿"), 1.27e12)

    def test_negative_and_plain(self):
        self.assertAlmostEqual(w.parse_cn_number("-9658万"), -9.658e7)
        self.assertAlmostEqual(w.parse_cn_number("1234.5"), 1234.5)

    def test_missing_values(self):
        for bad in (None, "", "-", "--", "None", "abc"):
            self.assertIsNone(w.parse_cn_number(bad), repr(bad))

    def test_percentages(self):
        self.assertAlmostEqual(w.parse_pct("8.43%"), 8.43)
        self.assertAlmostEqual(w.parse_pct("-0.94"), -0.94)
        self.assertIsNone(w.parse_pct("-"))

    def test_wanyi_is_not_read_as_wan(self):
        """1.27万亿 must not become 1.27万 - a 100,000,000x error."""
        self.assertGreater(w.parse_cn_number("1.27万亿"),
                           w.parse_cn_number("9999亿"))


class ScreenParsingTests(unittest.TestCase):
    def test_real_row_from_the_screener(self):
        row = {
            "代码": "600183", "名称": "生益科技",
            "最新价(元) 2026.08.27": "140.97",
            "涨跌幅(%) 2026.08.27": "8.43",
            "超大单净额(元) 2026.08.27": "25.04亿",
            "成交额(元) 2026.08.27": "143.14亿",
        }
        out = w.parse_screen_rows([row])
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c["code"], "600183")
        # relative tolerance: 143.14 x 1e8 lands 2e-6 short in binary float
        self.assertAlmostEqual(c["turnover"] / 1.4314e10, 1.0, places=9)
        self.assertAlmostEqual(c["flow_ratio"], 2.504e9 / 1.4314e10, places=6)

    def test_column_names_carry_dates(self):
        """Matching must be by fragment; the date changes daily."""
        row = {"代码": "000001", "名称": "X",
               "超大单净额(元) 2099.12.31": "1亿",
               "成交额(元) 2099.12.31": "50亿", "涨跌幅(%) 2099.12.31": "1"}
        self.assertEqual(len(w.parse_screen_rows([row])), 1)

    def test_unusable_rows_are_dropped_not_defaulted(self):
        rows = [{"代码": "600000", "成交额(元) x": "-", "超大单净额(元) x": "1亿"},
                {"代码": "notacode", "成交额(元) x": "50亿", "超大单净额(元) x": "1亿"}]
        self.assertEqual(w.parse_screen_rows(rows), [])


class SustainTests(unittest.TestCase):
    """Whale versus splash - the distinction a one-day screen cannot make."""

    def test_the_bairen_case_is_rejected(self):
        """佰仁医疗: nine sessions of noise, then one 4,029万 spike."""
        h = hist([4029e4, -17e4, -161e4, -97e4, 160e4,
                  -598e4, 364e4, 275e4, 69e4, 12e4])
        acc = w.sustained_accumulation(h)
        self.assertFalse(acc["sustained"])
        self.assertIn("block trade", acc["reason"])

    def test_steady_accumulation_is_accepted(self):
        h = hist([3e7, 2.5e7, 2.8e7, -1e7, 3.2e7, 2.1e7, 2.9e7, -5e6, 3.1e7, 2.7e7])
        acc = w.sustained_accumulation(h)
        self.assertTrue(acc["sustained"])
        self.assertGreaterEqual(acc["positive_days"], w.SUSTAIN_MIN_DAYS)

    def test_mostly_outflow_is_rejected(self):
        h = hist([1e7, -2e7, -3e7, -1e7, -2e7, -4e7, 1e7, -1e7, -2e7, -3e7])
        self.assertFalse(w.sustained_accumulation(h)["sustained"])

    def test_short_history_declines_rather_than_assumes(self):
        acc = w.sustained_accumulation(hist([1e7, 2e7, 1e7]))
        self.assertFalse(acc["sustained"])
        self.assertIn("need", acc["reason"])

    def test_one_day_dominating_is_a_block_trade(self):
        """Even with enough positive days, one session owning the flow is a splash."""
        h = hist([1e9, 1e5, 1e5, 1e5, 1e5, 1e5, 1e5, 1e5, 1e5, 1e5])
        acc = w.sustained_accumulation(h)
        self.assertEqual(acc["positive_days"], 10)
        self.assertFalse(acc["sustained"])


class EvaluateTests(unittest.TestCase):
    STEADY = hist([3e7, 2.5e7, 2.8e7, -1e7, 3.2e7, 2.1e7, 2.9e7, -5e6, 3.1e7, 2.7e7],
                  [0.4, 0.2, -0.3, 0.5, 0.1, -0.2, 0.6, 0.0, 0.3, -0.1])

    def cand(self, **kw):
        base = dict(code="600183", name="X", turnover=1.4e10,
                    whale_flow=2.5e9, flow_ratio=0.18, change_pct=1.0)
        base.update(kw)
        return base

    def test_a_good_candidate_is_selected(self):
        r = w.evaluate(self.cand(), self.STEADY)
        self.assertTrue(r["selected"])

    def test_shallow_water_is_rejected(self):
        r = w.evaluate(self.cand(turnover=1e8), self.STEADY)
        self.assertFalse(r["selected"])
        self.assertIn("deep-water", r["reason"])

    def test_index_flow_is_rejected_by_the_ratio(self):
        """建设银行: huge absolute flow, trivial relative to its turnover."""
        r = w.evaluate(self.cand(turnover=5e10, whale_flow=1.07e8, flow_ratio=0.002),
                       self.STEADY)
        self.assertFalse(r["selected"])
        self.assertIn("flow ratio", r["reason"])

    def test_a_completed_move_is_rejected(self):
        """+8%/day for ten sessions is the wake."""
        run = hist([3e7]*10, [8.0]*10)
        r = w.evaluate(self.cand(), run)
        self.assertFalse(r["selected"])
        self.assertIn("wake", r["reason"])

    def test_every_outcome_carries_a_reason(self):
        for cand, h in ((self.cand(), self.STEADY),
                        (self.cand(turnover=1e8), self.STEADY),
                        (self.cand(), hist([1e9] + [1e5]*9))):
            self.assertTrue(w.evaluate(cand, h)["reason"])

    def test_ranking_puts_the_strongest_ratio_first(self):
        a = w.evaluate(self.cand(code="000001", flow_ratio=0.06), self.STEADY)
        b = w.evaluate(self.cand(code="000002", flow_ratio=0.20), self.STEADY)
        self.assertEqual([c["code"] for c in w.rank([a, b])], ["000002", "000001"])

    def test_rejected_candidates_never_rank(self):
        bad = w.evaluate(self.cand(turnover=1e8), self.STEADY)
        self.assertEqual(w.rank([bad]), [])


class ThemeTests(unittest.TestCase):
    def test_one_theme_dominating_is_visible(self):
        """2026-08-27: the deep end was entirely AI hardware."""
        cands = [
            {"code": "300308", "turnover": 2.278e10},
            {"code": "600487", "turnover": 1.912e10},
            {"code": "688825", "turnover": 1.884e10},
            {"code": "601939", "turnover": 5.0e9},
        ]
        theme = {"300308": "AI硬件", "600487": "AI硬件", "688825": "AI硬件",
                 "601939": "银行"}
        out = w.theme_concentration(cands, lambda c: theme.get(c))
        self.assertEqual(out[0][0], "AI硬件")
        self.assertGreater(out[0][2], 0.90)

    def test_unlabelled_codes_do_not_crash(self):
        out = w.theme_concentration([{"code": "999999", "turnover": 1e9}],
                                    lambda c: None)
        self.assertEqual(out[0][0], "unknown")


class SafetyTests(unittest.TestCase):
    def test_module_holds_no_execution_path(self):
        src = Path(w.__file__).read_text(encoding="utf-8")
        for forbidden in ("buy_stock", "sell_stock", "execute_trade_action",
                          "requests", "urllib", "socket"):
            self.assertNotIn(forbidden, src, forbidden)


if __name__ == "__main__":
    unittest.main()
