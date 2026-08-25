#!/usr/bin/env bash
# Build the candidate pool for the session that just closed.
#
# refresh_distill_pipeline.py needs workbuddy_distill/raw_top100/<trade_date>/
# to exist; when it does not, it fetches the whole ~1000-stock universe, which
# takes minutes. Nothing ever scheduled that fetch - build_rankings() is only
# reachable from this pipeline and from build_tdx_rankings.py's own __main__ -
# so raw_top100 was only ever produced by someone running the script by hand.
# It stopped on 2026-08-21, and the intraday fallback in workbuddy_local_
# challenger.py has a 75s budget it can never meet, so every buy slot since has
# logged "候选池未就绪" and skipped.
#
# Running after the close is where a multi-minute universe fetch belongs. By the
# next morning the pool is fresh and the intraday path short-circuits on
# already_exists instead of timing out.
set -euo pipefail

REPO_ROOT="${TLFZ_ARKCLAW_ROOT:-/opt/stockbot}"
PYTHON_EXE="${TLFZ_PYTHON_EXE:-$REPO_ROOT/.venv/bin/python}"
DATA_DIR="${TLFZ_WORKBUDDY_DATA_DIR:-$REPO_ROOT/workbuddy/a-share-analyst}"
POOL_DIR="${TLFZ_WORKBUDDY_POOL_DIR:-$REPO_ROOT/workbuddy_pool}"
STATUS_DIR="${TLFZ_TRADING_DAY_STATUS_DIR:-$DATA_DIR/automation_status}"
LOCK_FILE="${TLFZ_DISTILL_REFRESH_LOCK_FILE:-$STATUS_DIR/stockbot-distill-refresh.lock}"

export TLFZ_ARKCLAW_ROOT="$REPO_ROOT"
export TLFZ_WORKBUDDY_ROOT="${TLFZ_WORKBUDDY_ROOT:-$REPO_ROOT/workbuddy}"
export TLFZ_WORKBUDDY_SKILL_ROOT="${TLFZ_WORKBUDDY_SKILL_ROOT:-$REPO_ROOT/workbuddy/skills/a-share-analyst}"
export TLFZ_WORKBUDDY_DATA_DIR="$DATA_DIR"
export TLFZ_WORKBUDDY_POOL_DIR="$POOL_DIR"
export TLFZ_PYTHON_EXE="$PYTHON_EXE"

mkdir -p "$DATA_DIR" "$POOL_DIR" "$STATUS_DIR"

if [[ ! -x "$PYTHON_EXE" ]]; then
  echo "[ERROR] Python executable not found or not executable: $PYTHON_EXE"
  exit 1
fi

# Never run two universe fetches at once, and never overlap the trading day.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[SKIP] distill refresh already running. lock=$LOCK_FILE"
  exit 0
fi

echo "[START] distill refresh repo_root=$REPO_ROOT python=$PYTHON_EXE"
cd "$REPO_ROOT"
"$PYTHON_EXE" "$REPO_ROOT/refresh_distill_pipeline.py" "$@"
echo "[DONE] distill refresh"
