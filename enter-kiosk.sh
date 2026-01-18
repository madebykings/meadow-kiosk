#!/bin/bash
set -euo pipefail

ROOT="/home/meadow/meadow-kiosk"
STATE_DIR="${ROOT}/state"
URL_FILE="${ROOT}/kiosk.url"

STOP_FLAG_RUN="/run/meadow/kiosk_stop"
STOP_FLAG_TMP="/tmp/meadow_kiosk_stop"

mkdir -p "${STATE_DIR}" 2>/dev/null || true
mkdir -p /run/meadow 2>/dev/null || true
chown meadow:meadow /run/meadow 2>/dev/null || true

# URL: arg > file > default
URL="${1:-}"
if [ -z "${URL}" ] && [ -f "${URL_FILE}" ]; then
  URL="$(head -n 1 "${URL_FILE}" | tr -d '\r\n' || true)"
fi
if [ -z "${URL}" ]; then
  URL="https://meadowvending.com/kiosk1/"
fi

# Normalize common 301 case
if [ "${URL}" = "https://meadowvending.com/kiosk1" ]; then
  URL="https://meadowvending.com/kiosk1/"
fi

echo "${URL}" > "${URL_FILE}"

# Make Enter deterministic: do a clean exit first
bash "${ROOT}/exit-kiosk.sh" >/dev/null 2>&1 || true
sleep 0.4

# Clear stop flags
rm -f "${STOP_FLAG_RUN}" 2>/dev/null || true
rm -f "${STOP_FLAG_TMP}" 2>/dev/null || true

# Kill any existing kiosk processes (belt + braces)
pkill -f "${ROOT}/kiosk-browser\.sh" 2>/dev/null || true
pkill -f "kiosk-browser\.sh" 2>/dev/null || true
pkill -f "chromium.*--kiosk" 2>/dev/null || true
pkill -f "chromium-browser.*--kiosk" 2>/dev/null || true

sleep 0.2

# GUI env (critical when launched via systemd/API)
export DISPLAY=":0"
export XAUTHORITY="/home/meadow/.Xauthority"
export XDG_RUNTIME_DIR="/run/user/1000"

nohup bash "${ROOT}/kiosk-browser.sh" >>"${STATE_DIR}/kiosk-enter.log" 2>&1 &
exit 0
