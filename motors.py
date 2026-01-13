# /home/meadow/meadow-kiosk/motors.py

import time
import RPi.GPIO as GPIO

# Use BCM because your config uses BCM pins (e.g. 23, 24)
GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)

def _pins_from_any(motor_pins):
    """
    Accept either:
      - dict motor->pin   e.g. {1:23, 2:24}
      - list/tuple/set of pins e.g. [23,24]
    """
    if isinstance(motor_pins, dict):
        return list(motor_pins.values())
    return list(motor_pins or [])

def setup_motors(motor_pins):
    pins = _pins_from_any(motor_pins)

    # de-dupe + sanitize
    clean = []
    for p in pins:
        try:
            p = int(p)
            if p > 0:
                clean.append(p)
        except Exception:
            pass

    for pin in sorted(set(clean)):
        GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)

def pulse_pin(pin: int, seconds: float):
    pin = int(pin)
    seconds = float(seconds)
    GPIO.output(pin, GPIO.HIGH)
    time.sleep(seconds)
    GPIO.output(pin, GPIO.LOW)

class MotorController:
    def __init__(self, motor_pins: dict, spin_times: dict):
        # motor_pins: {motor:int -> bcm_pin:int}
        self.motor_pins = {int(k): int(v) for k, v in (motor_pins or {}).items()}
        self.spin_times = {int(k): float(v) for k, v in (spin_times or {}).items()}
        setup_motors(self.motor_pins)

    def vend(self, motor: int):
        motor = int(motor)
        if motor not in self.motor_pins:
            raise ValueError(f"Motor {motor} not mapped")

        pin = self.motor_pins[motor]
        seconds = float(self.spin_times.get(motor, 2.0))
        pulse_pin(pin, seconds)
