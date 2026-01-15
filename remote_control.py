#!/usr/bin/env python3
# /home/meadow/meadow-kiosk/remote_control.py
#
# Poll WP for control commands (enter/exit/reload/set_url/reboot/shutdown/update_code)
# and execute them locally.
#
# HARDENING:
# - Dedupe commands by id
# - Uses systemd as source of truth (no duplicate processes)
# - Always tries to ack (even if already handled)
# - Auth sent as query param key=... (matches WP plugin expectations)
# - Backoff on repeated failures (prevents "death by 1000 polls")

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

# Command dedupe
LAST_CMD_FILE = "/home/meadow/state/remote_control_last_cmd_id"

POLL_INTERVAL = float(os.environ.get("MEADOW_CONTROL_POLL_INTERVAL", "2.5"))
HTTP_TIMEOUT = float(os.environ.get("MEADOW_CONTROL_HTTP_TIMEOUT", "8"))

CONTROL_SCOPE = "control"

# systemd units
KIOSK_BROWSER_UNIT = "meadow-kiosk-browser.service"
LAUNCHER_UNIT = "meadow-launcher.service"


def _mask(val: str) -> str:
    v = (val or "").strip()
    if not v:
        return "(missing)"
    if len(v) <= 6:
        return v[0] + "***"
    return v[:3] + "***" + v[-2:]


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


# -------------------------------------------------------------------
# systemd helpers (source of truth)
# -------------------------------------------------------------------

def systemctl(*args: str) -> int:
    """
    Run systemctl via sudo (requires sudoers drop-in added by install.sh).
    """
    cmd = ["sudo", "systemctl", *args]
    return subprocess.call(cmd)


def enter_kiosk() -> None:
    try:
        if os.path.exists(STOP_FLAG):
            os.remove(STOP_FLAG)
    except Exception:
        pass

    systemctl("stop", LAUNCHER_UNIT)
    systemctl("start", KIOSK_BROWSER_UNIT)
    log("[control] enter_kiosk -> start meadow-kiosk-browser, stop meadow-launcher")


def exit_kiosk() -> None:
    try:
        with open(STOP_FLAG, "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
    except Exception:
        pass

    systemctl("stop", KIOSK_BROWSER_UNIT)
    systemctl("start", LAUNCHER_UNIT)
    log("[control] exit_kiosk -> stop meadow-kiosk-browser, start meadow-launcher")


def reload_kiosk() -> None:
    systemctl("restart", KIOSK_BROWSER_UNIT)
    log("[control] reload -> restart meadow-kiosk-browser")


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

    reload_kiosk()


# -------------------------------------------------------------------
# WP polling + acknowledgements
# -------------------------------------------------------------------

def _get_wp_key(prov: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    """
    Prefer provision api_key (master), fall back to cached config keys.
    """
    return (prov.get("api_key") or cfg.get("api_key") or cfg.get("key") or "").strip()


def wp_get_next_command(prov: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    api = wp_api_base(prov)
    kiosk_id = int(cfg.get("kiosk_id") or 0)
    key = _get_wp_key(prov, cfg)

    if not api or not kiosk_id or not key:
        log(f"[control] missing auth for next-command (kiosk_id={kiosk_id}, key={_mask(key)})")
        return None

    params = {
        "kiosk_id": kiosk_id,
        "key": key,                 # ✅ WP expects key in params
        "scope": CONTROL_SCOPE,
        "_t": int(time.time()),
    }

    # Optional token, if your endpoint expects it (harmless if ignored)
    token = (cfg.get("token") or prov.get("token") or "").strip()
    if token:
        params["token"] = token

    headers = {
        "X-API-KEY": key,           # keep for future / debugging
        "Cache-Control": "no-store",
    }

    url = api + "/next-command"

    r = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        body = (r.text or "")[:400]
        log(f"[control] next-command HTTP error: {r.status_code} {r.reason} body={body}")
    r.raise_for_status()

    data = r.json()
    if not isinstance(data, dict):
        return None
    if not data.get("id") or not data.get("action"):
        return None
    return data


def wp_ack_command(prov: Dict[str, Any], cfg: Dict[str, Any], cmd_id: int) -> None:
    api = wp_api_base(prov)
    key = _get_wp_key(prov, cfg)
    if not api or not cmd_id or not key:
        return

    url = api + "/command-complete"

    # ✅ Send key in JSON too (some WP handlers validate it there)
    payload = {"id": int(cmd_id), "key": key, "ts": int(time.time())}

    headers = {"X-API-KEY": key, "Cache-Control": "no-store"}

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
        if r.status_code >= 400:
            body = (r.text or "")[:400]
            log(f"[control] ack HTTP error: {r.status_code} {r.reason} body={body}")
        r.raise_for_status()
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
        enter_kiosk()

    elif action == "exit_kiosk":
        exit_kiosk()

    elif action == "reload":
        reload_kiosk()

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

    # Don't print secrets in logs
    log(f"[control] provision loaded (domain={prov.get('domain')}, api_key={_mask(str(prov.get('api_key') or ''))})")

    backoff = 0.0
    failures = 0

    while True:
        try:
            cfg = load_cached_config()

            cmd = wp_get_next_command(prov, cfg)
            if not cmd:
                failures = 0
                backoff = 0.0
                time.sleep(POLL_INTERVAL)
                continue

            cmd_id = int(cmd.get("id") or 0)
            last_id = read_last_cmd_id()

            if cmd_id and cmd_id <= last_id:
                log(f"[control] cmd id={cmd_id} already handled (last={last_id}); trying ack only")
                wp_ack_command(prov, cfg, cmd_id)
                time.sleep(POLL_INTERVAL)
                continue

            handle_command(cmd)

            if cmd_id:
                write_last_cmd_id(cmd_id)
                wp_ack_command(prov, cfg, cmd_id)

            failures = 0
            backoff = 0.0

        except Exception as e:
            failures += 1
            backoff = min(30.0, 2.0 * failures)  # 2s,4s,6s... up to 30s
            log(f"[control] poll/handle error: {e} (failures={failures}, backoff={backoff}s)")
            time.sleep(backoff if backoff else 3.0)


if __name__ == "__main__":
    main()
