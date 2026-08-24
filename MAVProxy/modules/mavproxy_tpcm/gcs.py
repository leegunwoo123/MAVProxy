"""GCS-side TCP listener for the tpcm module.

Split out of the module itself because it is plain pymavlink: it needs no
mpstate and can be exercised against a socket on its own.

AP_FLAKE8_CLEAN
"""

from __future__ import annotations

import errno
import select
import socket
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from pymavlink import mavutil

from MAVProxy.modules.mavproxy_tpcm.runtime import SESSION_LATEST

_MAVLINK_MAGIC = (0xFD, 0xFE)
# A challenger must look like MAVLink before it may take the session, so a bare
# connect from a port scan or a health check cannot drop the GCS in flight.
_PROBE_MAX_BYTES = 512
_PROBE_TIMEOUT_S = 30.0
_MAX_PENDING = 4
# A yanked cable sends no FIN. Without probes the dead session holds the slot
# until the kernel default, which is two hours.
_KEEPALIVE_IDLE_S = 10
_KEEPALIVE_INTVL_S = 5
_KEEPALIVE_COUNT = 3
_SHORT_WRITE_LOG_S = 10.0


def _errname(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno, str(exc.errno))


def _peer_name(addr) -> str:
    try:
        return f"{addr[0]}:{addr[1]}"
    except Exception:
        return str(addr)


def _tune_socket(sock: socket.socket) -> None:
    sock.setblocking(False)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass
    for name, value in (
        ("TCP_KEEPIDLE", _KEEPALIVE_IDLE_S),
        ("TCP_KEEPINTVL", _KEEPALIVE_INTVL_S),
        ("TCP_KEEPCNT", _KEEPALIVE_COUNT),
    ):
        opt = getattr(socket, name, None)
        if opt is None:
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, opt, value)
        except OSError:
            pass
    try:
        mavutil.set_close_on_exec(sock.fileno())
    except Exception:
        pass


@dataclass
class _Pending:
    """A connection that has not earned the session yet."""

    sock: socket.socket
    peer: str
    deadline: float
    buf: bytes = b""


