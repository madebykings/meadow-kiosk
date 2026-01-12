import time
import uuid
import serial
import logging
from typing import Dict, Any, Optional, Tuple


# Dedicated Sigma serial log (separate from requests/urllib3 logs)
SIGMA_LOG_PATH = "/home/meadow/meadow-kiosk/sigma_serial.log"

sigma_logger = logging.getLogger("sigma_serial")
sigma_logger.setLevel(logging.DEBUG)

if not sigma_logger.handlers:
    fh = logging.FileHandler(SIGMA_LOG_PATH)
    fh.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [SIGMA_SERIAL] %(message)s")
    fh.setFormatter(fmt)
    sigma_logger.addHandler(fh)


def sigma_log(msg: str) -> None:
    try:
        sigma_logger.debug(msg)
    except Exception:
        pass


class SigmaIPP:
    """
    IPP-over-serial client for myPOS Sigma.

    Frame format:
      2-byte big-endian length (including the 2-byte header) + ASCII payload of "KEY=VALUE\\r\\n" lines.

    Important ordering:
      PROTOCOL must be first
      METHOD must be second
    """

    def __init__(self, port: str = "/dev/sigma", baud: int = 115200, version: str = "202", timeout: float = 0.2):
        self.port = port
        self.baud = int(baud or 115200)
        self.version = str(version or "202")
        self.timeout = float(timeout or 0.2)

    # ------------------------
    # Low-level frame helpers
    # ------------------------
    def _send_frame(self, ser: serial.Serial, lines) -> None:
        payload_txt = "".join([ln + "\r\n" for ln in lines])
        payload = payload_txt.encode("ascii", errors="replace")
        total_len = len(payload) + 2

        sigma_log(">> SEND:\n" + payload_txt.strip())
        ser.write(total_len.to_bytes(2, "big") + payload)
        ser.flush()

    def _read_frame(self, ser: serial.Serial, timeout: float = 5.0) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
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
            sigma_log("<< RECV:\n" + txt.strip())

            props: Dict[str, str] = {}
            for ln in txt.split("\r\n"):
                if "=" in ln:
                    k, v = ln.split("=", 1)
                    props[k] = v

            return props, txt

        return None, None

    def _toggle_lines(self, ser: serial.Serial) -> None:
        # Some CDC-ACM devices behave better after DTR/RTS toggles
        sigma_log("Toggling DTR/RTS")
        try:
            ser.dtr = False
            ser.rts = False
            time.sleep(0.2)
            ser.dtr = True
            ser.rts = True
            time.sleep(0.2)
        except Exception as e:
            sigma_log(f"DTR/RTS toggle error: {e!r}")

    # ------------------------
    # Public API
    # ------------------------
    def get_status(self, max_wait: float = 10.0) -> Optional[Dict[str, str]]:
        """
        Returns dict of status props or None.
        """
        sid = str(uuid.uuid4())
        lines = [
            "PROTOCOL=IPP",
            "METHOD=GET_STATUS",
            f"VERSION={self.version}",
            f"SID={sid}",
        ]

        sigma_log(f"GET_STATUS start sid={sid} port={self.port} baud={self.baud}")

        with serial.Serial(self.port, self.baud, timeout=self.timeout) as ser:
            self._toggle_lines(ser)
            self._send_frame(ser, lines)

            end = time.time() + float(max_wait)
            while time.time() < end:
                props, _ = self._read_frame(ser, timeout=3)
                if not props:
                    continue
                if props.get("METHOD") == "GET_STATUS" and props.get("SID") == sid:
                    sigma_log("GET_STATUS matched: " + str(props))
                    return props
                else:
                    sigma_log("GET_STATUS other frame: " + str(props))

        sigma_log("GET_STATUS timeout (no matching response)")
        return None

    def purchase(self, amount_minor, currency_num: str = "826", reference: str = "", max_wait: float = 180.0) -> Dict[str, Any]:
        """
        Blocking purchase.

        Tries two formats (like your proven test script):
          - minor units: "100"
          - decimal:     "1.00"

        Waits for the final PURCHASE update (commonly TIMEOUT==0).
        Returns:
          { approved: bool, status: str, stage: str, raw: dict, receipt: str, txid: str }
        """

        # Normalize to integer minor units
        try:
            minor_int = int(amount_minor)
        except Exception:
            minor_int = int(round(float(amount_minor) * 100))

        attempts = [
            str(minor_int),             # "100"
            f"{minor_int/100:.2f}",     # "1.00"
        ]

        sigma_log(
            f"PURCHASE start amount_minor={amount_minor} minor_int={minor_int} currency_num={currency_num} reference={reference} port={self.port}"
        )

        last_matching: Dict[str, str] = {}
        last_sid = ""

        for amt in attempts:
            sid = str(uuid.uuid4())
            last_sid = sid

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

            sigma_log(f"PURCHASE attempt amt={amt} sid={sid}")

            with serial.Serial(self.port, self.baud, timeout=self.timeout) as ser:
                self._toggle_lines(ser)
                self._send_frame(ser, lines)

                # First matching response
                first_resp: Optional[Dict[str, str]] = None
                end_first = time.time() + 25.0

                while time.time() < end_first:
                    props, _ = self._read_frame(ser, timeout=5.0)
                    if not props:
                        continue
                    if props.get("METHOD") == "PURCHASE" and props.get("SID") == sid:
                        first_resp = props
                        break

                if not first_resp:
                    sigma_log(f"PURCHASE no first response sid={sid}")
                    last_matching = {"PROTOCOL": "IPP", "METHOD": "PURCHASE", "SID": sid, "STATUS": "", "STAGE": ""}
                    continue

                last_matching = first_resp
                status_first = str(first_resp.get("STATUS") or "")
                stage_first = str(first_resp.get("STAGE") or "")
                timeout_first = str(first_resp.get("TIMEOUT") or "")
                sigma_log(f"PURCHASE first_resp status={status_first} stage={stage_first} timeout={timeout_first} resp={first_resp}")

                # If rejected immediately, try next format
                if status_first != "0":
                    sigma_log(f"PURCHASE rejected immediately (status={status_first}) for amt={amt}; trying next format")
                    continue

                # Accepted: keep reading for subsequent updates until final
                end = time.time() + float(max_wait)
                while time.time() < end:
                    props, _ = self._read_frame(ser, timeout=20.0)
                    if not props:
                        continue
                    if props.get("METHOD") != "PURCHASE" or props.get("SID") != sid:
                        continue

                    last_matching = props

                    # Many firmwares signal completion by TIMEOUT=0 (or missing)
                    t = str(props.get("TIMEOUT") or "")
                    if t in ("", "0"):
                        sigma_log(f"PURCHASE final detected (TIMEOUT={t}) props={props}")
                        break

                # Stop after accepted flow (don’t try next amt)
                break

        status = str(last_matching.get("STATUS") or "")
        stage = str(last_matching.get("STAGE") or "")

        # Approval heuristic:
        # - Prefer APPROVED / RESULT if firmware provides it
        # - Otherwise treat STATUS == "0" as success (Sigma firmwares vary)
        approved = False
        if "APPROVED" in last_matching:
            approved = str(last_matching.get("APPROVED")).lower() in ("1", "true", "yes")
        elif "RESULT" in last_matching:
            approved = str(last_matching.get("RESULT")).upper() in ("APPROVED", "00")
        else:
            approved = (status == "0")

        receipt = str(last_matching.get("RECEIPT") or "")
        txid = str(last_matching.get("TXID") or last_matching.get("RRN") or last_matching.get("AUTH") or "")

        sigma_log(
            f"PURCHASE done approved={approved} status={status} stage={stage} txid={txid} sid={last_sid} raw={last_matching}"
        )

        return {
            "approved": bool(approved),
            "status": status,
            "stage": stage,
            "raw": dict(last_matching or {}),
            "receipt": receipt,
            "txid": txid,
        }
