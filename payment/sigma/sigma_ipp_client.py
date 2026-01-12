import time
import uuid
import serial


class SigmaIPP:
    """
    Minimal IPP-over-serial client for myPOS Sigma.

    Frame format:
      2-byte big-endian length (including header) + ASCII "KEY=VALUE\r\n" lines.
    PROTOCOL must be first, METHOD must be second.
    """

    def __init__(self, port="/dev/sigma", baud=115200, version="202", timeout=0.2):
        self.port = port
        self.baud = int(baud or 115200)
        self.version = str(version or "202")
        self.timeout = float(timeout or 0.2)

    def _send_frame(self, ser, lines):
        payload = ("".join([ln + "\r\n" for ln in lines])).encode("ascii")
        total_len = len(payload) + 2
        ser.write(total_len.to_bytes(2, "big") + payload)
        ser.flush()

    def _read_frame(self, ser, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            hdr = ser.read(2)
            if len(hdr) < 2:
                continue

            total_len = int.from_bytes(hdr, "big")
            if total_len < 3:
                continue

            payload = ser.read(total_len - 2)
            if len(payload) < (total_len - 2):
                continue

            txt = payload.decode("ascii", errors="replace")
            props = {}
            for ln in txt.split("\r\n"):
                if "=" in ln:
                    k, v = ln.split("=", 1)
                    props[k] = v
            return props, txt

        return None, None

    def _toggle_lines(self, ser):
        # Some CDC-ACM devices behave better after DTR/RTS toggles
        ser.dtr = False
        ser.rts = False
        time.sleep(0.2)
        ser.dtr = True
        ser.rts = True
        time.sleep(0.2)

    def get_status(self):
        sid = str(uuid.uuid4())
        lines = [
            "PROTOCOL=IPP",
            "METHOD=GET_STATUS",
            f"VERSION={self.version}",
            f"SID={sid}",
        ]

        with serial.Serial(self.port, self.baud, timeout=self.timeout) as ser:
            self._toggle_lines(ser)
            self._send_frame(ser, lines)

            end = time.time() + 10
            while time.time() < end:
                props, _raw = self._read_frame(ser, timeout=3)
                if not props:
                    continue
                if props.get("METHOD") == "GET_STATUS" and props.get("SID") == sid:
                    return props

        return None

    def purchase(self, amount, currency="826", reference=""):
        """
        amount: either "100" (minor units) or "1.00" depending on firmware.
        Your pi_api sends minor units as string (e.g. "100") which is fine.
        """
        sid = str(uuid.uuid4())
        lines = [
            "PROTOCOL=IPP",
            "METHOD=PURCHASE",
            f"VERSION={self.version}",
            f"SID={sid}",
            f"AMOUNT={amount}",
            f"CURRENCY={currency}",
        ]
        if reference:
            lines.append(f"REFERENCE={reference}")

        with serial.Serial(self.port, self.baud, timeout=self.timeout) as ser:
            self._toggle_lines(ser)
            self._send_frame(ser, lines)

            # wait for matching PURCHASE response
            end = time.time() + 30
            last_props = None
            while time.time() < end:
                props, _raw = self._read_frame(ser, timeout=5)
                if not props:
                    continue
                last_props = props
                if props.get("METHOD") == "PURCHASE" and props.get("SID") == sid:
                    # Normalise output for the API layer
                    status = str(props.get("STATUS") or "")
                    stage = str(props.get("STAGE") or "")
                    approved = (status == "0") and (props.get("APPROVED") in ("1", "true", "TRUE", True) or props.get("RESULT") in ("APPROVED", "00"))
                    # Some firmwares don’t send APPROVED/RESULT; in that case pi_api can decide later.
                    return {
                        "approved": bool(approved) if ("APPROVED" in props or "RESULT" in props) else (status == "0"),
                        "status": status,
                        "stage": stage,
                        "raw": props,
                    }

        # If we timed out, return what we last saw (helps debugging)
        return {
            "approved": False,
            "status": str((last_props or {}).get("STATUS") or ""),
            "stage": str((last_props or {}).get("STAGE") or ""),
            "raw": last_props or {},
        }
