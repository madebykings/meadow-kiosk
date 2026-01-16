#!/bin/bash
set -euo pipefail

STOP_FLAG="/tmp/meadow_kiosk_stop"

# Ask kiosk loop to stop
touch "$STOP_FLAG" 2>/dev/null || true

# Stop the UI user service (Wayland-friendly)
systemctl --user stop meadow-kiosk-ui.service 2>/dev/null || true

# Kill any leftover chromium kiosk instances
pkill -f "chromium-browser.*--kiosk" 2>/dev/null || true
pkill -f "chromium.*--kiosk" 2>/dev/null || true

exit 0
