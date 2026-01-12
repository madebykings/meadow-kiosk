import time
import os
import json

import RPi.GPIO as GPIO

from modem import get_imei
from config_remote import get_config
from motors import setup_motors, spin
from wordpress import next_command, ack_command, heartbeat, set_screen_mode

from payment.sigma.sigma_ipp_client import SigmaIPP

# Where we'll store the kiosk URL for the browser launcher
KIOSK_URL_FILE = "/home/meadow/kiosk.url"

# Simple persistent state to prevent double-charge / double-vend
STATE_DIR = "/home/meadow/state"
CMD_STALE_SECONDS = 5 * 60  # treat "started" older than this as stale


def screen_on():
    # On Pi 5 this will log "Command not registered" but is harmless
    os.system("vcgencmd display_power 1")


def screen_off():
    os.system("vcgencmd display_power 0")


def write_kiosk_url(cfg):
    """
    Build the full kiosk URL from WordPress config and write it to KIOSK_URL_FILE
    Example: https://yourdomain.com/kiosk1
    """
    base = cfg["domain"].rstrip("/")
    page = cfg["kiosk_page"]  # e.g. "/kiosk1"
    url = base + page
    try:
        with open(KIOSK_URL_FILE, "w") as f:
            f.write(url.strip() + "\n")
        print("Kiosk URL written to", KIOSK_URL_FILE, "=>", url, flush=True)
    except Exception as e:
        print("Failed to write kiosk URL file:", e, flush=True)


def _state_path(cmd_id: int) -> str:
    return os.path.join(STATE_DIR, f"cmd_{cmd_id}.json")


def load_cmd_state(cmd_id: int):
    try:
        with open(_state_path(cmd_id), "r") as f:
            return json.load(f)
    except Exception:
        return None


def save_cmd_state(cmd_id: int, state: dict):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = _state_path(cmd_id) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, _state_path(cmd_id))


def is_stale(state: dict) -> bool:
    try:
        started_at = float(state.get("started_at", 0))
        return (time.time() - started_at) > CMD_STALE_SECONDS and not state.get("vended")
    except Exception:
        return True


