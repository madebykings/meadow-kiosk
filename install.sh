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

echo "=== Deploy scripts/assets ==="
sudo mkdir -p /home/meadow/meadow-kiosk
sudo cp -f "$SCRIPT_DIR/kiosk-browser.sh" /home/meadow/kiosk-browser.sh
sudo cp -f "$SCRIPT_DIR/offline.html" /home/meadow/offline.html

# exit-kiosk.sh (overwrite with updated version)
cat <<'EOF' | sudo tee /home/meadow/exit-kiosk.sh >/dev/null
#!/bin/bash
set -euo pipefail

STOP_FLAG="/tmp/meadow_kiosk_stop"

touch "$STOP_FLAG" 2>/dev/null || true
systemctl --user stop meadow-kiosk-ui.service 2>/dev/null || true

pkill -f "chromium-browser.*--kiosk" 2>/dev/null || true
pkill -f "chromium.*--kiosk" 2>/dev/null || true

exit 0
EOF

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

echo "=== Install systemd service (Pi API) ==="
sudo install -m 644 "$SCRIPT_DIR/systemd/meadow-kiosk.service" /etc/systemd/system/meadow-kiosk.service
sudo systemctl daemon-reload
sudo systemctl enable meadow-kiosk.service
sudo systemctl restart meadow-kiosk.service

echo "=== Install user systemd service (Kiosk UI) ==="
sudo -u meadow mkdir -p /home/meadow/.config/systemd/user
cat <<'EOF' | sudo -u meadow tee /home/meadow/.config/systemd/user/meadow-kiosk-ui.service >/dev/null
[Unit]
Description=Meadow Kiosk UI (Chromium watchdog)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/bin/bash /home/meadow/kiosk-browser.sh
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF

echo "=== Enable lingering for meadow (allow user services at boot) ==="
sudo loginctl enable-linger meadow

echo "=== Enable user kiosk-ui service ==="
sudo -u meadow systemctl --user daemon-reload
sudo -u meadow systemctl --user enable meadow-kiosk-ui.service || true

echo "=== Desktop + menu launcher (Enter kiosk) ==="
sudo mkdir -p /home/meadow/Desktop
cat <<'EOF' | sudo tee "/home/meadow/Desktop/Enter Meadow Kiosk.desktop" >/dev/null
[Desktop Entry]
Type=Application
Name=Enter Meadow Kiosk
Comment=Start Meadow kiosk UI (Chromium)
Exec=bash -lc 'systemctl --user restart meadow-kiosk-ui.service'
Icon=video-display
Terminal=false
Categories=Utility;
EOF
sudo chmod +x "/home/meadow/Desktop/Enter Meadow Kiosk.desktop"
sudo chown -R meadow:meadow /home/meadow/Desktop

sudo -u meadow mkdir -p /home/meadow/.local/share/applications
sudo -u meadow cp -f "/home/meadow/Desktop/Enter Meadow Kiosk.desktop" "/home/meadow/.local/share/applications/Enter Meadow Kiosk.desktop"

echo "=== Install complete ==="
echo "Wayland note: launch via menu or: gtk-launch 'Enter Meadow Kiosk'"
echo "To start kiosk now: sudo -u meadow systemctl --user restart meadow-kiosk-ui.service"
