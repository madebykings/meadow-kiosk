#!/usr/bin/env python3
import os
import sys
import time
import threading

import RPi.GPIO as GPIO

from config_remote import get_config
from motors import setup_motors, spin_motor
from wordpress import next_command, ack_command, heartbeat, set_screen_mode

# Where the browser kiosk script reads its URL from
KIOSK_URL_FILE = "/home/meadow/kiosk.url"

# PIR + behaviour tuning
PIR_PIN = 17              # BCM pin for your PIR sensor
ADS_IDLE_SECONDS = 30     # how long with no motion before going back to ads
HEARTBEAT_SECONDS = 60    # ping WP every 60s
COMMAND_POLL_SECONDS = 2  # how often to poll next-command when idle
THANKYOU_SECONDS = 5      # how long to let "thankyou" show before PIR resumes

# Global flag used to keep PIR from fighting with vending
_allow_pir_screen_changes = True


def write_kiosk_url(cfg):
    """
    Build the full kiosk URL from WordPress config and write it to KIOSK_URL_FILE.
    e.g. https://domain.com/kiosk1
    """
    domain = cfg.get("domain", "").rstrip("/")
    kiosk_page = cfg.get("kiosk_page", "/")
    url = f"{domain}{kiosk_page}"

    try:
        os.makedirs(os.path.dirname(KIOSK_URL_FILE), exist_ok=True)
        with open(KIOSK_URL_FILE, "w") as f:
            f.write(url + "\n")
        print(f"Kiosk URL written to {KIOSK_URL_FILE} => {url}", flush=True)
    except Exception as e:
        print(f"Failed to write kiosk URL file: {e}", file=sys.stderr, flush=True)


def setup_gpio():
    """
    Basic GPIO setup. Motors module also sets up BCM mode, but we keep it explicit here.
    """
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)


def pir_watcher_thread(cfg):
    """
    Background thread:
      - Watches the PIR pin
      - If motion: set screen mode to 'browse'
      - If idle for ADS_IDLE_SECONDS: set screen mode to 'ads'
    This ONLY toggles between 'ads' and 'browse', and only if _allow_pir_screen_changes is True.
    """
    global _allow_pir_screen_changes

    last_motion_ts = time.time()
    current_mode = "ads"

    # Start in ADS mode
    try:
        set_screen_mode(cfg, "ads")
        print("[PIR] Initial screen mode set to ADS", flush=True)
    except Exception as e:
        print(f"[PIR] Failed to set initial ADS mode: {e}", file=sys.stderr, flush=True)

    while True:
        try:
            now = time.time()
            pir_state = GPIO.input(PIR_PIN)  # 1 = motion, 0 = no motion

            if pir_state:
                last_motion_ts = now
                if _allow_pir_screen_changes and current_mode != "browse":
                    try:
                        set_screen_mode(cfg, "browse")
                        current_mode = "browse"
                        print("[PIR] Motion detected -> BROWSE", flush=True)
                    except Exception as e:
                        print(f"[PIR] Failed to set BROWSE mode: {e}", file=sys.stderr, flush=True)
            else:
                # no motion; if we've been idle long enough, go back to ADS
                if (
                    _allow_pir_screen_changes
                    and current_mode != "ads"
                    and (now - last_motion_ts) > ADS_IDLE_SECONDS
                ):
                    try:
                        set_screen_mode(cfg, "ads")
                        current_mode = "ads"
                        print("[PIR] Idle timeout -> ADS", flush=True)
                    except Exception as e:
                        print(f"[PIR] Failed to set ADS mode: {e}", file=sys.stderr, flush=True)

            time.sleep(0.2)  # fast-ish poll, but not crazy
        except Exception as e:
            print(f"[PIR] Error in PIR watcher: {e}", file=sys.stderr, flush=True)
            time.sleep(1.0)  # don't spin if something goes wrong


