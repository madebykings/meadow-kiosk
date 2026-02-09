Meadow Kiosk

Production installer and runtime for Meadow vending kiosks.

This repository is designed to be safe to re-run, easy to provision, and suitable for factory builds and remote updates.

📦 First-Time Setup (Fresh Raspberry Pi)
1️⃣ Flash Raspberry Pi OS

Use Raspberry Pi OS (64-bit).

During imaging, enable:

SSH

User: meadow

Password or SSH key

Network (any one is fine):

Wi-Fi

Ethernet

4G HAT (SIM inserted)

Boot the Pi and log in as user meadow.

2️⃣ Install Git (required)

Minimal Raspberry Pi OS images do not guarantee git is installed.

sudo apt update
sudo apt install -y git

3️⃣ Clone the Meadow kiosk repo
cd /home/meadow
git clone https://github.com/madebykings/meadow-kiosk.git
cd meadow-kiosk

4️⃣ Create your provision file

Copy and edit the provision template:

cp provision-edit.json provision.json
nano provision.json


At minimum, set:

kiosk_token

domain

provision_key

Optional but recommended:

4G APN settings

Cloudflare tunnel token + hostname

Save and exit.

5️⃣ Run the installer
chmod +x install.sh
sudo ./install.sh


This will:

Install system dependencies

Sync the repo to /home/meadow/meadow-kiosk

Configure networking (Wi-Fi / 4G)

Install and configure cloudflared (if provisioned)

Install Meadow systemd services

Set up kiosk hotkeys and watchdogs

6️⃣ Reboot
sudo reboot

7️⃣ Start the kiosk

After login (or via SSH):

bash /home/meadow/meadow-kiosk/enter-kiosk.sh


The kiosk launcher should appear.

🔁 Updating an Existing Kiosk (Most Common)

If the repo already exists on the Pi:

cd /home/meadow/meadow-kiosk
git pull
sudo ./install.sh
sudo reboot


This safely:

pulls the latest code

re-copies scripts

re-registers systemd services

reapplies networking + cloudflared config

The installer is idempotent and safe to re-run.

⌨️ Keyboard Shortcuts

Ctrl + Alt + E → Exit kiosk mode

Ctrl + R → Refresh current page

🌐 Cloudflare Tunnel Notes

install.sh installs cloudflared automatically

Tunnel login is non-interactive

Each kiosk uses a tunnel token stored in provision.json

DNS hostname (e.g. kiosk2.meadowvending.com) must already exist
(automatic DNS creation can be added later if needed)

📶 4G / Cellular Notes

Uses NetworkManager + ModemManager

APN is read from provision.json

Multiple APNs can be tried automatically

LTE-only mode is recommended for SIM7600X-based HATs

🧯 Troubleshooting
Kiosk won’t start / seems “stuck”
1️⃣ Check for stop flags
ls -l /run/meadow/kiosk_stop /tmp/meadow_kiosk_stop 2>/dev/null || true

2️⃣ Remove stop flags
sudo rm -f /run/meadow/kiosk_stop
rm -f /tmp/meadow_kiosk_stop

ls -l /run/meadow/kiosk_stop /tmp/meadow_kiosk_stop 2>/dev/null || echo "No stop flags"

3️⃣ Kill any leftover processes (optional but clean)
pkill -f "kiosk-browser\.sh" || true
pkill -f "chromium.*--kiosk" || true
pkill -f "chromium-browser.*--kiosk" || true

4️⃣ Run the kiosk in the foreground (for logs)
bash -x /home/meadow/meadow-kiosk/kiosk-browser.sh


This is the fastest way to see what’s actually failing.

🔧 systemd (UI service – if applicable)

If you are using a user-level UI service:

systemctl --user daemon-reload
systemctl --user enable meadow-kiosk-ui.service
systemctl --user restart meadow-kiosk-ui.service


(Note: most production kiosks rely on the main Meadow system service instead.)

🧠 Design Notes

install.sh is the single source of truth

All site/kiosk-specific data lives in provision.json

The system is designed for:

unattended installs

remote updates

factory imaging

safe recovery after crashes or power loss