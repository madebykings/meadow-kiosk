#!/usr/bin/env python3
import os
import json
import time
import signal
import sys
from typing import Dict, Any, Optional

import requests
import RPi.GPIO as GPIO

# ---------------------------------------------------------------------------
# Config / globals
# ---------------------------------------------------------------------------

PROVISION_PATH = "/boot/provision.json"
LOCAL_CONFIG_CACHE = "/boot/meadow-config.json"

POLL_NEXT_COMMAND_EVERY = 2.0    # seconds
POLL_SCREEN_MODE_EVERY  = 2.0    # seconds
LOG_PREFIX = "[MEADOW]"

config: Dict[str, Any] = {}
base_url: str = ""
kiosk_id: int = 0
api_key: str = ""

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", flush=True)


def log_error(msg: str) -> None:
    print(f"{LOG_PREFIX} ERROR: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Provisioning / config
# ---------------------------------------------------------------------------

def load_provision() -> Dict[str, Any]:
    """
    Read /boot/provision.json which should look like:

    {
      "token": "UNIT-0001-SPARTAN",
      "key": "MASTER-PROVISION-KEY",
      "domain": "https://greig65.sg-host.com"
    }
    """
    if not os.path.exists(PROVISION_PATH):
        raise RuntimeError(f"Provision file not found: {PROVISION_PATH}")

    with open(PROVISION_PATH, "r") as f:
        data = json.load(f)

    token = data.get("token")
    key = data.get("key")
    domain = data.get("domain")

    if not token or not key or not domain:
        raise RuntimeError("Provision file missing token/key/domain")

    return {
        "token": token,
        "key": key,
        "domain": domain.rstrip("/"),
    }


def fetch_kiosk_config(provision: Dict[str, Any]) -> Dict[str, Any]:
    """
    Call /meadow/v1/kiosk-config to get motors, pins, timeouts etc.
    """
    url = f"{provision['domain']}/wp-json/meadow/v1/kiosk-config"
    params = {
        "token": provision["token"],
        "key": provision["key"],
        # IMEI could be added here in future if you want
    }

    log(f"Fetching kiosk config from {url}")
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    cfg = resp.json()

    # Cache a copy locally for debugging
    try:
        with open(LOCAL_CONFIG_CACHE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        log_error(f"Failed to write local config cache: {e}")

    return cfg


def apply_config(cfg: Dict[str, Any]) -> None:
    global config, base_url, kiosk_id, api_key

    config = cfg
    base_url = cfg.get("domain", "").rstrip("/")
    kiosk_id = int(cfg.get("kiosk_id") or 0)
    api_key = cfg.get("api_key", "")

    if not base_url or not kiosk_id or not api_key:
        raise RuntimeError("Config missing domain/kiosk_id/api_key")

    log(f"Kiosk ID: {kiosk_id}")
    log(f"Base URL: {base_url}")

    # Motors pins
    motors = cfg.get("motors") or {}
    spin_time = cfg.get("spin_time") or {}
    log(f"Motors: {motors}")
    log(f"Spin times: {spin_time}")

    # PIR setup
    pir_pin = int(cfg.get("pir_pin") or 0)
    if pir_pin:
        GPIO.setup(pir_pin, GPIO.IN)
        log(f"PIR on GPIO {pir_pin}")
    else:
        log("No PIR configured (pir_pin == 0)")

    # Motor pins setup
    for motor_id, pin in motors.items():
        try:
            pin_int = int(pin)
        except (TypeError, ValueError):
            continue
        if not pin_int:
            continue
        GPIO.setup(pin_int, GPIO.OUT)
        GPIO.output(pin_int, GPIO.LOW)
        log(f"Motor {motor_id} on GPIO {pin_int}")

    # Timeouts
    ads_timeout = int(cfg.get("ads_timeout") or 0)
    idle_timeout = int(cfg.get("idle_timeout") or 0)
    thankyou_timeout = int(cfg.get("thankyou_timeout") or 0) or 10

    log(f"ads_timeout={ads_timeout}  idle_timeout={idle_timeout}  thankyou_timeout={thankyou_timeout}")


# ---------------------------------------------------------------------------
# Screen mode helpers
# ---------------------------------------------------------------------------

def get_screen_mode() -> Optional[str]:
    """
    Ask WP what mode the kiosk screen should show.
    """
    global config, base_url, kiosk_id

    try:
        url = f"{base_url}/wp-json/meadow/v1/kiosk-screen"
        resp = requests.get(url, params={"kiosk_id": kiosk_id}, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        mode = data.get("mode")
        # order_id = data.get("order_id")  # available if you want it
        return mode
    except Exception as e:
        log_error(f"get_screen_mode failed: {e}")
        return None


def set_screen_mode(mode: str, order_id: Optional[int] = None) -> None:
    """
    Let WP know we've changed mode (e.g. ads/browse after PIR, reset thankyou).
    """
    global base_url, kiosk_id, api_key

    try:
        url = f"{base_url}/wp-json/meadow/v1/kiosk-screen"
        payload = {
            "kiosk_id": kiosk_id,
            "key": api_key,
            "mode": mode,
        }
        if order_id:
            payload["order_id"] = int(order_id)

        resp = requests.post(url, data=payload, timeout=5)
        resp.raise_for_status()
        log(f"Set screen mode -> {mode}")
    except Exception as e:
        log_error(f"set_screen_mode({mode}) failed: {e}")


# ---------------------------------------------------------------------------
# Vend command helpers
# ---------------------------------------------------------------------------

def poll_next_command() -> Optional[Dict[str, Any]]:
    """
    GET /next-command?kiosk_id=&key=
    Returns dict with id + motor, or None if none.
    """
    global base_url, kiosk_id, api_key

    try:
        url = f"{base_url}/wp-json/meadow/v1/next-command"
        resp = requests.get(url, params={"kiosk_id": kiosk_id, "key": api_key}, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        log(f"next_command raw: {data}")
        if not data:
            return None
        if isinstance(data, dict) and "id" in data:
            return data
        return None
    except Exception as e:
        log_error(f"poll_next_command failed: {e}")
        return None


def post_command_complete(cmd_id: int, success: bool) -> None:
    """
    POST /command-complete with id, key, success.
    """
    global base_url, api_key

    try:
        url = f"{base_url}/wp-json/meadow/v1/command-complete"
        payload = {
            "id": int(cmd_id),
            "key": api_key,
            "success": "1" if success else "0",
        }
        resp = requests.post(url, data=payload, timeout=5)
        resp.raise_for_status()
        log(f"command-complete id={cmd_id} success={success}")
    except Exception as e:
        log_error(f"post_command_complete failed: {e}")


def spin_motor(motor_number: int) -> bool:
    """
    Actually drive the motor GPIO pin for the configured spin_time.
    Returns True if we did something, False on obvious config error.
    """
    motors = config.get("motors") or {}
    spin_time_map = config.get("spin_time") or {}

    pin = int(motors.get(str(motor_number)) or 0)
    duration = float(spin_time_map.get(str(motor_number)) or 0)

    if not pin or duration <= 0:
        log_error(f"spin_motor: invalid config for motor {motor_number}: pin={pin}, duration={duration}")
        return False

    log(f"VENDING: motor {motor_number} on GPIO {pin} for {duration} sec")

    try:
        GPIO.output(pin, GPIO.HIGH)
        time.sleep(duration)
        GPIO.output(pin, GPIO.LOW)
        log(f"VEND DONE: motor {motor_number}")
        return True
    except Exception as e:
        log_error(f"spin_motor error: {e}")
        # Try to ensure motor is off
        try:
            GPIO.output(pin, GPIO.LOW)
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main_loop():
    pir_pin = int(config.get("pir_pin") or 0)
    ads_timeout = int(config.get("ads_timeout") or 0)
    thankyou_timeout = int(config.get("thankyou_timeout") or 0) or 10

    last_motion_time = time.time()
    motion_active = False

    last_screen_poll = 0.0
    current_mode: str = "ads"
    thankyou_started_at: Optional[float] = None

    last_cmd_poll = 0.0

    log("Entering main loop")

    while True:
        now = time.time()

        # ---------------- PIR handling (only if we have a PIR configured) ----------------
        if pir_pin:
            try:
                motion = GPIO.input(pir_pin) == GPIO.HIGH
            except Exception as e:
                log_error(f"Reading PIR failed: {e}")
                motion = False

            if motion:
                if not motion_active:
                    log("[PIR] Motion detected")
                    motion_active = True
                last_motion_time = now

                # Only switch to browse if we're not in one of the "forced" modes
                if current_mode == "ads":
                    # Don't override vending / thankyou / error
                    set_screen_mode("browse")
                    current_mode = "browse"
            else:
                if motion_active:
                    # Just for logging when motion stops
                    motion_active = False
                    log("[PIR] Motion stopped")

            # If we've been idle long enough, go back to ads (but not during vending/thankyou/error)
            if ads_timeout > 0 and current_mode in ("browse", "ads"):
                idle_for = now - last_motion_time
                if idle_for >= ads_timeout and current_mode != "ads":
                    log(f"[PIR] Idle for {idle_for:.1f}s >= ads_timeout {ads_timeout}, switching to ads")
                    set_screen_mode("ads")
                    current_mode = "ads"

        # ---------------- Poll screen mode from WP ----------------
        if now - last_screen_poll >= POLL_SCREEN_MODE_EVERY:
            last_screen_poll = now
            mode = get_screen_mode()
            if mode:
                # If WP drives it to vending/thankyou/error, respect that
                if mode != current_mode:
                    log(f"[SCREEN] Mode changed server-side: {current_mode} -> {mode}")
                    current_mode = mode
                    if mode in ("thankyou", "error"):
                        thankyou_started_at = now
                    else:
                        thankyou_started_at = None

        # ---------------- Auto reset thankyou / error back to ads ----------------
        if current_mode in ("thankyou", "error") and thankyou_started_at:
            elapsed = now - thankyou_started_at
            if elapsed >= thankyou_timeout:
                log(f"[SCREEN] {current_mode} for {elapsed:.1f}s >= thankyou_timeout {thankyou_timeout}, back to ads")
                set_screen_mode("ads")
                current_mode = "ads"
                thankyou_started_at = None

        # ---------------- Poll for vend commands ----------------
        if now - last_cmd_poll >= POLL_NEXT_COMMAND_EVERY:
            last_cmd_poll = now
            cmd = poll_next_command()
            if cmd:
                cmd_id = int(cmd.get("id") or 0)
                motor_num = int(cmd.get("motor") or 0)
                if cmd_id and motor_num:
                    # Do the vend
                    success = spin_motor(motor_num)
                    # Let WP know result (this will set screen_mode to thankyou/error)
                    post_command_complete(cmd_id, success)
                    # After this, next screen poll will see "thankyou" or "error"
                else:
                    log_error(f"Bad command payload: {cmd}")

        time.sleep(0.1)


# ---------------------------------------------------------------------------
# Entrypoint / cleanup
# ---------------------------------------------------------------------------

def cleanup(signum=None, frame=None):
    log("Cleaning up GPIO and exiting")
    try:
        GPIO.cleanup()
    except Exception:
        pass
    sys.exit(0)


def main():
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    prov = load_provision()
    cfg = fetch_kiosk_config(prov)
    apply_config(cfg)

    main_loop()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_error(f"Fatal error: {e}")
        cleanup()
