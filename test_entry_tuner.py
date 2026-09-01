#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Daily tuning that converges instead of chasing.

Tuning on a day's trades fits noise; never tuning stays wrong. The tuner
re-estimates from ALL history every day and shrinks each effect toward zero by
its own reliability, so a finding seen once barely moves anything and one that
survives months converges on its full size.

The first real snapshot justified the caution immediately. Measured on price
paths, buys sharing a sector underperformed by -4.90% at T+5 (t=-2.39).
Measured on what the account REALISED, the same crowding shows +2.48pp
(t=+2.19) - the opposite sign, because the exit logic cuts crowded names early
and holds solo ones longer. A concentration cap wired on the price-path result
would have penalised the better-realising group.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import entry_tuner as et  # noqa: E402


def trip(pnl, **kw):
    d = {"code": "300747", "pnl_pct": pnl, "tier": 2, "mode": "pre_breakout",
         "entry_score": 70.0, "sector_same_day_buys": 1}
    d.update(kw)
    return d


class ShrinkTests(unittest.TestCase):
    """Reliability, not significance - the weight never reaches 1."""

    def test_no_evidence_moves_nothing(self):
        self.assertEqual(et.shrink_weight(0.0), 0.0)

    def test_half_weight_at_t_two(self):
        self.assertAlmostEqual(et.shrink_weight(2.0), 0.5, places=6)

    def test_weight_rises_with_t(self):
        ws = [et.shrink_weight(t) for t in (0.5, 1, 2, 3, 4, 8)]
        self.assertEqual(ws, sorted(ws))

    def test_weight_never_reaches_one(self):
        self.assertLess(et.shrink_weight(50.0), 1.0)

    def test_sign_does_not_change_the_weight(self):
        self.assertEqual(et.shrink_weight(-2.0), et.shrink_weight(2.0))


class EffectTests(unittest.TestCase):
    def test_a_group_is_measured_against_the_others_not_against_zero(self):
        """Both groups losing money is not evidence that either is the problem."""
        trips = ([trip(-5.0, sector_same_day_buys=2) for _ in range(10)]
                 + [trip(-5.0, sector_same_day_buys=1) for _ in range(10)])
        eff = et.estimate_effects(trips)["sector_crowding"]
        self.assertAlmostEqual(eff["crowded"]["effect_pp"], 0.0, places=6)
        self.assertAlmostEqual(eff["solo"]["effect_pp"], 0.0, places=6)

    def test_a_real_difference_is_found_with_the_right_sign(self):
        trips = ([trip(4.0, sector_same_day_buys=2) for _ in range(10)]
                 + [trip(-4.0, sector_same_day_buys=1) for _ in range(10)])
        eff = et.estimate_effects(trips)["sector_crowding"]
        self.assertGreater(eff["crowded"]["effect_pp"], 0)
        self.assertLess(eff["solo"]["effect_pp"], 0)

    def test_the_shrunk_effect_is_smaller_than_the_raw_one(self):
        trips = ([trip(4.0 + i * 0.4, sector_same_day_buys=2) for i in range(10)]
                 + [trip(-4.0 + i * 0.4, sector_same_day_buys=1) for i in range(10)])
        e = et.estimate_effects(trips)["sector_crowding"]["crowded"]
        self.assertLess(abs(e["shrunk_pp"]), abs(e["effect_pp"]))

    def test_a_thin_group_is_reported_but_not_eligible(self):
        """Reported so it can be watched; not eligible so it cannot act."""
        trips = ([trip(4.0, sector_same_day_buys=2) for _ in range(3)]
                 + [trip(-4.0, sector_same_day_buys=1) for _ in range(20)])
        e = et.estimate_effects(trips)["sector_crowding"]["crowded"]
        self.assertEqual(e["n"], 3)
        self.assertFalse(e["eligible"])

    def test_trips_without_an_attribute_are_skipped_not_bucketed(self):
        trips = [trip(1.0, sector_same_day_buys=None) for _ in range(5)]
        self.assertNotIn("sector_crowding", et.estimate_effects(trips))

    def test_score_bands_split_where_min_trade_score_steps(self):
        self.assertEqual(et._attr_score_band({"entry_score": 80}), "score76+")
        self.assertEqual(et._attr_score_band({"entry_score": 64}), "score64-69")
        self.assertEqual(et._attr_score_band({"entry_score": 51}), "score<64")


class AdjustmentTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, et, "TUNER_ENABLED", et.TUNER_ENABLED)
        self.effects = {
            "sector_crowding": {
                "crowded": {"shrunk_pp": -3.0, "t": -2.2, "eligible": True},
                "solo": {"shrunk_pp": +1.0, "t": 1.0, "eligible": False},
            }
        }

    def test_the_gate_is_off_by_default(self):
        import os
        self.assertNotIn(os.environ.get("TLFZ_ENTRY_TUNER", "0").lower(),
                         ("1", "true", "yes", "on"))

    def test_nothing_is_applied_while_the_gate_is_off(self):
        et.TUNER_ENABLED = False
        adj, used = et.score_adjustment({"sector_crowding": "crowded"}, self.effects)
        self.assertEqual(adj, 0.0)
        self.assertEqual(used, [])

    def test_an_eligible_effect_applies_with_its_sign(self):
        et.TUNER_ENABLED = True
        adj, used = et.score_adjustment({"sector_crowding": "crowded"}, self.effects)
        self.assertEqual(adj, -3.0)
        self.assertEqual(len(used), 1)

    def test_an_ineligible_effect_is_ignored(self):
        et.TUNER_ENABLED = True
        adj, used = et.score_adjustment({"sector_crowding": "solo"}, self.effects)
        self.assertEqual(adj, 0.0)
        self.assertEqual(used, [])

    def test_the_adjustment_is_capped_in_both_directions(self):
        """min_trade_score steps by 6 between regimes, so the cap keeps the
        tuner from silently rewriting which regime the book thinks it is in."""
        et.TUNER_ENABLED = True
        big = {"a": {"x": {"shrunk_pp": -99.0, "t": -9, "eligible": True}}}
        self.assertEqual(et.score_adjustment({"a": "x"}, big)[0], -et.MAX_ADJUSTMENT)
        big["a"]["x"]["shrunk_pp"] = 99.0
        self.assertEqual(et.score_adjustment({"a": "x"}, big)[0], et.MAX_ADJUSTMENT)

    def test_an_unknown_label_contributes_nothing(self):
        et.TUNER_ENABLED = True
        self.assertEqual(
            et.score_adjustment({"sector_crowding": "nope"}, self.effects)[0], 0.0)


class SnapshotTests(unittest.TestCase):
    def test_a_snapshot_records_the_settings_it_was_made_under(self):
        """An estimate is not interpretable without the knobs that produced it."""
        snap = et.build_snapshot([trip(1.0) for _ in range(5)], as_of="2026-09-01")
        for k in ("as_of", "trips", "shrink_k", "max_adjustment", "tuner_enabled"):
            self.assertIn(k, snap)
        self.assertEqual(snap["as_of"], "2026-09-01")

    def test_snapshots_append_so_convergence_is_visible(self):
        p = Path(tempfile.mkdtemp()) / "hist.jsonl"
        for day in ("2026-08-30", "2026-08-31", "2026-09-01"):
            et.append_snapshot(et.build_snapshot([trip(1.0)] * 5, as_of=day), str(p))
        lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[0])["as_of"], "2026-08-30")

    def test_the_latest_snapshot_is_the_last_line(self):
        p = Path(tempfile.mkdtemp()) / "hist.jsonl"
        et.append_snapshot(et.build_snapshot([trip(1.0)] * 5, as_of="2026-08-31"), str(p))
        et.append_snapshot(et.build_snapshot([trip(1.0)] * 5, as_of="2026-09-01"), str(p))
        self.assertEqual(et.load_latest_snapshot(str(p))["as_of"], "2026-09-01")

    def test_a_missing_history_is_none_not_an_error(self):
        self.assertIsNone(et.load_latest_snapshot("/nonexistent/hist.jsonl"))


class SafetyTests(unittest.TestCase):
    def test_the_module_holds_no_execution_path(self):
        src = Path(et.__file__).read_text(encoding="utf-8")
        for bad in ("buy_stock", "sell_stock", "execute_trade_action",
                    "mockTrading/trade", "mockTrading/cancel"):
            self.assertNotIn(bad, src)

    def test_the_runner_holds_no_execution_path(self):
        src = (SKILL / "run_entry_tuner.py").read_text(encoding="utf-8")
        for bad in ("buy_stock", "sell_stock", "execute_trade_action",
                    "mockTrading/trade", "mockTrading/cancel"):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
