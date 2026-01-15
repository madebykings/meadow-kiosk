#!/usr/bin/env python3
# /home/meadow/meadow-kiosk/remote_control.py
#
# Poll WP for control commands (exit/enter/reload/set_url/reboot/shutdown/update_code)
# and execute them locally.
#
# HARDENING INCLUDED:
# - Dedupe commands by id (prevents command storms if ack fails / service restarts).
# - Always attempts to ack (even if command already handled).
# - Kiosk and launcher are controlled via systemd services (single owner).
# - Launcher start is guarded with a cooldown (prevents spawn storms).

import json
import os
import time
import subprocess
from typing import Any, Dict, Optional

import requests

# -------------------------------------------------------------------
# Paths / config
# -------------------------------------------------------------------

PROVISION_PATH = "/boot/provision.json"
CACHE_PATH = "/home/meadow/kiosk.config.cache.json"

KIOSK_URL_FILE = "/home/meadow/kiosk.url"
STOP_FLAG = "/tmp/meadow_kiosk_stop"

UPDATE_SCRIPT = "/home/meadow/update-meadow.sh"

LOG_PATH = "/home/meadow/state/remote-control.log"

# systemd units (recommended architecture)
KIOSK_SERVICE = os.environ.get("MEADOW_KIOSK_SERVICE", "meadow-kiosk-browser.service")
LAUNCHER_SERVICE = os.environ.get("MEADOW_LAUNCHER_SERVICE", "meadow-launcher.service")

# Command dedupe
LAST_CMD_FILE = "/home/meadow/state/remote_control_last_cmd_id"

# Launcher cooldown (avoid rapid relaunch storms if launcher exits instantly)
LAUNCHER_COOLDOWN_FILE = "/tmp/meadow_launcher_last_start"
LAUNCHER_COOLDOWN_SECS = int(os.environ.get("MEADOW_LAUNCHER_COOLDOWN_SECS", "10"))

POLL_INTERVAL = float(os.environ.get("MEADOW_CONTROL_POLL_INTERVAL", "2.5"))
HTTP_TIMEOUT = float(os.environ.get("MEADOW_CONTROL_HTTP_TIMEOUT", "8"))

# provision.json should contain:
# { "domain": "https://meadowvending.com", "api_key": "..." }
CONTROL_SCOPE = "control"


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    line = f"{ts} {msg}"
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)


def load_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_provision() -> Dict[str, Any]:
    return load_json(PROVISION_PATH) or {}


def load_cached_config() -> Dict[str, Any]:
    return load_json(CACHE_PATH) or {}


def wp_api_base(prov: Dict[str, Any]) -> str:
    domain = (prov.get("domain") or "").rstrip("/")
    return domain + "/wp-json/meadow/v1"


# -------------------------------------------------------------------
# Dedupe helpers
# -------------------------------------------------------------------

def read_last_cmd_id() -> int:
    try:
        with open(LAST_CMD_FILE, "r", encoding="utf-8") as f:
            return int((f.read() or "0").strip() or 0)
    except Exception:
        return 0


def write_last_cmd_id(cmd_id: int) -> None:
    try:
        os.makedirs(os.path.dirname(LAST_CMD_FILE), exist_ok=True)
        with open(LAST_CMD_FILE, "w", encoding="utf-8") as f:
            f.write(str(int(cmd_id)))
    except Exception:
        pass


def launcher_recently_started() -> bool:
    try:
        if not os.path.exists(LAUNCHER_COOLDOWN_FILE):
            return False
        age = time.time() - os.path.getmtime(LAUNCHER_COOLDOWN_FILE)
        return age < LAUNCHER_COOLDOWN_SECS
    except Exception:
        return False


