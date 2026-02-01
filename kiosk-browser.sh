#!/bin/bash
set -euo pipefail

# ------------------------------------------------------------
# Meadow kiosk-browser.sh (MINIMAL)
# ------------------------------------------------------------
# Purpose: Launch Chromium in kiosk mode + keep it running.
# Extras: Kill the Sigma/pcmanfm "Error" popup on Wayland (labwc) via wlrctl.
#
# Watches for:
#   * STOP_FLAG (exit)
#
# DOES NOT:
#   * use UI/WP heartbeats
#   * do fetch checks
#   * switch to offline.html
#   * detect Aw, Snap
# ------------------------------------------------------------

ROOT="${MEADOW_ROOT:-/home/meadow/meadow-kiosk}"
STATE_DIR="${MEADOW_STATE_DIR:-${ROOT}/state}"

LOG_FILE="${MEADOW_KIOSK_LOG_FILE:-${STATE_DIR}/kiosk-browser.log}"
CHROMIUM_LOG="${MEADOW_CHROMIUM_LOG:-${STATE_DIR}/chromium.log}"

URL_FILE="${MEADOW_KIOSK_URL_FILE:-${ROOT}/kiosk.url}"
DEFAULT_URL="${MEADOW_DEFAULT_URL:-about:blank}"

# Stop flag: prefer /run, fallback /tmp
STOP_FLAG_RUN="${MEADOW_KIOSK_STOP_FLAG:-/run/meadow/kiosk_stop}"
STOP_FLAG_TMP="/tmp/meadow_kiosk_stop"

# PID file (prefer /run)
KIOSK_PIDFILE="${MEADOW_KIOSK_PIDFILE:-/run/meadow/kiosk_browser.pid}"

# Relaunch tuning
WATCH_INTERVAL="${MEADOW_WATCH_INTERVAL:-2}"
BACKOFF_START="${MEADOW_BACKOFF_START:-2}"
BACKOFF_MAX="${MEADOW_BACKOFF_MAX:-30}"

# Popup killer tuning
KILL_POPUPS="${MEADOW_KILL_POPUPS:-1}"              # 1=on, 0=off
KILL_POPUP_INTERVAL="${MEADOW_KILL_POPUP_INTERVAL:-2}"

# Wayland env defaults (labwc)
WAYLAND_RUNTIME_DIR="${MEADOW_WAYLAND_RUNTIME_DIR:-${XDG_RUNTIME_DIR:-/run/user/1000}}"
WAYLAND_DISPLAY_NAME="${MEADOW_WAYLAND_DISPLAY:-${WAYLAND_DISPLAY:-wayland-0}}"

# Dedicated Chrome profile
CHROME_PROFILE="${MEADOW_CHROME_PROFILE:-${STATE_DIR}/chrome-profile}"

log() {
  local msg="$1"
  local ts
  ts="$(date -Is 2>/dev/null || date)"
  mkdir -p "${STATE_DIR}" 2>/dev/null || true
  echo "${ts} ${msg}" >> "${LOG_FILE}" 2>/dev/null || true
  echo "${msg}" >&2
}

_stop_flag_present() {
  [ -f "$STOP_FLAG_RUN" ] || [ -f "$STOP_FLAG_TMP" ]
}

get_url() {
  local url="$DEFAULT_URL"
  if [ -f "$URL_FILE" ]; then
    url="$(head -n 1 "$URL_FILE" | tr -d '\r\n' || true)"
  fi
  if [ -z "${url:-}" ]; then
    url="$DEFAULT_URL"
  fi
  # Normalize common case to avoid 301 (kiosk1 -> kiosk1/)
  if [[ "$url" == "https://meadowvending.com/kiosk1" ]]; then
    url="https://meadowvending.com/kiosk1/"
  fi
  echo "$url"
}

find_chrome() {
  local bin
  bin="${MEADOW_CHROME_BIN:-}"
  if [ -n "$bin" ] && command -v "$bin" >/dev/null 2>&1; then
    echo "$bin"; return
  fi
  bin="$(command -v chromium-browser 2>/dev/null || true)"
  if [ -z "$bin" ]; then
    bin="$(command -v chromium 2>/dev/null || true)"
  fi
  echo "$bin"
}

