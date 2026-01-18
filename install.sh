#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="/home/meadow/meadow-kiosk"
MEADOW_USER="meadow"
MEADOW_GROUP="meadow"

echo "=== Meadow install.sh (canonical path: ${TARGET_DIR}) ==="

echo "=== Updating system ==="
sudo apt update
sudo apt upgrade -y

echo "=== Installing system dependencies ==="
sudo apt install -y \
  git \
  python3 \
  python3-serial \
  python3-rpi-lgpio \
  python3-tk \
  chromium \
  unclutter \
  xbindkeys \
  xdotool \
  dbus-x11 \
  modemmanager

echo "=== Ensure Meadow home ==="
sudo mkdir -p /home/meadow
sudo chown -R "${MEADOW_USER}:${MEADOW_GROUP}" /home/meadow

echo "=== Ensure canonical repo location exists ==="
sudo mkdir -p "${TARGET_DIR}"
sudo chown -R "${MEADOW_USER}:${MEADOW_GROUP}" "${TARGET_DIR}"

# If the install script is being run from somewhere else, sync the repo contents into TARGET_DIR.
# This avoids the old pattern of copying single files into /home/meadow/.
if [ "${SCRIPT_DIR}" != "${TARGET_DIR}" ]; then
  echo "=== Syncing repo into ${TARGET_DIR} (from ${SCRIPT_DIR}) ==="
  sudo rsync -a --delete --exclude '.git' "${SCRIPT_DIR}/" "${TARGET_DIR}/"
  sudo chown -R "${MEADOW_USER}:${MEADOW_GROUP}" "${TARGET_DIR}"
else
  echo "=== Running from ${TARGET_DIR} (no repo sync needed) ==="
fi

echo "=== Kill any stray kiosk processes (belt + braces) ==="
# New canonical paths
sudo pkill -f "${TARGET_DIR}/kiosk-browser\.sh" 2>/dev/null || true
sudo pkill -f "kiosk-browser\.sh" 2>/dev/null || true
sudo pkill -f "chromium.*--kiosk" 2>/dev/null || true
sudo pkill -f "chromium-browser.*--kiosk" 2>/dev/null || true

# Also kill legacy paths if they exist (from older installs)
sudo pkill -f "/home/meadow/kiosk-browser\.sh" 2>/dev/null || true

echo "=== Ensure runtime + state directories ==="
# Runtime dir used by kiosk-browser.sh (preferred)
sudo mkdir -p /run/meadow
sudo chown "${MEADOW_USER}:${MEADOW_GROUP}" /run/meadow

# State dir lives inside the repo (single source of truth)
sudo mkdir -p "${TARGET_DIR}/state"
sudo chown -R "${MEADOW_USER}:${MEADOW_GROUP}" "${TARGET_DIR}/state"

echo "=== Ensure executable bits on repo scripts ==="
sudo chmod 755 \
  "${TARGET_DIR}/kiosk-browser.sh" \
  "${TARGET_DIR}/enter-kiosk.sh" \
  "${TARGET_DIR}/exit-kiosk.sh" \
  "${TARGET_DIR}/update-meadow.sh" 2>/dev/null || true

# offline.html should be readable
sudo chmod 644 "${TARGET_DIR}/offline.html" 2>/dev/null || true
sudo chown -R "${MEADOW_USER}:${MEADOW_GROUP}" "${TARGET_DIR}"

echo "=== Install enter/exit wrappers (canonical) ==="
# Overwrite enter-kiosk.sh + exit-kiosk.sh with known-good versions that use canonical paths.
# (This avoids any drift from older installs.)
cat <<'EOF' | sudo tee "${TARGET_DIR}/enter-kiosk.sh" >/dev/null
#!/bin/bash
set -euo pipefail

ROOT="/home/meadow/meadow-kiosk"
STOP_FLAG_RUN="/run/meadow/kiosk_stop"
STOP_FLAG_TMP="/tmp/meadow_kiosk_stop"
URL_FILE="${ROOT}/kiosk.url"
STATE_DIR="${ROOT}/state"

mkdir -p "$STATE_DIR" 2>/dev/null || true

# Normalize URL to avoid redirect churn
URL="https://meadowvending.com/kiosk1/"
if [ -f "$URL_FILE" ]; then
  CANDIDATE="$(head -n 1 "$URL_FILE" | tr -d '\r\n' || true)"
  if [ -n "${CANDIDATE:-}" ]; then
    URL="$CANDIDATE"
  fi
