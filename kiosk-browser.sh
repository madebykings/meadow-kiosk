#!/bin/bash
set -euo pipefail

LOG_FILE="/home/meadow/state/kiosk-browser.log"
CHROMIUM_LOG="/home/meadow/state/chromium.log"
URL_FILE="/home/meadow/kiosk.url"
OFFLINE_URL="file:///home/meadow/offline.html"
DEFAULT_URL="about:blank"

mkdir -p /home/meadow/state 2>/dev/null || true

log() {
  local msg="$1"
  local ts
  ts="$(date -Is 2>/dev/null || date)"
  echo "${ts} ${msg}" >> "$LOG_FILE" 2>/dev/null || true
  echo "${msg}" >&2
}

# ------------------------------------------------------------
# Canonical runtime paths (prefer /tmp because it's easiest via SSH)
# Also support /run/meadow for backwards compatibility
# ------------------------------------------------------------
STOP_FLAG="${MEADOW_KIOSK_STOP_FLAG:-/tmp/meadow_kiosk_stop}"
STOP_FLAG_COMPAT_RUN="/run/meadow/kiosk_stop"

UI_HEARTBEAT_FILE="${MEADOW_UI_HEARTBEAT_FILE:-/tmp/meadow_ui_heartbeat}"
UI_HEARTBEAT_COMPAT_RUN="/run/meadow/ui_heartbeat"

WP_HEARTBEAT_FILE="${MEADOW_WP_HEARTBEAT_FILE:-/tmp/meadow_wp_heartbeat}"
WP_HEARTBEAT_COMPAT_RUN="/run/meadow/wp_heartbeat"

KIOSK_PIDFILE="${MEADOW_KIOSK_PIDFILE:-/tmp/meadow_kiosk_browser.pid}"
RESTART_LOG="${MEADOW_RESTART_LOG:-/tmp/meadow_kiosk_restart_times}"

# Health/heartbeat config
UI_HEARTBEAT_MAX_AGE="${MEADOW_UI_HEARTBEAT_MAX_AGE:-45}"
WP_HEARTBEAT_MAX_AGE="${MEADOW_WP_HEARTBEAT_MAX_AGE:-600}"   # was 180; 10 min is safer
HEARTBEAT_GRACE="${MEADOW_HEARTBEAT_GRACE:-120}"
WATCH_INTERVAL="${MEADOW_WATCH_INTERVAL:-5}"

# Restart safety
RESTART_WINDOW_SECS="${MEADOW_RESTART_WINDOW_SECS:-600}"
MAX_RESTARTS_IN_WINDOW="${MEADOW_MAX_RESTARTS_IN_WINDOW:-10}"
BACKOFF_START="${MEADOW_BACKOFF_START:-2}"
BACKOFF_MAX="${MEADOW_BACKOFF_MAX:-60}"

# Local daemon heartbeat endpoint (pi_api)
DAEMON_HEARTBEAT_URL="${MEADOW_DAEMON_HEARTBEAT_URL:-http://127.0.0.1:8765/heartbeat}"

log "[Meadow] Using STOP_FLAG=$STOP_FLAG (compat $STOP_FLAG_COMPAT_RUN)"
log "[Meadow] Using UI_HEARTBEAT_FILE=$UI_HEARTBEAT_FILE (compat $UI_HEARTBEAT_COMPAT_RUN)"
log "[Meadow] Using WP_HEARTBEAT_FILE=$WP_HEARTBEAT_FILE (compat $WP_HEARTBEAT_COMPAT_RUN)"
log "[Meadow] Using KIOSK_PIDFILE=$KIOSK_PIDFILE"
log "[Meadow] Using RESTART_LOG=$RESTART_LOG"
log "[Meadow] UI_HEARTBEAT_MAX_AGE=$UI_HEARTBEAT_MAX_AGE"
log "[Meadow] WP_HEARTBEAT_MAX_AGE=$WP_HEARTBEAT_MAX_AGE"

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
stop_flag_present() {
  [ -f "$STOP_FLAG" ] || [ -f "$STOP_FLAG_COMPAT_RUN" ]
}

get_url() {
  if [ -f "$URL_FILE" ]; then
    head -n 1 "$URL_FILE" | tr -d '\r\n'
  else
    echo "$DEFAULT_URL"
  fi
}

