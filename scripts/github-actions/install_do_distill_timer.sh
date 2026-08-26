#!/usr/bin/env bash
# Install the post-close timer that builds the candidate pool.
#
# Mirrors install_do_trading_day_timer.sh. Runs at 07:30 UTC = 15:30 China time,
# after WORKBUDDY_SOURCE_READY_TIME (15:20) so the just-closed session is the one
# built, and after the trading-day controller finishes its 15:06 close-node.
set -euo pipefail

REPO_ROOT="${1:-/opt/stockbot}"
RUN_AS_USER="${2:-stockbotrunner}"
RUN_AS_GROUP="${3:-$RUN_AS_USER}"
ENV_DIR="/etc/stockbot"
ENV_FILE="$ENV_DIR/trading-day.env"
SERVICE_FILE="/etc/systemd/system/stockbot-distill-refresh.service"
TIMER_FILE="/etc/systemd/system/stockbot-distill-refresh.timer"
LAUNCHER="$REPO_ROOT/scripts/github-actions/run_distill_refresh_on_do.sh"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root or via sudo."
  exit 1
fi

if [[ ! -f "$LAUNCHER" ]]; then
  echo "Launcher script not found: $LAUNCHER"
  exit 1
fi

mkdir -p "$ENV_DIR"

cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=Stockbot distill/candidate-pool refresh
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_AS_USER
Group=$RUN_AS_GROUP
WorkingDirectory=$REPO_ROOT
EnvironmentFile=-$ENV_FILE
# trading_calendar.latest_workbuddy_source_trade_date compares a NAIVE
# datetime.now() against WORKBUDDY_SOURCE_READY_TIME (15:20 China time). The
# droplet runs in UTC, so at this timer's 07:30 UTC fire moment the comparison
# saw 07:30 < 15:20 and resolved to the PREVIOUS trading day - which already
# existed, so ensure_trade_date_rankings short-circuited on already_exists and
# the run did nothing in 11 seconds. raw_top100/2026-08-25 was never built.
# Pinning the process to market time makes the naive comparison mean what the
# calendar module assumes throughout.
Environment=TZ=Asia/Shanghai
# A full-universe fetch takes minutes; give it room but never let it run into
# the next session.
TimeoutStartSec=3600
ExecStart=/usr/bin/env bash $LAUNCHER
EOF

cat >"$TIMER_FILE" <<EOF
[Unit]
Description=Refresh the candidate pool at 15:30 China time, after the close

[Timer]
OnCalendar=Mon..Fri *-*-* 07:30:00 UTC
Persistent=true
Unit=stockbot-distill-refresh.service

[Install]
WantedBy=timers.target
EOF

chmod 644 "$SERVICE_FILE" "$TIMER_FILE"
systemctl daemon-reload
systemctl enable --now stockbot-distill-refresh.timer
systemctl status stockbot-distill-refresh.timer --no-pager || true

echo
echo "Distill refresh timer installed."
echo "Manual run:  systemctl start stockbot-distill-refresh.service"
echo "Next runs:   systemctl list-timers stockbot-distill-refresh.timer"
echo "Logs:        journalctl -u stockbot-distill-refresh.service -f"
