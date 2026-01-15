#!/bin/bash
set -e

# Absolute path to this repo (used for copying helper scripts)
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

echo "=== Deploy launcher + kiosk scripts ==="
sudo mkdir -p /home/meadow/meadow-kiosk
sudo cp kiosk-browser.sh /home/meadow/kiosk-browser.sh
sudo cp kiosk-launcher.py /home/meadow/kiosk-launcher.py
sudo cp exit-kiosk.sh /home/meadow/exit-kiosk.sh
sudo cp offline.html /home/meadow/offline.html
sudo cp remote_control.py /home/meadow/meadow-kiosk/remote_control.py || true
sudo chmod +x /home/meadow/kiosk-browser.sh /home/meadow/exit-kiosk.sh /home/meadow/kiosk-launcher.py
sudo chmod 644 /home/meadow/offline.html || true

echo "=== Ensure state directory ==="
sudo mkdir -p /home/meadow/state
sudo chown -R meadow:meadow /home/meadow/state

echo "=== Configure xbindkeys hotkey (Ctrl+Alt+E) ==="
cat <<'EOF' > /home/meadow/.xbindkeysrc
# Exit kiosk mode anytime
"/home/meadow/exit-kiosk.sh"
  control+alt + e
EOF
sudo chown meadow:meadow /home/meadow/.xbindkeysrc

echo "=== Configure autostart (launcher on boot) ==="
mkdir -p /home/meadow/.config/lxsession/LXDE-pi
cat <<'EOF' > /home/meadow/.config/lxsession/LXDE-pi/autostart
@unclutter
@xset s off
@xset -dpms
@xset s noblank
@xbindkeys
@python3 /home/meadow/kiosk-launcher.py
EOF
sudo chown -R meadow:meadow /home/meadow/.config

echo "=== Configure autostart for Wayland/Labwc (XDG + systemd user) ==="
# XDG autostart (works on many desktops, including some Wayland sessions)
sudo -u meadow mkdir -p /home/meadow/.config/autostart
sudo cp -f "$SCRIPT_DIR/autostart/meadow-launcher.desktop" /home/meadow/.config/autostart/meadow-launcher.desktop
sudo chown meadow:meadow /home/meadow/.config/autostart/meadow-launcher.desktop

# systemd --user (most reliable across environments)
sudo -u meadow mkdir -p /home/meadow/.config/systemd/user
sudo cp -f "$SCRIPT_DIR/systemd-user/meadow-launcher.service" /home/meadow/.config/systemd/user/meadow-launcher.service
sudo chown -R meadow:meadow /home/meadow/.config/systemd

# Allow the meadow user's services to run at boot without an interactive login
sudo loginctl enable-linger meadow || true

# Enable the user service (ignore failures if no user session is active yet)
sudo -u meadow systemctl --user daemon-reload || true
sudo -u meadow systemctl --user enable meadow-launcher.service || true
sudo -u meadow systemctl --user restart meadow-launcher.service || true



echo "=== Desktop icons (Enter/Exit kiosk) ==="
sudo mkdir -p /home/meadow/Desktop
sudo cp desktop/enter-kiosk.desktop /home/meadow/Desktop/Enter\ Meadow\ Kiosk.desktop
sudo cp desktop/exit-kiosk.desktop /home/meadow/Desktop/Exit\ Meadow\ Kiosk.desktop
sudo chmod +x /home/meadow/Desktop/Enter\ Meadow\ Kiosk.desktop /home/meadow/Desktop/Exit\ Meadow\ Kiosk.desktop
sudo chown -R meadow:meadow /home/meadow/Desktop

echo "=== Installing systemd service ==="
sudo cp systemd/meadow-kiosk.service /etc/systemd/system/meadow-kiosk.service
sudo cp systemd/meadow-remote-control.service /etc/systemd/system/meadow-remote-control.service
sudo systemctl daemon-reload
sudo systemctl enable meadow-kiosk
sudo systemctl enable meadow-remote-control
sudo systemctl restart meadow-kiosk || true
sudo systemctl restart meadow-remote-control || true

echo "=== Install update helper (remote 'update_code' command) ==="
sudo cp -f "$SCRIPT_DIR/update-meadow.sh" /home/meadow/update-meadow.sh
sudo chmod +x /home/meadow/update-meadow.sh
sudo chown meadow:meadow /home/meadow/update-meadow.sh

echo "=== Install complete. Reboot recommended. ==="
