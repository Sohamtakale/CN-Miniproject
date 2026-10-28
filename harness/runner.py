"""Run the full TCP vs UDP test matrix and log every run to results/results.csv.

Examples::

    python harness/runner.py --quick                 # ~38 combos x 2 repeats, a few minutes
    python harness/runner.py --full --repeats 3      # full factorial (864 combos), hours
    python harness/runner.py --full --resume         # continue an interrupted full run
    python harness/runner.py --quick --protocol UDP --mode software
"""

from __future__ import annotations

import argparse
import itertools
import logging
import sys
import time
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import config  # noqa: E402
from common.utils import setup_logging  # noqa: E402
from harness.results_io import append_row, completed_counts, write_summary  # noqa: E402
from harness.servers import ServerManager  # noqa: E402
from harness.single_run import TestParams  # noqa: E402
from harness.single_run import execute_run  # noqa: E402
from netem.environment import MODES, resolve_mode  # noqa: E402

log = logging.getLogger("runner")


def full_matrix(protocols: list[str], num_packets: int, rate: int) -> list[TestParams]:
    """Full factorial: protocol x size x loss x delay x bandwidth x clients."""
    return [
        TestParams(p, s, num_packets, rate, c, l, d, b)
        for p, s, l, d, b, c in itertools.product(
            protocols, config.PACKET_SIZES, config.LOSS_PCTS, config.DELAYS_MS,
            config.BANDWIDTHS_MBPS, config.CLIENT_COUNTS)
    ]


def quick_matrix(protocols: list[str], num_packets: int, rate: int) -> list[TestParams]:
    """Demo subset: a loss x delay grid plus one-factor-at-a-time sweeps around the baseline.

    Enough to draw every plot (heatmap, vs loss, vs delay, vs size, vs bandwidth,
    vs clients) in a few minutes.
    """
    b = config.BASELINE
    base = dict(packet_size=int(b["packet_size"]), loss_pct=b["loss_pct_cfg"], delay_ms=b["delay_ms_cfg"],
                bandwidth_mbps=b["bandwidth_mbps_cfg"], num_clients=int(b["num_clients"]))
    variants: list[dict] = [dict(base, loss_pct=l, delay_ms=d)
                            for l, d in itertools.product(config.LOSS_PCTS, config.DELAYS_MS)]
    variants += [dict(base, packet_size=s) for s in config.PACKET_SIZES]
    variants += [dict(base, bandwidth_mbps=bw) for bw in config.BANDWIDTHS_MBPS]
    variants += [dict(base, num_clients=c) for c in config.CLIENT_COUNTS]
    seen: set[tuple] = set()
    out: list[TestParams] = []
    for p in protocols:
        for v in variants:
            key = (p, *sorted(v.items()))
            if key in seen:
                continue
            seen.add(key)
            out.append(TestParams(protocol=p, num_packets=num_packets, send_rate=rate, **v))
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Run the TCP vs UDP test matrix.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--quick", action="store_true", help="small demo subset (default)")
    g.add_argument("--full", action="store_true", help="full factorial matrix")
    ap.add_argument("--protocol", choices=["TCP", "UDP", "both"], default="both",
                    type=lambda s: "both" if s.lower() == "both" else s.upper(), help="protocol(s) to test (default both)")
    ap.add_argument("--repeats", type=int, default=None, help="repetitions per combo "
                    f"(default {config.QUICK_REPEATS} quick / {config.DEFAULT_REPEATS} full)")
    ap.add_argument("--packets", type=int, default=None, help="packets per client "
                    f"(default {config.QUICK_NUM_PACKETS} quick / {config.MATRIX_NUM_PACKETS} full)")
    ap.add_argument("--rate", type=int, default=config.MATRIX_SEND_RATE_PPS, help="pps per client, 0 = unlimited")
    ap.add_argument("--resume", action="store_true", help="skip combos that already have enough ok runs")
    ap.add_argument("--mode", choices=MODES, default="auto", help="emulation mode (default %(default)s)")
    ap.add_argument("--iface", default=config.DEFAULT_IFACE)
    ap.add_argument("--csv", type=Path, default=config.RESULTS_CSV)
    ap.add_argument("--limit", type=int, default=None, help="only run the first N combos (debugging)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    setup_logging(args.verbose, "runner")

    full = args.full
    repeats = args.repeats or (config.DEFAULT_REPEATS if full else config.QUICK_REPEATS)
    packets = args.packets or (config.MATRIX_NUM_PACKETS if full else config.QUICK_NUM_PACKETS)
    protocols = config.PROTOCOLS if args.protocol.lower() == "both" else [args.protocol.upper()]
    combos = (full_matrix if full else quick_matrix)(protocols, packets, args.rate)
    if args.limit:
        combos = combos[:args.limit]

    mode, reason = resolve_mode(args.mode, args.iface)
    log.info("emulation mode: %s - %s", mode, reason)
    done = completed_counts(args.csv) if args.resume else {}
    jobs = [(p, r) for p in combos for r in range(repeats) if r >= done.get(p.combo_id(), 0)]
    log.info("%s matrix: %d combos x %d repeats = %d runs (%d to do, %d skipped by --resume)",
             "full" if full else "quick", len(combos), repeats, len(combos) * repeats, len(jobs),
             len(combos) * repeats - len(jobs))

    errors = 0
    t0 = time.time()
    with ServerManager():
        bar = tqdm(jobs, unit="run", dynamic_ncols=True)
        for params, rep in bar:
            bar.set_description(f"{params.protocol} s{params.packet_size} l{params.loss_pct:g} "
                                f"d{params.delay_ms:g} bw{params.bandwidth_mbps:g} c{params.num_clients}")
            row = execute_run(params, mode, iface=args.iface, repeat=rep)
            append_row(row, args.csv)
            if row.get("status") != "ok":
                errors += 1
                tqdm.write(f"  run failed: {params.combo_id()} rep {rep}: {row.get('error')}")
    summary = write_summary(args.csv)
    log.info("finished %d runs in %.1f s (%d errors); results: %s; summary: %d combos -> %s",
             len(jobs), time.time() - t0, errors, args.csv, len(summary), config.SUMMARY_CSV)


if __name__ == "__main__":
    main()
