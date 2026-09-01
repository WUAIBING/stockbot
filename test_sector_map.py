#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Giving the decision path a sector it has never had.

`industry` reads "unknown" on all 2,507 records in v10_model_decisions.jsonl and
the scan CSV has no industry column. The `sector` score component is not a
sector measure - across those decisions it takes 20 distinct values against
1,067 for `stock` and 918 for `flow`, and on 2026-08-31 every candidate scored
exactly 60.41. One number per day cannot rank one stock against another.

The cost is visible in that day's buys: 300747 锐科激光, 688700 东威科技 and
688596 正帆科技 all sit in T0705 工业机械 - three of five in one sector, and
nothing in the system could say so.

Fixtures are the real mappings from the shipped tdxhy.cfg and incon.dat.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import sector_map as sm  # noqa: E402

# Real rows from tdxhy.cfg, and the TDXNHY names for them.
CODES = {
    "301217": "T1204",      # 铜冠铜箔
    "300747": "T070506",    # 锐科激光
    "688700": "T070506",    # 东威科技
    "688596": "T070506",    # 正帆科技
    "600186": "T030403",    # 莲花控股
    "002396": "T1202",      # 星网锐捷
    "688432": "T1203",      # 有研硅
}
# Names exist at BOTH levels. The bands this map serves - top 1-3 +0.91%,
# 4-10 +0.53%, 31+ +0.37% - were measured on the 5-character truncation
# (56 sectors, median ~60 members), which is what populates a "31+" band at
# all. The full code gives 112 sectors with median 23 and the bands would not
# mean the same thing, so DEFAULT_LEVEL is 5 and 专用机械 rolls up into its
# parent 工业机械.
NAMES = {
    "T1204": "元器件",
    "T0705": "工业机械",
    "T070506": "专用机械",
    "T0304": "食品饮料",
    "T030403": "食品",
    "T1202": "通信设备",
    "T1203": "半导体",
}


def smap():
    return sm.SectorMap(CODES, NAMES)


class LookupTests(unittest.TestCase):
    def test_a_code_resolves_to_a_named_sector(self):
        self.assertEqual(smap().sector_of("688432"), ("T1203", "半导体"))

    def test_a_deeper_code_rolls_up_to_its_named_parent(self):
        """T070506 专用机械 sits under T0705 工业机械 at the measured level."""
        self.assertEqual(smap().sector_of("300747"), ("T0705", "工业机械"))

    def test_the_three_machinery_names_share_a_sector(self):
        m = smap()
        secs = {m.sector_code(c) for c in ("300747", "688700", "688596")}
        self.assertEqual(secs, {"T0705"})
        self.assertEqual(m.sector_name("300747"), "工业机械")

    def test_an_unmapped_code_is_none_not_a_bucket(self):
        """A new listing is absent until the file refreshes. Folding it into a
        default would turn every new listing into one large fake sector."""
        self.assertIsNone(smap().sector_of("999999"))
        self.assertIsNone(smap().sector_code("999999"))

    def test_codes_are_zero_padded(self):
        self.assertEqual(smap().sector_code(600186), "T0304")

    def test_members_lists_the_sector(self):
        self.assertEqual(smap().members("T0705"),
                         ["300747", "688596", "688700"])


class NameParsingTests(unittest.TestCase):
    """incon.dat holds several classification systems; only TDXNHY matches."""

    SAMPLE = (
        "#ZJHHY\n"
        "A|农、林、牧、渔业\n"
        "T070506|WRONG SECTION SHOULD NOT WIN\n"
        "#TDXNHY\n"
        "T070506|专用机械\n"
        "T1204|元器件\n"
        "#SWHY\n"
        "T1204|ALSO WRONG\n"
    )

    def test_only_the_tdxnhy_section_is_read(self):
        import tempfile, os
        p = Path(tempfile.mkdtemp()) / "incon.dat"
        p.write_bytes(self.SAMPLE.encode("gbk"))
        names = sm.load_sector_names(str(p))
        self.assertEqual(names.get("T070506"), "专用机械")
        self.assertEqual(names.get("T1204"), "元器件")

    def test_a_missing_file_is_empty_not_an_error(self):
        self.assertEqual(sm.load_sector_names("/nonexistent/incon.dat"), {})
        self.assertEqual(sm.load_code_sectors("/nonexistent/tdxhy.cfg"), {})


