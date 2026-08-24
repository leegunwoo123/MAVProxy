"""TPCM_STATUS (id 5600) as a pymavlink message class, written rather than generated.

mavgen installs a dialect into pymavlink's own package directory. That makes
the module depend on a file outside its own install: a pymavlink upgrade or a
rebuilt venv deletes it, and anyone who installs MAVProxy without running the
generator gets a module that raises on the first encode. Everything mavgen
would emit beyond the class below is a copy of common.xml, which pymavlink
already ships, so only this one message is actually missing.

FIELDS is the wire format. tpcm.xml in this directory carries the same
definition for GCS-side consumers and has to change with it. To confirm the two
still agree after editing either, generate the dialect and compare:

    mavgen.py --lang=Python --wire-protocol=2.0 --output=/tmp/tpcm.py \\
        tpcm.xml            # needs common.xml beside it, from pymavlink
    # then check id, crc_extra, ordered_fieldnames and unpacker.format against
    # MAVLink_tpcm_status_message, and one packed frame byte for byte.

AP_FLAKE8_CLEAN
"""

from __future__ import annotations

import struct
from typing import Any, Dict, Tuple

from pymavlink.dialects.v20 import common

MSG_ID = 5600
MSG_NAME = "TPCM_STATUS"

# (field name, MAVLink type, struct code) in wire order. MAVLink orders a
# message without extension fields by descending type size, keeping
# declaration order within a size; tpcm.xml already declares them that way,
# so this is also the declaration order.
FIELDS: Tuple[Tuple[str, str, str], ...] = (
    ("time_usec", "uint64_t", "Q"),
    ("ac_input_voltage_1", "float", "f"),
    ("ac_input_voltage_2", "float", "f"),
    ("ac_input_voltage_3", "float", "f"),
    ("dc_output_voltage_1", "float", "f"),
    ("dc_output_voltage_2", "float", "f"),
    ("dc_output_voltage_3", "float", "f"),
    ("dc_output_current_1", "float", "f"),
    ("dc_output_current_2", "float", "f"),
    ("dc_output_current_3", "float", "f"),
    ("temperature", "float", "f"),
    ("battery_voltage", "float", "f"),
    ("power_source", "uint8_t", "B"),
)

FIELDNAMES = [name for name, _, _ in FIELDS]
FIELDTYPES = [ctype for _, ctype, _ in FIELDS]
NATIVE_FORMAT = "<" + "".join(code for _, _, code in FIELDS)

# TPCM_POWER_SOURCE, from tpcm.xml.
TPCM_POWER_SOURCE_CONVERT = 0
TPCM_POWER_SOURCE_BATTERY = 1
TPCM_POWER_SOURCE_PARALLEL_FAULT = 2
TPCM_POWER_SOURCE_PARALLEL_RECOVERED = 3


def _x25_crc(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16/MCRF4XX, the accumulator MAVLink uses for crc_extra and frames."""
    for byte in data:
        tmp = (byte ^ crc) & 0xFF
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


def _crc_extra() -> int:
    """The seed a receiver mixes in so a mismatched definition is rejected.

    Derived the way mavgen derives it, from the message name and every field's
    type and name, with the 16 bit result folded into 8. Computing it keeps one
    definition instead of a constant that can silently disagree with FIELDS.
    mavgen also accumulates a length byte per array field; none of these are
    arrays, so that step has nothing to add here.
    """
    crc = _x25_crc((MSG_NAME + " ").encode("ascii"))
    for name, ctype, _ in FIELDS:
        crc = _x25_crc((ctype + " ").encode("ascii"), crc)
        crc = _x25_crc((name + " ").encode("ascii"), crc)
    return (crc & 0xFF) ^ (crc >> 8)


class MAVLink_tpcm_status_message(common.MAVLink_message):
    """Tethered Power Control Manager status.

    Named after the class mavgen would have generated, so a caller that finds
    the message through a real tpcm dialect sees the same attribute.
    """

    id = MSG_ID
    msgname = MSG_NAME
    fieldnames = FIELDNAMES
    ordered_fieldnames = FIELDNAMES
    fieldtypes = FIELDTYPES
    fielddisplays_by_name: Dict[str, str] = {}
    fieldenums_by_name = {"power_source": "TPCM_POWER_SOURCE"}
    fieldunits_by_name = {
        "time_usec": "us",
        "ac_input_voltage_1": "V",
        "ac_input_voltage_2": "V",
        "ac_input_voltage_3": "V",
        "dc_output_voltage_1": "V",
        "dc_output_voltage_2": "V",
        "dc_output_voltage_3": "V",
        "dc_output_current_1": "A",
        "dc_output_current_2": "A",
        "dc_output_current_3": "A",
        "temperature": "degC",
        "battery_voltage": "V",
    }
    native_format = bytearray(NATIVE_FORMAT.encode("ascii"))
    orders = list(range(len(FIELDS)))
    lengths = [1] * len(FIELDS)
    array_lengths = [0] * len(FIELDS)
    crc_extra = _crc_extra()
    unpacker = struct.Struct(NATIVE_FORMAT)
    instance_field = None
    instance_offset = -1

    def __init__(self, **fields: Any) -> None:
        """Keyword-only, unlike mavgen's positional signature.

        Callers build the field dict from TpcmStatus, and a named argument list
        repeated here would be a second copy of FIELDS to keep in step.
        """
        missing = [name for name in FIELDNAMES if name not in fields]
        unknown = [name for name in fields if name not in FIELDNAMES]
        if missing or unknown:
            raise TypeError(
                f"{MSG_NAME}: missing {missing}, unknown {unknown}"
            )
        common.MAVLink_message.__init__(self, MSG_ID, MSG_NAME)
        self._fieldnames = FIELDNAMES
        self._instance_field = self.instance_field
        self._instance_offset = self.instance_offset
        for name in FIELDNAMES:
            setattr(self, name, fields[name])

    def pack(self, mav: Any, force_mavlink1: bool = False) -> bytes:
        payload = self.unpacker.pack(
            *[getattr(self, name) for name in FIELDNAMES]
        )
        return self._pack(
            mav, self.crc_extra, payload, force_mavlink1=force_mavlink1
        )
