#!/usr/bin/env bash
set -euo pipefail

CHECKOUT_ROOT="${1:-}"
EXPECTED_SHA="${2:-}"
EXPECTED_BRANCH="${3:-}"
EXPECTED_REMOTE_FRAGMENT="${4:-WUAIBING/stockbot}"

normalize_owner_repo() {
  local candidate="${1:-}"
  candidate="$(printf '%s' "$candidate" | tr '[:upper:]' '[:lower:]')"
  candidate="${candidate%/}"

  if [[ -z "$candidate" ]]; then
    return 1
  fi

  if [[ "$candidate" == *%2f* || "$candidate" == *%5c* || "$candidate" == *\?* || "$candidate" == *\#* ]]; then
    return 1
  fi

  case "$candidate" in
    https://github.com/*)
      candidate="${candidate#https://github.com/}"
      ;;
    ssh://git@github.com/*)
      candidate="${candidate#ssh://git@github.com/}"
      ;;
    git@github.com:*)
      candidate="${candidate#git@github.com:}"
      ;;
  esac

  candidate="${candidate%.git}"
  candidate="${candidate#/}"

  if [[ ! "$candidate" =~ ^[a-z0-9._-]+/[a-z0-9._-]+$ ]]; then
    return 1
  fi

  printf '%s\n' "$candidate"
}

normalize_github_remote() {
  local remote_url="${1:-}"
  local normalized=""
  remote_url="$(printf '%s' "$remote_url" | tr '[:upper:]' '[:lower:]')"

  if [[ -z "$remote_url" || "$remote_url" == *%2f* || "$remote_url" == *%5c* || "$remote_url" == *\\* ]]; then
    return 1
  fi

  if [[ "$remote_url" =~ ^https://github\.com/[a-z0-9._-]+/[a-z0-9._-]+(\.git)?$ ]]; then
    normalized="${remote_url#https://github.com/}"
  elif [[ "$remote_url" =~ ^ssh://git@github\.com/[a-z0-9._-]+/[a-z0-9._-]+(\.git)?$ ]]; then
    normalized="${remote_url#ssh://git@github.com/}"
  elif [[ "$remote_url" =~ ^git@github\.com:[a-z0-9._-]+/[a-z0-9._-]+(\.git)?$ ]]; then
    normalized="${remote_url#git@github.com:}"
  else
    return 1
  fi

  normalized="${normalized%.git}"
  printf '%s\n' "$normalized"
}

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
expected_owner_repo="$(normalize_owner_repo "$EXPECTED_REMOTE_FRAGMENT")" || {
  echo "[ERROR] Invalid expected remote fragment: $EXPECTED_REMOTE_FRAGMENT" >&2
  exit 1
}
normalized_remote="$(normalize_github_remote "$remote_url")" || {
  echo "[ERROR] Unexpected origin remote for deployment checkout: $remote_url" >&2
  exit 1
}
if [[ "$normalized_remote" != "$expected_owner_repo" ]]; then
  echo "[ERROR] Unexpected origin remote for deployment checkout: $remote_url" >&2
  exit 1
fi

git fetch --no-tags origin "$EXPECTED_BRANCH" --depth=1
remote_sha="$(git rev-parse FETCH_HEAD)"
if [[ "$remote_sha" != "$EXPECTED_SHA" ]]; then
  echo "[ERROR] Remote branch tip does not match GITHUB_SHA. branch=$EXPECTED_BRANCH remote_sha=$remote_sha expected=$EXPECTED_SHA" >&2
  exit 1
fi

echo "[OK] Checkout provenance verified: branch=$EXPECTED_BRANCH sha=$EXPECTED_SHA remote=$remote_url normalized_remote=$normalized_remote"