touch_restart() {
  local now
  now=$(date +%s)
  echo "$now" >> "$RESTART_LOG" 2>/dev/null || true
  if [ -f "$RESTART_LOG" ]; then
    awk -v now="$now" -v win="$RESTART_WINDOW_SECS" 'now-$1 <= win {print $1}' "$RESTART_LOG" > "${RESTART_LOG}.tmp" 2>/dev/null || true
    mv -f "${RESTART_LOG}.tmp" "$RESTART_LOG" 2>/dev/null || true
  fi
}

restart_count() {
  if [ ! -f "$RESTART_LOG" ]; then echo 0; return; fi
  wc -l < "$RESTART_LOG" 2>/dev/null | tr -d ' '
}

heartbeat_age() {
  local f="$1"
  local now hb
  now=$(date +%s)
  if [ -f "$f" ]; then
    hb=$(stat -c %Y "$f" 2>/dev/null || echo 0)
    echo $((now - hb))
  else
    echo 999999
  fi
}

# Prefer canonical file age, but if missing/stale and compat exists/fresher, use compat.
best_heartbeat_age() {
  local canonical="$1"
  local compat="$2"
  local a b
  a=$(heartbeat_age "$canonical")
  b=$(heartbeat_age "$compat")
  # choose fresher (smaller age)
  if [ "$b" -lt "$a" ]; then
    echo "$b"
  else
    echo "$a"
  fi
}

# Bridge: if local daemon responds, mark UI heartbeat as fresh
bridge_ui_heartbeat() {
  if curl -sS -m 1 -o /dev/null -X POST "$DAEMON_HEARTBEAT_URL" 2>/dev/null; then
    touch "$UI_HEARTBEAT_FILE" 2>/dev/null || true
    # keep compat file fresh too (optional)
    touch "$UI_HEARTBEAT_COMPAT_RUN" 2>/dev/null || true
  fi
}

