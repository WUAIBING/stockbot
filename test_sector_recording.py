#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The book must write down which sector it bought.

`industry` reads "unknown" on all 2,507 existing decision records, so the one
sector question that DID measure had to be answered by reconstructing sectors
from TDX dailies after the fact instead of reading the log.

That question: buys sharing a sector with another same-day buy underperformed
the names bought beside them by -4.90% at T+5 (t=-2.39) and -6.31% at T+10
(t=-2.26), demeaned within day. The sign survived dropping the 16-buy day,
dropping 半导体 and dropping 医药, but leave-one-day-out took T+5 to t=-1.62 on
17 shared names - so this is RECORDED, not enforced. A cap would have vetoed 17
of 60 buys on evidence one absent Tuesday can erase.

Recording is deliberately independent of TLFZ_REAL_SECTOR. That gate governs
scoring, and it stays off because the corrected sector score moved within-day
rank IC by -0.023 at T+1 (t=-0.52). Writing a field down cannot change a buy.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import evolving_model as em  # noqa: E402
import sector_map as sm  # noqa: E402

# The real 2026-08-31 basket: three of five in one sector.
MACHINERY = ("300747", "688700", "688596")
OTHERS = ("301217", "600186")


def has_map():
    return len(sm.SectorMap()) > 0


class ConcentrationTests(unittest.TestCase):
    def setUp(self):
        if not has_map():
            self.skipTest("tdxhy.cfg not present on this machine")

    def test_the_2026_08_31_basket_reports_three_in_one_sector(self):
        tally = em.sector_concentration(MACHINERY + OTHERS)
        self.assertEqual(tally.get("T0705"), 3)

    def test_a_spread_basket_reports_no_doubling(self):
        tally = em.sector_concentration(OTHERS)
        self.assertTrue(all(v == 1 for v in tally.values()), tally)

    def test_an_empty_basket_is_empty(self):
        self.assertEqual(em.sector_concentration([]), {})
        self.assertEqual(em.sector_concentration(None), {})

    def test_unmapped_codes_are_omitted_not_bucketed(self):
        """Folding new listings into a default would invent a crowded sector."""
        self.assertEqual(em.sector_concentration(["999999"]), {})

    def test_codes_are_zero_padded(self):
        self.assertEqual(em.sector_concentration([600186]),
                         em.sector_concentration(["600186"]))


class RecordTests(unittest.TestCase):
    def setUp(self):
        if not has_map():
            self.skipTest("tdxhy.cfg not present on this machine")
        self.tmp = Path(tempfile.mkdtemp()) / "decisions.jsonl"
        self.addCleanup(setattr, em, "MODEL_DECISIONS_FILE", em.MODEL_DECISIONS_FILE)
        em.MODEL_DECISIONS_FILE = self.tmp

    def write(self, codes, selected):
        cands = [{"code": c, "tier": 2, "mode": "pre_breakout",
                  "model_score": 70.0, "model_industry": "unknown"} for c in codes]
        em.record_decisions("close", cands, selected_codes=selected)
        return [json.loads(l) for l in self.tmp.read_text(encoding="utf-8").splitlines() if l.strip()]

    def test_every_record_carries_a_real_sector(self):
        rows = self.write(MACHINERY + OTHERS, MACHINERY + OTHERS)
        by = {r["code"]: r for r in rows}
        for c in MACHINERY:
            self.assertEqual(by[c]["sector"], "T0705")
            self.assertNotEqual(by[c]["sector_name"], "unknown")

    def test_a_sibling_buy_is_visible_in_the_record(self):
        """3 of 5 in T0705 - the thing nothing could say on 2026-08-31."""
        rows = self.write(MACHINERY + OTHERS, MACHINERY + OTHERS)
        by = {r["code"]: r for r in rows}
        for c in MACHINERY:
            self.assertEqual(by[c]["sector_same_day_buys"], 3)
        for c in OTHERS:
            self.assertEqual(by[c]["sector_same_day_buys"], 1)

    def test_the_count_reflects_buys_not_candidates(self):
        """A rejected sibling does not make a buy look crowded."""
        rows = self.write(MACHINERY + OTHERS, [MACHINERY[0]] + list(OTHERS))
        by = {r["code"]: r for r in rows}
        self.assertEqual(by[MACHINERY[0]]["sector_same_day_buys"], 1)
        # the rejected siblings still report the sector's buy count, which is 1
        self.assertEqual(by[MACHINERY[1]]["sector_same_day_buys"], 1)

    def test_a_rejected_row_still_gets_its_sector(self):
        rows = self.write(MACHINERY, [])
        for r in rows:
            self.assertEqual(r["sector"], "T0705")
            self.assertEqual(r["sector_same_day_buys"], 0)

    def test_the_legacy_industry_field_is_untouched(self):
        """Existing consumers keep reading exactly what they read before."""
        rows = self.write(MACHINERY, MACHINERY)
        for r in rows:
            self.assertEqual(r["industry"], "unknown")

    def test_recording_does_not_depend_on_the_scoring_gate(self):
        self.addCleanup(setattr, em, "REAL_SECTOR_ENABLED", em.REAL_SECTOR_ENABLED)
        em.REAL_SECTOR_ENABLED = False
        rows = self.write(MACHINERY, MACHINERY)
        self.assertEqual(rows[0]["sector"], "T0705")


class DegradationTests(unittest.TestCase):
    """A missing map must cost the record, never the run."""

    def setUp(self):
        self.addCleanup(setattr, em, "_SECTOR_MAP", em._SECTOR_MAP)
        self.addCleanup(setattr, em, "_SECTOR_MAP_LOADED", em._SECTOR_MAP_LOADED)
        em._SECTOR_MAP = None
        em._SECTOR_MAP_LOADED = True

    def test_concentration_is_empty_without_a_map(self):
        self.assertEqual(em.sector_concentration(MACHINERY), {})

    def test_records_still_write_without_a_map(self):
        tmp = Path(tempfile.mkdtemp()) / "d.jsonl"
        self.addCleanup(setattr, em, "MODEL_DECISIONS_FILE", em.MODEL_DECISIONS_FILE)
        em.MODEL_DECISIONS_FILE = tmp
        em.record_decisions("close",
                            [{"code": "300747", "tier": 2, "mode": "pre_breakout"}],
                            selected_codes=["300747"])
        row = json.loads(tmp.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["sector"], "unknown")
        self.assertEqual(row["sector_same_day_buys"], 0)


class SafetyTests(unittest.TestCase):
    def test_the_recording_path_holds_no_execution_call(self):
        src = Path(em.__file__).read_text(encoding="utf-8")
        start = src.index("def sector_concentration")
        end = src.index("def _load_decision_records")
        body = src[start:end]
        for bad in ("buy_stock", "sell_stock", "execute_trade_action",
                    "mockTrading/trade", "mockTrading/cancel"):
            self.assertNotIn(bad, body)


if __name__ == "__main__":
    unittest.main()
