#!/usr/bin/env python3
"""
Meadow Pi API (via Cloudflare Tunnel)

FAST PATH (no WP dependency):
  POST /sigma/purchase  { amount_minor:int, currency_num:str|int, reference:str }
  POST /vend            { motor:int }

OBSERVABILITY:
  GET  /health
  GET  /debug/config
  GET  /heartbeat   (or POST /heartbeat)  -> updates UI heartbeat file

ADMIN ONE-SHOT CONTROL (AUTH REQUIRED):
  POST /admin/enter-kiosk     { kiosk_id:int, key:str }
  POST /admin/exit-kiosk      { kiosk_id:int, key:str }
  POST /admin/reload-kiosk    { kiosk_id:int, key:str }
  POST /admin/set-url         { kiosk_id:int, key:str, url:str }
  POST /admin/reboot          { kiosk_id:int, key:str }
  POST /admin/shutdown        { kiosk_id:int, key:str }
  POST /admin/update-code     { kiosk_id:int, key:str, branch?:str }

Notes:
  - Binds to 127.0.0.1 only. Cloudflare Tunnel publishes externally.
  - FAST PATH endpoints remain unauthenticated (per your earlier request).
  - ADMIN endpoints require the same "api_key" you already store in cached config
    (MASTER-PROVISION-KEY1) and kiosk_id must match.
"""

from __future__ import annotations

import json
import os
import time
import threading
import traceback
import requests
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional, Tuple

from config_remote import load_provision, fetch_config_from_wp
from modem import get_imei
from motors import MotorController
from payment.sigma.sigma_ipp_client import SigmaIppClient


HOST = "127.0.0.1"
PORT = 8765

CACHE_PATH = "/home/meadow/kiosk.config.cache.json"

# Updated by the kiosk UI (Chromium) to prove the page/JS is alive
UI_HEARTBEAT_FILE = os.environ.get("MEADOW_UI_HEARTBEAT_FILE", "/tmp/meadow_ui_heartbeat")
WP_HEARTBEAT_FILE = os.environ.get("MEADOW_WP_HEARTBEAT_FILE", "/tmp/meadow_wp_heartbeat")

# Kiosk control
KIOSK_URL_FILE = "/home/meadow/kiosk.url"
STOP_FLAG = "/tmp/meadow_kiosk_stop"
UPDATE_SCRIPT = "/home/meadow/update-meadow.sh"

KIOSK_BROWSER_UNIT = "meadow-kiosk-browser.service"
LAUNCHER_UNIT = "meadow-launcher.service"


def _mask(s: str) -> str:
    s = (s or "").strip()
    if len(s) <= 6:
        return "***"
    return s[:3] + "***" + s[-2:]


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        handler.send_header("Access-Control-Allow-Headers", "Content-Type")
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        return


