"""CSV persistence for run results (append-only raw log + aggregated summary)."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pandas as pd

from common import config

PARAM_COLUMNS: list[str] = [
    "protocol", "packet_size", "loss_pct_cfg", "delay_ms_cfg", "jitter_ms_cfg",
    "bandwidth_mbps_cfg", "num_clients", "num_packets", "send_rate_pps",
]

METRIC_COLUMNS: list[str] = [
    "throughput_mbps", "goodput_mbps", "rtt_avg_ms", "rtt_min_ms", "rtt_max_ms",
    "rtt_p50_ms", "rtt_p95_ms", "rtt_p99_ms", "owd_avg_ms", "jitter_ms", "loss_pct",
    "delivery_ratio_pct", "out_of_order", "duplicates", "retransmissions", "completion_time_s",
]

CSV_COLUMNS: list[str] = [
    "run_id", "timestamp", "mode", "combo_id", "repeat", *PARAM_COLUMNS, "bandwidth_label",
    "sent", "received", "rx_bytes", "throughput_mbps", "goodput_mbps",
    "rtt_avg_ms", "rtt_min_ms", "rtt_max_ms", "rtt_p50_ms", "rtt_p95_ms", "rtt_p99_ms", "rtt_samples",
    "owd_avg_ms", "jitter_ms", "loss_pct", "delivery_ratio_pct", "out_of_order", "duplicates",
    "retransmissions", "retrans_source", "emu_dropped", "completion_time_s", "status", "error",
]


def append_row(row: dict[str, Any], path: Path = config.RESULTS_CSV) -> None:
    """Append one run to the CSV, writing the header if the file is new."""
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({k: _fmt(row.get(k)) for k in CSV_COLUMNS})


def _fmt(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, float):
        return round(v, 6)
    return v


def load_results(path: Path = config.RESULTS_CSV, ok_only: bool = True) -> pd.DataFrame:
    """Load the raw results CSV (empty DataFrame if missing)."""
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=CSV_COLUMNS)
    df = pd.read_csv(path)
    if ok_only and "status" in df:
        df = df[df["status"] == "ok"]
    return df


def completed_counts(path: Path = config.RESULTS_CSV) -> dict[str, int]:
    """Number of successful runs per combo_id (used by --resume)."""
    df = load_results(path)
    if df.empty:
        return {}
    return df.groupby("combo_id").size().to_dict()


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    """Mean and std of every metric per parameter combination."""
    if df.empty:
        return df
    keys = ["mode", *PARAM_COLUMNS]
    metrics = [m for m in METRIC_COLUMNS if m in df]
    g = df.groupby(keys, dropna=False)[metrics]
    out = g.mean().add_suffix("_mean").join(g.std().fillna(0).add_suffix("_std"))
    out["runs"] = g.size()
    return out.reset_index()


def write_summary(src: Path = config.RESULTS_CSV, dst: Path = config.SUMMARY_CSV) -> pd.DataFrame:
    """Recompute ``summary.csv`` from the raw log."""
    s = summarize(load_results(src))
    if not s.empty:
        s.to_csv(dst, index=False)
    return s
