# TCP vs UDP Performance Comparison System

A complete measurement testbed that compares **TCP** and **UDP** on throughput, latency (RTT), jitter,
packet loss, reliability and QoS suitability under emulated network conditions (loss, delay, bandwidth
limits, concurrent clients). It produces CSV results, publication-style plots, a live Streamlit
dashboard, an auto-generated conclusion and a full project report.

```
tcp_udp_compare/
  common/     protocol.py (packet header + TCP framing), config.py (all constants), metrics.py, utils.py
  tcp/        tcp_server.py, tcp_client.py
  udp/        udp_server.py, udp_client.py
  netem/      netem.sh, netem_py.py (tc netem wrapper), emulator.py (software emulator), environment.py
  harness/    runner.py (test matrix), single_run.py (one test), servers.py, results_io.py
  analysis/   analyzer.py (stats + plots), conclusion.py, report_builder.py
  dashboard/  app.py (Streamlit)
  results/    results.csv, summary.csv, plots/
  report/     conclusion.md, report.md (generated)
  tests/      pytest suite (unit + loopback integration)
```

## 1. Install

Requires **Python 3.10+**. Target platform is Linux / WSL2; macOS and Windows work in software-emulation mode.

```bash
cd tcp_udp_compare
make setup                # creates .venv and installs requirements.txt
# or manually:
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

(Windows without `make`: run the `python ...` commands shown below with `.venv\Scripts\python`.)

## 2. Quick start / demo (5 minutes)

```bash
make test        # 26 tests: header round trip, loss detection, metrics, end-to-end via emulator
make quick       # 38 combos x 2 repeats (~2-3 min), then plots + conclusion
make report      # report/report.md with embedded plots
make dashboard   # http://localhost:8501
```

## 3. Running each part

| What | Command |
|---|---|
| TCP server / client | `python tcp/tcp_server.py --port 9001` / `python tcp/tcp_client.py --num-packets 1000 --num-clients 5 --send-rate 1000` |
| UDP server / client | `python udp/udp_server.py --port 9002` / `python udp/udp_client.py --packet-size 512 --num-clients 10` |
| One test, any condition | `python harness/single_run.py --protocol UDP --loss 5 --delay 50 --bandwidth 10 --clients 5` |
| Quick matrix | `python harness/runner.py --quick` |
| Full matrix (864 combos) | `python harness/runner.py --full --repeats 3` (add `--resume` to continue) |
| Only one protocol | `python harness/runner.py --quick --protocol UDP` |
| Plots | `python analysis/analyzer.py [--csv results/results.csv]` |
| Conclusion | `python analysis/conclusion.py` -> `report/conclusion.md` |
| Report | `python analysis/report_builder.py` -> `report/report.md` |
| Dashboard | `streamlit run dashboard/app.py` |
| Software emulator alone | `python netem/emulator.py --protocol udp --listen-port 9102 --target-port 9002 --loss 5 --delay 50` |

Every script supports `--help`. All constants (ports, matrix levels, timeouts, emulator model) live in
`common/config.py`.

**Runner flags:** `--quick` (default) / `--full`, `--protocol TCP|UDP|both`, `--repeats N`,
`--packets N` (per client), `--rate PPS` (0 = unlimited), `--resume`, `--mode auto|netem|software|direct`,
`--iface lo`, `--csv PATH`, `--limit N`. Every run is appended to `results/results.csv` with a timestamp,
run id and combo id; `results/summary.csv` holds the mean and std per combination.

**Duration.** Quick: ~2-3 min. Full with 1 repeat: ~1 h on a laptop (TCP at 1 Mbps / 200 ms with 10
clients is the slow corner); with the default 3 repeats, ~3 h. It is resumable with `--resume`.

## 4. Network emulation: two modes

| Mode | When | How |
|---|---|---|
| `netem` | Linux/WSL2 with root (or passwordless sudo) and `tc` | `tc qdisc replace dev lo root netem delay X ms Y ms loss Z% rate R mbit limit 100` |
| `software` | anywhere else (macOS, Windows, no sudo) | `netem/emulator.py`: a UDP/TCP proxy between clients and servers |
| `direct` | baseline, no impairment | clients talk straight to the servers |

`--mode auto` (default) picks netem if it is usable, else software, and logs which one is active (the
dashboard shows it in the sidebar).

**netem notes (sudo).**

```bash
sudo apt install iproute2                       # provides tc
make netem-check                                # "available: tc netem on lo" ?
sudo .venv/bin/python harness/runner.py --quick --mode netem
sudo ./netem/netem.sh apply lo 50 5 1 10        # manual: 50ms+-5ms, 1% loss, 10 Mbit
sudo ./netem/netem.sh show lo
sudo ./netem/netem.sh clear lo                  # always safe to run
```

`NetemController` clears the qdisc in its context-manager exit, at interpreter exit and on
SIGTERM/SIGHUP, so a crashed run does not leave `lo` impaired. If something is ever left behind, run
`sudo ./netem/netem.sh clear lo`. On `lo` netem impairs both directions, so RTT = 2 x delay.

**Software emulator model.** Per direction: random loss, delay + uniform jitter (UDP can be reordered),
a shared rate-limited link with a 100-packet tail-drop queue. A proxy cannot drop bytes from a TCP stream,
so a TCP loss *event* stalls the stream for one RTT + 25 ms (retransmission, head-of-line blocking) and
halves a Reno-style congestion window that paces the connection at cwnd x MSS / RTT. TCP throughput
therefore falls roughly as MSS / (RTT x sqrt(p)), as it does on real networks.

## 5. WSL2 notes

* WSL2 kernels include `sch_netem`; if `tc qdisc add ... netem` fails with "Unknown qdisc", update WSL
  (`wsl --update`) or use `--mode software`.
* Run the dashboard inside WSL and open `http://localhost:8501` in the Windows browser.
* VM scheduling adds noise: expect sub-millisecond RTT/jitter values to vary by ~0.1 ms between runs.

