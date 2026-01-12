import json
import os
import requests

PROVISION_PATH = "/boot/provision.json"
CACHE_PATH = "/home/meadow/kiosk.config.cache.json"


def load_provision():
    with open(PROVISION_PATH, "r") as f:
        return json.load(f)


def load_cached_config():
    if not os.path.exists(CACHE_PATH):
        return None
    try:
        with open(CACHE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return None


def save_cached_config(cfg):
    try:
        with open(CACHE_PATH, "w") as f:
            json.dump(cfg, f)
    except Exception:
        pass


def fetch_config_from_wp(prov, imei=None, timeout=8):
    """
    Fetch per-kiosk config from WordPress.

    Expected provision.json shape (minimum):
      {
        "domain": "https://yourdomain.com",
        "kiosk_token": "SOME_SECRET_TOKEN"
      }

    WP endpoint expected:
      GET {domain}/wp-json/meadow/v1/kiosk-config?kiosk_token=...&imei=...

    Returns a dict config used by kiosk.py, including:
      kiosk_id, domain, api_key, motors, spin_time, kiosk_page, ads_page, thankyou_timeout, mode,
      OPTIONAL: sigma_terminal_id (per kiosk)
    """
    domain = prov.get("domain")
    kiosk_token = prov.get("kiosk_token") or prov.get("token")

    if not domain or not kiosk_token:
        raise RuntimeError("provision.json missing domain or kiosk_token")

    url = domain.rstrip("/") + "/wp-json/meadow/v1/kiosk-config"
    params = {"kiosk_token": kiosk_token}
    if imei:
        params["imei"] = imei

    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    cfg = r.json()

    # Ensure domain included in cfg (Pi relies on it)
    if "domain" not in cfg:
        cfg["domain"] = domain

    # Cache for offline boot
    save_cached_config(cfg)
    return cfg


def get_config(imei=None):
    """
    Main entry:
    - Read provision.json
    - Try to fetch config from WP
    - If that fails, fall back to cached config
    """
    prov = load_provision()
    try:
        return fetch_config_from_wp(prov, imei=imei)
    except Exception as e:
        cached = load_cached_config()
        if cached:
            print("get_config: using cached config due to error:", e, flush=True)
            return cached
        raise
