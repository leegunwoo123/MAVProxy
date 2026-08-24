"""Encode MAVLink2 bytes for the GCS: TPCM_STATUS(5600) and STATUSTEXT(253).

AP_FLAKE8_CLEAN
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Tuple

from MAVProxy.modules.mavproxy_tpcm.mapping import (
    status_to_mavlink_kwargs,
    time_usec_now,
)
from MAVProxy.modules.mavproxy_tpcm.messages import MAVLink_tpcm_status_message
from MAVProxy.modules.mavproxy_tpcm.types import TpcmStatus

# MAV_SEVERITY subset used by the relay.
SEV_CRITICAL = 2
SEV_ERROR = 3
SEV_WARNING = 4
SEV_NOTICE = 5
SEV_INFO = 6

STATUSTEXT_MAX_BYTES = 50
STATUSTEXT_PREFIX = "TPCM: "
# Chunking (id/chunk_seq) is unevenly supported across GCS, so text is cut.
STATUSTEXT_BODY_BYTES = STATUSTEXT_MAX_BYTES - len(STATUSTEXT_PREFIX)

DEFAULT_SYSTEM_ID = 1
DEFAULT_COMPONENT_ID = 158


class EncodeError(Exception):
    pass


def dialect_xml_path() -> Path:
    """Message definition shipped with this module, for the GCS to load.

    TPCM_STATUS field order is part of the wire format, so the XML has to
    version with the encoder rather than with the deployment tree. The relay
    does not read it at runtime; messages.py holds the same definition as code.
    """
    return Path(__file__).resolve().parent / "tpcm.xml"


def load_dialect():
    """common.xml, which pymavlink ships. TPCM_STATUS comes from messages.py.

    Nothing has to be generated into pymavlink for this module to work, so
    there is no longer a dialect that can be absent at runtime.
    """
    try:
        from pymavlink.dialects.v20 import common as dialect
    except ImportError as exc:
        raise EncodeError("pymavlink common dialect unavailable") from exc
    return dialect


class MavEncoder:
    """One MAVLink instance for every message this component sends.

    seq is per (system, component), not per message id: mavutil accounts loss
    in `last_seq[(sysid, compid)]` across all ids, and `diff = (seq2-seq) % 256`
    wraps a regression into a ~255 packet loss spike. A second counter for the
    same identity would therefore corrupt the GCS link quality readout.
    """

    def __init__(
        self,
        *,
        system_id: int = DEFAULT_SYSTEM_ID,
        component_id: int = DEFAULT_COMPONENT_ID,
        dialect: Any = None,
    ) -> None:
        if component_id == 1:
            raise EncodeError("component_id==1 forbidden")
        if not (0 <= component_id <= 255):
            raise EncodeError(f"invalid component_id:{component_id}")
        if not (1 <= system_id <= 255):
            raise EncodeError(f"invalid system_id:{system_id}")
        self.dialect = dialect if dialect is not None else load_dialect()
        self._mav = self.dialect.MAVLink(
            None, srcSystem=system_id, srcComponent=component_id
        )
        self._mav.robust_parsing = True
        self._mav.seq = 0

    @property
    def seq(self) -> int:
        return self._mav.seq & 0xFF

    @seq.setter
    def seq(self, value: int) -> None:
        self._mav.seq = value & 0xFF

    def _pack(self, msg: Any) -> bytes:
        # Force MAVLink2: msg id 5600 is MAV1-incompatible.
        try:
            buf = msg.pack(self._mav, force_mavlink1=False)
        except TypeError:
            buf = msg.pack(self._mav)
        # pack() does not advance seq; pymavlink only does that in send().
        self._mav.seq = (self._mav.seq + 1) & 0xFF
        return bytes(buf)

    def tpcm_status(self, status: TpcmStatus, time_usec: Optional[int] = None) -> bytes:
        if time_usec is None:
            time_usec = time_usec_now()
        kwargs = status_to_mavlink_kwargs(status, time_usec=time_usec)
        # The class comes from this package, not from self.dialect: pymavlink
        # ships no tpcm dialect and the module no longer installs one.
        return self._pack(MAVLink_tpcm_status_message(**kwargs))

    def statustext(self, severity: int, body: str) -> bytes:
        text = f"{STATUSTEXT_PREFIX}{body}"
        payload = text.encode("ascii", errors="replace")[:STATUSTEXT_MAX_BYTES]
        cls = self.dialect.MAVLink_statustext_message
        try:
            msg = cls(int(severity), payload, 0, 0)
        except TypeError:
            # Older pymavlink: no MAVLink2 extension fields, or char as str.
            try:
                msg = cls(int(severity), payload)
            except TypeError:
                msg = cls(int(severity), payload.decode("ascii", errors="replace"))
        return self._pack(msg)


def encode_tpcm_status(
    status: TpcmStatus,
    *,
    system_id: int,
    component_id: int,
    time_usec: Optional[int] = None,
    seq: int = 0,
    dialect: Any = None,
) -> Tuple[bytes, int]:
    """Return (mavlink2 packet bytes, next_seq).

    Kept for callers that hold their own seq. The relay uses MavEncoder so that
    TPCM_STATUS and STATUSTEXT cannot diverge into two counters.

    Never uses FC master. Caller must write only to GCS outputs.
    """
    enc = MavEncoder(
        system_id=system_id, component_id=component_id, dialect=dialect
    )
    enc.seq = seq
    buf = enc.tpcm_status(status, time_usec=time_usec)
    return buf, enc.seq
