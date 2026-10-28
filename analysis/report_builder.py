"""Build report/report.md: objective, architecture, methodology, matrix, plots,
results tables, analysis, conclusion, limitations and future work.

Usage::

    python analysis/report_builder.py [--csv results/results.csv]
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis import conclusion as concl  # noqa: E402
from analysis.analyzer import generate_all, load, slice_for, summary_table  # noqa: E402
from common import config  # noqa: E402

PLOT_CAPTIONS = [
    ("throughput_vs_loss.png", "Throughput vs configured loss, one panel per delay."),
    ("heatmap_throughput_loss_delay.png", "Throughput over the loss x delay grid."),
    ("heatmap_rtt_loss_delay.png", "Mean RTT over the loss x delay grid."),
    ("latency_vs_delay.png", "Mean RTT vs configured one-way delay (dashed: ideal 2 x delay)."),
    ("udp_loss_vs_setting.png", "Measured loss vs configured loss."),
    ("delivery_ratio.png", "Delivery ratio by protocol and loss setting."),
    ("jitter_vs_condition.png", "RFC 3550 jitter per condition (log scale)."),
    ("throughput_vs_packet_size.png", "Throughput vs packet size at a fixed packet rate."),
    ("clients_throughput_latency.png", "Aggregate throughput and mean RTT vs concurrent clients."),
    ("bandwidth_effect.png", "Throughput and loss under bandwidth limits."),
]

ARCHITECTURE = """```mermaid
flowchart LR
  R[Harness<br/>runner.py / single_run.py] --> C[TCP / UDP clients<br/>N threads]
  D[Streamlit dashboard] --> C
  C -- DATA / END --> E{{Network emulator<br/>tc netem or software proxy}}
  E --> S[TCP / UDP servers<br/>echo + receiver stats]
  S -- ACK / stats --> E --> C
  R -- one row per run --> CSV[(results.csv)]
  CSV --> A[analyzer.py / conclusion.py] --> O[plots, conclusion.md, report.md]
```"""


def _grid_table(df: pd.DataFrame, metric: str) -> str:
    g = slice_for(df, ["loss_pct_cfg", "delay_ms_cfg"])
    t = g.pivot_table(index="loss_pct_cfg", columns=["delay_ms_cfg", "protocol"], values=metric, aggfunc="mean")
    t.index = [f"loss {i:g}%" for i in t.index]
    t.columns = [f"{p} d={d:g}ms" for d, p in t.columns]
    return t.round(2).to_markdown()


def build(df: pd.DataFrame, csv: Path) -> str:
    generate_all(df)
    modes = ", ".join(sorted(df["mode"].unique()))
    n_combos = df["combo_id"].nunique()
    reps = df.groupby("combo_id").size()
    rel = os.path.relpath(config.PLOTS_DIR, config.REPORT_DIR)
    plots = "\n\n".join(f"**Figure {i}.** {cap}\n\n![{name}]({rel}/{name})"
                        for i, (name, cap) in enumerate([pc for pc in PLOT_CAPTIONS
                                                         if (config.PLOTS_DIR / pc[0]).exists()], 1))
    conclusion_body = concl.build_markdown(df).split("\n", 1)[1].replace("\n## ", "\n### ")

    matrix = pd.DataFrame([
        ("Protocol", ", ".join(config.PROTOCOLS)),
        ("Packet size (B)", ", ".join(map(str, config.PACKET_SIZES))),
        ("Loss (%)", ", ".join(f"{x:g}" for x in config.LOSS_PCTS)),
        ("One-way delay (ms)", ", ".join(f"{x:g}" for x in config.DELAYS_MS)
         + f" (jitter = {config.JITTER_RATIO:.0%} of delay)"),
        ("Bandwidth", ", ".join("unlimited" if not b else f"{b:g} Mbps" for b in config.BANDWIDTHS_MBPS)),
        ("Concurrent clients", ", ".join(map(str, config.CLIENT_COUNTS))),
        ("Repeats per combo", f"{config.DEFAULT_REPEATS} (full) / {config.QUICK_REPEATS} (quick)"),
        ("Packets per client / rate", f"{config.MATRIX_NUM_PACKETS} @ {config.MATRIX_SEND_RATE_PPS} pps"),
    ], columns=["Factor", "Levels"])

    return f"""# TCP vs UDP Performance Comparison - Project Report

_Generated {datetime.now():%Y-%m-%d %H:%M} from `{csv.name}` on {platform.system()} {platform.machine()},
Python {platform.python_version()}. Emulation mode: **{modes}**._

## 1. Objective

Measure and compare TCP and UDP on **throughput, delay (RTT), jitter, packet loss, reliability (delivery
ratio, ordering, duplicates)** and completion time under controlled network conditions (loss, delay,
bandwidth limits, concurrency), and derive data-backed recommendations for which transport suits which
class of application (QoS).

## 2. Architecture

{ARCHITECTURE}

