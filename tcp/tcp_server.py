"""Multi-client TCP echo/ACK server.

Each connection is served by its own thread. For every DATA packet the server
records receiver statistics and sends back a header-only ACK carrying the
original sequence number and timestamp (so the client can compute RTT). When
the client sends END (whose ``seq`` is the number of packets it sent) the
server replies with ``ACK|END`` and a JSON payload of its statistics.
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
from common.protocol import FrameReader, ProtocolError, frame, make_ack, unpack_header  # noqa: E402
from common.utils import setup_logging, tune_socket_buffers  # noqa: E402

log = logging.getLogger("tcp_server")


class TCPServer:
    """Threaded TCP server; call :meth:`serve_forever` and :meth:`stop`."""

    def __init__(self, host: str = config.DEFAULT_HOST, port: int = config.TCP_PORT) -> None:
        self.host, self.port = host, port
        self._stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(128)
        self.sock.settimeout(config.SOCKET_POLL_TIMEOUT_S)
        self.sessions_done = 0

    def serve_forever(self) -> None:
        log.info("TCP server listening on %s:%d", self.host, self.port)
        while not self._stop.is_set():
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn, addr), daemon=True).start()
        self.sock.close()

    def stop(self) -> None:
        self._stop.set()

    def _handle(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        stats = ReceiverStats()
        reader = FrameReader()
        conn.settimeout(config.SOCKET_POLL_TIMEOUT_S)
        if config.TCP_NODELAY:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        tune_socket_buffers(conn)
        log.debug("connection from %s", addr)
        try:
            while not self._stop.is_set():
                try:
                    data = conn.recv(config.TCP_RECV_CHUNK)
                except socket.timeout:
                    continue
                if not data:
                    break
                now = time.time_ns()
                out = bytearray()
                for pkt in reader.feed(data):
                    seq, ts, length, flags = unpack_header(pkt)
                    if flags & config.FLAG_END:
                        summary = json.dumps(stats.to_dict(sent_total=seq)).encode()
                        out += frame(make_ack(seq, ts, config.FLAG_END, summary))
                        conn.sendall(bytes(out))
                        self.sessions_done += 1
                        log.debug("session %s done: %d pkts", addr, stats.unique)
                        return
                    if flags & config.FLAG_DATA:
                        stats.on_packet(seq, ts, now, len(pkt), length)
                        out += frame(make_ack(seq, ts))
                if out:
                    conn.sendall(bytes(out))
        except (ProtocolError, OSError) as exc:
            log.warning("connection %s error: %s", addr, exc)
        finally:
            conn.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="TCP echo/ACK server for the TCP vs UDP comparison.")
    ap.add_argument("--host", default=config.DEFAULT_HOST, help="bind address (default %(default)s)")
    ap.add_argument("--port", type=int, default=config.TCP_PORT, help="listen port (default %(default)s)")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    server = TCPServer(args.host, args.port)
    signal.signal(signal.SIGTERM, lambda *_: server.stop())
    print(f"READY tcp {server.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.stop()
    log.info("TCP server stopped")


if __name__ == "__main__":
    main()
