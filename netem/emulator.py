"""Software network emulator: a UDP/TCP proxy that injects delay, jitter, loss
and bandwidth limits. Used when Linux ``tc netem`` is unavailable (macOS,
Windows, no sudo).

Model (applied independently in each direction, like netem on ``lo``):

* **Loss** - each datagram is dropped with probability ``loss``. For TCP the
  proxy cannot drop bytes from a stream, so a loss *event* (probability
  ``1-(1-loss)^segments`` per chunk) instead stalls that chunk and everything
  behind it by one emulated RTT + ``EMU_TCP_RECOVERY_MS`` - the head-of-line
  blocking a real retransmission causes.
  The loss also halves an emulated Reno congestion window that caps the
  stream at ``cwnd * MSS / RTT``, so TCP throughput falls roughly as
  ``MSS / (RTT * sqrt(p))`` (Mathis et al.) - the collapse seen in practice.
* **Rate** - a shared link serialises bytes at ``rate`` Mbps. UDP datagrams
  arriving when the queue already holds ``queue_limit`` packets are tail
  dropped; TCP instead gets backpressure (the proxy stops reading).
* **Delay/jitter** - delivery at ``tx_done + delay + U(-jitter, +jitter)``.
  UDP may be reordered by jitter (as with netem); TCP order is preserved.

Run standalone::

    python netem/emulator.py --protocol udp --listen-port 9102 --target-port 9002 --delay 50 --loss 5

It prints ``READY`` once listening and ``STATS {json}`` when terminated.
"""

from __future__ import annotations

import argparse
import heapq
import itertools
import json
import math
import random
import select
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import config  # noqa: E402


class LinkShaper:
    """One direction of an emulated link (thread-safe)."""

    def __init__(self, delay_ms: float, jitter_ms: float, loss_pct: float, rate_mbps: float,
                 queue_limit: int, rng: random.Random) -> None:
        self.delay = delay_ms / 1000
        self.jitter = jitter_ms / 1000
        self.loss = loss_pct / 100
        self.rate_Bps = rate_mbps * 1e6 / 8 if rate_mbps else 0.0
        self.queue_limit = queue_limit
        self.rng = rng
        self.link_free = 0.0
        self.lock = threading.Lock()
        self.stats = {"packets": 0, "bytes": 0, "dropped_loss": 0, "dropped_queue": 0, "loss_events": 0}

    def lost(self, segments: int = 1) -> bool:
        if self.loss <= 0:
            return False
        return self.rng.random() < 1 - (1 - self.loss) ** segments

    def backlog_s(self, now: float) -> float:
        return max(self.link_free - now, 0.0)

    def schedule(self, nbytes: int, now: float, drop_on_full: bool) -> float | None:
        """Reserve link time for ``nbytes``; return delivery time or None if tail-dropped."""
        with self.lock:
            tx = nbytes / self.rate_Bps if self.rate_Bps else 0.0
            start = max(now, self.link_free)
            if drop_on_full and tx and (start - now) / tx >= self.queue_limit:
                self.stats["dropped_queue"] += 1
                return None
            self.link_free = start + tx
            d = self.delay + (self.rng.uniform(-self.jitter, self.jitter) if self.jitter else 0.0)
            self.stats["packets"] += 1
            self.stats["bytes"] += nbytes
            return self.link_free + max(d, 0.0)


class Scheduler:
    """Delivers queued actions at their due time from a single thread."""

    def __init__(self) -> None:
        self._heap: list[tuple[float, int, Callable[[], None]]] = []
        self._counter = itertools.count()
        self._cv = threading.Condition()
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True, name="emu-scheduler")
        self._thread.start()

    def push(self, when: float, action: Callable[[], None]) -> None:
        with self._cv:
            heapq.heappush(self._heap, (when, next(self._counter), action))
            self._cv.notify()

    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self._stop:
                    if self._heap:
                        wait = self._heap[0][0] - time.monotonic()
                        if wait <= 0:
                            break
                        self._cv.wait(min(wait, config.EMU_SCHED_IDLE_S))
                    else:
                        self._cv.wait(config.EMU_SCHED_IDLE_S)
                if self._stop:
                    return
                _, _, action = heapq.heappop(self._heap)
            try:
                action()
            except OSError:
                pass

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify()


