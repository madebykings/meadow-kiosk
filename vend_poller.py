#!/usr/bin/env python3
# /home/meadow/meadow-kiosk/vend_poller.py
#
# Poll WP for queued vend commands and execute them locally.
# Uses local /vend endpoint for fast path.
# ACKs commands back to WP so they are not re-run.

import json
import time
import os
import requests
from typing import Dict, Any, Optional

# ---------------------------------------------------------
# Paths / config
# ---------------------------------------------------------

CACHE_PATH = "/home/meadow/kiosk.config.cache.json"
LOG_PATH   = "/home/meadow/state/vend-poller.log"

POLL_INTERVAL = 2.0
HTTP_TIMEOUT  = 8.0

# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------

def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    line = f"{ts} [vend] {msg}"
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    print(line, flush=True)

# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def load_cfg() -> Dict[str, Any]:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def wp_api_base(cfg: Dict[str, Any]) -> str:
    domain = (cfg.get("domain") or "").rstrip("/")
    return domain + "/wp-json/meadow/v1"

def local_vend(motor: int) -> Dict[str, Any]:
    url = "http://127.0.0.1:8765/vend"
    r = requests.post(
        url,
        json={"motor": int(motor)},
        timeout=HTTP_TIMEOUT
    )
    r.raise_for_status()
    return r.json()

# ---------------------------------------------------------
# WP calls
# ---------------------------------------------------------

def get_next_vend(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    api = wp_api_base(cfg)
    kiosk_id = int(cfg.get("kiosk_id") or 0)
    key = (cfg.get("api_key") or "").strip()

    if not api or not kiosk_id or not key:
        log(f"missing auth (kiosk_id={kiosk_id}, key={'***' if key else ''})")
        return None

    params = {
        "kiosk_id": kiosk_id,
        "scope": "vend",
        "_t": int(time.time()),
        "key": key,
    }

    url = api + "/next-command"
    r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)

    if r.status_code >= 400:
        log(f"next-command HTTP {r.status_code}: {r.text[:200]}")
        r.raise_for_status()

    data = r.json()
    if not isinstance(data, dict):
        return None
    if not data.get("id") or not data.get("motor"):
        return None

    return data

def ack_command(cfg: Dict[str, Any], cmd_id: int) -> None:
    api = wp_api_base(cfg)
    kiosk_id = int(cfg.get("kiosk_id") or 0)
    key = (cfg.get("api_key") or "").strip()

    payload = {
        "id": int(cmd_id),
        "kiosk_id": kiosk_id,
        "key": key,
    }

    url = api + "/command-complete"
    r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)

    if r.status_code >= 400:
        log(f"ack HTTP {r.status_code}: {r.text[:200]}")
        r.raise_for_status()

# ---------------------------------------------------------
# Main loop
# ---------------------------------------------------------

def main() -> None:
    log("starting")

    while True:
        try:
            cfg = load_cfg()
            cmd = get_next_vend(cfg)

            if not cmd:
                time.sleep(POLL_INTERVAL)
                continue

            cmd_id = int(cmd["id"])
            motor  = int(cmd["motor"])

            log(f"received id={cmd_id} motor={motor}")

            result = local_vend(motor)
            ok = bool(result.get("success"))

            log(f"local vend result ok={ok} err={result.get('error','')}")

            # Always ACK so WP stops retrying
            ack_command(cfg, cmd_id)
            log(f"ack ok id={cmd_id}")

        except Exception as e:
            log(f"error: {e}")
            time.sleep(2)

if __name__ == "__main__":
    main()
