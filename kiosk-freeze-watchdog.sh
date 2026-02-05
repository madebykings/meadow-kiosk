#!/bin/bash
set -euo pipefail

echo "$(date -Is) [WATCHDOG] start pid=$$"

# Tuned for Meadow: ads rotate every ~10s, videos mean screen should change frequently.
INTERVAL="${MEADOW_WD_INTERVAL:-17}"      # seconds between samples (avoid 10s alignment)
JITTER="${MEADOW_WD_JITTER:-3}"           # random 0..JITTER added to interval
WINDOW="${MEADOW_WD_WINDOW:-180}"         # seconds in decision window
MIN_UNIQUE="${MEADOW_WD_MIN_UNIQUE:-2}"   # must see at least 2 distinct frames in WINDOW
COOLDOWN="${MEADOW_WD_COOLDOWN:-30}"      # seconds to chill after a restart

# Simple rate limit: max 3 restarts per hour
RATE_WINDOW="${MEADOW_WD_RATE_WINDOW:-3600}"
MAX_RESTARTS="${MEADOW_WD_MAX_RESTARTS:-3}"
RATE_LOG="${MEADOW_WD_RATE_LOG:-/run/meadow/watchdog_restart_times}"

# -----------------------------
# Network self-heal (Pi 5 / NetworkManager)
# -----------------------------
NET_FAIL_LIMIT="${MEADOW_NET_FAIL_LIMIT:-4}"     # consecutive failures before recovery
NET_COOLDOWN="${MEADOW_NET_COOLDOWN:-120}"       # seconds between recovery actions
NET_FAILS=0
NET_LAST_ACTION=0

ENTER_KIOSK="${MEADOW_ENTER_KIOSK:-/home/meadow/meadow-kiosk/enter-kiosk.sh}"

SAMPLES=$(( (WINDOW + INTERVAL - 1) / INTERVAL ))

declare -a RING=()
IDX=0
FILLED=0

# Ensure runtime dir is usable even if created root-owned at boot
mkdir -p /run/meadow 2>/dev/null || true
chmod 0775 /run/meadow 2>/dev/null || true
chown meadow:meadow /run/meadow 2>/dev/null || true

SCREEN="/run/meadow/meadow_screen.png"

rate_touch() {
  local now
  now="$(date +%s)"
  echo "$now" >> "$RATE_LOG" 2>/dev/null || true
  awk -v now="$now" -v win="$RATE_WINDOW" 'now-$1 <= win {print $1}' "$RATE_LOG" > "${RATE_LOG}.tmp" 2>/dev/null || true
  mv -f "${RATE_LOG}.tmp" "$RATE_LOG" 2>/dev/null || true
}

rate_count() {
  [ -f "$RATE_LOG" ] || { echo 0; return; }
  wc -l < "$RATE_LOG" 2>/dev/null | tr -d ' '
}

has_outbound() {
  # IP + DNS: both must succeed
  ping -c 1 -W 2 1.1.1.1 >/dev/null 2>&1 || return 1
  ping -c 1 -W 2 google.com >/dev/null 2>&1 || return 1
  return 0
}

net_cooldown_ok() {
  local now
  now="$(date +%s)"
  [ $((now - NET_LAST_ACTION)) -ge "$NET_COOLDOWN" ]
}

recover_network() {
  NET_LAST_ACTION="$(date +%s)"
  echo "$(date -Is) [WATCHDOG] Network recovery starting (cloudflared -> NM bounce if needed -> restart services -> enter kiosk)"

  # 1) low-impact first: restart cloudflared (fixes CF 1033 when wedged)
  systemctl restart cloudflared >/dev/null 2>&1 || true
  sleep 6

  # 2) if still no outbound, bounce NetworkManager networking
  if ! has_outbound; then
    if systemctl is-active --quiet NetworkManager; then
      echo "$(date -Is) [WATCHDOG] Outbound still down; bouncing NetworkManager networking off/on"
      nmcli networking off >/dev/null 2>&1 || true
      sleep 5
      nmcli networking on >/dev/null 2>&1 || true
      sleep 8
    else
      echo "$(date -Is) [WATCHDOG] NetworkManager not active; skipping nmcli bounce"
    fi

    # restart cloudflared again after network bounce
    systemctl restart cloudflared >/dev/null 2>&1 || true
    sleep 4
  fi

  # 3) restart Meadow stack backend
  echo "$(date -Is) [WATCHDOG] Restarting meadow-kiosk.service"
  systemctl restart meadow-kiosk.service >/dev/null 2>&1 || true

  # You asked for this (service can take a bit)
  sleep 10

  # 4) force kiosk UI
  if [ -x "$ENTER_KIOSK" ]; then
    echo "$(date -Is) [WATCHDOG] Entering kiosk UI via enter-kiosk.sh"
    sudo -u meadow bash "$ENTER_KIOSK" >/dev/null 2>&1 || true
  else
    echo "$(date -Is) [WATCHDOG] WARNING: enter-kiosk.sh not executable at $ENTER_KIOSK"
  fi
}

while true; do
  # -----------------------------
  # Network check (runs regardless of Chromium state)
  # -----------------------------
  if has_outbound; then
    NET_FAILS=0
  else
    NET_FAILS=$((NET_FAILS + 1))
    echo "$(date -Is) [WATCHDOG] outbound check failed (${NET_FAILS}/${NET_FAIL_LIMIT})"

    if [ "$NET_FAILS" -ge "$NET_FAIL_LIMIT" ] && net_cooldown_ok; then
      NET_FAILS=0
      recover_network
      # small chill to avoid immediate re-trigger
      sleep 5
    fi
  fi

  # Only act if kiosk Chromium is actually running
  if ! pgrep -f "chromium.*--kiosk" >/dev/null; then
    RING=(); IDX=0; FILLED=0
    echo "$(date -Is) [WATCHDOG] chromium kiosk not running; waiting"
    sleep "$INTERVAL"
    continue
  fi

  # Take screenshot, hash, delete (no disk bloat)
  if ! grim "$SCREEN" >/dev/null 2>&1; then
    echo "$(date -Is) [WATCHDOG] grim failed; waiting"
    sleep "$INTERVAL"
    continue
  fi

  CUR="$(sha256sum "$SCREEN" | awk '{print $1}')"
  rm -f "$SCREEN"

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

  chrome_pid='?'
  if [ -r /run/meadow/kiosk_browser.pid ]; then
    chrome_pid="$(cat /run/meadow/kiosk_browser.pid 2>/dev/null || true)"
    [ -n "$chrome_pid" ] || chrome_pid='?'
  fi

  echo "$(date -Is) [WATCHDOG] tick chrome_pid=$chrome_pid filled=$FILLED/$SAMPLES"
  sleep $(( INTERVAL + (RANDOM % (JITTER+1)) ))
done
