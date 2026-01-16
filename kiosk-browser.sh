#!/bin/bash
set -euo pipefail

LOG_FILE="/home/meadow/state/kiosk-browser.log"

log() {
  local msg="$1"
  local ts
  ts="$(date -Is 2>/dev/null || date)"
  echo "${ts} ${msg}" >> "$LOG_FILE" 2>/dev/null || true
  echo "${msg}" >&2
}

URL_FILE="/home/meadow/kiosk.url"
DEFAULT_URL="about:blank"
OFFLINE_URL="file:///home/meadow/offline.html"

# -------------------------------------------------------------------
# Shared runtime paths (MUST be env-driven to avoid systemd PrivateTmp issues)
# -------------------------------------------------------------------
# Shared runtime paths (env-driven; avoids systemd PrivateTmp issues)
STOP_FLAG="${MEADOW_KIOSK_STOP_FLAG:-/tmp/meadow_kiosk_stop}"

# Heartbeats (env-driven)
UI_HEARTBEAT_FILE="${MEADOW_UI_HEARTBEAT_FILE:-/tmp/meadow_ui_heartbeat}"
WP_HEARTBEAT_FILE="${MEADOW_WP_HEARTBEAT_FILE:-/tmp/meadow_wp_heartbeat}"

KIOSK_PIDFILE="${MEADOW_KIOSK_PIDFILE:-/tmp/meadow_kiosk_browser.pid}"
RESTART_LOG="${MEADOW_RESTART_LOG:-/tmp/meadow_kiosk_restart_times}"

log "[Meadow] Using STOP_FLAG=$STOP_FLAG"
log "[Meadow] Using UI_HEARTBEAT_FILE=$UI_HEARTBEAT_FILE"
log "[Meadow] Using WP_HEARTBEAT_FILE=$WP_HEARTBEAT_FILE"
log "[Meadow] Using KIOSK_PIDFILE=$KIOSK_PIDFILE"
log "[Meadow] Using RESTART_LOG=$RESTART_LOG"

# Hung UI detection (Chromium wedged / JS stalled)
UI_HEARTBEAT_MAX_AGE="${MEADOW_UI_HEARTBEAT_MAX_AGE:-45}"

# Connectivity detection (Pi can reach WordPress)
WP_HEARTBEAT_MAX_AGE="${MEADOW_WP_HEARTBEAT_MAX_AGE:-180}"

# Grace period before enforcing heartbeats (boot / network warmup)
HEARTBEAT_GRACE="${MEADOW_HEARTBEAT_GRACE:-120}"
WATCH_INTERVAL="${MEADOW_WATCH_INTERVAL:-5}"

# Restart safety
RESTART_WINDOW_SECS="${MEADOW_RESTART_WINDOW_SECS:-600}"   # 10 min
MAX_RESTARTS_IN_WINDOW="${MEADOW_MAX_RESTARTS_IN_WINDOW:-10}"

BACKOFF_START="${MEADOW_BACKOFF_START:-2}"
BACKOFF_MAX="${MEADOW_BACKOFF_MAX:-60}"

# Local daemon heartbeat endpoint (must return 200)
DAEMON_HEARTBEAT_URL="${MEADOW_DAEMON_HEARTBEAT_URL:-http://127.0.0.1:8765/heartbeat}"

CHROMIUM_LOG="/home/meadow/state/chromium.log"

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
  if [ ! -f "$RESTART_LOG" ]; then
    echo 0
    return
  fi
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

# Bridge: if daemon responds, mark UI heartbeat as fresh
bridge_ui_heartbeat() {
  if curl -sS -m 1 -o /dev/null -X POST "$DAEMON_HEARTBEAT_URL"; then
    touch "$UI_HEARTBEAT_FILE" 2>/dev/null || true
  fi
}

# Small delay to let X/Wayland session start
sleep 2

BACKOFF="$BACKOFF_START"

