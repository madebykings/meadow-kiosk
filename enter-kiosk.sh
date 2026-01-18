#!/bin/bash
set -euo pipefail

# --------------------------------------------------
# Meadow – Enter Kiosk Mode
# --------------------------------------------------

# Optional URL override
URL="${1:-}"
URL_FILE="/home/meadow/kiosk.url"

# Resolve kiosk URL
if [ -z "$URL" ] && [ -f "$URL_FILE" ]; then
  URL="$(head -n 1 "$URL_FILE" | tr -d '\r\n')"
fi

if [ -z "$URL" ]; then
  URL="https://meadowvending.com/kiosk1"
fi

echo "$URL" > "$URL_FILE"

# --------------------------------------------------
# Runtime directories (required by kiosk-browser.sh)
# --------------------------------------------------
mkdir -p /run/meadow 2>/dev/null || true
chown meadow:meadow /run/meadow 2>/dev/null || true

# --------------------------------------------------
# Clear stop flags (ALL known locations)
# --------------------------------------------------
rm -f /run/meadow/kiosk_stop 2>/dev/null || true
rm -f /tmp/meadow_kiosk_stop 2>/dev/null || true

# --------------------------------------------------
# Kill any existing kiosk processes
# --------------------------------------------------
pkill -f "kiosk-browser\.sh" 2>/dev/null || true
pkill -f "chromium.*--kiosk" 2>/dev/null || true
pkill -f "chromium-browser.*--kiosk" 2>/dev/null || true

sleep 0.3

# --------------------------------------------------
# Ensure GUI env (critical when launched via systemd)
# --------------------------------------------------
export DISPLAY=":0"
export XAUTHORITY="/home/meadow/.Xauthority"
export XDG_RUNTIME_DIR="/run/user/1000"

# --------------------------------------------------
# Launch kiosk browser (repo path)
# --------------------------------------------------
nohup /home/meadow/meadow-kiosk/kiosk-browser.sh \
  >/home/meadow/state/kiosk-enter.log 2>&1 &

exit 0
