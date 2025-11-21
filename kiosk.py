import time
import os

import RPi.GPIO as GPIO

from modem import get_imei
from config_remote import get_config
from motors import setup_motors, spin
from wordpress import next_command, ack_command, heartbeat, set_screen_mode

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
    print("=== Meadow Kiosk Starting ===", flush=True)

    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)

    # 1) Get IMEI from SIM7600 (if present)
    imei = get_imei()
    print("IMEI:", imei, flush=True)

    # 2) Get config from WordPress (or cache)
    cfg = get_config(imei=imei)
    print("Config loaded for kiosk", cfg["kiosk_id"], flush=True)

    # cfg shape:
    # {
    #   "kiosk_id": 1,
    #   "domain": "https://yourdomain.com",
    #   "api_key": "SECRET",
    #   "motors": {"1": 22, "2": 23, "3": 24},
    #   "spin_time": {"1": 1.25, "2": 1.10, "3": 1.40},
    #   "kiosk_page": "/kiosk1",
    #   "ads_page": "/kiosk1-ads",
    #   "config_version": 4,
    #   "mode": "vending" | "display_collect" | "display_only",
    #   "pir_pin": 22,
    #   "ads_timeout": 30,
    #   "idle_timeout": 300,
    #   ...
    # }

    motors_map = cfg["motors"]        # motor_id -> GPIO pin
    spin_times = cfg["spin_time"]     # motor_id -> seconds

    # PIR settings from WP (with sane defaults)
    pir_pin = int(cfg.get("pir_pin", 22))        # default BCM 22 if not set
    ads_timeout = int(cfg.get("ads_timeout", 30))
    idle_timeout = int(cfg.get("idle_timeout", 300))

    # 3) Write kiosk URL so browser launcher knows where to go
    write_kiosk_url(cfg)

    # 4) Setup GPIO outputs for motors
    setup_motors(motors_map)

    # 5) Setup PIR (if configured)
    if pir_pin > 0:
        GPIO.setup(pir_pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
        print(f"[BOOT] PIR configured on BCM {pir_pin}", flush=True)
    else:
        print("[BOOT] PIR disabled (pir_pin <= 0)", flush=True)

    # State
    last_motion = time.time()
    last_heartbeat = 0
    current_mode = "ads"   # idle mode on boot
    in_vend = False        # True while a motor is spinning

    # Turn screen on and announce initial mode
    screen_on()
    heartbeat(cfg, imei=imei)
    print("[HB] Heartbeat sent", flush=True)
    set_screen_mode(cfg, current_mode)
    print("[PIR] Initial ADS mode", flush=True)

    try:
        while True:
            now = time.time()

            # --- PIR / screen-mode logic (only if PIR configured and NOT vending) ---
            if pir_pin > 0 and not in_vend:
                # Debounced read: majority of 5 samples to filter jitter
                samples = [GPIO.input(pir_pin) for _ in range(5)]
                pir_state = 1 if samples.count(1) >= 3 else 0

                # Motion detected (stable)
                if pir_state == 1:
                    last_motion = now
                    screen_on()
                    if current_mode in ("ads", "browse") and current_mode != "browse":
                        current_mode = "browse"
                        print("[PIR] Motion -> browse (debounced)", flush=True)
                        set_screen_mode(cfg, current_mode)

                idle_for = now - last_motion

                # After ads_timeout seconds of no motion, go to ads
                if idle_for > ads_timeout and current_mode == "browse":
                    current_mode = "ads"
                    print("[PIR] Idle -> ads", flush=True)
                    set_screen_mode(cfg, current_mode)

                # After idle_timeout seconds of no motion, turn screen off
                if idle_for > idle_timeout:
                    screen_off()

            # --- Poll WP for vend commands ---
            cmd = next_command(cfg)
            if cmd:
                motor_id = str(cmd["motor"])
                cmd_id = cmd["id"]

                # For safety, only touch motors in 'vending' mode kiosks
                kiosk_mode = cfg.get("mode", "vending")

                if kiosk_mode == "vending" and motor_id in motors_map:
                    pin = motors_map[motor_id]
                    duration = float(spin_times.get(motor_id, 1.2))

                    # Tell WP we are vending
                    current_mode = "vending"
                    set_screen_mode(cfg, current_mode)

                    in_vend = True
                    try:
                        print(f"[VEND] Motor {motor_id} on pin {pin} for {duration}s", flush=True)
                        spin(pin, duration)
                        ack_command(cfg, cmd_id, success=True)
                        print(f"[VEND] Complete OK for command {cmd_id}", flush=True)
                    except Exception as e:
                        print(f"[VEND] ERROR for command {cmd_id}: {e}", flush=True)
                        ack_command(cfg, cmd_id, success=False)
                    finally:
                        in_vend = False

                else:
                    # Kiosk is not in vending mode or unknown motor ID
                    print(f"[VEND] Ignored command {cmd_id} (mode={kiosk_mode}, motor_id={motor_id})", flush=True)
                    ack_command(cfg, cmd_id, success=False)

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
