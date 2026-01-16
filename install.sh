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

echo "=== Stop any legacy Meadow units if present (safe no-op if missing) ==="
for u in meadow-kiosk-browser meadow-launcher meadow-remote-control meadow-vend-poller; do
  sudo systemctl stop "${u}.service" 2>/dev/null || true
  sudo systemctl disable "${u}.service" 2>/dev/null || true
done

echo "=== Kill any stray kiosk processes (belt + braces) ==="
sudo pkill -f "/home/meadow/kiosk-launcher.py" 2>/dev/null || true
sudo pkill -f "/home/meadow/kiosk-browser.sh" 2>/dev/null || true
sudo pkill -f "remote_control.py" 2>/dev/null || true
sudo pkill -f "chromium.*--kiosk" 2>/dev/null || true
sudo pkill -f "chromium-browser.*--kiosk" 2>/dev/null || true

echo "=== Deploy scripts/assets (optional but useful) ==="
sudo mkdir -p /home/meadow/meadow-kiosk
sudo cp -f "$SCRIPT_DIR/kiosk-browser.sh" /home/meadow/kiosk-browser.sh
sudo cp -f "$SCRIPT_DIR/exit-kiosk.sh" /home/meadow/exit-kiosk.sh
sudo cp -f "$SCRIPT_DIR/offline.html" /home/meadow/offline.html
sudo chmod +x /home/meadow/kiosk-browser.sh /home/meadow/exit-kiosk.sh
sudo chmod 644 /home/meadow/offline.html || true
sudo chown meadow:meadow /home/meadow/kiosk-browser.sh /home/meadow/exit-kiosk.sh /home/meadow/offline.html || true

echo "=== Ensure state directory ==="
sudo mkdir -p /home/meadow/state
sudo chown -R meadow:meadow /home/meadow/state

echo "=== Configure xbindkeys hotkey (Ctrl+Alt+E) ==="
cat <<'EOF' | sudo tee /home/meadow/.xbindkeysrc >/dev/null
"/home/meadow/exit-kiosk.sh"
  control+alt + e
EOF
sudo chown meadow:meadow /home/meadow/.xbindkeysrc

echo "=== Desktop icon (Exit kiosk) ==="
sudo mkdir -p /home/meadow/Desktop
sudo cp -f "$SCRIPT_DIR/desktop/exit-kiosk.desktop" /home/meadow/Desktop/Exit\ Meadow\ Kiosk.desktop
sudo chmod +x /home/meadow/Desktop/Exit\ Meadow\ Kiosk.desktop
sudo chown -R meadow:meadow /home/meadow/Desktop

echo "=== Install update helper (optional) ==="
sudo cp -f "$SCRIPT_DIR/update-meadow.sh" /home/meadow/update-meadow.sh
sudo chmod +x /home/meadow/update-meadow.sh
sudo chown meadow:meadow /home/meadow/update-meadow.sh

echo "=== Install ONLY meadow-kiosk.service ==="
sudo install -m 644 "$SCRIPT_DIR/systemd/meadow-kiosk.service" /etc/systemd/system/meadow-kiosk.service

echo "=== Remove legacy Meadow unit files from /etc/systemd/system if present ==="
sudo rm -f /etc/systemd/system/meadow-remote-control.service || true
sudo rm -f /etc/systemd/system/meadow-launcher.service || true
sudo rm -f /etc/systemd/system/meadow-kiosk-browser.service || true
sudo rm -f /etc/systemd/system/meadow-vend-poller.service || true

echo "=== Reload systemd ==="
sudo systemctl daemon-reload

echo "=== Sudoers for admin endpoints (restart kiosk, reboot/shutdown) ==="
cat <<'EOF' | sudo tee /etc/sudoers.d/meadow-kiosk-control >/dev/null
meadow ALL=(root) NOPASSWD: /bin/systemctl start meadow-kiosk.service, /bin/systemctl stop meadow-kiosk.service, /bin/systemctl restart meadow-kiosk.service, /sbin/reboot, /sbin/shutdown
EOF
sudo chmod 440 /etc/sudoers.d/meadow-kiosk-control

echo "=== Enable + start meadow-kiosk.service ==="
sudo systemctl enable meadow-kiosk.service
sudo systemctl restart meadow-kiosk.service

echo "=== Install complete. Reboot recommended. ==="
