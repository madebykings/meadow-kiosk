#!/bin/bash
set -euo pipefail

# ------------------------------------------------------------
# Meadow kiosk-browser.sh
#
# - Launches Chromium in kiosk mode and watches for:
#   * STOP_FLAG (exit)
#   * UI heartbeat staleness (restart Chromium)
#   * WP heartbeat staleness (switch to offline.html)
#
# IMPORTANT PATH RULE:
# Prefer /run/meadow/* as primary. Fall back to /tmp/* only if /run file missing.
# ------------------------------------------------------------

LOG_FILE="/home/meadow/state/kiosk-browser.log"
CHROMIUM_LOG="/home/meadow/state/chromium.log"

URL_FILE="${MEADOW_KIOSK_URL_FILE:-/home/meadow/kiosk.url}"
DEFAULT_URL="${MEADOW_DEFAULT_URL:-about:blank}"
OFFLINE_URL="${MEADOW_OFFLINE_URL:-file:///home/meadow/offline.html}"

# Stop flag: prefer /run, fallback /tmp
STOP_FLAG_RUN="${MEADOW_KIOSK_STOP_FLAG:-/run/meadow/kiosk_stop}"
STOP_FLAG_TMP="/tmp/meadow_kiosk_stop"

# Heartbeats: prefer /run, fallback /tmp
UI_HEARTBEAT_RUN="${MEADOW_UI_HEARTBEAT_FILE:-/run/meadow/ui_heartbeat}"
WP_HEARTBEAT_RUN="${MEADOW_WP_HEARTBEAT_FILE:-/run/meadow/wp_heartbeat}"
UI_HEARTBEAT_TMP="/tmp/meadow_ui_heartbeat"
WP_HEARTBEAT_TMP="/tmp/meadow_wp_heartbeat"

# PID / restart tracking (prefer /run)
KIOSK_PIDFILE="${MEADOW_KIOSK_PIDFILE:-/run/meadow/kiosk_browser.pid}"
RESTART_LOG="${MEADOW_RESTART_LOG:-/run/meadow/kiosk_restart_times}"

# Watchdog tuning
UI_HEARTBEAT_MAX_AGE="${MEADOW_UI_HEARTBEAT_MAX_AGE:-45}"
WP_HEARTBEAT_MAX_AGE="${MEADOW_WP_HEARTBEAT_MAX_AGE:-180}"
HEARTBEAT_GRACE="${MEADOW_HEARTBEAT_GRACE:-120}"
WATCH_INTERVAL="${MEADOW_WATCH_INTERVAL:-5}"

RESTART_WINDOW_SECS="${MEADOW_RESTART_WINDOW_SECS:-600}"
MAX_RESTARTS_IN_WINDOW="${MEADOW_MAX_RESTARTS_IN_WINDOW:-10}"
BACKOFF_START="${MEADOW_BACKOFF_START:-2}"
BACKOFF_MAX="${MEADOW_BACKOFF_MAX:-60}"

# Optional: bridge UI heartbeat by poking local daemon
DAEMON_HEARTBEAT_URL="${MEADOW_DAEMON_HEARTBEAT_URL:-http://127.0.0.1:8765/heartbeat}"

# Dedicated Chrome profile (helps stability / avoids V8 OOM & weird state)
CHROME_PROFILE="${MEADOW_CHROME_PROFILE:-/home/meadow/state/chrome-profile}"

# ---- helpers ------------------------------------------------
log() {
  local msg="$1"
  local ts
  ts="$(date -Is 2>/dev/null || date)"
  mkdir -p /home/meadow/state 2>/dev/null || true
  echo "${ts} ${msg}" >> "$LOG_FILE" 2>/dev/null || true
  echo "${msg}" >&2
}

_pick_primary_or_fallback() {
  local run_path="$1"
  local tmp_path="$2"
  if [ -f "$run_path" ]; then
    echo "$run_path"
  elif [ -f "$tmp_path" ]; then
    echo "$tmp_path"
  else
    echo ""
  fi
}

_stop_flag_present() {
  [ -f "$STOP_FLAG_RUN" ] || [ -f "$STOP_FLAG_TMP" ]
}

_touch_best_effort() {
  local path="$1"
  mkdir -p "$(dirname "$path")" 2>/dev/null || true
  touch "$path" 2>/dev/null || true
}

_epoch_mtime() {
  local f="$1"
  if [ -f "$f" ]; then
    stat -c %Y "$f" 2>/dev/null || echo 0
  else
    echo 0
  fi
}

