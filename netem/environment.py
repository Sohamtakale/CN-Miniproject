"""Pick and manage the network-emulation mode for a test run.

Modes:

* ``netem``    - Linux ``tc netem`` on an interface (needs root/sudo).
* ``software`` - the built-in impairment proxy (:mod:`netem.emulator`), run as
  a subprocess; works everywhere without privileges.
* ``direct``   - no emulation at all (clients talk straight to the servers).
* ``auto``     - ``netem`` if available, otherwise ``software``.

:class:`NetworkEnvironment` is a context manager that applies the conditions
on entry and always removes them on exit, and tells the client which
host/port to connect to.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from types import TracebackType
from typing import Any

from common import config
from netem.netem_py import Conditions, NetemController, netem_available

log = logging.getLogger("environment")

MODES = ("auto", "netem", "software", "direct")


@dataclass(frozen=True)
class PortSet:
    """Server and emulator ports used by one harness/dashboard instance."""

    tcp: int = config.TCP_PORT
    udp: int = config.UDP_PORT
    emu_tcp: int = config.EMU_TCP_PORT
    emu_udp: int = config.EMU_UDP_PORT

    def server(self, protocol: str) -> int:
        return self.tcp if protocol.upper() == "TCP" else self.udp

    def emulator(self, protocol: str) -> int:
        return self.emu_tcp if protocol.upper() == "TCP" else self.emu_udp


HARNESS_PORTS = PortSet()
DASHBOARD_PORTS = PortSet(config.DASH_TCP_PORT, config.DASH_UDP_PORT, config.DASH_EMU_TCP_PORT, config.DASH_EMU_UDP_PORT)


@lru_cache(maxsize=8)
def resolve_mode(requested: str = "auto", iface: str = config.DEFAULT_IFACE) -> tuple[str, str]:
    """Return ``(mode, reason)`` for the requested mode."""
    if requested not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if requested in ("software", "direct"):
        return requested, "selected explicitly"
    ok, reason = netem_available(iface)
    if ok:
        return "netem", reason
    if requested == "netem":
        raise RuntimeError(f"netem requested but unavailable: {reason}")
    return "software", f"netem unavailable ({reason}); using software emulator"


@dataclass
class NetworkEnvironment:
    """Context manager that applies ``conditions`` for one protocol."""

    conditions: Conditions
    protocol: str
    mode: str = "software"
    ports: PortSet = field(default_factory=PortSet)
    host: str = config.DEFAULT_HOST
    iface: str = config.DEFAULT_IFACE
    seed: int | None = None

    def __post_init__(self) -> None:
        self.connect_port = self.ports.server(self.protocol)
        self.emu_stats: dict[str, Any] = {}
        self._proc: subprocess.Popen[str] | None = None
        self._netem: NetemController | None = None

    def __enter__(self) -> "NetworkEnvironment":
        if self.mode == "netem":
            self._netem = NetemController(self.iface)
            self._netem.apply(self.conditions)
        elif self.mode == "software":
            self._start_emulator()
            self.connect_port = self.ports.emulator(self.protocol)
        elif self.mode != "direct":
            raise ValueError(f"unresolved mode {self.mode!r}; call resolve_mode() first")
        return self

    def _start_emulator(self) -> None:
        c = self.conditions
        cmd = [sys.executable, str(config.PROJECT_ROOT / "netem" / "emulator.py"),
               "--protocol", self.protocol.lower(),
               "--listen-port", str(self.ports.emulator(self.protocol)),
               "--target-host", self.host, "--target-port", str(self.ports.server(self.protocol)),
               "--delay", f"{c.delay_ms:g}", "--jitter", f"{c.effective_jitter_ms:g}",
               "--loss", f"{c.loss_pct:g}", "--rate", f"{c.rate_mbps:g}"]
        if self.seed is not None:
            cmd += ["--seed", str(self.seed)]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line: list[str] = []
        t = threading.Thread(target=lambda: line.append(self._proc.stdout.readline()), daemon=True)  # type: ignore[union-attr]
        t.start()
        t.join(config.EMU_READY_TIMEOUT_S)
        if not line or not line[0].startswith("READY"):
            err = ""
            if self._proc.poll() is not None:
                err = self._proc.stderr.read() if self._proc.stderr else ""
            self._kill()
            raise RuntimeError(f"software emulator failed to start: {err.strip()[-300:]}")

    def _kill(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
            self._proc.wait()

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        if self._netem is not None:
            self._netem.clear()
        if self._proc is not None:
            try:
                self._proc.terminate()
                out, _ = self._proc.communicate(timeout=5)
                for ln in out.splitlines():
                    if ln.startswith("STATS "):
                        self.emu_stats = json.loads(ln[6:])
            except (subprocess.TimeoutExpired, json.JSONDecodeError):
                self._kill()
