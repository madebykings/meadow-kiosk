# sigma_ipp_client.py
#
# Robust IPP-over-serial client for myPOS Sigma (USB CDC ACM /dev/ttyACM0, symlink /dev/sigma)
# - Reads/returns the *final* frame for GET_STATUS / PURCHASE (Sigma often streams multiple frames per SID)
# - Auto-recovers when terminal is "not idle" due to an unfinished previous TX (common STATUS=20 case)
#   using COMPLETE_TX then CANCEL_TX (and optional CANCEL as a fallback)
# - Uses MINOR UNITS by default (e.g. 100 == £1.00) because your whole stack uses amount_minor
# - Logs full raw frames to /home/meadow/meadow-kiosk/sigma_serial.log
#
# Notes:
# - Exact status/stage semantics are from myPOS IPP docs you linked (stages/status/transaction status).
# - Treat "idle" as: final GET_STATUS frame has STATUS == "0"
# - Treat "purchase approved" as: final PURCHASE frame has STATUS == "0"
#   (optionally, if APPROVAL/APPROVED/RESULT exists, we can tighten that later)

import time
import uuid
import serial
import logging
from typing import Dict, Any, Optional, Tuple, List

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
    Ordering:
      PROTOCOL must be first, METHOD must be second.
    """

    def __init__(self, port: str = "/dev/sigma", baud: int = 115200, version: str = "202", timeout: float = 0.2):
        self.port = port
        self.baud = int(baud or 115200)
        self.version = str(version or "202")
        self.timeout = float(timeout or 0.2)

    # ------------------------
    # Low-level helpers
    # ------------------------
    def _send_frame(self, ser: serial.Serial, lines: List[str]) -> None:
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

    def _drain(self, ser: serial.Serial, seconds: float = 0.8, label: str = "drain") -> None:
        """Drain any unsolicited/in-flight frames for a short window."""
        end = time.time() + float(seconds)
        while time.time() < end:
            props, _ = self._read_frame(ser, timeout=0.25)
            if props:
                sigma_log(f"<< ({label}): {props}")

    def _send_method(self, ser: serial.Serial, method: str, extra: Optional[Dict[str, str]] = None) -> str:
        """Send a method with a fresh SID. Returns SID used."""
        sid = str(uuid.uuid4())
        lines = [
            "PROTOCOL=IPP",
            f"METHOD={method}",
            f"VERSION={self.version}",
            f"SID={sid}",
        ]
        if extra:
            for k, v in extra.items():
                lines.append(f"{k}={v}")
        self._send_frame(ser, lines)
        return sid

    def _read_final_for_sid(
        self,
        ser: serial.Serial,
        method: str,
        sid: str,
        max_wait: float,
        log_nonmatching: bool = True,
    ) -> Tuple[Optional[Dict[str, str]], List[Dict[str, str]]]:
        """
        Many Sigma methods stream multiple frames for the same SID.
        We consider a frame "final" when TIMEOUT is "0" or missing.
        Returns (final_props, all_matching_frames).
        """
        end = time.time() + float(max_wait)
        matching: List[Dict[str, str]] = []
        last: Optional[Dict[str, str]] = None

        while time.time() < end:
            props, _ = self._read_frame(ser, timeout=0.8)
            if not props:
                continue

            if props.get("METHOD") == method and props.get("SID") == sid:
                matching.append(props)
                last = props
                t = str(props.get("TIMEOUT") or "")
                if t in ("", "0"):
                    return last, matching
            else:
                if log_nonmatching:
                    sigma_log(f"<< (other): {props}")

        return last, matching

    def _is_idle_final_status(self, st_final: Optional[Dict[str, str]]) -> bool:
        return bool(st_final) and str(st_final.get("STATUS") or "") == "0"

    # ------------------------
    # GET_STATUS (final frame)
    # ------------------------
    def get_status_final(self, max_wait: float = 6.0) -> Optional[Dict[str, str]]:
        """
        Returns the *final* GET_STATUS frame for the request (TIMEOUT==0/missing).
        """
        sigma_log(f"GET_STATUS(final) start port={self.port} baud={self.baud}")

        with serial.Serial(self.port, self.baud, timeout=self.timeout) as ser:
            self._toggle_lines(ser)
            self._drain(ser, seconds=0.4, label="pre-status")

            sid = self._send_method(ser, "GET_STATUS")
            sigma_log(f"GET_STATUS SID={sid}")

            final, frames = self._read_final_for_sid(ser, "GET_STATUS", sid, max_wait=max_wait, log_nonmatching=True)
            sigma_log(f"GET_STATUS frames={len(frames)} final={final}")
            return final

    # ------------------------
    # Recovery to IDLE
    # ------------------------
    def ensure_idle(self, max_wait: float = 25.0) -> Dict[str, Any]:
        """
        Ensures terminal is idle before starting a new PURCHASE.

        Strategy (based on your observed behavior + IPP docs):
          1) GET_STATUS(final). If STATUS==0 -> idle.
          2) If STATUS==20 (or other non-idle), attempt:
             - COMPLETE_TX (lets terminal "finalise" the previous TX record)
             - CANCEL_TX (clears terminal to idle)
          3) Fallback: CANCEL (generic)
          4) Poll GET_STATUS(final) until STATUS==0 or timeout.

        Returns:
          { ok: bool, status_final: dict|None, tried: [methods...], note: str }
        """
        tried: List[str] = []
        note = ""

        with serial.Serial(self.port, self.baud, timeout=self.timeout) as ser:
            self._toggle_lines(ser)
            self._drain(ser, seconds=0.6, label="pre-ensure-idle")

            end = time.time() + float(max_wait)

            def poll_status() -> Optional[Dict[str, str]]:
                sid = self._send_method(ser, "GET_STATUS")
                final, _frames = self._read_final_for_sid(ser, "GET_STATUS", sid, max_wait=6.0, log_nonmatching=False)
                sigma_log(f"GET_STATUS(final) polled => {final}")
                return final

            st = poll_status()
            if self._is_idle_final_status(st):
                return {"ok": True, "status_final": st, "tried": tried, "note": "already_idle"}

            # Non-idle; attempt recovery sequence (COMPLETE_TX -> CANCEL_TX -> CANCEL)
            # COMPLETE_TX
            tried.append("COMPLETE_TX")
            sid_ct = self._send_method(ser, "COMPLETE_TX")
            sigma_log(f"COMPLETE_TX SID={sid_ct}")
            final_ct, _ = self._read_final_for_sid(ser, "COMPLETE_TX", sid_ct, max_wait=8.0, log_nonmatching=True)
            sigma_log(f"COMPLETE_TX final={final_ct}")
            self._drain(ser, seconds=0.6, label="post-complete_tx")

            # CANCEL_TX
            tried.append("CANCEL_TX")
            sid_cx = self._send_method(ser, "CANCEL_TX")
            sigma_log(f"CANCEL_TX SID={sid_cx}")
            final_cx, _ = self._read_final_for_sid(ser, "CANCEL_TX", sid_cx, max_wait=8.0, log_nonmatching=True)
            sigma_log(f"CANCEL_TX final={final_cx}")
            self._drain(ser, seconds=0.6, label="post-cancel_tx")

            # Poll for idle
            while time.time() < end:
                st = poll_status()
                if self._is_idle_final_status(st):
                    return {"ok": True, "status_final": st, "tried": tried, "note": "recovered_via_complete_cancel_tx"}
                time.sleep(0.8)

            # Fallback CANCEL (generic)
            tried.append("CANCEL")
            sid_can = self._send_method(ser, "CANCEL")
            sigma_log(f"CANCEL SID={sid_can}")
            final_can, _ = self._read_final_for_sid(ser, "CANCEL", sid_can, max_wait=6.0, log_nonmatching=True)
            sigma_log(f"CANCEL final={final_can}")
            self._drain(ser, seconds=0.6, label="post-cancel")

            # Final poll window
            end2 = time.time() + 10.0
            while time.time() < end2:
                st = poll_status()
                if self._is_idle_final_status(st):
                    return {"ok": True, "status_final": st, "tried": tried, "note": "recovered_via_cancel_fallback"}
                time.sleep(0.8)

            note = "failed_to_reach_idle"
            return {"ok": False, "status_final": st, "tried": tried, "note": note}

    # ------------------------
    # PURCHASE (final frame)
    # ------------------------
    def purchase(
        self,
        amount_minor: int,
        currency_num: str = "826",
        reference: str = "",
        max_wait: float = 180.0,
        auto_recover_idle: bool = True,
    ) -> Dict[str, Any]:
        """
        Blocking PURCHASE using MINOR UNITS (your desired flow).

        Behaviour:
          - Optionally ensure terminal idle first (auto_recover_idle=True)
          - Sends PURCHASE with AMOUNT=<minor units> (e.g. 100)
          - Waits for final PURCHASE frame (TIMEOUT==0/missing) and returns it

        Returns:
          {
            ok: bool,
            approved: bool,
            status: str,
            stage: str,
            raw: dict,
            receipt: str,
            txid: str,
            idle_recovery: {...} (if attempted)
          }
        """
        # Normalize to int
        try:
            minor_int = int(amount_minor)
        except Exception:
            minor_int = int(round(float(amount_minor) * 100))

        sigma_log(
            f"PURCHASE start amount_minor={amount_minor} minor_int={minor_int} currency_num={currency_num} reference={reference} port={self.port}"
        )

        idle_recovery = None
        if auto_recover_idle:
            idle_recovery = self.ensure_idle(max_wait=25.0)
            sigma_log(f"ensure_idle => {idle_recovery}")
            if not idle_recovery.get("ok"):
                # If we can't get idle, PURCHASE will likely reject instantly (as you saw).
                st = idle_recovery.get("status_final") or {}
                return {
                    "ok": False,
                    "approved": False,
                    "status": str(st.get("STATUS") or ""),
                    "stage": str(st.get("STAGE") or ""),
                    "raw": dict(st),
                    "receipt": "",
                    "txid": "",
                    "idle_recovery": idle_recovery,
                    "error": "not_idle",
                }

        with serial.Serial(self.port, self.baud, timeout=self.timeout) as ser:
            self._toggle_lines(ser)
            self._drain(ser, seconds=0.6, label="pre-purchase")

            sid = str(uuid.uuid4())
            lines = [
                "PROTOCOL=IPP",
                "METHOD=PURCHASE",
                f"VERSION={self.version}",
                f"SID={sid}",
                f"AMOUNT={minor_int}",
                f"CURRENCY={currency_num}",
            ]
            if reference:
                lines.append(f"REFERENCE={reference}")

            self._send_frame(ser, lines)
            sigma_log(f"PURCHASE sent sid={sid} amount={minor_int} currency={currency_num} reference={reference}")

            # Wait for final purchase frame for this SID
            final, frames = self._read_final_for_sid(ser, "PURCHASE", sid, max_wait=max_wait, log_nonmatching=True)
            sigma_log(f"PURCHASE frames={len(frames)} final={final}")

            if not final:
                return {
                    "ok": False,
                    "approved": False,
                    "status": "",
                    "stage": "",
                    "raw": {},
                    "receipt": "",
                    "txid": "",
                    "idle_recovery": idle_recovery,
                    "error": "timeout_no_final_frame",
                }

            status = str(final.get("STATUS") or "")
            stage = str(final.get("STAGE") or "")

            # Primary truth: STATUS==0 => success (approved)
            approved = (status == "0")

            # If firmware provides any of these, we can optionally refine (kept conservative here)
            # APPROVAL often appears as '00' for approved, otherwise not approved.
            if "APPROVAL" in final:
                approved = str(final.get("APPROVAL") or "").strip() == "00"

            # E-receipt properties may appear on some firmwares; keep whatever is present
            receipt = str(final.get("RECEIPT") or "")

            # Best tx id we can extract from fields that tend to appear:
            txid = (
                str(final.get("RRN") or "")
                or str(final.get("AUTH_CODE") or "")
                or str(final.get("TXID") or "")
                or ""
            )

            return {
                "ok": True,
                "approved": bool(approved),
                "status": status,
                "stage": stage,
                "raw": dict(final),
                "receipt": receipt,
                "txid": txid,
                "idle_recovery": idle_recovery,
            }
