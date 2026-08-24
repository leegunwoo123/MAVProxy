"""Optional helpers for building TPCM_STATUS field dicts (encode tested separately).

AP_FLAKE8_CLEAN
"""

from __future__ import annotations

import time
from typing import Any, Dict

from MAVProxy.modules.mavproxy_tpcm.types import TpcmStatus


def time_usec_now() -> int:
    """Unix epoch microseconds; 0 if clock looks unset (before 2000-01-01)."""
    now = time.time()
    if now < 946684800:  # 2000-01-01 UTC
        return 0
    return int(now * 1_000_000)


def status_to_mavlink_kwargs(status: TpcmStatus, time_usec: int | None = None) -> Dict[str, Any]:
    if time_usec is None:
        time_usec = time_usec_now()
    fields = status.as_dict()
    fields["time_usec"] = time_usec
    return fields