def mark_launcher_started() -> None:
    try:
        with open(LAUNCHER_COOLDOWN_FILE, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except Exception:
        pass


# -------------------------------------------------------------------
# systemd helpers
# -------------------------------------------------------------------

def systemctl(*args: str) -> int:
    return subprocess.call(["systemctl", *args])


def systemctl_user(*args: str) -> int:
    """
    If you later decide to run these as --user units, switch calls to this.
    For now we assume system units under /etc/systemd/system.
    """
    return subprocess.call(["systemctl", "--user", *args])


def is_active(unit: str) -> bool:
    return subprocess.call(["systemctl", "is-active", "--quiet", unit]) == 0


def start_unit(unit: str) -> None:
    systemctl("start", unit)


def stop_unit(unit: str) -> None:
    systemctl("stop", unit)


def restart_unit(unit: str) -> None:
    systemctl("restart", unit)


# -------------------------------------------------------------------
# Process helpers (only for chromium cleanup & safety)
# -------------------------------------------------------------------

def pkill_chromium() -> None:
    subprocess.call(["pkill", "-f", "chromium.*--kiosk"])
    subprocess.call(["pkill", "-f", "chromium-browser.*--kiosk"])
    subprocess.call(["pkill", "-f", "chromium --kiosk"])
    subprocess.call(["pkill", "-f", "chromium-browser --kiosk"])


# -------------------------------------------------------------------
# Actions
# -------------------------------------------------------------------

def start_launcher_once() -> None:
    """
    Start launcher via systemd (guarded) to avoid popup storms.
    """
    if is_active(LAUNCHER_SERVICE):
        log("[control] launcher service already active; not starting another")
        return

    if launcher_recently_started():
        log("[control] launcher cooldown active; not starting again yet")
        return

    # Start launcher service
    start_unit(LAUNCHER_SERVICE)
    mark_launcher_started()
    log("[control] launcher service started")


def stop_launcher() -> None:
    stop_unit(LAUNCHER_SERVICE)
    log("[control] launcher service stopped")


def start_kiosk() -> None:
    """
    Enter kiosk mode:
    - remove stop flag
    - stop launcher
    - start kiosk browser service
    """
    try:
        if os.path.exists(STOP_FLAG):
            os.remove(STOP_FLAG)
    except Exception:
        pass

    # Ensure launcher is not in the way
    stop_launcher()

    # Ensure a clean chromium slate (optional but helpful)
    pkill_chromium()

    start_unit(KIOSK_SERVICE)
    log("[control] kiosk service started")


def stop_kiosk() -> None:
    """
    Stop kiosk browser service and kill chromium just in case.
    """
    stop_unit(KIOSK_SERVICE)
    pkill_chromium()
    log("[control] kiosk service stopped; chromium killed")


def exit_to_desktop() -> None:
    """
    Exit kiosk mode:
    - create stop flag so kiosk loop exits
    - stop kiosk service
    - show launcher (guarded)
    """
    try:
        with open(STOP_FLAG, "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
    except Exception:
        pass

    stop_kiosk()
    start_launcher_once()


def reload_browser() -> None:
    """
    Restart kiosk browser service (re-reads kiosk.url).
    """
    # If service isn't active, starting is a sensible "reload"
    if is_active(KIOSK_SERVICE):
        restart_unit(KIOSK_SERVICE)
        log("[control] kiosk service restarted")
    else:
        log("[control] kiosk service not active; starting kiosk")
        start_kiosk()


def set_url_and_reload(url: str) -> None:
    url = (url or "").strip()
    if not url:
        log("[control] set_url: empty url; ignored")
        return

    try:
        with open(KIOSK_URL_FILE, "w", encoding="utf-8") as f:
            f.write(url + "\n")
        log(f"[control] kiosk.url updated to: {url}")
    except Exception as e:
        log(f"[control] failed writing kiosk.url: {e}")
        return

    reload_browser()


# -------------------------------------------------------------------
# WP polling + acknowledgements
# -------------------------------------------------------------------

def wp_get_next_command(prov: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    api = wp_api_base(prov)
    kiosk_id = int(cfg.get("kiosk_id") or 0)

    # WP expects a "key" — use kiosk_token (per provision.json)
    kiosk_token = str(prov.get("kiosk_token") or "").strip()

    # Keep api_key too (some endpoints may still use it)
    api_key = str(prov.get("api_key") or "").strip()

    if not api or not kiosk_id or not kiosk_token:
        return None

    params = {
    "kiosk_id": kiosk_id,
    "scope": CONTROL_SCOPE,
    "key": api_key,               # ✅ WP "key" == API KEY
    "kiosk_token": kiosk_token,   # optional extra (future hardening)
    "_t": int(time.time()),
    }

    headers = {
    "Cache-Control": "no-store",
    }
    if api_key:
        headers["X-API-KEY"] = api_key
    if kiosk_token:
        headers["X-KIOSK-TOKEN"] = kiosk_token

    url = api + "/next-command"

    try:
        r = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
    except requests.HTTPError as e:
        body = ""
        try:
            body = (r.text or "")[:1200]
        except Exception:
            pass
        log(f"[control] next-command HTTP error: {e} body={body}")
        raise

    data = r.json()
    if not isinstance(data, dict):
        return None

    if not data.get("id") or not data.get("action"):
        return None

    return data

def wp_ack_command(prov: Dict[str, Any], cmd_id: int) -> None:
    api = wp_api_base(prov)
    key = prov.get("api_key") or ""
    if not api or not cmd_id or not key:
        return

    headers = {"X-API-KEY": str(key), "Cache-Control": "no-store"}
    url = api + "/command-complete"
    try:
        requests.post(url, json={"id": int(cmd_id)}, headers=headers, timeout=HTTP_TIMEOUT).raise_for_status()
    except Exception as e:
        log(f"[control] ack failed for id={cmd_id}: {e}")


# -------------------------------------------------------------------
# Main loop
# -------------------------------------------------------------------

def handle_command(cmd: Dict[str, Any]) -> None:
    cmd_id = int(cmd.get("id") or 0)
    action = str(cmd.get("action") or "")
    payload = cmd.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    log(f"[control] received command id={cmd_id} action={action} payload={payload}")

    if action == "enter_kiosk":
        start_kiosk()

    elif action == "exit_kiosk":
        exit_to_desktop()

    elif action == "reload":
        reload_browser()

    elif action == "set_url":
        set_url_and_reload(str(payload.get("url") or ""))

    elif action == "update_code":
        branch = str(payload.get("branch") or "main")
        log(f"[control] update_code starting (branch={branch})")
        subprocess.Popen(["bash", UPDATE_SCRIPT, branch])

    elif action == "reboot":
        log("[control] reboot requested")
        subprocess.call(["sudo", "reboot"])

    elif action == "shutdown":
        log("[control] shutdown requested")
        subprocess.call(["sudo", "shutdown", "-h", "now"])

    else:
        log(f"[control] unknown action: {action}")


def main() -> None:
    prov = load_provision()
    if not prov:
        log("[control] ERROR: missing /boot/provision.json")
        while True:
            time.sleep(10)

    log(f"[control] loaded provision: {prov}")

    while True:
        try:
            cfg = load_cached_config()
            cmd = wp_get_next_command(prov, cfg)
            if not cmd:
                time.sleep(POLL_INTERVAL)
                continue

            cmd_id = int(cmd.get("id") or 0)
            last_id = read_last_cmd_id()

            # If already handled, do NOT execute again—just try ack.
            if cmd_id and cmd_id <= last_id:
                log(f"[control] cmd id={cmd_id} already handled (last={last_id}); trying ack only")
                wp_ack_command(prov, cmd_id)
                time.sleep(POLL_INTERVAL)
                continue

            # Execute
            handle_command(cmd)

            # Mark handled + ack
            if cmd_id:
                write_last_cmd_id(cmd_id)
                wp_ack_command(prov, cmd_id)

        except Exception as e:
            log(f"[control] poll/handle error: {e}")
            time.sleep(3)


if __name__ == "__main__":
    main()
