#!/usr/bin/env python3
"""Meadow Pi API (via Cloudflare Tunnel)

Fast local endpoints (no WP latency):
  POST /sigma/purchase            { amount_minor:int, currency_num:str|int, reference:str }
  POST /sigma/warm                { }  (best-effort warmup / ensure_idle; non-blocking if busy)
  POST /vend                      { motor:int }

Admin endpoints (require kiosk_id + key):
  POST /admin/vend-test           { kiosk_id:int, key:str, motor:int }
  POST /admin/control             { kiosk_id:int, key:str, action:str, payload?:object }

Health/debug:
  GET  /health
  GET  /debug/config
  GET/POST /heartbeat

Notes:
  - Binds to 127.0.0.1 only. Cloudflare Tunnel publishes it externally.
  - Admin endpoints REQUIRE kiosk_id + key (from cfg.api_key in WP config).
  - Kiosk mode control uses STOP_FLAG + direct launch of kiosk-browser.sh (no systemd required).
  - Sigma calls are guarded by a single lock so warmup can never overlap purchase.
  - Locking is BOTH in-process (threading) and cross-process (fcntl flock) to prevent overlap even if
    pi_api is accidentally started twice.
"""

from __future__ import annotations

import json
import os
import time
import threading
import traceback
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

import errno
import fcntl

import requests

from config_remote import load_provision, fetch_config_from_wp
from modem import get_imei
from motors import MotorController
from payment.sigma.sigma_ipp_client import SigmaIppClient


HOST = "127.0.0.1"
PORT = 8765

UI_HEARTBEAT_FILE = os.environ.get("MEADOW_UI_HEARTBEAT_FILE", "/tmp/meadow_ui_heartbeat")
WP_HEARTBEAT_FILE = os.environ.get("MEADOW_WP_HEARTBEAT_FILE", "/tmp/meadow_wp_heartbeat")

KIOSK_URL_FILE = os.environ.get("MEADOW_KIOSK_URL_FILE", "/home/meadow/kiosk.url")
STOP_FLAG = os.environ.get("MEADOW_KIOSK_STOP_FLAG", "/tmp/meadow_kiosk_stop")

# Direct kiosk control (no systemd)
KIOSK_SCRIPT = os.environ.get("MEADOW_KIOSK_SCRIPT", "/home/meadow/kiosk-browser.sh")
KIOSK_PIDFILE = os.environ.get("MEADOW_KIOSK_PIDFILE", "/tmp/meadow_kiosk_browser.pid")

UPDATE_SCRIPT = os.environ.get("MEADOW_UPDATE_SCRIPT", "/home/meadow/update-meadow.sh")

# Poll WP config every N seconds
CONFIG_POLL_SECS = int(os.environ.get("MEADOW_CONFIG_POLL_SECS", "30"))
HEARTBEAT_SECS = int(os.environ.get("MEADOW_HEARTBEAT_SECS", "60"))

# -------------------------------------------------------------------
# Sigma concurrency guard (warmup + purchase share one serial port)
# -------------------------------------------------------------------
# In-process lock (threads)
_SIGMA_THREAD_LOCK = threading.Lock()

# Cross-process lock (prevents overlap if pi_api is started twice)
SIGMA_LOCKFILE = os.environ.get("MEADOW_SIGMA_LOCKFILE", "/tmp/meadow_sigma.lock")

# Warmup should be fast + never block UI if Sigma is in-use
SIGMA_WARM_MAX_WAIT_SECS = 8.0     # bounded ensure_idle
SIGMA_BUSY_LOCK_TIMEOUT = 0.10     # if Sigma busy, warmup returns immediately

# Purchases can wait a bit longer to serialize safely
SIGMA_PURCHASE_LOCK_TIMEOUT = 10.0  # seconds to wait for lock before returning "busy"


