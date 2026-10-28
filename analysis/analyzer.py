"""Statistics and matplotlib/seaborn plots from results.csv.

Each plot varies one factor. When the data contains rows where every *other*
factor sits at the baseline (``config.BASELINE``) only those rows are used
(a clean one-factor-at-a-time slice); otherwise the other factors are averaged.
Error bars are the standard deviation across runs.

Usage::

    python analysis/analyzer.py                       # results/results.csv -> results/plots/
    python analysis/analyzer.py --csv results/results_full.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import seaborn as sns  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import config  # noqa: E402
from harness.results_io import load_results  # noqa: E402

FACTORS = ["packet_size", "loss_pct_cfg", "delay_ms_cfg", "bandwidth_mbps_cfg", "num_clients"]
LABELS = {
    "packet_size": "Packet size (bytes)",
    "loss_pct_cfg": "Configured packet loss (%)",
    "delay_ms_cfg": "Configured one-way delay (ms)",
    "bandwidth_mbps_cfg": "Bandwidth limit (Mbps, 0 = unlimited)",
    "num_clients": "Concurrent clients",
    "throughput_mbps": "Throughput (Mbps)",
    "goodput_mbps": "Goodput (Mbps)",
    "rtt_avg_ms": "Mean RTT (ms)",
    "rtt_p95_ms": "95th-percentile RTT (ms)",
    "jitter_ms": "Jitter, RFC 3550 (ms)",
    "loss_pct": "Measured packet loss (%)",
    "delivery_ratio_pct": "Delivery ratio (%)",
    "completion_time_s": "Completion time (s)",
}
# Reference palette, categorical slots 1-2 (validated light-mode steps).
COLORS = {"TCP": "#2a78d6", "UDP": "#eb6834"}
MARKERS = {"TCP": "o", "UDP": "s"}
INK, INK_2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"


def setup_style() -> None:
    """Thin marks, recessive grid and axes, readable labels."""
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": GRID, "axes.labelcolor": INK_2, "axes.titlecolor": INK,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "axes.axisbelow": True,
        "xtick.color": INK_2, "ytick.color": INK_2, "text.color": INK,
        "font.size": 10, "axes.titlesize": 12, "axes.titleweight": "bold", "axes.labelsize": 10,
        "lines.linewidth": 2, "lines.markersize": 6, "legend.frameon": False,
        "figure.dpi": 110, "savefig.dpi": 140, "savefig.bbox": "tight",
    })


# --------------------------------------------------------------------------- #
# Data helpers (also used by conclusion.py, report builder and dashboard)
# --------------------------------------------------------------------------- #
def load(csv: Path = config.RESULTS_CSV, mode: str | None = None) -> pd.DataFrame:
    """Load ok runs, optionally only one emulation mode."""
    df = load_results(csv)
    if mode and not df.empty:
        df = df[df["mode"] == mode]
    return df


def slice_for(df: pd.DataFrame, factor: str | list[str]) -> pd.DataFrame:
    """Rows where all factors except ``factor`` are at the baseline (if enough exist)."""
    vary = [factor] if isinstance(factor, str) else factor
    mask = pd.Series(True, index=df.index)
    for f in FACTORS:
        if f not in vary:
            mask &= df[f] == config.BASELINE[f]
    sub = df[mask]
    ok = all(sub[v].nunique() >= 2 for v in vary)
    return sub if ok else df


def agg(df: pd.DataFrame, by: list[str], metric: str) -> pd.DataFrame:
    """Mean, std and count of ``metric`` grouped by ``by``."""
    g = df.groupby(by)[metric].agg(["mean", "std", "count"]).reset_index()
    g["std"] = g["std"].fillna(0)
    return g


def _line(ax: plt.Axes, df: pd.DataFrame, x: str, metric: str, log_y: bool = False, legend: bool = True) -> None:
    ends: list[tuple[str, float, float]] = []
    for proto in ["TCP", "UDP"]:
        d = df[df["protocol"] == proto]
        if d.empty:
            continue
        a = agg(d, [x], metric).sort_values(x)
        ax.errorbar(a[x], a["mean"], yerr=a["std"], color=COLORS[proto], marker=MARKERS[proto],
                    capsize=3, elinewidth=1, label=proto, markeredgecolor=SURFACE, markeredgewidth=1.5)
        ends.append((proto, float(a[x].iloc[-1]), float(a["mean"].iloc[-1])))
    ax.set_xlabel(LABELS.get(x, x))
    ax.set_ylabel(LABELS.get(metric, metric))
    if log_y:
        ax.set_yscale("log")
    # Selective direct labels at line ends, pushed apart when the ends nearly coincide.
    lo, hi = ax.get_ylim()
    close = len(ends) == 2 and abs(ends[0][2] - ends[1][2]) < 0.06 * (hi - lo)
    order = sorted(ends, key=lambda e: e[2])
    for proto, xe, ye in ends:
        dy = 0 if not close else (-7 if proto == order[0][0] else 7)
        ax.annotate(proto, (xe, ye), xytext=(7, dy), textcoords="offset points", va="center",
                    color=INK_2, fontsize=9)
    if legend:
        ax.legend(loc="best")


def _save(fig: plt.Figure, out: Path, name: str) -> Path:
    path = out / name
    fig.savefig(path)
    plt.close(fig)
    return path


# --------------------------------------------------------------------------- #
# Plots
# --------------------------------------------------------------------------- #
def plot_throughput_vs_loss(df: pd.DataFrame, out: Path) -> Path:
    """Throughput vs loss, one panel per configured delay."""
    sub = slice_for(df, ["loss_pct_cfg", "delay_ms_cfg"])
    delays = sorted(sub["delay_ms_cfg"].unique())
    fig, axes = plt.subplots(1, len(delays), figsize=(4.2 * len(delays), 3.8), sharey=True, squeeze=False)
    for ax, d in zip(axes[0], delays):
        _line(ax, sub[sub["delay_ms_cfg"] == d], "loss_pct_cfg", "throughput_mbps", legend=ax is axes[0][0])
        ax.set_title(f"Delay {d:g} ms")
        if ax is not axes[0][0]:
            ax.set_ylabel("")
    fig.suptitle("Throughput vs packet loss: TCP vs UDP", fontweight="bold")
    return _save(fig, out, "throughput_vs_loss.png")


def plot_latency_vs_delay(df: pd.DataFrame, out: Path) -> Path:
    sub = slice_for(df, "delay_ms_cfg")
    fig, ax = plt.subplots(figsize=(6.5, 4))
    xs = np.array(sorted(sub["delay_ms_cfg"].unique()))
    ax.plot(xs, 2 * xs, ls="--", lw=1, color=INK_2, label="Ideal RTT = 2 × delay")
    _line(ax, sub, "delay_ms_cfg", "rtt_avg_ms")
    ax.set_title("Round-trip latency vs configured delay")
    return _save(fig, out, "latency_vs_delay.png")


def plot_udp_loss(df: pd.DataFrame, out: Path) -> Path:
    sub = slice_for(df, "loss_pct_cfg")
    fig, ax = plt.subplots(figsize=(6.5, 4))
    xs = np.array(sorted(sub["loss_pct_cfg"].unique()))
    ax.plot(xs, xs, ls="--", lw=1, color=INK_2, label="Measured = configured")
    _line(ax, sub, "loss_pct_cfg", "loss_pct")
    ax.set_title("Measured application-level loss vs configured loss")
    return _save(fig, out, "udp_loss_vs_setting.png")


def plot_throughput_vs_size(df: pd.DataFrame, out: Path) -> Path:
    sub = slice_for(df, "packet_size")
    fig, ax = plt.subplots(figsize=(6.5, 4))
    _line(ax, sub, "packet_size", "throughput_mbps")
    ax.set_title("Throughput vs packet size (fixed packet rate)")
    return _save(fig, out, "throughput_vs_packet_size.png")


def plot_clients(df: pd.DataFrame, out: Path) -> Path:
    """Throughput and latency vs clients as two panels (never a dual axis)."""
    sub = slice_for(df, "num_clients")
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4))
    _line(a1, sub, "num_clients", "throughput_mbps")
    a1.set_title("Aggregate throughput vs clients")
    _line(a2, sub, "num_clients", "rtt_avg_ms", legend=False)
    a2.set_title("Mean RTT vs clients")
    return _save(fig, out, "clients_throughput_latency.png")


def plot_bandwidth(df: pd.DataFrame, out: Path) -> Path:
    sub = slice_for(df, "bandwidth_mbps_cfg")
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4))
    for ax, metric in ((a1, "throughput_mbps"), (a2, "loss_pct")):
        a = agg(sub, ["protocol", "bandwidth_mbps_cfg"], metric)
        a["bw"] = a["bandwidth_mbps_cfg"].map(lambda b: "unlimited" if b == 0 else f"{b:g} Mbps")
        order = [l for _, l in sorted({(b if b else 1e9, lab) for b, lab in zip(a["bandwidth_mbps_cfg"], a["bw"])})]
        _grouped_bars(ax, a, "bw", order, metric)
    a1.set_title("Throughput vs bandwidth limit")
    a2.set_title("Loss vs bandwidth limit")
    return _save(fig, out, "bandwidth_effect.png")


def _grouped_bars(ax: plt.Axes, a: pd.DataFrame, cat: str, order: list[str], metric: str) -> None:
    protos = [p for p in ["TCP", "UDP"] if p in set(a["protocol"])]
    x = np.arange(len(order))
    w = 0.8 / max(len(protos), 1)
    for i, proto in enumerate(protos):
        d = a[a["protocol"] == proto].set_index(cat).reindex(order)
        ax.bar(x + (i - (len(protos) - 1) / 2) * w, d["mean"], w * 0.92, yerr=d["std"], color=COLORS[proto],
               label=proto, capsize=2, error_kw={"elinewidth": 1, "ecolor": INK_2})
    ax.set_xticks(x, order)
    ax.set_ylabel(LABELS.get(metric, metric))
    ax.grid(axis="x", visible=False)
    ax.legend()


def plot_jitter(df: pd.DataFrame, out: Path) -> Path:
    sub = slice_for(df, ["loss_pct_cfg", "delay_ms_cfg"]).copy()
    sub["condition"] = sub.apply(lambda r: f"d{r.delay_ms_cfg:g}\nl{r.loss_pct_cfg:g}%", axis=1)
    a = agg(sub, ["protocol", "condition", "delay_ms_cfg", "loss_pct_cfg"], "jitter_ms")
    order = (a.drop_duplicates("condition").sort_values(["delay_ms_cfg", "loss_pct_cfg"])["condition"].tolist())
    fig, ax = plt.subplots(figsize=(max(7, 0.7 * len(order) + 2), 4.2))
    _grouped_bars(ax, a, "condition", order, "jitter_ms")
    ax.set_yscale("log")
    ax.set_xlabel("Condition (d = one-way delay ms, l = loss %)")
    ax.set_title("Jitter by network condition")
    return _save(fig, out, "jitter_vs_condition.png")


def plot_heatmaps(df: pd.DataFrame, out: Path, metric: str = "throughput_mbps") -> Path:
    sub = slice_for(df, ["loss_pct_cfg", "delay_ms_cfg"])
    protos = [p for p in ["TCP", "UDP"] if p in set(sub["protocol"])]
    piv = {p: sub[sub["protocol"] == p].pivot_table(index="loss_pct_cfg", columns="delay_ms_cfg",
                                                    values=metric, aggfunc="mean") for p in protos}
    vmax = max(float(np.nanmax(v.values)) for v in piv.values()) if piv else 1
    fig, axes = plt.subplots(1, len(protos), figsize=(5.2 * len(protos), 4), squeeze=False)
    cmap = sns.light_palette("#2a78d6", as_cmap=True)  # sequential: one hue, light -> dark
    for ax, p in zip(axes[0], protos):
        sns.heatmap(piv[p], ax=ax, annot=True, fmt=".2f", cmap=cmap, vmin=0, vmax=vmax, linewidths=2,
                    linecolor=SURFACE, cbar_kws={"label": LABELS[metric]})
        ax.set_title(f"{p}: {LABELS[metric]}")
        ax.set_xlabel("One-way delay (ms)")
        ax.set_ylabel("Packet loss (%)")
        ax.grid(False)
    return _save(fig, out, f"heatmap_{metric.split('_')[0]}_loss_delay.png")


def plot_delivery_ratio(df: pd.DataFrame, out: Path) -> Path:
    sub = slice_for(df, "loss_pct_cfg")
    a = agg(sub, ["protocol", "loss_pct_cfg"], "delivery_ratio_pct")
    a["loss"] = a["loss_pct_cfg"].map(lambda v: f"{v:g}% loss")
    order = [f"{v:g}% loss" for v in sorted(a["loss_pct_cfg"].unique())]
    fig, ax = plt.subplots(figsize=(7, 4))
    _grouped_bars(ax, a, "loss", order, "delivery_ratio_pct")
    lo = max(0, float((a["mean"] - a["std"]).min()) - 5)
    ax.set_ylim(lo, 101)
    ax.set_title("Delivery ratio by protocol")
    return _save(fig, out, "delivery_ratio.png")


PLOTS = [plot_throughput_vs_loss, plot_latency_vs_delay, plot_udp_loss, plot_throughput_vs_size,
         plot_clients, plot_bandwidth, plot_jitter, plot_heatmaps, plot_delivery_ratio]


def generate_all(df: pd.DataFrame, out: Path = config.PLOTS_DIR) -> list[Path]:
    """Create every plot; plots whose data is missing are skipped with a message."""
    setup_style()
    out.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for fn in PLOTS:
        try:
            paths.append(fn(df, out))
        except (KeyError, ValueError, IndexError) as exc:
            print(f"  skipped {fn.__name__}: {exc}")
    try:
        paths.append(plot_heatmaps(df, out, "rtt_avg_ms"))
    except (KeyError, ValueError) as exc:
        print(f"  skipped RTT heatmap: {exc}")
    return paths


def summary_table(df: pd.DataFrame) -> pd.DataFrame:
    """Per-protocol overall means for the headline metrics."""
    cols = ["throughput_mbps", "goodput_mbps", "rtt_avg_ms", "rtt_p95_ms", "jitter_ms", "loss_pct",
            "delivery_ratio_pct", "out_of_order", "duplicates", "completion_time_s"]
    return df.groupby("protocol")[cols].mean().round(3)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Analyze results.csv and generate plots.")
    ap.add_argument("--csv", type=Path, default=config.RESULTS_CSV)
    ap.add_argument("--out", type=Path, default=config.PLOTS_DIR)
    ap.add_argument("--mode", default=None, help="only use runs from this emulation mode")
    args = ap.parse_args(argv)
    df = load(args.csv, args.mode)
    if df.empty:
        sys.exit(f"no successful runs in {args.csv}; run the harness first (make quick)")
    print(f"Loaded {len(df)} ok runs, {df['combo_id'].nunique()} combos, modes={sorted(df['mode'].unique())}")
    print("\nOverall means by protocol:\n" + summary_table(df).T.to_string())
    paths = generate_all(df, args.out)
    print(f"\nWrote {len(paths)} plots to {args.out}:")
    for p in paths:
        print("  ", p.name)


if __name__ == "__main__":
    main()
