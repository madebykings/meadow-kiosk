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
  xdotool \
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

sudo chmod 755 /home/meadow/kiosk-browser.sh
sudo chmod 644 /home/meadow/offline.html || true
sudo chown meadow:meadow /home/meadow/kiosk-browser.sh /home/meadow/offline.html || true

echo "=== Ensure state directory ==="
sudo mkdir -p /home/meadow/state
sudo chown -R meadow:meadow /home/meadow/state

echo "=== Install enter-kiosk.sh + exit-kiosk.sh ==="

# enter-kiosk.sh (no user services; just start kiosk-browser.sh)
cat <<'EOF' | sudo tee /home/meadow/meadow-kiosk/enter-kiosk.sh >/dev/null
#!/bin/bash
set -euo pipefail

STOP_FLAG="${MEADOW_KIOSK_STOP_FLAG:-/run/meadow/kiosk_stop}"

# Clear stop flags (support both legacy + new)
rm -f "$STOP_FLAG" 2>/dev/null || true
rm -f /tmp/meadow_kiosk_stop 2>/dev/null || true

# If already running, do nothing
if pgrep -f "kiosk-browser\.sh" >/dev/null 2>&1; then
  exit 0
fi

# Start kiosk loop detached
nohup /bin/bash /home/meadow/kiosk-browser.sh >/dev/null 2>&1 &
exit 0
EOF

# exit-kiosk.sh (stop flag + kill chromium kiosk)
cat <<'EOF' | sudo tee /home/meadow/exit-kiosk.sh >/dev/null
#!/bin/bash
set -euo pipefail

STOP_FLAG="${MEADOW_KIOSK_STOP_FLAG:-/run/meadow/kiosk_stop}"

# Ask kiosk loop to stop
mkdir -p "$(dirname "$STOP_FLAG")" 2>/dev/null || true
date -Is 2>/dev/null > "$STOP_FLAG" || true
touch /tmp/meadow_kiosk_stop 2>/dev/null || true

# Kill kiosk loop + chromium kiosk (best-effort)
pkill -f "kiosk-browser\.sh" 2>/dev/null || true
pkill -f "chromium-browser.*--kiosk" 2>/dev/null || true
pkill -f "chromium.*--kiosk" 2>/dev/null || true

exit 0
EOF

sudo chmod 755 /home/meadow/meadow-kiosk/enter-kiosk.sh /home/meadow/exit-kiosk.sh
sudo chown meadow:meadow /home/meadow/meadow-kiosk/enter-kiosk.sh /home/meadow/exit-kiosk.sh

echo "=== Configure hotkeys (xbindkeys) ==="
cat <<'EOF' | sudo tee /home/meadow/.xbindkeysrc >/dev/null
# Exit kiosk
"/home/meadow/exit-kiosk.sh"
  control+alt + e

# Refresh current page (best-effort)
"xdotool key --clearmodifiers ctrl+r"
  control + r
EOF
sudo chown meadow:meadow /home/meadow/.xbindkeysrc

echo "=== Autostart xbindkeys on login ==="
sudo -u meadow mkdir -p /home/meadow/.config/autostart
cat <<'EOF' | sudo -u meadow tee /home/meadow/.config/autostart/xbindkeys.desktop >/dev/null
[Desktop Entry]
Type=Application
Name=xbindkeys
Exec=/usr/bin/xbindkeys -f /home/meadow/.xbindkeysrc
X-GNOME-Autostart-enabled=true
EOF

echo "=== Desktop icon (Enter kiosk) ==="
sudo mkdir -p /home/meadow/Desktop
cat <<'EOF' | sudo tee "/home/meadow/Desktop/Enter Meadow Kiosk.desktop" >/dev/null
[Desktop Entry]
Type=Application
Name=Enter Meadow Kiosk
Comment=Start Meadow kiosk UI (Chromium)
Exec=bash -lc '/home/meadow/meadow-kiosk/enter-kiosk.sh'
Icon=video-display
Terminal=false
Categories=Utility;
EOF

sudo chmod 755 "/home/meadow/Desktop/Enter Meadow Kiosk.desktop"
sudo chown -R meadow:meadow /home/meadow/Desktop

# Mark trusted (helps on some Pi OS setups)
gio set "/home/meadow/Desktop/Enter Meadow Kiosk.desktop" metadata::trusted true 2>/dev/null || true

echo "=== Install systemd service (Pi API) ==="
sudo install -m 644 "$SCRIPT_DIR/systemd/meadow-kiosk.service" /etc/systemd/system/meadow-kiosk.service
sudo systemctl daemon-reload
sudo systemctl enable meadow-kiosk.service
sudo systemctl restart meadow-kiosk.service

echo "=== Install complete ==="
echo ""
echo "Hotkeys:"
echo "  Ctrl+Alt+E = Exit kiosk"
echo "  Ctrl+R     = Refresh"
echo ""
echo "Desktop:"
echo "  Enter Meadow Kiosk"
