#!/bin/bash
set -euo pipefail

LOG_FILE="/home/meadow/state/kiosk-browser.log"
CHROMIUM_LOG="/home/meadow/state/chromium.log"
URL_FILE="/home/meadow/kiosk.url"

log() {
  local msg="$1"
  local ts
  ts="$(date -Is 2>/dev/null || date)"
  mkdir -p /home/meadow/state 2>/dev/null || true
  echo "${ts} ${msg}" >> "$LOG_FILE" 2>/dev/null || true
  echo "${msg}" >&2
}

# Stop flags (support both)
STOP_FLAG_RUN="/run/meadow/kiosk_stop"
STOP_FLAG_TMP="/tmp/meadow_kiosk_stop"

# Bail if stop flag exists
if [ -f "$STOP_FLAG_RUN" ] || [ -f "$STOP_FLAG_TMP" ]; then
  log "[Meadow] STOP_FLAG present — not starting kiosk"
  exit 0
fi

URL="about:blank"
if [ -f "$URL_FILE" ]; then
  URL="$(head -n 1 "$URL_FILE" | tr -d '\r\n')"
fi
if [ -z "$URL" ]; then URL="about:blank"; fi

# Display env (best effort; harmless if already set)
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/home/meadow/.Xauthority}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"

# Pick chromium binary
CHROME_BIN="$(command -v chromium-browser 2>/dev/null || true)"
if [ -z "$CHROME_BIN" ]; then
  CHROME_BIN="$(command -v chromium 2>/dev/null || true)"
fi
if [ -z "$CHROME_BIN" ]; then
  log "[Meadow] ERROR: Chromium not found"
  exit 1
fi

log "[Meadow] Launching chromium url=$URL"
echo "----- $(date -Is 2>/dev/null || date) launching chromium url=$URL -----" >> "$CHROMIUM_LOG" 2>/dev/null || true

# Start chromium and wait
"$CHROME_BIN" \
  --kiosk \
  --noerrdialogs \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --disable-features=TranslateUI \
  --overscroll-history-navigation=0 \
  --autoplay-policy=no-user-gesture-required \
  --allow-running-insecure-content \
  --unsafely-treat-insecure-origin-as-secure=http://127.0.0.1:8765 \
  --disable-features=BlockInsecurePrivateNetworkRequests,PrivateNetworkAccessSendPreflights \
  "$URL" >> "$CHROMIUM_LOG" 2>&1 &
PID=$!

# If stop flag appears, kill chromium
while kill -0 "$PID" 2>/dev/null; do
  if [ -f "$STOP_FLAG_RUN" ] || [ -f "$STOP_FLAG_TMP" ]; then
    log "[Meadow] STOP_FLAG present — stopping chromium"
    kill "$PID" 2>/dev/null || true
    break
  fi
  sleep 1
done

wait "$PID" 2>/dev/null || true
log "[Meadow] Chromium exited; kiosk-browser.sh done"
exit 0
