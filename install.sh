#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="/home/meadow/meadow-kiosk"
MEADOW_USER="meadow"
MEADOW_GROUP="meadow"

echo "=== Meadow install.sh (canonical path: ${TARGET_DIR}) ==="

echo "=== Updating system ==="
sudo apt update
sudo apt upgrade -y

echo "=== Installing system dependencies ==="
sudo apt install -y \
  git \
  rsync \
  rpi-eeprom \
  python3 \
  python3-pip \
  python3-serial \
  python3-rpi-lgpio \
  python3-gpiozero \
  python3-tk \
  chromium \
  unclutter \
  xbindkeys \
  xdotool \
  dbus-x11 \
  modemmanager \
  network-manager \
  usb-modeswitch \
  grim \
  curl \
  ca-certificates

echo "=== Pi 5 EEPROM auto-boot sanity check (WAIT_FOR_POWER_BUTTON / POWER_OFF_ON_HALT) ==="
if command -v rpi-eeprom-config >/dev/null 2>&1; then
  EEPROM_CFG="$(sudo -E rpi-eeprom-config 2>/dev/null || true)"
  WAIT_VAL="$(echo "${EEPROM_CFG}" | grep -E '^\s*WAIT_FOR_POWER_BUTTON=' | tail -n 1 | cut -d= -f2 | tr -d '[:space:]' || true)"
  HALT_VAL="$(echo "${EEPROM_CFG}" | grep -E '^\s*POWER_OFF_ON_HALT=' | tail -n 1 | cut -d= -f2 | tr -d '[:space:]' || true)"

  if [[ "${WAIT_VAL:-}" == "1" || "${HALT_VAL:-}" == "1" ]]; then
    echo ""
    echo "!!! WARNING: Pi EEPROM bootloader config suggests it may wait for the power button !!!"
    echo " Detected: WAIT_FOR_POWER_BUTTON=${WAIT_VAL:-} POWER_OFF_ON_HALT=${HALT_VAL:-}"
    echo ""
    echo "For auto-boot on power apply, set:"
    echo " WAIT_FOR_POWER_BUTTON=0"
    echo " POWER_OFF_ON_HALT=0"
    echo ""
    echo "Fix (interactive):"
    echo " sudo -E rpi-eeprom-config --edit"
    echo "Then save + reboot:"
    echo " sudo reboot"
    echo ""
  else
    echo "[Meadow] EEPROM config looks OK for auto-boot (WAIT_FOR_POWER_BUTTON=${WAIT_VAL:-} POWER_OFF_ON_HALT=${HALT_VAL:-})"
  fi
else
  echo "[Meadow] rpi-eeprom-config not found; skipping EEPROM check."
fi

echo "=== Pi 5 USB boot / non-PD power hint (usb_max_current_enable=1) ==="
CFG="/boot/firmware/config.txt"
if [[ -f "${CFG}" ]]; then
  if grep -qE '^\s*usb_max_current_enable\s*=\s*1\s*$' "${CFG}"; then
    echo "[Meadow] ${CFG} already has usb_max_current_enable=1"
  else
    echo "[Meadow] Adding usb_max_current_enable=1 to ${CFG} (safe; helps some USB/NVMe boot on non-PD/GPIO power)"
    echo "" | sudo tee -a "${CFG}" >/dev/null
    echo "usb_max_current_enable=1" | sudo tee -a "${CFG}" >/dev/null
  fi
else
  echo "[Meadow] ${CFG} not found; skipping usb_max_current_enable hint."
  echo " (On your image it should be /boot/firmware/config.txt)"
fi

echo "=== Ensure Meadow home ==="
sudo mkdir -p /home/meadow
sudo chown -R "${MEADOW_USER}:${MEADOW_GROUP}" /home/meadow

echo "=== Ensure canonical repo location exists ==="
sudo mkdir -p "${TARGET_DIR}"
sudo chown -R "${MEADOW_USER}:${MEADOW_GROUP}" "${TARGET_DIR}"

# If the install script is being run from somewhere else, sync the repo contents into TARGET_DIR.
if [ "${SCRIPT_DIR}" != "${TARGET_DIR}" ]; then
  echo "=== Syncing repo into ${TARGET_DIR} (from ${SCRIPT_DIR}) ==="
  sudo rsync -a --delete --exclude '.git' "${SCRIPT_DIR}/" "${TARGET_DIR}/"
  sudo chown -R "${MEADOW_USER}:${MEADOW_GROUP}" "${TARGET_DIR}"