class _SigmaGlobalLock:
    """
    Composite lock:
      1) threading.Lock (prevents overlap inside this process)
      2) fcntl.flock on SIGMA_LOCKFILE (prevents overlap across processes)
    """

    def __init__(self, lockfile: str) -> None:
        self.lockfile = lockfile
        self._fd: Optional[int] = None
        self._held_thread = False

    def acquire(self, timeout: float) -> bool:
        deadline = time.time() + max(0.0, float(timeout))

        # 1) Thread lock (block/poll until timeout)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            got = _SIGMA_THREAD_LOCK.acquire(timeout=min(0.25, remaining))
            if got:
                self._held_thread = True
                break

        # 2) File lock (non-busy loop until timeout)
        try:
            fd = os.open(self.lockfile, os.O_CREAT | os.O_RDWR, 0o666)
            self._fd = fd

            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    self.release()
                    return False
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    # Stamp debug info (best-effort)
                    try:
                        os.ftruncate(fd, 0)
                        os.lseek(fd, 0, os.SEEK_SET)
                        os.write(fd, f"pid={os.getpid()} ts={int(time.time())}\n".encode("utf-8"))
                    except Exception:
                        pass
                    return True
                except OSError as e:
                    if e.errno not in (errno.EACCES, errno.EAGAIN):
                        self.release()
                        return False
                    time.sleep(0.05)

        except Exception:
            self.release()
            return False

    def release(self) -> None:
        # Release file lock
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None

        # Release thread lock
        if self._held_thread:
            try:
                _SIGMA_THREAD_LOCK.release()
            except Exception:
                pass
            self._held_thread = False

    def __enter__(self) -> "_SigmaGlobalLock":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def _cors_origin(handler: BaseHTTPRequestHandler) -> str:
    # Strict (lock down) if you want:
    # return "https://meadowvending.com"
    # Flexible: echo Origin if present, else "*"
    return handler.headers.get("Origin") or "*"


def _send_cors(handler: BaseHTTPRequestHandler) -> None:
    origin = _cors_origin(handler)
    handler.send_header("Access-Control-Allow-Origin", origin)
    handler.send_header("Vary", "Origin")
    handler.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, X-Admin-Key, X-Meadow-Key")
    handler.send_header("Access-Control-Max-Age", "86400")


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        _send_cors(handler)
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


def _git_short_hash() -> str:
    try:
        cwd = os.path.dirname(os.path.abspath(__file__))
        out = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=cwd, stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except Exception:
        return ""


def _pid_is_running(pid: int) -> bool:
    return pid > 1 and os.path.exists(f"/proc/{pid}")


def _read_pidfile() -> int:
    try:
        with open(KIOSK_PIDFILE, "r", encoding="utf-8") as f:
            return int((f.read() or "").strip() or "0")
    except Exception:
        return 0


def _write_pidfile(pid: int) -> None:
    try:
        with open(KIOSK_PIDFILE, "w", encoding="utf-8") as f:
            f.write(str(int(pid)) + "\n")
    except Exception:
        pass


def _proc_running(pattern: str) -> bool:
    try:
        rc = subprocess.call(
            ["pgrep", "-fa", pattern],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return rc == 0
    except Exception:
        return False


def _kiosk_running() -> bool:
    if _proc_running(r"kiosk-browser\.sh"):
        return True
    if _proc_running(r"chromium.*--kiosk") or _proc_running(r"chromium-browser.*--kiosk"):
        return True
    return _pid_is_running(_read_pidfile())


# -------------------------------------------------------------------
# Runtime State
# -------------------------------------------------------------------

class RuntimeState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cfg: Dict[str, Any] = {}
        self._motors: Optional[MotorController] = None
        self._sigma_path: str = "/dev/sigma"
        self._sigma_baud: int = 115200

        self._last_config_ok: bool = False
        self._last_config_error: str = ""
        self._last_config_ts: int = 0

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
                "derived": {"motors": self._derived_motor_map, "spin_time": self._derived_spin_map},
                "heartbeat": {
                    "ok": self._last_heartbeat_ok,
                    "error": self._last_heartbeat_error,
                    "ts": self._last_heartbeat_ts,
                },
                "cached_imei": self._cached_imei,
                "sigma_path": self._sigma_path,
                "sigma_baud": self._sigma_baud,
                "sigma_lockfile": SIGMA_LOCKFILE,
                "motors_loaded": self._motors is not None,
                "kiosk": {
                    "script": KIOSK_SCRIPT,
                    "pidfile": KIOSK_PIDFILE,
                    "running": _kiosk_running(),
                    "stop_flag_exists": os.path.exists(STOP_FLAG),
                    "url_file": KIOSK_URL_FILE,
                },
            }

    def get_sigma(self) -> Tuple[str, int]:
        with self._lock:
            return self._sigma_path, self._sigma_baud

    def get_motors(self) -> Optional[MotorController]:
        with self._lock:
            return self._motors

    def get_auth(self) -> Tuple[int, str, str]:
        with self._lock:
            kiosk_id = int(self._cfg.get("kiosk_id") or 0)
            key = (self._cfg.get("api_key") or self._cfg.get("key") or "").strip()
            domain = (self._cfg.get("domain") or "").strip()
        return kiosk_id, key, domain


STATE = RuntimeState()


# -------------------------------------------------------------------
# Admin auth
# -------------------------------------------------------------------