def main():
    print("=== Meadow Kiosk Starting (TOUCH + myPOS Sigma) ===", flush=True)

    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)

    # 1) Get IMEI from SIM7600 (if present)
    imei = get_imei()
    print("IMEI:", imei, flush=True)

    # 2) Get config from WordPress (or cache)
    cfg = get_config(imei=imei)
    print("Config loaded for kiosk", cfg.get("kiosk_id"), flush=True)

    motors_map = cfg["motors"]        # motor_id -> GPIO pin
    spin_times = cfg["spin_time"]     # motor_id -> seconds
    thankyou_timeout = int(cfg.get("thankyou_timeout", 10))

    # Optional per-kiosk terminal lock (recommended)
    expected_terminal_id = cfg.get("sigma_terminal_id")  # e.g. "80417289"

    # 3) Write kiosk URL so browser launcher knows where to go
    write_kiosk_url(cfg)

    # 4) Setup GPIO outputs for motors
    setup_motors(motors_map)

    # 5) Setup Sigma
    sigma = SigmaIPP(port="/dev/sigma", version="202")

    # State
    last_heartbeat = 0
    current_mode = "ads"
    in_action = False
    thankyou_started_at = None

    # Turn screen on and announce initial mode
    screen_on()
    heartbeat(cfg, imei=imei)
    print("[HB] Heartbeat sent", flush=True)
    set_screen_mode(cfg, current_mode)
    print("[SCREEN] Initial ADS mode (touch kiosk)", flush=True)

    try:
        while True:
            now = time.time()

            # --- Auto-reset thankyou/error back to ads ---
            if thankyou_started_at is not None:
                elapsed = now - thankyou_started_at
                if elapsed > thankyou_timeout:
                    current_mode = "ads"
                    set_screen_mode(cfg, current_mode)
                    thankyou_started_at = None
                    print("[SCREEN] Return to ADS", flush=True)

            # --- Poll WP for commands ---
            cmd = next_command(cfg)
            if cmd and not in_action:
                cmd_id = int(cmd["id"])
                motor_id = str(cmd["motor"])
                kiosk_mode = cfg.get("mode", "vending")

                if kiosk_mode != "vending" or motor_id not in motors_map:
                    print(
                        f"[CMD] Ignored {cmd_id} (mode={kiosk_mode}, motor_id={motor_id})",
                        flush=True,
                    )
                    ack_command(cfg, cmd_id, success=False)
                    continue

                pin = motors_map[motor_id]
                duration = float(spin_times.get(motor_id, 1.2))
                amount = cmd.get("amount")

                if not amount:
                    print(f"[CMD] Missing amount for {cmd_id}", flush=True)
                    ack_command(cfg, cmd_id, success=False)
                    continue

                # Idempotency / anti-double charge/vend
                state = load_cmd_state(cmd_id)
                if state:
                    if is_stale(state):
                        print(f"[STATE] Stale state for {cmd_id}, resetting", flush=True)
                        state = None
                    elif state.get("vended"):
                        print(f"[STATE] Already vended {cmd_id}, acking", flush=True)
                        # tell WP it's complete (prevents repeats)
                        ack_command(cfg, cmd_id, success=True, payment_data=state.get("payment"))
                        continue
                    elif state.get("paid") and not state.get("vended"):
                        print(f"[STATE] Already paid {cmd_id}, will vend without charging again", flush=True)
                if not state:
                    state = {"started_at": time.time(), "paid": False, "vended": False, "payment": None}
                    save_cmd_state(cmd_id, state)

                in_action = True

                # If not paid yet, take payment now
                if not state.get("paid"):
                    reference = f"KIOSK-{cfg['kiosk_id']}-CMD-{cmd_id}"

                    current_mode = "payment"
                    set_screen_mode(cfg, current_mode)
                    print(f"[PAY] PURCHASE £{amount} ref={reference}", flush=True)

                    pay = sigma.purchase(amount=str(amount), reference=reference)

                    if not pay["success"]:
                        print("[PAY] DECLINED/TIMEOUT", flush=True)
                        ack_command(cfg, cmd_id, success=False)
                        current_mode = "error"
                        set_screen_mode(cfg, current_mode)
                        thankyou_started_at = time.time()
                        in_action = False
                        continue

                    tx = pay["data"] or {}

                    # Optional: ensure this kiosk uses the expected terminal
                    if expected_terminal_id and tx.get("TERMINAL_ID") != str(expected_terminal_id):
                        print(
                            f"[PAY] Terminal mismatch expected={expected_terminal_id} got={tx.get('TERMINAL_ID')}",
                            flush=True,
                        )
                        ack_command(cfg, cmd_id, success=False)
                        current_mode = "error"
                        set_screen_mode(cfg, current_mode)
                        thankyou_started_at = time.time()
                        in_action = False
                        continue

                    state["paid"] = True
                    state["payment"] = tx
                    save_cmd_state(cmd_id, state)
                    print(f"[PAY] APPROVED rrn={tx.get('RRN')} auth={tx.get('AUTH_CODE')}", flush=True)

                # Vend (either after new payment, or retry after crash with paid=True)
                current_mode = "vending"
                set_screen_mode(cfg, current_mode)

                try:
                    print(f"[VEND] Motor {motor_id} pin {pin} for {duration}s (cmd {cmd_id})", flush=True)
                    spin(pin, duration)

                    state["vended"] = True
                    save_cmd_state(cmd_id, state)

                    ack_command(cfg, cmd_id, success=True, payment_data=state.get("payment"))
                    print(f"[VEND] OK cmd {cmd_id}", flush=True)

                    # WP/frontend shows thankyou; Pi auto-returns to ads later
                    thankyou_started_at = time.time()

                except Exception as e:
                    print(f"[VEND] ERROR cmd {cmd_id}: {e}", flush=True)
                    ack_command(cfg, cmd_id, success=False)
                    thankyou_started_at = time.time()

                finally:
                    in_action = False

            # --- Heartbeat every 60 seconds ---
            if now - last_heartbeat > 60:
                heartbeat(cfg, imei=imei)
                last_heartbeat = now
                print("[HB] Heartbeat sent", flush=True)

            time.sleep(0.2)

    finally:
        GPIO.cleanup()
        print("GPIO cleaned up, exiting.", flush=True)


if __name__ == "__main__":
    main()
