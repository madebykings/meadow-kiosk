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


def fetch_config_from_wp(prov, imei=None, timeout=10):
    """
    Fetch per-kiosk config from WordPress.

    provision.json (boot) expected minimum:
      {
        "domain": "https://meadowvending.com",
        "kiosk_token": "UNIT-0001-SPARTAN",
        "api_key": "MASTER-PROVISION-KEY"
      }

    WordPress endpoint (per your plugin):
      GET {domain}/wp-json/meadow/v1/kiosk-config?token=...&key=...&imei=...

    Returns dict used by kiosk.py.
    """
    domain = (prov.get("domain") or prov.get("provision_url") or "").strip()
    kiosk_token = (prov.get("kiosk_token") or prov.get("token") or "").strip()
    master_key = (prov.get("api_key") or prov.get("key") or "").strip()

    if not domain or not kiosk_token or not master_key:
        raise RuntimeError(
            f"provision.json missing domain/kiosk_token/api_key. Loaded keys: {list(prov.keys())}"
        )

    url = domain.rstrip("/") + "/wp-json/meadow/v1/kiosk-config"

    # IMPORTANT: match WP plugin param names
    params = {
        "token": kiosk_token,
        "key": master_key,
    }
    if imei:
        params["imei"] = imei

    r = requests.get(url, params=params, timeout=timeout)

    # Better error output than raise_for_status()
    if r.status_code != 200:
        raise RuntimeError(f"kiosk-config failed {r.status_code}: {r.text[:500]}")

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
