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
        self.port = str(port or "/dev/sigma")
        self.baud = int(baud or 115200)
        self.version = str(version or "202")
        self.timeout = float(timeout or 0.2)

    # -----------------------------
    # Low-level framing
    # -----------------------------
    def _send_frame(self, ser, lines):
        payload = ("".join([ln + "\r\n" for ln in lines])).encode("ascii")
        total_len = len(payload) + 2
        ser.write(total_len.to_bytes(2, "big") + payload)
        ser.flush()

    def _read_frame(self, ser, timeout=5.0):
        end = time.time() + float(timeout)
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
        try:
            ser.dtr = False
            ser.rts = False
            time.sleep(0.2)
            ser.dtr = True
            ser.rts = True
            time.sleep(0.2)
        except Exception:
            # Not all adapters expose these
            pass

    # -----------------------------
    # GET_STATUS (single session)
    # -----------------------------
    def _get_status_on_open_serial(self, ser, max_wait=10):
        sid = str(uuid.uuid4())
        lines = [
            "PROTOCOL=IPP",
            "METHOD=GET_STATUS",
            f"VERSION={self.version}",
            f"SID={sid}",
        ]
        self._send_frame(ser, lines)

        end = time.time() + float(max_wait)
        last = None
        while time.time() < end:
            props, _raw = self._read_frame(ser, timeout=3)
            if not props:
                continue
            last = props
            if props.get("METHOD") == "GET_STATUS" and props.get("SID") == sid:
                return props
        return last

    # -----------------------------
    # PURCHASE (matches your proven flow)
    # -----------------------------
    def purchase(self, amount_minor, currency_num="826", reference="", max_wait=180):
        """
        Blocking purchase.

        - Opens serial once
        - Toggles DTR/RTS once
        - GET_STATUS first (like your proven script)
        - Tries minor units first (e.g. "100" pence)
        - If rejected immediately, tries decimal (e.g. "1.00")
        - If accepted, keeps reading until "final" (often TIMEOUT==0)

        Returns dict:
          { approved: bool, status: str, stage: str, raw: dict, receipt: str }
        """

        # Normalize minor units to int
        try:
            minor_int = int(amount_minor)
        except Exception:
            minor_int = int(round(float(amount_minor) * 100))

        attempts = [
            str(minor_int),            # "100"
            f"{minor_int/100:.2f}",    # "1.00"
        ]

        last = None

        with serial.Serial(self.port, self.baud, timeout=self.timeout) as ser:
            self._toggle_lines(ser)

            # Wake/check terminal (this is the key difference vs your failing output)
            st = self._get_status_on_open_serial(ser, max_wait=10)
            # If terminal reports not-ready, bail early but return useful info
            if not st or str(st.get("STATUS") or "") != "0":
                return {
                    "approved": False,
                    "status": str((st or {}).get("STATUS") or ""),
                    "stage": str((st or {}).get("STAGE") or ""),
                    "raw": st or {},
                    "receipt": str((st or {}).get("RECEIPT") or ""),
                }

            # Now attempt purchase formats
            for amt in attempts:
                sid = str(uuid.uuid4())
                lines = [
                    "PROTOCOL=IPP",
                    "METHOD=PURCHASE",
                    f"VERSION={self.version}",
                    f"SID={sid}",
                    f"AMOUNT={amt}",
                    f"CURRENCY={currency_num}",
                ]
                if reference:
                    lines.append(f"REFERENCE={reference}")

                self._send_frame(ser, lines)

                # First response: accepted/rejected
                first_resp = None
                end_first = time.time() + 25
                while time.time() < end_first:
                    props, _raw = self._read_frame(ser, timeout=5)
                    if not props:
                        continue
                    # show only matching SID
                    if props.get("METHOD") == "PURCHASE" and props.get("SID") == sid:
                        first_resp = props
                        break

                if not first_resp:
                    last = {"PROTOCOL": "IPP", "METHOD": "PURCHASE", "SID": sid, "STATUS": "", "STAGE": ""}
                    continue

                last = first_resp

                # If rejected immediately, try next amount format
                if str(first_resp.get("STATUS") or "") != "0":
                    continue

                # Accepted -> keep reading until we get a "final" update
                end = time.time() + float(max_wait)
                seen_after_first = False

                while time.time() < end:
                    props, _raw = self._read_frame(ser, timeout=20)
                    if not props:
                        continue
                    if props.get("METHOD") != "PURCHASE" or props.get("SID") != sid:
                        continue

                    last = props

                    if seen_after_first:
                        # many firmwares signal completion with TIMEOUT=0
                        if str(props.get("TIMEOUT") or "") in ("", "0"):
                            break
                    else:
                        seen_after_first = True

                # We got as far as we can on this attempt
                break

        status = str((last or {}).get("STATUS") or "")
        stage = str((last or {}).get("STAGE") or "")

        # Approval heuristic:
        approved = False
        if last:
            if "APPROVED" in last:
                approved = str(last.get("APPROVED")).lower() in ("1", "true", "yes")
            elif "RESULT" in last:
                approved = str(last.get("RESULT")).upper() in ("APPROVED", "00")
            else:
                # fall back: STATUS==0
                approved = (status == "0")

        return {
            "approved": bool(approved),
            "status": status,
            "stage": stage,
            "raw": last or {},
            "receipt": str((last or {}).get("RECEIPT") or ""),
        }