class GcsTcpListener(mavutil.mavtcpin):
    """tcpin that can hand the session to a GCS that shows up later.

    pymavlink keeps the first accepted socket forever: it does not re-accept
    after a clean FIN, so a reconnecting GCS is met with silence. Connections
    are polled from the module idle task rather than the main select loop,
    because MAVProxy watches exactly one fd per output.
    """

    def __init__(
        self,
        device: str,
        *,
        session: str = SESSION_LATEST,
        log: Optional[Callable[[str], None]] = None,
        on_session: Optional[Callable[[], None]] = None,
        source_system: int = 255,
        source_component: int = 0,
    ) -> None:
        super(GcsTcpListener, self).__init__(
            device,
            source_system=source_system,
            source_component=source_component,
        )
        self.session = session
        self._log = log
        self._on_session = on_session
        self._pending: List[_Pending] = []
        self._carry = b""
        self._peer: Optional[str] = None
        self._closed = False
        self._last_short_write = 0.0
        # mavtcpin leaves fd on the listen socket, which would make MAVProxy
        # read the connection backlog as if it were GCS traffic.
        self.fd = None

    @property
    def peer(self) -> str:
        return self._peer or "-"

    def poll(self, now: Optional[float] = None) -> None:
        """Accept and promote sessions. Driven by the module idle task."""
        if self._closed:
            return
        ts = time.monotonic() if now is None else now
        watch = [entry.sock for entry in self._pending]
        if self.port is None or self.session == SESSION_LATEST:
            watch.append(self.listen)
        if not watch:
            return
        try:
            ready, _, _ = select.select(watch, [], [], 0)
        except (OSError, ValueError):
            return
        if self.listen in ready:
            self._accept_all(ts)
        for entry in list(self._pending):
            if entry.sock in ready:
                self._probe(entry)
        for entry in list(self._pending):
            if ts >= entry.deadline:
                self._drop_pending(entry, "probe_timeout")

    def _accept_all(self, now: float) -> None:
        while True:
            try:
                sock, addr = self.listen.accept()
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    self._emit(f"gcs_accept_fail {_errname(exc)}")
                return
            peer = _peer_name(addr)
            _tune_socket(sock)
            if self.port is None:
                # Nothing to protect, and a GCS that waits for a heartbeat
                # before sending would never pass the probe below.
                self._promote(sock, peer, b"")
                continue
            if len(self._pending) >= _MAX_PENDING:
                self._drop_pending(self._pending[0], "pending_overflow")
            self._pending.append(_Pending(sock, peer, now + _PROBE_TIMEOUT_S))
            self._emit(f"gcs_pending {peer}")

    def _probe(self, entry: _Pending) -> None:
        try:
            data = entry.sock.recv(256)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            self._drop_pending(entry, f"probe_{_errname(exc)}")
            return
        if not data:
            self._drop_pending(entry, "probe_eof")
            return
        entry.buf += data
        for offset, byte in enumerate(entry.buf):
            if byte in _MAVLINK_MAGIC:
                self._pending.remove(entry)
                self._promote(entry.sock, entry.peer, entry.buf[offset:])
                return
        if len(entry.buf) >= _PROBE_MAX_BYTES:
            self._drop_pending(entry, "probe_not_mavlink")

    def _promote(self, sock: socket.socket, peer: str, carry: bytes) -> None:
        previous = self._peer
        if self.port is not None:
            self._close_active("replaced")
        self.port = sock
        self.fd = sock.fileno()
        self._carry = carry
        self._peer = peer
        if previous:
            self._emit(f"gcs_session_switch {previous} -> {peer}")
        else:
            self._emit(f"gcs_session_open {peer}")
        if self._on_session is not None:
            try:
                self._on_session()
            except Exception:
                pass

    def _drop_pending(self, entry: _Pending, reason: str) -> None:
        if entry in self._pending:
            self._pending.remove(entry)
        try:
            entry.sock.close()
        except OSError:
            pass
        self._emit(f"gcs_pending_drop {entry.peer} {reason}")

    def _close_active(self, reason: str) -> None:
        peer = self.peer
        try:
            self.port.close()
        except OSError:
            pass
        self.port = None
        self.fd = None
        self._carry = b""
        self._peer = None
        self._emit(f"gcs_session_close {peer} {reason}")

    def recv(self, n: Optional[int] = None) -> bytes:
        if self.port is None:
            return b""
        if self._carry:
            data, self._carry = self._carry, b""
            return data
        if n is None:
            n = self.mav.bytes_needed()
        try:
            data = self.port.recv(n)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return b""
            self._close_active(f"recv_{_errname(exc)}")
            return b""
        if not data:
            # Base mavtcpin ignores EOF and then spins on a readable dead fd.
            self._close_active("eof")
            return b""
        return data

    def write(self, buf: bytes) -> None:
        if self.port is None:
            return
        try:
            sent = self.port.send(buf)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                self._note_short_write("send_buffer_full")
                return
            self._close_active(f"write_{_errname(exc)}")
            return
        if sent < len(buf):
            self._note_short_write(f"short={sent}/{len(buf)}")

    def _note_short_write(self, detail: str) -> None:
        now = time.monotonic()
        if now - self._last_short_write < _SHORT_WRITE_LOG_S:
            return
        self._last_short_write = now
        self._emit(f"gcs_write_partial {self.peer} {detail}")

    def close(self) -> None:
        self._closed = True
        for entry in list(self._pending):
            self._drop_pending(entry, "shutdown")
        if self.port is not None:
            self._close_active("shutdown")
        try:
            self.listen.close()
        except OSError:
            pass

    def _emit(self, body: str) -> None:
        if self._log is not None:
            self._log(body)
