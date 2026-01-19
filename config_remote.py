import json
import os
import time
import requests

PROVISION_PATH = "/boot/provision.json"
CACHE_PATH = "/home/meadow/meadow-kiosk/kiosk.config.cache.json"


# ------------------------------------------------------------
# Provision + cache helpers
# ------------------------------------------------------------

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


# ------------------------------------------------------------
# Safe fallback + normalisation
# ------------------------------------------------------------

def safe_fallback_config(prov=None, imei=None, reason=None):
    """
    Minimal config that guarantees kiosk.py can boot.
    Used when WP + cache are unavailable or invalid.
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

        "motors": {},
        "spin_time": {},
        "motor_pulse_ms": 350,

        "vend": {"enabled": False},
        "payment": {"enabled": False},
        "ads": {"enabled": True},

        "updated_at": int(time.time()),
    }


def normalize_config(cfg, prov=None, imei=None):
    """
    Ensure kiosk.py never crashes due to missing keys.
    """
    if not isinstance(cfg, dict):
        cfg = {}

    cfg.setdefault("motors", {})
    cfg.setdefault("spin_time", {})
    cfg.setdefault("motor_pulse_ms", 350)

    if prov and isinstance(prov, dict):
        cfg.setdefault(
            "domain",
            (prov.get("domain") or prov.get("provision_url") or "").strip()
        )
        cfg.setdefault(
            "kiosk_token",
            (prov.get("kiosk_token") or prov.get("token") or "").strip()
        )

    if imei:
        cfg.setdefault("imei", imei)

    cfg.setdefault("vend", {})
    cfg.setdefault("payment", {})
    cfg.setdefault("ads", {})

    if not cfg.get("motors") or not cfg.get("spin_time"):
        cfg.setdefault("mode", "safe")
        cfg["vend"]["enabled"] = False
        cfg["payment"]["enabled"] = False
        cfg["ads"].setdefault("enabled", True)

    cfg["vend"].setdefault("enabled", True)
    cfg["payment"].setdefault("enabled", True)
    cfg["ads"].setdefault("enabled", True)

    cfg.setdefault("updated_at", int(time.time()))

    return cfg


# ------------------------------------------------------------
# Remote fetch
# ------------------------------------------------------------

def fetch_config_from_wp(prov, imei=None, timeout=10):
    """
    Fetch per-kiosk config from WordPress.

    provision.json MUST contain:
      {
        "domain": "https://meadowvending.com",
        "kiosk_token": "UNIT-0001-SPARTAN",
        "provision_key": "MASTER-PROVISION-KEY"
      }

    WP endpoint:
      GET /wp-json/meadow/v1/kiosk-config?token=...&key=...&imei=...
    """
    domain = (prov.get("domain") or prov.get("provision_url") or "").strip()
    kiosk_token = (prov.get("kiosk_token") or prov.get("token") or "").strip()
    provision_key = (prov.get("provision_key") or "").strip()

    if not domain or not kiosk_token or not provision_key:
        raise RuntimeError(
            "provision.json missing domain/kiosk_token/provision_key. "
            f"Keys present: {list(prov.keys())}"
        )

    url = domain.rstrip("/") + "/wp-json/meadow/v1/kiosk-config"

    # WP expects 'key' query param today; keep that stable.
    params = {"token": kiosk_token, "key": provision_key}
    if imei:
        params["imei"] = imei

    try:
        r = requests.get(url, params=params, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"kiosk-config request failed: {e}")

    if r.status_code != 200:
        body_preview = (r.text or "")[:500]
        raise RuntimeError(f"kiosk-config failed {r.status_code} at {url}: {body_preview}")

    cfg = r.json()
    cfg.setdefault("domain", domain)

    cfg = normalize_config(cfg, prov=prov, imei=imei)
    save_cached_config(cfg)
    return cfg


# ------------------------------------------------------------
# Public entry point
# ------------------------------------------------------------

def get_config(imei=None):
    """
    Main entry point used by kiosk.py

    Order:
      1) Remote WP config
      2) Cached config
      3) Safe fallback config

    MUST NEVER raise (kiosk must boot).
    """
    prov = load_provision()

    try:
        return fetch_config_from_wp(prov, imei=imei)
    except Exception as e:
        cached = load_cached_config()
        if cached:
            print("get_config: using cached config due to error:", e, flush=True)
            cached.setdefault("degraded", True)
            cached.setdefault("degraded_reason", str(e)[:300])
            return normalize_config(cached, prov=prov, imei=imei)

        print("get_config: no cache available; using safe fallback:", e, flush=True)
        return normalize_config(
            safe_fallback_config(prov=prov, imei=imei, reason=str(e)[:300]),
            prov=prov,
            imei=imei,
        )
