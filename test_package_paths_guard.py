#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests can never write to live data, and are sealed by default.

On 2026-09-14 suites run from a /tmp tree of symlinks wrote into the live data
directory mid-session (package_paths resolved through the symlink) and replaced
13 positions' strategy context with a fixture. Separately, two tests "failed"
for weeks only because they read the server's real data. Both are fixed by one
rule: under a test runner the data directory is a fresh temp dir unless set,
and a set directory that resolves into a live prefix is refused.

Each case runs in a subprocess, because DATA_DIR is chosen at import time.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent / "workbuddy" / "skills" / "a-share-analyst"
sys.path.insert(0, str(SKILL))

import package_paths  # noqa: E402

PROBE = r"""
import json, sys
sys.argv[0] = %(argv0)r
sys.path.insert(0, %(skill)r)
import package_paths as p
out = {"data_dir": str(p.DATA_DIR), "default": str(p.DEFAULT_DATA_DIR)}
if %(with_tdx)r:
    import tdx_hosts
    out["cache"] = str(tdx_hosts.CACHE_FILE)
print(json.dumps(out))
"""


def probe(argv0, env=None, with_tdx=False):
    full_env = {k: v for k, v in os.environ.items()
                if k not in ("TLFZ_WORKBUDDY_DATA_DIR", "TLFZ_TEST_MODE",
                             "TLFZ_LIVE_DATA_PREFIXES", "TDX_HOST_CACHE_FILE")}
    full_env.update(env or {})
    code = PROBE % {"argv0": argv0, "skill": str(SKILL), "with_tdx": with_tdx}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=full_env)


def parsed(result):
    assert result.returncode == 0, result.stderr[-800:]
    return json.loads(result.stdout.strip().splitlines()[-1])


class DetectionTests(unittest.TestCase):
    def test_runners_are_recognised(self):
        for argv0 in ("/opt/stockbot/.venv/bin/python -m unittest",
                      "/usr/lib/python3.10/unittest/__main__.py",
                      "/opt/stockbot/.venv/lib/python3.10/site-packages/pytest/__main__.py",
                      "/opt/stockbot/.venv/bin/pytest",
                      "C:\\Python313\\Scripts\\pytest.exe",
                      "/tmp/x/test_core_trading_smoke.py"):
            self.assertTrue(package_paths.running_under_test_runner(argv0), argv0)

    def test_production_entry_points_are_not(self):
        for argv0 in ("v10_moni_trader.py", "/opt/stockbot/workbuddy/skills/a-share-analyst/scanner_v10.py",
                      "fish_monitor.py", "-c", ""):
            self.assertFalse(package_paths.running_under_test_runner(argv0), argv0)

    def test_this_very_run_is_sealed(self):
        """The suite running these tests got a temp dir, not the real one."""
        if os.environ.get("TLFZ_WORKBUDDY_DATA_DIR"):
            self.skipTest("data dir set explicitly for this run")
        self.assertTrue(str(package_paths.DATA_DIR).startswith(tempfile.gettempdir()))
        self.assertNotEqual(package_paths.DATA_DIR, package_paths.DEFAULT_DATA_DIR)


class SealingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "workbuddy"
        self.live = self.tmp / "live"
        (self.live / "a-share-analyst").mkdir(parents=True)

    def env(self, **extra):
        e = {"TLFZ_WORKBUDDY_ROOT": str(self.root), "TLFZ_LIVE_DATA_PREFIXES": str(self.live)}
        e.update(extra)
        return e

    def test_a_test_run_gets_a_fresh_temp_dir(self):
        out = parsed(probe("python -m unittest", self.env()))
        self.assertIn("stockbot-test-data-", out["data_dir"])
        self.assertNotEqual(out["data_dir"], out["default"])

    def test_the_temp_dir_is_removed_at_exit(self):
        out = parsed(probe("python -m unittest", self.env()))
        self.assertFalse(Path(out["data_dir"]).exists())

    def test_production_still_uses_the_default_dir(self):
        out = parsed(probe("v10_moni_trader.py", self.env()))
        self.assertEqual(out["data_dir"], out["default"])

    def test_a_configured_copy_is_honoured(self):
        copy = self.tmp / "copy"
        out = parsed(probe("python -m unittest", self.env(TLFZ_WORKBUDDY_DATA_DIR=str(copy))))
        self.assertEqual(out["data_dir"], str(copy))

    def test_a_configured_live_dir_is_refused(self):
        r = probe("python -m unittest",
                  self.env(TLFZ_WORKBUDDY_DATA_DIR=str(self.live / "a-share-analyst")))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Refusing to run tests against live data", r.stderr)

    def test_a_symlink_into_live_is_refused(self):
        """The 09-14 shape: a /tmp path that is really the live directory."""
        link = self.tmp / "looks-like-tmp"
        try:
            os.symlink(self.live / "a-share-analyst", link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not permitted here")
        r = probe("python -m unittest", self.env(TLFZ_WORKBUDDY_DATA_DIR=str(link)))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Refusing to run tests against live data", r.stderr)

    def test_production_can_still_use_live(self):
        """The guard binds tests only; the trader must keep writing live data."""
        out = parsed(probe("v10_moni_trader.py",
                           self.env(TLFZ_WORKBUDDY_DATA_DIR=str(self.live / "a-share-analyst"))))
        self.assertEqual(out["data_dir"], str(self.live / "a-share-analyst"))

    def test_the_tdx_host_cache_follows_the_sealed_dir(self):
        """tdx_hosts used to build its cache path itself, bypassing the guard."""
        out = parsed(probe("python -m unittest", self.env(), with_tdx=True))
        self.assertTrue(out["cache"].startswith(out["data_dir"]))


if __name__ == "__main__":
    unittest.main()
