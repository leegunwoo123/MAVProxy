"""TPCM relay: sensor UART/USB ASCII -> TPCM_STATUS(5600) to the GCS only.

A Tethered Power Control Manager reports AC/DC rails, temperature, battery and
which source is carrying the load. This module reads those ASCII lines from a
serial port and republishes them as MAVLink to the GCS outputs. TPCM_STATUS is
never written to the master link: the flight controller has no use for it and
the extra traffic would compete with telemetry on the same UART.

Configuration is a YAML file, found via $TPCM_CONFIG or ~/.mavproxy/tpcm.yaml.

AP_FLAKE8_CLEAN
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from MAVProxy.modules.lib import mp_module
from MAVProxy.modules.lib import mp_util
from MAVProxy.modules.mavproxy_tpcm.encode import SEV_CRITICAL, dialect_xml_path
from MAVProxy.modules.mavproxy_tpcm.gcs import GcsTcpListener
from MAVProxy.modules.mavproxy_tpcm.runtime import TpcmConfig, TpcmEngine

CONFIG_NAME = "tpcm.yaml"


def default_config_path() -> Path:
    """Where to look for tpcm.yaml.

    $TPCM_CONFIG first so a systemd unit can name the file it deploys, then the
    MAVProxy dot directory, then the working directory.
    """
    env = os.environ.get("TPCM_CONFIG")
    if env:
        return Path(env)
    dot = Path(mp_util.dot_mavproxy(CONFIG_NAME))
    if dot.is_file():
        return dot
    local = Path(CONFIG_NAME)
    if local.is_file():
        return local
    return dot


class TpcmModule(mp_module.MPModule):
    def __init__(self, mpstate):
        super(TpcmModule, self).__init__(mpstate, "tpcm", "TPCM STATUS relay")
        self.engine: Optional[TpcmEngine] = None
        self.gcs: Optional[GcsTcpListener] = None
        self._use_select = False
        self._registered_fd: Optional[int] = None
        cfg_path = default_config_path()
        workdir = Path(mpstate.status.logdir or mp_util.dot_mavproxy())
        try:
            cfg = TpcmConfig.from_yaml(cfg_path, workdir=workdir)
        except Exception as exc:
            print(f"tpcm: config load failed ({cfg_path}): {exc}")
            # Still open the GCS listener: without it a broken yaml would cost
            # the FC telemetry link as well, not just the TPCM data.
            self._ensure_gcs_tcp(TpcmConfig(device=""))
            return
        self.engine = TpcmEngine(cfg, also_stdout=False)
        self._ensure_gcs_tcp(cfg)
        self._ensure_fc_master(cfg)
        if not self.engine.enabled:
            print("tpcm: disabled (config)")
            return
        self.engine.try_open()
        self._try_register_select()
        self.add_command("tpcm", self.cmd_tpcm, "tpcm status", ["status", "xml"])

    def _ensure_gcs_tcp(self, cfg: TpcmConfig) -> None:
        """Own the GCS listener from yaml unless --out already took the address.

        tcp.session only applies to a listener we opened. A plain --out=tcpin
        binds the port first and keeps its first client.
        """
        if not cfg.tcp_bind.strip() or not (1 <= cfg.tcp_port <= 65535):
            return
        addr = cfg.gcs_tcp_device
        outputs = getattr(self.mpstate, "mav_outputs", None)
        if outputs is None:
            self._gcs_log("gcs_listen_fail mav_outputs missing")
            return
        for out in outputs:
            if getattr(out, "address", None) == addr:
                self._gcs_log(f"gcs_listen already {addr} session=external")
                return
        try:
            conn = GcsTcpListener(
                f"{cfg.tcp_bind}:{cfg.tcp_port}",
                session=cfg.tcp_session,
                log=self._gcs_log,
                on_session=self._on_gcs_session,
                source_system=self.settings.source_system,
                source_component=self.settings.source_component,
            )
        except Exception as exc:
            self._gcs_log(f"gcs_listen_fail {addr} {type(exc).__name__}")
            return
        self.gcs = conn
        outputs.append(conn)
        try:
            mp_util.child_fd_list_add(conn.listen.fileno())
        except Exception:
            pass
        self._gcs_log(f"gcs_listen {addr} session={cfg.tcp_session}")

    def _close_gcs(self) -> None:
        if self.gcs is None:
            return
        outputs = getattr(self.mpstate, "mav_outputs", None) or []
        if self.gcs in outputs:
            outputs.remove(self.gcs)
        try:
            self.gcs.close()
        except Exception:
            pass
        self.gcs = None

    def _poll_gcs(self) -> None:
        if self.gcs is None:
            return
        try:
            self.gcs.poll()
        except Exception as exc:
            self._gcs_log(f"gcs_poll_fail {type(exc).__name__}", rate_limit=True)

    def _on_gcs_session(self) -> None:
        """Replay identity and unresolved faults; idle_task flushes the queue.

        Only fires for a listener we own. With an external --out=tcpin there is
        no hook, so boot-time faults stay in the log only.
        """
        if self.engine is not None:
            self.engine.queue_session_replay()

    def _ensure_fc_master(self, cfg: TpcmConfig) -> None:
        """Open FC UART from yaml unless --master already has the same device."""
        if not cfg.fc_device.strip() or cfg.fc_baud_rate <= 0:
            return
        if cfg.fc_device == cfg.device:
            # validate() rejects this too, so config_reject already told the
            # GCS; a second CRITICAL for one root cause is noise.
            self._fc_log("fc_open_fail device_conflict")
            return
        masters = getattr(self.mpstate, "mav_master", None) or []
        for master in masters:
            if getattr(master, "address", None) == cfg.fc_device:
                self._fc_log(f"fc_open already {cfg.fc_device}")
                self._fc_ok()
                return
        link = self.mpstate.module("link")
        if link is None:
            self._fc_log("fc_open_fail link module missing")
            self._fc_fault(cfg.fc_device)
            return
        try:
            self.mpstate.settings.baudrate = int(cfg.fc_baud_rate)
        except Exception:
            pass
        ok = False
        try:
            ok = bool(link.link_add(cfg.fc_device))
        except Exception as exc:
            self._fc_log(f"fc_open_fail {cfg.fc_device} {type(exc).__name__}")
            self._fc_fault(cfg.fc_device)
            return
        if ok:
            self._fc_log(f"fc_open {cfg.fc_device} baud={cfg.fc_baud_rate}")
            self._fc_ok()
        else:
            self._fc_log(f"fc_open_fail {cfg.fc_device}")
            self._fc_fault(cfg.fc_device)

    def _fc_fault(self, device: str) -> None:
        """The relay is the only party that can explain FC silence to the GCS."""
        if self.engine is None:
            return
        self.engine.latch("fc", SEV_CRITICAL, f"FC UART {device} open failed")

    def _fc_ok(self) -> None:
        if self.engine is not None:
            self.engine.clear_latch("fc")

    def _gcs_log(self, body: str, *, rate_limit: bool = False) -> None:
        if self.engine is not None:
            self.engine.log.emit(body.split(" ", 1)[0], body, rate_limit=rate_limit)
        else:
            print(f"tpcm: {body}")

    def _fc_log(self, body: str) -> None:
        if self.engine is not None:
            self.engine.log.emit("fc", body)
        else:
            print(f"tpcm: {body}")

    def _try_register_select(self) -> None:
        if self.engine is None or self.engine.fd is None:
            self._use_select = False
            return
        # POSIX selectable UART; Windows COM is polled in idle_task.
        if os.name == "nt":
            self._use_select = False
            return
        fd = self.engine.fd
        self.mpstate.select_extra[fd] = (self._on_fd, None)
        self._registered_fd = fd
        self._use_select = True

    def _unregister_select(self) -> None:
        if self._registered_fd is None:
            return
        self.mpstate.select_extra.pop(self._registered_fd, None)
        self._registered_fd = None
        self._use_select = False

    def _on_fd(self, _args) -> None:
        if self.engine is None:
            return
        try:
            self._send_gcs_only(self.engine.on_readable())
        except Exception as exc:
            self.engine.fault("select_extra", exc)
            self._unregister_select()
            self._send_gcs_only(self.engine.take_outbound())

    def _send_gcs_only(self, packets: List[bytes]) -> None:
        """Write TPCM_STATUS and STATUSTEXT to mav_outputs only — never master.

        stats.dropped therefore counts both; it has always meant "could not
        hand to any output" rather than "lost sensor frame".
        """
        if not packets:
            return
        outputs = getattr(self.mpstate, "mav_outputs", None) or []
        if not outputs:
            if self.engine:
                self.engine.stats.dropped += len(packets)
            return
        for pkt in packets:
            sent_any = False
            for out in list(outputs):
                try:
                    out.write(pkt)
                    sent_any = True
                except Exception:
                    if self.engine:
                        self.engine.stats.dropped += 1
                        self.engine.log.emit(
                            "gcs_write",
                            "gcs_write_fail",
                            rate_limit=True,
                        )
            if not sent_any and self.engine:
                self.engine.stats.dropped += 1

    def idle_task(self) -> None:
        # Before the engine check: the listener must keep accepting even when
        # the TPCM side is disabled, or FC telemetry loses the GCS.
        self._poll_gcs()
        if self.engine is None:
            return
        if not self.engine.enabled:
            # Config faults live here; they still need a way out.
            self._send_gcs_only(self.engine.take_outbound())
            return
        try:
            # Always poll. select_extra alone can sit on an open UART with zero
            # recv (BBB ttyS*) while the FC --master link still works.
            if not self._use_select and self.engine.fd is not None and os.name != "nt":
                self._try_register_select()
            self._send_gcs_only(self.engine.idle(poll_read=True))
        except Exception as exc:
            self.engine.fault("idle_task", exc)
            self._unregister_select()
            self._send_gcs_only(self.engine.take_outbound())

    def cmd_tpcm(self, args) -> None:
        if not args or args[0] == "status":
            if self.engine is None:
                print("tpcm: no engine")
                return
            s = self.engine.stats
            print(
                f"tpcm enabled={self.engine.enabled} device={self.engine.cfg.device} "
                f"fc={self.engine.cfg.fc_device}@{self.engine.cfg.fc_baud_rate} "
                f"tcp={self.engine.cfg.gcs_tcp_device} "
                f"session={self.gcs.session if self.gcs else 'external'} "
                f"peer={self.gcs.peer if self.gcs else '-'} "
                f"ok={s.ok_frames} err={s.err_frames} enc={s.encoded} drop={s.dropped} "
                f"select={self._use_select} compid={self.engine.cfg.component_id}"
            )
            return
        if args[0] == "xml":
            # The GCS needs this definition to decode 5600; the relay does not
            # read it, so point at it rather than making anyone go hunting.
            print(dialect_xml_path())
            return
        print("Usage: tpcm <status|xml>")

    def unload(self) -> None:
        self._unregister_select()
        if self.engine is not None:
            # Must run before _close_gcs(): once the socket is gone, write()
            # returns on port is None and the message vanishes.
            self.engine.emit_stop()
            self._send_gcs_only(self.engine.take_outbound())
        self._close_gcs()
        if self.engine is not None:
            self.engine.close_port()
            self.engine.log.close()
        super(TpcmModule, self).unload()


def init(mpstate):
    return TpcmModule(mpstate)