while true; do
  if [ -f "$STOP_FLAG" ]; then
    log "[Meadow] STOP_FLAG present — kiosk start blocked"
    exit 0
  fi

  # Decide URL
  URL="$(get_url)"
  if [ -z "$URL" ]; then URL="$DEFAULT_URL"; fi

  WP_AGE="$(heartbeat_age "$WP_HEARTBEAT_FILE")"
  if [ -f "$WP_HEARTBEAT_FILE" ] && [ "$WP_AGE" -gt "$WP_HEARTBEAT_MAX_AGE" ]; then
    URL="$OFFLINE_URL"
  fi

  # Detect chromium binary
  CHROME_BIN="${MEADOW_CHROME_BIN:-}"
  if [ -z "$CHROME_BIN" ]; then
    CHROME_BIN="$(command -v chromium-browser 2>/dev/null || true)"
    if [ -z "$CHROME_BIN" ]; then
      CHROME_BIN="$(command -v chromium 2>/dev/null || true)"
    fi
  fi

  if [ -z "$CHROME_BIN" ]; then
    log "[Meadow] ERROR: Chromium not found. Install 'chromium' package."
    exit 1
  fi

  # Ensure heartbeat files exist (prevents huge ages on first run)
  mkdir -p "$(dirname "$UI_HEARTBEAT_FILE")" "$(dirname "$WP_HEARTBEAT_FILE")" "$(dirname "$RESTART_LOG")" "$(dirname "$KIOSK_PIDFILE")" 2>/dev/null || true
  touch "$UI_HEARTBEAT_FILE" 2>/dev/null || true
  touch "$WP_HEARTBEAT_FILE" 2>/dev/null || true

  # Clear previous chromium log header (optional)
  echo "----- $(date -Is 2>/dev/null || date) launching chromium url=$URL -----" >> "$CHROMIUM_LOG" 2>/dev/null || true

  # --- Ensure we have a display session (Pi OS desktop) ---
  export DISPLAY="${DISPLAY:-:0}"
  export XAUTHORITY="${XAUTHORITY:-/home/meadow/.Xauthority}"
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"

  # Chromium kiosk flags
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
    --disable-gpu \
    --disable-software-rasterizer \
    --disable-dev-shm-usage \
    "$URL" >> /home/meadow/state/chromium.log 2>&1 &

  CHROME_PID=$!
  echo "$CHROME_PID" > "$KIOSK_PIDFILE" 2>/dev/null || true
  START_TS=$(date +%s)

  # Watch loop while Chromium is running
  while kill -0 "$CHROME_PID" 2>/dev/null; do
    if [ -f "$STOP_FLAG" ]; then
      kill "$CHROME_PID" 2>/dev/null || true
      wait "$CHROME_PID" 2>/dev/null || true
      exit 0
    fi

    NOW=$(date +%s)
    ELAPSED=$((NOW - START_TS))

    bridge_ui_heartbeat

    UI_AGE="$(heartbeat_age "$UI_HEARTBEAT_FILE")"
    if [ "$ELAPSED" -gt "$HEARTBEAT_GRACE" ] && [ "$UI_AGE" -gt "$UI_HEARTBEAT_MAX_AGE" ]; then
      log "[Meadow] UI heartbeat stale (${UI_AGE}s) — restarting Chromium"
      kill "$CHROME_PID" 2>/dev/null || true
      break
    fi

    WP_AGE="$(heartbeat_age "$WP_HEARTBEAT_FILE")"
    if [ "$ELAPSED" -gt "$HEARTBEAT_GRACE" ] && [ "$WP_AGE" -gt "$WP_HEARTBEAT_MAX_AGE" ] && [ "$URL" != "$OFFLINE_URL" ]; then
      log "[Meadow] WP heartbeat stale (${WP_AGE}s) — switching to offline screen"
      kill "$CHROME_PID" 2>/dev/null || true
      break
    fi

    sleep "$WATCH_INTERVAL"
  done

  wait "$CHROME_PID" 2>/dev/null || true

  touch_restart
  CNT="$(restart_count)"

  if [ "$CNT" -gt "$MAX_RESTARTS_IN_WINDOW" ]; then
    log "[Meadow] Too many restarts (${CNT} in ${RESTART_WINDOW_SECS}s) — stopping kiosk and showing launcher"
    echo "$(date -Is 2>/dev/null || date)" > "$STOP_FLAG" 2>/dev/null || true

    # Guarded, detached launcher start
    if ! pgrep -f "/home/meadow/kiosk-launcher.py" >/dev/null 2>&1; then
      nohup python3 /home/meadow/kiosk-launcher.py >/dev/null 2>&1 &
    else
      log "[Meadow] launcher already running; not starting another"
    fi

    exit 0
  fi

  log "[Meadow] Chromium exited — backoff ${BACKOFF}s (restart count ${CNT}/${MAX_RESTARTS_IN_WINDOW})"
  sleep "$BACKOFF"

  BACKOFF=$((BACKOFF * 2))
  if [ "$BACKOFF" -gt "$BACKOFF_MAX" ]; then
    BACKOFF="$BACKOFF_MAX"
  fi

  UI_AGE="$(heartbeat_age "$UI_HEARTBEAT_FILE")"
  if [ "$UI_AGE" -le "$UI_HEARTBEAT_MAX_AGE" ]; then
    BACKOFF="$BACKOFF_START"
  fi
done
