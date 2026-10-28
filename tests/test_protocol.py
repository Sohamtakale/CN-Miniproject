"""Tests for the packet header and TCP framing."""

from __future__ import annotations

import os
import socket

import pytest

from common import config
from common.protocol import (
    HEADER_SIZE,
    FrameReader,
    ProtocolError,
    frame,
    make_ack,
    pack_packet,
    payload_size_for,
    read_frame,
    unpack_packet,
)


def test_header_size_is_15_bytes() -> None:
    assert HEADER_SIZE == 4 + 8 + 2 + 1


@pytest.mark.parametrize("size", [0, 1, 49, 1385, 9000])
def test_pack_unpack_round_trip(size: int) -> None:
    payload = os.urandom(size)
    raw = pack_packet(123456, config.FLAG_DATA, payload, send_ts_ns=987654321012345)
    assert len(raw) == HEADER_SIZE + size
    pkt = unpack_packet(raw)
    assert pkt.seq == 123456
    assert pkt.send_ts_ns == 987654321012345
    assert pkt.flags == config.FLAG_DATA
    assert pkt.payload == payload
    assert pkt.is_data and not pkt.is_ack and not pkt.is_end
    assert pkt.size == len(raw)


def test_flags_combination_and_ack_echo() -> None:
    ack = unpack_packet(make_ack(7, 42, extra_flags=config.FLAG_END, payload=b"{}"))
    assert ack.seq == 7 and ack.send_ts_ns == 42
    assert ack.is_ack and ack.is_end and not ack.is_data
    assert ack.payload == b"{}"


def test_max_sequence_number() -> None:
    pkt = unpack_packet(pack_packet(2**32 - 1, config.FLAG_DATA))
    assert pkt.seq == 2**32 - 1
    with pytest.raises(ProtocolError):
        pack_packet(2**32, config.FLAG_DATA)


def test_truncated_packets_rejected() -> None:
    raw = pack_packet(1, config.FLAG_DATA, b"x" * 10)
    with pytest.raises(ProtocolError):
        unpack_packet(raw[:5])
    with pytest.raises(ProtocolError):
        unpack_packet(raw[:-1])


def test_payload_size_for() -> None:
    assert payload_size_for(64) == 64 - HEADER_SIZE
    assert payload_size_for(1400) == 1400 - HEADER_SIZE
    with pytest.raises(ProtocolError):
        payload_size_for(HEADER_SIZE - 1)


def test_frame_reader_handles_split_and_coalesced_frames() -> None:
    pkts = [pack_packet(i, config.FLAG_DATA, os.urandom(i * 10)) for i in range(5)]
    stream = b"".join(frame(p) for p in pkts)
    reader = FrameReader()
    got: list[bytes] = []
    for i in range(0, len(stream), 7):  # feed in awkward 7-byte chunks
        got.extend(reader.feed(stream[i:i + 7]))
    assert got == pkts


def test_read_frame_over_socketpair() -> None:
    a, b = socket.socketpair()
    try:
        pkt = pack_packet(9, config.FLAG_END, b"hello")
        a.sendall(frame(pkt))
        assert read_frame(b) == pkt
        a.close()
        assert read_frame(b) is None  # EOF
    finally:
        b.close()
