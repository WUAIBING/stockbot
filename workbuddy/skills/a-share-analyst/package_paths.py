from __future__ import annotations

import os
from pathlib import Path


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


DATA_DIR = _pick_data_dir()
