import serial
import time


def get_imei(port="/dev/ttyUSB2"):
    """
    Query SIM7600 over AT commands to get IMEI.
    Returns IMEI string or None.
    """
    try:
        ser = serial.Serial(port, baudrate=115200, timeout=1)
    except Exception:
        return None

    time.sleep(0.5)
    ser.write(b'AT+GSN\r')
    time.sleep(0.5)
    resp = ser.read_all().decode(errors="ignore")
    ser.close()

    lines = [l.strip() for l in resp.splitlines() if l.strip()]
    for line in lines:
        if line.isdigit() and 14 <= len(line) <= 15:
            return line

    return None
