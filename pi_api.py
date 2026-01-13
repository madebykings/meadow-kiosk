#!/usr/bin/env python3
"""Meadow Pi Local API

Endpoints (JSON):
  POST /sigma/purchase  { amount_minor:int, currency_num:str|int, reference:str }
  POST /vend            { motor:int }
  GET  /health

Security:
  - Intended to be exposed via Cloudflare Tunnel.
  - Require a secret header for ALL POST endpoints to prevent public abuse.
"""

from __future__ import annotations

import json
import os
import time
import threading
import traceback
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional, Tuple

from config_remote import load_provision, fetch_config_from_wp
from motors import MotorController
from payment.sigma.sigma_ipp_client import SigmaIppClient


HOST = "127.0.0.1"
PORT = 8765

# Cloudflare Tunnel injects this header on requests that reach the origin.
# We require it for all POST endpoints (purchase/vend) to prevent public abuse.
TUNNEL_AUTH_HEADER = os.getenv("MEADOW_TUNNEL_HEADER", "X-Meadow-Tunnel")
TUNNEL_AUTH_SECRET = os.getenv("MEADOW_TUNNEL_SECRET", "")  # set in systemd env / env file


def _require_tunnel_auth(handler: BaseHTTPRequestHandler) -> bool:
    # Fail closed if not configured
    if not TUNNEL_AUTH_SECRET:
        return False
    got = (handler.headers.get(TUNNEL_AUTH_HEADER) or "").strip()
    return got == TUNNEL_AUTH_SECRET


# During rollout, you can set MEADOW_TUNNEL_FAIL_OPEN=1 to avoid blocking POSTs
FAIL_OPEN = (os.getenv("MEADOW_TUNNEL_FAIL_OPEN", "0").strip() == "1")


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    try:
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        # CORS (fine for now)
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


class RuntimeState:
    """Holds the latest WP config + live controllers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cfg: Dict[str, Any] = {}
        self._motors: Optional[MotorController] = None
        self._sigma_path: str = "/dev/sigma"
        self._sigma_baud: int = 115200

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

            self._motors = MotorController(mm, sm) if mm else None

            payment = (cfg.get("payment") or {})
            sigma = ((payment.get("sigma") or {}) if isinstance(payment, dict) else {})

            usb_path = str(sigma.get("usb_path") or "").strip()
            self._sigma_path = usb_path if usb_path else "/dev/sigma"

            try:
                self._sigma_baud = int(sigma.get("baud") or 115200)
            except Exception:
                self._sigma_baud = 115200

    def get_sigma(self) -> Tuple[str, int]:
        with self._lock:
            return self._sigma_path, self._sigma_baud

    def get_motors(self) -> Optional[MotorController]:
        with self._lock:
            return self._motors


STATE = RuntimeState()


def _config_poll_loop() -> None:
    prov = load_provision()
    while True:
        try:
            cfg = fetch_config_from_wp(prov, imei=None, timeout=8)
            if cfg:
                STATE.update_from_wp(cfg)
        except Exception:
            pass
        time.sleep(30)


def _require_tunnel_auth(handler: BaseHTTPRequestHandler) -> bool:
    # If no secret configured, either fail closed (default) or fail open if explicitly allowed.
    if not TUNNEL_AUTH_SECRET:
        if FAIL_OPEN:
            return True
        return False

    got = (handler.headers.get(TUNNEL_AUTH_HEADER) or "").strip()
    return hmac.compare_digest(got, TUNNEL_AUTH_SECRET)


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path.startswith("/health"):
            sigma_path, sigma_baud = STATE.get_sigma()
            motors_ok = STATE.get_motors() is not None
            return _json_response(self, 200, {
                "ok": True,
                "sigma_path": sigma_path,
                "sigma_baud": sigma_baud,
                "motors_loaded": motors_ok,
            })

        return _json_response(self, 404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        # Lock down POST endpoints to only traffic that came through Cloudflare Tunnel
        if not _require_tunnel_auth(self):
            # debug: show what we received (header name only + length)
            got = (self.headers.get(TUNNEL_AUTH_HEADER) or "")
            print(f"[pi_api] FORBIDDEN {self.path} header={TUNNEL_AUTH_HEADER} len={len(got)} secret_set={bool(TUNNEL_AUTH_SECRET)}")
            return _json_response(self, 403, {"ok": False, "error": "forbidden"})

        if self.path.startswith("/sigma/purchase"):
            return self._handle_sigma_purchase()

        if self.path.startswith("/vend"):
            return self._handle_vend()

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

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> None:
    t = threading.Thread(target=_config_poll_loop, daemon=True)
    t.start()

    httpd = HTTPServer((HOST, PORT), Handler)
    print(f"[pi_api] listening on http://{HOST}:{PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
