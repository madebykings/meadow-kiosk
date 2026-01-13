#!/usr/bin/env python3
"""
sigma_ipp_client.py — myPOS Sigma IPP-over-serial (USB CDC-ACM) production client

Wire format (CONFIRMED by your working sigma_purchase.py):
- 2-byte big-endian total length (INCLUDING these 2 bytes)
- ASCII lines of "KEY=VALUE\r\n"
- PROTOCOL must be first, METHOD must be second
- Correlate responses by SID
- Final frame is where TIMEOUT is missing/empty or "0"

Lifecycle (CONFIRMED by your debugging):
- If GET_STATUS(final).STATUS == 20 => recover:
    COMPLETE_TX -> CANCEL_TX -> poll until STATUS == 0
  (REVERSAL included as last resort because your tester used it; some firmware needs it)
- PURCHASE: wait for first matching frame; if STATUS != 0 return it; else continue until final frame.

Important robustness fix:
- Some CDC-ACM devices / drivers reject DTR/RTS ioctls during open().
  PySerial can throw BrokenPipeError inside _update_dtr_state().
  We open with dsrdtr=False and rtscts=False and we NEVER fail if DTR/RTS toggles fail.

Public API:
- SigmaIppClient.purchase(amount_minor:int, currency_num:str="826", reference:str="")
- SigmaIppClient.get_status_final()
- SigmaIppClient.ensure_idle()

Compatibility:
- Provides SigmaIPP class with the signature your pi_api.py previously used.
"""

from __future__ import annotations

import os
import time
import uuid
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, List

import serial

# -----------------------------
# Defaults / logging
# -----------------------------

DEFAULT_PORT = os.getenv("SIGMA_PORT", "/dev/sigma")
DEFAULT_BAUD = int(os.getenv("SIGMA_BAUD", "115200"))
DEFAULT_VERSION = str(os.getenv("SIGMA_VERSION", "202"))

SIGMA_LOG_PATH = "/home/meadow/meadow-kiosk/sigma_serial.log"


def _default_sigma_logger() -> logging.Logger:
    logger = logging.getLogger("sigma_serial")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        os.makedirs(os.path.dirname(SIGMA_LOG_PATH), exist_ok=True)
        fh = logging.FileHandler(SIGMA_LOG_PATH)
        fh.setLevel(logging.DEBUG)
        fmt = logging.Formatter("%(asctime)s [SIGMA_SERIAL] %(message)s")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


class SigmaError(Exception):
    pass


class SigmaTimeout(SigmaError):
    pass


@dataclass
class SigmaFrame:
    props: Dict[str, str]
    raw_text: str
    sid: str
    method: str


# -----------------------------
# Framing helpers (length-prefixed IPP)
# -----------------------------

def _build_payload_lines(lines: List[str]) -> bytes:
    return ("".join([ln + "\r\n" for ln in lines])).encode("ascii", errors="strict")


def _write_frame(ser: serial.Serial, payload: bytes) -> None:
    total_len = len(payload) + 2
    ser.write(total_len.to_bytes(2, "big") + payload)
    ser.flush()


def _read_one_frame(ser: serial.Serial, timeout_s: float) -> Optional[Tuple[Dict[str, str], str]]:
    """
    Read a single IPP frame within timeout_s.
    Returns (props, raw_text) or None.
    """
    end = time.time() + float(timeout_s)
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
        props: Dict[str, str] = {}
        for ln in txt.split("\r\n"):
            if "=" in ln:
                k, v = ln.split("=", 1)
                props[k] = v

        return props, txt

    return None


def _timeout_is_final(props: Dict[str, str]) -> bool:
    t = str(props.get("TIMEOUT") or "")
    return t in ("", "0")


def _toggle_lines_safe(ser: serial.Serial) -> None:
    """
    Some CDC-ACM devices respond more reliably after DTR/RTS toggles.
    But some drivers/devices reject these ioctls, so this must never raise.
    """
    try:
        ser.dtr = False
        ser.rts = False
        time.sleep(0.2)
        ser.dtr = True
        ser.rts = True
        time.sleep(0.2)
    except Exception:
        return


# -----------------------------
# Client
# -----------------------------

