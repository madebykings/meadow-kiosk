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
