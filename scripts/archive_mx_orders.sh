#!/usr/bin/env bash
# Snapshot the broker order window before it rolls past.
#
# The 妙想 orders endpoint keeps roughly 90 days and takes no date range, so the
# first ten weeks of this account - and about 12,090 of realised P&L - are gone
# for good. History now only survives because this runs.
#
# IT MUST FIRE BEFORE CLOSE-NODE. The episode-history correction in
# v10_moni_trader reads this archive rather than calling the API, so today's
# fills have to be in it before the 15:06 close-node phase rebuilds the day's
# episodes. The timer sits at 15:02 China time for that reason.
set -euo pipefail

KEY="${MX_APIKEY:-}"
if [ -z "${KEY}" ] && [ -r /opt/stockbot/.mx_apikey ]; then
  KEY="$(cat /opt/stockbot/.mx_apikey)"
fi
if [ -z "${KEY}" ]; then
  echo "[ERROR] no MX_APIKEY in the environment and /opt/stockbot/.mx_apikey is unreadable" >&2
  exit 1
fi

PY="${TLFZ_PYTHON_EXE:-/opt/stockbot/.venv/bin/python}"
SKILL_DIR=/opt/stockbot/workbuddy/skills/mx-moni
ARCHIVER=/opt/stockbot/workbuddy/skills/a-share-analyst/mx_moni_orders_archive.py
OUT_DIR=/opt/stockbot/workbuddy/a-share-analyst/mx_orders_archive

mkdir -p "${OUT_DIR}"
exec env MX_APIKEY="${KEY}" "${PY}" "${ARCHIVER}" \
  --skill-dir "${SKILL_DIR}" \
  --python "${PY}" \
  --out-dir "${OUT_DIR}"
