"""Tests for loss detection and metric calculations."""

from __future__ import annotations

import math

import pytest

from common.metrics import (
    ClientResult,
    ReceiverStats,
    aggregate_run,
    loss_percent,
    rfc3550_jitter,
    rtt_summary,
    throughput_mbps,
)

MS = 1_000_000


def feed(stats: ReceiverStats, seqs: list[int], size: int = 100) -> None:
    for i, s in enumerate(seqs):
        stats.on_packet(s, send_ts_ns=s * MS, recv_ts_ns=s * MS + 5 * MS + i, nbytes=size, payload_len=size - 15)


def test_loss_detection_on_sequence_with_gaps() -> None:
    st = ReceiverStats()
    feed(st, [0, 1, 2, 5, 6, 9])  # missing 3,4,7,8
    assert st.unique == 6
    assert st.lost() == 4
    assert st.gaps() == [(3, 4), (7, 8)]
    # Sender reports 12 sent -> tail loss of 10, 11 is also counted.
    assert st.lost(sent_total=12) == 6


def test_duplicates_and_out_of_order() -> None:
    st = ReceiverStats()
    feed(st, [0, 1, 3, 2, 2, 4, 1])
    assert st.received_total == 7
    assert st.unique == 5
    assert st.duplicates == 2
    assert st.out_of_order == 1  # seq 2 arrived after 3
    assert st.lost(sent_total=5) == 0


def test_no_loss() -> None:
    st = ReceiverStats()
    feed(st, list(range(100)))
    assert st.lost(100) == 0 and st.gaps() == []
    d = st.to_dict(100)
    assert d["lost"] == 0 and d["unique"] == 100
    assert d["owd_avg_ms"] == pytest.approx(5.0, abs=0.01)


def test_jitter_zero_for_constant_transit() -> None:
    assert rfc3550_jitter([0, 10, 20, 30], [5, 15, 25, 35]) == 0.0


def test_jitter_rfc3550_formula() -> None:
    # transit: 5, 7, 5 -> D=2 then 2 -> J = 2/16, then J += (2 - J)/16
    j1 = 2 / 16
    j2 = j1 + (2 - j1) / 16
    assert rfc3550_jitter([0, 10, 20], [5, 17, 25]) == pytest.approx(j2)


def test_receiver_jitter_matches_helper() -> None:
    send = [0, 10, 20, 30, 40]
    recv = [3, 16, 22, 35, 41]
    st = ReceiverStats()
    for i, (s, r) in enumerate(zip(send, recv)):
        st.on_packet(i, s * MS, r * MS, 100, 85)
    assert st.jitter_ns / MS == pytest.approx(rfc3550_jitter(send, recv))


def test_throughput_and_loss_percent() -> None:
    assert throughput_mbps(1_250_000, 1.0) == pytest.approx(10.0)
    assert throughput_mbps(1000, 0) == 0.0
    assert loss_percent(100, 95) == pytest.approx(5.0)
    assert loss_percent(0, 0) == 0.0
    assert loss_percent(10, 12) == 0.0  # duplicates never give negative loss


def test_rtt_summary() -> None:
    s = rtt_summary([float(x) for x in range(1, 101)])
    assert s["rtt_min_ms"] == 1 and s["rtt_max_ms"] == 100
    assert s["rtt_avg_ms"] == pytest.approx(50.5)
    assert s["rtt_p50_ms"] == pytest.approx(50.5)
    assert s["rtt_p99_ms"] == pytest.approx(99.01)
    assert all(v is None for v in rtt_summary([]).values())


def test_aggregate_run() -> None:
    def stats(unique: int, first: int, last: int) -> dict:
        return {"unique": unique, "bytes_total": unique * 1000, "payload_bytes_unique": unique * 985,
                "first_recv_ns": first, "last_recv_ns": last, "jitter_ms": 1.0, "owd_avg_ms": 2.0,
                "out_of_order": 1, "duplicates": 0}

    results = [
        ClientResult(0, sent=100, rtts_ms=[1.0, 3.0], start_ns=0, end_ns=2_000_000_000,
                     server_stats=stats(90, 0, 1_000_000_000)),
        ClientResult(1, sent=100, rtts_ms=[2.0], start_ns=0, end_ns=1_000_000_000,
                     server_stats=stats(100, 0, 1_000_000_000)),
    ]
    row = aggregate_run(results)
    assert row["sent"] == 200 and row["received"] == 190
    assert row["loss_pct"] == pytest.approx(5.0)
    assert row["delivery_ratio_pct"] == pytest.approx(95.0)
    assert row["throughput_mbps"] == pytest.approx(190_000 * 8 / 1e6)
    assert row["rtt_avg_ms"] == pytest.approx(2.0)
    assert row["completion_time_s"] == pytest.approx(2.0)
    assert row["out_of_order"] == 2
    assert row["status"] == "ok"


def test_aggregate_run_missing_stats_is_error() -> None:
    row = aggregate_run([ClientResult(0, sent=10, error="timeout")])
    assert row["status"] == "error" and row["loss_pct"] == 100.0
    assert not math.isnan(row["throughput_mbps"])