fi
if [ "$URL" = "https://meadowvending.com/kiosk1" ]; then
  URL="https://meadowvending.com/kiosk1/"
fi
echo "$URL" > "$URL_FILE"

# Ensure /run/meadow exists
mkdir -p /run/meadow 2>/dev/null || true
chown meadow:meadow /run/meadow 2>/dev/null || true

# Make "Enter" behave like the proven-good sequence: exit then enter
bash "${ROOT}/exit-kiosk.sh" >/dev/null 2>&1 || true
sleep 0.4

# Clear stop flags so kiosk loop can run
rm -f "$STOP_FLAG_RUN" 2>/dev/null || true
rm -f "$STOP_FLAG_TMP" 2>/dev/null || true

# Display env (important when launched via systemd / API)
export DISPLAY=":0"
export XAUTHORITY="/home/meadow/.Xauthority"
export XDG_RUNTIME_DIR="/run/user/1000"

nohup bash "${ROOT}/kiosk-browser.sh" >>"${STATE_DIR}/kiosk-enter.log" 2>&1 &
exit 0
EOF

cat <<'EOF' | sudo tee "${TARGET_DIR}/exit-kiosk.sh" >/dev/null
#!/bin/bash
set -euo pipefail

ROOT="/home/meadow/meadow-kiosk"
STOP_FLAG_RUN="/run/meadow/kiosk_stop"
STOP_FLAG_TMP="/tmp/meadow_kiosk_stop"

mkdir -p /run/meadow 2>/dev/null || true
chown meadow:meadow /run/meadow 2>/dev/null || true

# Ask kiosk loop to stop
date -Is 2>/dev/null > "$STOP_FLAG_RUN" || true
touch "$STOP_FLAG_TMP" 2>/dev/null || true

# Kill kiosk loop + chromium kiosk (best-effort)
pkill -f "${ROOT}/kiosk-browser\.sh" 2>/dev/null || true
pkill -f "kiosk-browser\.sh" 2>/dev/null || true
pkill -f "chromium-browser.*--kiosk" 2>/dev/null || true
pkill -f "chromium.*--kiosk" 2>/dev/null || true

exit 0
EOF

sudo chmod 755 "${TARGET_DIR}/enter-kiosk.sh" "${TARGET_DIR}/exit-kiosk.sh"
sudo chown "${MEADOW_USER}:${MEADOW_GROUP}" "${TARGET_DIR}/enter-kiosk.sh" "${TARGET_DIR}/exit-kiosk.sh"

echo "=== Configure hotkeys (xbindkeys) ==="
cat <<'EOF' | sudo tee /home/meadow/.xbindkeysrc >/dev/null
# Exit kiosk
"/home/meadow/meadow-kiosk/exit-kiosk.sh"
control+alt + e

# Refresh current page (best-effort)
"xdotool key --clearmodifiers ctrl+r"
control + r
EOF
sudo chown "${MEADOW_USER}:${MEADOW_GROUP}" /home/meadow/.xbindkeysrc

echo "=== Autostart xbindkeys on login ==="
sudo -u "${MEADOW_USER}" mkdir -p /home/meadow/.config/autostart
cat <<'EOF' | sudo -u "${MEADOW_USER}" tee /home/meadow/.config/autostart/xbindkeys.desktop >/dev/null
[Desktop Entry]
Type=Application
Name=xbindkeys
Exec=/usr/bin/xbindkeys -f /home/meadow/.xbindkeysrc
X-GNOME-Autostart-enabled=true
EOF

echo "=== Install systemd service (Pi API) ==="
sudo install -m 644 "${TARGET_DIR}/systemd/meadow-kiosk.service" /etc/systemd/system/meadow-kiosk.service
sudo systemctl daemon-reload
sudo systemctl enable meadow-kiosk.service
sudo systemctl restart meadow-kiosk.service

echo "=== Install complete ==="
echo ""
echo "Canonical install dir: ${TARGET_DIR}"
echo "Hotkeys:"
echo "  Ctrl+Alt+E = Exit kiosk"
echo "  Ctrl+R     = Refresh"
echo ""
echo "Start kiosk:"
echo "  bash ${TARGET_DIR}/enter-kiosk.sh"