else
  echo "=== Running from ${TARGET_DIR} (no repo sync needed) ==="
fi

PROVISION_JSON="${TARGET_DIR}/provision.json"

json_get() {
  # Usage: json_get ".network.apn"
  local path="$1"
  if [ -f "$PROVISION_JSON" ]; then
    python3 - "$path" <<PY 2>/dev/null || true
import json,sys
p=sys.argv[1]
try:
  d=json.load(open("${PROVISION_JSON}","r"))
except Exception:
  sys.exit(0)

def get(obj, path):
  path = path.strip()
  if path.startswith("."): path = path[1:]
  if not path: return obj
  cur = obj
  for part in path.split("."):
    if part.endswith("]") and "[" in part:
      k, idx = part[:-1].split("[", 1)
      if k:
        cur = cur.get(k, {}) if isinstance(cur, dict) else None
      try:
        cur = cur[int(idx)] if isinstance(cur, list) else None
      except Exception:
        return None
    else:
      cur = cur.get(part, None) if isinstance(cur, dict) else None
    if cur is None:
      return None
  return cur

v=get(d,p)
if v is None:
  pass
elif isinstance(v,(list,dict)):
  print(json.dumps(v))
else:
  print(str(v))
PY
  else
    echo ""
  fi
}

truthy() {
  case "${1:-}" in
    1|true|True|TRUE|yes|Yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

# ------------------------------------------------------------
# 4G setup (NetworkManager + ModemManager) driven by provision.json
# ------------------------------------------------------------
ensure_nm_mm() {
  sudo systemctl enable NetworkManager ModemManager >/dev/null 2>&1 || true
  sudo systemctl restart NetworkManager ModemManager >/dev/null 2>&1 || true
}

setup_4g_from_provision() {
  local enable
  enable="$(json_get ".network.enable_4g")"
  if ! truthy "$enable"; then
    echo "[4G] provision.json: network.enable_4g not true; skipping 4G setup"
    return 0
  fi

  ensure_nm_mm

  # Wait briefly for wwan0 to appear (best-effort)
  for _ in $(seq 1 15); do
    nmcli -t -f DEVICE,TYPE dev status 2>/dev/null | grep -q '^wwan0:gsm' && break
    sleep 1
  done

  local conn_name apn lte_only
  conn_name="$(json_get ".network.conn_name")"
  [ -n "$conn_name" ] && [ "$conn_name" != "null" ] || conn_name="meadow-4g"

  apn="$(json_get ".network.apn")"
  lte_only="$(json_get ".network.lte_only")"

  # Candidates: explicit apn + apn_fallbacks + defaults
  local candidates=""
  if [ -n "$apn" ] && [ "$apn" != "null" ]; then
    candidates="$apn"
  fi

  local fallbacks_json fb
  fallbacks_json="$(json_get ".network.apn_fallbacks")"
  if [ -n "$fallbacks_json" ] && [ "$fallbacks_json" != "null" ]; then
    fb="$(python3 - <<PY 2>/dev/null || true
import json
try:
  arr=json.loads('''$fallbacks_json''')
  print(" ".join([str(x) for x in arr if x]))
except Exception:
  pass
PY
)"
    candidates="$candidates $fb"
  fi

  candidates="$(echo "$candidates" | xargs || true)"
  [ -n "$candidates" ] || candidates="internet everywhere payg.internet"

  echo "[4G] Connection: $conn_name"
  echo "[4G] APN candidates: $candidates"

  # Create connection if missing (seed with first APN)
  if ! nmcli -t -f NAME con show | grep -qx "$conn_name"; then
    local first_apn
    first_apn="$(echo "$candidates" | awk '{print $1}')"
    sudo nmcli con add type gsm ifname "*" con-name "$conn_name" apn "$first_apn" >/dev/null
  fi

  # Make stable
  sudo nmcli con modify "$conn_name" connection.autoconnect yes >/dev/null || true
  sudo nmcli con modify "$conn_name" ipv4.method auto >/dev/null || true

  if truthy "$lte_only"; then
    sudo nmcli con modify "$conn_name" gsm.network-type lte >/dev/null || true
  fi

  # Try APNs until connected
  local apn_try
  for apn_try in $candidates; do
    echo "[4G] Trying APN: $apn_try"
    sudo nmcli con modify "$conn_name" gsm.apn "$apn_try" >/dev/null || true
    if sudo nmcli con up "$conn_name" >/dev/null 2>&1; then
      echo "[4G] Connected with APN: $apn_try"
      return 0
    fi
  done

  echo "[4G] WARNING: failed to connect using APNs: $candidates (install continues)" >&2
  return 0
}

