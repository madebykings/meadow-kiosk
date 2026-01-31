#!/bin/bash
set -euo pipefail

INTERVAL=17
JITTER=3
WINDOW=180
MIN_UNIQUE=2
COOLDOWN=30

SAMPLES=$(( (WINDOW + INTERVAL - 1) / INTERVAL ))

declare -a RING=()
IDX=0
FILLED=0

while true; do
  # Only act if kiosk Chromium is running
  if ! pgrep -f "chromium.*--kiosk" >/dev/null; then
    RING=(); IDX=0; FILLED=0
    sleep "$INTERVAL"
    continue
  fi

  # Screenshot → hash → delete (RAM only)
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
    UNIQUE=$(printf "%s\n" "${RING[@]}" | sort -u | wc -l | tr -d ' ')
    if [ "$UNIQUE" -lt "$MIN_UNIQUE" ]; then
      echo "$(date -Is) [WATCHDOG] Screen frozen (~${WINDOW}s, unique=${UNIQUE}) — restarting Chromium"
      pkill -f "chromium.*--kiosk" || true
      RING=(); IDX=0; FILLED=0
      sleep "$COOLDOWN"
      continue
    fi
  fi

  sleep $(( INTERVAL + (RANDOM % (JITTER+1)) ))
done