## 6. Packet format and metrics

| Field | Bytes |
|---|---|
| sequence number (uint32) | 4 |
| send timestamp, ns (uint64) | 8 |
| payload length (uint16) | 2 |
| flags: DATA=1, ACK=2, END=4 | 1 |
| payload (random) | packet_size - 15 |

TCP frames each packet with a 4-byte length prefix. The server ACKs each DATA packet (header only,
echoing seq + timestamp -> RTT). The client's END carries the number of packets sent; the server answers
with its receiver statistics as JSON (UDP retries END until that reply arrives).

* **Throughput** = server-received bytes / receive window; **goodput** = unique payload bytes / window
* **RTT** avg, min, max, p50, p95, p99 (ms); **one-way delay** (shared clock on loopback)
* **Jitter**: RFC 3550, J += (|D| - J) / 16
* **Loss %** = (sent - unique received) / sent; **delivery ratio** = 100 - loss; out-of-order; duplicates
* **TCP retransmissions**: `/proc/net/snmp` (Linux), `netstat -s` (macOS) or emulator loss events
* **Completion time**: first send until the END reply

## 7. Expected sample output

`python harness/single_run.py --protocol UDP --loss 5 --delay 50`:

```
UDP | software | size=1024B loss=5% delay=50ms bw=unlimited clients=1
  status       : ok
  sent/received: 200/193   loss=3.50%  delivery=96.50%
  throughput   : 7.733 Mbps   goodput=7.620 Mbps
  RTT ms       : avg=100.539 min=90.542 p50=100.362 p95=107.168 p99=108.898 max=109.810
  jitter       : 3.195 ms   one-way delay=49.984 ms
  reorder/dup  : 106/0   retrans=0 (none (UDP))   emu_dropped=16
  completion   : 0.723 s
```

`make quick` (excerpt from `report/conclusion.md`, software mode):

```
- Delay 50 ms: TCP starts at 4.67 Mbps with no loss; it collapses below 50% of that at 1% loss ...
- Delay alone (no loss): raising delay from 0 to 200 ms changes TCP throughput 8.42 -> 1.14 Mbps (-87%)
  and UDP 8.33 -> 7.25 Mbps (-13%).
| Application   | Recommended | Data-backed reason
| File transfer | TCP         | Every byte must arrive: TCP delivered 100.0% at 10% loss, UDP only 88.8% ...
| VoIP          | UDP         | Late audio is useless: ... TCP p95 RTT was 397 ms vs UDP 106 ms ...
```

Exact numbers vary with hardware and mode; the trends (TCP: 100% delivery, but throughput collapses and
latency inflates with loss x delay; UDP: steady rate and low latency, loss = configured loss) are stable.

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `Address already in use` | A previous server is still running: `pkill -f tcp_server.py; pkill -f udp_server.py`. The harness reuses servers already listening on its ports. |
| netem unavailable | Check `make netem-check`. You need Linux, `tc` (iproute2) and root or `sudo -n`. Otherwise `--mode software` is used automatically. |
| `lo` still slow after a crash | `sudo ./netem/netem.sh clear lo` |
| UDP loss at 0% configured | Socket buffers overflow at very high rates. Lower `--rate`, or raise `net.core.rmem_max` (Linux) / `kern.ipc.maxsockbuf` (macOS). |
| Runs marked `error` in the CSV | They are logged with the reason and excluded from analysis. Re-run with `--resume` to fill the gaps. |
| Dashboard port clash | The dashboard uses ports 9201/9202 (servers) and 9301/9302 (emulator), so it can run beside the harness (9001/9002, 9101/9102). |
| `ModuleNotFoundError: common` | Run scripts from the project root, e.g. `python harness/runner.py`. |
