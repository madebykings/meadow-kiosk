#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
  dbus-x11 \
  modemmanager

echo "=== Ensure Meadow home ==="
sudo mkdir -p /home/meadow
sudo chown -R meadow:meadow /home/meadow

echo "=== Stop existing Meadow services (clear down) ==="
sudo systemctl stop meadow-kiosk-browser.service 2>/dev/null || true
sudo systemctl stop meadow-launcher.service 2>/dev/null || true
sudo systemctl stop meadow-remote-control.service 2>/dev/null || true
sudo systemctl stop meadow-kiosk.service 2>/dev/null || true

echo "=== Kill any stray processes (belt + braces) ==="
sudo pkill -f "/home/meadow/kiosk-launcher.py" 2>/dev/null || true
sudo pkill -f "/home/meadow/kiosk-browser.sh" 2>/dev/null || true
sudo pkill -f "chromium.*--kiosk" 2>/dev/null || true
sudo pkill -f "chromium-browser.*--kiosk" 2>/dev/null || true

echo "=== Deploy launcher + kiosk scripts ==="
sudo mkdir -p /home/meadow/meadow-kiosk
sudo cp -f "$SCRIPT_DIR/kiosk-browser.sh" /home/meadow/kiosk-browser.sh
sudo cp -f "$SCRIPT_DIR/kiosk-launcher.py" /home/meadow/kiosk-launcher.py
sudo cp -f "$SCRIPT_DIR/exit-kiosk.sh" /home/meadow/exit-kiosk.sh
sudo cp -f "$SCRIPT_DIR/offline.html" /home/meadow/offline.html

# Remote control lives inside the repo folder on the Pi
sudo cp -f "$SCRIPT_DIR/remote_control.py" /home/meadow/meadow-kiosk/remote_control.py

sudo chmod +x /home/meadow/kiosk-browser.sh /home/meadow/exit-kiosk.sh
sudo chmod +x /home/meadow/kiosk-launcher.py
sudo chmod 644 /home/meadow/offline.html /home/meadow/meadow-kiosk/remote_control.py || true

echo "=== Ensure state directory ==="
sudo mkdir -p /home/meadow/state
sudo chown -R meadow:meadow /home/meadow/state

echo "=== Configure xbindkeys hotkey (Ctrl+Alt+E) ==="
cat <<'EOF' | sudo tee /home/meadow/.xbindkeysrc >/dev/null
# Exit kiosk mode anytime
"/home/meadow/exit-kiosk.sh"
  control+alt + e
EOF
sudo chown meadow:meadow /home/meadow/.xbindkeysrc

echo "=== Desktop icons (Enter/Exit kiosk) ==="
sudo mkdir -p /home/meadow/Desktop
sudo cp -f "$SCRIPT_DIR/desktop/enter-kiosk.desktop" /home/meadow/Desktop/Enter\ Meadow\ Kiosk.desktop
sudo cp -f "$SCRIPT_DIR/desktop/exit-kiosk.desktop" /home/meadow/Desktop/Exit\ Meadow\ Kiosk.desktop
sudo chmod +x /home/meadow/Desktop/Enter\ Meadow\ Kiosk.desktop /home/meadow/Desktop/Exit\ Meadow\ Kiosk.desktop
sudo chown -R meadow:meadow /home/meadow/Desktop

echo "=== Install update helper (remote 'update_code' command) ==="
sudo cp -f "$SCRIPT_DIR/update-meadow.sh" /home/meadow/update-meadow.sh
sudo chmod +x /home/meadow/update-meadow.sh
sudo chown meadow:meadow /home/meadow/update-meadow.sh

echo "=== Install systemd unit files (source of truth) ==="
sudo install -m 644 "$SCRIPT_DIR/systemd/meadow-kiosk.service" /etc/systemd/system/meadow-kiosk.service
sudo install -m 644 "$SCRIPT_DIR/systemd/meadow-remote-control.service" /etc/systemd/system/meadow-remote-control.service
sudo install -m 644 "$SCRIPT_DIR/systemd/meadow-kiosk-browser.service" /etc/systemd/system/meadow-kiosk-browser.service
sudo install -m 644 "$SCRIPT_DIR/systemd/meadow-launcher.service" /etc/systemd/system/meadow-launcher.service

echo "=== Sudoers: allow remote_control to start/stop services without password ==="
# remote_control runs as user meadow and needs to call sudo systemctl start/stop/restart
# This is locked to systemctl for the specific Meadow units only.
cat <<'EOF' | sudo tee /etc/sudoers.d/meadow-kiosk-control >/dev/null
meadow ALL=(root) NOPASSWD: /bin/systemctl start meadow-kiosk-browser.service, /bin/systemctl stop meadow-kiosk-browser.service, /bin/systemctl restart meadow-kiosk-browser.service, /bin/systemctl start meadow-launcher.service, /bin/systemctl stop meadow-launcher.service, /bin/systemctl restart meadow-launcher.service, /bin/systemctl restart meadow-remote-control.service, /bin/systemctl restart meadow-kiosk.service
EOF
sudo chmod 440 /etc/sudoers.d/meadow-kiosk-control

echo "=== Reload systemd + enable services ==="
sudo systemctl daemon-reload

sudo systemctl enable meadow-kiosk.service
sudo systemctl enable meadow-remote-control.service
sudo systemctl enable meadow-launcher.service
sudo systemctl enable meadow-kiosk-browser.service

echo "=== Start core services (leave kiosk-browser stopped by default) ==="
sudo systemctl restart meadow-kiosk.service
sudo systemctl restart meadow-remote-control.service
sudo systemctl restart meadow-launcher.service

echo "=== Install complete. Reboot recommended. ==="
