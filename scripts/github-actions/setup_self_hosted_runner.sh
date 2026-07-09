#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <repo-url> <runner-token> [runner-root] [runner-name] [runner-labels]"
  exit 1
fi

REPO_URL="$1"
RUNNER_TOKEN="$2"
RUNNER_ROOT="${3:-/opt/actions-runner/stockbot-do}"
RUNNER_NAME="${4:-$(hostname)}"
RUNNER_LABELS="${5:-linux,stockbot-do}"
WORK_DIR="_work"

mkdir -p "$RUNNER_ROOT"
cd "$RUNNER_ROOT"

RUNNER_VERSION="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"].lstrip("v"))')"
RUNNER_ARCHIVE="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
RUNNER_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${RUNNER_ARCHIVE}"

if [[ ! -f ./config.sh ]]; then
  curl -fsSL "$RUNNER_URL" -o "$RUNNER_ARCHIVE"
  tar xzf "$RUNNER_ARCHIVE"
  rm -f "$RUNNER_ARCHIVE"
fi

./config.sh \
  --url "$REPO_URL" \
  --token "$RUNNER_TOKEN" \
  --name "$RUNNER_NAME" \
  --labels "$RUNNER_LABELS" \
  --work "$WORK_DIR" \
  --unattended \
  --replace

sudo ./svc.sh install
sudo ./svc.sh start
sudo ./svc.sh status || true
