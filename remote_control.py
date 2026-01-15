#!/usr/bin/env python3
import os
import time
import json
import subprocess
from typing import Any, Dict, Optional

import requests

from config_remote import get_config
from modem import get_imei

STOP_FLAG = "/tmp/meadow_kiosk_stop"
URL_FILE = "/home/meadow/kiosk.url"
HEARTBEAT_FILE = os.environ.get("MEADOW_HEARTBEAT_FILE", "/tmp/meadow_kiosk_heartbeat")
WP_HEARTBEAT_FILE = os.environ.get("MEADOW_WP_HEARTBEAT_FILE", "/tmp/meadow_wp_heartbeat")

DISPLAY_ENV = {
    "DISPLAY": os.environ.get("DISPLAY", ":0"),
    "XAUTHORITY": os.environ.get("XAUTHORITY", "/home/meadow/.Xauthority"),
}

def api_base(cfg: Dict[str, Any]) -> str:
    return cfg["domain"].rstrip("/") + "/wp-json/meadow/v1"

def fetch_control_command(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Fetch a non-vend control command from WP.

    Expected WP response (examples):
      {} or [] -> none
      {"id": 10, "action": "reload"} 
      {"id": 11, "action": "set_url", "url": "https://..."}
      {"id": 12, "action": "exit_kiosk"}
      {"id": 13, "action": "enter_kiosk"}
      {"id": 14, "action": "reboot"}

    Uses existing endpoint /next-command with scope=control so you don't need a new route.
    Vend commands can continue to use the same endpoint without scope or with scope=vend.
    """
    url = api_base(cfg) + "/next-command"
    params = {
        "kiosk_id": cfg.get("kiosk_id"),
        "key": cfg.get("api_key"),
        "scope": "control",
    }

    def touch_wp_heartbeat() -> None:
        try:
            with open(WP_HEARTBEAT_FILE, "a"):
                os.utime(WP_HEARTBEAT_FILE, None)
        except Exception:
            pass

    def touch_heartbeat() -> None:
        try:
            with open(HEARTBEAT_FILE, "w") as f:
                f.write(str(int(time.time())))
        except Exception:
            pass

    try:
        r = requests.get(url, params=params, timeout=5)
        if r.status_code != 200:
            return None
        # If we can reach WP at all, update heartbeat so the kiosk watchdog
        # can detect "hung" sessions vs offline connectivity.
        touch_heartbeat()
        touch_wp_heartbeat()
        data = r.json()
    except Exception:
        return None

    # WP might return list; take first
    if isinstance(data, list):
        if not data:
            return None
        data = data[0]

    if not isinstance(data, dict) or not data:
        return None

    # Ignore vend-style commands (motor) in this poller
    if data.get("motor") and not data.get("action"):
        return None

    if not data.get("action") or not data.get("id"):
        return None

    return data

def ack(cfg: Dict[str, Any], cmd_id: int, success: bool, note: str = "") -> None:
    url = api_base(cfg) + "/command-complete"
    payload = {
        "id": int(cmd_id),
        "key": cfg.get("api_key"),
        "success": bool(success),
        "ts": int(time.time()),
    }
    if note:
        payload["note"] = note[:200]
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception:
        pass

def write_url(new_url: str) -> None:
    new_url = (new_url or "").strip()
    if not new_url:
        return
    # Basic validation: keep it https and avoid obvious garbage
    if not (new_url.startswith("https://") or new_url.startswith("http://")):
        return
    tmp_path = URL_FILE + ".tmp"
    os.makedirs(os.path.dirname(URL_FILE), exist_ok=True)
    with open(tmp_path, "w") as f:
        f.write(new_url + "
")
    os.replace(tmp_path, URL_FILE)

def clear_stop_flag() -> None:
    try:
        os.remove(STOP_FLAG)
    except FileNotFoundError:
        pass

def set_stop_flag() -> None:
    with open(STOP_FLAG, "w") as f:
        f.write(str(int(time.time())))

def pkill_chromium() -> None:
    # Be tolerant across distros/package names
    subprocess.call(["pkill", "-f", "chromium.*--kiosk"])
    subprocess.call(["pkill", "-f", "chromium-browser.*--kiosk"])
    subprocess.call(["pkill", "-f", "chromium --kiosk"])
    subprocess.call(["pkill", "-f", "chromium-browser --kiosk"])

def is_kiosk_running() -> bool:
    try:
        out = subprocess.check_output(["pgrep", "-f", "chromium.*--kiosk"], text=True).strip()
        return bool(out)
    except Exception:
        return False

def start_kiosk_browser() -> None:
    # Start the loop script; it will relaunch chromium if it closes.
    env = os.environ.copy()
    env.update(DISPLAY_ENV)
    subprocess.Popen(["/home/meadow/kiosk-browser.sh"], env=env)

def start_launcher_popup() -> None:
    env = os.environ.copy()
    env.update(DISPLAY_ENV)
    subprocess.Popen(["python3", "/home/meadow/kiosk-launcher.py"], env=env)

def handle_action(action: str, cmd: Dict[str, Any]) -> (bool, str):
    action = (action or "").strip().lower()
    if action == "reload":
        clear_stop_flag()
        pkill_chromium()
        return True, "reloaded"
    if action == "set_url":
        url = (cmd.get("url") or cmd.get("kiosk_url") or "").strip()
        if not url:
            return False, "missing url"
        write_url(url)
        clear_stop_flag()
        pkill_chromium()
        return True, "url updated"
    if action == "exit_kiosk" or action == "enter_desktop":
        set_stop_flag()
        pkill_chromium()
        # bring launcher back for local operator
        start_launcher_popup()
        return True, "exited"
    if action == "enter_kiosk":
        clear_stop_flag()
        if not is_kiosk_running():
            start_kiosk_browser()
        return True, "entered"
    if action == "update_code":
        # Pull latest code and restart services. Runs async; check /home/meadow/update.log.
        payload = cmd.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        branch = (payload.get("branch") or cmd.get("branch") or "main")
        subprocess.Popen(["bash", "/home/meadow/update-meadow.sh", str(branch)])
        return True, f"updating ({branch})"

    if action == "reboot":
        # Needs passwordless sudo for meadow user (documented in README)
        subprocess.call(["sudo", "reboot"])
        return True, "rebooting"
    if action == "shutdown":
        subprocess.call(["sudo", "shutdown", "-h", "now"])
        return True, "shutting down"
    return False, f"unknown action: {action}"

def maybe_seed_url_from_cfg(cfg: Dict[str, Any]) -> None:
    # If WP config provides kiosk_url and local file doesn't exist, write it.
    if os.path.exists(URL_FILE):
        return
    url = (cfg.get("kiosk_url") or cfg.get("ui_url") or "").strip()
    if url:
        write_url(url)

def main():
    imei = get_imei()  # best effort
    cfg = get_config(imei=imei)
    maybe_seed_url_from_cfg(cfg)

    poll_s = float(os.environ.get("MEADOW_CONTROL_POLL", "2.0"))

    while True:
        try:
            cmd = fetch_control_command(cfg)
            if not cmd:
                time.sleep(poll_s)
                continue

            ok, note = handle_action(cmd.get("action"), cmd)
            ack(cfg, cmd.get("id"), ok, note=note)
            time.sleep(0.5)
        except Exception:
            time.sleep(poll_s)

if __name__ == "__main__":
    main()
