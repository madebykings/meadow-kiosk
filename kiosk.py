import time
import os

import RPi.GPIO as GPIO

from modem import get_imei
from config_remote import get_config
from motors import setup_motors, spin
from wordpress import (
    next_command,
    ack_command,
    heartbeat,
    set_screen_mode,
)

# NEW: Sigma payment client
from payment.sigma.sigma_ipp_client import SigmaIPP

# Where we'll store the kiosk URL for the browser launcher
KIOSK_URL_FILE = "/home/meadow/kiosk.url"


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


def main():
    print("=== Meadow Kiosk Starting (TOUCH + myPOS Sigma) ===", flush=True)

    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)

    # 1) Get IMEI from SIM7600 (if present)
    imei = get_imei()
    print("IMEI:", imei, flush=True)

    # 2) Get config from WordPress (or cache)
    cfg = get_config(imei=imei)
    print("Config loaded for kiosk", cfg["kiosk_id"], flush=True)

    motors_map = cfg["motors"]        # motor_id -> GPIO pin
    spin_times = cfg["spin_time"]     # motor_id -> seconds
    thankyou_timeout = int(cfg.get("thankyou_timeout", 10))

    # Optional (recommended): expected terminal ID check
    expected_terminal_id = cfg.get("sigma_terminal_id")

    # 3) Write kiosk URL so browser launcher knows where to go
    write_kiosk_url(cfg)

    # 4) Setup GPIO outputs for motors
    setup_motors(motors_map)

    # 5) Init Sigma client (per-kiosk, per-Pi)
    sigma = SigmaIPP(port="/dev/sigma", version="202")

    # State
    last_heartbeat = 0
    current_mode = "ads"
    in_vend = False
    thankyou_started_at = None

    screen_on()
    heartbeat(cfg, imei=imei)
    set_screen_mode(cfg, current_mode)

    print("[SCREEN] Initial ADS mode", flush=True)

    try:
        while True:
            now = time.time()

            # --- Auto-reset thankyou/error back to ads ---
            if thankyou_started_at is not None:
                if now - thankyou_started_at > thankyou_timeout:
                    current_mode = "ads"
                    set_screen_mode(cfg, current_mode)
                    thankyou_started_at = None
                    print("[SCREEN] Returned to ADS", flush=True)

            # --- Poll WP for vend commands ---
            cmd = next_command(cfg)
            if cmd and not in_vend:
                motor_id = str(cmd["motor"])
                cmd_id = cmd["id"]
                kiosk_mode = cfg.get("mode", "vending")

                if kiosk_mode != "vending" or motor_id not in motors_map:
                    print(
                        f"[VEND] Ignored command {cmd_id} "
                        f"(mode={kiosk_mode}, motor_id={motor_id})",
                        flush=True,
                    )
                    ack_command(cfg, cmd_id, success=False)
                    continue

                pin = motors_map[motor_id]
                duration = float(spin_times.get(motor_id, 1.2))

                # --- PAYMENT STEP ---
                amount = cmd.get("amount")
                if not amount:
                    print("[PAY] Missing amount in command", flush=True)
                    ack_command(cfg, cmd_id, success=False)
                    continue

                reference = f"KIOSK-{cfg['kiosk_id']}-CMD-{cmd_id}"

                print(
                    f"[PAY] Starting payment £{amount} "
                    f"(ref={reference})",
                    flush=True,
                )

                current_mode = "payment"
                set_screen_mode(cfg, current_mode)

                payment = sigma.purchase(
                    amount=str(amount),
                    reference=reference,
                )

                if not payment["success"]:
                    print("[PAY] DECLINED or TIMEOUT", flush=True)
                    ack_command(cfg, cmd_id, success=False)
                    current_mode = "error"
                    set_screen_mode(cfg, current_mode)
                    thankyou_started_at = time.time()
                    continue

                tx = payment["data"]

                # Optional terminal-ID sanity check
                if expected_terminal_id:
                    if tx.get("TERMINAL_ID") != expected_terminal_id:
                        print(
                            "[PAY] Terminal ID mismatch!",
                            tx.get("TERMINAL_ID"),
                            flush=True,
                        )
                        ack_command(cfg, cmd_id, success=False)
                        continue

                print(
                    f"[PAY] APPROVED auth={tx.get('AUTH_CODE')} rrn={tx.get('RRN')}",
                    flush=True,
                )

                # --- VEND STEP ---
                current_mode = "vending"
                set_screen_mode(cfg, current_mode)
                in_vend = True

                try:
                    print(
                        f"[VEND] Motor {motor_id} on pin {pin} for {duration}s",
                        flush=True,
                    )
                    spin(pin, duration)

                    ack_command(
                        cfg,
                        cmd_id,
                        success=True,
                        payment_data=tx,  # WP should store metadata
                    )

                    print(f"[VEND] Complete OK for command {cmd_id}", flush=True)
                    thankyou_started_at = time.time()

                except Exception as e:
                    print(f"[VEND] ERROR: {e}", flush=True)
                    ack_command(cfg, cmd_id, success=False)
                    thankyou_started_at = time.time()

                finally:
                    in_vend = False

            # --- Heartbeat every 60s ---
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