# ------------------------------------------------------------
# cloudflared install + token service driven by provision.json
# ------------------------------------------------------------
install_cloudflared() {
  local install enable
  install="$(json_get ".cloudflared.install")"
  enable="$(json_get ".cloudflared.enable")"

  if ! truthy "$install" && ! truthy "$enable"; then
    echo "[cloudflared] disabled by provision.json (install+enable false); skipping install"
    return 0
  fi

  if command -v cloudflared >/dev/null 2>&1; then
    echo "[cloudflared] Already installed: $(cloudflared --version 2>/dev/null || true)"
    return 0
  fi

  echo "[cloudflared] Installing via Cloudflare apt repo"
  sudo mkdir -p /usr/share/keyrings

  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
    | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null

  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
    | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null

  sudo apt update
  sudo apt install -y cloudflared

  echo "[cloudflared] Installed: $(cloudflared --version 2>/dev/null || true)"
}

setup_cloudflared_token_service() {
  local enable token hostname service_name
  enable="$(json_get ".cloudflared.enable")"
  if ! truthy "$enable"; then
    echo "[cloudflared] enable is false; skipping service setup"
    return 0
  fi

  token="$(json_get ".cloudflared.tunnel_token")"
  hostname="$(json_get ".cloudflared.hostname")"
  service_name="$(json_get ".cloudflared.service_name")"
  [ -n "$service_name" ] && [ "$service_name" != "null" ] || service_name="meadow-cloudflared"

  if [ -z "${token:-}" ] || [ "$token" = "null" ]; then
    echo "[cloudflared] No tunnel_token set. Installed/hardened only."
    if [ -n "${hostname:-}" ] && [ "$hostname" != "null" ]; then
      echo "[cloudflared] Hostname is set to: ${hostname} (you still need to map it to a tunnel in Cloudflare)"
    fi
    return 0
  fi

  echo "[cloudflared] Writing ${service_name}.service (token-based)"
  sudo tee "/etc/systemd/system/${service_name}.service" >/dev/null <<EOF
[Unit]
Description=Meadow Cloudflare Tunnel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/cloudflared tunnel --no-autoupdate run --token ${token}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable "${service_name}.service"
  sudo systemctl restart "${service_name}.service" || true

  if [ -n "${hostname:-}" ] && [ "$hostname" != "null" ]; then
    echo "[cloudflared] Hostname in provision.json: ${hostname}"
    echo "[cloudflared] NOTE: DNS routing should already exist in Cloudflare unless you add API automation later."
  fi
}

harden_cloudflared_restart() {
  local want
  want="$(json_get ".cloudflared.restart_always")"
  if ! truthy "$want"; then
    echo "[cloudflared] restart_always not requested; skipping hardening"
    return 0
  fi

  # Harden stock cloudflared.service too if it exists on older images
  if systemctl list-unit-files | grep -q '^cloudflared\.service'; then
    echo "[cloudflared] Applying Restart=always drop-in for cloudflared.service"
    sudo mkdir -p /etc/systemd/system/cloudflared.service.d
    cat <<'EOF' | sudo tee /etc/systemd/system/cloudflared.service.d/restart.conf >/dev/null
[Service]
Restart=always
RestartSec=5
EOF
    sudo systemctl daemon-reload
    sudo systemctl restart cloudflared || true
  fi
}

# Run provisioning steps (best-effort; do not fail install if SIM/token not present yet)
setup_4g_from_provision || true
install_cloudflared || true
setup_cloudflared_token_service || true
harden_cloudflared_restart || true

echo "=== Install python deps (best-effort) ==="
if [ -f "${TARGET_DIR}/requirements.txt" ]; then
  sudo -H python3 -m pip install --upgrade pip
  sudo -H python3 -m pip install -r "${TARGET_DIR}/requirements.txt" || true
else
  sudo -H python3 -m pip install --upgrade pip
  sudo -H python3 -m pip install flask requests pyserial || true
fi

