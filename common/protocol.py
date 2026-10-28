"""Packet format shared by TCP and UDP.

Wire layout (network byte order)::

    +---------+----------------+-------------+-------+-----------+
    | seq u32 | send_ts_ns u64 | length u16  | flags | payload   |
    +---------+----------------+-------------+-------+-----------+
       4 B          8 B            2 B         1 B    length B

TCP is a byte stream, so each packet is additionally prefixed with a 4-byte
length field (see :func:`frame` / :func:`read_frame`).
"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass

from common import config

HEADER = struct.Struct(config.HEADER_FORMAT)
HEADER_SIZE: int = HEADER.size  # 15 bytes
FRAME_PREFIX = struct.Struct(config.FRAME_PREFIX_FORMAT)
FRAME_PREFIX_SIZE: int = FRAME_PREFIX.size


class ProtocolError(ValueError):
    """Raised when bytes on the wire do not form a valid packet."""


@dataclass(frozen=True)
class Packet:
    """A decoded packet."""

    seq: int
    send_ts_ns: int
    flags: int
    payload: bytes = b""

    @property
    def is_data(self) -> bool:
        return bool(self.flags & config.FLAG_DATA)

    @property
    def is_ack(self) -> bool:
        return bool(self.flags & config.FLAG_ACK)

    @property
    def is_end(self) -> bool:
        return bool(self.flags & config.FLAG_END)

    @property
    def size(self) -> int:
        """Total packet size on the wire (header + payload, no TCP prefix)."""
        return HEADER_SIZE + len(self.payload)


def pack_packet(seq: int, flags: int, payload: bytes = b"", send_ts_ns: int | None = None) -> bytes:
    """Serialize a packet.

    Args:
        seq: Sequence number (0 .. 2**32-1).
        flags: Bitmask of ``config.FLAG_*``.
        payload: Payload bytes (max 65535).
        send_ts_ns: Send timestamp; defaults to ``time.time_ns()``.
    """
    if len(payload) > 0xFFFF:
        raise ProtocolError(f"payload too large: {len(payload)} bytes")
    if not 0 <= seq <= 0xFFFFFFFF:
        raise ProtocolError(f"sequence number out of range: {seq}")
    ts = time.time_ns() if send_ts_ns is None else send_ts_ns
    return HEADER.pack(seq, ts, len(payload), flags) + payload


def unpack_header(data: bytes) -> tuple[int, int, int, int]:
    """Decode only the header: returns ``(seq, send_ts_ns, length, flags)``."""
    if len(data) < HEADER_SIZE:
        raise ProtocolError(f"packet shorter than header: {len(data)} bytes")
    return HEADER.unpack_from(data)


def unpack_packet(data: bytes) -> Packet:
    """Decode a full packet and validate the payload length."""
    seq, ts, length, flags = unpack_header(data)
    payload = data[HEADER_SIZE:HEADER_SIZE + length]
    if len(payload) != length:
        raise ProtocolError(f"truncated payload: header says {length}, got {len(payload)}")
    return Packet(seq=seq, send_ts_ns=ts, flags=flags, payload=bytes(payload))


def payload_size_for(packet_size: int) -> int:
    """Payload bytes needed so header + payload equals ``packet_size``."""
    if packet_size < HEADER_SIZE:
        raise ProtocolError(f"packet_size must be >= header size ({HEADER_SIZE})")
    return packet_size - HEADER_SIZE


def make_ack(pkt_seq: int, pkt_ts_ns: int, extra_flags: int = 0, payload: bytes = b"") -> bytes:
    """Build an ACK echoing the original seq and timestamp (for RTT)."""
    return pack_packet(pkt_seq, config.FLAG_ACK | extra_flags, payload, send_ts_ns=pkt_ts_ns)


# --------------------------------------------------------------------------- #
# TCP framing
# --------------------------------------------------------------------------- #
def frame(packet: bytes) -> bytes:
    """Prefix a packet with its length for transmission over a TCP stream."""
    return FRAME_PREFIX.pack(len(packet)) + packet


def recv_exact(sock: socket.socket, n: int) -> bytes | None:
    """Read exactly ``n`` bytes; return ``None`` if the peer closed first."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def read_frame(sock: socket.socket) -> bytes | None:
    """Read one length-prefixed packet from a TCP socket (``None`` on EOF)."""
    prefix = recv_exact(sock, FRAME_PREFIX_SIZE)
    if prefix is None:
        return None
    (length,) = FRAME_PREFIX.unpack(prefix)
    if length < HEADER_SIZE or length > config.MAX_PACKET_SIZE:
        raise ProtocolError(f"invalid frame length {length}")
    return recv_exact(sock, length)


class FrameReader:
    """Incremental decoder for length-prefixed frames (buffered, fewer syscalls)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        """Add received bytes and return all complete packets."""
        self._buf.extend(data)
        out: list[bytes] = []
        while len(self._buf) >= FRAME_PREFIX_SIZE:
            (length,) = FRAME_PREFIX.unpack_from(self._buf)
            if length < HEADER_SIZE or length > config.MAX_PACKET_SIZE:
                raise ProtocolError(f"invalid frame length {length}")
            end = FRAME_PREFIX_SIZE + length
            if len(self._buf) < end:
                break
            out.append(bytes(self._buf[FRAME_PREFIX_SIZE:end]))
            del self._buf[:end]
        return out