_age_of_file() {
  local f="$1"
  local now ts
  now="$(date +%s)"
  ts="$(_epoch_mtime "$f")"
  if [ "$ts" -le 0 ]; then
    echo 999999
  else
    echo $((now - ts))
  fi
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

touch_restart() {
  local now
  now="$(date +%s)"
  mkdir -p "$(dirname "$RESTART_LOG")" 2>/dev/null || true
  echo "$now" >> "$RESTART_LOG" 2>/dev/null || true

  # Keep only entries in window
  if [ -f "$RESTART_LOG" ]; then
    awk -v now="$now" -v win="$RESTART_WINDOW_SECS" 'now-$1 <= win {print $1}' "$RESTART_LOG" > "${RESTART_LOG}.tmp" 2>/dev/null || true
    mv -f "${RESTART_LOG}.tmp" "$RESTART_LOG" 2>/dev/null || true
  fi
}

restart_count() {
  if [ ! -f "$RESTART_LOG" ]; then
    echo 0
    return
  fi
  wc -l < "$RESTART_LOG" 2>/dev/null | tr -d ' '
}

bridge_ui_heartbeat() {
  if curl -sS -m 1 -o /dev/null -X POST "$DAEMON_HEARTBEAT_URL" 2>/dev/null; then
    _touch_best_effort "$UI_HEARTBEAT_RUN"
    _touch_best_effort "$UI_HEARTBEAT_TMP"
  fi
}

find_chrome() {
  local bin
  bin="${MEADOW_CHROME_BIN:-}"

  if [ -n "$bin" ] && command -v "$bin" >/dev/null 2>&1; then
    echo "$bin"
    return
  fi

  bin="$(command -v chromium-browser 2>/dev/null || true)"
  if [ -z "$bin" ]; then
    bin="$(command -v chromium 2>/dev/null || true)"
  fi
  echo "$bin"
}

# ---- startup ------------------------------------------------
sleep 2

mkdir -p \
  "$(dirname "$STOP_FLAG_RUN")" \
  "$(dirname "$UI_HEARTBEAT_RUN")" \
  "$(dirname "$WP_HEARTBEAT_RUN")" \
  "$(dirname "$KIOSK_PIDFILE")" \
  "$(dirname "$RESTART_LOG")" \
  "/home/meadow/state" \
  "$CHROME_PROFILE" \
  2>/dev/null || true

log "[Meadow] Using STOP_FLAG_RUN=$STOP_FLAG_RUN (fallback $STOP_FLAG_TMP)"
log "[Meadow] Using UI_HEARTBEAT_RUN=$UI_HEARTBEAT_RUN (fallback $UI_HEARTBEAT_TMP)"
log "[Meadow] Using WP_HEARTBEAT_RUN=$WP_HEARTBEAT_RUN (fallback $WP_HEARTBEAT_TMP)"
log "[Meadow] Using KIOSK_PIDFILE=$KIOSK_PIDFILE"
log "[Meadow] Using RESTART_LOG=$RESTART_LOG"

if _stop_flag_present; then
  log "[Meadow] STOP_FLAG present — kiosk start blocked"
  exit 0
fi

CHROME_BIN="$(find_chrome)"
if [ -z "$CHROME_BIN" ]; then
  log "[Meadow] ERROR: Chromium not found. Install 'chromium' package."
  exit 1
fi

# Ensure heartbeat files exist so ages aren't huge on first run
_touch_best_effort "$UI_HEARTBEAT_RUN"
_touch_best_effort "$UI_HEARTBEAT_TMP"
_touch_best_effort "$WP_HEARTBEAT_RUN"
_touch_best_effort "$WP_HEARTBEAT_TMP"

BACKOFF="$BACKOFF_START"

# ---- main loop ---------------------------------------------
while true; do
  if _stop_flag_present; then
    log "[Meadow] STOP_FLAG present — exiting kiosk loop"
    exit 0
  fi

  URL="$(get_url)"

  UI_HB_FILE="$(_pick_primary_or_fallback "$UI_HEARTBEAT_RUN" "$UI_HEARTBEAT_TMP")"
  WP_HB_FILE="$(_pick_primary_or_fallback "$WP_HEARTBEAT_RUN" "$WP_HEARTBEAT_TMP")"

  # If WP heartbeat exists and is stale, start in offline mode
  if [ -n "$WP_HB_FILE" ]; then
    WP_AGE="$(_age_of_file "$WP_HB_FILE")"
    if [ "$WP_AGE" -gt "$WP_HEARTBEAT_MAX_AGE" ]; then
      URL="$OFFLINE_URL"
    fi
  fi

  # Display env (best effort)
  export DISPLAY="${DISPLAY:-:0}"
  export XAUTHORITY="${XAUTHORITY:-/home/meadow/.Xauthority}"
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"

  log "[Meadow] Launching chromium url=$URL"
  echo "----- $(date -Is 2>/dev/null || date) launching chromium url=$URL -----" >> "$CHROMIUM_LOG" 2>/dev/null || true

  # Launch chromium (MUST background to track PID)
  "$CHROME_BIN" \
    --no-sandbox \
    --disable-dev-shm-usage \
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
  START_TS="$(date +%s)"

  # Watch loop while chromium is alive
  while kill -0 "$CHROME_PID" 2>/dev/null; do
    if _stop_flag_present; then
      log "[Meadow] STOP_FLAG present — stopping chromium"
      kill "$CHROME_PID" 2>/dev/null || true
      break
    fi

    NOW="$(date +%s)"
    ELAPSED=$((NOW - START_TS))

    # Optional bridge heartbeat
    bridge_ui_heartbeat

    # Re-pick heartbeat sources each tick
    UI_HB_FILE="$(_pick_primary_or_fallback "$UI_HEARTBEAT_RUN" "$UI_HEARTBEAT_TMP")"
    WP_HB_FILE="$(_pick_primary_or_fallback "$WP_HEARTBEAT_RUN" "$WP_HEARTBEAT_TMP")"

    # UI watchdog (after grace)
    if [ -n "$UI_HB_FILE" ] && [ "$ELAPSED" -gt "$HEARTBEAT_GRACE" ]; then
      UI_AGE="$(_age_of_file "$UI_HB_FILE")"
      if [ "$UI_AGE" -gt "$UI_HEARTBEAT_MAX_AGE" ]; then
        log "[Meadow] UI heartbeat stale (${UI_AGE}s) — restarting Chromium"
        kill "$CHROME_PID" 2>/dev/null || true
        break
      fi
    fi

    # WP watchdog: if stale and not already offline, restart to switch URL
    if [ -n "$WP_HB_FILE" ] && [ "$ELAPSED" -gt "$HEARTBEAT_GRACE" ] && [ "$URL" != "$OFFLINE_URL" ]; then
      WP_AGE="$(_age_of_file "$WP_HB_FILE")"
      if [ "$WP_AGE" -gt "$WP_HEARTBEAT_MAX_AGE" ]; then
        log "[Meadow] WP heartbeat stale (${WP_AGE}s) — switching to offline screen"
        kill "$CHROME_PID" 2>/dev/null || true
        break
      fi
    fi

    sleep "$WATCH_INTERVAL"
  done

  wait "$CHROME_PID" 2>/dev/null || true

  # restart accounting
  touch_restart
  CNT="$(restart_count)"

  if [ "$CNT" -gt "$MAX_RESTARTS_IN_WINDOW" ]; then
    log "[Meadow] Too many restarts (${CNT} in ${RESTART_WINDOW_SECS}s) — stopping kiosk"
    echo "$(date -Is 2>/dev/null || date)" > "$STOP_FLAG_RUN" 2>/dev/null || true
    echo "$(date -Is 2>/dev/null || date)" > "$STOP_FLAG_TMP" 2>/dev/null || true
    exit 0
  fi

  log "[Meadow] Chromium exited — backoff ${BACKOFF}s (restart count ${CNT}/${MAX_RESTARTS_IN_WINDOW})"
  sleep "$BACKOFF"

  # exponential backoff
  BACKOFF=$((BACKOFF * 2))
  if [ "$BACKOFF" -gt "$BACKOFF_MAX" ]; then
    BACKOFF="$BACKOFF_MAX"
  fi

  # If UI heartbeat looks healthy, reset backoff quickly
  UI_HB_FILE="$(_pick_primary_or_fallback "$UI_HEARTBEAT_RUN" "$UI_HEARTBEAT_TMP")"
  if [ -n "$UI_HB_FILE" ]; then
    UI_AGE="$(_age_of_file "$UI_HB_FILE")"
    if [ "$UI_AGE" -le "$UI_HEARTBEAT_MAX_AGE" ]; then
      BACKOFF="$BACKOFF_START"
    fi
  fi
done
