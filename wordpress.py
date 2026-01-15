import requests
import time


def api_base(cfg):
    """
    Build the base API URL from the kiosk config.
    Example: "https://domain.com/wp-json/meadow/v1"
    """
    return cfg["domain"].rstrip("/") + "/wp-json/meadow/v1"


def _get_key(cfg):
    """
    Support both legacy/new config shapes.
    Prefer api_key, fall back to key.
    """
    k = (cfg.get("api_key") or cfg.get("key") or "").strip()
    return k


def _mask_key(k: str) -> str:
    if not k:
        return "(missing)"
    if len(k) <= 6:
        return k[0] + "***"
    return k[:3] + "***" + k[-2:]


def next_command(cfg, scope=None):
    """
    Ask WP if there is a command for this kiosk.

    Supports:
      - {"id": 123, "motor": 2}
      - [{"id": 123, "motor": 2}, ...]
      - {} or [] or None for no commands
    """
    url = api_base(cfg) + "/next-command"

    key = _get_key(cfg)
    params = {
        "kiosk_id": cfg["kiosk_id"],
        "key": key,
    }
    if scope:
        params["scope"] = scope

    if not params["kiosk_id"] or not key:
        print(f"next_command: missing kiosk_id or key (kiosk_id={params.get('kiosk_id')}, key={_mask_key(key)})")
        return None

    try:
        r = requests.get(url, params=params, timeout=5)
        if r.status_code >= 400:
            print(f"next_command HTTP error: {r.status_code} body={r.text}")
            return None
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
    except Exception:
        print("next_command: invalid motor:", motor)
        return None

    return data


def ack_command(cfg, cmd_id, success=True):
    """
    Notify WP that a command was processed.
    """
    url = api_base(cfg) + "/command-complete"

    key = _get_key(cfg)
    payload = {
        "id": cmd_id,
        "key": key,
        "success": bool(success),
        "ts": int(time.time()),
    }

    if not key:
        print(f"ack_command: missing key (cmd_id={cmd_id})")
        return

    try:
        r = requests.post(url, json=payload, timeout=5)
        if r.status_code >= 400:
            print(f"ack_command HTTP error: {r.status_code} body={r.text}")
    except Exception as e:
        print("ack_command: error:", e)


def heartbeat(cfg, imei=None):
    """
    Regular heartbeat to WP with basic info.
    """
    url = api_base(cfg) + "/kiosk-heartbeat"

    key = _get_key(cfg)
    payload = {
        "kiosk_id": cfg["kiosk_id"],
        "key": key,
        "pi_git": (cfg.get("pi_git") or cfg.get("config_version")),
        "ts": int(time.time()),
    }
    if imei:
        payload["imei"] = imei

    if not key:
        print("heartbeat: missing key")
        return

    try:
        r = requests.post(url, json=payload, timeout=5)
        if r.status_code >= 400:
            print(f"heartbeat HTTP error: {r.status_code} body={r.text}")
    except Exception as e:
        print("heartbeat: error:", e)


def set_screen_mode(cfg, mode, order_id=None):
    """
    mode: 'browse', 'ads', 'vending', 'thankyou', 'error'
    """
    url = api_base(cfg) + "/kiosk-screen"

    key = _get_key(cfg)
    payload = {
        "kiosk_id": cfg["kiosk_id"],
        "key": key,
        "mode": mode,
    }
    if order_id is not None:
        payload["order_id"] = int(order_id)

    if not key:
        print("set_screen_mode: missing key")
        return

    try:
        r = requests.post(url, json=payload, timeout=5)
        if r.status_code >= 400:
            print(f"set_screen_mode HTTP error: {r.status_code} body={r.text}")
    except Exception as e:
        print("set_screen_mode: error:", e)