def vend_one_command(cfg, motors_map, cmd):
    """
    Handle a single vend command:
      - Set screen to 'vending'
      - Spin the appropriate motor
      - Ack success/fail
      - Let Woo handle switching to 'thankyou' via /command-complete
    """
    global _allow_pir_screen_changes

    cmd_id = cmd.get("id")
    motor_num = int(cmd.get("motor", 0))
    if not cmd_id or not motor_num:
        print(f"[VEND] Invalid command payload: {cmd}", flush=True)
        return

    pin = motors_map.get(str(motor_num)) or motors_map.get(motor_num)
    if not pin:
        print(f"[VEND] No GPIO pin mapping for motor {motor_num}", flush=True)
        ack_command(cfg, cmd_id, success=False)
        return

    spin_time_cfg = cfg.get("spin_time", {}) or {}
    seconds = float(spin_time_cfg.get(str(motor_num), 2.0))

    print(f"[VEND] Starting vend: cmd_id={cmd_id}, motor={motor_num}, pin={pin}, seconds={seconds}", flush=True)

    success = False
    try:
        _allow_pir_screen_changes = False   # freeze PIR-based changes
        # Tell WP: we're vending
        set_screen_mode(cfg, "vending")

        # Spin the motor
        spin_motor(pin, seconds)
        success = True
        print(f"[VEND] Vend complete for cmd {cmd_id}", flush=True)
    except Exception as e:
        print(f"[VEND] ERROR spinning motor: {e}", file=sys.stderr, flush=True)
        success = False
    finally:
        try:
            ack_command(cfg, cmd_id, success=success)
        except Exception as e:
            print(f"[VEND] Failed to ack command {cmd_id}: {e}", file=sys.stderr, flush=True)

        # Let the 'thankyou' screen live for a moment.
        # Woo/REST sets screen_mode to 'thankyou' in /command-complete;
        # We just avoid PIR overriding it immediately.
        time.sleep(THANKYOU_SECONDS)
        _allow_pir_screen_changes = True


def main():
    print("=== Meadow Kiosk starting ===", flush=True)

    setup_gpio()

    imei = get_modem_imei()
    print(f"IMEI: {imei}", flush=True)

    cfg = get_config(imei=imei)
    print(f"Config loaded for kiosk_id {cfg.get('kiosk_id')}", flush=True)

    write_kiosk_url(cfg)

    # Setup motors
    motors_map = cfg.get("motors", {})
    if not motors_map:
        print("[BOOT] No motors mapping in config!", file=sys.stderr, flush=True)
    setup_motors(motors_map)

    # Setup PIR pin
    GPIO.setup(PIR_PIN, GPIO.IN)
    print(f"[BOOT] PIR configured on BCM {PIR_PIN}", flush=True)

    # Start PIR watcher thread
    pir_thread = threading.Thread(target=pir_watcher_thread, args=(cfg,), daemon=True)
    pir_thread.start()

    last_heartbeat = 0

    try:
        while True:
            now = time.time()

            # Heartbeat every HEARTBEAT_SECONDS
            if now - last_heartbeat > HEARTBEAT_SECONDS:
                try:
                    heartbeat(cfg, imei=imei)
                    last_heartbeat = now
                    print("[HB] Heartbeat sent", flush=True)
                except Exception as e:
                    print(f"[HB] Heartbeat failed: {e}", file=sys.stderr, flush=True)

            # Poll for commands
            cmd = next_command(cfg)
            if cmd:
                print(f"[CMD] Next command: {cmd}", flush=True)
                vend_one_command(cfg, motors_map, cmd)
            else:
                # Idle: short sleep so we don't hammer the API
                time.sleep(COMMAND_POLL_SECONDS)

    except KeyboardInterrupt:
        print("Shutting down (KeyboardInterrupt)…", flush=True)
    finally:
        GPIO.cleanup()
        print("GPIO cleaned up.", flush=True)


if __name__ == "__main__":
    main()