def _read_json(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _load_cached_config_file() -> Dict[str, Any]:
    try:
        if os.path.exists(CACHE_PATH):
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _systemctl(*args: str) -> int:
    # Uses sudo; your install.sh already set sudoers drop-in for meadow user.
    return subprocess.call(["sudo", "systemctl", *args])


def _touch(path: str) -> None:
    try:
        with open(path, "a", encoding="utf-8"):
            os.utime(path, None)
    except Exception:
        pass


class RuntimeState:
    """Holds last WP config + live controllers + poll status."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cfg: Dict[str, Any] = {}
        self._motors: Optional[MotorController] = None
        self._sigma_path: str = "/dev/sigma"
        self._sigma_baud: int = 115200

        self._last_config_ok: bool = False
        self._last_config_error: str = ""
        self._last_config_ts: int = 0

        # Heartbeat cache (WP and UI)
        self._last_heartbeat_ok: bool = False
        self._last_heartbeat_error: str = ""
        self._last_heartbeat_ts: int = 0
        self._cached_imei: str = ""

        self._derived_motor_map: Dict[int, int] = {}
        self._derived_spin_map: Dict[int, float] = {}

    def get_cfg_copy(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._cfg)

    def mark_poll_result(self, ok: bool, err: str = "") -> None:
        with self._lock:
            self._last_config_ok = bool(ok)
            self._last_config_error = (err or "")[:2000]
            self._last_config_ts = int(time.time())

    def mark_heartbeat_result(self, ok: bool, err: str = "") -> None:
        with self._lock:
            self._last_heartbeat_ok = bool(ok)
            self._last_heartbeat_error = (err or "")[:300]
            self._last_heartbeat_ts = int(time.time())

    def get_cached_imei(self) -> str:
        with self._lock:
            return self._cached_imei

    def set_cached_imei(self, imei: str) -> None:
        with self._lock:
            self._cached_imei = (imei or "")[:40]

    def update_from_wp(self, cfg: Dict[str, Any]) -> None:
        with self._lock:
            self._cfg = cfg or {}

            motor_map = (cfg.get("motors") or {})
            spin_map = (cfg.get("spin_time") or {})

            mm: Dict[int, int] = {}
            sm: Dict[int, float] = {}

            for k, v in dict(motor_map).items():
                try:
                    mm[int(k)] = int(v)
                except Exception:
                    continue

            for k, v in dict(spin_map).items():
                try:
                    sm[int(k)] = float(v)
                except Exception:
                    continue

            self._derived_motor_map = dict(mm)
            self._derived_spin_map = dict(sm)

            self._motors = MotorController(mm, sm) if mm else None

            payment = (cfg.get("payment") or {})
            sigma = ((payment.get("sigma") or {}) if isinstance(payment, dict) else {})

            usb_path = str(sigma.get("usb_path") or "").strip()
            self._sigma_path = usb_path if usb_path else "/dev/sigma"

            try:
                self._sigma_baud = int(sigma.get("baud") or 115200)
            except Exception:
                self._sigma_baud = 115200

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "last_config_ok": self._last_config_ok,
                "last_config_error": self._last_config_error,
                "last_config_ts": self._last_config_ts,
                "cfg": self._cfg,
                "derived": {
                    "motors": self._derived_motor_map,
                    "spin_time": self._derived_spin_map,
                },
                "heartbeat": {
                    "ok": self._last_heartbeat_ok,
                    "error": self._last_heartbeat_error,
                    "ts": self._last_heartbeat_ts,
                },
                "cached_imei": self._cached_imei,
                "sigma_path": self._sigma_path,
                "sigma_baud": self._sigma_baud,
                "motors_loaded": self._motors is not None,
            }

    def get_sigma(self) -> Tuple[str, int]:
        with self._lock:
            return self._sigma_path, self._sigma_baud

    def get_motors(self) -> Optional[MotorController]:
        with self._lock:
            return self._motors


STATE = RuntimeState()


def _git_short_hash() -> str:
    """Return short git hash for current checkout, or empty string."""
    try:
        cwd = os.path.dirname(os.path.abspath(__file__))
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd,
            stderr=subprocess.DEVNULL
        )
        return out.decode().strip()
    except Exception:
        return ""


def _post_heartbeat(cfg: Dict[str, Any]) -> None:
    """POST heartbeat to WP if config contains kiosk_id + api_key + domain."""
    try:
        domain = (cfg.get("domain") or "").strip()
        kiosk_id = int(cfg.get("kiosk_id") or 0)
        key = (cfg.get("api_key") or "").strip()
        if not domain or not kiosk_id or not key:
            return

        url = domain.rstrip("/") + "/wp-json/meadow/v1/kiosk-heartbeat"

        imei = STATE.get_cached_imei()
        if not imei:
            imei = get_imei() or ""
            if imei:
                STATE.set_cached_imei(imei)

        payload: Dict[str, Any] = {
            "kiosk_id": kiosk_id,
            "key": key,
            "pi_git": _git_short_hash(),
            "ts": int(time.time()),
        }
        if imei:
            payload["imei"] = imei

        r = requests.post(url, json=payload, timeout=6)
        if r.status_code != 200:
            STATE.mark_heartbeat_result(False, f"HTTP {r.status_code}")
        else:
            STATE.mark_heartbeat_result(True, "")
            _touch(WP_HEARTBEAT_FILE)
    except Exception as e:
        STATE.mark_heartbeat_result(False, str(e)[:200])


def _heartbeat_loop() -> None:
    while True:
        cfg = STATE.get_cfg_copy()
        _post_heartbeat(cfg)
        time.sleep(60)


def _config_poll_loop() -> None:
    try:
        prov = load_provision()
        print("[pi_api] loaded provision:", prov)
    except Exception as e:
        print("[pi_api] FAILED to load provision.json")
        print("".join(traceback.format_exception(type(e), e, e.__traceback__)))
        return

    while True:
        try:
            cfg = fetch_config_from_wp(prov, imei=None, timeout=8)
            if cfg and isinstance(cfg, dict):
                STATE.update_from_wp(cfg)
                STATE.mark_poll_result(True, "")
            else:
                STATE.mark_poll_result(False, "empty_config")
        except Exception as e:
            err = "".join(traceback.format_exception(type(e), e, e.__traceback__))[-2000:]
            STATE.mark_poll_result(False, err)
        time.sleep(30)


# -----------------------------
# ADMIN AUTH + ACTIONS
# -----------------------------

def _expected_admin_kiosk_id() -> int:
    # Prefer live state config, fall back to cached file
    cfg = STATE.get_cfg_copy()
    try:
        kid = int(cfg.get("kiosk_id") or 0)
        if kid:
            return kid
    except Exception:
        pass
    try:
        kid = int(_load_cached_config_file().get("kiosk_id") or 0)
        return kid
    except Exception:
        return 0


def _expected_admin_key() -> str:
    # Prefer live state config, fall back to cached file
    cfg = STATE.get_cfg_copy()
    k = (cfg.get("api_key") or cfg.get("key") or "").strip()
    if k:
        return k
    k = (_load_cached_config_file().get("api_key") or "").strip()
    return k


def _require_admin_auth(data: Dict[str, Any]) -> Tuple[bool, str]:
    try:
        req_kiosk_id = int(data.get("kiosk_id") or 0)
    except Exception:
        req_kiosk_id = 0

    req_key = str(data.get("key") or "").strip()

    exp_kiosk_id = _expected_admin_kiosk_id()
    exp_key = _expected_admin_key()

    if not exp_kiosk_id or not exp_key:
        return False, "server_not_ready"

    if req_kiosk_id != exp_kiosk_id:
        return False, "bad_kiosk_id"

    if req_key != exp_key:
        return False, "bad_key"

    return True, ""


def _enter_kiosk() -> None:
    # remove stop flag so watchdog loop continues
    try:
        if os.path.exists(STOP_FLAG):
            os.remove(STOP_FLAG)
    except Exception:
        pass
    _systemctl("stop", LAUNCHER_UNIT)
    _systemctl("start", KIOSK_BROWSER_UNIT)


def _exit_kiosk() -> None:
    try:
        with open(STOP_FLAG, "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
    except Exception:
        pass
    _systemctl("stop", KIOSK_BROWSER_UNIT)
    _systemctl("start", LAUNCHER_UNIT)


def _reload_kiosk() -> None:
    _systemctl("restart", KIOSK_BROWSER_UNIT)


def _set_url_and_reload(url: str) -> None:
    url = (url or "").strip()
    if not url:
        raise ValueError("empty_url")
    with open(KIOSK_URL_FILE, "w", encoding="utf-8") as f:
        f.write(url + "\n")
    _reload_kiosk()


def _update_code(branch: str) -> None:
    branch = (branch or "main").strip()[:64]
    subprocess.Popen(["bash", UPDATE_SCRIPT, branch])


# -----------------------------
# HTTP HANDLER
# -----------------------------

class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path.startswith("/health"):
            snap = STATE.snapshot()
            return _json_response(self, 200, {
                "ok": True,
                "sigma_path": snap["sigma_path"],
                "sigma_baud": snap["sigma_baud"],
                "motors_loaded": snap["motors_loaded"],
                "last_config_ok": snap["last_config_ok"],
                "last_config_ts": snap["last_config_ts"],
                "last_config_error": snap["last_config_error"],
            })

        if self.path.startswith("/debug/config"):
            return _json_response(self, 200, STATE.snapshot())

        if self.path.startswith("/heartbeat"):
            _touch(UI_HEARTBEAT_FILE)
            return _json_response(self, 200, {"ok": True})

        return _json_response(self, 404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        if self.path.startswith("/heartbeat"):
            _touch(UI_HEARTBEAT_FILE)
            return _json_response(self, 200, {"ok": True})

        if self.path.startswith("/sigma/purchase"):
            return self._handle_sigma_purchase()

        if self.path.startswith("/vend"):
            return self._handle_vend()

        # ---- ADMIN (auth required) ----
        if self.path.startswith("/admin/"):
            return self._handle_admin()

        return _json_response(self, 404, {"ok": False, "error": "not_found"})

    def _handle_admin(self) -> None:
        data = _read_json(self)
        ok, err = _require_admin_auth(data)
        if not ok:
            return _json_response(self, 403, {"ok": False, "error": err})

        try:
            if self.path.startswith("/admin/enter-kiosk"):
                _enter_kiosk()
                return _json_response(self, 200, {"ok": True})

            if self.path.startswith("/admin/exit-kiosk"):
                _exit_kiosk()
                return _json_response(self, 200, {"ok": True})

            if self.path.startswith("/admin/reload-kiosk"):
                _reload_kiosk()
                return _json_response(self, 200, {"ok": True})

            if self.path.startswith("/admin/set-url"):
                url = str(data.get("url") or "")
                _set_url_and_reload(url)
                return _json_response(self, 200, {"ok": True})

            if self.path.startswith("/admin/reboot"):
                subprocess.Popen(["sudo", "reboot"])
                return _json_response(self, 200, {"ok": True})

            if self.path.startswith("/admin/shutdown"):
                subprocess.Popen(["sudo", "shutdown", "-h", "now"])
                return _json_response(self, 200, {"ok": True})

            if self.path.startswith("/admin/update-code"):
                branch = str(data.get("branch") or "main")
                _update_code(branch)
                return _json_response(self, 200, {"ok": True, "branch": branch})

            return _json_response(self, 404, {"ok": False, "error": "not_found"})
        except Exception as e:
            return _json_response(self, 500, {"ok": False, "error": str(e)})

    def _handle_sigma_purchase(self) -> None:
        data = _read_json(self)
        amount_minor = data.get("amount_minor")
        currency_num = str(data.get("currency_num") or "826")
        reference = str(data.get("reference") or "")[:64]

        try:
            amount_minor_int = int(amount_minor)
            if amount_minor_int <= 0:
                raise ValueError("amount_minor must be > 0")
        except Exception:
            return _json_response(self, 400, {"ok": False, "error": "bad_amount"})

        sigma_path, sigma_baud = STATE.get_sigma()
        port_candidates = [sigma_path, "/dev/sigma", "/dev/ttyACM0", "/dev/ttyUSB0"]

        last_err = ""
        for port in port_candidates:
            if not port or not os.path.exists(port):
                continue

            try:
                with SigmaIppClient(port=port, baudrate=sigma_baud) as sigma:
                    r = sigma.purchase(
                        amount_minor=amount_minor_int,
                        currency_num=currency_num,
                        reference=reference,
                        first_wait=25.0,
                        final_wait=180.0,
                    )

                status = str(r.get("status") or "")
                stage = str(r.get("stage") or "")
                approved = bool(r.get("approved"))

                raw = r.get("raw") or {}
                if not isinstance(raw, dict):
                    raw = {}

                payload = {
                    "approved": approved,
                    "status": status,
                    "stage": stage,
                    "raw": r.get("raw") or r,
                    "receipt": raw.get("RECEIPT", ""),
                    "txid": str(raw.get("TXID") or raw.get("RRN") or ""),
                    "port": port,
                }

                if status and status != "0" and not approved:
                    return _json_response(self, 409, {"ok": False, "error": "sigma_rejected", **payload})

                return _json_response(self, 200, {"ok": True, **payload})

            except Exception as e:
                last_err = "".join(traceback.format_exception(type(e), e, e.__traceback__))[-2000:]
                continue

        return _json_response(self, 502, {"ok": False, "error": "sigma_failed", "detail": last_err})

    def _handle_vend(self) -> None:
        data = _read_json(self)
        try:
            motor = int(data.get("motor"))
        except Exception:
            return _json_response(self, 400, {"ok": False, "success": False, "error": "bad_motor"})

        controller = STATE.get_motors()
        if controller is None:
            return _json_response(self, 503, {"ok": False, "success": False, "error": "motors_not_loaded"})

        try:
            controller.vend(motor)
            return _json_response(self, 200, {"ok": True, "success": True})
        except Exception as e:
            return _json_response(self, 500, {"ok": False, "success": False, "error": str(e)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> None:
    threading.Thread(target=_config_poll_loop, daemon=True).start()
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    httpd = HTTPServer((HOST, PORT), Handler)
    print(f"[pi_api] listening on http://{HOST}:{PORT}")
    print(f"[pi_api] admin expects kiosk_id={_expected_admin_kiosk_id()} key={_mask(_expected_admin_key())}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
