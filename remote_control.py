#!/usr/bin/env python3
# /home/meadow/meadow-kiosk/remote_control.py
#
# Poll WP for control commands and execute them locally.
#
# WP plugin auth expectation (per your PHP):
#   next-command:   requires kiosk_id + key
#   command-complete: requires kiosk_id + key
# where key == per-kiosk meta `_meadow_api_key` (NOT master provision key, NOT kiosk_token).

import json
import os
import time
import subprocess
from typing import Any, Dict, Optional

import requests

PROVISION_PATH = "/boot/provision.json"
CACHE_PATH = "/home/meadow/kiosk.config.cache.json"

KIOSK_URL_FILE = "/home/meadow/kiosk.url"

LOG_PATH = "/home/meadow/state/remote-control.log"
LAST_CMD_FILE = "/home/meadow/state/remote_control_last_cmd_id"

# Launcher cooldown (avoid rapid relaunch storms)
LAUNCHER_COOLDOWN_FILE = "/tmp/meadow_launcher_last_start"
LAUNCHER_COOLDOWN_SECS = int(os.environ.get("MEADOW_LAUNCHER_COOLDOWN_SECS", "10"))

POLL_INTERVAL = float(os.environ.get("MEADOW_CONTROL_POLL_INTERVAL", "2.5"))
HTTP_TIMEOUT = float(os.environ.get("MEADOW_CONTROL_HTTP_TIMEOUT", "8"))

CONTROL_SCOPE = "control"

# systemd unit names (we manage processes via systemd, not spawning)
SVC_BROWSER = "meadow-kiosk-browser.service"
SVC_LAUNCHER = "meadow-launcher.service"


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


def systemctl(*args: str) -> int:
    # use system-wide systemd
    cmd = ["sudo", "systemctl", *args]
    return subprocess.call(cmd)


def svc_is_active(unit: str) -> bool:
    try:
        r = subprocess.call(["systemctl", "is-active", "--quiet", unit])
        return r == 0
    except Exception:
        return False


def start_launcher_once() -> None:
    if svc_is_active(SVC_LAUNCHER):
        log("[control] launcher already active; not starting another")
        return
    if launcher_recently_started():
        log("[control] launcher cooldown active; not starting again yet")
        return

    # start (not enable) so it doesn't appear on boot
    systemctl("start", SVC_LAUNCHER)
    mark_launcher_started()
    log("[control] launcher started")


def stop_launcher() -> None:
    systemctl("stop", SVC_LAUNCHER)


def start_kiosk() -> None:
    # stop launcher if visible, then start kiosk browser watchdog
    stop_launcher()
    systemctl("start", SVC_BROWSER)
    log("[control] kiosk started (browser service)")


def exit_to_desktop() -> None:
    # stop kiosk browser watchdog; it will kill chromium in ExecStop
    systemctl("stop", SVC_BROWSER)
    log("[control] kiosk stopped (browser service)")
    start_launcher_once()


def reload_browser() -> None:
    systemctl("restart", SVC_BROWSER)
    log("[control] kiosk browser restarted")


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


def get_auth(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Your WP plugin expects:
      kiosk_id (int)
      key      (string)  == per-kiosk _meadow_api_key
    The per-kiosk key is returned in kiosk-config as `api_key` and cached.
    """
    kiosk_id = int(cfg.get("kiosk_id") or 0)
    key = str(cfg.get("api_key") or "").strip()
    if not kiosk_id or not key:
        return None
    return {"kiosk_id": kiosk_id, "key": key}


def wp_get_next_command(prov: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    api = wp_api_base(prov)
    auth = get_auth(cfg)
    if not api or not auth:
        return None

    params = {
        "kiosk_id": auth["kiosk_id"],
        "key": auth["key"],
        "scope": CONTROL_SCOPE,
        "_t": int(time.time()),
    }

    url = api + "/next-command"
    r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
    if not r.ok:
        # helpful log (you were seeing these)
        body = (r.text or "")[:400]
        log(f"[control] next-command HTTP error: {r.status_code} {r.reason} for url: {r.url} body={body}")
        r.raise_for_status()

    data = r.json()
    if not isinstance(data, dict):
        return None
    if not data.get("id") or not data.get("action"):
        return None
    return data


def wp_ack_command(prov: Dict[str, Any], cfg: Dict[str, Any], cmd_id: int, success: bool = True) -> None:
    api = wp_api_base(prov)
    auth = get_auth(cfg)
    if not api or not auth or not cmd_id:
        return

    payload = {
        "id": int(cmd_id),
        "kiosk_id": int(auth["kiosk_id"]),
        "key": str(auth["key"]),
        "success": bool(success),
        "scope": CONTROL_SCOPE,
    }

    url = api + "/command-complete"
    try:
        r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        if not r.ok:
            body = (r.text or "")[:400]
            log(f"[control] ack HTTP error: {r.status_code} {r.reason} body={body}")
            r.raise_for_status()
    except Exception as e:
        log(f"[control] ack failed for id={cmd_id}: {e}")


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
        # leave your existing update helper in place
        branch = str(payload.get("branch") or "main")
        log(f"[control] update_code starting (branch={branch})")
        subprocess.Popen(["bash", "/home/meadow/update-meadow.sh", branch])

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

            # already handled? just ack
            if cmd_id and cmd_id <= last_id:
                log(f"[control] cmd id={cmd_id} already handled (last={last_id}); trying ack only")
                wp_ack_command(prov, cfg, cmd_id, success=True)
                time.sleep(POLL_INTERVAL)
                continue

            # execute
            ok = True
            try:
                handle_command(cmd)
            except Exception as e:
                ok = False
                log(f"[control] command execution error id={cmd_id}: {e}")

            # record + ack
            if cmd_id:
                write_last_cmd_id(cmd_id)
                wp_ack_command(prov, cfg, cmd_id, success=ok)

        except Exception as e:
            log(f"[control] poll/handle error: {e}")
            time.sleep(3)


if __name__ == "__main__":
    main()
