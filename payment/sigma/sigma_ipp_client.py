# sigma_ipp_client.py
import time
import uuid
import serial
import logging
from typing import Dict, Any, Optional, Tuple

# --- Dedicated Sigma serial log ---
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
      2-byte big-endian length (including the 2-byte header)
      + ASCII payload of "KEY=VALUE\\r\\n" lines.

    Ordering (important):
      PROTOCOL must be first
      METHOD must be second
    """

    def __init__(self, port: str = "/dev/sigma", baud: int = 115200, version: str = "202", timeout: float = 0.2):
        self.port = port
        self.baud = int(baud or 115200)
        self.version = str(version or "202")
        self.timeout = float(timeout or 0.2)

    # ------------------------
    # Low-level helpers
    # ------------------------
    def _send_frame(self, ser: serial.Serial, lines) -> None:
        payload_txt = "".join([ln + "\r\n" for ln in lines])
        payload = payload_txt.encode("ascii", errors="replace")
        total_len = len(payload) + 2

        sigma_log(">> SEND:\n" + payload_txt.strip())
        ser.write(total_len.to_bytes(2, "big") + payload)
        ser.flush()

    def _read_frame(self, ser: serial.Serial, timeout: float = 5.0) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
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
            sigma_log("<< RECV:\n" + txt.strip())

            props: Dict[str, str] = {}
            for ln in txt.split("\r\n"):
                if "=" in ln:
                    k, v = ln.split("=", 1)
                    props[k] = v

            return props, txt

        return None, None

    def _toggle_lines(self, ser: serial.Serial) -> None:
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

    def _drain_frames(self, ser: serial.Serial, seconds: float = 1.0, label: str = "drain") -> None:
        end = time.time() + float(seconds)
        while time.time() < end:
            props, _ = self._read_frame(ser, timeout=0.25)
            if props:
                sigma_log(f"<< ({label}): {props}")

    def _send_method(self, ser: serial.Serial, method: str, extra_lines=None) -> str:
        sid = str(uuid.uuid4())
        lines = [
            "PROTOCOL=IPP",
            f"METHOD={method}",
            f"VERSION={self.version}",
            f"SID={sid}",
        ]
        if extra_lines:
            lines.extend(extra_lines)
        sigma_log(f">> {method} SID={sid}")
        self._send_frame(ser, lines)
        return sid

    # ------------------------
    # GET_STATUS (final frame)
    # ------------------------
    def get_status_final(self, ser: serial.Serial, max_wait: float = 6.0, verbose_log: bool = False) -> Optional[Dict[str, str]]:
        """
        GET_STATUS can return multiple frames for the same SID.
        Treat the FINAL frame as TIMEOUT==0 (or missing), otherwise return last seen.
        """
        sid = str(uuid.uuid4())
        lines = [
            "PROTOCOL=IPP",
            "METHOD=GET_STATUS",
            f"VERSION={self.version}",
            f"SID={sid}",
        ]

        self._drain_frames(ser, seconds=0.4, label="pre-status")

        sigma_log(f">> GET_STATUS SID={sid}")
        self._send_frame(ser, lines)

        end = time.time() + float(max_wait)
        last = None
        while time.time() < end:
            props, _ = self._read_frame(ser, timeout=0.8)
            if not props:
                continue

            if verbose_log and not (props.get("METHOD") == "GET_STATUS" and props.get("SID") == sid):
                sigma_log("<< (other): " + str(props))

            if props.get("METHOD") == "GET_STATUS" and props.get("SID") == sid:
                last = props
                sigma_log("<< GET_STATUS(frame): " + str(props))

                t = str(props.get("TIMEOUT") or "")
                if t in ("", "0"):
                    break

        if last:
            sigma_log("<< GET_STATUS(final): " + str(last))
        return last

    def get_status(self, max_wait: float = 10.0) -> Optional[Dict[str, str]]:
        """
        Public: open port, return FINAL GET_STATUS frame.
        """
        sigma_log(f"GET_STATUS start port={self.port} baud={self.baud}")
        with serial.Serial(self.port, self.baud, timeout=self.timeout) as ser:
            self._toggle_lines(ser)
            return self.get_status_final(ser, max_wait=max_wait, verbose_log=True)

    def _is_idle(self, st_final: Optional[Dict[str, str]]) -> bool:
        if not st_final:
            return False
        return str(st_final.get("STATUS") or "") == "0"

    # ------------------------
    # Clear STATUS=20 ("NOT COMPLETED LAST TX")
    # ------------------------
    def clear_to_idle(self, ser: serial.Serial, max_wait: float = 25.0) -> bool:
        """
        If GET_STATUS(final).STATUS == 20, PURCHASE will reject until cleared.

        Strategy:
          1) COMPLETE_TX
          2) If still not idle, CANCEL_TX
          3) Poll GET_STATUS(final) until idle or timeout
        """
        st = self.get_status_final(ser, max_wait=6.0, verbose_log=True)
        if not st:
            sigma_log("clear_to_idle: GET_STATUS no response")
            return False

        if self._is_idle(st):
            sigma_log("clear_to_idle: already idle")
            return True

        status_code = str(st.get("STATUS") or "")
        sigma_log(f"clear_to_idle: not idle STATUS={status_code}; attempting recovery")

        # COMPLETE_TX
        self._send_method(ser, "COMPLETE_TX")
        self._drain_frames(ser, seconds=1.5, label="after-COMPLETE_TX")

        st = self.get_status_final(ser, max_wait=6.0, verbose_log=True)
        if st and self._is_idle(st):
            sigma_log("clear_to_idle: idle after COMPLETE_TX")
            return True

        # CANCEL_TX
        self._send_method(ser, "CANCEL_TX")
        self._drain_frames(ser, seconds=1.5, label="after-CANCEL_TX")

        end = time.time() + float(max_wait)
        while time.time() < end:
            st = self.get_status_final(ser, max_wait=6.0, verbose_log=False)
            if st:
                sigma_log("clear_to_idle poll: " + str(st))
            if st and self._is_idle(st):
                sigma_log("clear_to_idle: idle after CANCEL_TX")
                return True
            time.sleep(1.0)

        sigma_log("clear_to_idle: failed to reach idle before timeout")
        return False

    # ------------------------
    # PURCHASE (wait final frame)
    # ------------------------
    def _purchase_one(self, ser: serial.Serial, amount_str: str, currency_num: str, reference: str, first_wait: float, final_wait: float) -> Dict[str, str]:
        sid = str(uuid.uuid4())
        lines = [
            "PROTOCOL=IPP",
            "METHOD=PURCHASE",
            f"VERSION={self.version}",
            f"SID={sid}",
            f"AMOUNT={amount_str}",
            f"CURRENCY={currency_num}",
        ]
        if reference:
            lines.append(f"REFERENCE={reference}")

        self._drain_frames(ser, seconds=0.4, label="pre-purchase")

        sigma_log(f"PURCHASE >> amt={amount_str} currency={currency_num} ref={reference} sid={sid}")
        self._send_frame(ser, lines)

        # First matching frame
        first = None
        end_first = time.time() + float(first_wait)
        while time.time() < end_first:
            props, _ = self._read_frame(ser, timeout=1.0)
            if not props:
                continue
            if props.get("METHOD") == "PURCHASE" and props.get("SID") == sid:
                first = props
                break

        if not first:
            sigma_log(f"PURCHASE no first response sid={sid}")
            return {"PROTOCOL": "IPP", "METHOD": "PURCHASE", "SID": sid, "STATUS": "", "STAGE": ""}

        status_first = str(first.get("STATUS") or "")
        sigma_log(f"PURCHASE first_resp status={status_first} stage={first.get('STAGE')} timeout={first.get('TIMEOUT')} resp={first}")

        # If rejected immediately, return first
        if status_first != "0":
            return first

        # Accepted: keep reading until final for this SID
        last = first
        end = time.time() + float(final_wait)
        while time.time() < end:
            props, _ = self._read_frame(ser, timeout=5.0)
            if not props:
                continue
            if props.get("METHOD") == "PURCHASE" and props.get("SID") == sid:
                last = props
                t = str(props.get("TIMEOUT") or "")
                if t in ("", "0"):
                    sigma_log(f"PURCHASE final detected TIMEOUT={t} props={props}")
                    break

        return last

    def purchase(
        self,
        amount_minor,
        currency_num: str = "826",
        reference: str = "",
        max_wait: float = 180.0,
        auto_clear_not_completed_last_tx: bool = True,
    ) -> Dict[str, Any]:
        """
        Blocking purchase with correct preconditions.

        Key behaviour learned from Sigma IPP:
          - If GET_STATUS(final).STATUS == 20 ("NOT COMPLETED LAST TX"),
            PURCHASE will reject until you COMPLETE_TX (preferred) or CANCEL_TX.
          - PURCHASE returns multiple frames; final typically TIMEOUT==0.

        Attempts:
          1) decimal (e.g. "1.00")
          2) minor units (e.g. "100")
        (Your terminal accepted both formats once idle; keeping both to be safe.)
        """

        # Normalize to integer minor units
        try:
            minor_int = int(amount_minor)
        except Exception:
            minor_int = int(round(float(amount_minor) * 100))

        attempts = [
            f"{minor_int/100:.2f}",   # "1.00"
            str(minor_int),          # "100"
        ]

        sigma_log(
            f"PURCHASE start amount_minor={amount_minor} minor_int={minor_int} currency_num={currency_num} reference={reference} port={self.port}"
        )

        last_matching: Dict[str, str] = {}

        with serial.Serial(self.port, self.baud, timeout=self.timeout) as ser:
            self._toggle_lines(ser)

            # Optional: clear STATUS=20 before attempting purchase
            if auto_clear_not_completed_last_tx:
                st = self.get_status_final(ser, max_wait=6.0, verbose_log=True)
                if st and str(st.get("STATUS") or "") == "20":
                    sigma_log("PURCHASE precheck: STATUS=20 (NOT COMPLETED LAST TX) -> clearing to idle")
                    self.clear_to_idle(ser, max_wait=25.0)

            # Ensure we're idle
            st = self.get_status_final(ser, max_wait=6.0, verbose_log=True)
            if not st:
                return {"approved": False, "status": "", "stage": "", "raw": {}, "receipt": "", "txid": ""}

            if not self._is_idle(st):
                # If not idle, attempt recovery (covers STATUS=20 and other non-idle conditions)
                sigma_log(f"PURCHASE precheck: terminal not idle STATUS={st.get('STATUS')} -> attempting clear_to_idle")
                ok = self.clear_to_idle(ser, max_wait=25.0)
                if not ok:
                    # Return the final status frame so caller can see why it refused
                    return {
                        "approved": False,
                        "status": str(st.get("STATUS") or ""),
                        "stage": str(st.get("STAGE") or ""),
                        "raw": dict(st),
                        "receipt": "",
                        "txid": "",
                    }

            # Run attempts
            for amt in attempts:
                resp = self._purchase_one(
                    ser,
                    amount_str=amt,
                    currency_num=currency_num,
                    reference=reference,
                    first_wait=25.0,
                    final_wait=float(max_wait),
                )
                last_matching = resp
                # If accepted initially, we'll have reached a final frame (TIMEOUT==0)
                # Regardless, stop after the first attempt that didn't immediately reject by formatting.
                # If it rejected immediately with a format issue, try next format.
                if str(resp.get("STATUS") or "") == "0":
                    break
                # If immediate reject on stage 1 etc, try the next format
                # (If user cancels/declines you'll still see non-zero; we still stop after first decimal attempt
                # only if it progressed beyond "format" - but safest is to try both always.)
                # We'll try both formats unless the first was accepted (STATUS==0).
                continue

        status = str(last_matching.get("STATUS") or "")
        stage = str(last_matching.get("STAGE") or "")

        # Approval:
        # - Prefer APPROVAL / TX_STATUS fields if present at final stage
        # - Otherwise STATUS == "0" means flow accepted/ok
        approved = False
        if "APPROVAL" in last_matching:
            # myPOS often uses APPROVAL=00 for approved
            approved = str(last_matching.get("APPROVAL") or "").strip() in ("00", "0")
        elif "TX_STATUS" in last_matching:
            approved = str(last_matching.get("TX_STATUS") or "").strip() in ("0", "00")
        elif "APPROVED" in last_matching:
            approved = str(last_matching.get("APPROVED")).lower() in ("1", "true", "yes")
        elif "RESULT" in last_matching:
            approved = str(last_matching.get("RESULT")).upper() in ("APPROVED", "00")
        else:
            approved = (status == "0")

        receipt = str(last_matching.get("RECEIPT") or "")
        txid = str(
            last_matching.get("RRN")
            or last_matching.get("AUTH_CODE")
            or last_matching.get("AUTH")
            or last_matching.get("TXID")
            or ""
        )

        sigma_log(f"PURCHASE done approved={approved} status={status} stage={stage} txid={txid} raw={last_matching}")

        return {
            "approved": bool(approved),
            "status": status,
            "stage": stage,
            "raw": dict(last_matching or {}),
            "receipt": receipt,
            "txid": txid,
        }
