"""TPCM UART ASCII line parser.

AP_FLAKE8_CLEAN
"""

from __future__ import annotations

import math
import re
from typing import Tuple, Union

from MAVProxy.modules.mavproxy_tpcm.types import ParseError, TpcmStatus

_TOKEN_SPLIT = re.compile(r"[ \t]+")


def _finite_float(token: str) -> Union[float, ParseError]:
    try:
        value = float(token)
    except ValueError:
        return ParseError(f"non_numeric:{token!r}")
    if not math.isfinite(value):
        return ParseError(f"non_finite:{token!r}")
    return value


def parse_line(
    line: bytes | str,
    *,
    max_line_bytes: int = 256,
) -> Tuple[Union[TpcmStatus, None], Union[ParseError, None]]:
    """Parse one CRLF-stripped TPCM status line.

    Returns (status, None) on success, (None, ParseError) on discard.

    Does no logging of its own. ParseError names the reason and at most the one
    token that failed, so the caller decides how much of a noisy stream reaches
    the log; TpcmEngine truncates the line and rate limits it.
    """
    if isinstance(line, bytes):
        if len(line) > max_line_bytes:
            return None, ParseError("line_too_long")
        try:
            text = line.decode("ascii")
        except UnicodeDecodeError:
            return None, ParseError("non_ascii")
    else:
        text = line
        if len(text.encode("utf-8", errors="replace")) > max_line_bytes:
            return None, ParseError("line_too_long")

    text = text.strip("\r\n")
    if not text:
        return None, ParseError("empty")

    # Reject other control characters / illegal noise early.
    if any(ord(ch) < 32 and ch not in "\t" for ch in text):
        return None, ParseError("illegal_control")

    # The hardware sends commas; earlier bench captures were space/tab
    # separated, so accept both rather than rejecting half the samples.
    parts = _TOKEN_SPLIT.split(text.strip().replace(",", " "))
    if not parts or parts[0] != "Value":
        return None, ParseError("bad_prefix")
    fields = parts[1:]
    mapped = _map_fields(fields)
    if isinstance(mapped, ParseError):
        return None, mapped
    return mapped, None


def _map_fields(fields: list[str]) -> Union[TpcmStatus, ParseError]:
    # 12 fields is the full 3-channel layout. 9 is the 2-channel board:
    #   AC1,AC2, DCV1,DCV2, DCI1,DCI2, temp, bat, power_source
    # The missing third channel becomes NaN rather than 0, so the GCS can tell
    # "not fitted" from "zero volts".
    if len(fields) == 12:
        n_numeric = 11
        layout = "12"
    elif len(fields) == 9:
        n_numeric = 8
        layout = "9"
    else:
        return ParseError(f"field_count:{len(fields)}")

    values = []
    for idx, token in enumerate(fields[:n_numeric]):
        parsed = _finite_float(token)
        if isinstance(parsed, ParseError):
            return ParseError(f"field{idx + 1}:{parsed.reason}")
        values.append(parsed)

    # power_source: integer 0..3 only (no float coercion like 1.5)
    ps_tok = fields[n_numeric]
    if not re.fullmatch(r"[0-3]", ps_tok):
        return ParseError(f"power_source:{ps_tok!r}")
    power_source = int(ps_tok)

    if layout == "12":
        ac1, ac2, ac3 = values[0], values[1], values[2]
        dc1, dc2, dc3 = values[3], values[4], values[5]
        i1, i2, i3 = values[6], values[7], values[8]
        temperature, battery = values[9], values[10]
    else:
        ac1, ac2, ac3 = values[0], values[1], math.nan
        dc1, dc2, dc3 = values[2], values[3], math.nan
        i1, i2, i3 = values[4], values[5], math.nan
        temperature, battery = values[6], values[7]

    i1, i2, i3 = i1 * 2.0, i2 * 2.0, i3 * 2.0

    # Zero-padded tokens are fine here: float("024") is 24.0.
    return TpcmStatus(
        ac_input_voltage_1=ac1,
        ac_input_voltage_2=ac2,
        ac_input_voltage_3=ac3,
        dc_output_voltage_1=dc1,
        dc_output_voltage_2=dc2,
        dc_output_voltage_3=dc3,
        dc_output_current_1=i1,
        dc_output_current_2=i2,
        dc_output_current_3=i3,
        temperature=temperature,
        battery_voltage=battery,
        power_source=power_source,
    )


class LineAssembler:
    """Assemble CRLF-terminated lines from a byte stream."""

    def __init__(self, max_line_bytes: int = 256) -> None:
        self.max_line_bytes = max_line_bytes
        self._buf = bytearray()
        self._overflow = False

    def clear(self) -> None:
        self._buf.clear()
        self._overflow = False

    def feed(self, data: bytes) -> list[bytes]:
        lines: list[bytes] = []
        for byte in data:
            if byte == 0x0A:  # LF
                if self._overflow:
                    self._buf.clear()
                    self._overflow = False
                    continue
                raw = bytes(self._buf)
                self._buf.clear()
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
                lines.append(raw)
                continue
            if self._overflow:
                continue
            self._buf.append(byte)
            if len(self._buf) > self.max_line_bytes:
                self._buf.clear()
                self._overflow = True
        return lines
