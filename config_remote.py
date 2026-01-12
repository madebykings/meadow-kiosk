import json
import os
import time
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


def safe_fallback_config(prov=None, imei=None, reason=None):
    """
    Minimum safe config so the kiosk can boot even if WP is down or returns 4xx/5xx.
    The app should treat this as "offline / out of service" and disable payment + vending.
    """
    domain = ""
    kiosk_token = ""
    if isinstance(prov, dict):
        domain = (prov.get("domain") or prov.get("provision_url") or "").strip()
        kiosk_token = (prov.get("kiosk_token") or prov.get("token") or "").strip()

    return {
        "mode": "safe",
        "reason": reason or "remote_config_unavailable",
        "domain": domain,
        "kiosk_token": kiosk_token,
        "imei": imei or "",
        "payment": {"enabled": False},
        "vend": {"enabled": False},
        "ads": {"enabled": True},  # let the UI still display something if you want
        "updated_at": int(time.time()),
    }


def fetch_config_from_wp(prov, imei=None, timeout=10):
    """
    Fetch per-kiosk config from WordPress.

    provision.json expected minimum:
      {
        "domain": "https://meadowvending.com",
        "kiosk_token": "UNIT-0001-SPARTAN",
        "api_key": "MASTER-PROVISION-KEY"
      }

    WP endpoint:
      GET {domain}/wp-json/meadow/v1/kiosk-config?token=...&key=...&imei=...

    Returns dict used by kiosk.py.
    """
    domain = (prov.get("domain") or prov.get("provision_url") or "").strip()
    kiosk_token = (prov.get("kiosk_token") or prov.get("token") or "").strip()
    master_key = (prov.get("api_key") or prov.get("key") or "").strip()

    if not domain or not kiosk_token or not master_key:
        # Don't include secrets in logs/errors
        raise RuntimeError(
            f"provision.json missing domain/kiosk_token/api_key. Loaded keys: {list(prov.keys())}"
        )

    url = domain.rstrip("/") + "/wp-json/meadow/v1/kiosk-config"

    params = {"token": kiosk_token, "key": master_key}
    if imei:
        params["imei"] = imei

    try:
        r = requests.get(url, params=params, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"kiosk-config request failed: {e}")

    if r.status_code != 200:
        # Keep body short; avoid leaking any sensitive data
        body_preview = (r.text or "")[:500]
        raise RuntimeError(
            f"kiosk-config failed {r.status_code} at {url}: {body_preview}"
        )

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
    - Try fetch from WP (and cache)
    - If that fails, fall back to cached config
    - If no cached config exists (first boot / cleared cache), return safe fallback config
      so kiosk can boot instead of crashing the service.
    """
    prov = load_provision()

    try:
        return fetch_config_from_wp(prov, imei=imei)
    except Exception as e:
        cached = load_cached_config()
        if cached:
            print("get_config: using cached config due to error:", e, flush=True)
            # Optional: stamp that we are in degraded mode
            cached.setdefault("degraded", True)
            cached.setdefault("degraded_reason", str(e)[:300])
            return cached

        # No cache available: return a safe config (do NOT crash)
        print("get_config: no cache available; using safe fallback due to error:", e, flush=True)
        return safe_fallback_config(prov=prov, imei=imei, reason=str(e)[:300])
