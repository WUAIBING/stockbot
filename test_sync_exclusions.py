#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generated runtime state must survive a deploy.

sync_repo_to_do.sh runs `rsync -a --delete`, so anything not excluded is
replaced by the repo copy on every deploy. Three generated directories were
already excluded - raw_top100, evaluations, artifacts - but the templates
directory was missed, and both files in it are tracked with empty contents.

Observed on 2026-08-26: a distill search that had just promoted 12 combinations
had its registry reset to 0 templates by the next sync. That silently reset the
distill chain on every deploy, on top of the 37-day pool freeze.

raw_top100 being excluded is what let the regenerated ranking history survive the
same deploy, which is the contrast that makes the omission obvious.
"""
from __future__ import annotations
import re, unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "scripts" / "github-actions" / "sync_repo_to_do.sh"
SRC = SCRIPT.read_text(encoding="utf-8")
EXCLUDES = set(re.findall(r"--exclude=(\S+)", SRC))


class ExclusionTests(unittest.TestCase):
    def test_generated_registries_are_excluded(self):
        self.assertIn("workbuddy_distill/templates/**", EXCLUDES)

    def test_the_other_generated_dirs_are_still_excluded(self):
        for path in ("workbuddy_distill/raw_top100/**",
                     "workbuddy_distill/artifacts/**",
                     "workbuddy_distill/evaluations/**",
                     "workbuddy_pool/**",
                     "workbuddy/a-share-analyst/**"):
            self.assertIn(path, EXCLUDES, path)

    def test_secrets_and_environment_stay_excluded(self):
        for path in (".git/", ".venv/", ".mx_apikey"):
            self.assertIn(path, EXCLUDES, path)

    def test_delete_is_still_on(self):
        """The exclusions only matter because --delete makes rsync destructive."""
        self.assertIn("--delete", SRC)

    def test_source_directories_are_not_excluded(self):
        """Over-excluding would stop code reaching the droplet at all."""
        for path in EXCLUDES:
            self.assertNotIn("workbuddy/skills/a-share-analyst/**", path)
            self.assertNotIn("workbuddy_distill/scripts", path)

    def test_script_still_parses(self):
        import subprocess
        r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
