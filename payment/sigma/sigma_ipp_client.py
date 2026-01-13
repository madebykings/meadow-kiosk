# sigma_ipp_client.py
"""
myPOS Sigma (IPP over USB/Serial) client — production-ready lifecycle handling.

Assumptions / context baked in (from your Sigma IPP debugging):
- Device path: /dev/sigma (symlink to /dev/ttyACM0)
- If GET_STATUS returns STATUS=20 ("NOT COMPLETED LAST TX"):
    1) issue COMPLETE_TX
    2) then CANCEL_TX
    3) then wait for a *final* GET_STATUS with TIMEOUT=0 returning STATUS=0
- PURCHASE requires waiting for the *final* response (TIMEOUT=0 style)
- User-declined amounts can surface as non-zero STATUS at STAGE=5
  (treat that as a terminal decision, then wait for READY before next op)

This file is intentionally defensive:
- Robust framing + tolerant key=value parsing
- Explicit “wait for final frame” loops with a max wall-clock guard
- Serial read is line/frame based with a binary-safe buffer
- Dedicated serial logger support (optional)

If your on-wire frame format differs from the default STX/ETX/LRC framing below,
only update `SigmaFramer` (build_frame / feed / extract_frames).
Everything else (lifecycle/retries/state) remains valid.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Callable

import serial  # pyserial


# -----------------------------
# Logging
# -----------------------------

DEFAULT_DEVICE = "/dev/sigma"
DEFAULT_BAUDRATE = 115200  # adjust if your Sigma requires different
DEFAULT_WRITE_TIMEOUT = 2.0

SIGMA_LOG_PATH = "/home/meadow/meadow-kiosk/sigma_serial.log"


def _build_default_sigma_logger() -> logging.Logger:
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


# -----------------------------
# Data models
# -----------------------------

@dataclass
class SigmaFrame:
    raw: bytes
    payload: bytes  # unframed payload (application-level content)


@dataclass
class SigmaResponse:
    """Parsed response (best-effort KV parsing) + raw frames."""
    fields: Dict[str, str]
    frames: List[SigmaFrame] = field(default_factory=list)

    @property
    def status(self) -> Optional[int]:
        v = self.fields.get("STATUS")
        try:
            return int(v) if v is not None else None
        except ValueError:
            return None

    @property
    def stage(self) -> Optional[int]:
        v = self.fields.get("STAGE")
        try:
            return int(v) if v is not None else None
        except ValueError:
            return None

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self.fields.get(key, default)


class SigmaError(Exception):
    pass


class SigmaTimeout(SigmaError):
    pass


class SigmaProtocolError(SigmaError):
    pass


# -----------------------------
# Framing (default: STX ... ETX LRC)
# -----------------------------

class SigmaFramer:
    """
    Default framing: STX (0x02) + payload + ETX (0x03) + LRC (xor of payload+ETX)

    If your current working code uses a different wire format (length-prefix, CRC16,
    ASCII with newlines, etc.), update only this class.
    """

    STX = 0x02
    ETX = 0x03

    def __init__(self, sigma_logger: Optional[logging.Logger] = None):
        self._buf = bytearray()
        self._log = sigma_logger

    @staticmethod
    def lrc(data: bytes) -> int:
        x = 0
        for b in data:
            x ^= b
        return x & 0xFF

    def build_frame(self, payload: bytes) -> bytes:
        body = payload + bytes([self.ETX])
        lrc = self.lrc(body)
        return bytes([self.STX]) + payload + bytes([self.ETX, lrc])

    def feed(self, chunk: bytes) -> None:
        if chunk:
            self._buf.extend(chunk)

    def extract_frames(self) -> List[SigmaFrame]:
        frames: List[SigmaFrame] = []

        while True:
            # Find STX
            try:
                stx_i = self._buf.index(self.STX)
            except ValueError:
                # No STX; drop noise
                if self._buf:
                    self._buf.clear()
                break

            if stx_i > 0:
                # discard leading noise
                del self._buf[:stx_i]

            # Need at least STX + ETX + LRC
            if len(self._buf) < 4:
                break

            # Find ETX after STX
            try:
                etx_i = self._buf.index(self.ETX, 1)
            except ValueError:
                break

            # Need LRC byte after ETX
            if etx_i + 1 >= len(self._buf):
                break

            lrc_byte = self._buf[etx_i + 1]
            payload = bytes(self._buf[1:etx_i])  # between STX and ETX
            body = payload + bytes([self.ETX])
            expected = self.lrc(body)

            frame_raw = bytes(self._buf[: etx_i + 2])

            # Consume buffer for this candidate regardless; if invalid, continue scanning
            del self._buf[: etx_i + 2]

            if lrc_byte != expected:
                if self._log:
                    self._log.debug(
                        f"RX frame LRC mismatch: got={lrc_byte:02X} expected={expected:02X} raw={frame_raw!r}"
                    )
                continue

            frames.append(SigmaFrame(raw=frame_raw, payload=payload))

        return frames


# -----------------------------
# Parsing helpers (tolerant)
# -----------------------------

_KV_RE = re.compile(r"([A-Z0-9_]+)\s*=\s*([^;|,\r\n]+)", re.IGNORECASE)


def parse_kv(payload: bytes) -> Dict[str, str]:
    """
    Best-effort parse of key=value pairs from an ASCII-ish payload.
    Works across separators: ; | , whitespace, CRLF.
    """
    try:
        text = payload.decode("utf-8", errors="replace")
    except Exception:
        text = str(payload)

    fields: Dict[str, str] = {}

    for m in _KV_RE.finditer(text):
        k = m.group(1).strip().upper()
        v = m.group(2).strip()
        fields[k] = v

    # Also preserve entire message for debugging
    fields.setdefault("_RAW", text.strip())
    return fields


# -----------------------------
# Client
# -----------------------------

class SigmaIppClient:
    def __init__(
        self,
        port: str = DEFAULT_DEVICE,
        baudrate: int = DEFAULT_BAUDRATE,
        serial_timeout: float = 0.2,
        write_timeout: float = DEFAULT_WRITE_TIMEOUT,
        sigma_logger: Optional[logging.Logger] = None,
        app_logger: Optional[logging.Logger] = None,
    ):
        self.port = port
        self.baudrate = baudrate
        self.serial_timeout = serial_timeout
        self.write_timeout = write_timeout
        self.sigma_log = sigma_logger or _build_default_sigma_logger()
        self.log = app_logger or logging.getLogger(__name__)
        self._ser: Optional[serial.Serial] = None
        self._framer = SigmaFramer(self.sigma_log)

    # ---- lifecycle ----

    def open(self) -> None:
        if self._ser and self._ser.is_open:
            return
        self._ser = serial.Serial(
            self.port,
            self.baudrate,
            timeout=self.serial_timeout,
            write_timeout=self.write_timeout,
        )
        self._flush()
        self.sigma_log.debug(f"OPEN port={self.port} baud={self.baudrate}")

    def close(self) -> None:
        if self._ser:
            try:
                self._ser.close()
            finally:
                self._ser = None
        self.sigma_log.debug("CLOSE")

    def __enter__(self) -> "SigmaIppClient":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---- public API ----

    def ensure_ready(self, max_wait_s: float = 30.0) -> None:
        """
        Enforce the production-safe precondition:
        - If STATUS=20 => COMPLETE_TX -> CANCEL_TX -> wait final READY
        - Otherwise wait until READY (STATUS=0)
        """
        st = self.get_status(timeout_mode="normal")
        if st.status == 20:
            self.log.info("Sigma reports STATUS=20 (NOT COMPLETED LAST TX). Running recovery.")
            self._recover_not_completed_last_tx(max_wait_s=max_wait_s)
        else:
            self._wait_for_ready(max_wait_s=max_wait_s)

    def purchase(
        self,
        amount_minor: int,
        currency: str = "GBP",
        max_wait_s: float = 180.0,
        ref: Optional[str] = None,
    ) -> SigmaResponse:
        """
        Execute a PURCHASE flow and wait for the final frame (TIMEOUT=0 style),
        then wait until terminal is READY before returning.

        Returns a parsed SigmaResponse with accumulated frames.
        """
        self.ensure_ready(max_wait_s=30.0)

        ref = (ref or uuid.uuid4().hex[:12]).upper()
        cmd = self._build_command(
            "PURCHASE",
            {
                "AMOUNT": str(int(amount_minor)),
                "CURRENCY": currency,
                "REF": ref,
            },
        )
        self._write_frame(cmd)

        # Wait for the "final" purchase completion response
        resp = self._read_until_complete(
            max_wait_s=max_wait_s,
            is_complete=self._purchase_complete_predicate,
        )

        # If decline/error surfaced at STAGE=5, treat as final decision (per your finding)
        # but still enforce READY before returning.
        self._wait_for_ready(max_wait_s=30.0)

        return resp

    def complete_tx(self, max_wait_s: float = 60.0) -> SigmaResponse:
        cmd = self._build_command("COMPLETE_TX", {"REF": uuid.uuid4().hex[:12].upper()})
        self._write_frame(cmd)
        return self._read_until_complete(max_wait_s=max_wait_s, is_complete=self._generic_complete_predicate)

    def cancel_tx(self, max_wait_s: float = 60.0) -> SigmaResponse:
        cmd = self._build_command("CANCEL_TX", {"REF": uuid.uuid4().hex[:12].upper()})
        self._write_frame(cmd)
        return self._read_until_complete(max_wait_s=max_wait_s, is_complete=self._generic_complete_predicate)

    def get_status(self, timeout_mode: str = "normal", max_wait_s: float = 10.0) -> SigmaResponse:
        """
        timeout_mode:
          - "normal": bounded read window (serial timeouts) up to max_wait_s
          - "final": emulate TIMEOUT=0 semantics by waiting for next status frame
                    but still guarded by max_wait_s (production safety)
        """
        if timeout_mode not in ("normal", "final"):
            raise ValueError("timeout_mode must be 'normal' or 'final'")

        params = {
            "REF": uuid.uuid4().hex[:12].upper(),
            "TIMEOUT": "0" if timeout_mode == "final" else "1",
        }
        cmd = self._build_command("GET_STATUS", params)
        self._write_frame(cmd)

        return self._read_until_complete(
            max_wait_s=max_wait_s,
            is_complete=self._status_complete_predicate,
        )

    # -----------------------------
    # Internal: recovery + waits
    # -----------------------------

    def _recover_not_completed_last_tx(self, max_wait_s: float) -> None:
        # Spec per your debugging: COMPLETE_TX then CANCEL_TX then wait final GET_STATUS TIMEOUT=0 => STATUS=0
        try:
            self.complete_tx(max_wait_s=min(60.0, max_wait_s))
        except SigmaError as e:
            self.log.warning(f"COMPLETE_TX during recovery raised: {e!r}")

        try:
            self.cancel_tx(max_wait_s=min(60.0, max_wait_s))
        except SigmaError as e:
            self.log.warning(f"CANCEL_TX during recovery raised: {e!r}")

        # Must wait for final GET_STATUS TIMEOUT=0 with STATUS=0
        deadline = time.monotonic() + max_wait_s
        while time.monotonic() < deadline:
            st = self.get_status(timeout_mode="final", max_wait_s=min(30.0, max_wait_s))
            if st.status == 0:
                return
        raise SigmaTimeout("Recovery from STATUS=20 timed out waiting for STATUS=0")

    def _wait_for_ready(self, max_wait_s: float = 30.0) -> None:
        deadline = time.monotonic() + max_wait_s
        last: Optional[SigmaResponse] = None
        while time.monotonic() < deadline:
            last = self.get_status(timeout_mode="normal", max_wait_s=5.0)
            if last.status == 0:
                return
            if last.status == 20:
                self._recover_not_completed_last_tx(max_wait_s=max_wait_s)
                return
            time.sleep(0.25)
        raise SigmaTimeout(f"Terminal not READY (STATUS=0) within {max_wait_s}s. Last={last.fields if last else None}")

    # -----------------------------
    # Internal: command building
    # -----------------------------

    def _build_command(self, name: str, params: Dict[str, str]) -> bytes:
        """
        Build an application payload.

        Default format: ASCII "CMD=NAME;K=V;K=V"
        If your terminal expects a different payload structure, update here.
        """
        parts = [f"CMD={name}"]
        for k, v in params.items():
            parts.append(f"{k}={v}")
        payload = ";".join(parts)
        return payload.encode("utf-8")

    # -----------------------------
    # Internal: IO (write/read)
    # -----------------------------

    def _flush(self) -> None:
        if not self._ser:
            return
        try:
            self._ser.reset_input_buffer()
            self._ser.reset_output_buffer()
        except Exception:
            pass

    def _write_frame(self, payload: bytes) -> None:
        if not self._ser or not self._ser.is_open:
            self.open()

        frame = self._framer.build_frame(payload)
        self.sigma_log.debug(f"TX {frame!r}")
        try:
            self._ser.write(frame)
            self._ser.flush()
        except Exception as e:
            raise SigmaError(f"Serial write failed: {e}") from e

    def _read_frames(self, max_wait_s: float) -> List[SigmaFrame]:
        """
        Read and return any valid frames observed within max_wait_s.
        """
        if not self._ser or not self._ser.is_open:
            self.open()

        deadline = time.monotonic() + max_wait_s
        out: List[SigmaFrame] = []

        while time.monotonic() < deadline:
            try:
                chunk = self._ser.read(4096)
            except Exception as e:
                raise SigmaError(f"Serial read failed: {e}") from e

            if chunk:
                self.sigma_log.debug(f"RXCHUNK {chunk!r}")
                self._framer.feed(chunk)
                frames = self._framer.extract_frames()
                if frames:
                    for fr in frames:
                        self.sigma_log.debug(f"RX {fr.raw!r}")
                    out.extend(frames)
                    break  # return promptly once we have frames
            else:
                # No data this tick
                time.sleep(0.01)

        return out

    def _read_until_complete(
        self,
        max_wait_s: float,
        is_complete: Callable[[SigmaResponse], bool],
    ) -> SigmaResponse:
        """
        Keep reading frames until predicate says complete, or timeout.
        Accumulates frames and merges parsed fields (last write wins).
        """
        deadline = time.monotonic() + max_wait_s
        all_frames: List[SigmaFrame] = []
        merged: Dict[str, str] = {}
        last_resp: Optional[SigmaResponse] = None

        while time.monotonic() < deadline:
            frames = self._read_frames(max_wait_s=min(1.0, max_wait_s))
            if not frames:
                continue

            all_frames.extend(frames)

            # Parse every payload and merge
            for fr in frames:
                fields = parse_kv(fr.payload)
                merged.update(fields)

            last_resp = SigmaResponse(fields=dict(merged), frames=list(all_frames))

            if is_complete(last_resp):
                return last_resp

            # If purchase showed decline at stage 5, we can treat as complete (per your finding)
            if self._is_decline_at_stage5(last_resp):
                return last_resp

        raise SigmaTimeout(f"Timed out waiting for completion after {max_wait_s}s. Last={last_resp.fields if last_resp else None}")

    # -----------------------------
    # Completion predicates
    # -----------------------------

    @staticmethod
    def _status_complete_predicate(resp: SigmaResponse) -> bool:
        # Any status response with STATUS present is "complete" for a GET_STATUS command.
        return resp.status is not None

    @staticmethod
    def _generic_complete_predicate(resp: SigmaResponse) -> bool:
        """
        Generic "command complete" heuristic:
        - STATUS present AND (STAGE absent OR STAGE in a terminal-ish range)
        This is intentionally tolerant.
        """
        if resp.status is None:
            return False
        if resp.stage is None:
            return True
        return resp.stage >= 6 or resp.stage == 0

    @staticmethod
    def _purchase_complete_predicate(resp: SigmaResponse) -> bool:
        """
        PURCHASE "final frame" heuristic:
        - STATUS present AND either:
            - STAGE >= 6 (completed)
            - or fields include typical completion markers (APPROVED/DECLINED/ERROR)
        You can tighten this if you have an exact marker from your logs.
        """
        if resp.status is None:
            return False
        stg = resp.stage
        if stg is not None and stg >= 6:
            return True

        raw = (resp.get("_RAW") or "").upper()
        # Common terminal keywords (best-effort)
        if any(k in raw for k in ("APPROVED", "DECLINED", "CANCELLED", "CANCELED", "ERROR", "FAILED", "COMPLETED")):
            return True

        # Some protocols send RESULT=... or RESP=...
        result = (resp.get("RESULT") or resp.get("RESP") or "").upper()
        if result in ("APPROVED", "DECLINED", "CANCELLED", "CANCELED", "ERROR", "FAILED", "SUCCESS"):
            return True

        return False

    @staticmethod
    def _is_decline_at_stage5(resp: SigmaResponse) -> bool:
        # Your observed behavior: non-zero STATUS at STAGE=5 indicates user-declined amount.
        return (resp.stage == 5) and (resp.status is not None) and (resp.status != 0)


# -----------------------------
# Minimal smoke test (optional)
# -----------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    client = SigmaIppClient(
        port=DEFAULT_DEVICE,
        baudrate=DEFAULT_BAUDRATE,
    )

    with client:
        st = client.get_status(timeout_mode="normal")
        print("STATUS:", st.status, "STAGE:", st.stage, "RAW:", st.get("_RAW"))

        # Example purchase (amount in minor units, e.g. 150 = £1.50)
        # resp = client.purchase(amount_minor=150, currency="GBP")
        # print("PURCHASE STATUS:", resp.status, "STAGE:", resp.stage)
        # print("FIELDS:", resp.fields)
