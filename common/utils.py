"""Shared helpers: logging, sockets, pacing, live counters, TCP retransmission counters."""

from __future__ import annotations

import logging
import platform
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from common import config
from common.metrics import ClientResult

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(verbose: bool = False, name: str = "tcpudp") -> logging.Logger:
    """Configure root logging once and return a named logger."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=LOG_FORMAT, datefmt="%H:%M:%S")
    return logging.getLogger(name)


def tune_socket_buffers(sock: socket.socket, size: int = config.SOCKET_BUFFER_BYTES) -> None:
    """Enlarge send/receive buffers; silently keep OS defaults if refused."""
    for opt in (socket.SO_RCVBUF, socket.SO_SNDBUF):
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, size)
        except OSError:
            # macOS caps buffers (kern.ipc.maxsockbuf); halve until accepted.
            s = size // 2
            while s >= 65536:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, opt, s)
                    break
                except OSError:
                    s //= 2


def wait_for_tcp_port(host: str, port: int, timeout: float) -> bool:
    """Poll until something accepts TCP connections on ``host:port``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def port_in_use(port: int, host: str = config.DEFAULT_HOST, kind: int = socket.SOCK_STREAM) -> bool:
    """True if a server already owns ``host:port``.

    TCP: something accepts connections (a plain bind test would also fail on
    harmless TIME_WAIT leftovers). UDP: binding the port fails.
    """
    if kind == socket.SOCK_STREAM:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            return False
    with socket.socket(socket.AF_INET, kind) as s:
        try:
            s.bind((host, port))
            return False
        except OSError:
            return True


class Pacer:
    """Spaces out sends to a target packets-per-second rate (0 = unlimited)."""

    def __init__(self, rate_pps: float) -> None:
        self.interval = 1.0 / rate_pps if rate_pps and rate_pps > 0 else 0.0
        self.start = time.perf_counter()

    def wait(self, index: int) -> None:
        """Block until packet ``index`` is due."""
        if not self.interval:
            return
        delay = self.start + index * self.interval - time.perf_counter()
        if delay > config.PACING_SLEEP_THRESHOLD_S:
            time.sleep(delay)
        # if we are late we send immediately (catch-up burst)


@dataclass
class LiveCounters:
    """Thread-safe counters that client threads update for live dashboards."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    sent: int = 0
    sent_bytes: int = 0
    acked: int = 0
    acked_bytes: int = 0
    new_rtts: list[float] = field(default_factory=list)

    def on_send(self, nbytes: int) -> None:
        with self.lock:
            self.sent += 1
            self.sent_bytes += nbytes

    def on_ack(self, nbytes: int, rtt_ms: float) -> None:
        with self.lock:
            self.acked += 1
            self.acked_bytes += nbytes
            self.new_rtts.append(rtt_ms)

    def snapshot(self) -> dict[str, float]:
        """Return totals and drain the RTT samples gathered since last call."""
        with self.lock:
            rtts, self.new_rtts = self.new_rtts, []
            return {"sent": self.sent, "sent_bytes": self.sent_bytes, "acked": self.acked,
                    "acked_bytes": self.acked_bytes, "rtts": rtts}


ProgressCallback = Callable[[dict[str, float]], None]


def run_parallel_clients(
    worker: Callable[[int, LiveCounters], ClientResult],
    num_clients: int,
    progress_cb: ProgressCallback | None = None,
    timeout_s: float = config.RUN_TIMEOUT_S,
) -> list[ClientResult]:
    """Run ``worker(client_id, counters)`` in ``num_clients`` threads.

    While the threads run, ``progress_cb`` is called every
    ``config.LIVE_SAMPLE_INTERVAL_S`` with a sample containing elapsed time,
    instantaneous ACKed throughput, new RTT samples and an ACK-based loss
    estimate. Clients still running after ``timeout_s`` are reported as errors.
    """
    counters = LiveCounters()
    results: list[ClientResult | None] = [None] * num_clients

    def _run(cid: int) -> None:
        try:
            results[cid] = worker(cid, counters)
        except Exception as exc:  # never lose a whole run to one thread
            results[cid] = ClientResult(cid, error=f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=_run, args=(i,), daemon=True, name=f"client-{i}") for i in range(num_clients)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    last_bytes, last_t = 0, t0
    while True:
        alive = [t for t in threads if t.is_alive()]
        if not alive:
            break
        if time.perf_counter() - t0 > timeout_s:  # watchdog: never hang the whole matrix
            for i, t in enumerate(threads):
                if t.is_alive() and results[i] is None:
                    results[i] = ClientResult(i, error=f"run exceeded {timeout_s:.0f} s watchdog")
            return [r if r is not None else ClientResult(i, error="no result") for i, r in enumerate(results)]
        alive[0].join(timeout=config.LIVE_SAMPLE_INTERVAL_S)
        if progress_cb is not None:
            now = time.perf_counter()
            snap = counters.snapshot()
            dt = max(now - last_t, 1e-6)
            snap["elapsed_s"] = now - t0
            snap["throughput_mbps"] = (snap["acked_bytes"] - last_bytes) * 8 / dt / 1e6
            snap["est_loss_pct"] = (1 - snap["acked"] / snap["sent"]) * 100 if snap["sent"] else 0.0
            last_bytes, last_t = snap["acked_bytes"], now
            progress_cb(snap)
    for t in threads:
        t.join()
    return [r if r is not None else ClientResult(i, error="no result") for i, r in enumerate(results)]


# --------------------------------------------------------------------------- #
# TCP retransmission counters (system-wide, best effort)
# --------------------------------------------------------------------------- #
_NETSTAT_RETRANS = re.compile(r"(\d+)\s+data packets?\s+\(\d+ bytes?\)\s+retransmitted")


def read_tcp_retrans() -> tuple[int | None, str]:
    """Return ``(RetransSegs, source)`` or ``(None, "n/a")`` if unavailable.

    Linux: ``/proc/net/snmp`` (Tcp: RetransSegs). macOS/BSD: ``netstat -s -p tcp``.
    The counter is system-wide, so concurrent traffic adds noise.
    """
    snmp = Path("/proc/net/snmp")
    try:
        if snmp.exists():
            lines = [ln.split() for ln in snmp.read_text().splitlines() if ln.startswith("Tcp:")]
            if len(lines) >= 2:
                hdr, vals = lines[0], lines[1]
                return int(vals[hdr.index("RetransSegs")]), "snmp"
        if platform.system() == "Darwin":
            out = subprocess.run(["netstat", "-s", "-p", "tcp"], capture_output=True, text=True, timeout=5).stdout
            m = _NETSTAT_RETRANS.search(out)
            if m:
                return int(m.group(1)), "netstat"
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return None, "n/a"


def fmt_bw(mbps: float) -> str:
    """Human label for a bandwidth setting (0 = unlimited)."""
    if not mbps:
        return "unlimited"
    return f"{mbps:g} Mbps"
