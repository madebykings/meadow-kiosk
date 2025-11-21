cd ~/meadow-kiosk
cat > kiosk.py << 'EOF'
#!/usr/bin/env python3
import os
import sys
import time
import threading

import RPi.GPIO as GPIO

from config_remote import get_config
from motors import setup_motors, spin
from wordpress import next_command, ack_command, heartbeat, set_screen_mode

# Where the browser kiosk script reads its URL from
KIOSK_URL_FILE = "/home/meadow/kiosk.url"

# PIR settings
PIR_PIN = 22         # Your chosen PIR pin (BCM)
ADS_IDLE_SECONDS = 30
HEARTBEAT_SECONDS = 60
COMMAND_POLL_SECONDS = 2
THANKYOU_SECONDS = 5

_allow_pir_screen_changes = True


def write_kiosk_url(cfg):
    base = cfg.get("domain", "").rstrip("/")
    page = cfg.get("kiosk_page", "/")
    url = f"{base}{page}"

    try:
        os.makedirs(os.path.dirname(KIOSK_URL_FILE), exist_ok=True)
        with open(KIOSK_URL_FILE, "w") as f:
            f.write(url + "\n")
        print(f"Kiosk URL written to {KIOSK_URL_FILE} => {url}", flush=True)
    except Exception as e:
        print(f"Failed to write kiosk URL file: {e}", file=sys.stderr, flush=True)


def setup_gpio():
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)


def pir_watcher_thread(cfg):
    global _allow_pir_screen_changes

    last_motion_ts = time.time()
    current_mode = "ads"

    try:
        set_screen_mode(cfg, "ads")
        print("[PIR] Initial ADS mode", flush=True)
    except Exception:
        pass

    while True:
        try:
            now = time.time()
            pir_state = GPIO.input(PIR_PIN)

            # Motion
            if pir_state:
                last_motion_ts = now
                if _allow_pir_screen_changes and current_mode != "browse":
                    try:
                        set_screen_mode(cfg, "browse")
                        current_mode = "browse"
                        print("[PIR] Motion -> browse", flush=True)
                    except Exception:
                        pass

            # No motion — idle timeout
            elif (
                _allow_pir_screen_changes
                and current_mode != "ads"
                and (now - last_motion_ts) > ADS_IDLE_SECONDS
            ):
                try:
                    set_screen_mode(cfg, "ads")
                    current_mode = "ads"
                    print("[PIR] Idle -> ads", flush=True)
                except Exception:
                    pass

            time.sleep(0.2)

        except Exception as e:
            print(f"[PIR] Error: {e}", flush=True)
            time.sleep(1)


def vend_one_command(cfg, motors_map, cmd):
    global _allow_pir_screen_changes

    cmd_id = cmd.get("id")
    motor_num = str(cmd.get("motor"))

    if not cmd_id or not motor_num:
        print("[VEND] Invalid command", flush=True)
        return

    pin = motors_map.get(motor_num)
    if not pin:
        print(f"[VEND] Unknown motor {motor_num}", flush=True)
        ack_command(cfg, cmd_id, success=False)
        return

    seconds = float(cfg.get("spin_time", {}).get(motor_num, 2.0))

    print(f"[VEND] Starting vend motor {motor_num} on pin {pin}", flush=True)

    _allow_pir_screen_changes = False

    try:
        set_screen_mode(cfg, "vending")
        spin(pin, seconds)
        ack_command(cfg, cmd_id, success=True)
        print(f"[VEND] Vend done", flush=True)

    except Exception as e:
        print(f"[VEND] Error: {e}", flush=True)
        ack_command(cfg, cmd_id, success=False)

    finally:
        time.sleep(THANKYOU_SECONDS)
        _allow_pir_screen_changes = True


def main():
    print("=== Meadow Kiosk Starting ===", flush=True)

    setup_gpio()

    # IMEI not used in this build
    imei = None
    print("IMEI:", imei, flush=True)

    cfg = get_config(imei=imei)
    print(f"Config loaded for kiosk {cfg.get('kiosk_id')}", flush=True)

    write_kiosk_url(cfg)

    motors_map = cfg.get("motors", {})
    spin_times = cfg.get("spin_time", {})
    if not motors_map:
        print("[BOOT] WARNING: no motors configured in config", flush=True)

    setup_motors(motors_map)
    GPIO.setup(PIR_PIN, GPIO.IN)
    print(f"[BOOT] PIR configured on BCM {PIR_PIN}", flush=True)

    pir_thread = threading.Thread(target=pir_watcher_thread, args=(cfg,), daemon=True)
    pir_thread.start()

    last_heartbeat = 0

    try:
        while True:
            now = time.time()

            if now - last_heartbeat > HEARTBEAT_SECONDS:
                heartbeat(cfg, imei=imei)
                last_heartbeat = now
                print("[HB] Heartbeat sent", flush=True)

            cmd = next_command(cfg)
            if cmd:
                print("[CMD] Received:", cmd, flush=True)
                vend_one_command(cfg, motors_map, cmd)
            else:
                time.sleep(COMMAND_POLL_SECONDS)

    except KeyboardInterrupt:
        pass
    finally:
        GPIO.cleanup()
        print("GPIO cleaned up", flush=True)


if __name__ == "__main__":
    main()
EOF
