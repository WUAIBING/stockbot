#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The candidate pool froze on 2026-07-17. The cause was bad data, not bad gates.

Chain: the template search passed 0 of 873 trials -> the registry's templates
list was empty -> load_registry() raised -> the pool never rebuilt -> every buy
round skipped. The fallback that exists precisely for this could not fire: it
read two evolution artifacts that have never been written on the host. Every skip
printed a routine-looking [WARN] and returned EXIT_NO_ACTION, so nothing
escalated for 37 days.

A profit-only promotion path was added on 2026-08-26 to explain the "0 of 873",
on the reasoning that the hit-rate gates must be miscalibrated against
profitability. That reasoning was wrong, and these tests now pin why.

13 of the 25 dates in that window carried prices fabricated by a pytdx socket
desync; 36.8% of all ranking rows were bad (58,619 of 159,415). Rebuilt from TDX
vipdoc, the SAME search on the SAME gates passes 4 templates:

    metric            corrupt   clean    gate
    top100_hit_rate     0.045   0.152    0.15 (pass)
    top30_hit_rate      0.033   0.098    0.05
    hit_day_rate        0.483   0.960    0.70
    passed_templates        0       5

The thresholds were never the problem. The template the profit path promoted,
gapmix_head5_skip35_end40, still fails these gates on clean data (top100 0.080,
hit_day 0.812), so that path promoted something the corrected evidence rejects.

What survives from that change and is still needed: the fallback repointed at an
artifact that actually exists, and the staleness alarm.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "workbuddy_distill" / "scripts"))

import distill_local_templates as dlt  # noqa: E402

# the best trial as measured on CLEAN data: gapmix_head10_skip20_end35
CLEAN_BEST = dict(
    top100_hit_rate=0.152,
    top30_hit_rate=0.098,
    hit_day_rate=0.960,
    candidate_win_rate=0.560,
    candidate_avg_return=1.400,
    portfolio_positive_day_rate=0.740,
    profit_priority_score=95.9,
)

# the same window measured on CORRUPT data, which produced "0 of 873"
CORRUPT_BEST = dict(
    top100_hit_rate=0.0451,
    top30_hit_rate=0.0332,
    hit_day_rate=0.4830,
    candidate_win_rate=0.5834,
    candidate_avg_return=1.6819,
    portfolio_positive_day_rate=0.7784,
    profit_priority_score=108.9016,
)


def verdict(metrics, **over):
    kw = dict(metrics)
    kw.update(over)
    return dlt.classify_verdict(
        kw.pop("top100_hit_rate"),
        kw.pop("top30_hit_rate"),
        kw.pop("hit_day_rate"),
        **kw,
    )


class CleanDataPassesTheOriginalGates(unittest.TestCase):
    def test_the_best_clean_template_passes(self):
        """4 templates reached 'pass' on clean data with no gate change."""
        self.assertEqual(verdict(CLEAN_BEST), ("pass", "promote"))

    def test_the_same_window_failed_on_corrupt_data(self):
        """Why the outage looked like a threshold problem."""
        self.assertEqual(verdict(CORRUPT_BEST)[0], "fail")

    def test_corrupt_data_failed_on_ranking_not_profit(self):
        """Its profit score was the HIGHEST in the table - hence the wrong turn."""
        self.assertGreaterEqual(CORRUPT_BEST["profit_priority_score"], 108)
        self.assertLess(CORRUPT_BEST["top100_hit_rate"],
                        dlt.THRESHOLDS["prototype"]["top100_hit_rate_min"])
        self.assertLess(CORRUPT_BEST["hit_day_rate"],
                        dlt.THRESHOLDS["prototype"]["hit_day_rate_min"])


class ProfitOnlyPromotionIsGone(unittest.TestCase):
    def test_no_verdict_can_be_profit_pass(self):
        """A high profit score alone must not promote anything."""
        self.assertEqual(verdict(CORRUPT_BEST)[0], "fail")
        self.assertEqual(
            verdict(CORRUPT_BEST, profit_priority_score=200.0)[0], "fail")

    def test_tier_rank_has_no_profit_pass(self):
        self.assertEqual(dlt.tier_rank("profit_pass"), 0)
        self.assertEqual(dlt.tier_rank("priority"), 4)
        self.assertEqual(dlt.tier_rank("pass"), 3)
        self.assertEqual(dlt.tier_rank("prototype"), 2)
        self.assertEqual(dlt.tier_rank("fail"), 1)

    def test_the_promoted_template_still_fails_on_clean_data(self):
        """gapmix_head5_skip35_end40, measured clean: top100 0.080, hit_day 0.812."""
        self.assertEqual(
            verdict(CLEAN_BEST, top100_hit_rate=0.080, top30_hit_rate=0.049,
                    hit_day_rate=0.812, profit_priority_score=108.0)[0],
            "fail")

    def test_the_constant_is_removed(self):
        self.assertFalse(hasattr(dlt, "PROFIT_PASS_MIN_EVALUATION_DAYS"))

    def test_conventional_verdicts_still_work(self):
        self.assertEqual(
            verdict(CLEAN_BEST, top100_hit_rate=0.20, top30_hit_rate=0.07,
                    hit_day_rate=0.75, candidate_win_rate=0.60,
                    candidate_avg_return=2.5, portfolio_positive_day_rate=0.60)[0],
            "priority")
        self.assertEqual(
            verdict(CLEAN_BEST, top100_hit_rate=0.11, top30_hit_rate=0.04,
                    hit_day_rate=0.62, candidate_win_rate=0.48,
                    candidate_avg_return=1.2, profit_priority_score=80.0)[0],
            "prototype")


