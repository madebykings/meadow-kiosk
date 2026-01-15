#!/bin/bash
set -euo pipefail

STOP_FLAG="/tmp/meadow_kiosk_stop"
LAUNCHER="/home/meadow/kiosk-launcher.py"
COOLDOWN_FILE="/tmp/meadow_launcher_last_start"
COOLDOWN_SECS="${MEADOW_LAUNCHER_COOLDOWN_SECS:-10}"

# Stop the kiosk relaunch loop
touch "$STOP_FLAG" 2>/dev/null || true

# Kill chromium kiosk if running
pkill -f "chromium-browser.*--kiosk" 2>/dev/null || true
pkill -f "chromium.*--kiosk" 2>/dev/null || true

# Kill the kiosk loop script if running
pkill -f "/home/meadow/kiosk-browser.sh" 2>/dev/null || true

launcher_running() {
  pgrep -f "$LAUNCHER" >/dev/null 2>&1
}

cooldown_active() {
  [ -f "$COOLDOWN_FILE" ] || return 1
  local now ts age
  now=$(date +%s)
  ts=$(stat -c %Y "$COOLDOWN_FILE" 2>/dev/null || echo 0)
  age=$((now - ts))
  [ "$age" -lt "$COOLDOWN_SECS" ]
}

mark_started() {
  echo "$(date +%s)" > "$COOLDOWN_FILE" 2>/dev/null || true
}

# Relaunch the launcher (guarded + non-blocking)
if launcher_running; then
  exit 0
fi

if cooldown_active; then
  exit 0
fi

nohup python3 "$LAUNCHER" >/dev/null 2>&1 &
mark_started

exit 0