# ------------------------------------------------------------
# Popup killer (Wayland / labwc)
# ------------------------------------------------------------
close_sigma_popup_wayland() {
  # Closes pcmanfm "Error" popup (used by Sigma/network UI weirdness)
  if [ "${KILL_POPUPS}" != "1" ]; then
    return
  fi
  if command -v wlrctl >/dev/null 2>&1; then
    XDG_RUNTIME_DIR="$WAYLAND_RUNTIME_DIR" WAYLAND_DISPLAY="$WAYLAND_DISPLAY_NAME" \
      wlrctl toplevel close app_id:pcmanfm title:Error >/dev/null 2>&1 || true
  fi
}

# ---- startup ------------------------------------------------

sleep 1

mkdir -p \
  "$(dirname "$STOP_FLAG_RUN")" \
  "$(dirname "$KIOSK_PIDFILE")" \
  "$STATE_DIR" \
  "$CHROME_PROFILE" \
  2>/dev/null || true

log "[Meadow] MINIMAL kiosk-browser starting"
log "[Meadow] STOP_FLAG_RUN=$STOP_FLAG_RUN (fallback $STOP_FLAG_TMP)"
log "[Meadow] KIOSK_PIDFILE=$KIOSK_PIDFILE"
log "[Meadow] URL_FILE=$URL_FILE"

if _stop_flag_present; then
  log "[Meadow] STOP_FLAG present — kiosk start blocked"
  exit 0
fi

CHROME_BIN="$(find_chrome)"
if [ -z "$CHROME_BIN" ]; then
  log "[Meadow] ERROR: Chromium not found. Install 'chromium' package."
  exit 1
fi

BACKOFF="$BACKOFF_START"

# ---- main loop ---------------------------------------------

while true; do
  if _stop_flag_present; then
    log "[Meadow] STOP_FLAG present — exiting kiosk loop"
    exit 0
  fi

  URL="$(get_url)"

  # Display env (best effort)
  export DISPLAY="${DISPLAY:-:0}"
  export XAUTHORITY="${XAUTHORITY:-/home/meadow/.Xauthority}"
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"

  log "[Meadow] Launching chromium url=$URL"
  echo "----- $(date -Is 2>/dev/null || date) launching chromium url=$URL -----" >> "$CHROMIUM_LOG" 2>/dev/null || true

  "$CHROME_BIN" \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-gpu \
    --disable-gpu-compositing \
    --disable-software-rasterizer \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --no-first-run \
    --no-default-browser-check \
    --disable-sync \
    --disable-notifications \
    --disable-background-networking \
    --disable-component-update \
    --disable-features=TranslateUI,BackForwardCache,BlockInsecurePrivateNetworkRequests,PrivateNetworkAccessSendPreflights \
    --overscroll-history-navigation=0 \
    --autoplay-policy=no-user-gesture-required \
    --allow-running-insecure-content \
    --unsafely-treat-insecure-origin-as-secure=http://127.0.0.1:8765 \
    --user-data-dir="$CHROME_PROFILE" \
    "$URL" >> "$CHROMIUM_LOG" 2>&1 &

  CHROME_PID=$!
  echo "$CHROME_PID" > "$KIOSK_PIDFILE" 2>/dev/null || true

  # Watch loop while chromium is alive
  LAST_POPUP_KILL=0
  while kill -0 "$CHROME_PID" 2>/dev/null; do
    if _stop_flag_present; then
      log "[Meadow] STOP_FLAG present — stopping chromium"
      kill "$CHROME_PID" 2>/dev/null || true
      break
    fi

    NOW="$(date +%s)"

    # popup killer
    if [ "${KILL_POPUPS}" = "1" ]; then
      if [ $((NOW - LAST_POPUP_KILL)) -ge "${KILL_POPUP_INTERVAL}" ]; then
        close_sigma_popup_wayland
        LAST_POPUP_KILL="$NOW"
      fi
    fi

    sleep "$WATCH_INTERVAL"
  done

  wait "$CHROME_PID" 2>/dev/null || true

  log "[Meadow] Chromium exited — backoff ${BACKOFF}s"
  sleep "$BACKOFF"

  BACKOFF=$((BACKOFF * 2))
  if [ "$BACKOFF" -gt "$BACKOFF_MAX" ]; then
    BACKOFF="$BACKOFF_MAX"
  fi
done
