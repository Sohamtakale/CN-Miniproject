"""Start/stop the TCP and UDP servers as subprocesses."""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import threading
from types import TracebackType

from common import config
from common.utils import port_in_use
from netem.environment import PortSet

log = logging.getLogger("servers")


class ServerManager:
    """Context manager that ensures both servers are running.

    If a port is already bound (e.g. servers started manually) the existing
    server is reused and left running on exit.
    """

    def __init__(self, ports: PortSet = PortSet(), host: str = config.DEFAULT_HOST) -> None:
        self.ports, self.host = ports, host
        self.procs: list[subprocess.Popen[str]] = []

    def start(self) -> "ServerManager":
        specs = [("tcp", self.ports.tcp, socket.SOCK_STREAM), ("udp", self.ports.udp, socket.SOCK_DGRAM)]
        for name, port, kind in specs:
            if port_in_use(port, self.host, kind):
                log.info("%s port %d already in use - reusing running server", name.upper(), port)
                continue
            script = config.PROJECT_ROOT / name / f"{name}_server.py"
            proc = subprocess.Popen([sys.executable, str(script), "--host", self.host, "--port", str(port)],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            line: list[str] = []
            t = threading.Thread(target=lambda p=proc: line.append(p.stdout.readline()), daemon=True)  # type: ignore[union-attr]
            t.start()
            t.join(config.SERVER_READY_TIMEOUT_S)
            if not line or not line[0].startswith("READY"):
                proc.kill()
                raise RuntimeError(f"{name} server failed to start on port {port}")
            self.procs.append(proc)
            log.info("started %s server on port %d (pid %d)", name.upper(), port, proc.pid)
        return self

    def stop(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        self.procs.clear()

    def __enter__(self) -> "ServerManager":
        return self.start()

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.stop()
