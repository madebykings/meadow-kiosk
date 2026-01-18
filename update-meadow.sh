#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/home/meadow/meadow-kiosk"
BRANCH="${1:-main}"
LOG="${REPO_DIR}/state/update.log"

mkdir -p "${REPO_DIR}/state" || true
exec >>"$LOG" 2>&1

echo "---- $(date -Is) UPDATE START (branch=$BRANCH) ----"

# Stop kiosk/browser loop to avoid thrash during update
mkdir -p /run/meadow || true
touch /run/meadow/kiosk_stop || true
touch /tmp/meadow_kiosk_stop || true

sudo systemctl stop meadow-kiosk.service || true

cd "$REPO_DIR"
git fetch --all --prune
git reset --hard "origin/${BRANCH}"

if [ -f requirements.txt ]; then
  python3 -m pip install --upgrade pip || true
  python3 -m pip install -r requirements.txt || true
fi

sudo systemctl daemon-reload || true
sudo systemctl start meadow-kiosk.service || true

rm -f /run/meadow/kiosk_stop || true
rm -f /tmp/meadow_kiosk_stop || true

echo "---- $(date -Is) UPDATE DONE ----"
