#!/bin/bash
set -euo pipefail

# Stop kiosk browser watchdog (also kills chromium via ExecStop)
sudo systemctl stop meadow-kiosk-browser.service 2>/dev/null || true

# Start launcher on-demand (NOT enabled at boot)
sudo systemctl start meadow-launcher.service 2>/dev/null || true

exit 0
