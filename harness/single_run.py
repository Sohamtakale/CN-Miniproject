"""Execute one test (one protocol, one network condition) and log it.

Library use: :func:`execute_run`. CLI example::

    python harness/single_run.py --protocol UDP --packet-size 1024 --loss 5 --delay 50 --clients 5
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import config  # noqa: E402
from common.utils import ProgressCallback, fmt_bw, read_tcp_retrans, setup_logging  # noqa: E402
from harness.results_io import append_row  # noqa: E402
from harness.servers import ServerManager  # noqa: E402
from netem.environment import MODES, NetworkEnvironment, PortSet, resolve_mode  # noqa: E402
from netem.netem_py import Conditions  # noqa: E402
from tcp.tcp_client import run_tcp_test  # noqa: E402
from udp.udp_client import run_udp_test  # noqa: E402


@dataclass(frozen=True)
class TestParams:
    """All inputs that define one test configuration."""

    protocol: str = "TCP"
    packet_size: int = 1024
    num_packets: int = config.MATRIX_NUM_PACKETS
    send_rate: int = config.MATRIX_SEND_RATE_PPS
    num_clients: int = 1
    loss_pct: float = 0
    delay_ms: float = 0
    bandwidth_mbps: float = 0
    jitter_ms: float | None = None

    @property
    def conditions(self) -> Conditions:
        return Conditions(self.delay_ms, self.jitter_ms, self.loss_pct, self.bandwidth_mbps)

    def combo_id(self) -> str:
        """Stable identifier of the parameter combination (used by --resume)."""
        return (f"{self.protocol}-s{self.packet_size}-l{self.loss_pct:g}-d{self.delay_ms:g}"
                f"-j{self.conditions.effective_jitter_ms:g}-bw{self.bandwidth_mbps:g}"
                f"-c{self.num_clients}-n{self.num_packets}-r{self.send_rate}")


def execute_run(
    params: TestParams,
    mode: str,
    ports: PortSet = PortSet(),
    iface: str = config.DEFAULT_IFACE,
    repeat: int = 0,
    progress_cb: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Apply conditions, run the clients and return one CSV row.

    ``mode`` must already be resolved (netem/software/direct). Servers must be running.
    """
    proto = params.protocol.upper()
    row: dict[str, Any] = {
        "run_id": uuid.uuid4().hex[:10],
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "combo_id": params.combo_id(),
        "repeat": repeat,
        "protocol": proto,
        "packet_size": params.packet_size,
        "loss_pct_cfg": params.loss_pct,
        "delay_ms_cfg": params.delay_ms,
        "jitter_ms_cfg": params.conditions.effective_jitter_ms,
        "bandwidth_mbps_cfg": params.bandwidth_mbps,
        "bandwidth_label": fmt_bw(params.bandwidth_mbps),
        "num_clients": params.num_clients,
        "num_packets": params.num_packets,
        "send_rate_pps": params.send_rate,
    }
    retrans_before, src = read_tcp_retrans() if proto == "TCP" else (None, "n/a")
    wall0, mono0 = time.time(), time.monotonic()  # monotonic stops while the host sleeps
    try:
        with NetworkEnvironment(params.conditions, proto, mode, ports, iface=iface) as env:
            runner = run_tcp_test if proto == "TCP" else run_udp_test
            metrics, _ = runner(config.DEFAULT_HOST, env.connect_port, params.packet_size, params.num_packets,
                                params.send_rate, params.num_clients, progress_cb)
        row.update(metrics)
    except Exception as exc:  # log failed runs instead of aborting the matrix
        row.update(status="error", error=f"{type(exc).__name__}: {exc}"[:500])
        return row

    slept = (time.time() - wall0) - (time.monotonic() - mono0)
    if slept > config.SLEEP_DRIFT_S:  # suspend/sleep corrupts every timing metric
        row.update(status="error", error=f"host slept/suspended for {slept:.0f} s during the run")
    row["emu_dropped"] = env.emu_stats.get("dropped") if env.emu_stats else None
    if proto == "UDP":
        row["retransmissions"], row["retrans_source"] = 0, "none (UDP)"
    elif mode == "software":
        row["retransmissions"] = env.emu_stats.get("tcp_loss_events") if env.emu_stats else None
        row["retrans_source"] = "emulator"
    else:
        after, src = read_tcp_retrans()
        ok = retrans_before is not None and after is not None
        row["retransmissions"] = after - retrans_before if ok else None
        row["retrans_source"] = src if ok else "n/a"
    return row


