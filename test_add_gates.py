#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Adding to a winner: our book said yes, the market says no.

Our 124 round trips said adds returned +4.83% against +1.07% for a fresh entry
(t+2.38), on cells of 8 and 13 trades. Asked of every liquid stock from 2015 to
2026 - 2.0M observations, excess over the same session's liquid universe, t
across entry sessions - it inverts:

    first up +10%, then         next 10 sess    next 60 sess
      reached within 4 sess       -2.007%         -5.325%   t-19.24 / t-39.60
      reached in 5 or more        -0.980%         -2.785%   t-18.26 / t-29.52
      baseline                    -0.000%         +0.000%

Buying strength underperforms at every horizon, worsening with time. The boat
disagreed because it measured forward to the ACTUAL exit - crediting a good
exit rule to the entry - over three months whose only profitable one was June.

So adds stay shut. What survives is SPEED (slow beats fast, +2.540% at 60
sessions, t+17.68 - though both are negative) and the sector gate being a
constant on a scale that no longer exists.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import add_gates as ag  # noqa: E402

# Real recentred per-sector scores, 2026-08-31 shape.
SECTORS = {"T0705": 72.9, "T1204": 61.0, "T1203": 55.4, "T0304": 47.2,
           "T1202": 40.1, "T0203": 31.8, "T1101": 23.6}


class HoldWindowTests(unittest.TestCase):
    """A floor, not a ceiling - the inversion this module corrects."""

    def test_the_grinder_passes(self):
        """002396 星网锐捷: 17+ sessions, +31.57%, excluded by the old ceiling.
        Passing here means "less bad", not "good" - the slow bucket still
        measures -0.980% over 10 sessions on the market."""
        ok, why = ag.hold_window_ok(17)
        self.assertTrue(ok)
        self.assertIn("17", why)

    def test_the_fast_spike_is_refused(self):
        """Fast movers are the worst group on 11 years of market data:
        -2.007% over the next 10 sessions (t-19.24) against -0.980% for the
        slower group. The old ceiling selected exactly this group."""
        ok, why = ag.hold_window_ok(3)
        self.assertFalse(ok)
        self.assertIn("spent", why)

    def test_the_boundary_is_the_fifth_session(self):
        self.assertFalse(ag.hold_window_ok(4)[0])
        self.assertTrue(ag.hold_window_ok(5)[0])

    def test_an_unknown_hold_length_refuses(self):
        """A halted name has no session count; absence is not a pass."""
        self.assertFalse(ag.hold_window_ok(None)[0])


class ProfitTests(unittest.TestCase):
    def test_the_measured_trigger_passes(self):
        self.assertTrue(ag.profit_ok(10.41)[0])       # 600403 大有能源

    def test_below_the_trigger_refuses(self):
        self.assertFalse(ag.profit_ok(3.49)[0])       # 603039 泛微网络

    def test_a_loser_refuses(self):
        self.assertFalse(ag.profit_ok(-8.11)[0])      # 300747 锐科激光

    def test_a_missing_figure_refuses(self):
        self.assertFalse(ag.profit_ok(None)[0])


class SectorRankTests(unittest.TestCase):
    """A percentile, because the constant could not survive a scale change."""

    def test_the_strongest_sector_passes(self):
        ok, why = ag.sector_rank_ok("T0705", SECTORS)
        self.assertTrue(ok)
        self.assertIn("100%", why)

    def test_a_weak_sector_refuses(self):
        self.assertFalse(ag.sector_rank_ok("T1101", SECTORS)[0])

    def test_the_middle_refuses_at_the_top_third(self):
        self.assertFalse(ag.sector_rank_ok("T0304", SECTORS)[0])

    def test_a_single_bucket_refuses_rather_than_passing_everything(self):
        """THE BUG. One global number carries no information about whether THIS
        sector is strong. The old constant compared against it and became a
        market-wide on/off switch - open 23% of days, shut 77%, for reasons
        unrelated to the stock. Passing on no information is how that happened,
        so no information must mean no."""
        ok, why = ag.sector_rank_ok("unknown", {"unknown": 80.0})
        self.assertFalse(ok)
        self.assertIn("no ranking possible", why)

    def test_it_survives_the_scale_change_that_broke_the_constant(self):
        """Raw real scores run 6.02-55.29 and never reach the old 75; recentred
        they run 23.61-72.88 and still never reach it. A percentile ranks the
        same either way."""
        raw = {k: v - 17.59 for k, v in SECTORS.items()}
        self.assertTrue(max(SECTORS.values()) < 75.0)
        self.assertTrue(max(raw.values()) < 75.0)
        self.assertEqual(ag.sector_rank_ok("T0705", SECTORS)[0],
                         ag.sector_rank_ok("T0705", raw)[0])

    def test_an_unscored_sector_refuses(self):
        self.assertFalse(ag.sector_rank_ok("T9999", SECTORS)[0])

    def test_no_scores_at_all_refuses(self):
        self.assertFalse(ag.sector_rank_ok("T0705", {})[0])
        self.assertFalse(ag.sector_rank_ok(None, SECTORS)[0])


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, ag, "ADD_GATES_ENABLED", ag.ADD_GATES_ENABLED)

    def test_the_gate_is_off_by_default(self):
        import os
        self.assertNotIn(os.environ.get("TLFZ_ADD_GATES", "0").lower(),
                         ("1", "true", "yes", "on"))

    def test_nothing_is_allowed_while_disabled(self):
        ag.ADD_GATES_ENABLED = False
        ok, checks = ag.evaluate_add({"code": "002396", "profit_pct": 31.57},
                                     "T0705", SECTORS, 17)
        self.assertFalse(ok)
        self.assertEqual(checks[0][0], "enabled")

    def test_a_qualifying_position_is_allowed(self):
        ag.ADD_GATES_ENABLED = True
        ok, checks = ag.evaluate_add({"code": "002396", "profit_pct": 31.57},
                                     "T0705", SECTORS, 17,
                                     tradable_codes=["002396"])
        self.assertTrue(ok, checks)

    def test_every_gate_reports_even_when_it_passes(self):
        """A check that only speaks when refusing leaves no way to tell a real
        pass from one that silently did nothing."""
        ag.ADD_GATES_ENABLED = True
        _ok, checks = ag.evaluate_add({"code": "002396", "profit_pct": 31.57},
                                      "T0705", SECTORS, 17)
        self.assertEqual({c[0] for c in checks},
                         {"profit", "hold_window", "sector_rank"})
        for _name, _passed, why in checks:
            self.assertTrue(why)

    def test_one_failed_gate_refuses_the_add(self):
        ag.ADD_GATES_ENABLED = True
        ok, _ = ag.evaluate_add({"code": "603039", "profit_pct": 3.49},
                                "T0705", SECTORS, 20)
        self.assertFalse(ok)

    def test_a_halted_name_is_refused_before_anything_else(self):
        """688432 有研硅 is suspended with no reopen date."""
        ag.ADD_GATES_ENABLED = True
        ok, checks = ag.evaluate_add({"code": "688432", "profit_pct": 20.0},
                                     "T1203", SECTORS, 10,
                                     tradable_codes=["002396"])
        self.assertFalse(ok)
        self.assertEqual(checks[0][0], "tradable")


class SafetyTests(unittest.TestCase):
    def test_the_module_places_no_orders(self):
        src = Path(ag.__file__).read_text(encoding="utf-8")
        for bad in ("buy_stock", "sell_stock", "execute_trade_action",
                    "mockTrading/trade", "mockTrading/cancel", "requests.post"):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
