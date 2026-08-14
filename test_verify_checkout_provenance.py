import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "github-actions" / "verify_checkout_provenance.sh"
GIT_EXE = shutil.which("git")


def find_bash():
    candidates = [
        shutil.which("bash"),
        shutil.which("sh"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


BASH_EXE = find_bash()


@unittest.skipUnless(GIT_EXE, "git is required")
@unittest.skipUnless(BASH_EXE, "bash is required")
class VerifyCheckoutProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.remote_repo = self.workspace / "origin.git"
        self.checkout_repo = self.workspace / "checkout"
        self.real_git = GIT_EXE

        self._run_git(["init", "--bare", str(self.remote_repo)], cwd=self.workspace)
        self._run_git(["init", str(self.checkout_repo)], cwd=self.workspace)
        self._run_git(["config", "user.name", "Trae Test"], cwd=self.checkout_repo)
        self._run_git(["config", "user.email", "trae@example.com"], cwd=self.checkout_repo)
        (self.checkout_repo / "README.md").write_text("hello\n", encoding="utf-8")
        self._run_git(["add", "README.md"], cwd=self.checkout_repo)
        self._run_git(["commit", "-m", "initial"], cwd=self.checkout_repo)
        self._run_git(["branch", "-M", "main"], cwd=self.checkout_repo)
        self._run_git(["remote", "add", "origin", str(self.remote_repo)], cwd=self.checkout_repo)
        self._run_git(["push", "-u", "origin", "main"], cwd=self.checkout_repo)

        self.head_sha = self._run_git(["rev-parse", "HEAD"], cwd=self.checkout_repo).stdout.strip()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run_git(self, args, cwd):
        return subprocess.run(
            [self.real_git, *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    def _run_script(self, fake_origin_url, expected_remote="WUAIBING/stockbot", enable_fetch_rewrite=False):
        self._run_git(["remote", "set-url", "origin", fake_origin_url], cwd=self.checkout_repo)
        env = os.environ.copy()
        if enable_fetch_rewrite:
            wrapper_dir = self.workspace / "git-wrapper"
            wrapper_dir.mkdir()
            wrapper = wrapper_dir / "git"
            wrapper.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "if [[ \"${1:-}\" == \"fetch\" ]]; then\n"
                "  shift\n"
                "  if [[ \"${1:-}\" == \"--no-tags\" ]]; then shift; fi\n"
                "  if [[ \"${1:-}\" == \"origin\" ]]; then shift; fi\n"
                "  exec \"$REAL_GIT\" fetch --no-tags \"$PROVENANCE_FETCH_REMOTE\" \"$@\"\n"
                "fi\n"
                "exec \"$REAL_GIT\" \"$@\"\n",
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            env["PATH"] = str(wrapper_dir) + os.pathsep + env["PATH"]
            env["REAL_GIT"] = self.real_git
            env["PROVENANCE_FETCH_REMOTE"] = str(self.remote_repo)

        return subprocess.run(
            [BASH_EXE, str(SCRIPT_PATH), str(self.checkout_repo), self.head_sha, "main", expected_remote],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

    def test_accepts_exact_https_github_remote(self):
        result = self._run_script("https://github.com/WUAIBING/stockbot.git", enable_fetch_rewrite=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("normalized_remote=wuaibing/stockbot", result.stdout)

    def test_rejects_spoofed_host_with_embedded_github_path(self):
        result = self._run_script("https://evil.com/github.com/WUAIBING/stockbot.git")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unexpected origin remote", result.stderr)

    def test_rejects_extra_path_segments(self):
        result = self._run_script("git@github.com:WUAIBING/stockbot/extra.git")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unexpected origin remote", result.stderr)

    def test_rejects_encoded_separator_attack(self):
        result = self._run_script("https://github.com/WUAIBING%2Fstockbot.git")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unexpected origin remote", result.stderr)


if __name__ == "__main__":
    unittest.main()
