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

    def _wait_for_method_sid(self, ser, method, sid, max_wait=30, read_timeout=5):
        """
        Reads frames until we see METHOD=method and SID=sid.
        Returns (last_props_for_sid, all_frames_for_sid)
        """
        end = time.time() + max_wait
        frames = []
        last = None

        while time.time() < end:
            props, _raw = self._read_frame(ser, timeout=read_timeout)
            if not props:
                continue

            if props.get("SID") == sid and props.get("METHOD") == method:
                frames.append(props)
                last = props

                # Heuristic: many firmwares mark finality with TIMEOUT=0 or missing
                t = props.get("TIMEOUT")
                if t in (None, "", "0", 0):
                    break

        return last, frames

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

    def _amount_alt_format(self, amount):
        """
        If amount is "100" (minor units) => alt "1.00"
        If amount is "1.00" (major units) => alt "100"
        """
        s = str(amount).strip()

        # Looks like decimal major units
        if "." in s:
            try:
                pence = int(round(float(s) * 100))
                return str(pence)
            except Exception:
                return None

        # Looks like minor units
        try:
            pence = int(s)
            return f"{pence/100:.2f}"
        except Exception:
            return None

    def _purchase_once(self, ser, amount, currency="826", reference=""):
        """
        One PURCHASE attempt within an already-opened serial session.
        Returns dict: approved/status/stage/raw/frames
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

        self._send_frame(ser, lines)

        # Collect frames for this SID until final-ish frame
        last, frames = self._wait_for_method_sid(ser, "PURCHASE", sid, max_wait=180, read_timeout=20)

        last_props = last or (frames[-1] if frames else {})

        status = str((last_props or {}).get("STATUS") or "")
        stage = str((last_props or {}).get("STAGE") or "")

        # IMPORTANT:
        # STATUS=0 often just means "accepted / proceed" early, not final approval.
        # Prefer RESULT/APPROVED if present in *final* frame; otherwise treat
        # terminal final STATUS != 0 as decline.
        approved = False
        if last_props:
            if "RESULT" in last_props:
                approved = (str(last_props.get("RESULT")).upper() in ("APPROVED", "00", "SUCCESS"))
            elif "APPROVED" in last_props:
                approved = str(last_props.get("APPROVED")).lower() in ("1", "true", "yes")
            else:
                # Fallback: if we got to a final frame and STATUS==0, assume approved
                # (some firmwares don’t include RESULT/APPROVED)
                approved = (status == "0")

        return {
            "approved": bool(approved),
            "status": status,
            "stage": stage,
            "raw": last_props or {},
            "frames": frames,
            "sid": sid,
            "amount_sent": str(amount),
        }

    def purchase(self, amount, currency="826", reference=""):
        """
        amount: either "100" (minor units) or "1.00" depending on firmware.
        This method will:
          - Toggle lines
          - GET_STATUS gate (STATUS must be "0")
          - Try PURCHASE with amount; if instant reject pattern, retry with alt amount format
          - Keep reading until final-ish frame
        """
        with serial.Serial(self.port, self.baud, timeout=self.timeout) as ser:
            self._toggle_lines(ser)

            # 1) Gate on GET_STATUS ready
            st = None
            try:
                # inline status call on same open port for reliability
                sid = str(uuid.uuid4())
                self._send_frame(ser, [
                    "PROTOCOL=IPP",
                    "METHOD=GET_STATUS",
                    f"VERSION={self.version}",
                    f"SID={sid}",
                ])
                end = time.time() + 10
                while time.time() < end:
                    props, _raw = self._read_frame(ser, timeout=3)
                    if props and props.get("METHOD") == "GET_STATUS" and props.get("SID") == sid:
                        st = props
                        break
            except Exception:
                st = None

            if not st or str(st.get("STATUS") or "") != "0":
                return {
                    "approved": False,
                    "status": str((st or {}).get("STATUS") or "NO_STATUS"),
                    "stage": str((st or {}).get("STAGE") or ""),
                    "raw": st or {},
                    "frames": [],
                    "error": "terminal_not_ready",
                }

            # 2) First attempt
            first = self._purchase_once(ser, amount=str(amount), currency=str(currency), reference=str(reference))

            # If we get the instant reject pattern (like your STATUS=20, STAGE=1, TIMEOUT=0),
            # retry with the alternate amount encoding.
            # We detect "instant" by: 1 frame only + final-ish + approved false + status != 0
            instant_reject = (
                (not first.get("approved")) and
                (first.get("status") not in ("0", "")) and
                (len(first.get("frames") or []) <= 1)
            )

            if instant_reject:
                alt = self._amount_alt_format(amount)
                if alt and alt != str(amount):
                    second = self._purchase_once(ser, amount=alt, currency=str(currency), reference=str(reference))
                    # Return the second attempt but keep first attempt in debug
                    second["attempt1"] = first
                    second["attempt2"] = True
                    return second

            first["attempt2"] = False
            return first
