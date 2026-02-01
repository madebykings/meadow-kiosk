#!/bin/bash
set -euo pipefail

# Tuned for Meadow: ads rotate every ~10s, videos mean screen should change frequently.
INTERVAL="${MEADOW_WD_INTERVAL:-17}"      # seconds between samples (avoid 10s alignment)
JITTER="${MEADOW_WD_JITTER:-3}"           # random 0..JITTER added to interval
WINDOW="${MEADOW_WD_WINDOW:-180}"         # seconds in decision window (3 min)
MIN_UNIQUE="${MEADOW_WD_MIN_UNIQUE:-2}"   # must see at least 2 distinct frames in WINDOW
COOLDOWN="${MEADOW_WD_COOLDOWN:-30}"      # seconds to chill after a restart

# Simple rate limit: max 3 restarts per hour
RATE_WINDOW="${MEADOW_WD_RATE_WINDOW:-3600}"
MAX_RESTARTS="${MEADOW_WD_MAX_RESTARTS:-3}"
RATE_LOG="${MEADOW_WD_RATE_LOG:-/run/meadow/watchdog_restart_times}"

SAMPLES=$(( (WINDOW + INTERVAL - 1) / INTERVAL ))

declare -a RING=()
IDX=0
FILLED=0

mkdir -p /run/meadow 2>/dev/null || true

rate_touch() {
  local now
  now="$(date +%s)"
  echo "$now" >> "$RATE_LOG" 2>/dev/null || true
  # keep only within window
  awk -v now="$now" -v win="$RATE_WINDOW" 'now-$1 <= win {print $1}' "$RATE_LOG" > "${RATE_LOG}.tmp" 2>/dev/null || true
  mv -f "${RATE_LOG}.tmp" "$RATE_LOG" 2>/dev/null || true
}

rate_count() {
  [ -f "$RATE_LOG" ] || { echo 0; return; }
  wc -l < "$RATE_LOG" 2>/dev/null | tr -d ' '
}

while true; do
  # Only act if kiosk Chromium is actually running
  if ! pgrep -f "chromium.*--kiosk" >/dev/null; then
    RING=(); IDX=0; FILLED=0
    sleep "$INTERVAL"
    continue
  fi

  # Take screenshot to tmpfs, hash, delete (no disk bloat)
  if ! sudo -u meadow XDG_RUNTIME_DIR=/run/user/1000 grim /run/meadow_screen.png 2>/dev/null; then
    sleep "$INTERVAL"
    continue
  fi

  CUR="$(sha256sum /run/meadow_screen.png | awk '{print $1}')"
  rm -f /run/meadow_screen.png

  RING[$IDX]="$CUR"
  IDX=$(( (IDX + 1) % SAMPLES ))
  [ "$FILLED" -lt "$SAMPLES" ] && FILLED=$((FILLED + 1))

  if [ "$FILLED" -eq "$SAMPLES" ]; then
    UNIQUE="$(printf "%s\n" "${RING[@]}" | sort -u | wc -l | tr -d ' ')"
    if [ "$UNIQUE" -lt "$MIN_UNIQUE" ]; then
      CNT="$(rate_count)"
      if [ "$CNT" -ge "$MAX_RESTARTS" ]; then
        echo "$(date -Is) [WATCHDOG] Frozen but rate-limited (${CNT}/${MAX_RESTARTS} in ${RATE_WINDOW}s) — not restarting"
        RING=(); IDX=0; FILLED=0
        sleep "$COOLDOWN"
        continue
      fi

      echo "$(date -Is) [WATCHDOG] Screen frozen (~${WINDOW}s, unique=${UNIQUE}) — restarting Chromium"
      rate_touch
      pkill -f "chromium.*--kiosk" || true
      RING=(); IDX=0; FILLED=0
      sleep "$COOLDOWN"
      continue
    fi
  fi

  sleep $(( INTERVAL + (RANDOM % (JITTER+1)) ))
done