def _auth_admin(data: Dict[str, Any]) -> Tuple[bool, str]:
    want_kiosk_id, want_key, _domain = STATE.get_auth()
    got_kiosk_id = int(data.get("kiosk_id") or 0)
    got_key = (str(data.get("key") or "")).strip()

    if not want_kiosk_id or not want_key:
        return False, "pi_not_ready_no_auth"
    if got_kiosk_id != want_kiosk_id:
        return False, "bad_kiosk_id"
    if got_key != want_key:
        return False, "bad_key"
    return True, ""


# -------------------------------------------------------------------
# WP config polling + heartbeat
# -------------------------------------------------------------------

def _post_heartbeat(cfg: Dict[str, Any]) -> None:
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

        payload = {"kiosk_id": kiosk_id, "key": key, "pi_git": _git_short_hash(), "ts": int(time.time())}
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
        _post_heartbeat(STATE.get_cfg_copy())
        time.sleep(max(10, HEARTBEAT_SECS))


def _config_poll_loop() -> None:
    try:
        prov = load_provision()
        print("[pi_api] loaded provision:", prov, flush=True)
    except Exception as e:
        print("[pi_api] FAILED to load provision.json", flush=True)
        print("".join(traceback.format_exception(type(e), e, e.__traceback__)), flush=True)
        return

    while True:
        try:
            cfg = fetch_config_from_wp(prov, imei=None, timeout=8)
            if not cfg:
                STATE.mark_poll_result(False, "empty_config")
            else:
                STATE.update_from_wp(cfg)
                STATE.mark_poll_result(True, "")
        except Exception as e:
            err = "".join(traceback.format_exception(type(e), e, e.__traceback__))[-2000:]
            STATE.mark_poll_result(False, err)
        time.sleep(max(10, CONFIG_POLL_SECS))


# -------------------------------------------------------------------
# Local control actions (files + direct script launch)
# -------------------------------------------------------------------

