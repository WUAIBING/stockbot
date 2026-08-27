#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The disclosure calendar tilt, where it meets live trading.

The measurement behind it, over 24,773 disclosure events on 272 dates, ranked
inside each stock own sector and quoted against each band own placebo:

    band        K=3            K=5            K=10          stocks
    top 1-3   +0.91 t=3.03   +0.49 t=1.30   -0.21 t=-0.47    1,084
    4-10      +0.53 t=2.73   +0.71 t=2.64   +0.94 t=2.45     2,342
    11-30     +0.49 t=3.42   +0.69 t=3.79   +0.74 t=3.00     5,082
    31+       +0.37 t=2.18   +0.54 t=2.33   +0.50 t=1.70    11,679

Three properties matter more than the arithmetic, and each is pinned below:

  IT IS OFF UNLESS ASKED FOR. Default disabled, so importing this module or
  shipping the file changes no live behaviour.

  IT ONLY BREAKS TIES. ranking_score is the SECONDARY sort key, applied after
  big_meat_seed_score. seed_score is deliberately untouched because it is
  re-derived into effective_tier through fixed thresholds - a bump there would
  promote a name between tiers and change its POSITION SIZE, which is far more
  than +0.91% over three sessions can justify.

  A STALE FILE IS IGNORED, NOT REUSED. The signal is how many sessions remain
  before a scheduled report. Yesterday file is not slightly out of date, it
  carries the wrong number, and acting on it would enter names whose window has
  already closed.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import v10_moni_trader as trader  # noqa: E402


def payload(today=20260827, selected=()):
    return {"today": today, "selected": list(selected)}


class TiltHarness(unittest.TestCase):
    def arm(self, data, *, enabled=True, filename="disclosure_candidates_latest.json"):
        tmp = Path(tempfile.mkdtemp()) / filename
        if data is not None:
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.addCleanup(setattr, trader, "DISCLOSURE_TILT_ENABLED",
                        trader.DISCLOSURE_TILT_ENABLED)
        self.addCleanup(setattr, trader, "DISCLOSURE_TILT_FILE",
                        trader.DISCLOSURE_TILT_FILE)
        self.addCleanup(setattr, trader, "_DISCLOSURE_TILT_CACHE",
                        trader._DISCLOSURE_TILT_CACHE)
        trader.DISCLOSURE_TILT_ENABLED = enabled
        trader.DISCLOSURE_TILT_FILE = tmp
        trader._DISCLOSURE_TILT_CACHE = {"loaded": False, "by_code": {}, "note": ""}


class DefaultOffTests(TiltHarness):
    def test_disabled_by_default_on_import(self):
        """Shipping the file must not change live behaviour on its own."""
        self.assertFalse(trader.DISCLOSURE_TILT_ENABLED)

    def test_disabled_yields_no_bonus_even_with_a_good_file(self):
        self.arm(payload(selected=[{"code": "688825", "sector_rank": 1,
                                    "sessions_until": 2}]), enabled=False)
        bonus, note = trader._disclosure_tilt_bonus("688825")
        self.assertEqual(bonus, 0.0)
        self.assertEqual(note, "")


