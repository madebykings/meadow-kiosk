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

Important fix:
- Sigma can still report STATUS=20 *after* a successful purchase. So we run post-purchase
  ensure_idle() best-effort to clean the terminal for the NEXT customer.

Enhancement (UI support):
- purchase(..., on_phase=...) emits on_phase("finalising", props) once when Sigma first
  indicates approval (STATUS=0) but the response is not yet final (TIMEOUT not 0/missing).
  This lines up with the Sigma "Auth ✅" tick and lets your UI show a "Processing..." state.

PySerial robustness:
- Some builds/devices throw BrokenPipeError inside serialposix._update_dtr_state() while opening
- Some pyserial builds do NOT support do_not_open=True
- We open by constructing Serial(port=None, ...) then setting .port and calling .open()
  with a temporary patch around _update_dtr_state() to ignore errno 32.

Public API:
- SigmaIppClient.purchase(amount_minor:int, currency_num:str="826", reference:str="", on_phase=callable)
- SigmaIppClient.get_status_final()
- SigmaIppClient.ensure_idle()

Compatibility:
- Provides SigmaIPP class with the signature your pi_api.py can use if desired.
"""

from __future__ import annotations

import os
import time
import uuid
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, List, Callable

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


def _open_serial_safely(port: str, baudrate: int, timeout: float) -> serial.Serial:
    """
    Open Serial without relying on do_not_open=True (not available in some pyserial builds),
    and avoid fatal BrokenPipeError from DTR ioctl by temporarily patching _update_dtr_state.
    """
    ser = serial.Serial(
        port=None,
        baudrate=baudrate,
        timeout=timeout,
        rtscts=False,
        dsrdtr=False,
    )
    ser.port = port

    orig = None
    patched = False
    try:
        try:
            import serial.serialposix as sp  # type: ignore
            orig = getattr(sp.Serial, "_update_dtr_state", None)
            if orig:
                def _safe_update_dtr_state(self_):  # type: ignore
                    try:
                        orig(self_)
                    except BrokenPipeError:
                        return
                    except OSError as e:
                        if getattr(e, "errno", None) == 32:
                            return
                        raise
                sp.Serial._update_dtr_state = _safe_update_dtr_state  # type: ignore
                patched = True
        except Exception:
            pass

        try:
            ser.open()
        except BrokenPipeError:
            pass

    finally:
        if patched and orig:
            try:
                import serial.serialposix as sp  # type: ignore
                sp.Serial._update_dtr_state = orig  # type: ignore
            except Exception:
                pass

    return ser


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

        self._ser = _open_serial_safely(self.port, self.baudrate, self.read_timeout)

        _toggle_lines_safe(self._ser)

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
        """
        Read and log any frames for up to `seconds`. Stops early when nothing arrives.
        (Useful as a best-effort pre-drain, but do NOT use for protocol correctness.)
        """
        if not self._ser or not self._ser.is_open:
            self.open()
        assert self._ser is not None

        end = time.time() + float(seconds)
        while time.time() < end:
            got = _read_one_frame(self._ser, timeout_s=0.25)
            if not got:
                break  # ✅ stop early when no data
            props, raw = got
            self.log.debug(f"RX ({label}) {props} RAW={raw!r}")

    def _wait_for_sid(
        self,
        method: str,
        sid: str,
        first_wait: float,
        final_wait: float,
        log_other: bool = True,
        on_match: Optional[Callable[[Dict[str, str], bool], None]] = None,  # (props, is_final)
    ) -> Optional[SigmaFrame]:
        """
        Wait for frames matching METHOD+SID.
        - waits for first matching frame up to first_wait
        - if STATUS != 0 returns immediately
        - else continues until final frame (TIMEOUT missing/0) or final_wait

        on_match(props, is_final) is called for each matching frame (best-effort, never raises).
        """
        if not self._ser or not self._ser.is_open:
            self.open()
        assert self._ser is not None

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
                is_final = _timeout_is_final(props)
                if on_match:
                    try:
                        on_match(props, is_final)
                    except Exception:
                        pass

                first = props
                first_raw = raw
                self.log.debug(f"RX({method} first) {props} RAW={raw!r}")
                break

        if not first:
            return None

        if str(first.get("STATUS") or "") != "0":
            return SigmaFrame(props=first, raw_text=first_raw, sid=sid, method=method)

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
                is_final = _timeout_is_final(props)
                if on_match:
                    try:
                        on_match(props, is_final)
                    except Exception:
                        pass

                last = props
                last_raw = raw
                self.log.debug(f"RX({method}) {props} RAW={raw!r}")
                if is_final:
                    break

        return SigmaFrame(props=last, raw_text=last_raw, sid=sid, method=method)

    def _send_and_wait_final(
        self,
        method: str,
        extra_lines: Optional[List[str]] = None,
        first_wait: float = 6.0,
        final_wait: float = 25.0,
    ) -> Optional[SigmaFrame]:
        """
        Send METHOD and wait for its response frames to complete (final TIMEOUT=0/missing).
        """
        sid = self._send_method(method, extra_lines=extra_lines)
        return self._wait_for_sid(
            method,
            sid,
            first_wait=first_wait,
            final_wait=final_wait,
            log_other=True,
        )

    # -----------------------------
    # Status / idle handling
    # -----------------------------

    def get_status_final(self, max_wait: float = 6.0) -> Optional[Dict[str, str]]:
        sid = self._send_method("GET_STATUS")
        frame = self._wait_for_sid(
            "GET_STATUS",
            sid,
            first_wait=max_wait,
            final_wait=max_wait,
            log_other=True,
        )
        return frame.props if frame else None

    @staticmethod
    def _is_idle(st: Optional[Dict[str, str]]) -> bool:
        return bool(st) and str(st.get("STATUS") or "") == "0"

    def ensure_idle(self, max_total_wait: float = 45.0) -> bool:
        """
        Bring terminal to IDLE (STATUS=0). If STATUS=20, run recovery sequence:
          COMPLETE_TX -> CANCEL_TX -> poll GET_STATUS(final) until STATUS == 0
        REVERSAL is last resort.
        """
        deadline = time.time() + float(max_total_wait)

        # Best-effort: clear any stale frames so we start clean
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
                # ✅ Deterministic: wait for COMPLETE/CANCEL responses rather than draining randomly
                self._send_and_wait_final("COMPLETE_TX", first_wait=6.0, final_wait=30.0)
                self._send_and_wait_final("CANCEL_TX", first_wait=6.0, final_wait=30.0)

                # Poll until idle or deadline
                while time.time() < deadline:
                    st = self.get_status_final(max_wait=6.0)
                    if self._is_idle(st):
                        return True
                    if str((st or {}).get("STATUS") or "") == "20":
                        time.sleep(0.6)
                        continue
                    time.sleep(0.6)

                # last resort
                self._send_and_wait_final("REVERSAL", first_wait=6.0, final_wait=35.0)
                st = self.get_status_final(max_wait=6.0)
                if self._is_idle(st):
                    return True

                return False

            # For any other non-idle code, just keep polling a bit
            time.sleep(0.8)
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
        on_phase: Optional[Callable[[str, Dict[str, str]], None]] = None,
    ) -> Dict[str, Any]:
        """
        PURCHASE using amount in minor units (e.g. 100 for £1.00).
        Returns dict compatible with your existing API expectations.

        on_phase("finalising", props) fires once when we first see an approved (STATUS=0)
        non-final frame (TIMEOUT not 0/missing). This is your UI "Processing..." window.
        """
        if not self._ser or not self._ser.is_open:
            self.open()

        # Best-effort: flush any stray frames before starting
        self._drain(seconds=2.0, label="pre-purchase-drain")

        if not self.ensure_idle(max_total_wait=45.0):
            raise SigmaTimeout("Terminal not idle before purchase")

        pounds = int(amount_minor) // 100
        pence = int(amount_minor) % 100
        amt_str = f"{pounds}.{pence:02d}"
        extra = [
            f"AMOUNT={amt_str}",
            f"CURRENCY={str(currency_num)}",
        ]
        if reference:
            extra.append(f"REFERENCE={reference[:64]}")

        sid = self._send_method("PURCHASE", extra_lines=extra)

        fired_finalising = False

        def _on_purchase_frame(p: Dict[str, str], is_final: bool) -> None:
            nonlocal fired_finalising
            if fired_finalising:
                return

            # Sigma can emit STATUS=0 frames before the customer taps (e.g. "waiting for card").
            # Only flip UI to "finalising" once auth really happened:
            #  - typically STAGE >= 5 on your flow, OR
            #  - we see real auth markers like TXID/RRN/AUTHCODE/etc.
            stage_str = str(p.get("STAGE") or "")
            try:
                stage_i = int(stage_str) if stage_str.isdigit() else -1
            except Exception:
                stage_i = -1

            has_auth_markers = bool(
                (p.get("TXID") or "").strip()
                or (p.get("RRN") or "").strip()
                or (p.get("AUTHCODE") or "").strip()
                or (p.get("APPROVAL") or "").strip()
            )

            if str(p.get("STATUS") or "") == "0" and (not is_final) and (stage_i >= 5 or has_auth_markers):
                fired_finalising = True
                if on_phase:
                    try:
                        on_phase("finalising", p)
                    except Exception:
                        pass

        frame = self._wait_for_sid(
            "PURCHASE",
            sid,
            first_wait=first_wait,
            final_wait=final_wait,
            log_other=True,
            on_match=_on_purchase_frame,
        )
        if not frame:
            raise SigmaTimeout("No PURCHASE response within timeout")

        props = frame.props
        status = str(props.get("STATUS") or "")
        stage = str(props.get("STAGE") or "")
        timeout = str(props.get("TIMEOUT") or "")

        approved = (status == "0")

        # ✅ CRITICAL: clean terminal for NEXT transaction (Sigma sometimes leaves STATUS=20 after success)
        try:
            self.ensure_idle(max_total_wait=20.0)
        except Exception as e:
            self.log.debug(f"post-purchase ensure_idle failed: {e!r}")

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
    def __init__(self, port: str = DEFAULT_PORT, baud: int = DEFAULT_BAUD, version: str = DEFAULT_VERSION):
        self._client = SigmaIppClient(port=port, baudrate=baud, version=version)

    def purchase(
        self,
        amount_minor: int,
        currency_num: str = "826",
        reference: str = "",
        on_phase: Optional[Callable[[str, Dict[str, str]], None]] = None,
    ) -> Dict[str, Any]:
        with self._client as c:
            return c.purchase(
                amount_minor=amount_minor,
                currency_num=currency_num,
                reference=reference,
                on_phase=on_phase,
            )
