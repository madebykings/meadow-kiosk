#!/bin/bash
set -e

echo "=== Updating system ==="
sudo apt update
sudo apt upgrade -y

echo "=== Installing system dependencies ==="
sudo apt install -y \
    git \
    python3-pip \
    python3-serial \
    python3-rpi-lgpio \
    chromium \
    modemmanager \
    network-manager \
    usb-modeswitch \
    ppp \
    unclutter

echo "=== Installing Python modules ==="
pip3 install --break-system-packages requests pyserial RPi.GPIO

echo "=== Enabling UART for SIM7600 ==="
sudo raspi-config nonint do_serial 2

echo "=== Creating Chromium kiosk autostart (dynamic URL from WP) ==="
mkdir -p ~/.config/lxsession/LXDE-pi

cat <<'EOF' > /home/meadow/kiosk-browser.sh
#!/bin/bash
URL_FILE="/home/meadow/kiosk.url"
while [ ! -s "$URL_FILE" ]; do
  sleep 1
done
URL=$(tr -d '\r\n' < "$URL_FILE")
[ -z "$URL" ] && URL="https://google.com"
chromium --noerrdialogs --disable-infobars --kiosk "$URL"
EOF

chmod +x /home/meadow/kiosk-browser.sh

cat <<'EOF' > ~/.config/lxsession/LXDE-pi/autostart
@unclutter
@xset s off
@xset -dpms
@xset s noblank
@/home/pi/kiosk-browser.sh
EOF

echo "=== Installing systemd service ==="
sudo cp systemd/meadow-kiosk.service /etc/systemd/system/
sudo systemctl enable meadow-kiosk

echo "=== Install complete. Reboot recommended. ==="

