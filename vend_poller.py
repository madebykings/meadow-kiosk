#!/usr/bin/env python3
# /home/meadow/meadow-kiosk/vend_poller.py
#
# Poll WP for vend commands (scope=vend) and execute them locally.
# This is ONLY for WP-admin queued vend/test-motor convenience.
# Customer vending remains fast-path (JS -> Pi /vend).

import json
import os
import time
import traceback
from typing import Any, Dict, Optional

import requests

PROVISION_PATH = "/boot/provision.json"
CACHE_PATH = "/home/meadow/kiosk.config.cache.json"
LOG_PATH = "/home/meadow/state/vend-poller.log"

POLL_INTERVAL = float(os.environ.get("MEADOW_VEND_POLL_INTERVAL", "2.5"))
HTTP_TIMEOUT = float(os.environ.get("MEADOW_VEND_HTTP_TIMEOUT", "8"))

SCOPE = "vend"

# Local Pi API (same machine)
LOCAL_PI_API = "http://127.0.0.1:8765"


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


def load_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def api_base(cfg: Dict[str, Any], prov: Dict[str, Any]) -> str:
    domain = (cfg.get("domain") or prov.get("domain") or "").rstrip("/")
    return domain + "/wp-json/meadow/v1"


def auth_key(cfg: Dict[str, Any], prov: Dict[str, Any]) -> str:
    # Use kiosk config api_key first (KEY1), fall back to provision api_key
    return (cfg.get("api_key") or prov.get("api_key") or "").strip()


def wp_next_vend(cfg: Dict[str, Any], prov: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    kiosk_id = int(cfg.get("kiosk_id") or 0)
    key = auth_key(cfg, prov)
    api = api_base(cfg, prov)

    if not kiosk_id or not key or not api:
        return None

    url = api + "/next-command"
    params = {"kiosk_id": kiosk_id, "scope": SCOPE, "_t": int(time.time()), "key": key}

    r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        log(f"[vend] next-command HTTP {r.status_code}: {r.text[:400]}")
    r.raise_for_status()

    data = r.json()

    # WP might return [] (none), {} (none), dict (one), list (many)
    if isinstance(data, list):
        if not data:
            return None
        data = data[0]
    if not isinstance(data, dict):
        return None

    # normalize: expect at least id + motor
    cmd_id = int(data.get("id") or 0)
    motor = data.get("motor") or data.get("motor_number")
    try:
        motor = int(motor) if motor is not None else 0
    except Exception:
        motor = 0

    if not cmd_id or not motor:
        return None

    return {"id": cmd_id, "motor": motor}


def wp_ack(cfg: Dict[str, Any], prov: Dict[str, Any], cmd_id: int, success: bool, error: str = "") -> None:
    key = auth_key(cfg, prov)
    api = api_base(cfg, prov)
    if not cmd_id or not key or not api:
        return

    url = api + "/command-complete"
    payload = {"id": int(cmd_id), "key": key, "success": bool(success), "error": (error or "")[:240], "ts": int(time.time())}
    try:
        r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        if r.status_code >= 400:
            log(f"[vend] ack HTTP {r.status_code}: {r.text[:400]}")
        r.raise_for_status()
    except Exception as e:
        log(f"[vend] ack failed id={cmd_id}: {e}")


def local_vend(motor: int) -> Dict[str, Any]:
    # Call the already-working local vend endpoint (we know it spins motors)
    r = requests.post(f"{LOCAL_PI_API}/vend", json={"motor": int(motor)}, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json() if r.headers.get("content-type","").lower().startswith("application/json") else {"raw": r.text}


def main() -> None:
    prov = load_json(PROVISION_PATH)
    if not prov:
        log("[vend] ERROR: missing /boot/provision.json")
        while True:
            time.sleep(10)

    log("[vend] starting")

    backoff = POLL_INTERVAL
    while True:
        cfg = load_json(CACHE_PATH)

        try:
            cmd = wp_next_vend(cfg, prov)
            if not cmd:
                time.sleep(backoff)
                # gentle backoff up to 10s to avoid hammering WP
                backoff = min(10.0, backoff + 0.5)
                continue

            backoff = POLL_INTERVAL  # reset backoff when we have work

            cmd_id = int(cmd["id"])
            motor = int(cmd["motor"])

            log(f"[vend] received id={cmd_id} motor={motor}")

            ok = False
            err = ""
            try:
                res = local_vend(motor)
                ok = bool(res.get("success", False))
                err = str(res.get("error") or "")
                log(f"[vend] local vend result ok={ok} err={err}")
            except Exception as e:
                ok = False
                err = f"{e}"
                log(f"[vend] local vend EXCEPTION: {e}")
                log(traceback.format_exc().splitlines()[-3:])

            wp_ack(cfg, prov, cmd_id, ok, err)

        except Exception as e:
            log(f"[vend] poll loop error: {e}")
            time.sleep(2.5)


if __name__ == "__main__":
    main()
