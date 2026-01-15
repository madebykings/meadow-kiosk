#!/bin/bash
set -euo pipefail

# Absolute path to this repo (used for copying helper scripts)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Meadow install.sh (services-based) ==="
echo "Repo: $SCRIPT_DIR"

# ------------------------------------------------------------
# 0) Clear down existing processes/services (safe re-run)
# ------------------------------------------------------------
echo "=== Clear down existing Meadow processes/services ==="

# Stop services if they exist
sudo systemctl stop meadow-kiosk-browser.service 2>/dev/null || true
sudo systemctl stop meadow-launcher.service 2>/dev/null || true
sudo systemctl stop meadow-remote-control.service 2>/dev/null || true
sudo systemctl stop meadow-kiosk.service 2>/dev/null || true

# Kill any stale processes from older installs
sudo pkill -f "/home/meadow/kiosk-browser.sh" 2>/dev/null || true
sudo pkill -f "/home/meadow/kiosk-launcher.py" 2>/dev/null || true
sudo pkill -f "chromium.*--kiosk" 2>/dev/null || true
sudo pkill -f "chromium-browser.*--kiosk" 2>/dev/null || true

# Reset kiosk stop flag (launcher-first boot)
sudo rm -f /tmp/meadow_kiosk_stop 2>/dev/null || true

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

echo "=== Deploy launcher + kiosk scripts ==="
sudo mkdir -p /home/meadow/meadow-kiosk

# Core scripts
sudo cp -f "$SCRIPT_DIR/kiosk-browser.sh" /home/meadow/kiosk-browser.sh
sudo cp -f "$SCRIPT_DIR/kiosk-launcher.py" /home/meadow/kiosk-launcher.py
sudo cp -f "$SCRIPT_DIR/exit-kiosk.sh" /home/meadow/exit-kiosk.sh
sudo cp -f "$SCRIPT_DIR/offline.html" /home/meadow/offline.html
sudo cp -f "$SCRIPT_DIR/remote_control.py" /home/meadow/meadow-kiosk/remote_control.py

sudo chmod +x /home/meadow/kiosk-browser.sh /home/meadow/exit-kiosk.sh /home/meadow/kiosk-launcher.py
sudo chmod 644 /home/meadow/offline.html || true

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

# ------------------------------------------------------------
# 1) Autostart: stop starting launcher directly from desktop files
#    We now run launcher via systemd service (single owner)
# ------------------------------------------------------------
echo "=== Disable legacy autostarts that launch python directly (best-effort) ==="

# LXDE autostart (legacy) - remove python launcher line if present
if [ -f /home/meadow/.config/lxsession/LXDE-pi/autostart ]; then
  sudo sed -i '/kiosk-launcher\.py/d' /home/meadow/.config/lxsession/LXDE-pi/autostart || true
fi

# XDG autostart file (legacy) - keep the file but it should not start python directly
# If your meadow-launcher.desktop runs python, replace it in git later with one that starts the service.
# For now we simply keep it but you can remove it if it causes duplicates.
# sudo rm -f /home/meadow/.config/autostart/meadow-launcher.desktop 2>/dev/null || true

# Labwc autostart (legacy) - remove direct python launcher if present
if [ -f /home/meadow/.config/labwc/autostart ]; then
  sudo sed -i '/kiosk-launcher\.py/d' /home/meadow/.config/labwc/autostart || true
fi

# ------------------------------------------------------------
# 2) Desktop icons (Enter/Exit kiosk)
# ------------------------------------------------------------
echo "=== Desktop icons (Enter/Exit kiosk) ==="
sudo mkdir -p /home/meadow/Desktop
sudo cp -f "$SCRIPT_DIR/desktop/enter-kiosk.desktop" /home/meadow/Desktop/Enter\ Meadow\ Kiosk.desktop
sudo cp -f "$SCRIPT_DIR/desktop/exit-kiosk.desktop" /home/meadow/Desktop/Exit\ Meadow\ Kiosk.desktop
sudo chmod +x /home/meadow/Desktop/Enter\ Meadow\ Kiosk.desktop /home/meadow/Desktop/Exit\ Meadow\ Kiosk.desktop
sudo chown -R meadow:meadow /home/meadow/Desktop

# ------------------------------------------------------------
# 3) Systemd services
# ------------------------------------------------------------
echo "=== Installing systemd services ==="

# Existing services (leave as-is)
sudo cp -f "$SCRIPT_DIR/systemd/meadow-kiosk.service" /etc/systemd/system/meadow-kiosk.service
sudo cp -f "$SCRIPT_DIR/systemd/meadow-remote-control.service" /etc/systemd/system/meadow-remote-control.service

# NEW services (must exist in repo under systemd/)
sudo cp -f "$SCRIPT_DIR/systemd/meadow-kiosk-browser.service" /etc/systemd/system/meadow-kiosk-browser.service
sudo cp -f "$SCRIPT_DIR/systemd/meadow-launcher.service" /etc/systemd/system/meadow-launcher.service

sudo systemctl daemon-reload

# Enable core services
sudo systemctl enable meadow-kiosk.service
sudo systemctl enable meadow-remote-control.service

# Enable launcher on boot (safe default)
sudo systemctl enable meadow-launcher.service

# Do NOT auto-enable kiosk browser (remote_control decides when to enter kiosk)
sudo systemctl disable meadow-kiosk-browser.service 2>/dev/null || true

# Start services
sudo systemctl restart meadow-kiosk.service || true
sudo systemctl restart meadow-remote-control.service || true
sudo systemctl restart meadow-launcher.service || true

# ------------------------------------------------------------
# 4) Update helper
# ------------------------------------------------------------
echo "=== Install update helper (remote 'update_code' command) ==="
sudo cp -f "$SCRIPT_DIR/update-meadow.sh" /home/meadow/update-meadow.sh
sudo chmod +x /home/meadow/update-meadow.sh
sudo chown meadow:meadow /home/meadow/update-meadow.sh

echo "=== Install complete. Reboot recommended. ==="
echo "Next: sudo reboot"
