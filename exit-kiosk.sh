#!/bin/bash
set -euo pipefail

ROOT="/home/meadow/meadow-kiosk"
STOP_FLAG_RUN="/run/meadow/kiosk_stop"
STOP_FLAG_TMP="/tmp/meadow_kiosk_stop"

mkdir -p /run/meadow 2>/dev/null || true
chown meadow:meadow /run/meadow 2>/dev/null || true

# Ask kiosk loop to stop
date -Is 2>/dev/null > "${STOP_FLAG_RUN}" || true
touch "${STOP_FLAG_TMP}" 2>/dev/null || true

# Kill kiosk loop + chromium kiosk (best-effort)
pkill -f "${ROOT}/kiosk-browser\.sh" 2>/dev/null || true
pkill -f "kiosk-browser\.sh" 2>/dev/null || true
pkill -f "chromium-browser.*--kiosk" 2>/dev/null || true
pkill -f "chromium.*--kiosk" 2>/dev/null || true

exit 0
