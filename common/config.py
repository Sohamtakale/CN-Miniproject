"""Central configuration for the TCP vs UDP comparison system.

Every tunable constant lives here so that no other module contains magic numbers.
"""

from __future__ import annotations

from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
RESULTS_DIR: Path = PROJECT_ROOT / "results"
PLOTS_DIR: Path = RESULTS_DIR / "plots"
RESULTS_CSV: Path = RESULTS_DIR / "results.csv"
SUMMARY_CSV: Path = RESULTS_DIR / "summary.csv"
REPORT_DIR: Path = PROJECT_ROOT / "report"
REPORT_MD: Path = REPORT_DIR / "report.md"
CONCLUSION_MD: Path = REPORT_DIR / "conclusion.md"
NETEM_SCRIPT: Path = PROJECT_ROOT / "netem" / "netem.sh"

# --------------------------------------------------------------------------- #
# Network endpoints
# --------------------------------------------------------------------------- #
DEFAULT_HOST: str = "127.0.0.1"
TCP_PORT: int = 9001
UDP_PORT: int = 9002
# Ports the software emulator (proxy) listens on; it forwards to the servers.
EMU_TCP_PORT: int = 9101
EMU_UDP_PORT: int = 9102
# Separate port block used by the dashboard so it can run alongside the harness.
DASH_TCP_PORT: int = 9201
DASH_UDP_PORT: int = 9202
DASH_EMU_TCP_PORT: int = 9301
DASH_EMU_UDP_PORT: int = 9302
DEFAULT_IFACE: str = "lo"

# --------------------------------------------------------------------------- #
# Packet format
# --------------------------------------------------------------------------- #
# seq (uint32) | send timestamp ns (uint64) | payload length (uint16) | flags (uint8)
HEADER_FORMAT: str = "!IQHB"
# TCP length prefix (uint32) placed before every packet on the stream.
FRAME_PREFIX_FORMAT: str = "!I"
FLAG_DATA: int = 0x01
FLAG_ACK: int = 0x02
FLAG_END: int = 0x04
MAX_PACKET_SIZE: int = 65_000
MAX_UDP_DATAGRAM: int = 65_507

# --------------------------------------------------------------------------- #
# Socket behaviour
# --------------------------------------------------------------------------- #
SOCKET_BUFFER_BYTES: int = 4 * 1024 * 1024
TCP_NODELAY: bool = True            # disable Nagle so echoes are not batched
TCP_RECV_CHUNK: int = 65_536
CONNECT_TIMEOUT_S: float = 5.0
SOCKET_POLL_TIMEOUT_S: float = 0.2  # recv timeout used to poll stop flags
TCP_END_TIMEOUT_S: float = 120.0    # wait for END reply (stalls under loss)
TCP_SEND_TIMEOUT_S: float = 60.0    # max time one sendall may block
RUN_TIMEOUT_S: float = 300.0        # watchdog: abandon a run that takes longer
SLEEP_DRIFT_S: float = 2.0          # wall vs monotonic drift that flags host sleep
UDP_END_RETRIES: int = 12
UDP_END_MIN_TIMEOUT_S: float = 0.4
UDP_MIN_DRAIN_S: float = 0.3        # min wait for trailing ACKs after sending
UDP_MAX_DRAIN_S: float = 3.0
UDP_DRAIN_RTT_FACTOR: float = 2.5
SERVER_SESSION_TTL_S: float = 60.0  # forget idle UDP sessions after this
SERVER_READY_TIMEOUT_S: float = 10.0
PACING_SLEEP_THRESHOLD_S: float = 0.001
LIVE_SAMPLE_INTERVAL_S: float = 0.25
MAX_OWD_SAMPLES: int = 100_000

# --------------------------------------------------------------------------- #
# Network emulation
# --------------------------------------------------------------------------- #
JITTER_RATIO: float = 0.10          # jitter = delay * ratio when not given
NETEM_LIMIT_PKTS: int = 100         # queue limit (netem 'limit' + emulator)
EMU_TCP_MSS: int = 1448             # segment size used to model TCP loss
EMU_TCP_RECOVERY_MS: float = 25.0   # extra stall per loss event (+1 RTT)
EMU_TCP_INIT_CWND: int = 10           # segments (RFC 6928)
EMU_TCP_MAX_CWND: int = 10_000        # segments (cap)
EMU_TCP_BASE_RTT_MS: float = 1.0      # RTT floor for cwnd pacing on loopback
EMU_TCP_MAX_BACKLOG_BYTES: int = 128 * 1024  # proxy queue before backpressure
EMU_READY_TIMEOUT_S: float = 10.0
EMU_SCHED_IDLE_S: float = 0.05

# --------------------------------------------------------------------------- #
# Test matrix
# --------------------------------------------------------------------------- #
PROTOCOLS: list[str] = ["TCP", "UDP"]
PACKET_SIZES: list[int] = [64, 512, 1024, 1400]
LOSS_PCTS: list[float] = [0, 1, 5, 10]
DELAYS_MS: list[float] = [0, 50, 200]
# Bandwidth limits in Mbps; 0 means unlimited.
BANDWIDTHS_MBPS: list[float] = [0, 10, 1]
CLIENT_COUNTS: list[int] = [1, 5, 10]
DEFAULT_REPEATS: int = 3

# Per-client packet count and send rate (packets/s, 0 = unlimited) for the matrix.
MATRIX_NUM_PACKETS: int = 200
MATRIX_SEND_RATE_PPS: int = 1000
QUICK_NUM_PACKETS: int = 200
QUICK_REPEATS: int = 2

# Baseline used for one-factor-at-a-time slices (quick mode + analyzer).
BASELINE: dict[str, float] = {
    "packet_size": 1024,
    "loss_pct_cfg": 0,
    "delay_ms_cfg": 0,
    "bandwidth_mbps_cfg": 0,
    "num_clients": 1,
}

# Dashboard defaults
DASH_DEFAULT_PACKETS: int = 500
DASH_DEFAULT_RATE_PPS: int = 500
DASH_REFRESH_S: float = 0.3

# Analysis thresholds
TCP_COLLAPSE_FRACTION: float = 0.5  # TCP "collapses" below 50% of its baseline
