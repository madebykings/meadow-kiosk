#!/usr/bin/env python3
"""Meadow Pi API (via Cloudflare Tunnel)

Endpoints (JSON):
  POST /sigma/purchase            { amount_minor:int, currency_num:str|int, reference:str }
  POST /vend                      { motor:int }
  POST /admin/vend-test           { kiosk_id:int, key:str, motor:int }
  POST /admin/consume-wp-command  { kiosk_id:int, key:str, scope?:'vend'|'control' }  # one-shot (no polling)

  GET  /health
  GET  /debug/config              (shows last WP config + derived maps)
  GET/POST /heartbeat             (UI watchdog touch)

Notes:
  - Binds to 127.0.0.1 only. Cloudflare Tunnel publishes it externally.
  - Admin endpoints REQUIRE kiosk_id + key (cfg.api_key).
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

# Updated by the kiosk UI (Chromium) to prove the page/JS is alive
UI_HEARTBEAT_FILE = os.environ.get("MEADOW_UI_HEARTBEAT_FILE", "/tmp/meadow_ui_heartbeat")
WP_HEARTBEAT_FILE = os.environ.get("MEADOW_WP_HEARTBEAT_FILE", "/tmp/meadow_wp_heartbeat")


# -------------------------------------------------------------------
# HTTP helpers
# -------------------------------------------------------------------

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


def _touch(path: str) -> None:
    try:
        with open(path, "a"):
            os.utime(path, None)
    except Exception:
        pass


# -------------------------------------------------------------------
# Runtime state
# -------------------------------------------------------------------

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

    def get_heartbeat_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "ok": self._last_heartbeat_ok,
                "error": self._last_heartbeat_error,
                "ts": self._last_heartbeat_ts,
            }

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

    def get_auth(self) -> Tuple[int, str, str]:
        """
        Returns (kiosk_id, api_key, domain) from last WP config.
        """
        with self._lock:
            kiosk_id = int(self._cfg.get("kiosk_id") or 0)
            key = (self._cfg.get("api_key") or self._cfg.get("key") or "").strip()
            domain = (self._cfg.get("domain") or "").strip()
        return kiosk_id, key, domain


def _git_short_hash() -> str:
    """Return short git hash for current checkout, or empty string."""
    try:
        cwd = os.path.dirname(os.path.abspath(__file__))
        out = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=cwd, stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except Exception:
        return ""


STATE = RuntimeState()


# -------------------------------------------------------------------
# Auth for admin endpoints
# -------------------------------------------------------------------

def _auth_admin(data: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Admin endpoints require kiosk_id + key to match config cache.
    """
    want_kiosk_id, want_key, _domain = STATE.get_auth()
    got_kiosk_id = int(data.get("kiosk_id") or 0)
    got_key = (str(data.get("key") or "")).strip()

    if not want_kiosk_id or not want_key:
        return False, "pi_not_ready_no_auth"  # config not loaded yet
    if got_kiosk_id != want_kiosk_id:
        return False, "bad_kiosk_id"
    if got_key != want_key:
        return False, "bad_key"
    return True, ""