echo "=== Kill any stray kiosk processes (belt + braces) ==="
sudo pkill -f "${TARGET_DIR}/kiosk-browser\.sh" 2>/dev/null || true
sudo pkill -f "kiosk-browser\.sh" 2>/dev/null || true
sudo pkill -f "chromium.*--kiosk" 2>/dev/null || true
sudo pkill -f "chromium-browser.*--kiosk" 2>/dev/null || true
sudo pkill -f "/home/meadow/kiosk-browser\.sh" 2>/dev/null || true

echo "=== Ensure runtime + state directories ==="
sudo mkdir -p /run/meadow
sudo chown "${MEADOW_USER}:${MEADOW_GROUP}" /run/meadow
sudo mkdir -p "${TARGET_DIR}/state"
sudo chown -R "${MEADOW_USER}:${MEADOW_GROUP}" "${TARGET_DIR}/state"

echo "=== Ensure executable bits on repo scripts ==="
sudo chmod 755 \
  "${TARGET_DIR}/kiosk-browser.sh" \
  "${TARGET_DIR}/enter-kiosk.sh" \
  "${TARGET_DIR}/exit-kiosk.sh" \
  "${TARGET_DIR}/update-meadow.sh" \
  "${TARGET_DIR}/kiosk-freeze-watchdog.sh" \
  2>/dev/null || true
sudo chmod 644 "${TARGET_DIR}/offline.html" 2>/dev/null || true
sudo chown -R "${MEADOW_USER}:${MEADOW_GROUP}" "${TARGET_DIR}"

echo "=== Install enter/exit wrappers (canonical) ==="
cat <<'EOF' | sudo tee "${TARGET_DIR}/enter-kiosk.sh" >/dev/null
#!/bin/bash
set -euo pipefail

ROOT="/home/meadow/meadow-kiosk"
STOP_FLAG_RUN="/run/meadow/kiosk_stop"
STOP_FLAG_TMP="/tmp/meadow_kiosk_stop"
URL_FILE="${ROOT}/kiosk.url"
STATE_DIR="${ROOT}/state"

mkdir -p "$STATE_DIR" 2>/dev/null || true

URL="https://meadowvending.com/kiosk1/"
if [ -f "$URL_FILE" ]; then
  CANDIDATE="$(head -n 1 "$URL_FILE" | tr -d '\r\n' || true)"
  if [ -n "${CANDIDATE:-}" ]; then
    URL="$CANDIDATE"
  fi
fi
if [ "$URL" = "https://meadowvending.com/kiosk1" ]; then
  URL="https://meadowvending.com/kiosk1/"
fi
echo "$URL" > "$URL_FILE"

mkdir -p /run/meadow 2>/dev/null || true
chown meadow:meadow /run/meadow 2>/dev/null || true

bash "${ROOT}/exit-kiosk.sh" >/dev/null 2>&1 || true
sleep 0.4

rm -f "$STOP_FLAG_RUN" 2>/dev/null || true
rm -f "$STOP_FLAG_TMP" 2>/dev/null || true

export DISPLAY=":0"
export XAUTHORITY="/home/meadow/.Xauthority"
export XDG_RUNTIME_DIR="/run/user/1000"

nohup bash "${ROOT}/kiosk-browser.sh" >>"${STATE_DIR}/kiosk-enter.log" 2>&1 &
exit 0
EOF

cat <<'EOF' | sudo tee "${TARGET_DIR}/exit-kiosk.sh" >/dev/null
#!/bin/bash
set -euo pipefail

ROOT="/home/meadow/meadow-kiosk"
STOP_FLAG_RUN="/run/meadow/kiosk_stop"
STOP_FLAG_TMP="/tmp/meadow_kiosk_stop"

mkdir -p /run/meadow 2>/dev/null || true
chown meadow:meadow /run/meadow 2>/dev/null || true

date -Is 2>/dev/null > "$STOP_FLAG_RUN" || true
touch "$STOP_FLAG_TMP" 2>/dev/null || true

pkill -f "${ROOT}/kiosk-browser\.sh" 2>/dev/null || true
pkill -f "kiosk-browser\.sh" 2>/dev/null || true
pkill -f "chromium-browser.*--kiosk" 2>/dev/null || true
pkill -f "chromium.*--kiosk" 2>/dev/null || true

exit 0
EOF

