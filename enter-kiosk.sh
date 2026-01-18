#!/bin/bash
set -euo pipefail

# --------------------------------------------------
# Meadow – Enter Kiosk Mode
# --------------------------------------------------

URL="${1:-}"
URL_FILE="/home/meadow/kiosk.url"

# Ensure log dir exists (critical)
mkdir -p /home/meadow/state 2>/dev/null || true
chown meadow:meadow /home/meadow/state 2>/dev/null || true

# Resolve kiosk URL
if [ -z "$URL" ] && [ -f "$URL_FILE" ]; then
  URL="$(head -n 1 "$URL_FILE" | tr -d '\r\n')"
fi
if [ -z "$URL" ]; then
  URL="https://meadowvending.com/kiosk1"
fi

echo "$URL" > "$URL_FILE"

# Runtime dir for kiosk-browser.sh
mkdir -p /run/meadow 2>/dev/null || true
chown meadow:meadow /run/meadow 2>/dev/null || true

# Clear stop flags (all known locations)
rm -f /run/meadow/kiosk_stop 2>/dev/null || true
rm -f /tmp/meadow_kiosk_stop 2>/dev/null || true

# Kill any existing kiosk processes
pkill -f "kiosk-browser\.sh" 2>/dev/null || true
pkill -f "chromium.*--kiosk" 2>/dev/null || true
pkill -f "chromium-browser.*--kiosk" 2>/dev/null || true

sleep 0.2

# GUI env (critical when launched via systemd)
export DISPLAY=":0"
export XAUTHORITY="/home/meadow/.Xauthority"
export XDG_RUNTIME_DIR="/run/user/1000"

# Launch kiosk browser
nohup bash /home/meadow/meadow-kiosk/kiosk-browser.sh >>/home/meadow/state/kiosk-enter.log 2>&1 &

exit 0
