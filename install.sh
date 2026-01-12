#!/bin/bash
set -e

echo "=== Updating system ==="
sudo apt update
sudo apt upgrade -y

echo "=== Installing system dependencies ==="
sudo apt install -y \
  git \
  python3 \
  python3-serial \
  python3-rpi-lgpio \
  chromium \
  unclutter \
  modemmanager \
  network-manager \
  usb-modeswitch \
  ca-certificates

echo "=== Creating state directory ==="
sudo mkdir -p /home/meadow/state
sudo chown -R meadow:meadow /home/meadow/state

echo "=== Installing myPOS Sigma udev rule (creates /dev/sigma) ==="
sudo cp hardware/mypos-sigma/udev/99-mypos-sigma.rules /etc/udev/rules.d/99-mypos-sigma.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
echo "If Sigma is already plugged in, unplug/replug USB to get /dev/sigma."

echo "=== Installing browser launcher ==="
sudo cp kiosk-browser.sh /home/meadow/kiosk-browser.sh
sudo chmod +x /home/meadow/kiosk-browser.sh
sudo chown meadow:meadow /home/meadow/kiosk-browser.sh

echo "=== Setting LXDE autostart (kiosk browser) ==="
mkdir -p ~/.config/lxsession/LXDE-pi
cat <<'EOF' > ~/.config/lxsession/LXDE-pi/autostart
@unclutter
@xset s off
@xset -dpms
@xset s noblank
@/home/meadow/kiosk-browser.sh
EOF

echo "=== Installing systemd service ==="
sudo cp systemd/meadow-kiosk.service /etc/systemd/system/meadow-kiosk.service
sudo systemctl daemon-reload
sudo systemctl enable meadow-kiosk
sudo systemctl restart meadow-kiosk || true

echo "=== Install complete. Reboot recommended. ==="
