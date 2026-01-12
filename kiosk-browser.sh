#!/bin/bash
set -e

URL_FILE="/home/meadow/kiosk.url"
DEFAULT_URL="about:blank"

get_url() {
  if [ -f "$URL_FILE" ]; then
    head -n 1 "$URL_FILE" | tr -d '\r\n'
  else
    echo "$DEFAULT_URL"
  fi
}

# Small delay to let X start
sleep 2

while true; do
  URL="$(get_url)"
  if [ -z "$URL" ]; then
    URL="$DEFAULT_URL"
  fi

  # Chromium kiosk flags (tune as needed)
  chromium-browser \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --disable-features=TranslateUI \
    --overscroll-history-navigation=0 \
    "$URL" || true

  sleep 1
done
