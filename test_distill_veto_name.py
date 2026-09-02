#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A template with no negative veto must not take down the pool build.

It did, for four sessions. `build_veto_name` called `params.get("family", ...)`
without a guard, so a template whose `negative_veto` is absent raised

    AttributeError: 'NoneType' object has no attribute 'get'

from `build_workbuddy_distill_pool.build_payload`. Every
`refresh_distill_pipeline` run died there from 2026-08-28 onward, the Workbuddy
candidate pool froze at 2026-08-27, and that account stopped buying:

    [ERROR] Workbuddy 候选池长期过期，买入持续停止:
            候选池已过期 4 天，买入已停止 4 天

The only outward sign was systemd exit-code 3, which nothing reads.

The timing is the nasty part. With `promoted_combination_count: 0` the registry
holds no templates, so `load_registry` falls back to the artifact templates -
and those carry no `negative_veto`. The crash therefore arrives exactly when
the fallback is the last thing standing, and disables the fallback too.

`select_candidates`, a few lines below in the same module, already declares
`veto_params: dict[str, Any] | None = None` and returns early on None. A
template without a veto was always a normal case; only this function forgot.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from workbuddy_distill.scripts.distill_local_templates import (  # noqa: E402
    NO_VETO_NAME,
    build_veto_name,
)

TAIL = {"family": "recent_tail_veto", "lookback": 20, "tail_cutoff": 30,
        "min_appearances": 2, "max_tail_pct": -3.5}
FAKE_HEAD = {"family": "fake_head_veto", "lookback": 20, "head_cutoff": 10,
             "min_prev_pct": 5.0, "fail_rank_min": 30, "fail_pct_max": -4.0,
             "min_failures": 2}


class NoVetoTests(unittest.TestCase):
    """The four-day outage, in four assertions."""

    def test_none_returns_a_name_instead_of_raising(self):
        self.assertEqual(build_veto_name(None), NO_VETO_NAME)

    def test_an_empty_veto_returns_the_same_name(self):
        self.assertEqual(build_veto_name({}), NO_VETO_NAME)

    def test_the_name_is_a_string_not_none(self):
        """It is added to a set of names that is later sorted, so returning
        None would move the crash downstream rather than remove it."""
        self.assertIsInstance(build_veto_name(None), str)
        self.assertTrue(build_veto_name(None))

    def test_it_cannot_collide_with_a_real_veto_name(self):
        for params in (TAIL, FAKE_HEAD):
            self.assertNotEqual(build_veto_name(params), NO_VETO_NAME)


class RealVetoTests(unittest.TestCase):
    """The guard must not change any name that already worked."""

    def test_the_default_family_still_builds_its_name(self):
        self.assertEqual(
            build_veto_name(TAIL),
            "tailveto_lb20_tail30_minapp2_maxpct3.5")

    def test_a_named_family_still_builds_its_name(self):
        self.assertTrue(build_veto_name(FAKE_HEAD).startswith("fakehead_lb20_"))

    def test_the_risk_warning_suffix_survives(self):
        with_flag = dict(TAIL, exclude_risk_warning=True)
        self.assertTrue(build_veto_name(with_flag).endswith("_nost"))
        self.assertFalse(build_veto_name(TAIL).endswith("_nost"))

    def test_names_are_stable_across_calls(self):
        """Names key the veto registry; an unstable one silently forks it."""
        self.assertEqual(build_veto_name(TAIL), build_veto_name(dict(TAIL)))


class CallSiteTests(unittest.TestCase):
    """Subscripting is what actually crashed; .get is the contract."""

    def test_the_pool_builder_never_subscripts_negative_veto(self):
        src = (ROOT / "build_workbuddy_distill_pool.py").read_text(encoding="utf-8")
        self.assertNotIn('["negative_veto"]', src)

    def test_every_call_site_goes_through_get(self):
        src = (ROOT / "build_workbuddy_distill_pool.py").read_text(encoding="utf-8")
        calls = [l.strip() for l in src.splitlines() if "build_veto_name(" in l
                 and not l.strip().startswith("#") and "import" not in l]
        self.assertGreaterEqual(len(calls), 3)
        for line in calls:
            self.assertIn(".get(", line, line)

    def test_the_sibling_function_documents_the_same_contract(self):
        """select_candidates already took `veto_params: dict | None = None`."""
        src = (ROOT / "workbuddy_distill" / "scripts"
               / "distill_local_templates.py").read_text(encoding="utf-8")
        self.assertIn("veto_params: dict[str, Any] | None = None", src)


if __name__ == "__main__":
    unittest.main()