class BandTests(TiltHarness):
    LEADER = {"code": "688825", "sector_rank": 1, "sessions_until": 2,
              "sector": "半导体"}
    MID = {"code": "603186", "sector_rank": 6, "sessions_until": 3,
           "sector": "元件"}
    UNRANKED = {"code": "600900", "sector_rank": None, "sessions_until": 2,
                "sector": "电力"}

    def test_a_sector_leader_gets_the_larger_tilt(self):
        self.arm(payload(selected=[self.LEADER, self.MID]))
        lead, _ = trader._disclosure_tilt_bonus("688825")
        mid, _ = trader._disclosure_tilt_bonus("603186")
        self.assertEqual(lead, trader.DISCLOSURE_TILT_LEADER_BONUS)
        self.assertEqual(mid, trader.DISCLOSURE_TILT_MID_BONUS)
        self.assertGreater(lead, mid)

    def test_an_unranked_name_gets_the_smaller_tilt_not_the_leader_one(self):
        """A failed sector query must not be rewarded as though it were rank 1."""
        self.arm(payload(selected=[self.UNRANKED]))
        bonus, note = trader._disclosure_tilt_bonus("600900")
        self.assertEqual(bonus, trader.DISCLOSURE_TILT_MID_BONUS)
        self.assertIn("未排名", note)

    def test_a_name_not_on_the_calendar_gets_nothing(self):
        self.arm(payload(selected=[self.LEADER]))
        self.assertEqual(trader._disclosure_tilt_bonus("000001"), (0.0, ""))

    def test_the_note_says_why(self):
        self.arm(payload(selected=[self.LEADER]))
        _, note = trader._disclosure_tilt_bonus("688825")
        self.assertIn("披露倾斜", note)
        self.assertIn("2", note)

    def test_the_tilt_stays_small_against_model_score(self):
        """model_score runs to ~100; this is a tiebreaker, not a re-ranking."""
        self.assertLessEqual(trader.DISCLOSURE_TILT_LEADER_BONUS, 2.0)
        self.assertGreater(trader.DISCLOSURE_TILT_LEADER_BONUS, 0.0)


class StaleAndMissingTests(TiltHarness):
    def test_a_missing_file_is_not_an_error(self):
        self.arm(None)
        self.assertEqual(trader._disclosure_tilt_bonus("688825"), (0.0, ""))

    def test_a_corrupt_file_is_not_an_error(self):
        self.arm(payload())
        Path(trader.DISCLOSURE_TILT_FILE).write_text("not json", encoding="utf-8")
        self.assertEqual(trader._disclosure_tilt_bonus("688825"), (0.0, ""))

    def test_a_stale_file_is_ignored_rather_than_reused(self):
        """Sessions-until from yesterday is the WRONG number, not a stale one."""
        self.arm(payload(today=20260826,
                         selected=[{"code": "688825", "sector_rank": 1,
                                    "sessions_until": 2}]))
        bonus, _ = trader._disclosure_tilt_bonus("688825", today_yyyymmdd=20260827)
        self.assertEqual(bonus, 0.0)

    def test_a_current_file_is_used(self):
        self.arm(payload(today=20260827,
                         selected=[{"code": "688825", "sector_rank": 1,
                                    "sessions_until": 2}]))
        bonus, _ = trader._disclosure_tilt_bonus("688825", today_yyyymmdd=20260827)
        self.assertEqual(bonus, trader.DISCLOSURE_TILT_LEADER_BONUS)

    def test_rejected_candidates_in_the_file_are_not_tilted(self):
        """Only the selected list carries names inside the measured window."""
        data = payload(selected=[{"code": "688825", "sector_rank": 1,
                                  "sessions_until": 2}])
        data["rejected"] = [{"code": "000001", "sector_rank": 90,
                             "sessions_until": 2}]
        self.arm(data)
        self.assertEqual(trader._disclosure_tilt_bonus("000001"), (0.0, ""))


class WiringTests(unittest.TestCase):
    """Where the bonus is allowed to land, checked in the source itself."""

    SRC = Path(trader.__file__).read_text(encoding="utf-8")

    def test_it_is_added_to_the_secondary_sort_key(self):
        self.assertIn("+ disclosure_bonus, 4)", self.SRC)

    def test_it_never_touches_seed_score(self):
        """seed_score is re-derived into effective_tier by threshold: a bump
        there changes POSITION SIZE, not just ordering."""
        for line in self.SRC.splitlines():
            if "disclosure_bonus" in line:
                self.assertNotIn("seed_score", line, line.strip())
                self.assertNotIn("effective_tier", line, line.strip())

    def test_the_helper_cannot_raise_into_the_buy_path(self):
        self.assertIn("except (OSError, ValueError):", self.SRC)


if __name__ == "__main__":
    unittest.main()
