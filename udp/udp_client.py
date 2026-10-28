"""UDP load-generating client.

Each client thread owns a UDP socket, sends ``num_packets`` datagrams at
``send_rate`` pps and collects the server's ACK echoes for RTT. After sending
it waits for trailing ACKs (adaptive drain), then sends END repeatedly until
the server's statistics arrive (END itself may be lost).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import config  # noqa: E402
from common.metrics import NS_PER_MS, ClientResult, aggregate_run  # noqa: E402
from common.protocol import ProtocolError, pack_packet, payload_size_for, unpack_packet  # noqa: E402
from common.utils import LiveCounters, Pacer, ProgressCallback, run_parallel_clients, tune_socket_buffers  # noqa: E402


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def udp_client_worker(
    client_id: int,
    counters: LiveCounters,
    host: str,
    port: int,
    packet_size: int,
    num_packets: int,
    send_rate: float,
) -> ClientResult:
    """Run one UDP client session and return what it measured."""
    res = ClientResult(client_id)
    payload = os.urandom(payload_size_for(packet_size))
    stop = threading.Event()
    got_end = threading.Event()
    acked: set[int] = set()
    state = {"last_ack": time.monotonic(), "max_rtt_ms": 0.0}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tune_socket_buffers(sock)
    sock.connect((host, port))
    sock.settimeout(config.SOCKET_POLL_TIMEOUT_S)

    def receiver() -> None:
        while not stop.is_set():
            try:
                data = sock.recv(config.MAX_UDP_DATAGRAM)
            except socket.timeout:
                continue
            except OSError:  # e.g. ECONNREFUSED if server is down
                time.sleep(config.SOCKET_POLL_TIMEOUT_S)
                continue
            now = time.time_ns()
            try:
                pkt = unpack_packet(data)
            except ProtocolError:
                continue
            if pkt.is_end:
                res.server_stats = json.loads(pkt.payload)
                got_end.set()
                return
            if pkt.seq in acked:
                continue
            acked.add(pkt.seq)
            rtt = (now - pkt.send_ts_ns) / NS_PER_MS
            res.rtts_ms.append(rtt)
            res.acks += 1
            state["last_ack"] = time.monotonic()
            state["max_rtt_ms"] = max(state["max_rtt_ms"], rtt)
            counters.on_ack(packet_size, rtt)

    rx = threading.Thread(target=receiver, daemon=True)
    rx.start()
    pacer = Pacer(send_rate)
    res.start_ns = time.time_ns()
    try:
        for seq in range(num_packets):
            pacer.wait(seq)
            try:
                sock.send(pack_packet(seq, config.FLAG_DATA, payload))
            except OSError:  # ENOBUFS etc: the datagram is lost, keep going
                pass
            res.sent += 1
            res.sent_bytes += packet_size
            counters.on_send(packet_size)
        last_send = time.monotonic()

        # Drain: wait for outstanding ACKs, adapting to the observed RTT.
        while res.acks < res.sent:
            rtt_s = state["max_rtt_ms"] / 1000
            drain = _clamp(config.UDP_DRAIN_RTT_FACTOR * rtt_s, config.UDP_MIN_DRAIN_S, config.UDP_MAX_DRAIN_S) \
                if rtt_s else config.UDP_MAX_DRAIN_S
            if time.monotonic() > max(last_send, state["last_ack"]) + drain:
                break
            time.sleep(0.01)

        # END handshake with retries.
        end_timeout = max(config.UDP_END_MIN_TIMEOUT_S, config.UDP_DRAIN_RTT_FACTOR * state["max_rtt_ms"] / 1000)
        for _ in range(config.UDP_END_RETRIES):
            try:
                sock.send(pack_packet(res.sent, config.FLAG_END))
            except OSError:
                pass
            if got_end.wait(end_timeout):
                break
        if not got_end.is_set():
            res.error = "no END reply from server"
    finally:
        res.end_ns = time.time_ns()
        stop.set()
        rx.join(timeout=1)
        sock.close()
    return res


def run_udp_test(
    host: str = config.DEFAULT_HOST,
    port: int = config.UDP_PORT,
    packet_size: int = 1024,
    num_packets: int = 1000,
    send_rate: float = 0,
    num_clients: int = 1,
    progress_cb: ProgressCallback | None = None,
) -> tuple[dict, list[ClientResult]]:
    """Run ``num_clients`` concurrent UDP clients; return (metrics row, client results)."""
    results = run_parallel_clients(
        lambda cid, ctr: udp_client_worker(cid, ctr, host, port, packet_size, num_packets, send_rate),
        num_clients,
        progress_cb,
    )
    return aggregate_run(results), results


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="UDP client: send datagrams, measure RTT/loss/jitter.")
    ap.add_argument("--host", default=config.DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=config.UDP_PORT)
    ap.add_argument("--packet-size", type=int, default=1024, help="bytes incl. 15 B header")
    ap.add_argument("--num-packets", type=int, default=1000, help="packets per client")
    ap.add_argument("--send-rate", type=float, default=0, help="packets/s per client, 0 = unlimited")
    ap.add_argument("--num-clients", type=int, default=1)
    args = ap.parse_args(argv)
    row, _ = run_udp_test(args.host, args.port, args.packet_size, args.num_packets, args.send_rate, args.num_clients)
    print(json.dumps(row, indent=2))


if __name__ == "__main__":
    main()
