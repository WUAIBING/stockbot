#!/usr/bin/env python3
"""Tests for position_sizer.py and pipeline_schema.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"))

from position_sizer import SizerConfig, compute_position_weights
from pipeline_schema import (
    PipelineValidationError,
    validate_candidate_pool,
)


class PositionSizerTests(unittest.TestCase):
    def _candidates(self, *specs):
        return [
            {
                "code": s[0],
                "name": s[1],
                "score": s[2],
                "avg_candidate_win_rate": s[3],
                "avg_candidate_avg_return": s[4],
                "avg_profitability_priority": s[5],
                "volatility": s[6],
                "correlation_group": s[7],
                "selection_rank": s[8] if len(s) > 8 else i + 1,
            }
            for i, s in enumerate(specs)
        ]

    def test_normal_allocation_ranks_by_conviction(self):
        candidates = self._candidates(
            ("000001", "A", 92, 0.62, 2.5, 115, 22, "fin"),
            ("000002", "B", 85, 0.55, 1.9, 95, 35, "real"),
        )
        alloc, _ = compute_position_weights(candidates, 1_000_000, drawdown_pct=0.0, window_key="10:00")
        self.assertGreater(len(alloc), 0)
        self.assertEqual(alloc[0].code, "000001")
        self.assertGreater(alloc[0].weight_pct, alloc[1].weight_pct)

    def test_drawdown_max_blocks_all(self):
        candidates = self._candidates(("000001", "A", 92, 0.62, 2.5, 115, 22, "fin"))
        alloc, dbg = compute_position_weights(candidates, 1_000_000, drawdown_pct=16.0, window_key="10:00")
        self.assertEqual(len(alloc), 0)
        self.assertEqual(dbg["blocked"], "drawdown_exceeded_max")

    def test_drawdown_at_threshold_no_throttle(self):
        candidates = self._candidates(("000001", "A", 92, 0.62, 2.5, 115, 22, "fin"))
        _, dbg = compute_position_weights(candidates, 1_000_000, drawdown_pct=5.0, window_key="10:00")
        self.assertEqual(dbg["drawdown_scale"], 1.0)

    def test_drawdown_mid_throttles(self):
        candidates = self._candidates(("000001", "A", 92, 0.62, 2.5, 115, 22, "fin"))
        _, dbg = compute_position_weights(candidates, 1_000_000, drawdown_pct=10.0, window_key="10:00")
        self.assertEqual(dbg["drawdown_scale"], 0.5)

    def test_correlation_group_concentration_cap(self):
        candidates = self._candidates(
            ("000001", "A", 90, 0.60, 2.0, 110, 20, "fin"),
            ("000002", "B", 88, 0.58, 1.8, 105, 22, "fin"),
            ("000003", "C", 85, 0.55, 1.5, 95, 25, "fin"),
        )
        alloc, _ = compute_position_weights(candidates, 1_000_000, drawdown_pct=0.0, window_key="10:00")
        total_fin = sum(a.weight_pct for a in alloc)
        self.assertLessEqual(total_fin, 25.0)

    def test_max_positions_limit(self):
        candidates = self._candidates(
            ("000001", "A", 90, 0.60, 2.0, 110, 20, "a"),
            ("000002", "B", 88, 0.58, 1.8, 105, 22, "b"),
            ("000003", "C", 85, 0.55, 1.5, 95, 25, "c"),
            ("000004", "D", 82, 0.52, 1.2, 88, 30, "d"),
            ("000005", "E", 78, 0.50, 1.0, 82, 28, "e"),
            ("000006", "F", 75, 0.48, 0.8, 78, 32, "f"),
            ("000007", "G", 72, 0.45, 0.5, 72, 35, "g"),
            ("000008", "H", 68, 0.42, 0.3, 65, 40, "h"),
            ("000009", "I", 65, 0.40, 0.1, 60, 45, "i"),
        )
        config = SizerConfig(max_positions=5)
        alloc, _ = compute_position_weights(candidates, 1_000_000, drawdown_pct=0.0, window_key="10:00", config=config)
        self.assertLessEqual(len(alloc), 5)

    def test_low_volatility_gets_higher_weight(self):
        low_vol = self._candidates(("000001", "A", 85, 0.55, 1.8, 95, 15, "fin"))
        high_vol = self._candidates(("000002", "B", 85, 0.55, 1.8, 95, 55, "fin"))
        a1, _ = compute_position_weights(low_vol, 1_000_000, drawdown_pct=0.0, window_key="10:00")
        a2, _ = compute_position_weights(high_vol, 1_000_000, drawdown_pct=0.0, window_key="10:00")
        self.assertGreater(a1[0].vol_scale, a2[0].vol_scale)

    def test_window_cap_enforced(self):
        candidates = self._candidates(("000001", "A", 95, 0.65, 3.0, 120, 18, "fin"))
        a_late, _ = compute_position_weights(candidates, 1_000_000, drawdown_pct=0.0, window_key="14:50")
        a_early, _ = compute_position_weights(candidates, 1_000_000, drawdown_pct=0.0, window_key="10:00")
        self.assertLessEqual(a_late[0].entry_cap_ratio, a_early[0].entry_cap_ratio)


class PipelineSchemaTests(unittest.TestCase):
    def _valid_pool(self, **overrides):
        base = {
            "generated_at": "2026-07-15 10:00:00",
            "trade_date": "2026-07-15",
            "status": "ok",
            "selected_count": 1,
            "candidate_count": 3,
            "selected_records": [
                {
                    "code": "000001",
                    "name": "TEST",
                    "tier": 1,
                    "selection_rank": 1,
                    "selection_score": 92.0,
                    "avg_profitability_priority": 115.0,
                    "avg_candidate_win_rate": 0.62,
                    "avg_candidate_avg_return": 2.5,
                    "target_weight_pct": 5.0,
                    "score": 92.0,
                    "volatility": 22.0,
                }
            ],
        }
        base.update(overrides)
        return base

    def test_valid_pool_no_violations(self):
        violations = validate_candidate_pool(self._valid_pool())
        self.assertEqual(len(violations), 0)

    def test_bad_status_raises(self):
        with self.assertRaises(PipelineValidationError) as ctx:
            validate_candidate_pool(self._valid_pool(status="error"))
        self.assertIn("error", str(ctx.exception))

    def test_missing_required_field_raises(self):
        with self.assertRaises(PipelineValidationError):
            validate_candidate_pool(self._valid_pool(
                selected_records=[{"code": "000001", "name": "test"}]
            ))

    def test_nan_score_raises(self):
        bad = self._valid_pool()
        bad["selected_records"][0]["selection_score"] = float("nan")
        with self.assertRaises(PipelineValidationError) as ctx:
            validate_candidate_pool(bad)
        self.assertIn("NaN", str(ctx.exception))

    def test_inf_volatility_raises(self):
        bad = self._valid_pool()
        bad["selected_records"][0]["volatility"] = float("inf")
        with self.assertRaises(PipelineValidationError) as ctx:
            validate_candidate_pool(bad)
        self.assertIn("Inf", str(ctx.exception))

    def test_out_of_range_score_raises(self):
        bad = self._valid_pool()
        bad["selected_records"][0]["selection_score"] = 999.0
        with self.assertRaises(PipelineValidationError) as ctx:
            validate_candidate_pool(bad)
        self.assertIn("outside", str(ctx.exception))

    def test_negative_win_rate_raises(self):
        bad = self._valid_pool()
        bad["selected_records"][0]["avg_candidate_win_rate"] = -0.5
        with self.assertRaises(PipelineValidationError) as ctx:
            validate_candidate_pool(bad)
        self.assertIn("outside", str(ctx.exception))

    def test_count_mismatch_raises(self):
        with self.assertRaises(PipelineValidationError) as ctx:
            validate_candidate_pool(self._valid_pool(selected_count=99))
        self.assertIn("99", str(ctx.exception))

    def test_code_with_market_suffix_warns(self):
        bad = self._valid_pool()
        bad["selected_records"][0]["code"] = "000001.SZ"
        with self.assertRaises(PipelineValidationError) as ctx:
            validate_candidate_pool(bad)
        self.assertIn("market suffix", str(ctx.exception))

    def test_multiple_violations_reported(self):
        bad = self._valid_pool(
            status="error",
            selected_records=[{"code": "", "name": ""}],
        )
        with self.assertRaises(PipelineValidationError) as ctx:
            validate_candidate_pool(bad)
        self.assertIn("schema violations", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
