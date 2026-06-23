#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/outlook-event-agent}"
APP_USER="${APP_USER:-outlook-agent}"
SERVICE_NAME="${SERVICE_NAME:-outlook-event-agent.service}"
DIGEST_SERVICE_NAME="${DIGEST_SERVICE_NAME:-outlook-event-agent-digest.service}"
DIGEST_TIMER_NAME="${DIGEST_TIMER_NAME:-outlook-event-agent-digest.timer}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo APP_DIR=$APP_DIR bash scripts/install-systemd.sh" >&2
  exit 1
fi

id "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR"
rsync -a --delete \
  --exclude "__pycache__" \
  --exclude "data/*.sqlite3" \
  --exclude "data/last_run.json" \
  --exclude "config.local.json" \
  ./ "$APP_DIR/"

if [[ ! -f "$APP_DIR/config.local.json" ]]; then
  cp "$APP_DIR/config.example.json" "$APP_DIR/config.local.json"
fi
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
fi

install -m 0644 "$APP_DIR/deploy/outlook-event-agent.service" "/etc/systemd/system/$SERVICE_NAME"
install -m 0644 "$APP_DIR/deploy/outlook-event-agent-digest.service" "/etc/systemd/system/$DIGEST_SERVICE_NAME"
install -m 0644 "$APP_DIR/deploy/outlook-event-agent-digest.timer" "/etc/systemd/system/$DIGEST_TIMER_NAME"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

cat <<EOF
Installed $SERVICE_NAME.

Next:
1. Edit $APP_DIR/config.local.json
2. Edit $APP_DIR/.env
3. Authorize calendar access, then start:
   sudo systemctl start $SERVICE_NAME
   journalctl -u $SERVICE_NAME -f
4. Optional daily digest after notifications are configured:
   sudo systemctl enable --now $DIGEST_TIMER_NAME
EOF
