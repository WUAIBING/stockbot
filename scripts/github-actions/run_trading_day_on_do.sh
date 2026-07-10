#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${TLFZ_ARKCLAW_ROOT:-/opt/stockbot}"
WORKBUDDY_ROOT="${TLFZ_WORKBUDDY_ROOT:-$REPO_ROOT/workbuddy}"
SKILL_ROOT="${TLFZ_WORKBUDDY_SKILL_ROOT:-$WORKBUDDY_ROOT/skills/a-share-analyst}"
DATA_DIR="${TLFZ_WORKBUDDY_DATA_DIR:-$REPO_ROOT/workbuddy/a-share-analyst}"
POOL_DIR="${TLFZ_WORKBUDDY_POOL_DIR:-$REPO_ROOT/workbuddy_pool}"
PYTHON_EXE="${TLFZ_PYTHON_EXE:-$REPO_ROOT/.venv/bin/python}"
LOCK_FILE="${TLFZ_TRADING_DAY_LOCK_FILE:-/tmp/stockbot-trading-day.lock}"
SECRET_FILE="${STOCKBOT_MX_APIKEY_FILE:-$REPO_ROOT/.mx_apikey}"
TRIGGER_SOURCE="${STOCKBOT_TRIGGER_SOURCE:-do-systemd}"

if [[ -z "${MX_APIKEY:-}" && -s "$SECRET_FILE" ]]; then
  secret_line="$(head -n 1 "$SECRET_FILE" | tr -d '\r\n')"
  if [[ "$secret_line" == MX_APIKEY=* ]]; then
    export MX_APIKEY="${secret_line#MX_APIKEY=}"
  else
    export MX_APIKEY="$secret_line"
  fi
fi

export TLFZ_ARKCLAW_ROOT="$REPO_ROOT"
export TLFZ_WORKBUDDY_ROOT="$WORKBUDDY_ROOT"
export TLFZ_WORKBUDDY_SKILL_ROOT="$SKILL_ROOT"
export TLFZ_WORKBUDDY_DATA_DIR="$DATA_DIR"
export TLFZ_WORKBUDDY_POOL_DIR="$POOL_DIR"
export TLFZ_PYTHON_EXE="$PYTHON_EXE"
export MX_API_URL="${MX_API_URL:-https://mkapi2.dfcfs.com/finskillshub}"

mkdir -p "$DATA_DIR" "$POOL_DIR"

if [[ ! -x "$PYTHON_EXE" ]]; then
  echo "[ERROR] Python executable not found or not executable: $PYTHON_EXE"
  exit 1
fi

if [[ -z "${MX_APIKEY:-}" ]]; then
  echo "[ERROR] MX_APIKEY is not configured. Set it in the environment or store it in $SECRET_FILE"
  exit 1
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[SKIP] trading-day controller already running. lock=$LOCK_FILE trigger_source=$TRIGGER_SOURCE"
  exit 0
fi

echo "[START] trigger_source=$TRIGGER_SOURCE repo_root=$REPO_ROOT python=$PYTHON_EXE"
cd "$SKILL_ROOT"
"$PYTHON_EXE" "$SKILL_ROOT/github_actions_trade_day.py" "$@"
