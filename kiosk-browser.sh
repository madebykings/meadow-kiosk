#!/bin/bash
set -euo pipefail

# ------------------------------------------------------------
# Meadow kiosk-browser.sh
# ------------------------------------------------------------

ROOT="${MEADOW_ROOT:-/home/meadow/meadow-kiosk}"
STATE_DIR="${MEADOW_STATE_DIR:-${ROOT}/state}"
LOG_FILE="${MEADOW_KIOSK_LOG_FILE:-${STATE_DIR}/kiosk-browser.log}"
CHROMIUM_LOG="${MEADOW_CHROMIUM_LOG:-${STATE_DIR}/chromium.log}"

URL_FILE="${MEADOW_KIOSK_URL_FILE:-${ROOT}/kiosk.url}"
DEFAULT_URL="${MEADOW_DEFAULT_URL:-about:blank}"
OFFLINE_URL="${MEADOW_OFFLINE_URL:-file://${ROOT}/offline.html}"

STOP_FLAG_RUN="${MEADOW_KIOSK_STOP_FLAG:-/run/meadow/kiosk_stop}"
STOP_FLAG_TMP="/tmp/meadow_kiosk_stop"

UI_HEARTBEAT_RUN="${MEADOW_UI_HEARTBEAT_FILE:-/run/meadow/ui_heartbeat}"
WP_HEARTBEAT_RUN="${MEADOW_WP_HEARTBEAT_FILE:-/run/meadow/wp_heartbeat}"
UI_HEARTBEAT_TMP="/tmp/meadow_ui_heartbeat"
WP_HEARTBEAT_TMP="/tmp/meadow_wp_heartbeat"

KIOSK_PIDFILE="${MEADOW_KIOSK_PIDFILE:-/run/meadow/kiosk_browser.pid}"
RESTART_LOG="${MEADOW_RESTART_LOG:-/run/meadow/kiosk_restart_times"

UI_HEARTBEAT_MAX_AGE="${MEADOW_UI_HEARTBEAT_MAX_AGE:-45}"
WP_HEARTBEAT_MAX_AGE="${MEADOW_WP_HEARTBEAT_MAX_AGE:-180}"
HEARTBEAT_GRACE="${MEADOW_HEARTBEAT_GRACE:-120}"
WATCH_INTERVAL="${MEADOW_WATCH_INTERVAL:-5}"

RESTART_WINDOW_SECS="${MEADOW_RESTART_WINDOW_SECS:-600}"
MAX_RESTARTS_IN_WINDOW="${MEADOW_MAX_RESTARTS_IN_WINDOW:-10}"
BACKOFF_START="${MEADOW_BACKOFF_START:-2}"
BACKOFF_MAX="${MEADOW_BACKOFF_MAX:-60}"

DAEMON_HEARTBEAT_URL="${MEADOW_DAEMON_HEARTBEAT_URL:-http://127.0.0.1:8765/heartbeat}"
CHROME_PROFILE="${MEADOW_CHROME_PROFILE:-${STATE_DIR}/chrome-profile}"

log() {
  local msg="$1"
  local ts
  ts="$(date -Is 2>/dev/null || date)"
  mkdir -p "${STATE_DIR}" 2>/dev/null || true
  echo "${ts} ${msg}" >> "${LOG_FILE}" 2>/dev/null || true
  echo "${msg}" >&2
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

  # If it looks like a page path and doesn't end with /, add /
  # (avoid 301 churn, especially for /kiosk1)
  if [[ "$url" =~ ^https?:// ]] && [[ "$url" != *"."* ]] && [[ "$url" != */ ]]; then
    url="${url}/"
  fi

  echo "$url"
}

ensure_dirs() {
  mkdir -p /run/meadow 2>/dev/null || true
  mkdir -p "${STATE_DIR}" 2>/dev/null || true
  mkdir -p "$(dirname "${KIOSK_PIDFILE}")" 2>/dev/null || true
  mkdir -p "$(dirname "${RESTART_LOG}")" 2>/dev/null || true
  mkdir -p "${CHROME_PROFILE}" 2>/dev/null || true
}

choose_heartbeat_file() {
  local run="$1"
  local tmp="$2"
  if [ -f "$run" ]; then echo "$run"; return; fi
  if [ -f "$tmp" ]; then echo "$tmp"; return; fi
  echo ""
}

write_pid() {
  ensure_dirs
  echo "$$" > "${KIOSK_PIDFILE}" 2>/dev/null || true
}

record_restart() {
  ensure_dirs
  echo "$(date +%s)" >> "${RESTART_LOG}" 2>/dev/null || true
  tail -n 200 "${RESTART_LOG}" 2>/dev/null > "${RESTART_LOG}.tmp" || true
  mv -f "${RESTART_LOG}.tmp" "${RESTART_LOG}" 2>/dev/null || true
}

restart_count_in_window() {
  local now cutoff
  now="$(date +%s)"
  cutoff=$((now - RESTART_WINDOW_SECS))
  if [ ! -f "${RESTART_LOG}" ]; then
    echo 0
    return
  fi
  awk -v c="$cutoff" '$1 >= c {n++} END{print n+0}' "${RESTART_LOG}" 2>/dev/null || echo 0
}

pkill_chromium() {
  pkill -f "chromium.*--kiosk" 2>/dev/null || true
  pkill -f "chromium-browser.*--kiosk" 2>/dev/null || true
}

chrome_bin() {
  if command -v chromium-browser >/dev/null 2>&1; then
    echo "chromium-browser"
  else
    echo "chromium"
  fi
}

main_loop() {
  ensure_dirs
  write_pid

  local CHROME_BIN
  CHROME_BIN="$(chrome_bin)"

  local backoff="${BACKOFF_START}"
  local url

  while true; do
    if _stop_flag_present; then
      log "[Meadow] STOP_FLAG present — exiting kiosk-browser.sh"
      pkill_chromium
      exit 0
    fi

    url="$(get_url)"

    # Decide offline vs online based on WP heartbeat staleness
    local hb_file hb_age
    hb_file="$(choose_heartbeat_file "${WP_HEARTBEAT_RUN}" "${WP_HEARTBEAT_TMP}")"
    hb_age="$(_age_of_file "${hb_file}")"

    if [ "${hb_age}" -gt "${WP_HEARTBEAT_MAX_AGE}" ]; then
      url="${OFFLINE_URL}"
    fi

    log "----- $(date -Is) launching chromium url=${url} -----"
    echo "----- $(date -Is) launching chromium url=${url} -----" >> "${CHROMIUM_LOG}" 2>/dev/null || true

    record_restart
    local n
    n="$(restart_count_in_window)"
    if [ "${n}" -gt "${MAX_RESTARTS_IN_WINDOW}" ]; then
      log "[Meadow] Too many restarts (${n} in ${RESTART_WINDOW_SECS}s) — stopping kiosk"
      touch "${STOP_FLAG_RUN}" 2>/dev/null || true
      touch "${STOP_FLAG_TMP}" 2>/dev/null || true
      pkill_chromium
      exit 0
    fi

    # Launch chromium (kiosk)
    "${CHROME_BIN}" \
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
      --user-data-dir="${CHROME_PROFILE}" \
      "${url}" >> "${CHROMIUM_LOG}" 2>&1 || true

    log "[Meadow] Chromium exited — backoff ${backoff}s"
    sleep "${backoff}" || true
    backoff=$((backoff * 2))
    if [ "${backoff}" -gt "${BACKOFF_MAX}" ]; then backoff="${BACKOFF_MAX}"; fi
  done
}

main_loop
