import requests
import time


def api_base(cfg):
    return cfg["domain"].rstrip("/") + "/wp-json/meadow/v1"


def next_command(cfg):
    """
    Ask WP if there is a vend command for this kiosk.
    Expected response: {"id": 123, "motor": 2} or {} / None.
    """
    url = api_base(cfg) + "/next-command"
    params = {"kiosk_id": cfg["kiosk_id"], "key": cfg["api_key"]}
    try:
        r = requests.get(url, params=params, timeout=5)
    except Exception:
        return None

    if r.status_code != 200:
        return None

    data = r.json()
    if data.get("motor"):
        return data
    return None


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
    except Exception:
        pass


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
    except Exception:
        pass


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
    except Exception:
        pass

