#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${1:-}"
PYTHON_CANDIDATE="${2:-}"

if [[ -z "$REPO_ROOT" ]]; then
  echo "[ERROR] REPO_ROOT is required" >&2
  exit 1
fi

if [[ -z "$PYTHON_CANDIDATE" ]]; then
  PYTHON_CANDIDATE="$REPO_ROOT/.venv/bin/python"
fi

if [[ ! -x "$PYTHON_CANDIDATE" ]]; then
  echo "[ERROR] Python executable not found or not executable: $PYTHON_CANDIDATE" >&2
  exit 1
fi

"$PYTHON_CANDIDATE" - <<'PY'
import importlib
import sys

required_modules = ("numpy", "pandas", "pytdx", "xlrd", "openpyxl")
missing = []
for module_name in required_modules:
    try:
        importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001
        missing.append(f"{module_name}: {exc}")

if missing:
    print("[ERROR] Python runtime is missing required modules:", file=sys.stderr)
    for item in missing:
        print(f"  - {item}", file=sys.stderr)
    sys.exit(1)

print(sys.executable)
PY
