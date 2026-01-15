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
    Support both legacy and current config shapes.
    Prefer api_key, fall back to key.
    """
    return (cfg.get("api_key") or cfg.get("key") or "").strip()


def _mask(val):
    if not val:
        return "(missing)"
    if len(val) <= 6:
        return val[0] + "***"
    return val[:3] + "***" + val[-2:]


def _auth_params(cfg):
    """
    Build auth params consistently for all WP calls.
    """
    p = {
        "kiosk_id": cfg.get("kiosk_id"),
        "key": _get_key(cfg),
        "_t": int(time.time()),
    }
    if cfg.get("token"):
        p["token"] = cfg["token"]
    return p


def next_command(cfg, scope=None):
    """
    Ask WP if there is a command for this kiosk.

    Supports:
      - {"id": 123, "motor": 2}
      - [{"id": 123, "motor": 2}, ...]
      - {} or [] or None for no commands
    """
    url = api_base(cfg) + "/next-command"
    params = _auth_params(cfg)

    if scope:
        params["scope"] = scope

    if not params.get("kiosk_id") or not params.get("key"):
        print(
            f"next_command: missing auth "
            f"(kiosk_id={params.get('kiosk_id')}, key={_mask(params.get('key'))})"
        )
        return None

    try:
        r = requests.get(url, params=params, timeout=5)
        if r.status_code >= 400:
            print(f"next_command HTTP {r.status_code}: {r.text}")
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
    if motor is None:
        return None

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
    payload = {
        "id": int(cmd_id),
        "success": bool(success),
        "ts": int(time.time()),
    }
    payload.update(_auth_params(cfg))

    if not payload.get("key"):
        print(f"ack_command: missing key (cmd_id={cmd_id})")
        return

    try:
        r = requests.post(url, json=payload, timeout=5)
        if r.status_code >= 400:
            print(f"ack_command HTTP {r.status_code}: {r.text}")
    except Exception as e:
        print("ack_command: error:", e)


def heartbeat(cfg, imei=None):
    """
    Regular heartbeat to WP with basic info.
    """
    url = api_base(cfg) + "/kiosk-heartbeat"
    payload = {
        "pi_git": (cfg.get("pi_git") or cfg.get("config_version")),
        "ts": int(time.time()),
    }
    payload.update(_auth_params(cfg))

    if imei:
        payload["imei"] = imei

    if not payload.get("key"):
        print("heartbeat: missing key")
        return

    try:
        r = requests.post(url, json=payload, timeout=5)
        if r.status_code >= 400:
            print(f"heartbeat HTTP {r.status_code}: {r.text}")
    except Exception as e:
        print("heartbeat: error:", e)


def set_screen_mode(cfg, mode, order_id=None):
    """
    mode: 'browse', 'ads', 'vending', 'thankyou', 'error'
    """
    url = api_base(cfg) + "/kiosk-screen"
    payload = {
        "mode": mode,
        "ts": int(time.time()),
    }
    payload.update(_auth_params(cfg))

    if order_id is not None:
        payload["order_id"] = int(order_id)

    if not payload.get("key"):
        print("set_screen_mode: missing key")
        return

    try:
        r = requests.post(url, json=payload, timeout=5)
        if r.status_code >= 400:
            print(f"set_screen_mode HTTP {r.status_code}: {r.text}")
    except Exception as e:
        print("set_screen_mode: error:", e)
