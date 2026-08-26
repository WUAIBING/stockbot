#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The candidate pool froze on 2026-07-17 and stayed frozen.

Chain: the template search passed 0 of 873 trials -> the registry's templates
list was empty -> load_registry() raised -> the pool never rebuilt -> every buy
round skipped. The fallback that exists precisely for this could not fire: it
read two evolution artifacts that have never been written on the host.

Every skip printed a routine-looking [WARN] and returned EXIT_NO_ACTION, which
the schedule treats as success, so nothing ever escalated.

BEST below holds the real metrics of the best of those 873 trials. It clears the
highest profit bar in the table (profit_priority_score 108.9 >= 108) at a 58.3%
win rate and +1.68% one-day forward return, and was discarded for not matching a
top-100 ranking often enough.
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

# the actual best trial: gapmix_head5_skip35_end4
BEST = dict(
    top100_hit_rate=0.0451,
    top30_hit_rate=0.0332,
    hit_day_rate=0.4830,
    candidate_win_rate=0.5834,
    candidate_avg_return=1.6819,
    portfolio_positive_day_rate=0.7784,
    profit_priority_score=108.9016,
    evaluation_days=24,
)


def verdict(**over):
    kw = dict(BEST)
    kw.update(over)
    return dlt.classify_verdict(
        kw.pop("top100_hit_rate"),
        kw.pop("top30_hit_rate"),
        kw.pop("hit_day_rate"),
        **kw,
    )


class ProfitPassTests(unittest.TestCase):
    def test_the_blocked_template_is_now_promoted(self):
        self.assertEqual(verdict(), ("profit_pass", "promote"))

    def test_it_still_fails_every_ranking_gate(self):
        """Promotion is on profit; the ranking gates genuinely are not met."""
        self.assertLess(
            BEST["top100_hit_rate"],
            dlt.THRESHOLDS["prototype"]["top100_hit_rate_min"],
        )
        self.assertLess(
            BEST["hit_day_rate"],
            dlt.THRESHOLDS["prototype"]["hit_day_rate_min"],
        )

    def test_a_small_sample_cannot_promote(self):
        """A lucky short window must not restart trading."""
        self.assertEqual(verdict(evaluation_days=5)[0], "fail")
        self.assertEqual(
            verdict(evaluation_days=dlt.PROFIT_PASS_MIN_EVALUATION_DAYS - 1)[0], "fail"
        )
        self.assertEqual(
            verdict(evaluation_days=dlt.PROFIT_PASS_MIN_EVALUATION_DAYS)[0],
            "profit_pass",
        )

    def test_ranking_blind_templates_cannot_promote(self):
        """Profit-first is not ranking-blind: the prototype top30 bar applies."""
        self.assertEqual(verdict(top30_hit_rate=0.0)[0], "fail")
        self.assertEqual(verdict(top30_hit_rate=0.02)[0], "fail")

    def test_merely_good_profit_is_not_enough(self):
        """The bar is the PRIORITY profit gate, not a lowered one.

        Second place in the real search scored 93.3 - it must not promote.
        """
        self.assertEqual(
            verdict(
                profit_priority_score=93.3,
                candidate_win_rate=0.557,
                candidate_avg_return=0.850,
                portfolio_positive_day_rate=0.739,
            )[0],
            "fail",
        )

    def test_conventional_verdicts_are_unchanged(self):
        """A template meeting the ranking gates classifies exactly as before."""
        self.assertEqual(
            verdict(top100_hit_rate=0.20, top30_hit_rate=0.07, hit_day_rate=0.75)[0],
            "priority",
        )
        self.assertEqual(
            verdict(top100_hit_rate=0.16, top30_hit_rate=0.055, hit_day_rate=0.72)[0],
            "pass",
        )

    def test_weak_profit_and_weak_ranking_still_fails(self):
        self.assertEqual(
            verdict(
                candidate_win_rate=0.30,
                candidate_avg_return=-1.0,
                portfolio_positive_day_rate=0.20,
                profit_priority_score=40.0,
            )[0],
            "fail",
        )

    def test_tier_ordering(self):
        r = dlt.tier_rank
        self.assertGreater(r("priority"), r("pass"))
        self.assertGreater(r("pass"), r("profit_pass"))
        self.assertGreater(r("profit_pass"), r("prototype"))
        self.assertGreater(r("prototype"), r("fail"))


class SearchFallbackTests(unittest.TestCase):
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

    def test_promoted_template_is_recovered(self):
        self.write(
            {
                "passed_templates": [self.entry("gapmix_head5_skip35_end4", "profit_pass")],
                "top_templates": [],
                "window_profile": {"mode": "core_plus_buffer"},
            }
        )
        reg = self.b._fallback_registry_from_search()
        self.assertIsNotNone(reg)
        self.assertEqual(reg["champion_template_name"], "gapmix_head5_skip35_end4")
        self.assertEqual(len(reg["templates"]), 1)
        self.assertEqual(reg["window"], {"mode": "core_plus_buffer"})

    def test_a_search_that_rejected_everything_still_fails(self):
        """The fallback must not trade on a template the search called fail."""
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
        bad = {"template_name": "bad", "verdict": "profit_pass"}
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
