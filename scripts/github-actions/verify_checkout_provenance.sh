#!/usr/bin/env bash
set -euo pipefail

CHECKOUT_ROOT="${1:-}"
EXPECTED_SHA="${2:-}"
EXPECTED_BRANCH="${3:-}"
EXPECTED_REMOTE_FRAGMENT="${4:-WUAIBING/stockbot}"

if [[ -z "$CHECKOUT_ROOT" || -z "$EXPECTED_SHA" || -z "$EXPECTED_BRANCH" ]]; then
  echo "[ERROR] Usage: verify_checkout_provenance.sh <checkout_root> <expected_sha> <expected_branch> [expected_remote_fragment]" >&2
  exit 1
fi

if [[ ! -d "$CHECKOUT_ROOT/.git" ]]; then
  echo "[ERROR] Checkout root is not a git repository: $CHECKOUT_ROOT" >&2
  exit 1
fi

cd "$CHECKOUT_ROOT"

head_sha="$(git rev-parse HEAD)"
if [[ "$head_sha" != "$EXPECTED_SHA" ]]; then
  echo "[ERROR] Checkout HEAD does not match GITHUB_SHA. head=$head_sha expected=$EXPECTED_SHA" >&2
  exit 1
fi

remote_url="$(git remote get-url origin)"
if [[ "$remote_url" != *"$EXPECTED_REMOTE_FRAGMENT"* ]]; then
  echo "[ERROR] Unexpected origin remote for deployment checkout: $remote_url" >&2
  exit 1
fi

git fetch --no-tags origin "$EXPECTED_BRANCH" --depth=1
remote_sha="$(git rev-parse FETCH_HEAD)"
if [[ "$remote_sha" != "$EXPECTED_SHA" ]]; then
  echo "[ERROR] Remote branch tip does not match GITHUB_SHA. branch=$EXPECTED_BRANCH remote_sha=$remote_sha expected=$EXPECTED_SHA" >&2
  exit 1
fi

echo "[OK] Checkout provenance verified: branch=$EXPECTED_BRANCH sha=$EXPECTED_SHA remote=$remote_url"
