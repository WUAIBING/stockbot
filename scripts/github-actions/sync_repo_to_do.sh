#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="${1:-${GITHUB_WORKSPACE:-}}"
TARGET_ROOT="${2:-/opt/stockbot}"
DRY_RUN="${SYNC_DRY_RUN:-0}"

if [[ -z "$SOURCE_ROOT" ]]; then
  echo "SOURCE_ROOT is required"
  exit 1
fi

if [[ ! -d "$SOURCE_ROOT" ]]; then
  echo "Source root does not exist: $SOURCE_ROOT"
  exit 1
fi

if [[ ! -d "$TARGET_ROOT" ]]; then
  mkdir -p "$TARGET_ROOT"
fi

RSYNC_ARGS=(
  -a
  --delete
  --itemize-changes
  --human-readable
  --exclude=.git/
  --exclude=.venv/
  --exclude=.mx_apikey
  --exclude=__pycache__/
  --exclude=.pytest_cache/
  --exclude=workbuddy/a-share-analyst/**
  --exclude=workbuddy/skills/a-share-analyst/task_wrappers/**
  --exclude=workbuddy_pool/**
  --exclude=workbuddy_distill/raw_top100/**
  --exclude=workbuddy_distill/evaluations/**
  --exclude=workbuddy_distill/artifacts/**
)

if [[ "$DRY_RUN" == "1" ]]; then
  RSYNC_ARGS+=(--dry-run)
fi

echo "Syncing repository checkout to DO runtime repo"
echo "Source: $SOURCE_ROOT"
echo "Target: $TARGET_ROOT"

rsync "${RSYNC_ARGS[@]}" "$SOURCE_ROOT"/ "$TARGET_ROOT"/

echo "Sync complete"
