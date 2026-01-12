import time
import uuid
import serial


class SigmaIPP:
    """
    Minimal myPOS IPP client for Sigma/UN20 over USB CDC-ACM.

    - Uses framed IPP messages: 2-byte big-endian length + KEY=VALUE\\r\\n lines.
    - Known-good on IPP VERSION=202 for Sigma UN20.
    - Uses /dev/sigma by default (set via udev rule) to avoid ttyACM renumbering.
    """

    def __init__(self, port="/dev/sigma", version="202"):
        self.port = port
        self.version = version

    def _toggle_lines(self, ser):
        # Some CDC-ACM devices respond more reliably after toggling line state.
        ser.dtr = False
        ser.rts = False
        time.sleep(0.2)
        ser.dtr = True
        ser.rts = True
        time.sleep(0.2)

    def _send_frame(self, ser, lines):
        # PROTOCOL must be first, METHOD must be second.
        payload = ("".join([ln + "\r\n" for ln in lines])).encode("ascii")
        total_len = len(payload) + 2
        ser.write(total_len.to_bytes(2, "big") + payload)
        ser.flush()

    def _read_frame(self, ser, timeout=10.0):
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
            return props
        return None

    def get_status(self, timeout=8.0):
        with serial.Serial(self.port, 115200, timeout=0.2) as ser:
            self._toggle_lines(ser)

            sid = str(uuid.uuid4())
            self._send_frame(ser, [
                "PROTOCOL=IPP",
                "METHOD=GET_STATUS",
                f"VERSION={self.version}",
                f"SID={sid}",
            ])

            end = time.time() + timeout
            while time.time() < end:
                p = self._read_frame(ser, timeout=3.0)
                if not p:
                    continue
                if p.get("METHOD") == "GET_STATUS" and p.get("SID") == sid:
                    return p
            return None

    def purchase(self, amount, reference, currency="826", timeout=180):
        """
        amount: string like "1.00"
        currency: numeric ISO 4217, GBP=826
        reference: your order/command reference (recommended)
        Returns: {"success": bool, "data": dict|None, "sid": sid}
        """
        sid = str(uuid.uuid4())
        with serial.Serial(self.port, 115200, timeout=0.2) as ser:
            self._toggle_lines(ser)

            self._send_frame(ser, [
                "PROTOCOL=IPP",
                "METHOD=PURCHASE",
                f"VERSION={self.version}",
                f"SID={sid}",
                f"AMOUNT={amount}",
                f"CURRENCY={currency}",
                f"REFERENCE={reference}",
            ])

            end = time.time() + timeout
            while time.time() < end:
                p = self._read_frame(ser, timeout=10.0)
                if not p:
                    continue

                if p.get("METHOD") != "PURCHASE":
                    continue
                # Some responses echo SID in SID_ORIGINAL
                if p.get("SID") != sid and p.get("SID_ORIGINAL") != sid:
                    continue

                if p.get("STAGE") == "5":
                    success = (p.get("TX_STATUS") == "0" and p.get("APPROVAL") == "00")
                    return {"success": success, "data": p, "sid": sid}

            return {"success": False, "data": None, "sid": sid}