def print_row(row: dict[str, Any]) -> None:
    """Pretty-print the key metrics of a run."""
    def f(k: str, unit: str = "", nd: int = 3) -> str:
        v = row.get(k)
        return "-" if v is None or v == "" else (f"{v:.{nd}f}{unit}" if isinstance(v, float) else f"{v}{unit}")

    print(f"\n{row['protocol']} | {row['mode']} | size={row['packet_size']}B loss={row['loss_pct_cfg']:g}% "
          f"delay={row['delay_ms_cfg']:g}ms bw={row['bandwidth_label']} clients={row['num_clients']}")
    print(f"  status       : {row.get('status')} {row.get('error') or ''}")
    print(f"  sent/received: {f('sent')}/{f('received')}   loss={f('loss_pct', '%', 2)}  "
          f"delivery={f('delivery_ratio_pct', '%', 2)}")
    print(f"  throughput   : {f('throughput_mbps', ' Mbps')}   goodput={f('goodput_mbps', ' Mbps')}")
    print(f"  RTT ms       : avg={f('rtt_avg_ms')} min={f('rtt_min_ms')} p50={f('rtt_p50_ms')} "
          f"p95={f('rtt_p95_ms')} p99={f('rtt_p99_ms')} max={f('rtt_max_ms')}")
    print(f"  jitter       : {f('jitter_ms', ' ms')}   one-way delay={f('owd_avg_ms', ' ms')}")
    print(f"  reorder/dup  : {f('out_of_order')}/{f('duplicates')}   retrans={f('retransmissions')} "
          f"({row.get('retrans_source', '')})   emu_dropped={f('emu_dropped')}")
    print(f"  completion   : {f('completion_time_s', ' s')}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Run a single TCP or UDP test under given network conditions.")
    ap.add_argument("--protocol", choices=["TCP", "UDP", "tcp", "udp"], default="TCP")
    ap.add_argument("--packet-size", type=int, default=1024, help="bytes incl. header (default %(default)s)")
    ap.add_argument("--num-packets", type=int, default=config.MATRIX_NUM_PACKETS, help="per client")
    ap.add_argument("--send-rate", type=int, default=config.MATRIX_SEND_RATE_PPS, help="pps per client, 0=unlimited")
    ap.add_argument("--clients", type=int, default=1)
    ap.add_argument("--loss", type=float, default=0, help="percent")
    ap.add_argument("--delay", type=float, default=0, help="ms (one way)")
    ap.add_argument("--jitter", type=float, default=None, help=f"ms (default delay*{config.JITTER_RATIO})")
    ap.add_argument("--bandwidth", type=float, default=0, help="Mbps, 0 = unlimited")
    ap.add_argument("--mode", choices=MODES, default="auto", help="emulation mode (default %(default)s)")
    ap.add_argument("--iface", default=config.DEFAULT_IFACE, help="netem interface")
    ap.add_argument("--csv", type=Path, default=config.RESULTS_CSV, help="results CSV to append to")
    ap.add_argument("--no-log", action="store_true", help="do not append to the CSV")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    log = setup_logging(args.verbose, "single_run")

    mode, reason = resolve_mode(args.mode, args.iface)
    log.info("emulation mode: %s (%s)", mode, reason)
    params = TestParams(args.protocol.upper(), args.packet_size, args.num_packets, args.send_rate, args.clients,
                        args.loss, args.delay, args.bandwidth, args.jitter)
    with ServerManager():
        row = execute_run(params, mode, iface=args.iface)
    print_row(row)
    if not args.no_log:
        append_row(row, args.csv)
        log.info("appended run %s to %s", row["run_id"], args.csv)


if __name__ == "__main__":
    main()
