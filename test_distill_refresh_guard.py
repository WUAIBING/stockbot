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


class RawTop100RootTests(unittest.TestCase):
    """The location must resolve without importing workbuddy_distill.

    The first version of this guard did `from workbuddy_distill.scripts...
    import RAW_TOP100_ROOT`. The challenger runs with the skill directory as
    cwd, so that raises ModuleNotFoundError in production - the guard fell
    through, returned True, and the 75s timeout stayed exactly as it was. The
    original tests missed it because they patched sys.modules, so the real
    import was never exercised. These tests do not mock the import at all.
    """

    def test_resolves_without_importing_workbuddy_distill(self):
        import builtins
        real_import = builtins.__import__

        def refuse(name, *a, **k):
            if name.startswith("workbuddy_distill"):
                raise ModuleNotFoundError(name)
            return real_import(name, *a, **k)

        with patch.object(builtins, "__import__", side_effect=refuse):
            root = ch._raw_top100_root()
        self.assertIsNotNone(root, "must resolve even when the import fails")
        self.assertEqual(root.name, "raw_top100")
        self.assertEqual(root.parent.name, "workbuddy_distill")

    def test_root_is_anchored_under_arkclaw_root(self):
        root = ch._raw_top100_root()
        self.assertIsNotNone(root)
        self.assertEqual(root, Path(ch.ARKCLAW_ROOT) / "workbuddy_distill" / "raw_top100")

    def test_returns_none_when_the_layout_is_unexpected(self):
        with patch.object(ch, "ARKCLAW_ROOT", Path("/nonexistent-xyz")):
            self.assertIsNone(ch._raw_top100_root())


class RawRankingsReadyTests(unittest.TestCase):
    def test_detects_present_and_absent_dates(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workbuddy_distill" / "raw_top100" / "2026-08-21").mkdir(parents=True)
            (root / "workbuddy_distill" / "raw_top100" / "2026-08-21"
             / "full_rank.csv").write_text("code\n", encoding="utf-8")
            with patch.object(ch, "ARKCLAW_ROOT", root):
                self.assertTrue(ch._raw_rankings_ready("2026-08-21"))
                self.assertFalse(ch._raw_rankings_ready("2026-08-24"))

    def test_directory_without_full_rank_is_not_ready(self):
        """A half-written date directory must not count as usable."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workbuddy_distill" / "raw_top100" / "2026-08-24").mkdir(parents=True)
            with patch.object(ch, "ARKCLAW_ROOT", root):
                self.assertFalse(ch._raw_rankings_ready("2026-08-24"))

    def test_unresolvable_layout_falls_through_rather_than_blocking(self):
        with patch.object(ch, "ARKCLAW_ROOT", Path("/nonexistent-xyz")):
            self.assertTrue(ch._raw_rankings_ready("2026-08-24"))

    def test_against_the_real_repo_layout(self):
        """End-to-end: no patching at all, real ARKCLAW_ROOT on disk."""
        root = ch._raw_top100_root()
        if root is None or not root.is_dir():
            self.skipTest("raw_top100 not present in this checkout")
        present = sorted(p.name for p in root.iterdir()
                         if (p / "full_rank.csv").exists())
        for date in present:
            self.assertTrue(ch._raw_rankings_ready(date), date)
        self.assertFalse(ch._raw_rankings_ready("1999-01-01"))


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
