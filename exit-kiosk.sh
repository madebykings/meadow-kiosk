#!/bin/bash
set -euo pipefail

STOP_FLAG="/tmp/meadow_kiosk_stop"
LAUNCHER_UNIT="meadow-launcher.service"
KIOSK_UNIT="meadow-kiosk-browser.service"

# Tell kiosk loop to stop (script may check this)
touch "$STOP_FLAG" 2>/dev/null || true

# Stop the systemd-managed kiosk loop (THIS is the key)
sudo systemctl stop "$KIOSK_UNIT" || true

# Kill any leftover chromium kiosk instances (belt + braces)
pkill -f "chromium-browser.*--kiosk" 2>/dev/null || true
pkill -f "chromium.*--kiosk" 2>/dev/null || true

# Start the launcher service (desktop mode)
sudo systemctl start "$LAUNCHER_UNIT" || true

exit 0
