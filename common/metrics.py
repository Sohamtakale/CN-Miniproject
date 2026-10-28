"""Metric computation shared by servers, clients, harness and tests.

* :class:`ReceiverStats` - per-session receiver bookkeeping (loss, duplicates,
  reordering, RFC 3550 jitter, one-way delay, byte counts).
* :func:`rtt_summary` - percentile summary of RTT samples.
* :class:`ClientResult` / :func:`aggregate_run` - combine per-client results
  into the single row of metrics that the harness logs.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from common import config

NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000


class ReceiverStats:
    """Tracks what a receiver saw for one sender session.

    Loss is detected from gaps in sequence numbers; if the sender reports the
    total it sent (in the END packet) tail losses are counted too.
    """

    def __init__(self) -> None:
        self.received_total: int = 0      # every packet, duplicates included
        self.unique: int = 0
        self.duplicates: int = 0
        self.out_of_order: int = 0
        self.bytes_total: int = 0          # packet bytes incl. header and dups
        self.payload_bytes_unique: int = 0
        self.highest_seq: int = -1
        self.first_recv_ns: int | None = None
        self.last_recv_ns: int | None = None
        self.jitter_ns: float = 0.0
        self._prev_transit_ns: int | None = None
        self._seen: set[int] = set()
        self.owd_sum_ns: int = 0
        self.owd_min_ns: int | None = None
        self.owd_max_ns: int | None = None
        self._owd_samples: list[int] = []

    def on_packet(self, seq: int, send_ts_ns: int, recv_ts_ns: int, nbytes: int, payload_len: int) -> None:
        """Record arrival of a DATA packet."""
        self.received_total += 1
        self.bytes_total += nbytes
        if self.first_recv_ns is None:
            self.first_recv_ns = recv_ts_ns
        self.last_recv_ns = recv_ts_ns

        if seq in self._seen:
            self.duplicates += 1
            return
        self._seen.add(seq)
        self.unique += 1
        self.payload_bytes_unique += payload_len
        if seq < self.highest_seq:
            self.out_of_order += 1
        else:
            self.highest_seq = seq

        # One-way delay (valid when sender and receiver share a clock, e.g. loopback).
        owd = recv_ts_ns - send_ts_ns
        self.owd_sum_ns += owd
        self.owd_min_ns = owd if self.owd_min_ns is None else min(self.owd_min_ns, owd)
        self.owd_max_ns = owd if self.owd_max_ns is None else max(self.owd_max_ns, owd)
        if len(self._owd_samples) < config.MAX_OWD_SAMPLES:
            self._owd_samples.append(owd)

        # RFC 3550 interarrival jitter: J += (|D| - J) / 16
        transit = recv_ts_ns - send_ts_ns
        if self._prev_transit_ns is not None:
            d = abs(transit - self._prev_transit_ns)
            self.jitter_ns += (d - self.jitter_ns) / 16.0
        self._prev_transit_ns = transit

    def lost(self, sent_total: int | None = None) -> int:
        """Packets never received.

        Args:
            sent_total: Count reported by the sender. If ``None`` only gaps up
                to the highest sequence number seen are counted.
        """
        expected = sent_total if sent_total is not None else self.highest_seq + 1
        return max(expected - self.unique, 0)

    def gaps(self) -> list[tuple[int, int]]:
        """Missing sequence ranges ``(start, end)`` inclusive, up to highest seq."""
        out: list[tuple[int, int]] = []
        start: int | None = None
        for s in range(self.highest_seq + 1):
            if s not in self._seen:
                if start is None:
                    start = s
            elif start is not None:
                out.append((start, s - 1))
                start = None
        if start is not None:
            out.append((start, self.highest_seq))
        return out

    def to_dict(self, sent_total: int | None = None) -> dict[str, Any]:
        """JSON-serialisable summary (sent back to the client in the END reply)."""
        owd_avg_ms = (self.owd_sum_ns / self.unique / NS_PER_MS) if self.unique else None
        p95 = float(np.percentile(self._owd_samples, 95)) / NS_PER_MS if self._owd_samples else None
        return {
            "sent_reported": sent_total,
            "received_total": self.received_total,
            "unique": self.unique,
            "lost": self.lost(sent_total),
            "duplicates": self.duplicates,
            "out_of_order": self.out_of_order,
            "bytes_total": self.bytes_total,
            "payload_bytes_unique": self.payload_bytes_unique,
            "first_recv_ns": self.first_recv_ns,
            "last_recv_ns": self.last_recv_ns,
            "jitter_ms": self.jitter_ns / NS_PER_MS,
            "owd_avg_ms": owd_avg_ms,
            "owd_min_ms": self.owd_min_ns / NS_PER_MS if self.owd_min_ns is not None else None,
            "owd_max_ms": self.owd_max_ns / NS_PER_MS if self.owd_max_ns is not None else None,
            "owd_p95_ms": p95,
        }


# --------------------------------------------------------------------------- #
# Simple metric helpers (unit tested)
# --------------------------------------------------------------------------- #
def throughput_mbps(nbytes: int, duration_s: float) -> float:
    """Megabits per second; 0 if the duration is not positive."""
    if duration_s <= 0:
        return 0.0
    return nbytes * 8 / duration_s / 1e6


def loss_percent(sent: int, received: int) -> float:
    """(sent - received) / sent as a percentage, clamped to [0, 100]."""
    if sent <= 0:
        return 0.0
    return min(max((sent - received) / sent * 100.0, 0.0), 100.0)


def rfc3550_jitter(send_ts: list[float], recv_ts: list[float]) -> float:
    """Final RFC 3550 jitter for paired send/receive timestamps (same units)."""
    j = 0.0
    for i in range(1, len(send_ts)):
        d = abs((recv_ts[i] - recv_ts[i - 1]) - (send_ts[i] - send_ts[i - 1]))
        j += (d - j) / 16.0
    return j


def rtt_summary(rtts_ms: list[float]) -> dict[str, float | None]:
    """avg/min/max/p50/p95/p99 of RTT samples in milliseconds."""
    keys = ["rtt_avg_ms", "rtt_min_ms", "rtt_max_ms", "rtt_p50_ms", "rtt_p95_ms", "rtt_p99_ms"]
    if not rtts_ms:
        return {k: None for k in keys}
    a = np.asarray(rtts_ms, dtype=float)
    p50, p95, p99 = np.percentile(a, [50, 95, 99])
    return dict(zip(keys, [float(a.mean()), float(a.min()), float(a.max()), float(p50), float(p95), float(p99)]))


# --------------------------------------------------------------------------- #
# Run aggregation
# --------------------------------------------------------------------------- #
@dataclass
class ClientResult:
    """Everything one client thread measured during a run."""

    client_id: int
    sent: int = 0
    sent_bytes: int = 0
    acks: int = 0
    rtts_ms: list[float] = field(default_factory=list)
    start_ns: int = 0
    end_ns: int = 0
    server_stats: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def aggregate_run(results: list[ClientResult]) -> dict[str, Any]:
    """Combine client results into run-level metrics.

    Throughput uses receiver-side bytes over the receiver's active window
    (first to last arrival across all sessions), like iperf's server report.
    """
    sent = sum(r.sent for r in results)
    stats = [r.server_stats for r in results if r.server_stats]
    received = sum(s["unique"] for s in stats)
    rx_bytes = sum(s["bytes_total"] for s in stats)
    payload_unique = sum(s["payload_bytes_unique"] for s in stats)
    firsts = [s["first_recv_ns"] for s in stats if s.get("first_recv_ns") is not None]
    lasts = [s["last_recv_ns"] for s in stats if s.get("last_recv_ns") is not None]

    starts = [r.start_ns for r in results if r.sent]
    ends = [r.end_ns for r in results if r.sent]
    completion_s = (max(ends) - min(starts)) / NS_PER_S if starts and ends else 0.0

    rx_window_s = (max(lasts) - min(firsts)) / NS_PER_S if firsts and lasts else 0.0
    if rx_window_s <= 0:  # single packet or no data: fall back to client wall time
        rx_window_s = completion_s

    all_rtts = [x for r in results for x in r.rtts_ms]
    jitters = [s["jitter_ms"] for s in stats if s.get("jitter_ms") is not None]
    owds = [s["owd_avg_ms"] for s in stats if s.get("owd_avg_ms") is not None]
    loss = loss_percent(sent, received)
    errors = [f"c{r.client_id}: {r.error}" for r in results if r.error]

    row: dict[str, Any] = {
        "sent": sent,
        "received": received,
        "rx_bytes": rx_bytes,
        "throughput_mbps": throughput_mbps(rx_bytes, rx_window_s),
        "goodput_mbps": throughput_mbps(payload_unique, rx_window_s),
        **rtt_summary(all_rtts),
        "rtt_samples": len(all_rtts),
        "owd_avg_ms": float(np.mean(owds)) if owds else None,
        "jitter_ms": float(np.mean(jitters)) if jitters else None,
        "loss_pct": loss,
        "delivery_ratio_pct": 100.0 - loss if sent else 0.0,
        "out_of_order": sum(s["out_of_order"] for s in stats),
        "duplicates": sum(s["duplicates"] for s in stats),
        "completion_time_s": completion_s,
        "status": "ok" if not errors and len(stats) == len(results) else "error",
        "error": "; ".join(errors)[:500],
    }
    return row


def safe_float(x: Any) -> float:
    """Convert to float, mapping ``None``/invalid to NaN."""
    try:
        v = float(x)
        return v if math.isfinite(v) else math.nan
    except (TypeError, ValueError):
        return math.nan