# Wait for a usable display to avoid "Missing X server or $DISPLAY"
wait_for_display() {
  export DISPLAY="${DISPLAY:-:0}"
  export XAUTHORITY="${XAUTHORITY:-/home/meadow/.Xauthority}"
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"

  # Wait up to 60s for X socket (Pi OS desktop)
  for _ in $(seq 1 60); do
    if [ -S "/tmp/.X11-unix/X0" ]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

# Pick chromium binary
detect_chromium() {
  local bin=""
  bin="$(command -v chromium-browser 2>/dev/null || true)"
  if [ -z "$bin" ]; then
    bin="$(command -v chromium 2>/dev/null || true)"
  fi
  echo "$bin"
}

# ------------------------------------------------------------
# Startup guards
# ------------------------------------------------------------
if stop_flag_present; then
  log "[Meadow] STOP_FLAG present — kiosk start blocked"
  exit 0
fi

mkdir -p "$(dirname "$KIOSK_PIDFILE")" "$(dirname "$RESTART_LOG")" 2>/dev/null || true
touch "$UI_HEARTBEAT_FILE" 2>/dev/null || true
touch "$WP_HEARTBEAT_FILE" 2>/dev/null || true

CHROME_BIN="$(detect_chromium)"
if [ -z "$CHROME_BIN" ]; then
  log "[Meadow] ERROR: Chromium not found. Install 'chromium' package."
  exit 1
fi

BACKOFF="$BACKOFF_START"

# ------------------------------------------------------------
# Watchdog loop
# ------------------------------------------------------------
while true; do
  if stop_flag_present; then
    log "[Meadow] STOP_FLAG present — exiting watchdog"
    exit 0
  fi

  # Ensure we have a display, otherwise don't thrash restarts
  if ! wait_for_display; then
    log "[Meadow] No X display detected (missing /tmp/.X11-unix/X0) — retrying in 10s"
    sleep 10
    continue
  fi

  # Decide URL
  URL="$(get_url)"
  if [ -z "$URL" ]; then URL="$DEFAULT_URL"; fi

  # If WP heartbeat is stale, force offline
  WP_AGE="$(best_heartbeat_age "$WP_HEARTBEAT_FILE" "$WP_HEARTBEAT_COMPAT_RUN")"
  if [ "$WP_AGE" -gt "$WP_HEARTBEAT_MAX_AGE" ] && [ -f "$OFFLINE_URL" ]; then
    log "[Meadow] WP heartbeat stale (${WP_AGE}s) — switching to offline screen"
    URL="$OFFLINE_URL"
  fi

  # Belt + braces: kill any stray kiosk chromium before launching
  pkill -f "chromium-browser.*--kiosk" 2>/dev/null || true
  pkill -f "chromium.*--kiosk" 2>/dev/null || true

  log "[Meadow] Launching chromium url=$URL"
  echo "----- $(date -Is 2>/dev/null || date) launching chromium url=$URL -----" >> "$CHROMIUM_LOG" 2>/dev/null || true

  "$CHROME_BIN" \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --no-sandbox \
    --disable-setuid-sandbox \
    --disable-session-crashed-bubble \
    --disable-features=TranslateUI \
    --overscroll-history-navigation=0 \
    --autoplay-policy=no-user-gesture-required \
    --allow-running-insecure-content \
    --unsafely-treat-insecure-origin-as-secure=http://127.0.0.1:8765 \
    --disable-features=BlockInsecurePrivateNetworkRequests,PrivateNetworkAccessSendPreflights \
    --disable-dev-shm-usage \
    "$URL" >> "$CHROMIUM_LOG" 2>&1 &

  CHROME_PID=$!
  echo "$CHROME_PID" > "$KIOSK_PIDFILE" 2>/dev/null || true
  START_TS=$(date +%s)

  # Watch while chromium is running
  while kill -0 "$CHROME_PID" 2>/dev/null; do
    if stop_flag_present; then
      log "[Meadow] STOP_FLAG present — stopping chromium"
      kill "$CHROME_PID" 2>/dev/null || true
      break
    fi

    NOW=$(date +%s)
    ELAPSED=$((NOW - START_TS))

    bridge_ui_heartbeat

    # UI stale => restart chromium
    UI_AGE="$(best_heartbeat_age "$UI_HEARTBEAT_FILE" "$UI_HEARTBEAT_COMPAT_RUN")"
    if [ "$ELAPSED" -gt "$HEARTBEAT_GRACE" ] && [ "$UI_AGE" -gt "$UI_HEARTBEAT_MAX_AGE" ]; then
      log "[Meadow] UI heartbeat stale (${UI_AGE}s) — restarting Chromium"
      kill "$CHROME_PID" 2>/dev/null || true
      break
    fi

    # WP stale => restart chromium so we relaunch to offline screen next loop
    WP_AGE="$(best_heartbeat_age "$WP_HEARTBEAT_FILE" "$WP_HEARTBEAT_COMPAT_RUN")"
    if [ "$ELAPSED" -gt "$HEARTBEAT_GRACE" ] && [ "$WP_AGE" -gt "$WP_HEARTBEAT_MAX_AGE" ] && [ "$URL" != "$OFFLINE_URL" ]; then
      log "[Meadow] WP heartbeat stale (${WP_AGE}s) — restarting to offline screen"
      kill "$CHROME_PID" 2>/dev/null || true
      break
    fi

    sleep "$WATCH_INTERVAL"
  done

  wait "$CHROME_PID" 2>/dev/null || true

  touch_restart
  CNT="$(restart_count)"

  if [ "$CNT" -gt "$MAX_RESTARTS_IN_WINDOW" ]; then
    log "[Meadow] Too many restarts (${CNT} in ${RESTART_WINDOW_SECS}s) — setting STOP_FLAG and exiting"
    echo "$(date -Is 2>/dev/null || date)" > "$STOP_FLAG" 2>/dev/null || true
    exit 0
  fi

  log "[Meadow] Chromium exited — backoff ${BACKOFF}s (restart count ${CNT}/${MAX_RESTARTS_IN_WINDOW})"
  sleep "$BACKOFF"

  BACKOFF=$((BACKOFF * 2))
  if [ "$BACKOFF" -gt "$BACKOFF_MAX" ]; then
    BACKOFF="$BACKOFF_MAX"
  fi

  # If UI is healthy again, reset backoff
  UI_AGE="$(best_heartbeat_age "$UI_HEARTBEAT_FILE" "$UI_HEARTBEAT_COMPAT_RUN")"
  if [ "$UI_AGE" -le "$UI_HEARTBEAT_MAX_AGE" ]; then
    BACKOFF="$BACKOFF_START"
  fi
done