sudo chmod 755 "${TARGET_DIR}/enter-kiosk.sh" "${TARGET_DIR}/exit-kiosk.sh"
sudo chown "${MEADOW_USER}:${MEADOW_GROUP}" "${TARGET_DIR}/enter-kiosk.sh" "${TARGET_DIR}/exit-kiosk.sh"

echo "=== Configure hotkeys (xbindkeys) ==="
cat <<'EOF' | sudo tee /home/meadow/.xbindkeysrc >/dev/null
# Exit kiosk
"/home/meadow/meadow-kiosk/exit-kiosk.sh"
  control+alt + e

# Refresh current page (best-effort)
"xdotool key --clearmodifiers ctrl+r"
  control + r
EOF
sudo chown "${MEADOW_USER}:${MEADOW_GROUP}" /home/meadow/.xbindkeysrc

echo "=== Autostart xbindkeys on login ==="
sudo -u "${MEADOW_USER}" mkdir -p /home/meadow/.config/autostart
cat <<'EOF' | sudo -u "${MEADOW_USER}" tee /home/meadow/.config/autostart/xbindkeys.desktop >/dev/null
[Desktop Entry]
Type=Application
Name=xbindkeys
Exec=/usr/bin/xbindkeys -f /home/meadow/.xbindkeysrc
X-GNOME-Autostart-enabled=true
EOF

echo "=== labwc: hide cursor (Wayland-safe) ==="
LABWC_DIR="/home/meadow/.config/labwc"
LABWC_RC="${LABWC_DIR}/rc.xml"
sudo -u "${MEADOW_USER}" mkdir -p "${LABWC_DIR}"

if [ ! -f "${LABWC_RC}" ]; then
  cat <<'EOF' | sudo -u "${MEADOW_USER}" tee "${LABWC_RC}" >/dev/null
<?xml version="1.0"?>
<labwc_config>
  <core>
    <hideCursor>true</hideCursor>
    <hideCursorDelay>0</hideCursorDelay>
  </core>
</labwc_config>
EOF
else
  if grep -q "<hideCursor>" "${LABWC_RC}"; then
    sudo sed -i 's#<hideCursor>.*</hideCursor>#<hideCursor>true</hideCursor>#' "${LABWC_RC}" || true
  fi
  if grep -q "<hideCursorDelay>" "${LABWC_RC}"; then
    sudo sed -i 's#<hideCursorDelay>.*</hideCursorDelay>#<hideCursorDelay>0</hideCursorDelay>#' "${LABWC_RC}" || true
  fi
  if ! grep -q "<core>" "${LABWC_RC}"; then
    sudo sed -i 's#</labwc_config>#  <core>\n    <hideCursor>true</hideCursor>\n    <hideCursorDelay>0</hideCursorDelay>\n  </core>\n</labwc_config>#' "${LABWC_RC}" || true
  fi
fi
sudo chown -R "${MEADOW_USER}:${MEADOW_GROUP}" "${LABWC_DIR}"

echo "=== Install systemd service (Pi API) ==="
sudo install -m 644 "${TARGET_DIR}/systemd/meadow-kiosk.service" /etc/systemd/system/meadow-kiosk.service
sudo systemctl daemon-reload
sudo systemctl enable meadow-kiosk.service
sudo systemctl restart meadow-kiosk.service

echo "=== Install systemd service (Freeze Watchdog) ==="
sudo install -m 644 "${TARGET_DIR}/systemd/meadow-kiosk-freeze-watchdog.service" /etc/systemd/system/meadow-kiosk-freeze-watchdog.service
sudo systemctl daemon-reload
sudo systemctl enable meadow-kiosk-freeze-watchdog.service
sudo systemctl start meadow-kiosk-freeze-watchdog.service

echo ""
echo "=== Pi 5 power note (GPIO 5V header) ==="
echo "If you power the Pi 5 via 5V GPIO pins (not official USB-C PD), a slow 5V rise time can prevent auto-boot."
echo "Use a strong 5V rail (often 5V/5A), short/thick cabling, avoid soft-start converters, or switch to the official Pi 5 PSU."
echo ""

echo "=== Install complete ==="
echo ""
echo "Canonical install dir: ${TARGET_DIR}"
echo "Hotkeys:"
echo " Ctrl+Alt+E = Exit kiosk"
echo " Ctrl+R     = Refresh"
echo ""
echo "Start kiosk:"
echo " bash ${TARGET_DIR}/enter-kiosk.sh"
