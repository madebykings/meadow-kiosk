#!/bin/bash
set -euo pipefail

URL="${1:-}"
URL_FILE="/home/meadow/kiosk.url"

# Use existing URL_FILE if no arg passed
if [ -z "$URL" ]; then
  if [ -f "$URL_FILE" ]; then
    URL="$(head -n 1 "$URL_FILE" | tr -d '\r\n')"
  fi
fi

# Fallback if still empty
if [ -z "$URL" ]; then
  URL="https://meadowvending.com/kiosk1"
fi

# Make sure it’s stored (so kiosk-browser.sh reads it)
echo "$URL" > "$URL_FILE"

# Clear stop flags (both old and new locations)
rm -f /run/meadow/kiosk_stop 2>/dev/null || true
rm -f /tmp/meadow_kiosk_stop 2>/dev/null || true

# Kill any previous kiosk chromium
pkill -f "chromium.*--kiosk|chromium-browser.*--kiosk" 2>/dev/null || true
pkill -f "kiosk-browser\.sh" 2>/dev/null || true

# Launch kiosk-browser.sh detached (it will start chromium)
nohup /home/meadow/kiosk-browser.sh >/dev/null 2>&1 &
