#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${1:-}"
PYTHON_CANDIDATE="${2:-}"

if [[ -z "$REPO_ROOT" ]]; then
  echo "[ERROR] REPO_ROOT is required" >&2
  exit 1
fi

declare -a candidate_specs=()
if [[ -n "$PYTHON_CANDIDATE" ]]; then
  candidate_specs+=("$PYTHON_CANDIDATE")
fi
candidate_specs+=(
  "$REPO_ROOT/.venv/bin/python"
  "python3"
  "python"
)

declare -a attempted_candidates=()
declare -a failure_reasons=()

for candidate_spec in "${candidate_specs[@]}"; do
  [[ -n "$candidate_spec" ]] || continue

  resolved_candidate=""
  if [[ "$candidate_spec" == */* || "$candidate_spec" == *\\* ]]; then
    if [[ -x "$candidate_spec" ]]; then
      resolved_candidate="$candidate_spec"
    else
      attempted_candidates+=("$candidate_spec")
      failure_reasons+=("[missing] $candidate_spec")
      continue
    fi
  else
    if command -v "$candidate_spec" >/dev/null 2>&1; then
      resolved_candidate="$(command -v "$candidate_spec")"
    else
      attempted_candidates+=("$candidate_spec")
      failure_reasons+=("[missing] $candidate_spec")
      continue
    fi
  fi

  if [[ " ${attempted_candidates[*]} " == *" $resolved_candidate "* ]]; then
    continue
  fi
  attempted_candidates+=("$resolved_candidate")

  if resolved_path="$("$resolved_candidate" - <<'PY'
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
  )"; then
    printf '%s\n' "$resolved_path"
    exit 0
  fi

  failure_reasons+=("[invalid] $resolved_candidate")
done

echo "[ERROR] No usable Python runtime found for stockbot workflows." >&2
if [[ ${#attempted_candidates[@]} -gt 0 ]]; then
  echo "[ERROR] Attempted candidates:" >&2
  for candidate in "${attempted_candidates[@]}"; do
    echo "  - $candidate" >&2
  done
fi
if [[ ${#failure_reasons[@]} -gt 0 ]]; then
  echo "[ERROR] Candidate failures:" >&2
  for reason in "${failure_reasons[@]}"; do
    echo "  - $reason" >&2
  done
fi
exit 1
