import requests
import time


def api_base(cfg):
    """
    Build the base API URL from the kiosk config.
    Example domain in config:
      "domain": "https://greiga65.sg-host.com/"
    """
    return cfg["domain"].rstrip("/") + "/wp-json/meadow/v1"


def next_command(cfg):
    """
    Ask WP if there is a vend command for this kiosk.

    Original expected response (single object):
      {"id": 123, "motor": 2}

    Now also supports WP returning:
      [ {"id": 123, "motor": 2}, ... ]  (a list/queue of commands)
      {} or [] or None meaning "no command".
    """
    url = api_base(cfg) + "/next-command"
    params = {"kiosk_id": cfg["kiosk_id"], "key": cfg["api_key"]}

    try:
        r = requests.get(url, params=params, timeout=5)
    except Exception as e:
        print("next_command: request failed:", e)
        return None

    if r.status_code != 200:
        print("next_command: non-200 status:", r.status_code)
        return None

    try:
        data = r.json()
    except ValueError:
        print("next_command: response not JSON")
        return None

    print("next_command raw:", data)

    # --- Handle list response from WP ---
    if isinstance(data, list):
        if not data:
            # Empty list = no commands
            return None
        # Take the first command as "next"
        data = data[0]

    # --- Must be a dict now ---
    if not isinstance(data, dict):
        print("next_command: unexpected data type:", type(data))
        return None

    # --- Must contain a motor key ---
    motor = data.get("motor")
    if not motor:
        return None

    # Normalise/convert motor to int
    try:
        data["motor"] = int(motor)
    except (TypeError, ValueError):
        print("next_command: invalid motor value:", motor)
        return None

    return data


def ack_command(cfg, cmd_id, success=True):
    """
    Notify WP that a command was processed.
    """
    url = api_base(cfg) + "/command-complete"
    payload = {
        "id": cmd_id,
        "key": cfg["api_key"],
        "success": bool(success),
        "ts": int(time.time()),
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print("ack_command: request failed:", e)


def heartbeat(cfg, imei=None):
    """
    Regular heartbeat to WP with basic info.
    """
    url = api_base(cfg) + "/kiosk-heartbeat"
    payload = {
        "kiosk_id": cfg["kiosk_id"],
        "config_version": cfg.get("config_version"),
        "ts": int(time.time()),
    }
    if imei:
        payload["imei"] = imei
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print("heartbeat: request failed:", e)


def set_screen_mode(cfg, mode, order_id=None):
    """
    mode: 'browse', 'ads', 'vending', 'thankyou', 'error'
    """
    url = api_base(cfg) + "/kiosk-screen"
    payload = {
        "kiosk_id": cfg["kiosk_id"],
        "key": cfg["api_key"],
        "mode": mode,
    }
    if order_id is not None:
        payload["order_id"] = int(order_id)
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print("set_screen_mode: request failed:", e)
