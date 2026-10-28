"""Multi-client UDP echo/ACK server.

A single socket serves all clients; sessions are keyed by the client address
(each client thread uses its own socket/port). For every DATA datagram the
server updates the session's :class:`ReceiverStats` (loss, duplicates,
reordering, RFC 3550 jitter, one-way delay) and replies with a header-only
ACK. END datagrams (``seq`` = number sent) are answered with ``ACK|END`` and
JSON statistics; the reply is cached so retransmitted ENDs get the same answer.
A janitor thread evicts idle sessions.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import config  # noqa: E402
from common.metrics import ReceiverStats  # noqa: E402
from common.protocol import ProtocolError, make_ack, unpack_header  # noqa: E402
from common.utils import setup_logging, tune_socket_buffers  # noqa: E402

log = logging.getLogger("udp_server")

Addr = tuple[str, int]


class UDPServer:
    """UDP receiver with per-client sessions."""

    def __init__(self, host: str = config.DEFAULT_HOST, port: int = config.UDP_PORT) -> None:
        self.host = host
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tune_socket_buffers(self.sock)
        self.sock.bind((host, port))
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(config.SOCKET_POLL_TIMEOUT_S)
        self.sessions: dict[Addr, ReceiverStats] = {}
        self.finished: dict[Addr, bytes] = {}   # cached END replies
        self.last_seen: dict[Addr, float] = {}
        self.lock = threading.Lock()
        self.sessions_done = 0

    def serve_forever(self) -> None:
        log.info("UDP server listening on %s:%d", self.host, self.port)
        threading.Thread(target=self._janitor, daemon=True).start()
        sock = self.sock
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(config.MAX_UDP_DATAGRAM)
            except socket.timeout:
                continue
            except OSError:
                break
            now = time.time_ns()
            try:
                seq, ts, length, flags = unpack_header(data)
            except ProtocolError:
                continue
            with self.lock:
                self.last_seen[addr] = time.monotonic()
                if flags & config.FLAG_END:
                    reply = self.finished.get(addr)
                    if reply is None:
                        stats = self.sessions.pop(addr, ReceiverStats())
                        summary = json.dumps(stats.to_dict(sent_total=seq)).encode()
                        reply = make_ack(seq, ts, config.FLAG_END, summary)
                        self.finished[addr] = reply
                        self.sessions_done += 1
                    self._send(reply, addr)
                    continue
                if flags & config.FLAG_DATA:
                    if addr in self.finished:  # straggler after END: ignore
                        continue
                    stats = self.sessions.get(addr)
                    if stats is None:
                        stats = self.sessions[addr] = ReceiverStats()
                    stats.on_packet(seq, ts, now, len(data), length)
            self._send(make_ack(seq, ts), addr)
        sock.close()

    def _send(self, data: bytes, addr: Addr) -> None:
        try:
            self.sock.sendto(data, addr)
        except OSError as exc:  # e.g. ENOBUFS under overload: that is a loss
            log.debug("sendto %s failed: %s", addr, exc)

    def _janitor(self) -> None:
        while not self._stop.wait(config.SERVER_SESSION_TTL_S / 4):
            cutoff = time.monotonic() - config.SERVER_SESSION_TTL_S
            with self.lock:
                for addr in [a for a, t in self.last_seen.items() if t < cutoff]:
                    self.sessions.pop(addr, None)
                    self.finished.pop(addr, None)
                    self.last_seen.pop(addr, None)

    def stop(self) -> None:
        self._stop.set()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="UDP echo/ACK server for the TCP vs UDP comparison.")
    ap.add_argument("--host", default=config.DEFAULT_HOST, help="bind address (default %(default)s)")
    ap.add_argument("--port", type=int, default=config.UDP_PORT, help="listen port (default %(default)s)")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    server = UDPServer(args.host, args.port)
    signal.signal(signal.SIGTERM, lambda *_: server.stop())
    print(f"READY udp {server.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.stop()
    log.info("UDP server stopped")


if __name__ == "__main__":
    main()
