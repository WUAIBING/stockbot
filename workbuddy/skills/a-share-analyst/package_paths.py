from __future__ import annotations

import getpass
import os
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


def _pick_data_dir() -> Path:
    configured = os.environ.get("TLFZ_WORKBUDDY_DATA_DIR", "").strip()
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