class Emulator:
    """UDP or TCP impairment proxy: ``listen_port`` -> ``target``."""

    def __init__(self, protocol: str, listen_port: int, target: tuple[str, int], delay_ms: float = 0,
                 jitter_ms: float = 0, loss_pct: float = 0, rate_mbps: float = 0,
                 queue_limit: int = config.NETEM_LIMIT_PKTS, seed: int | None = None,
                 listen_host: str = config.DEFAULT_HOST) -> None:
        self.protocol = protocol.lower()
        self.listen = (listen_host, listen_port)
        self.target = target
        self.delay_ms = delay_ms
        rng = random.Random(seed)
        self.fwd = LinkShaper(delay_ms, jitter_ms, loss_pct, rate_mbps, queue_limit, rng)
        self.rev = LinkShaper(delay_ms, jitter_ms, loss_pct, rate_mbps, queue_limit, rng)
        self.sched = Scheduler()  # UDP (non-blocking sendto); each TCP direction gets its own
        self._tcp_scheds: list[Scheduler] = []
        self._stop = threading.Event()
        self._socks: list[socket.socket] = []
        self.connections = 0

    # ------------------------------------------------------------------ UDP
    def _serve_udp(self) -> None:
        lsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        _bufs(lsock)
        lsock.bind(self.listen)
        lsock.settimeout(config.SOCKET_POLL_TIMEOUT_S)
        self._socks.append(lsock)
        upstreams: dict[tuple[str, int], socket.socket] = {}
        self._ready.set()
        while not self._stop.is_set():
            try:
                data, addr = lsock.recvfrom(config.MAX_UDP_DATAGRAM)
            except socket.timeout:
                continue
            except OSError:
                break
            up = upstreams.get(addr)
            if up is None:
                up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                _bufs(up)
                up.connect(self.target)
                up.settimeout(config.SOCKET_POLL_TIMEOUT_S)
                upstreams[addr] = up
                self._socks.append(up)
                self.connections += 1
                threading.Thread(target=self._udp_reverse, args=(up, lsock, addr), daemon=True).start()
            self._udp_forward(self.fwd, data, lambda d=data, s=up: s.send(d))

    def _udp_forward(self, shaper: LinkShaper, data: bytes, send: Callable[[], Any]) -> None:
        if shaper.lost():
            shaper.stats["dropped_loss"] += 1
            return
        when = shaper.schedule(len(data), time.monotonic(), drop_on_full=True)
        if when is not None:
            self.sched.push(when, send)

    def _udp_reverse(self, up: socket.socket, lsock: socket.socket, addr: tuple[str, int]) -> None:
        while not self._stop.is_set():
            try:
                data = up.recv(config.MAX_UDP_DATAGRAM)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                time.sleep(config.SOCKET_POLL_TIMEOUT_S)
                continue
            self._udp_forward(self.rev, data, lambda d=data: lsock.sendto(d, addr))

    # ------------------------------------------------------------------ TCP
    def _serve_tcp(self) -> None:
        lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lsock.bind(self.listen)
        lsock.listen(128)
        lsock.settimeout(config.SOCKET_POLL_TIMEOUT_S)
        self._socks.append(lsock)
        self._ready.set()
        while not self._stop.is_set():
            try:
                client, _ = lsock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                server = socket.create_connection(self.target, timeout=config.CONNECT_TIMEOUT_S)
            except OSError:
                client.close()
                continue
            self.connections += 1
            for s in (client, server):
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                _bufs(s)
                s.settimeout(None)  # blocking: sendall must never give up midway
                self._socks.append(s)
            threading.Thread(target=self._tcp_pump, args=(client, server, self.fwd), daemon=True).start()
            threading.Thread(target=self._tcp_pump, args=(server, client, self.rev), daemon=True).start()

    def _tcp_pump(self, src: socket.socket, dst: socket.socket, shaper: LinkShaper) -> None:
        """Copy src -> dst through the shaper, preserving byte order.

        A Reno-style congestion window limits the sending rate to
        ``cwnd * MSS / RTT``: slow start / additive increase per delivered
        segment, multiplicative decrease (halving) per loss event.
        """
        rtt = max(2 * self.delay_ms, config.EMU_TCP_BASE_RTT_MS) / 1000
        penalty = rtt + config.EMU_TCP_RECOVERY_MS / 1000
        cwnd, ssthresh = float(config.EMU_TCP_INIT_CWND), math.inf
        cc_free = 0.0  # time the congestion window allows the next byte out
        last_delivery = 0.0
        # Own delivery thread per direction: a blocking write towards one peer must never
        # stop delivery in the other direction (that would deadlock under backpressure).
        sched = Scheduler()
        self._tcp_scheds.append(sched)
        pending = [0]  # bytes scheduled but not yet delivered
        lock = threading.Lock()

        def deliver(chunk: bytes) -> None:
            try:
                dst.sendall(chunk)
            finally:
                with lock:
                    pending[0] -= len(chunk)

        while not self._stop.is_set():
            # Backpressure: do not read more while the emulated queue is full.
            now = time.monotonic()
            backlog_bytes = shaper.backlog_s(now) * shaper.rate_Bps if shaper.rate_Bps else 0
            if pending[0] > config.EMU_TCP_MAX_BACKLOG_BYTES or backlog_bytes > config.EMU_TCP_MAX_BACKLOG_BYTES:
                time.sleep(0.002)
                continue
            try:
                readable, _, _ = select.select([src], [], [], config.SOCKET_POLL_TIMEOUT_S)
                if not readable:
                    continue
                chunk = src.recv(config.TCP_RECV_CHUNK)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            now = time.monotonic()
            segs = math.ceil(len(chunk) / config.EMU_TCP_MSS)
            send_at = max(now, cc_free)
            cc_free = send_at + len(chunk) / (cwnd * config.EMU_TCP_MSS / rtt)
            when = shaper.schedule(len(chunk), send_at, drop_on_full=False) or send_at
            if shaper.lost(segs):
                shaper.stats["loss_events"] += 1
                when += penalty
                ssthresh = max(cwnd / 2, 2.0)
                cwnd = ssthresh
            else:
                cwnd += segs if cwnd < ssthresh else segs / cwnd
                cwnd = min(cwnd, config.EMU_TCP_MAX_CWND)
            when = max(when, last_delivery)
            last_delivery = when
            with lock:
                pending[0] += len(chunk)
            sched.push(when, lambda c=chunk: deliver(c))
        # Propagate EOF after the last byte has been delivered.
        sched.push(max(last_delivery, time.monotonic()), lambda: _shutdown_wr(dst))

    # ------------------------------------------------------------------ API
    def start(self) -> None:
        self._ready = threading.Event()
        target = self._serve_udp if self.protocol == "udp" else self._serve_tcp
        self._thread = threading.Thread(target=target, daemon=True, name=f"emu-{self.protocol}")
        self._thread.start()
        if not self._ready.wait(config.EMU_READY_TIMEOUT_S):
            raise RuntimeError("emulator failed to start")

    def stop(self) -> None:
        self._stop.set()
        self.sched.stop()
        for sc in self._tcp_scheds:
            sc.stop()
        for s in self._socks:
            try:
                s.close()
            except OSError:
                pass

    def stats(self) -> dict[str, Any]:
        f, r = self.fwd.stats, self.rev.stats
        return {
            "protocol": self.protocol,
            "connections": self.connections,
            "fwd": dict(f),
            "rev": dict(r),
            "dropped": f["dropped_loss"] + f["dropped_queue"] + r["dropped_loss"] + r["dropped_queue"],
            "tcp_loss_events": f["loss_events"] + r["loss_events"],
        }