# -------------------------------------------------------------------
# WP heartbeat + config polling
# -------------------------------------------------------------------

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

        payload = {
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
            print("[pi_api] polling WP for kiosk config…")
            cfg = fetch_config_from_wp(prov, imei=None, timeout=8)

            if not cfg:
                print("[pi_api] poll returned empty config")
                STATE.mark_poll_result(False, "empty_config")
            else:
                print("[pi_api] config received OK")
                STATE.update_from_wp(cfg)
                STATE.mark_poll_result(True, "")

        except Exception as e:
            err = "".join(traceback.format_exception(type(e), e, e.__traceback__))[-2000:]
            print("[pi_api] CONFIG POLL FAILED:")
            print(err)
            STATE.mark_poll_result(False, err)

        time.sleep(30)


# -------------------------------------------------------------------
# One-shot WP command consume (NO polling)
# -------------------------------------------------------------------

def _wp_api_base(domain: str) -> str:
    return domain.rstrip("/") + "/wp-json/meadow/v1"


def _wp_next_command(domain: str, kiosk_id: int, key: str, scope: str) -> Optional[Dict[str, Any]]:
    """
    Calls WP /next-command with ?kiosk_id=&key=&scope=
    Returns dict or None.
    """
    url = _wp_api_base(domain) + "/next-command"
    params = {
        "kiosk_id": int(kiosk_id),
        "key": str(key),
        "scope": str(scope or "vend"),
        "_t": int(time.time()),
    }
    r = requests.get(url, params=params, timeout=10)
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    if isinstance(data, list):
        if not data:
            return None
        data = data[0]
    if not isinstance(data, dict) or not data.get("id"):
        return None
    return data


def _wp_ack_command(domain: str, kiosk_id: int, key: str, cmd_id: int) -> Tuple[bool, str]:
    """
    Calls WP /command-complete with JSON {id,kiosk_id,key}
    """
    url = _wp_api_base(domain) + "/command-complete"
    payload = {"id": int(cmd_id), "kiosk_id": int(kiosk_id), "key": str(key), "ts": int(time.time())}
    r = requests.post(url, json=payload, timeout=10)
    if r.status_code != 200:
        return False, (r.text or "")[:400]
    return True, ""


# -------------------------------------------------------------------
# HTTP handler
# -------------------------------------------------------------------

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

        if self.path.startswith("/admin/vend-test"):
            return self._handle_admin_vend_test()

        if self.path.startswith("/admin/consume-wp-command"):
            return self._handle_admin_consume_wp_command()

        return _json_response(self, 404, {"ok": False, "error": "not_found"})

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

    def _handle_admin_vend_test(self) -> None:
        """
        One-shot vend with auth. This replaces the polling vend-poller service.
        """
        data = _read_json(self)
        ok, err = _auth_admin(data)
        if not ok:
            return _json_response(self, 403, {"ok": False, "error": err})

        try:
            motor = int(data.get("motor") or 0)
        except Exception:
            motor = 0
        if motor <= 0:
            return _json_response(self, 400, {"ok": False, "error": "missing_motor"})

        controller = STATE.get_motors()
        if controller is None:
            return _json_response(self, 503, {"ok": False, "error": "motors_not_loaded"})

        try:
            controller.vend(motor)
            return _json_response(self, 200, {"ok": True, "motor": motor})
        except Exception as e:
            return _json_response(self, 500, {"ok": False, "motor": motor, "error": str(e)})

    def _handle_admin_consume_wp_command(self) -> None:
        """
        One-shot:
          - fetch ONE queued command from WP (/next-command?kiosk_id&key&scope=vend/control)
          - if vend: execute locally
          - ack back to WP (/command-complete)

        This gives you a "WP button works" path without any background polling daemon.
        """
        data = _read_json(self)
        ok, err = _auth_admin(data)
        if not ok:
            return _json_response(self, 403, {"ok": False, "error": err})

        kiosk_id, key, domain = STATE.get_auth()
        if not domain:
            return _json_response(self, 503, {"ok": False, "error": "no_domain"})

        scope = str(data.get("scope") or "vend").strip().lower()
        if scope not in ("vend", "control"):
            scope = "vend"

        cmd = _wp_next_command(domain, kiosk_id, key, scope)
        if not cmd:
            return _json_response(self, 200, {"ok": True, "found": False})

        cmd_id = int(cmd.get("id") or 0)
        action = str(cmd.get("action") or "")
        payload = cmd.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}

        # Execute known actions (vend is most important)
        exec_ok = False
        exec_err = ""

        try:
            if action in ("vend", "spin_motor"):
                motor = int(payload.get("motor") or cmd.get("motor") or 0)
                if motor <= 0:
                    raise ValueError("missing motor")
                controller = STATE.get_motors()
                if controller is None:
                    raise RuntimeError("motors_not_loaded")
                controller.vend(motor)
                exec_ok = True

            else:
                # For now we don't execute control actions here (remote_control.py already does)
                exec_ok = True
        except Exception as e:
            exec_ok = False
            exec_err = str(e)

        ack_ok, ack_err = _wp_ack_command(domain, kiosk_id, key, cmd_id)

        return _json_response(self, 200, {
            "ok": True,
            "found": True,
            "cmd": {"id": cmd_id, "action": action, "scope": scope},
            "exec_ok": exec_ok,
            "exec_err": exec_err,
            "ack_ok": ack_ok,
            "ack_err": ack_err,
        })

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> None:
    threading.Thread(target=_config_poll_loop, daemon=True).start()
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    httpd = HTTPServer((HOST, PORT), Handler)
    print(f"[pi_api] listening on http://{HOST}:{PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
