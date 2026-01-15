#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/home/meadow/meadow-kiosk"
BRANCH="${1:-main}"
LOG="/home/meadow/update.log"

exec >>"$LOG" 2>&1
echo "---- $(date -Is) UPDATE START (branch=$BRANCH) ----"

# Stop kiosk/browser loop to avoid thrash during update
touch /tmp/meadow_kiosk_stop || true

# Stop services (ignore failures)
sudo systemctl stop meadow-remote-control || true
sudo systemctl stop meadow-kiosk || true

cd "$REPO_DIR"

# Ensure clean state
git fetch --all --prune
git reset --hard "origin/${BRANCH}"

# Optional Python deps
if [ -f requirements.txt ]; then
  python3 -m pip install --upgrade pip || true
  python3 -m pip install -r requirements.txt || true
fi

# Restart services
sudo systemctl daemon-reload || true
sudo systemctl start meadow-kiosk
sudo systemctl start meadow-remote-control

# Allow kiosk to run again
rm -f /tmp/meadow_kiosk_stop || true

echo "---- $(date -Is) UPDATE DONE ----"
