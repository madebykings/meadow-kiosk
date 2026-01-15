#!/bin/bash
set -e

STOP_FLAG="/tmp/meadow_kiosk_stop"

# Stop the kiosk relaunch loop
touch "$STOP_FLAG"

# Kill chromium kiosk if running
pkill -f "chromium-browser.*--kiosk" || true
pkill -f "chromium.*--kiosk" || true

# Kill the kiosk loop script if running
pkill -f "/home/meadow/kiosk-browser.sh" || true

# Relaunch the launcher (non-blocking)
nohup python3 /home/meadow/kiosk-launcher.py >/dev/null 2>&1 &

exit 0
