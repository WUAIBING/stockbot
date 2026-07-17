#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Resolve MX runtime env from stable fallback locations.

This keeps DO/manual recovery paths aligned with the normal service runtime:
- Prefer already exported env vars.
- Fallback to repo-level `.mx_apikey`.
- Fallback to env files that may contain `MX_APIKEY=...`.
"""

from __future__ import annotations

import os
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]


def _candidate_files() -> list[Path]:
    paths = [
        REPO_ROOT / ".mx_apikey",
        Path.cwd() / ".mx_apikey",
        REPO_ROOT / ".env",
        Path.cwd() / ".env",
        Path("/etc/stockbot/trading-day.env"),
    ]
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _strip_wrapped_quotes(text: str) -> str:
    value = str(text or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def _extract_from_env_line(line: str, key: str) -> str:
    text = str(line or "").strip()
    if not text or text.startswith("#"):
        return ""
    if text.startswith("export "):
        text = text[len("export ") :].strip()
    prefix = f"{key}="
    if not text.startswith(prefix):
        return ""
    return _strip_wrapped_quotes(text[len(prefix) :])


def _read_candidate_value(path: Path, key: str) -> str:
    try:
        if not path.exists() or not path.is_file():
            return ""
    except Exception:
        return ""
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

    if path.name == ".mx_apikey":
        first_line = next((line.strip() for line in raw.splitlines() if line.strip()), "")
        if not first_line:
            return ""
        if first_line.startswith(f"{key}=") or first_line.startswith(f"export {key}="):
            return _extract_from_env_line(first_line, key)
        if key == "MX_APIKEY":
            return _strip_wrapped_quotes(first_line)
        return ""

    for line in raw.splitlines():
        value = _extract_from_env_line(line, key)
        if value:
            return value
    return ""


def ensure_env(key: str, *, overwrite_empty: bool = False) -> str:
    current = str(os.environ.get(key, "")).strip()
    if current:
        return current
    for path in _candidate_files():
        value = _read_candidate_value(path, key)
        if value:
            os.environ[key] = value
            return value
    return ""


def ensure_mx_runtime_env() -> dict[str, str]:
    resolved = {
        "MX_APIKEY": ensure_env("MX_APIKEY"),
        "MX_API_URL": ensure_env("MX_API_URL"),
        "HOLDINGS_API_URL": ensure_env("HOLDINGS_API_URL"),
    }
    return resolved
