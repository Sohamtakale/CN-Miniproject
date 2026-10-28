"""Generate plain-English, data-backed findings -> report/conclusion.md.

Usage::

    python analysis/conclusion.py [--csv results/results.csv] [--out report/conclusion.md]
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis.analyzer import load, slice_for  # noqa: E402
from common import config  # noqa: E402

# metric -> (pretty name, True if higher is better)
METRICS: dict[str, tuple[str, bool]] = {
    "throughput_mbps": ("Throughput", True),
    "rtt_avg_ms": ("Mean RTT", False),
    "rtt_p95_ms": ("p95 RTT", False),
    "jitter_ms": ("Jitter", False),
    "delivery_ratio_pct": ("Delivery ratio", True),
    "completion_time_s": ("Completion time", False),
}
TIE_TOLERANCE = 0.05  # within 5% counts as a tie


def _mean(df: pd.DataFrame, proto: str, metric: str, **where: float) -> float:
    d = df[df["protocol"] == proto]
    for k, v in where.items():
        d = d[d[k] == v]
    return float(d[metric].mean()) if not d.empty else math.nan


def winner(tcp: float, udp: float, higher_better: bool) -> str:
    """'TCP', 'UDP' or 'tie' (within TIE_TOLERANCE)."""
    if math.isnan(tcp) or math.isnan(udp):
        return "n/a"
    scale = max(abs(tcp), abs(udp), 1e-9)
    if abs(tcp - udp) / scale <= TIE_TOLERANCE:
        return "tie"
    return ("TCP" if tcp > udp else "UDP") if higher_better else ("TCP" if tcp < udp else "UDP")


def _f(v: float, nd: int = 2) -> str:
    return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.{nd}f}"


def winners_table(df: pd.DataFrame) -> pd.DataFrame:
    """Winner of every metric for every (loss, delay) condition at baseline otherwise."""
    grid = slice_for(df, ["loss_pct_cfg", "delay_ms_cfg"])
    rows = []
    for (loss, delay), _ in grid.groupby(["loss_pct_cfg", "delay_ms_cfg"]):
        r: dict[str, object] = {"Loss %": f"{loss:g}", "Delay ms": f"{delay:g}"}
        for m, (name, hib) in METRICS.items():
            t = _mean(grid, "TCP", m, loss_pct_cfg=loss, delay_ms_cfg=delay)
            u = _mean(grid, "UDP", m, loss_pct_cfg=loss, delay_ms_cfg=delay)
            r[name] = winner(t, u, hib)
        rows.append(r)
    return pd.DataFrame(rows)


def crossover(df: pd.DataFrame) -> list[str]:
    """Where TCP throughput collapses, per configured delay."""
    grid = slice_for(df, ["loss_pct_cfg", "delay_ms_cfg"])
    lines = []
    for delay in sorted(grid["delay_ms_cfg"].unique()):
        g = grid[grid["delay_ms_cfg"] == delay]
        losses = sorted(g["loss_pct_cfg"].unique())
        base = _mean(g, "TCP", "throughput_mbps", loss_pct_cfg=losses[0])
        collapse = below_udp = None
        for loss in losses:
            t = _mean(g, "TCP", "throughput_mbps", loss_pct_cfg=loss)
            u = _mean(g, "UDP", "throughput_mbps", loss_pct_cfg=loss)
            if collapse is None and t < config.TCP_COLLAPSE_FRACTION * base:
                collapse = (loss, t)
            if below_udp is None and t < u * (1 - TIE_TOLERANCE):
                below_udp = (loss, t, u)
        frac = int(config.TCP_COLLAPSE_FRACTION * 100)
        s = f"- **Delay {delay:g} ms:** TCP starts at {_f(base)} Mbps with no loss; "
        if collapse:
            s += f"it collapses below {frac}% of that at **{collapse[0]:g}% loss** ({_f(collapse[1])} Mbps). "
        else:
            s += f"it never falls below {frac}% of that within the tested loss range (max {losses[-1]:g}%). "
        if below_udp:
            s += (f"TCP falls behind UDP from **{below_udp[0]:g}% loss** "
                  f"({_f(below_udp[1])} vs {_f(below_udp[2])} Mbps).")
        else:
            s += "TCP stays level with UDP at every tested loss rate."
        lines.append(s)
    # delay-only effect
    d0 = slice_for(df, "delay_ms_cfg")
    delays = sorted(d0["delay_ms_cfg"].unique())
    if len(delays) > 1:
        t0, t1 = (_mean(d0, "TCP", "throughput_mbps", delay_ms_cfg=d) for d in (delays[0], delays[-1]))
        u0, u1 = (_mean(d0, "UDP", "throughput_mbps", delay_ms_cfg=d) for d in (delays[0], delays[-1]))
        lines.append(f"- **Delay alone (no loss):** raising delay from {delays[0]:g} to {delays[-1]:g} ms changes "
                     f"TCP throughput {_f(t0)} -> {_f(t1)} Mbps ({_pct(t1, t0)}) and UDP {_f(u0)} -> {_f(u1)} "
                     f"Mbps ({_pct(u1, u0)}). TCP is RTT-bound (slow start, ACK clocking); "
                     "UDP sends at the application's rate whatever the RTT.")
    return lines


def _ratio(a: float, b: float) -> str:
    if not b or math.isnan(a) or math.isnan(b):
        return "n/a"
    return f"{a / b:.1f}x higher" if a >= b else f"{b / a:.1f}x lower"


def _pct(new: float, old: float) -> str:
    if not old or math.isnan(old) or math.isnan(new):
        return "n/a"
    return f"{(new - old) / old * 100:+.0f}%"


def udp_loss_table(df: pd.DataFrame) -> pd.DataFrame:
    grid = slice_for(df, ["loss_pct_cfg", "delay_ms_cfg"])
    u = grid[grid["protocol"] == "UDP"]
    if u.empty:
        return pd.DataFrame()
    t = u.pivot_table(index="loss_pct_cfg", columns="delay_ms_cfg", values="loss_pct", aggfunc="mean").round(2)
    t.index = [f"{i:g}% configured" for i in t.index]
    t.columns = [f"delay {c:g} ms" for c in t.columns]
    return t


def qos_table(df: pd.DataFrame) -> pd.DataFrame:
    """Application -> recommended protocol with the measurement that justifies it."""
    grid = slice_for(df, ["loss_pct_cfg", "delay_ms_cfg"])
    losses = sorted(grid["loss_pct_cfg"].unique())
    delays = sorted(grid["delay_ms_cfg"].unique())
    hi_loss = losses[-1]
    mid_loss = next((l for l in losses if l >= 5), hi_loss)
    mid_delay = next((d for d in delays if d > 0), delays[-1])
    low_loss = next((l for l in losses if l > 0), hi_loss)

    udp_deliv = _mean(grid, "UDP", "delivery_ratio_pct", loss_pct_cfg=hi_loss)
    tcp_deliv = _mean(grid, "TCP", "delivery_ratio_pct", loss_pct_cfg=hi_loss)
    tcp_tp0 = _mean(grid, "TCP", "throughput_mbps", loss_pct_cfg=0, delay_ms_cfg=mid_delay)
    tcp_tpm = _mean(grid, "TCP", "throughput_mbps", loss_pct_cfg=mid_loss, delay_ms_cfg=mid_delay)
    udp_tp0 = _mean(grid, "UDP", "throughput_mbps", loss_pct_cfg=0, delay_ms_cfg=mid_delay)
    udp_tpm = _mean(grid, "UDP", "throughput_mbps", loss_pct_cfg=mid_loss, delay_ms_cfg=mid_delay)
    tcp_p95 = _mean(grid, "TCP", "rtt_p95_ms", loss_pct_cfg=low_loss, delay_ms_cfg=mid_delay)
    udp_p95 = _mean(grid, "UDP", "rtt_p95_ms", loss_pct_cfg=low_loss, delay_ms_cfg=mid_delay)
    tcp_j = _mean(grid, "TCP", "jitter_ms", loss_pct_cfg=mid_loss, delay_ms_cfg=mid_delay)
    udp_j = _mean(grid, "UDP", "jitter_ms", loss_pct_cfg=mid_loss, delay_ms_cfg=mid_delay)
    tcp_rtt0 = _mean(grid, "TCP", "rtt_avg_ms", loss_pct_cfg=0, delay_ms_cfg=0)
    udp_rtt0 = _mean(grid, "UDP", "rtt_avg_ms", loss_pct_cfg=0, delay_ms_cfg=0)
    tcp_rttm = _mean(grid, "TCP", "rtt_avg_ms", loss_pct_cfg=low_loss, delay_ms_cfg=mid_delay)
    udp_rttm = _mean(grid, "UDP", "rtt_avg_ms", loss_pct_cfg=low_loss, delay_ms_cfg=mid_delay)

    cond = f"{mid_loss:g}% loss / {mid_delay:g} ms"
    rows = [
        ("File transfer", "TCP",
         f"Every byte must arrive: TCP delivered {_f(tcp_deliv, 1)}% at {hi_loss:g}% loss, UDP only "
         f"{_f(udp_deliv, 1)}%. Slower completion is acceptable."),
        ("Web browsing", "TCP",
         f"Pages must be complete and in order (TCP: 0 reordered/lost). Cost: TCP mean RTT {_f(tcp_rttm, 0)} ms vs "
         f"UDP {_f(udp_rttm, 0)} ms at {low_loss:g}% loss / {mid_delay:g} ms, so keep connections short-lived "
         "(or use QUIC: reliable streams over UDP)."),
        ("Video streaming (live)", "UDP",
         f"Needs steady rate more than perfection: at {cond} UDP kept {_f(udp_tpm)} of {_f(udp_tp0)} Mbps "
         f"({_pct(udp_tpm, udp_tp0)}) while TCP fell from {_f(tcp_tp0)} to {_f(tcp_tpm)} Mbps "
         f"({_pct(tcp_tpm, tcp_tp0)}). Codecs/FEC hide the loss. (Buffered VOD often uses TCP/HTTP.)"),
        ("VoIP", "UDP",
         f"Late audio is useless: at {low_loss:g}% loss / {mid_delay:g} ms TCP p95 RTT was {_f(tcp_p95, 0)} ms vs "
         f"UDP {_f(udp_p95, 0)} ms ({_ratio(tcp_p95, udp_p95)}), because a lost TCP segment holds back every "
         f"later packet. " + (f"Jitter at {cond}: TCP {_f(tcp_j)} ms vs UDP {_f(udp_j)} ms. " if udp_j < tcp_j else "")
         + "A few lost 20 ms frames are inaudible; concealment covers them."),
        ("Online gaming", "UDP",
         f"Latency must stay low and predictable under loss: at {low_loss:g}% loss / {mid_delay:g} ms the mean RTT "
         f"was TCP {_f(tcp_rttm, 0)} ms vs UDP {_f(udp_rttm, 0)} ms (no head-of-line blocking); on a clean link "
         f"they are comparable (TCP {_f(tcp_rtt0, 3)} / UDP {_f(udp_rtt0, 3)} ms). Stale position updates can "
         "simply be dropped."),
    ]
    return pd.DataFrame(rows, columns=["Application", "Recommended", "Data-backed reason"])


def extra_findings(df: pd.DataFrame) -> list[str]:
    out = []
    bw = slice_for(df, "bandwidth_mbps_cfg")
    limits = sorted(b for b in bw["bandwidth_mbps_cfg"].unique() if b > 0)
    if limits:
        b = limits[0]
        ul, tl = (_mean(bw, p, "loss_pct", bandwidth_mbps_cfg=b) for p in ("UDP", "TCP"))
        ut, tt = (_mean(bw, p, "throughput_mbps", bandwidth_mbps_cfg=b) for p in ("UDP", "TCP"))
        ur, tr = (_mean(bw, p, "rtt_avg_ms", bandwidth_mbps_cfg=b) for p in ("UDP", "TCP"))
        s = (f"- **Bandwidth bottleneck ({b:g} Mbps):** throughput is capped at the link rate "
             f"(TCP {_f(tt)} / UDP {_f(ut)} Mbps) and the bottleneck queue inflates RTT to {_f(tr, 0)} ms (TCP) / "
             f"{_f(ur, 0)} ms (UDP). ")
        if ul > tl + 1:
            s += (f"UDP has no congestion control, so it kept sending above capacity and lost {_f(ul, 1)}% of its "
                  f"packets to queue overflow; TCP slowed down instead and lost {_f(tl, 1)}%.")
        else:
            s += (f"In these runs the offered load fitted in the queue, so neither protocol lost packets "
                  f"(UDP {_f(ul, 1)}%, TCP {_f(tl, 1)}%).")
        out.append(s)
    cl = slice_for(df, "num_clients")
    cs = sorted(cl["num_clients"].unique())
    if len(cs) > 1:
        parts = []
        for p in ("TCP", "UDP"):
            a, z = (_mean(cl, p, "throughput_mbps", num_clients=c) for c in (cs[0], cs[-1]))
            parts.append(f"{p} {_f(a)} -> {_f(z)} Mbps")
        out.append(f"- **Concurrency:** going from {cs[0]} to {cs[-1]} clients scales aggregate throughput "
                   f"({'; '.join(parts)}).")
    sz = slice_for(df, "packet_size")
    ss = sorted(sz["packet_size"].unique())
    if len(ss) > 1:
        parts = []
        for p in ("TCP", "UDP"):
            a, z = (_mean(sz, p, "goodput_mbps", packet_size=s) for s in (ss[0], ss[-1]))
            parts.append(f"{p} {_f(a)} -> {_f(z)} Mbps")
        hdr = 15 / ss[0] * 100
        out.append(f"- **Packet size:** at a fixed packet rate goodput grows with size ({'; '.join(parts)}); with "
                   f"{ss[0]} B packets the 15 B header alone is {hdr:.0f}% overhead (TCP adds 4 B framing).")
    g = slice_for(df, ["loss_pct_cfg", "delay_ms_cfg"])
    ooo = g[g["protocol"] == "UDP"]["out_of_order"].max()
    if pd.notna(ooo) and ooo > 0:
        out.append(f"- **Reordering:** with delay jitter UDP saw up to {int(ooo)} out-of-order packets per run; "
                   "TCP always delivered in order (0), at the price of head-of-line blocking.")
    return out


def build_markdown(df: pd.DataFrame) -> str:
    """Full conclusion document."""
    modes = ", ".join(sorted(df["mode"].unique()))
    wt = winners_table(df)
    counts = {p: int((wt.iloc[:, 2:] == p).sum().sum()) for p in ("TCP", "UDP", "tie")}
    lines = [
        "# Conclusion (auto-generated)",
        "",
        f"_Based on {len(df)} successful runs, {df['combo_id'].nunique()} parameter combinations, "
        f"emulation mode: {modes}._",
        "",
        "## Headline",
        "",
        f"Across the loss x delay grid UDP wins **{counts['UDP']}** metric/condition comparisons, TCP wins "
        f"**{counts['TCP']}**, and **{counts['tie']}** are ties (within {int(TIE_TOLERANCE * 100)}%). "
        "TCP always wins on **reliability** (100% delivery, in order); UDP wins on **latency, jitter and "
        "throughput as soon as the path has loss or delay**. On a clean, low-latency link the two are "
        "nearly identical.",
        "",
        "## Where TCP throughput collapses",
        "",
        *crossover(df),
        "",
        "## Winner per metric and condition",
        "",
        wt.to_markdown(index=False),
        "",
        "## UDP measured loss (%) per condition",
        "",
        udp_loss_table(df).to_markdown(),
        "",
        "UDP loss tracks the configured loss (each direction is impaired independently, so only "
        "data-direction loss counts here); TCP's measured loss is always 0% because it retransmits.",
        "",
        "## Other findings",
        "",
        *extra_findings(df),
        "",
        "## QoS recommendation",
        "",
        qos_table(df).to_markdown(index=False),
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Write data-backed conclusions to report/conclusion.md.")
    ap.add_argument("--csv", type=Path, default=config.RESULTS_CSV)
    ap.add_argument("--out", type=Path, default=config.CONCLUSION_MD)
    ap.add_argument("--mode", default=None)
    args = ap.parse_args(argv)
    df = load(args.csv, args.mode)
    if df.empty:
        sys.exit(f"no successful runs in {args.csv}; run the harness first")
    md = build_markdown(df)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(md)
    print(md)
    print(f"\n[written to {args.out}]")


if __name__ == "__main__":
    main()