class SearchFallbackTests(unittest.TestCase):
    """Repointed at an artifact that exists. This half of the change stands."""

    def setUp(self):
        import build_workbuddy_distill_pool as b

        self.b = b
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "template_search_latest.json"
        original = b.TEMPLATE_SEARCH_FILE
        b.TEMPLATE_SEARCH_FILE = self.path
        self.addCleanup(lambda: setattr(b, "TEMPLATE_SEARCH_FILE", original))

    def write(self, payload):
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def entry(name, verdict_value):
        return {
            "template_name": name,
            "base_template_name": name,
            "params": {"cutoff": 20},
            "metrics": {"business_score": 76.4},
            "verdict": verdict_value,
        }

    def test_a_passing_template_is_recovered(self):
        self.write(
            {
                "passed_templates": [self.entry("gapmix_head10_skip20_end35", "pass")],
                "top_templates": [],
                "window_profile": {"mode": "core_plus_buffer"},
            }
        )
        reg = self.b._fallback_registry_from_search()
        self.assertIsNotNone(reg)
        self.assertEqual(reg["champion_template_name"], "gapmix_head10_skip20_end35")
        self.assertEqual(reg["window"], {"mode": "core_plus_buffer"})

    def test_profit_pass_is_no_longer_eligible(self):
        """That verdict cannot be produced; it must not be honoured either."""
        self.write({"passed_templates": [self.entry("stale", "profit_pass")],
                    "top_templates": []})
        self.assertIsNone(self.b._fallback_registry_from_search())

    def test_a_search_that_rejected_everything_still_fails(self):
        self.write(
            {
                "passed_templates": [],
                "prototype_templates": [],
                "top_templates": [self.entry("junk", "fail")],
            }
        )
        self.assertIsNone(self.b._fallback_registry_from_search())

    def test_missing_artifact_returns_none(self):
        self.assertIsNone(self.b._fallback_registry_from_search())

    def test_prototype_is_eligible(self):
        self.write(
            {
                "passed_templates": [],
                "prototype_templates": [self.entry("proto", "prototype")],
                "top_templates": [],
            }
        )
        reg = self.b._fallback_registry_from_search()
        self.assertIsNotNone(reg)
        self.assertEqual(reg["champion_template_name"], "proto")

    def test_entries_without_params_or_metrics_are_skipped(self):
        bad = {"template_name": "bad", "verdict": "pass"}
        self.write({"passed_templates": [bad], "top_templates": []})
        self.assertIsNone(self.b._fallback_registry_from_search())

    def test_duplicates_are_collapsed_and_capped(self):
        pool = [
            self.entry("a", "pass"),
            self.entry("a", "pass"),
            self.entry("b", "pass"),
            self.entry("c", "pass"),
            self.entry("d", "pass"),
        ]
        self.write({"passed_templates": pool, "top_templates": []})
        reg = self.b._fallback_registry_from_search()
        names = [e["template_name"] for e in reg["templates"]]
        self.assertEqual(names, ["a", "b", "c"])


class StalenessTests(unittest.TestCase):
    """The alarm. This half of the change stands too."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "workbuddy" / "skills" / "a-share-analyst"))
        import workbuddy_local_challenger as c

        self.c = c

    def test_the_freeze_reads_as_an_outage(self):
        text = self.c._describe_pool_staleness("2026-07-17", "2026-08-25")
        self.assertIn("已过期", text)
        self.assertIn("39", text)

    def test_one_day_behind_is_routine(self):
        text = self.c._describe_pool_staleness("2026-08-24", "2026-08-25")
        self.assertIn("落后", text)
        self.assertNotIn("已过期", text)

    def test_alarm_threshold(self):
        self.assertNotIn(
            "已过期", self.c._describe_pool_staleness("2026-08-23", "2026-08-25")
        )
        self.assertIn(
            "已过期", self.c._describe_pool_staleness("2026-08-22", "2026-08-25")
        )

    def test_unparseable_dates_do_not_crash(self):
        self.assertIn("无法解析", self.c._describe_pool_staleness("", "2026-08-25"))
        self.assertIn("无法解析", self.c._describe_pool_staleness("nonsense", "2026-08-25"))
        self.assertIsNone(self.c._pool_staleness_days("nope", "2026-08-25"))

    def test_a_pool_ahead_of_expectation_is_zero_not_negative(self):
        self.assertEqual(self.c._pool_staleness_days("2026-08-26", "2026-08-25"), 0)


if __name__ == "__main__":
    unittest.main()