def _enter_kiosk() -> Tuple[bool, str]:
    """
    Enter kiosk mode by:
      - removing STOP_FLAG (if present)
      - starting kiosk-browser.sh directly (no systemd, no sudo)
      - writing PIDFILE for basic tracking
    """
    try:
        # Clear stop flag + stale pidfile
        for p in (STOP_FLAG, KIOSK_PIDFILE):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

        # If already running, do nothing
        if _kiosk_running():
            return True, "already_running"

        if not os.path.exists(KIOSK_SCRIPT):
            return False, f"missing_script:{KIOSK_SCRIPT}"

        # Start kiosk browser script with a display/session environment
        env = os.environ.copy()
        env.setdefault("DISPLAY", ":0")
        env.setdefault("XAUTHORITY", "/home/meadow/.Xauthority")
        env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")

        p = subprocess.Popen(
            ["bash", KIOSK_SCRIPT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )

        _write_pidfile(p.pid)
        return True, ""
    except Exception as e:
        return False, str(e)



def _exit_kiosk() -> Tuple[bool, str]:
    """
    Exit kiosk mode by:
      - creating STOP_FLAG (kiosk-browser.sh exits if it sees it)
      - killing kiosk-browser.sh + chromium kiosk processes (best-effort)
      - clearing PIDFILE
    """
    try:
        # Create stop flag
        try:
            # STOP_FLAG might be in /run/meadow/... in future; mkdir if needed
            d = os.path.dirname(STOP_FLAG)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(STOP_FLAG, "w", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z") + "\n")
        except Exception:
            pass

        # Kill the script loop (best-effort)
        subprocess.call(["pkill", "-f", r"kiosk-browser\.sh"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Kill chromium kiosk (best-effort)
        subprocess.call(["pkill", "-f", r"chromium.*--kiosk"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.call(["pkill", "-f", r"chromium-browser.*--kiosk"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.call(["pkill", "-f", r"chromium --kiosk"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.call(["pkill", "-f", r"chromium-browser --kiosk"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # If we have a pidfile, try to terminate that PID too
        pid = _read_pidfile()
        if _pid_is_running(pid):
            try:
                os.kill(pid, 15)
            except Exception:
                pass

        # Clear pidfile
        try:
            if os.path.exists(KIOSK_PIDFILE):
                os.remove(KIOSK_PIDFILE)
        except Exception:
            pass

        return True, ""
    except Exception as e:
        return False, str(e)


def _reload_kiosk() -> Tuple[bool, str]:
    ok, err = _exit_kiosk()
    if not ok:
        return False, err
    time.sleep(0.5)
    return _enter_kiosk()


def _set_url(url: str) -> Tuple[bool, str]:
    url = (url or "").strip()
    if not url:
        return False, "empty_url"
    try:
        with open(KIOSK_URL_FILE, "w", encoding="utf-8") as f:
            f.write(url + "\n")
        return True, ""
    except Exception as e:
        return False, str(e)


def _update_code(branch: str) -> Tuple[bool, str]:
    b = (branch or "main").strip() or "main"
    try:
        subprocess.Popen(["bash", UPDATE_SCRIPT, b])
        return True, ""
    except Exception as e:
        return False, str(e)


def _reboot() -> Tuple[bool, str]:
    try:
        subprocess.Popen(["sudo", "reboot"])
        return True, ""
    except Exception as e:
        return False, str(e)


def _shutdown() -> Tuple[bool, str]:
    try:
        subprocess.Popen(["sudo", "shutdown", "-h", "now"])
        return True, ""
    except Exception as e:
        return False, str(e)



# -------------------------------------------------------------------
# HTTP handler
# -------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self) -> None:
        self.send_response(204)
        _send_cors(self)
        self.end_headers()

    def do_GET(self) -> None:
        if self.path.startswith("/health"):
            snap = STATE.snapshot()
            return _json_response(self, 200, {
                "ok": True,
                "sigma_path": snap["sigma_path"],
                "sigma_baud": snap["sigma_baud"],
                "sigma_lockfile": snap["sigma_lockfile"],
                "motors_loaded": snap["motors_loaded"],
                "last_config_ok": snap["last_config_ok"],
                "last_config_ts": snap["last_config_ts"],
                "last_config_error": snap["last_config_error"],
                "kiosk_running": snap["kiosk"]["running"],
                "stop_flag_exists": snap["kiosk"]["stop_flag_exists"],
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

        if self.path.startswith("/sigma/warm"):
            return self._handle_sigma_warm()

        if self.path.startswith("/sigma/purchase"):
            return self._handle_sigma_purchase()

        if self.path.startswith("/vend"):
            return self._handle_vend()

        if self.path.startswith("/admin/vend-test"):
            return self._handle_admin_vend_test()

        if self.path.startswith("/admin/control"):
            return self._handle_admin_control()

        return _json_response(self, 404, {"ok": False, "error": "not_found"})

    # -----------------------------
    # Sigma
    # -----------------------------

    def _handle_sigma_warm(self) -> None:
        """
        Best-effort warm:
        - If Sigma is busy (purchase or warm already running), return warm_skipped immediately.
        - Otherwise run ensure_idle() (bounded), which performs STATUS=20 recovery if needed.
        """
        _ = _read_json(self)

        sigma_path, sigma_baud = STATE.get_sigma()
        port_candidates = [sigma_path, "/dev/sigma", "/dev/ttyACM0", "/dev/ttyUSB0"]

        t0 = time.time()
        lock = _SigmaGlobalLock(SIGMA_LOCKFILE)
        if not lock.acquire(timeout=SIGMA_BUSY_LOCK_TIMEOUT):
            return _json_response(self, 200, {
                "ok": True,
                "warm_skipped": True,
                "reason": "sigma_busy",
                "t_ms": int((time.time() - t0) * 1000),
            })

        try:
            last_err = ""
            for port in port_candidates:
                if not port or not os.path.exists(port):
                    continue
                try:
                    with SigmaIppClient(port=port, baudrate=sigma_baud) as sigma:
                        idle_ok = sigma.ensure_idle(max_total_wait=SIGMA_WARM_MAX_WAIT_SECS)

                    return _json_response(self, 200, {
                        "ok": True,
                        "warm_skipped": False,
                        "idle_ok": bool(idle_ok),
                        "port": port,
                        "t_ms": int((time.time() - t0) * 1000),
                    })
                except Exception as e:
                    last_err = "".join(traceback.format_exception(type(e), e, e.__traceback__))[-2000:]
                    continue

            return _json_response(self, 502, {
                "ok": False,
                "error": "sigma_warm_failed",
                "detail": last_err,
                "t_ms": int((time.time() - t0) * 1000),
            })
        finally:
            lock.release()

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

        t0 = time.time()
        lock = _SigmaGlobalLock(SIGMA_LOCKFILE)
        if not lock.acquire(timeout=SIGMA_PURCHASE_LOCK_TIMEOUT):
            # 423 Locked makes it clearer to the JS that this is a retry-able lock issue.
            return _json_response(self, 423, {
                "ok": False,
                "error": "sigma_busy_try_again",
                "retry_ms": 900,
                "t_ms": int((time.time() - t0) * 1000),
            })

        try:
            sigma_path, sigma_baud = STATE.get_sigma()
            port_candidates = [sigma_path, "/dev/sigma", "/dev/ttyACM0", "/dev/ttyUSB0"]

            last_err = ""
            for port in port_candidates:
                if not port or not os.path.exists(port):
                    continue

                try:
                    with SigmaIppClient(port=port, baudrate=sigma_baud) as sigma:
                        # purchase() already does:
                        #  - ensure_idle() (including STATUS=20 recovery)
                        #  - wait until final frame
                        r = sigma.purchase(
                            amount_minor=amount_minor_int,
                            currency_num=currency_num,
                            reference=reference,
                            first_wait=25.0,
                            final_wait=180.0,
                        )

                        # Extra safety: immediately try to return terminal to idle for next customer.
                        # (If it’s already idle, this is quick.)
                        try:
                            sigma.ensure_idle(max_total_wait=10.0)
                        except Exception:
                            pass

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
                        "raw": raw,
                        "receipt": raw.get("RECEIPT", ""),
                        "txid": str(raw.get("TXID") or raw.get("RRN") or ""),
                        "port": port,
                        "t_ms": int((time.time() - t0) * 1000),
                    }

                    if status and status != "0" and not approved:
                        return _json_response(self, 409, {"ok": False, "error": "sigma_rejected", **payload})

                    return _json_response(self, 200, {"ok": True, **payload})

                except Exception as e:
                    last_err = "".join(traceback.format_exception(type(e), e, e.__traceback__))[-2000:]
                    continue

            return _json_response(self, 502, {"ok": False, "error": "sigma_failed", "detail": last_err})

        finally:
            lock.release()

        # -----------------------------
    # Vend (async / non-blocking)
    # -----------------------------

    def _handle_vend(self) -> None:
        data = _read_json(self)
        try:
            motor = int(data.get("motor"))
        except Exception:
            return _json_response(self, 400, {"ok": False, "success": False, "error": "bad_motor"})

        controller = STATE.get_motors()
        if controller is None:
            return _json_response(self, 503, {"ok": False, "success": False, "error": "motors_not_loaded"})

        t0 = time.time()

        def _do_vend() -> None:
            try:
                controller.vend(motor)
            except Exception:
                # Best-effort: vend failures should be handled upstream (WP vend-result / telemetry)
                pass

        threading.Thread(target=_do_vend, daemon=True).start()

        return _json_response(self, 200, {
            "ok": True,
            "success": True,
            "queued": True,
            "motor": motor,
            "t_ms": int((time.time() - t0) * 1000),
        })

    def _handle_admin_vend_test(self) -> None:
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

        t0 = time.time()

        def _do_vend() -> None:
            try:
                controller.vend(motor)
            except Exception:
                pass

        threading.Thread(target=_do_vend, daemon=True).start()

        return _json_response(self, 200, {
            "ok": True,
            "motor": motor,
            "queued": True,
            "t_ms": int((time.time() - t0) * 1000),
        })

    # -----------------------------
    # Admin control
    # -----------------------------

    def _handle_admin_control(self) -> None:
        data = _read_json(self)
        ok, err = _auth_admin(data)
        if not ok:
            return _json_response(self, 403, {"ok": False, "error": err})

        action = str(data.get("action") or "").strip()
        payload = data.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}

        if action == "enter_kiosk":
            a_ok, a_err = _enter_kiosk()
        elif action == "exit_kiosk":
            a_ok, a_err = _exit_kiosk()
        elif action == "reload_kiosk":
            a_ok, a_err = _reload_kiosk()
        elif action == "set_url":
            a_ok, a_err = _set_url(str(payload.get("url") or ""))
        elif action == "set_url_reload":
            a_ok, a_err = _set_url(str(payload.get("url") or ""))
            if a_ok:
                a_ok, a_err = _reload_kiosk()
        elif action == "update_code":
            a_ok, a_err = _update_code(str(payload.get("branch") or "main"))
        elif action == "reboot":
            a_ok, a_err = _reboot()
        elif action == "shutdown":
            a_ok, a_err = _shutdown()
        else:
            return _json_response(self, 400, {"ok": False, "error": "unknown_action", "action": action})

        return _json_response(self, 200, {"ok": True, "action": action, "action_ok": a_ok, "action_err": a_err})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> None:
    threading.Thread(target=_config_poll_loop, daemon=True).start()
    threading.Thread(target=_heartbeat_loop, daemon=True).start()

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[pi_api] listening on http://{HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
 
