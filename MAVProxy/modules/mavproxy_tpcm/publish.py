"""GCS publish rate policy: 1 Hz downsample, power_source change immediate.

AP_FLAKE8_CLEAN
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from MAVProxy.modules.mavproxy_tpcm.types import TpcmStatus


@dataclass(frozen=True)
class PublishDecision:
    should_publish: bool
    reason: str  # "rate" | "power_source_change" | "skip"


class RateLimitedPublisher:
    def __init__(self, target_hz: float = 1.0, downsample: bool = True) -> None:
        if target_hz <= 0:
            raise ValueError("target_hz must be > 0")
        self.target_hz = target_hz
        self.downsample = downsample
        self._min_interval_s = 1.0 / target_hz
        self._last_publish_monotonic: Optional[float] = None
        self._last_power_source: Optional[int] = None
        self._latest: Optional[TpcmStatus] = None

    @property
    def latest(self) -> Optional[TpcmStatus]:
        return self._latest

    def consider(self, status: TpcmStatus, now_monotonic: float) -> PublishDecision:
        self._latest = status
        power_changed = (
            self._last_power_source is not None and
            status.power_source != self._last_power_source
        )

        if not self.downsample:
            self._last_publish_monotonic = now_monotonic
            self._last_power_source = status.power_source
            return PublishDecision(True, "rate")

        if power_changed:
            self._last_publish_monotonic = now_monotonic
            self._last_power_source = status.power_source
            return PublishDecision(True, "power_source_change")

        if self._last_publish_monotonic is None:
            self._last_publish_monotonic = now_monotonic
            self._last_power_source = status.power_source
            return PublishDecision(True, "rate")

        elapsed = now_monotonic - self._last_publish_monotonic
        if elapsed + 1e-9 >= self._min_interval_s:
            self._last_publish_monotonic = now_monotonic
            self._last_power_source = status.power_source
            return PublishDecision(True, "rate")

        # Keep last seen power_source even when skipping, so a later change detects.
        self._last_power_source = status.power_source
        return PublishDecision(False, "skip")
