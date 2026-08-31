#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fixing the sector map must change ranking, not the score level.

The industry map was keyed on the market field, so all 5,586 lines collapsed to
three entries, every stock resolved to "unknown", and the whole universe fell
into one bucket. That is a bug, and fixing it is correct.

But the fix moves the SCALE. The single-bucket score sat at 60.41 on 2026-08-31
while real sector scores run 6.02 to 55.29, so every candidate dropped - mean
3.32, worst 10.27 - even though ranking barely moved (top-10 overlap 8 of 10).

That level matters because min_trade_score is a CONSTANT chosen by market
regime (64/58/52), not a percentile, so it cannot follow the scale. Deployed
raw, the fix cut candidates clearing 64 from 29 to 21 - tightening a book
already holding 12 of 25 slots by 28%, the opposite of what it needs.

Recentring restores the level and keeps the ranking:

    gate off   industry_map 3   sector groups 1    clearing 64: 29
    gate on    industry_map 5586, groups 22        clearing 64: 32
    mean model_score 63.86 -> 63.85 (delta -0.00)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import evolving_model as em  # noqa: E402


def rows(spec):
    """spec: list of (code, tier, weekly_slope, amt_ratio)."""
    return [{"code": c, "tier": t, "weekly_slope": s, "amt_ratio": a,
             "close_vs_ma20_pct": 1.0, "rsi14": 55.0, "is_green": True,
             "mode": "pre_breakout"}
            for c, t, s, a in spec]


SPEC = [("300747", 2, 5.0, 1.5), ("688700", 2, 4.0, 1.4),
        ("688596", 2, 3.0, 1.3), ("301217", 2, 6.0, 1.6),
        ("600186", 3, 1.0, 1.1), ("002396", 1, 8.0, 2.0),
        ("688432", 2, 2.0, 1.2), ("600206", 2, 7.0, 1.8)]
# T0705 holds three of them, exactly as on 2026-08-31.
IMAP = {"300747": "T070506", "688700": "T070506", "688596": "T070506",
        "301217": "T1204", "600186": "T030403", "002396": "T1202",
        "688432": "T1203", "600206": "T1203"}


class RecentreTests(unittest.TestCase):
    def setUp(self):
        self.rows = rows(SPEC)
        self.legacy = em._compute_sector_stats(self.rows, {})
        self.real = em._compute_sector_stats(self.rows, IMAP)

    def test_the_broken_map_makes_exactly_one_bucket(self):
        """Every stock unknown, so one group and one score for the whole book."""
        self.assertEqual(len(self.legacy), 1)

    def test_the_real_map_separates_sectors(self):
        self.assertGreater(len(self.real), 1)

    def test_recentring_is_off_when_the_gate_is_off(self):
        self.addCleanup(setattr, em, "REAL_SECTOR_ENABLED", em.REAL_SECTOR_ENABLED)
        em.REAL_SECTOR_ENABLED = False
        out = em._recentre_sector_scores(self.rows, IMAP, self.real)
        self.assertEqual(out, self.real)

    def test_recentring_preserves_the_mean(self):
        """The level is what min_trade_score reads; only ranking may move."""
        self.addCleanup(setattr, em, "REAL_SECTOR_ENABLED", em.REAL_SECTOR_ENABLED)
        em.REAL_SECTOR_ENABLED = True
        out = em._recentre_sector_scores(self.rows, IMAP, self.real)
        target = list(self.legacy.values())[0]["score"]
        got = [out[IMAP[r["code"]]]["score"] for r in self.rows]
        self.assertAlmostEqual(sum(got)/len(got), target, places=1)

    def test_recentring_preserves_relative_order(self):
        """Shifting every sector by one constant cannot reorder them."""
        self.addCleanup(setattr, em, "REAL_SECTOR_ENABLED", em.REAL_SECTOR_ENABLED)
        em.REAL_SECTOR_ENABLED = True
        out = em._recentre_sector_scores(self.rows, IMAP, self.real)
        before = [k for k, _ in sorted(self.real.items(), key=lambda kv: -kv[1]["score"])]
        after = [k for k, _ in sorted(out.items(), key=lambda kv: -kv[1]["score"])]
        self.assertEqual(before, after)

    def test_the_shift_is_recorded_for_inspection(self):
        self.addCleanup(setattr, em, "REAL_SECTOR_ENABLED", em.REAL_SECTOR_ENABLED)
        em.REAL_SECTOR_ENABLED = True
        out = em._recentre_sector_scores(self.rows, IMAP, self.real)
        entry = list(out.values())[0]
        self.assertIn("recentre_shift", entry)
        self.assertIn("score_before_recentre", entry)

    def test_scores_stay_inside_the_scale(self):
        self.addCleanup(setattr, em, "REAL_SECTOR_ENABLED", em.REAL_SECTOR_ENABLED)
        em.REAL_SECTOR_ENABLED = True
        out = em._recentre_sector_scores(self.rows, IMAP, self.real)
        for v in out.values():
            self.assertGreaterEqual(v["score"], 0.0)
            self.assertLessEqual(v["score"], 100.0)

    def test_empty_input_is_returned_untouched(self):
        self.addCleanup(setattr, em, "REAL_SECTOR_ENABLED", em.REAL_SECTOR_ENABLED)
        em.REAL_SECTOR_ENABLED = True
        self.assertEqual(em._recentre_sector_scores([], IMAP, {}), {})
        self.assertEqual(em._recentre_sector_scores(self.rows, IMAP, {}), {})


class GateTests(unittest.TestCase):
    """Default must reproduce the historical behaviour exactly."""

    def test_the_gate_is_off_by_default(self):
        import os
        self.assertNotIn(os.environ.get("TLFZ_REAL_SECTOR", "0").lower(),
                         ("1", "true", "yes", "on"))

    def test_the_broken_key_is_kept_verbatim_when_off(self):
        """Not a partial fix: with the gate off the map is the old three
        entries, so behaviour is bit-identical rather than nearly so."""
        src = Path(em.__file__).read_text(encoding="utf-8")
        self.assertIn("mapping[parts[0].strip()] = industry", src)
        self.assertIn("if REAL_SECTOR_ENABLED:", src)

    def test_min_trade_score_is_a_constant_not_a_percentile(self):
        """Which is exactly why the level had to be preserved: a constant
        threshold cannot follow a scale change."""
        ctx = {"market": {"score": 58.0}}
        self.assertEqual(em.compute_min_trade_score(ctx), 64.0)
        self.assertEqual(em.compute_min_trade_score({"market": {"score": 80.0}}), 52.0)


if __name__ == "__main__":
    unittest.main()
