"""Rotating log of events only: start, parse errors, power_source change.

Deliberately not a data log. Recording every frame would fill a board's flash
and would put raw sensor bytes on disk, which is what the aggregate counters
and the rate limit exist to avoid.

AP_FLAKE8_CLEAN
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Optional


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class EventLogger:
    def __init__(
        self,
        log_path: Path,
        *,
        max_bytes: int = 2_097_152,
        backup_count: int = 3,
        rate_limit_sec: float = 10.0,
        also_stdout: bool = True,
    ) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._rate_limit_sec = rate_limit_sec
        self._last_emit: Dict[str, float] = {}
        self._also_stdout = also_stdout
        self._logger = logging.getLogger(f"tpcm.event.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        handler = RotatingFileHandler(
            str(log_path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(handler)

    def emit(
        self,
        key: str,
        body: str,
        *,
        rate_limit: bool = False,
        now: Optional[float] = None,
    ) -> bool:
        ts = time.monotonic() if now is None else now
        if rate_limit:
            last = self._last_emit.get(key)
            if last is not None and (ts - last) < self._rate_limit_sec:
                return False
            self._last_emit[key] = ts
        line = f"{_iso_now()} tpcm {body}"
        self._logger.info(line)
        if self._also_stdout:
            print(line, flush=True)
        return True

    def close(self) -> None:
        for handler in self._logger.handlers[:]:
            handler.close()
            self._logger.removeHandler(handler)
