import time
import os

import RPi.GPIO as GPIO

from modem import get_imei
from config_remote import get_config
from motors import setup_motors, spin
from wordpress import next_command, ack_command, heartbeat, set_screen_mode

# PIR sensor GPIO pin (BCM numbering)
PIR_PIN = 17

# Seconds of no motion before switching to ads mode
ADS_TIMEOUT = 30

# Seconds of no motion before turning screen off
IDLE_TIMEOUT = 300

# Where we'll store the kiosk URL for the browser launcher
KIOSK_URL_FILE = "/home/meadow/kiosk.url"


def screen_on():
    os.system("vcgencmd display_power 1")


def screen_off():
    os.system("vcgencmd display_power 0")


def write_kiosk_url(cfg):
    """
    Build the full kiosk URL from WordPress config and write it to /boot/kiosk.url
    Example: https://yourdomain.com/kiosk1
    """
    base = cfg["domain"].rstrip("/")
    page = cfg["kiosk_page"]  # e.g. "/kiosk1"
    url = base + page
    try:
        with open(KIOSK_URL_FILE, "w") as f:
            f.write(url.strip() + "\n")
        print("Kiosk URL written to", KIOSK_URL_FILE, "=>", url)
    except Exception as e:
        print("Failed to write kiosk URL file:", e)


def main():
    GPIO.setmode(GPIO.BCM)

    # 1) Get IMEI from SIM7600 (if present)
    imei = get_imei()
    print("IMEI:", imei)

    # 2) Get config from WordPress (or cache)
    cfg = get_config(imei=imei)
    print("Config loaded for kiosk_id", cfg["kiosk_id"])

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
    #   ...
    # }

    motors_map = cfg["motors"]        # motor_id -> GPIO pin
    spin_times = cfg["spin_time"]     # motor_id -> seconds

    # 3) Write kiosk URL so browser launcher knows where to go
    write_kiosk_url(cfg)

    # 4) Setup GPIO
    setup_motors(motors_map)
    GPIO.setup(PIR_PIN, GPIO.IN)

    last_motion = time.time()
    last_heartbeat = 0
    current_mode = "browse"

    screen_on()
    heartbeat(cfg, imei=imei)
    set_screen_mode(cfg, current_mode)

    try:
        while True:
            now = time.time()

            # PIR logic: motion detected
            if GPIO.input(PIR_PIN):
                last_motion = now
                screen_on()
                if current_mode != "browse":
                    current_mode = "browse"
                    set_screen_mode(cfg, current_mode)

            idle_for = now - last_motion

            # Switch to ads mode after ADS_TIMEOUT
            if idle_for > ADS_TIMEOUT and current_mode != "ads":
                current_mode = "ads"
                set_screen_mode(cfg, current_mode)

            # Turn screen off after IDLE_TIMEOUT
            if idle_for > IDLE_TIMEOUT:
                screen_off()

            # Poll WP for vend commands
            cmd = next_command(cfg)
            if cmd:
                motor_id = str(cmd["motor"])
                cmd_id = cmd["id"]

                if motor_id in motors_map:
                    pin = motors_map[motor_id]
                    duration = spin_times.get(motor_id, 1.2)
                    try:
                        spin(pin, duration)
                        ack_command(cfg, cmd_id, success=True)
                    except Exception:
                        ack_command(cfg, cmd_id, success=False)
                else:
                    # Unknown motor id
                    ack_command(cfg, cmd_id, success=False)

            # Heartbeat every 60 seconds
            if now - last_heartbeat > 60:
                heartbeat(cfg, imei=imei)
                last_heartbeat = now

            time.sleep(0.2)

    finally:
        GPIO.cleanup()


if __name__ == "__main__":
    main()

