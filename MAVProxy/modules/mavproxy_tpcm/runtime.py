"""Serial TPCM engine: open/read/parse/rate/encode without MAVProxy.

AP_FLAKE8_CLEAN
"""

from __future__ import annotations

import io
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from MAVProxy.modules.mavproxy_tpcm.encode import (
    SEV_CRITICAL,
    SEV_ERROR,
    SEV_INFO,
    SEV_NOTICE,
    SEV_WARNING,
    EncodeError,
    MavEncoder,
)
from MAVProxy.modules.mavproxy_tpcm.eventlog import EventLogger
from MAVProxy.modules.mavproxy_tpcm.parser import LineAssembler, parse_line
from MAVProxy.modules.mavproxy_tpcm.publish import RateLimitedPublisher
from MAVProxy.modules.mavproxy_tpcm.types import TpcmStatus

SESSION_FIRST = "first"
SESSION_LATEST = "latest"
TCP_SESSIONS = (SESSION_FIRST, SESSION_LATEST)

# A GCS that is absent for minutes must not turn into an unbounded backlog of
# stale alarms delivered all at once on connect; latched state replays instead.
_OUTBOUND_MAX = 32
# A GCS waiting on the port is promoted in the same idle cycle that flushes the
# boot notice, so identity would otherwise go out twice.
_START_DEDUP_S = 2.0


@dataclass
class TpcmStats:
    ok_frames: int = 0
    err_frames: int = 0
    encoded: int = 0
    dropped: int = 0
    last_rx_monotonic: Optional[float] = None


@dataclass
class TpcmConfig:
    device: str
    baud_rate: int = 115200
    max_line_bytes: int = 256
    receive_timeout_ms: int = 3000
    target_publish_hz: float = 1.0
    downsample: bool = True
    system_id: int = 1
    component_id: int = 158
    signing_enabled: bool = False
    log_dir: str = "logs"
    error_file: str = "tpcm_error.log"
    log_max_bytes: int = 2_097_152
    log_backup_count: int = 3
    rate_limit_sec: float = 10.0
    statustext_enabled: bool = True
    statustext_throttle_sec: float = 60.0
    parse_report_sec: float = 60.0
    tcp_bind: str = "0.0.0.0"
    tcp_port: int = 6000
    tcp_session: str = SESSION_LATEST
    fc_device: str = "/dev/fc_uart"
    fc_baud_rate: int = 921600
    workdir: Path = field(default_factory=lambda: Path("."))

    @property
    def gcs_tcp_device(self) -> str:
        return f"tcpin:{self.tcp_bind}:{self.tcp_port}"

    @classmethod
    def from_yaml(cls, path: Path, workdir: Optional[Path] = None) -> "TpcmConfig":
        # PyYAML is only in the MAVProxy "recommended" extra, so the import
        # stays here: the caller reports it as a config load failure and the
        # FC link keeps working.
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        t = data.get("tpcm") or {}
        m = data.get("mavlink") or {}
        lg = data.get("logging") or {}
        st = data.get("statustext") or {}
        tcp = data.get("tcp") or {}
        fc = data.get("fc") or {}
        wd = workdir or path.resolve().parent.parent
        return cls(
            device=str(t["device"]),
            baud_rate=int(t.get("baud_rate", 115200)),
            max_line_bytes=int(t.get("max_line_bytes", 256)),
            receive_timeout_ms=int(t.get("receive_timeout_ms", 3000)),
            target_publish_hz=float(t.get("target_publish_hz", 1)),
            downsample=bool(t.get("downsample", True)),
            system_id=int(m.get("system_id", 1)),
            component_id=int(m.get("component_id", 158)),
            signing_enabled=bool(m.get("signing_enabled", False)),
            log_dir=str(lg.get("dir", "logs")),
            error_file=str(lg.get("error_file", "tpcm_error.log")),
            log_max_bytes=int(lg.get("max_bytes", 2_097_152)),
            log_backup_count=int(lg.get("backup_count", 3)),
            rate_limit_sec=float(lg.get("rate_limit_sec", 10)),
            statustext_enabled=bool(st.get("enabled", True)),
            statustext_throttle_sec=float(st.get("throttle_sec", 60)),
            parse_report_sec=float(st.get("parse_report_sec", 60)),
            tcp_bind=str(tcp.get("bind", "0.0.0.0")),
            tcp_port=int(tcp.get("port", 6000)),
            tcp_session=str(tcp.get("session", SESSION_LATEST)).strip().lower(),
            fc_device=str(fc.get("device", "/dev/fc_uart")),
            fc_baud_rate=int(fc.get("baud_rate", 921600)),
            workdir=Path(wd),
        )

    def validate(self) -> Optional[str]:
        if self.component_id == 1:
            return "component_id==1 forbidden"
        # Rejecting an out-of-range id here is what keeps TPCM_STATUS off the
        # air: the STATUSTEXT fallback identity must never carry sensor data.
        if not (0 <= self.component_id <= 255):
            return "mavlink.component_id invalid"
        if not (1 <= self.system_id <= 255):
            return "mavlink.system_id invalid"
        if self.signing_enabled:
            return "signing_enabled not supported in this draft"
        if not self.device:
            return "tpcm.device empty"
        if not self.tcp_bind.strip():
            return "tcp.bind empty"
        if not (1 <= self.tcp_port <= 65535):
            return "tcp.port invalid"
        if self.tcp_session not in TCP_SESSIONS:
            return "tcp.session invalid"
        if not self.fc_device.strip():
            return "fc.device empty"
        if self.fc_baud_rate <= 0:
            return "fc.baud_rate invalid"
        if self.fc_device == self.device:
            return "fc.device equals tpcm.device"
        return None


