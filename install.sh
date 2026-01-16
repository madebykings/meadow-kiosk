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

echo "=== Kill any stray kiosk processes (belt + braces) ==="
sudo pkill -f "/home/meadow/kiosk-browser.sh" 2>/dev/null || true
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

echo "=== Desktop icon (Enter kiosk ONLY) ==="
sudo mkdir -p /home/meadow/Desktop

# If you keep a desktop file in repo, copy it, otherwise generate it.
if [ -f "$SCRIPT_DIR/desktop/enter-kiosk.desktop" ]; then
  sudo cp -f "$SCRIPT_DIR/desktop/enter-kiosk.desktop" /home/meadow/Desktop/Enter\ Meadow\ Kiosk.desktop
else
  cat <<'EOF' | sudo tee /home/meadow/Desktop/Enter\ Meadow\ Kiosk.desktop >/dev/null
[Desktop Entry]
Type=Application
Name=Enter Meadow Kiosk
Comment=Launch Meadow kiosk mode (fullscreen)
Exec=bash -lc 'sudo systemctl restart meadow-kiosk.service'
Icon=video-display
Terminal=false
Categories=Utility;
EOF
fi

# Ensure correct Exec line (just in case repo file differs)
sudo sed -i "s/meadow-kiosk-browser\.service/meadow-kiosk.service/g" /home/meadow/Desktop/Enter\ Meadow\ Kiosk.desktop

# Make launcher runnable + owned by meadow
sudo chmod +x /home/meadow/Desktop/Enter\ Meadow\ Kiosk.desktop
sudo chown -R meadow:meadow /home/meadow/Desktop

# Optional: also install into application menu
sudo mkdir -p /home/meadow/.local/share/applications
sudo cp -f /home/meadow/Desktop/Enter\ Meadow\ Kiosk.desktop /home/meadow/.local/share/applications/Enter\ Meadow\ Kiosk.desktop
sudo chown -R meadow:meadow /home/meadow/.local/share/applications

echo "=== Install update helper (optional) ==="
sudo cp -f "$SCRIPT_DIR/update-meadow.sh" /home/meadow/update-meadow.sh
sudo chmod +x /home/meadow/update-meadow.sh
sudo chown meadow:meadow /home/meadow/update-meadow.sh

echo "=== Install ONLY meadow-kiosk.service ==="
sudo install -m 644 "$SCRIPT_DIR/systemd/meadow-kiosk.service" /etc/systemd/system/meadow-kiosk.service

echo "=== Reload systemd ==="
sudo systemctl daemon-reload

echo "=== Sudoers for kiosk control (restart/stop/start + reboot/shutdown) ==="
cat <<'EOF' | sudo tee /etc/sudoers.d/meadow-kiosk-control >/dev/null
meadow ALL=(root) NOPASSWD: /bin/systemctl start meadow-kiosk.service, /bin/systemctl stop meadow-kiosk.service, /bin/systemctl restart meadow-kiosk.service, /sbin/reboot, /sbin/shutdown
EOF
sudo chmod 440 /etc/sudoers.d/meadow-kiosk-control

echo "=== Enable + start meadow-kiosk.service ==="
sudo systemctl enable meadow-kiosk.service
sudo systemctl restart meadow-kiosk.service

echo "=== Install complete. Reboot recommended. ==="
echo "If the icon appears but won’t launch: right-click it and choose 'Allow Launching'."
