import json
import os
import requests

PROVISION_PATH = "/boot/provision.json"
CACHE_PATH = "/boot/kiosk.config.cache.json"


def load_provision():
    with open(PROVISION_PATH) as f:
        return json.load(f)


def load_cached_config():
    if not os.path.exists(CACHE_PATH):
        return None
    with open(CACHE_PATH) as f:
        return json.load(f)


def save_cached_config(cfg):
    with open(CACHE_PATH, "w") as f:
        json.dump(cfg, f)


def fetch_config_from_wp(prov, imei=None):
    """
    Call WordPress /kiosk-config with kiosk_token + master api_key (+ optional IMEI).
    """
    params = {"token": prov["kiosk_token"], "key": prov["api_key"]}
    if imei:
        params["imei"] = imei

    url = prov["provision_url"].rstrip("/") + "/wp-json/meadow/v1/kiosk-config"
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    cfg = r.json()
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
    except Exception:
        cached = load_cached_config()
        if cached:
            return cached
        raise