class TpcmEngine:
    """Byte-pump for TPCM UART/USB. Caller supplies serial read and GCS write."""

    def __init__(
        self,
        cfg: TpcmConfig,
        *,
        also_stdout: bool = False,
        dialect: Any = None,
    ) -> None:
        self.cfg = cfg
        self.stats = TpcmStats()
        self.enabled = False
        self._ser = None
        self._fd: Optional[int] = None
        self._asm = LineAssembler(max_line_bytes=cfg.max_line_bytes)
        self._pub = RateLimitedPublisher(
            target_hz=cfg.target_publish_hz, downsample=cfg.downsample
        )
        self._enc: Optional[MavEncoder] = None
        self._backoff_s = 1.0
        self._next_open_monotonic = 0.0
        self._last_power_source: Optional[int] = None
        self._saw_first_recv = False
        self._saw_first_encode = False
        self._rx_stale = False
        self._boot_monotonic = time.monotonic()
        self._outbound: List[bytes] = []
        # Unresolved conditions, replayed when a GCS session opens. Boot-time
        # faults happen before any GCS exists, so a one-shot send is lost.
        self._latched: "OrderedDict[str, Tuple[int, str]]" = OrderedDict()
        self._notify_last: Dict[str, float] = {}
        self._parse_window_start: Optional[float] = None
        self._parse_window_count = 0
        self._parse_window_reasons: Dict[str, int] = {}
        log_path = (cfg.workdir / cfg.log_dir / cfg.error_file).resolve()
        self.log = EventLogger(
            log_path,
            max_bytes=cfg.log_max_bytes,
            backup_count=cfg.log_backup_count,
            rate_limit_sec=cfg.rate_limit_sec,
            also_stdout=also_stdout,
        )
        self._build_encoder(dialect)
        err = cfg.validate()
        if err:
            self.log.emit("config", f"config_reject {err}")
            self.enabled = False
            self.latch("config", SEV_CRITICAL, f"config rejected: {err}")
        else:
            self.enabled = True
        self.emit_start()

    def _build_encoder(self, dialect: Any) -> None:
        """Arrange a sender.

        TPCM_STATUS is defined in this package, so there is no longer a state
        where the message is unavailable and the engine has to run on
        STATUSTEXT alone.
        """
        try:
            self._enc = MavEncoder(
                system_id=self.cfg.system_id,
                component_id=self.cfg.component_id,
                dialect=dialect,
            )
        except EncodeError:
            # Reachable only for ids validate() rejects, so the engine is about
            # to be disabled and this identity carries STATUSTEXT alone.
            try:
                self._enc = MavEncoder(dialect=dialect)
            except EncodeError:
                self._enc = None

    # -- GCS notification -------------------------------------------------

    def notify(
        self,
        severity: int,
        body: str,
        *,
        key: Optional[str] = None,
        throttle_s: float = 0.0,
        now: Optional[float] = None,
    ) -> bool:
        """Queue one STATUSTEXT. Separate from the log: the operator reads this."""
        if not self.cfg.statustext_enabled or self._enc is None:
            return False
        ts = time.monotonic() if now is None else now
        if key is not None and throttle_s > 0.0:
            last = self._notify_last.get(key)
            if last is not None and (ts - last) < throttle_s:
                return False
            self._notify_last[key] = ts
        try:
            pkt = self._enc.statustext(severity, body)
        except Exception:
            return False
        self._queue(pkt)
        return True

    def _queue(self, pkt: bytes) -> None:
        """One queue for every message so the wire order matches seq order.

        Draining notifications and status packets from separate lists would put
        a lower seq behind a higher one, and mavutil reads that regression as a
        ~255 packet loss spike.
        """
        if len(self._outbound) >= _OUTBOUND_MAX:
            self._outbound.pop(0)
        self._outbound.append(pkt)

    def latch(
        self,
        name: str,
        severity: int,
        body: str,
        *,
        throttle_s: float = 0.0,
        now: Optional[float] = None,
    ) -> None:
        """Send now and keep it for session-open replay until cleared."""
        self._latched[name] = (severity, body)
        self.notify(
            severity,
            body,
            key=name if throttle_s > 0.0 else None,
            throttle_s=throttle_s,
            now=now,
        )

    def clear_latch(self, name: str) -> None:
        self._latched.pop(name, None)
        self._notify_last.pop(name, None)

    def queue_session_replay(self) -> None:
        """Re-send identity plus unresolved faults to a GCS that just connected."""
        self.notify(
            SEV_INFO, self._start_body(), key="start", throttle_s=_START_DEDUP_S
        )
        for severity, body in list(self._latched.values()):
            self.notify(severity, body)

    def take_outbound(self) -> List[bytes]:
        if not self._outbound:
            return []
        out, self._outbound = self._outbound, []
        return out

    def _start_body(self) -> str:
        up = int(max(0.0, time.monotonic() - self._boot_monotonic))
        dev = Path(self.cfg.device).name or self.cfg.device
        return f"up {up}s dev={dev} {self.cfg.target_publish_hz:g}Hz"

    # -- lifecycle --------------------------------------------------------

    def emit_start(self, note: str = "") -> None:
        extra = f" {note}" if note else ""
        self.log.emit(
            "start",
            f"start device={self.cfg.device} baud={self.cfg.baud_rate} "
            f"compid={self.cfg.component_id} enabled={int(self.enabled)}{extra}",
        )
        # Uptime lets the GCS tell a fresh boot from a module reload that kept
        # the TCP session, and from a replay on a late connect.
        self.notify(
            SEV_INFO, self._start_body(), key="start", throttle_s=_START_DEDUP_S
        )

    def emit_stop(self) -> None:
        s = self.stats
        self.log.emit(
            "stop",
            f"stop ok={s.ok_frames} err={s.err_frames} enc={s.encoded} drop={s.dropped}",
        )
        # Caller must flush before closing the GCS socket, which is the only
        # path out; absence of this message never means the relay is alive.
        self.notify(SEV_NOTICE, "relay stopping (planned)")

    @property
    def fd(self) -> Optional[int]:
        return self._fd

    def close_port(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None
        self._fd = None
        self._asm.clear()

    def try_open(self, now: Optional[float] = None) -> bool:
        if not self.enabled:
            return False
        ts = time.monotonic() if now is None else now
        if self._ser is not None:
            return True
        if ts < self._next_open_monotonic:
            return False
        try:
            import serial

            self._ser = serial.Serial(
                self.cfg.device,
                baudrate=self.cfg.baud_rate,
                timeout=0,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            # pyserial may assert DTR/RTS on open; some USB-UART stop TX then.
            try:
                self._ser.dtr = False
                self._ser.rts = False
                self._ser.reset_input_buffer()
            except Exception:
                pass
            # Windows COM: fileno() often raises UnsupportedOperation.
            # Open still succeeds; idle_task polls instead of select_extra.
            self._fd = None
            try:
                self._fd = self._ser.fileno()
            except (AttributeError, OSError, io.UnsupportedOperation):
                self._fd = None
            self._backoff_s = 1.0
            self.log.emit("uart", f"uart_open {self.cfg.device}")
            self.clear_latch("uart")
            return True
        except Exception as exc:
            self._next_open_monotonic = ts + self._backoff_s
            self.log.emit(
                "uart_fail",
                f"uart_open_fail {self.cfg.device} {type(exc).__name__} "
                f"backoff={self._backoff_s:.0f}s",
                rate_limit=True,
            )
            dev = Path(self.cfg.device).name or self.cfg.device
            self.latch(
                "uart",
                SEV_ERROR,
                f"sensor UART {dev} open fail",
                throttle_s=self.cfg.statustext_throttle_sec,
                now=ts,
            )
            self._backoff_s = min(30.0, max(1.0, self._backoff_s * 2))
            self.close_port()
            return False

    def on_readable(self) -> List[bytes]:
        """Read available bytes; return list of MAVLink packets to send to GCS only."""
        if self._ser is None:
            return self.take_outbound()
        try:
            chunk = self._ser.read(1024)
        except Exception as exc:
            self.log.emit("uart_read", f"uart_read_fail {type(exc).__name__}", rate_limit=True)
            self.close_port()
            self._next_open_monotonic = time.monotonic() + self._backoff_s
            return self.take_outbound()
        if not chunk:
            return self.take_outbound()
        return self.ingest_bytes(chunk, time.monotonic())

    def ingest_bytes(self, chunk: bytes, now_monotonic: float) -> List[bytes]:
        for raw in self._asm.feed(chunk):
            status, perr = parse_line(raw, max_line_bytes=self.cfg.max_line_bytes)
            if status is None:
                self.stats.err_frames += 1
                reason = perr.reason if perr else "unknown"
                preview = raw[:64].decode("ascii", errors="replace")
                extra = "..." if len(raw) > 64 else ""
                self.log.emit(
                    f"parse:{reason}",
                    f"parse_error {reason} n={len(raw)} ascii={preview!r}{extra}",
                    rate_limit=True,
                    now=now_monotonic,
                )
                self._count_parse_error(reason, now_monotonic)
                continue
            self.stats.ok_frames += 1
            was_stale = self._rx_stale
            self.stats.last_rx_monotonic = now_monotonic
            if was_stale:
                self._rx_stale = False
                silent_ms = int(self.cfg.receive_timeout_ms)
                self.log.emit(
                    "rx_resume",
                    f"rx_resume after_stale_timeout_ms={silent_ms} "
                    f"src={status.power_source} "
                    f"T={status.temperature:.1f}C Bat={status.battery_voltage:.1f}V",
                )
                self.clear_latch("rx_stale")
                self.notify(SEV_INFO, "sensor link restored")
            if not self._saw_first_recv:
                self._saw_first_recv = True
                self.log.emit(
                    "recv",
                    f"recv src={status.power_source} "
                    f"T={status.temperature:.1f}C Bat={status.battery_voltage:.1f}V",
                )
            elif (
                self._last_power_source is not None and
                status.power_source != self._last_power_source
            ):
                self.log.emit(
                    "power_source",
                    f"power_source {self._last_power_source}->{status.power_source} "
                    f"T={status.temperature:.1f}C Bat={status.battery_voltage:.1f}V",
                )
                self.notify(
                    SEV_NOTICE,
                    f"power src {self._last_power_source}->{status.power_source}",
                )
            self._last_power_source = status.power_source
            self._maybe_encode(status, now_monotonic)
        return self.take_outbound()

    def _count_parse_error(self, reason: str, now_monotonic: float) -> None:
        if self._parse_window_start is None:
            self._parse_window_start = now_monotonic
        self._parse_window_count += 1
        head = reason.split(":", 1)[0]
        self._parse_window_reasons[head] = self._parse_window_reasons.get(head, 0) + 1

    def _report_parse_errors(self, now_monotonic: float) -> None:
        """Aggregate instead of per-frame: raw bytes are a log concern, not a GCS one."""
        if self._parse_window_start is None or self._parse_window_count == 0:
            return
        window = self.cfg.parse_report_sec
        if window <= 0.0:
            return
        if (now_monotonic - self._parse_window_start) < window:
            return
        top = max(self._parse_window_reasons.items(), key=lambda kv: kv[1])[0]
        self.notify(
            SEV_WARNING,
            f"{self._parse_window_count} bad frames/{int(window)}s ({top})",
        )
        self._parse_window_start = None
        self._parse_window_count = 0
        self._parse_window_reasons = {}

    def _maybe_encode(self, status: TpcmStatus, now_monotonic: float) -> None:
        decision = self._pub.consider(status, now_monotonic)
        if not decision.should_publish:
            return
        if not self.enabled or self._enc is None:
            return
        try:
            pkt = self._enc.tpcm_status(status)
        except EncodeError as exc:
            self.stats.dropped += 1
            self.log.emit("encode", f"encode_fail {exc}", rate_limit=True)
            return
        self.stats.encoded += 1
        if not self._saw_first_encode:
            self._saw_first_encode = True
            self.log.emit(
                "encode_first",
                f"encode_first len={len(pkt)} reason={decision.reason} "
                f"src={status.power_source}",
            )
        self._queue(pkt)

    def _check_rx_stale(self, now_monotonic: float) -> None:
        """Pin unplug often leaves the fd open with silence — log that gap."""
        if self.stats.last_rx_monotonic is None:
            return
        timeout_s = self.cfg.receive_timeout_ms / 1000.0
        silent_s = now_monotonic - self.stats.last_rx_monotonic
        if silent_s <= timeout_s:
            return
        self._asm.clear()
        if self._rx_stale:
            return
        self._rx_stale = True
        self.log.emit(
            "rx_stale",
            f"rx_stale device={self.cfg.device} "
            f"silent_ms={int(silent_s * 1000)} "
            f"timeout_ms={self.cfg.receive_timeout_ms}",
        )
        self.latch("rx_stale", SEV_ERROR, f"sensor silent {silent_s:.1f}s")

    def idle(self, now: Optional[float] = None, *, poll_read: bool = True) -> List[bytes]:
        """Reconnect / stale handling. Optionally poll serial (Windows, no select)."""
        ts = time.monotonic() if now is None else now
        if not self.enabled:
            return self.take_outbound()
        self._report_parse_errors(ts)
        if self._ser is None:
            self.try_open(ts)
            return self.take_outbound()
        self._check_rx_stale(ts)
        if not poll_read:
            return self.take_outbound()
        return self.on_readable()

    def fault(self, where: str, exc: BaseException) -> None:
        self.log.emit(
            f"fault:{where}",
            f"fault {where} {type(exc).__name__}",
            rate_limit=True,
        )
        self.notify(
            SEV_CRITICAL,
            f"internal fault {where}",
            key=f"fault:{where}",
            throttle_s=self.cfg.statustext_throttle_sec,
        )
        self.close_port()
        self._next_open_monotonic = time.monotonic() + self._backoff_s
        self._backoff_s = min(30.0, max(1.0, self._backoff_s * 2))
