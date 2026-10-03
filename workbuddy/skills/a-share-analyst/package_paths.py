from __future__ import annotations

import atexit
import getpass
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    import pwd
except ImportError:  # pragma: no cover - Windows
    pwd = None  # type: ignore[assignment]


PACKAGE_ROOT = Path(os.environ.get("TLFZ_WORKBUDDY_ROOT", str(Path(__file__).resolve().parents[2])))
SKILLS_DIR = Path(os.environ.get("TLFZ_WORKBUDDY_SKILLS_DIR", str(PACKAGE_ROOT / "skills")))
CSI1000_SKILLS_DIR = SKILLS_DIR / "csi1000-skills"
DEFAULT_DATA_DIR = PACKAGE_ROOT / "a-share-analyst"
FALLBACK_DATA_DIR = Path.home() / ".workbuddy" / "tlfz-workbuddy-data" / "a-share-analyst"


# TESTS NEVER TOUCH LIVE DATA.
#
# On 2026-09-14 test suites were run on the droplet from a /tmp tree of
# symlinks. PACKAGE_ROOT resolves through the symlink, so DEFAULT_DATA_DIR was
# the live directory, and the tests overwrote v10_position_state.json with a
# fixture - the strategy context of 13 real positions - mid-session. A rule in
# a notes file did not prevent it; this does.
#
# Under a test runner the data directory is a fresh empty temp directory unless
# one is configured, and a configured directory that resolves into a live
# prefix is refused outright. That also makes tests hermetic: two tests that
# "failed" for weeks were only reading the server's real data files.
LIVE_DATA_PREFIXES = tuple(
    p for p in os.environ.get("TLFZ_LIVE_DATA_PREFIXES", "/opt/stockbot").split(os.pathsep) if p.strip()
)


def running_under_test_runner(argv0: str | None = None) -> bool:
    """True for unittest, pytest, or a test_*.py run directly."""
    if str(os.environ.get("TLFZ_TEST_MODE", "")).strip().lower() in ("1", "true", "yes", "on"):
        return True
    if argv0 is None:
        argv0 = sys.argv[0] if sys.argv else ""
    norm = str(argv0 or "").replace("\\", "/")
    base = norm.rsplit("/", 1)[-1]
    return (
        "-m unittest" in norm                      # unittest rewrites argv[0] to this
        or norm.endswith("unittest/__main__.py")
        or norm.endswith("pytest/__main__.py")
        or base in ("pytest", "py.test", "pytest.exe", "py.test.exe")
        or (base.startswith("test_") and base.endswith(".py"))
    )


def is_live_path(path_like: str | Path) -> bool:
    """Does this path resolve - through any symlink - into a live data prefix?"""
    real = os.path.realpath(str(path_like)).replace("\\", "/")
    for prefix in LIVE_DATA_PREFIXES:
        p = os.path.realpath(prefix).replace("\\", "/").rstrip("/")
        if real == p or real.startswith(p + "/"):
            return True
    return False


def _pick_data_dir() -> Path:
    configured = os.environ.get("TLFZ_WORKBUDDY_DATA_DIR", "").strip()
    if running_under_test_runner():
        if configured:
            if is_live_path(configured):
                raise RuntimeError(
                    "Refusing to run tests against live data: TLFZ_WORKBUDDY_DATA_DIR=%s resolves into %s. "
                    "Point it at a copy." % (configured, os.path.realpath(configured)))
        else:
            scratch = Path(tempfile.mkdtemp(prefix="stockbot-test-data-"))
            atexit.register(shutil.rmtree, str(scratch), True)
            return scratch
    candidates = [Path(configured)] if configured else [DEFAULT_DATA_DIR, FALLBACK_DATA_DIR]
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            if candidate.exists() and candidate.is_dir():
                return candidate
        except OSError:
            continue
    raise RuntimeError("No writable data directory available for TLFZ workbuddy package.")


def _safe_owner_name(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return ""
    if pwd is None:
        return ""
    try:
        return str(pwd.getpwuid(stat.st_uid).pw_name).strip()
    except KeyError:
        return str(stat.st_uid)


def _current_user_name() -> str:
    if os.name == "posix" and pwd is not None:
        try:
            return str(pwd.getpwuid(os.geteuid()).pw_name).strip()
        except KeyError:
            return str(os.geteuid())
    return getpass.getuser().strip()


def _coerce_path(path_like: str | Path | None) -> Path:
    if path_like is None:
        return DATA_DIR
    return path_like if isinstance(path_like, Path) else Path(path_like)


def assert_runtime_write_identity(path_like: str | Path | None = None) -> None:
    path = _coerce_path(path_like)
    if os.name != "posix":
        return
    current_user = _current_user_name()
    if not current_user:
        return
    expected_owner = _safe_owner_name(path)
    if not expected_owner and path.parent != path:
        expected_owner = _safe_owner_name(path.parent)
    explicit_owner = str(os.environ.get("TLFZ_RUNTIME_OWNER", "")).strip()
    if explicit_owner:
        expected_owner = explicit_owner
    if not expected_owner or current_user == expected_owner:
        return
    raise RuntimeError(
        "Refusing to write runtime artifacts with mismatched user identity: "
        f"current_user={current_user}, expected_owner={expected_owner}, path={path}"
    )


DATA_DIR = _pick_data_dir()
