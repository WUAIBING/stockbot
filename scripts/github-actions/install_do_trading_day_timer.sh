#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${1:-/opt/stockbot}"
RUN_AS_USER="${2:-stockbotrunner}"
RUN_AS_GROUP="${3:-$RUN_AS_USER}"
ENV_DIR="/etc/stockbot"
ENV_FILE="$ENV_DIR/trading-day.env"
SERVICE_FILE="/etc/systemd/system/stockbot-trading-day.service"
TIMER_FILE="/etc/systemd/system/stockbot-trading-day.timer"
LAUNCHER="$REPO_ROOT/scripts/github-actions/run_trading_day_on_do.sh"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root or via sudo."
  exit 1
fi

if [[ ! -f "$LAUNCHER" ]]; then
  echo "Launcher script not found: $LAUNCHER"
  exit 1
fi

mkdir -p "$ENV_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
  cat >"$ENV_FILE" <<EOF
# Optional overrides for the DO local trading-day timer.
# Leave MX_APIKEY empty when /opt/stockbot/.mx_apikey already exists.
MX_APIKEY=
MX_API_URL=https://mkapi2.dfcfs.com/finskillshub
TLFZ_PYTHON_EXE=$REPO_ROOT/.venv/bin/python
STOCKBOT_TRIGGER_SOURCE=do-systemd
EOF
  chmod 600 "$ENV_FILE"
fi

cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=Stockbot trading-day launcher
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_AS_USER
Group=$RUN_AS_GROUP
WorkingDirectory=$REPO_ROOT
EnvironmentFile=-$ENV_FILE
ExecStart=/usr/bin/env bash $LAUNCHER
EOF

cat >"$TIMER_FILE" <<EOF
[Unit]
Description=Run Stockbot trading-day launcher at 09:25 China time

[Timer]
OnCalendar=Mon..Fri *-*-* 01:25:00 UTC
Persistent=true
Unit=stockbot-trading-day.service

[Install]
WantedBy=timers.target
EOF

chmod 644 "$SERVICE_FILE" "$TIMER_FILE"
systemctl daemon-reload
systemctl enable --now stockbot-trading-day.timer
systemctl status stockbot-trading-day.timer --no-pager

echo
echo "Timer installed."
echo "Optional overrides: $ENV_FILE"
echo "Manual start: systemctl start stockbot-trading-day.service"
echo "Next runs: systemctl list-timers stockbot-trading-day.timer"
