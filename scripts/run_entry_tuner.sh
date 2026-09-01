#!/usr/bin/env bash
# Re-estimate which entries pay, once per trading day.
#
# ORDER MATTERS. It reads two files that are only complete late in the day:
# the broker order archive (written 15:02, the ground truth for fills) and the
# decision log (written by close-node at 15:06, the ground truth for why an
# entry was made). Running before either would estimate today from yesterday's
# evidence and file it under today's date, which is worse than not running -
# the whole point of a daily series is that each line means what it says.
#
# It appends one snapshot line. It places no orders.
set -euo pipefail

PY="${TLFZ_PYTHON_EXE:-/opt/stockbot/.venv/bin/python}"
RUNNER=/opt/stockbot/workbuddy/skills/a-share-analyst/run_entry_tuner.py

exec "${PY}" "${RUNNER}"
