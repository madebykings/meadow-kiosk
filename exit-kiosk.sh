#!/bin/bash
set -euo pipefail

# Set stop flags so kiosk-browser.sh won’t relaunch
mkdir -p /run/meadow 2>/dev/null || true
touch /run/meadow/kiosk_stop 2>/dev/null || true
touch /tmp/meadow_kiosk_stop 2>/dev/null || true

# Kill chromium + kiosk loop
pkill -f "kiosk-browser\.sh" 2>/dev/null || true
pkill -f "chromium.*--kiosk|chromium-browser.*--kiosk" 2>/dev/null || true

exit 0
