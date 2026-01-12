import time
import uuid
import serial

class SigmaIPP:
    def __init__(self, port="/dev/sigma", version="202"):
        self.port = port
        self.version = version

    def _send(self, ser, lines):
        payload = ("".join([ln + "\r\n" for ln in lines])).encode("ascii")
        length = len(payload) + 2
        ser.write(length.to_bytes(2, "big") + payload)
        ser.flush()

    def _read(self, ser, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            hdr = ser.read(2)
            if len(hdr) < 2:
                continue
            total = int.from_bytes(hdr, "big")
            body = ser.read(total - 2)
            if len(body) < total - 2:
                continue

            text = body.decode("ascii", errors="replace")
            props = {}
            for line in text.split("\r\n"):
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k] = v
            return props
        return None

    def _toggle(self, ser):
        ser.dtr = False
        ser.rts = False
        time.sleep(0.2)
        ser.dtr = True
        ser.rts = True
        time.sleep(0.2)

    def purchase(self, amount, reference, currency="826", timeout=180):
        with serial.Serial(self.port, 115200, timeout=0.2) as ser:
            self._toggle(ser)

            sid = str(uuid.uuid4())
            self._send(ser, [
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
                p = self._read(ser, timeout=10)
                if not p:
                    continue

                if p.get("METHOD") != "PURCHASE":
                    continue
                if p.get("SID") != sid and p.get("SID_ORIGINAL") != sid:
                    continue

                if p.get("STAGE") == "5":
                    success = (
                        p.get("TX_STATUS") == "0"
                        and p.get("APPROVAL") == "00"
                    )
                    return {
                        "success": success,
                        "data": p,
                        "sid": sid
                    }

        return {"success": False, "data": None, "sid": sid}
