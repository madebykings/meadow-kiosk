import time
import RPi.GPIO as GPIO


def setup_motors(pin_map):
    """
    pin_map: dict like {"1": 22, "2": 23, "3": 24}
    """
    for pin in pin_map.values():
        GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)


def spin(pin, seconds):
    """
    Turn relay/motor ON for given time, then OFF.
    Assumes HIGH = ON for your relay board.
    """
    GPIO.output(pin, GPIO.HIGH)
    time.sleep(seconds)
    GPIO.output(pin, GPIO.LOW)


class MotorController:
    """Compatibility wrapper expected by `pi_api.py`.

    Newer versions of the Pi service run `pi_api.py`, which imports
    `MotorController` from this module. The original code in this repo only
    exposed `setup_motors()` + `spin()`. This class wraps those functions
    without changing existing behaviour.
    """

    def __init__(self, motor_pins, spin_times=None):
        # motor_pins: { "1": 17, "2": 27 } etc
        self.motor_pins = {int(k): int(v) for k, v in (motor_pins or {}).items()}
        self.spin_times = {int(k): float(v) for k, v in (spin_times or {}).items()}
        # Initialise pins
        setup_motors(self.motor_pins)

    def vend(self, motor: int) -> bool:
        motor = int(motor)
        if motor not in self.motor_pins:
            raise ValueError(f"Unknown motor {motor}")

        pin = self.motor_pins[motor]
        seconds = float(self.spin_times.get(motor, 1.0))
        if seconds <= 0:
            seconds = 1.0

        spin(pin, seconds)
        return True

    def cleanup(self):
        try:
            GPIO.cleanup()
        except Exception:
            pass