class SigmaIppClient:
    def __init__(
        self,
        port: str = DEFAULT_PORT,
        baudrate: int = DEFAULT_BAUD,
        version: str = DEFAULT_VERSION,
        read_timeout: float = 0.2,
        sigma_logger: Optional[logging.Logger] = None,
    ):
        self.port = port
        self.baudrate = int(baudrate)
        self.version = str(version)
        self.read_timeout = float(read_timeout)
        self.log = sigma_logger or _default_sigma_logger()
        self._ser: Optional[serial.Serial] = None

    # ---- lifecycle ----

    def open(self) -> None:
    if self._ser and self._ser.is_open:
        return

    # Create Serial object WITHOUT opening the port
    self._ser = serial.Serial(
        port=None,
        baudrate=self.baudrate,
        timeout=self.read_timeout,
        rtscts=False,
        dsrdtr=False,
        do_not_open=True,
    )

    # Assign port and open explicitly
    self._ser.port = self.port
    try:
        self._ser.open()
    except BrokenPipeError:
        # Some CDC-ACM drivers throw here even though the fd is valid
        # Continue anyway
        pass

    # Best-effort line toggle — NEVER fatal
    try:
        _toggle_lines_safe(self._ser)
    except Exception:
        pass

    try:
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()
    except Exception:
        pass

    self.log.debug(f"OPEN port={self.port} baud={self.baudrate} version={self.version}")


    def close(self) -> None:
        if self._ser:
            try:
                self._ser.close()
            finally:
                self._ser = None
        self.log.debug("CLOSE")

    def __enter__(self) -> "SigmaIppClient":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---- IO helpers ----

    def _send_method(self, method: str, extra_lines: Optional[List[str]] = None) -> str:
        if not self._ser or not self._ser.is_open:
            self.open()
        assert self._ser is not None

        sid = str(uuid.uuid4())
        lines = [
            "PROTOCOL=IPP",
            f"METHOD={method}",
            f"VERSION={self.version}",
            f"SID={sid}",
        ]
        if extra_lines:
            lines.extend(extra_lines)

        payload = _build_payload_lines(lines)
        self.log.debug(f"TX method={method} sid={sid} lines={lines}")
        _write_frame(self._ser, payload)
        return sid

    def _drain(self, seconds: float = 1.0, label: str = "drain") -> None:
        if not self._ser or not self._ser.is_open:
            self.open()
        assert self._ser is not None

        end = time.time() + float(seconds)
        while time.time() < end:
            got = _read_one_frame(self._ser, timeout_s=0.25)
            if not got:
                continue
            props, raw = got
            self.log.debug(f"RX ({label}) {props} RAW={raw!r}")

    def _wait_for_sid(
        self,
        method: str,
        sid: str,
        first_wait: float,
        final_wait: float,
        log_other: bool = True,
    ) -> Optional[SigmaFrame]:
        """
        Wait for frames matching METHOD+SID.
        - waits for first matching frame up to first_wait
        - if STATUS != 0 returns immediately
        - else continues until final frame (TIMEOUT missing/0) or final_wait
        """
        if not self._ser or not self._ser.is_open:
            self.open()
        assert self._ser is not None

        # First matching response
        end_first = time.time() + float(first_wait)
        first: Optional[Dict[str, str]] = None
        first_raw: str = ""
        while time.time() < end_first:
            got = _read_one_frame(self._ser, timeout_s=1.0)
            if not got:
                continue
            props, raw = got
            if log_other and not (props.get("METHOD") == method and props.get("SID") == sid):
                self.log.debug(f"RX(other) {props} RAW={raw!r}")

            if props.get("METHOD") == method and props.get("SID") == sid:
                first = props
                first_raw = raw
                self.log.debug(f"RX({method} first) {props} RAW={raw!r}")
                break

        if not first:
            return None

        if str(first.get("STATUS") or "") != "0":
            return SigmaFrame(props=first, raw_text=first_raw, sid=sid, method=method)

        # Continue until final frame
        last = first
        last_raw = first_raw
        end = time.time() + float(final_wait)
        while time.time() < end:
            got = _read_one_frame(self._ser, timeout_s=5.0)
            if not got:
                continue
            props, raw = got
            if log_other and not (props.get("METHOD") == method and props.get("SID") == sid):
                self.log.debug(f"RX(other) {props} RAW={raw!r}")

            if props.get("METHOD") == method and props.get("SID") == sid:
                last = props
                last_raw = raw
                self.log.debug(f"RX({method}) {props} RAW={raw!r}")
                if _timeout_is_final(props):
                    break

        return SigmaFrame(props=last, raw_text=last_raw, sid=sid, method=method)

    # -----------------------------
    # Status / idle handling
    # -----------------------------

    def get_status_final(self, max_wait: float = 6.0) -> Optional[Dict[str, str]]:
        sid = self._send_method("GET_STATUS")
        frame = self._wait_for_sid("GET_STATUS", sid, first_wait=max_wait, final_wait=max_wait, log_other=True)
        return frame.props if frame else None

    @staticmethod
    def _is_idle(st: Optional[Dict[str, str]]) -> bool:
        return bool(st) and str(st.get("STATUS") or "") == "0"

    def ensure_idle(self, max_total_wait: float = 45.0) -> bool:
        """
        Bring terminal to IDLE (STATUS=0). If STATUS=20, run recovery sequence:
          COMPLETE_TX -> CANCEL_TX -> (optional) REVERSAL
        Always polls GET_STATUS(final) after each step.
        """
        deadline = time.time() + float(max_total_wait)

        # drain helps remove stale chatter
        self._drain(seconds=1.0, label="pre-ensure-idle")

        st = self.get_status_final(max_wait=6.0)
        if not st:
            self.log.debug("No GET_STATUS response")
            return False

        if self._is_idle(st):
            return True

        while time.time() < deadline:
            code = str(st.get("STATUS") or "")
            self.log.debug(f"Not idle. STATUS={code}")

            if code == "20":
                # 1) COMPLETE_TX
                self._send_method("COMPLETE_TX")
                self._drain(seconds=2.0, label="after-COMPLETE_TX")
                time.sleep(1.0)
                st = self.get_status_final(max_wait=6.0)
                if self._is_idle(st):
                    return True

                # 2) CANCEL_TX
                self._send_method("CANCEL_TX")
                self._drain(seconds=2.0, label="after-CANCEL_TX")
                time.sleep(1.0)
                st = self.get_status_final(max_wait=6.0)
                if self._is_idle(st):
                    return True

                # 3) Optional REVERSAL (some firmware needs it; your tester did it)
                self._send_method("REVERSAL")
                self._drain(seconds=3.0, label="after-REVERSAL")
                time.sleep(1.0)
                st = self.get_status_final(max_wait=6.0)
                if self._is_idle(st):
                    return True

            else:
                # Unknown non-idle code: poll a bit
                time.sleep(1.0)
                st = self.get_status_final(max_wait=6.0)
                if self._is_idle(st):
                    return True

        return False

    # -----------------------------
    # Purchase
    # -----------------------------

    def purchase(
        self,
        amount_minor: int,
        currency_num: str = "826",
        reference: str = "",
        first_wait: float = 25.0,
        final_wait: float = 180.0,
    ) -> Dict[str, Any]:
        """
        PURCHASE using amount in minor units (e.g. 100 for £1.00).
        Returns dict compatible with your existing API expectations:
          approved(bool), status(str), stage(str), timeout(str), raw(dict), sid(str)
        """
        if not self._ser or not self._ser.is_open:
            self.open()

        # Drain chatter and ensure idle
        self._drain(seconds=2.0, label="pre-purchase-drain")
        if not self.ensure_idle(max_total_wait=45.0):
            raise SigmaTimeout("Terminal not idle before purchase")

        amt_str = str(int(amount_minor))  # your successful test used "100" style
        extra = [
            f"AMOUNT={amt_str}",
            f"CURRENCY={str(currency_num)}",
        ]
        if reference:
            extra.append(f"REFERENCE={reference[:64]}")

        sid = self._send_method("PURCHASE", extra_lines=extra)

        frame = self._wait_for_sid("PURCHASE", sid, first_wait=first_wait, final_wait=final_wait, log_other=True)
        if not frame:
            raise SigmaTimeout("No PURCHASE response within timeout")

        props = frame.props
        status = str(props.get("STATUS") or "")
        stage = str(props.get("STAGE") or "")
        timeout = str(props.get("TIMEOUT") or "")

        approved = (status == "0")

        return {
            "approved": approved,
            "status": status,
            "stage": stage,
            "timeout": timeout,
            "raw": props,
            "sid": sid,
        }


# -----------------------------
# Compatibility wrapper (optional)
# -----------------------------

class SigmaIPP:
    """
    Backwards compatible class for existing imports:
      SigmaIPP(port=..., baud=...)
      .purchase(amount_minor=..., currency_num=..., reference=...)
    """

    def __init__(self, port: str = DEFAULT_PORT, baud: int = DEFAULT_BAUD, version: str = DEFAULT_VERSION):
        self._client = SigmaIppClient(port=port, baudrate=baud, version=version)

    def purchase(self, amount_minor: int, currency_num: str = "826", reference: str = "") -> Dict[str, Any]:
        with self._client as c:
            return c.purchase(amount_minor=amount_minor, currency_num=currency_num, reference=reference)
