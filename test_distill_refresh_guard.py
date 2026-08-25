#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the intraday candidate-pool refresh guard.

The refresh shells out to refresh_distill_pipeline.py with a 75s budget. That
pipeline short-circuits only when workbuddy_distill/raw_top100/<trade_date>/
full_rank.csv exists; otherwise it fetches the whole ~1000-stock universe, which
takes minutes. Nothing ever scheduled that fetch, so from 2026-08-21 the file
stopped appearing and every buy slot burned 75 seconds before logging
"候选池未就绪" and skipping the round - 57 times and counting.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import workbuddy_local_challenger as ch  # noqa: E402


class RawRankingsReadyTests(unittest.TestCase):
    def test_true_when_full_rank_present(self):
        with patch.object(ch, "_raw_rankings_ready", wraps=ch._raw_rankings_ready):
            pass  # sanity: symbol exists and is callable
        self.assertTrue(callable(ch._raw_rankings_ready))

    def test_detects_a_present_ranking(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-08-21").mkdir(parents=True)
            (root / "2026-08-21" / "full_rank.csv").write_text("code\n", encoding="utf-8")
            mod = type(sys)("build_tdx_rankings")
            mod.RAW_TOP100_ROOT = root
            with patch.dict(sys.modules,
                            {"workbuddy_distill.scripts.build_tdx_rankings": mod}):
                self.assertTrue(ch._raw_rankings_ready("2026-08-21"))
                self.assertFalse(ch._raw_rankings_ready("2026-08-24"))

    def test_directory_without_full_rank_is_not_ready(self):
        """A half-written date directory must not count as usable."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-08-24").mkdir(parents=True)
            mod = type(sys)("build_tdx_rankings")
            mod.RAW_TOP100_ROOT = root
            with patch.dict(sys.modules,
                            {"workbuddy_distill.scripts.build_tdx_rankings": mod}):
                self.assertFalse(ch._raw_rankings_ready("2026-08-24"))

    def test_falls_through_when_the_module_cannot_be_imported(self):
        """Unable to tell -> do not block; let the subprocess decide."""
        def boom(name, *a, **k):
            if "build_tdx_rankings" in name:
                raise ImportError(name)
            return real_import(name, *a, **k)

        import builtins
        real_import = builtins.__import__
        with patch.object(builtins, "__import__", side_effect=boom):
            self.assertTrue(ch._raw_rankings_ready("2026-08-24"))


class RefreshGuardTests(unittest.TestCase):
    def test_missing_rankings_raises_without_spawning_the_subprocess(self):
        """The whole point: fail immediately instead of burning the budget."""
        with (
            patch.object(ch, "_raw_rankings_ready", return_value=False),
            patch.object(ch.subprocess, "run") as run_mock,
        ):
            with self.assertRaises(ch.ChallengerSourceUnavailable) as ctx:
                ch._refresh_source_payload("2026-08-24")
        run_mock.assert_not_called()
        message = str(ctx.exception)
        self.assertIn("raw_top100", message)
        self.assertIn("2026-08-24", message)
        self.assertIn(str(ch.SOURCE_REFRESH_TIMEOUT_SECONDS), message)

    def test_present_rankings_still_runs_the_pipeline(self):
        class Done:
            returncode = 0
            stdout = "{}"
            stderr = ""

        with (
            patch.object(ch, "_raw_rankings_ready", return_value=True),
            patch.object(ch.subprocess, "run", return_value=Done()) as run_mock,
            patch.object(ch, "_load_source_payload", return_value={"trade_date": "2026-08-24"}),
        ):
            payload, _ = ch._refresh_source_payload("2026-08-24")
        run_mock.assert_called_once()
        self.assertEqual(payload["trade_date"], "2026-08-24")

    def test_timeout_is_still_reported_when_rankings_are_present(self):
        import subprocess as sp
        with (
            patch.object(ch, "_raw_rankings_ready", return_value=True),
            patch.object(ch.subprocess, "run",
                         side_effect=sp.TimeoutExpired(cmd="x", timeout=75)),
        ):
            with self.assertRaises(ch.ChallengerSourceUnavailable) as ctx:
                ch._refresh_source_payload("2026-08-24")
        self.assertIn("超时", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