def _bufs(sock: socket.socket) -> None:
    for opt in (socket.SO_RCVBUF, socket.SO_SNDBUF):
        size = config.SOCKET_BUFFER_BYTES
        while size >= 65536:
            try:
                sock.setsockopt(socket.SOL_SOCKET, opt, size)
                break
            except OSError:
                size //= 2


def _shutdown_wr(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Software network emulator (impairment proxy).")
    ap.add_argument("--protocol", choices=["tcp", "udp"], required=True)
    ap.add_argument("--listen-port", type=int, required=True)
    ap.add_argument("--target-host", default=config.DEFAULT_HOST)
    ap.add_argument("--target-port", type=int, required=True)
    ap.add_argument("--delay", type=float, default=0, help="one-way delay per direction, ms")
    ap.add_argument("--jitter", type=float, default=0, help="+- ms (uniform)")
    ap.add_argument("--loss", type=float, default=0, help="percent per direction")
    ap.add_argument("--rate", type=float, default=0, help="Mbps per direction, 0 = unlimited")
    ap.add_argument("--queue-limit", type=int, default=config.NETEM_LIMIT_PKTS, help="packets")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args(argv)

    emu = Emulator(args.protocol, args.listen_port, (args.target_host, args.target_port), args.delay,
                   args.jitter, args.loss, args.rate, args.queue_limit, args.seed)
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    signal.signal(signal.SIGINT, lambda *_: done.set())
    emu.start()
    print(f"READY {args.protocol} {args.listen_port}", flush=True)
    while not done.wait(0.2):
        pass
    emu.stop()
    print("STATS " + json.dumps(emu.stats()), flush=True)


if __name__ == "__main__":
    main()