* **common/** - shared packet format (`protocol.py`: 4 B seq, 8 B send timestamp ns, 2 B length, 1 B flags +
  random payload; TCP adds a 4 B length prefix), metrics (`metrics.py`), configuration (`config.py`).
* **tcp/, udp/** - threaded echo servers and multi-client load generators. The server ACKs every DATA
  packet (header only, original seq + timestamp, so the client gets RTT) and keeps receiver statistics,
  returned as JSON in reply to the END packet. UDP retries END until it gets the statistics back.
* **netem/** - `netem.sh` + `netem_py.py` drive Linux `tc qdisc ... netem`; `emulator.py` is a software
  impairment proxy used when netem is unavailable; `environment.py` picks the mode and guarantees cleanup.
* **harness/** - `single_run.py` (one test), `runner.py` (matrix, `--quick/--full/--resume`), CSV logging.
* **analysis/** - statistics/plots, the auto-conclusion and this report. **dashboard/** - Streamlit UI.

## 3. Methodology

1. TCP and UDP servers run as separate processes; for every run the emulator (or netem) is configured,
   N client threads send `num_packets` packets of `packet_size` bytes at a fixed packet rate, then END.
2. **Throughput** = bytes received by the server / receiver active window (first to last arrival);
   **goodput** counts unique payload bytes only. **RTT** = ACK arrival - send timestamp (client clock);
   avg/min/max/p50/p95/p99 over all ACKs. **One-way delay** = server arrival - send timestamp (valid
   because client and server share a clock on loopback). **Jitter** follows RFC 3550
   (J += (|D| - J)/16). **Loss** = (sent - unique received) / sent using sequence gaps plus the sent
   count carried in END; **delivery ratio** = 100 - loss; duplicates and out-of-order arrivals are
   counted per session. TCP **retransmissions** come from `/proc/net/snmp` (Linux) or `netstat -s`
   (macOS) deltas in netem mode, or from the emulator's loss-event count in software mode.
3. **Emulation.** netem on `lo` impairs every packet in both directions (RTT = 2 x delay). The software
   emulator reproduces that per direction: random loss, delay + uniform jitter (UDP may reorder),
   a shared rate-limited link with a {config.NETEM_LIMIT_PKTS}-packet tail-drop queue. Because a proxy
   cannot drop bytes from a TCP stream, a TCP loss event stalls the stream by one RTT +
   {config.EMU_TCP_RECOVERY_MS:g} ms (retransmission / head-of-line blocking) and halves a Reno-style
   congestion window (initial {config.EMU_TCP_INIT_CWND} segments) that paces the connection at
   cwnd x MSS / RTT.
4. Each combination is repeated; tables and plots show the mean, error bars the standard deviation.
   Plots vary one factor while the others sit at the baseline (1024 B, 0% loss, 0 ms, unlimited,
   1 client) when such runs exist; otherwise they average over the other factors.

## 4. Test matrix

{matrix.to_markdown(index=False)}

This report covers **{len(df)} successful runs** over **{n_combos} combinations** (repeats per combination:
min {reps.min()}, max {reps.max()}).

## 5. Results

### 5.1 Overall means by protocol

{summary_table(df).T.to_markdown()}

### 5.2 Throughput (Mbps) over the loss x delay grid

{_grid_table(df, "throughput_mbps")}

### 5.3 Mean RTT (ms) over the loss x delay grid

{_grid_table(df, "rtt_avg_ms")}

### 5.4 Measured loss (%) over the loss x delay grid

{_grid_table(df, "loss_pct")}

### 5.5 Plots

{plots}

## 6. Analysis and conclusion
{conclusion_body}

## 7. Limitations

* **Loopback, not a real network.** All traffic stays in one host's kernel: no real NIC, switch or
  propagation delay, and impairments are synthetic (uniform random loss, no burst loss or cross traffic).
* **Software emulation is a model.** In software mode TCP loss is modelled (stall + Reno cwnd) rather than
  produced by the kernel's real congestion control (CUBIC/BBR, SACK, RTO back-off); absolute TCP numbers
  under loss are indicative, the trends are what matter. netem mode on Linux removes this limitation.
* **VM / OS noise.** Python threads, the GIL, a user-space proxy and WSL2/VM scheduling add jitter of tens to
  hundreds of microseconds and cap the packet rate; very small RTT/jitter differences are within the noise.
* **Short flows.** {config.MATRIX_NUM_PACKETS} packets per client means TCP spends much of a high-RTT run in slow
  start, which depresses its throughput relative to long bulk transfers.
* **Sender-side bufferbloat.** Clients write into large ({config.SOCKET_BUFFER_BYTES // 2**20} MB) socket
  buffers. When the offered load exceeds a bandwidth limit, TCP data waits in those buffers and the
  application-level RTT (which starts at `send()`) includes that queueing, reaching tens of seconds at 1 Mbps.
  This is real behaviour for an application that writes faster than the path can carry.
* **Host sleep.** Runs during which the host slept are detected (wall-clock vs monotonic drift) and marked
  as errors; long matrices should run under `caffeinate` (macOS) or with sleep disabled.
* **Shared clock.** One-way delay is only meaningful because sender and receiver share a clock.
* System-wide retransmission counters (netem mode) include unrelated traffic.

## 8. Future work

* **QUIC** (e.g. `aioquic`): reliable, multiplexed streams over UDP without TCP's head-of-line blocking.
* **Congestion-control variants**: compare Reno, CUBIC and BBR (`sysctl net.ipv4.tcp_congestion_control`)
  under the same netem conditions.
* **Real multi-host tests** across Wi-Fi/LAN/WAN with PTP/NTP-synchronised clocks for one-way delay.
* Burst-loss models (Gilbert-Elliott, `netem loss gemodel`), reordering and corruption.
* Application-level QoS scores (MOS for VoIP, frame deadline misses for video and gaming).
"""


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Generate report/report.md from results.")
    ap.add_argument("--csv", type=Path, default=config.RESULTS_CSV)
    ap.add_argument("--out", type=Path, default=config.REPORT_MD)
    args = ap.parse_args(argv)
    df = load(args.csv)
    if df.empty:
        sys.exit(f"no successful runs in {args.csv}; run `make quick` first")
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    config.CONCLUSION_MD.write_text(concl.build_markdown(df))
    args.out.write_text(build(df, args.csv))
    print(f"wrote {args.out} and {config.CONCLUSION_MD} ({len(df)} runs)")


if __name__ == "__main__":
    main()
