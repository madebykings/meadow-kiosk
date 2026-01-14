import requests
import time


def api_base(cfg):
    """
    Build the base API URL from the kiosk config.
    Example: "https://domain.com/wp-json/meadow/v1"
    """
    return cfg["domain"].rstrip("/") + "/wp-json/meadow/v1"


def next_command(cfg):
    """
    Ask WP if there is a vend command for this kiosk.

    Supports:
      - {"id": 123, "motor": 2}
      - [{"id": 123, "motor": 2}, ...]
      - {} or [] or None for no commands
    """
    url = api_base(cfg) + "/next-command"
    params = {
        "kiosk_id": cfg["kiosk_id"],
        "key": cfg["api_key"],
    }

    try:
        r = requests.get(url, params=params, timeout=5)
    except Exception as e:
        print("next_command: request failed:", e)
        return None

    try:
        data = r.json()
    except ValueError:
        print("next_command: response not JSON:", r.text)
        return None

    print("next_command raw:", data)

    # If WP returns a list, take the first command
    if isinstance(data, list):
        if not data:
            return None
        data = data[0]

    # If still not a dict, ignore
    if not isinstance(data, dict):
        print("next_command: unexpected data type:", type(data))
        return None

    motor = data.get("motor")
    if not motor:
        return None

    # Convert motor to int
    try:
        data["motor"] = int(motor)
    except:
        print("next_command: invalid motor:", motor)
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
        print("ack_command: error:", e)


def heartbeat(cfg, imei=None):
    """
    Regular heartbeat to WP with basic info.
    """
    url = api_base(cfg) + "/kiosk-heartbeat"
    payload = {
        "kiosk_id": cfg["kiosk_id"],
        "key": cfg.get("api_key"),
        "pi_git": (cfg.get("pi_git") or cfg.get("config_version")),
        "ts": int(time.time()),
    }
    if imei:
        payload["imei"] = imei

    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print("heartbeat: error:", e)


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
        print("set_screen_mode: error:", e)