class RankTests(unittest.TestCase):
    """The validated bands are top 1-3 +0.91%, 4-10 +0.53%, 31+ +0.37%."""

    def turnover(self, **kw):
        base = {c: 1.0e8 for c in CODES}
        base.update(kw)
        return base

    def test_the_biggest_turnover_ranks_first(self):
        t = self.turnover(**{"300747": 9.0e8, "688700": 5.0e8, "688596": 1.0e8})
        r = sm.rank_within_sector("300747", t, smap(), min_members=3)
        self.assertEqual(r["rank"], 1)
        self.assertEqual(r["sector_name"], "工业机械")

    def test_the_smallest_ranks_last(self):
        t = self.turnover(**{"300747": 9.0e8, "688700": 5.0e8, "688596": 1.0e8})
        self.assertEqual(
            sm.rank_within_sector("688596", t, smap(), min_members=3)["rank"], 3)

    def test_a_thin_sector_declines_rather_than_flattering(self):
        """Being 1 of 3 is not leadership."""
        r = sm.rank_within_sector("300747", self.turnover(), smap(),
                                  min_members=10)
        self.assertIsNone(r["rank"])
        self.assertIn("need 10", r["reason"])

    def test_an_unknown_sector_declines(self):
        r = sm.rank_within_sector("999999", self.turnover(), smap())
        self.assertIsNone(r["rank"])
        self.assertIn("sector unknown", r["reason"])

    def test_a_code_with_no_turnover_declines(self):
        """688432 有研硅 was halted: no turnover, so no rank."""
        t = {c: 1.0e8 for c in CODES if c != "688432"}
        r = sm.rank_within_sector("688432", t, smap(), min_members=1)
        self.assertIsNone(r["rank"])

    def test_the_result_admits_it_is_a_proxy(self):
        """The validated bands used a trailing 20-session mean, not one day."""
        t = self.turnover(**{"300747": 9.0e8})
        r = sm.rank_within_sector("300747", t, smap(), min_members=3)
        self.assertEqual(r["basis"], "single_session_turnover_proxy")

    def test_every_outcome_carries_a_reason(self):
        for code in ("300747", "999999"):
            self.assertTrue(
                sm.rank_within_sector(code, self.turnover(), smap())["reason"])


class ConcentrationTests(unittest.TestCase):
    def test_the_2026_08_31_basket_shows_its_concentration(self):
        out = sm.concentration(
            ["301217", "300747", "688700", "688596", "600186"], smap())
        self.assertEqual(out[0]["sector"], "T0705")
        self.assertEqual(out[0]["count"], 3)
        self.assertEqual(out[0]["sector_name"], "工业机械")

    def test_unmapped_codes_group_as_unknown_not_as_a_sector(self):
        out = sm.concentration(["999999", "888888"], smap())
        self.assertEqual(out[0]["sector"], "unknown")

    def test_an_empty_basket_is_empty(self):
        self.assertEqual(sm.concentration([], smap()), [])


class PathTests(unittest.TestCase):
    """The fallback path had one dirname too many.

    sector_map.py lives in workbuddy/skills/a-share-analyst/, so ONE dirname
    reaches workbuddy/skills/ where csi1000-skills sits. Going up twice and then
    re-appending "skills" built workbuddy/skills/skills/csi1000-skills, which
    never exists.

    The miss was silent: load_code_sectors returns {} on OSError-or-absent, so
    SectorMap loaded zero codes and every stock resolved to unknown - exactly
    the bug this module exists to fix. It hid on the droplet because the
    absolute /opt/stockbot path matches first, and it hid in the tests because
    ShippedDataTests SKIPS when the map is empty. A skip is not a pass.
    """

    def test_no_candidate_path_doubles_the_skills_directory(self):
        for p in sm._TDXHY_CANDIDATES + sm._INCON_CANDIDATES:
            norm = p.replace("\\", "/")
            self.assertNotIn("skills/skills", norm, p)

    def test_the_repo_relative_fallback_resolves_when_the_file_is_there(self):
        """If the repo ships the data, the fallback MUST find it - rather than
        skipping, which is how the doubled path survived."""
        import os
        repo = Path(sm.__file__).resolve().parent.parent / "csi1000-skills"
        if not (repo / "tdxhy.cfg").exists():
            self.skipTest("csi1000-skills/tdxhy.cfg not in this checkout")
        self.assertTrue(any(os.path.exists(p) for p in sm._TDXHY_CANDIDATES),
                        "shipped tdxhy.cfg exists but no candidate path finds it")
        self.assertGreater(len(sm.SectorMap()), 1000)


class ShippedDataTests(unittest.TestCase):
    """The real files, if present on this machine."""

    def test_the_shipped_map_covers_the_book(self):
        m = sm.SectorMap()
        if len(m) == 0:
            self.skipTest("tdxhy.cfg not present on this machine")
        for code in ("301217", "300747", "688596", "600186", "002396", "688432"):
            self.assertIsNotNone(m.sector_of(code), code)

    def test_the_shipped_names_resolve(self):
        m = sm.SectorMap()
        if len(m) == 0:
            self.skipTest("tdxhy.cfg not present on this machine")
        name = m.sector_name("688432")
        self.assertTrue(name and not name.startswith("T"), name)


if __name__ == "__main__":
    unittest.main()
