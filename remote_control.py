#!/usr/bin/env python3
# /home/meadow/meadow-kiosk/remote_control.py
#
# Poll WP for control commands (exit/enter/reload/set_url/reboot/shutdown/update_code)
# and execute them locally.
#
# HARDENING INCLUDED:
# - Never spawn multiple kiosk-launchers (pgrep guard + cooldown).
# - Dedupe commands by id (prevents command storms if ack fails / service restarts).
# - Always attempts to ack (even if command already handled).

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

LAUNCHER = "/home/meadow/kiosk-launcher.py"
KIOSK_BROWSER = "/home/meadow/kiosk-browser.sh"
UPDATE_SCRIPT = "/home/meadow/update-meadow.sh"

LOG_PATH = "/home/meadow/state/remote-control.log"

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


def build_env_for_gui() -> Dict[str, str]:
    """
    Ensure spawned processes can talk to the GUI session.
    For labwc/Wayland this isn't always needed, but keeping DISPLAY/XAUTHORITY
    doesn't hurt for Xwayland setups.
    """
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    env.setdefault("XAUTHORITY", "/home/meadow/.Xauthority")
    return env


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
# Process helpers
# -------------------------------------------------------------------

def pkill_chromium() -> None:
    # broad but targeted at kiosk chromium invocations
    subprocess.call(["pkill", "-f", "chromium.*--kiosk"])
    subprocess.call(["pkill", "-f", "chromium-browser.*--kiosk"])
    subprocess.call(["pkill", "-f", "chromium --kiosk"])
    subprocess.call(["pkill", "-f", "chromium-browser --kiosk"])


def is_chromium_running() -> bool:
    try:
        out = subprocess.check_output(["pgrep", "-f", "chromium.*--kiosk"], text=True).strip()
        return bool(out)
    except Exception:
        return False


def kill_launcher_processes() -> None:
    # clean up any duplicated launchers from a previous bug state
    subprocess.call(["pkill", "-f", LAUNCHER])


def is_launcher_running() -> bool:
    try:
        out = subprocess.check_output(["pgrep", "-f", LAUNCHER], text=True).strip()
        return bool(out)
    except Exception:
        return False


def start_launcher_once(env: Dict[str, str]) -> None:
    """
    Start the launcher if not already running.
    Includes cooldown to prevent rapid relaunch storms.
    """
    if is_launcher_running():
        log("[control] launcher already running; not starting another")
        return

    if launcher_recently_started():
        log("[control] launcher cooldown active; not starting again yet")
        return

    # kill any stale dupes then start
    kill_launcher_processes()
    time.sleep(0.2)

    subprocess.Popen(["python3", LAUNCHER], env=env)
    mark_launcher_started()
    log("[control] launcher started")


def start_kiosk(env: Dict[str, str]) -> None:
    """
    Enter kiosk mode:
    - remove stop flag
    - kill any launcher
    - start kiosk loop (kiosk-browser.sh)
    """
    try:
        if os.path.exists(STOP_FLAG):
            os.remove(STOP_FLAG)
    except Exception:
        pass

    kill_launcher_processes()
    subprocess.Popen([KIOSK_BROWSER], env=env)
    log("[control] kiosk started")


def exit_to_desktop(env: Dict[str, str]) -> None:
    """
    Exit kiosk mode:
    - create stop flag so kiosk loop exits
    - kill chromium kiosk
    - show launcher (only once)
    """
    try:
        with open(STOP_FLAG, "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
    except Exception:
        pass

    pkill_chromium()
    log("[control] kiosk stopped; chromium killed")

    start_launcher_once(env)


def reload_browser(env: Dict[str, str]) -> None:
    """
    Reload kiosk chromium.
    If chromium isn't running, start kiosk.
    """
    if is_chromium_running():
        pkill_chromium()
        time.sleep(0.5)
        subprocess.Popen([KIOSK_BROWSER], env=env)
        log("[control] chromium reloaded (kiosk-browser.sh relaunched)")
    else:
        log("[control] chromium not running; starting kiosk")
        start_kiosk(env)


def set_url_and_reload(env: Dict[str, str], url: str) -> None:
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

    reload_browser(env)


# -------------------------------------------------------------------
# WP polling + acknowledgements
# -------------------------------------------------------------------

def wp_get_next_command(prov: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    api = wp_api_base(prov)
    kiosk_id = int(cfg.get("kiosk_id") or 0)
    key = prov.get("api_key") or ""

    if not api or not kiosk_id or not key:
        return None

    params = {
        "kiosk_id": kiosk_id,
        "scope": CONTROL_SCOPE,
        "_t": int(time.time()),
    }

    headers = {
        "X-API-KEY": str(key),
        "Cache-Control": "no-store",
    }

    url = api + "/next-command"
    r = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
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

def handle_command(env: Dict[str, str], cmd: Dict[str, Any]) -> None:
    cmd_id = int(cmd.get("id") or 0)
    action = str(cmd.get("action") or "")
    payload = cmd.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    log(f"[control] received command id={cmd_id} action={action} payload={payload}")

    if action == "enter_kiosk":
        start_kiosk(env)

    elif action == "exit_kiosk":
        exit_to_desktop(env)

    elif action == "reload":
        reload_browser(env)

    elif action == "set_url":
        set_url_and_reload(env, str(payload.get("url") or ""))

    elif action == "update_code":
        branch = str(payload.get("branch") or "main")
        log(f"[control] update_code starting (branch={branch})")
        subprocess.Popen(["bash", UPDATE_SCRIPT, branch], env=env)

    elif action == "reboot":
        log("[control] reboot requested")
        subprocess.call(["sudo", "reboot"])

    elif action == "shutdown":
        log("[control] shutdown requested")
        subprocess.call(["sudo", "shutdown", "-h", "now"])

    else:
        log(f"[control] unknown action: {action}")


def main() -> None:
    env = build_env_for_gui()

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
            handle_command(env, cmd)

            # Mark handled + ack
            if cmd_id:
                write_last_cmd_id(cmd_id)
                wp_ack_command(prov, cmd_id)

        except Exception as e:
            log(f"[control] poll/handle error: {e}")
            time.sleep(3)


if __name__ == "__main__":
    main()
