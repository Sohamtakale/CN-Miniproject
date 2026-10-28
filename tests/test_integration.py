"""End-to-end tests on loopback: real servers, clients and the software emulator."""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from netem.emulator import Emulator
from tcp.tcp_client import run_tcp_test
from tcp.tcp_server import TCPServer
from udp.udp_client import run_udp_test
from udp.udp_server import UDPServer


@pytest.fixture(scope="module")
def servers() -> Iterator[tuple[int, int]]:
    tcp, udp = TCPServer(port=0), UDPServer(port=0)  # port 0 = any free port
    for s in (tcp, udp):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield tcp.port, udp.port
    tcp.stop()
    udp.stop()


def test_tcp_direct(servers: tuple[int, int]) -> None:
    row, _ = run_tcp_test(port=servers[0], packet_size=512, num_packets=200, num_clients=3)
    assert row["status"] == "ok", row["error"]
    assert row["sent"] == row["received"] == 600
    assert row["loss_pct"] == 0 and row["out_of_order"] == 0
    assert row["rtt_samples"] == 600 and row["throughput_mbps"] > 0


def test_udp_direct(servers: tuple[int, int]) -> None:
    row, _ = run_udp_test(port=servers[1], packet_size=256, num_packets=200, send_rate=2000, num_clients=2)
    assert row["status"] == "ok", row["error"]
    assert row["sent"] == 400 and row["loss_pct"] < 5


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_udp_through_lossy_emulator(servers: tuple[int, int]) -> None:
    port = _free_port()
    emu = Emulator("udp", port, ("127.0.0.1", servers[1]), loss_pct=20, delay_ms=5, seed=1)
    emu.start()
    try:
        row, _ = run_udp_test(port=port, packet_size=256, num_packets=500, send_rate=2000)
    finally:
        emu.stop()
    assert row["status"] == "ok", row["error"]
    assert 10 < row["loss_pct"] < 30          # ~20% configured
    assert row["rtt_min_ms"] >= 9             # 2 x 5 ms delay


def test_tcp_through_lossy_emulator_is_reliable(servers: tuple[int, int]) -> None:
    port = _free_port()
    emu = Emulator("tcp", port, ("127.0.0.1", servers[0]), loss_pct=10, delay_ms=5, seed=1)
    emu.start()
    try:
        row, _ = run_tcp_test(port=port, packet_size=512, num_packets=200, send_rate=2000)
    finally:
        emu.stop()
    assert row["status"] == "ok", row["error"]
    assert row["loss_pct"] == 0 and row["received"] == 200
    assert emu.stats()["tcp_loss_events"] > 0
