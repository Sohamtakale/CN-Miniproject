"""TCP load-generating client.

Each client thread opens one connection, sends ``num_packets`` length-prefixed
packets at ``send_rate`` pps while a receiver thread collects the per-packet
ACKs (RTT), then sends END and waits for the server's statistics.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import config  # noqa: E402
from common.metrics import NS_PER_MS, ClientResult, aggregate_run  # noqa: E402
from common.protocol import FrameReader, frame, pack_packet, payload_size_for, unpack_packet  # noqa: E402
from common.utils import LiveCounters, Pacer, ProgressCallback, run_parallel_clients, tune_socket_buffers  # noqa: E402


def tcp_client_worker(
    client_id: int,
    counters: LiveCounters,
    host: str,
    port: int,
    packet_size: int,
    num_packets: int,
    send_rate: float,
    end_timeout: float = config.TCP_END_TIMEOUT_S,
) -> ClientResult:
    """Run one TCP client session and return what it measured."""
    res = ClientResult(client_id)
    payload = os.urandom(payload_size_for(packet_size))
    done = threading.Event()

    sock = socket.create_connection((host, port), timeout=config.CONNECT_TIMEOUT_S)
    if config.TCP_NODELAY:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    tune_socket_buffers(sock)
    sock.settimeout(config.TCP_SEND_TIMEOUT_S)  # sendall must not give up mid-frame under backpressure

    def receiver() -> None:
        reader = FrameReader()
        deadline: float | None = None
        try:
            while not done.is_set():
                if deadline is None and end_sent.is_set():
                    deadline = time.monotonic() + end_timeout
                if deadline is not None and time.monotonic() > deadline:
                    res.error = "timeout waiting for END reply"
                    return
                readable, _, _ = select.select([sock], [], [], config.SOCKET_POLL_TIMEOUT_S)
                if not readable:
                    continue
                data = sock.recv(config.TCP_RECV_CHUNK)
                if not data:
                    if res.server_stats is None:
                        res.error = "server closed connection early"
                    return
                now = time.time_ns()
                for raw in reader.feed(data):
                    pkt = unpack_packet(raw)
                    if pkt.is_end:
                        res.server_stats = json.loads(pkt.payload)
                        return
                    rtt = (now - pkt.send_ts_ns) / NS_PER_MS
                    res.rtts_ms.append(rtt)
                    res.acks += 1
                    counters.on_ack(packet_size, rtt)
        except Exception as exc:
            res.error = f"receiver: {type(exc).__name__}: {exc}"
        finally:
            done.set()

    end_sent = threading.Event()
    rx = threading.Thread(target=receiver, daemon=True)
    rx.start()
    pacer = Pacer(send_rate)
    res.start_ns = time.time_ns()
    try:
        for seq in range(num_packets):
            if done.is_set():
                break
            pacer.wait(seq)
            sock.sendall(frame(pack_packet(seq, config.FLAG_DATA, payload)))
            res.sent += 1
            res.sent_bytes += packet_size
            counters.on_send(packet_size)
        sock.sendall(frame(pack_packet(res.sent, config.FLAG_END)))
        end_sent.set()
        rx.join()
    except OSError as exc:
        res.error = f"send: {exc}"
        done.set()
    finally:
        res.end_ns = time.time_ns()
        sock.close()
    return res


def run_tcp_test(
    host: str = config.DEFAULT_HOST,
    port: int = config.TCP_PORT,
    packet_size: int = 1024,
    num_packets: int = 1000,
    send_rate: float = 0,
    num_clients: int = 1,
    progress_cb: ProgressCallback | None = None,
) -> tuple[dict, list[ClientResult]]:
    """Run ``num_clients`` concurrent TCP clients; return (metrics row, client results)."""
    results = run_parallel_clients(
        lambda cid, ctr: tcp_client_worker(cid, ctr, host, port, packet_size, num_packets, send_rate),
        num_clients,
        progress_cb,
    )
    return aggregate_run(results), results


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="TCP client: send packets, measure RTT/throughput.")
    ap.add_argument("--host", default=config.DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=config.TCP_PORT)
    ap.add_argument("--packet-size", type=int, default=1024, help="bytes incl. 15 B header")
    ap.add_argument("--num-packets", type=int, default=1000, help="packets per client")
    ap.add_argument("--send-rate", type=float, default=0, help="packets/s per client, 0 = unlimited")
    ap.add_argument("--num-clients", type=int, default=1)
    args = ap.parse_args(argv)
    row, _ = run_tcp_test(args.host, args.port, args.packet_size, args.num_packets, args.send_rate, args.num_clients)
    print(json.dumps(row, indent=2))


if __name__ == "__main__":
    main()
