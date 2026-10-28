"""Streamlit dashboard for the TCP vs UDP comparison system.

Run with::

    streamlit run dashboard/app.py

The dashboard starts its own TCP/UDP servers on a separate port block
(``config.DASH_*``) so it can run alongside the test harness. Network
conditions use tc-netem when available, otherwise the software emulator.
"""

from __future__ import annotations

import atexit
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis import conclusion as concl  # noqa: E402
from common import config  # noqa: E402
from common.utils import fmt_bw  # noqa: E402
from harness.results_io import CSV_COLUMNS, append_row, load_results  # noqa: E402
from harness.servers import ServerManager  # noqa: E402
from harness.single_run import TestParams, execute_run  # noqa: E402
from netem.environment import DASHBOARD_PORTS, resolve_mode  # noqa: E402

COLORS = {"TCP": "#2a78d6", "UDP": "#eb6834"}
FACTORS = {
    "protocol": "Protocol", "packet_size": "Packet size (B)", "loss_pct_cfg": "Loss (%)",
    "delay_ms_cfg": "Delay (ms)", "bandwidth_mbps_cfg": "Bandwidth (Mbps, 0 = unlimited)",
    "num_clients": "Clients", "mode": "Emulation mode",
}
METRICS = {
    "throughput_mbps": "Throughput (Mbps)", "goodput_mbps": "Goodput (Mbps)", "rtt_avg_ms": "Mean RTT (ms)",
    "rtt_p95_ms": "p95 RTT (ms)", "rtt_p99_ms": "p99 RTT (ms)", "jitter_ms": "Jitter (ms)",
    "loss_pct": "Packet loss (%)", "delivery_ratio_pct": "Delivery ratio (%)",
    "out_of_order": "Out-of-order packets", "duplicates": "Duplicates",
    "retransmissions": "TCP retransmissions", "completion_time_s": "Completion time (s)",
}
# metric -> higher is better?
COMPARE_METRICS = {
    "throughput_mbps": True, "goodput_mbps": True, "rtt_avg_ms": False, "rtt_p95_ms": False,
    "jitter_ms": False, "loss_pct": False, "delivery_ratio_pct": True, "completion_time_s": False,
}

st.set_page_config(page_title="TCP vs UDP Performance", page_icon="📶", layout="wide")


# --------------------------------------------------------------------------- #
# One-time resources
# --------------------------------------------------------------------------- #
@st.cache_resource(show_spinner="Starting TCP/UDP servers...")
def get_servers() -> ServerManager:
    mgr = ServerManager(DASHBOARD_PORTS).start()
    atexit.register(mgr.stop)
    return mgr


@st.cache_data(show_spinner=False)
def detect(requested: str) -> tuple[str, str]:
    try:
        return resolve_mode(requested)
    except RuntimeError as exc:
        return "software", f"{exc}; falling back to software emulator"


def csv_files() -> list[Path]:
    files = sorted(config.RESULTS_DIR.glob("*.csv"), key=lambda p: (p.name != "results.csv", p.name))
    return [f for f in files if f.name != "summary.csv"]


@st.cache_data(show_spinner=False)
def load_csv(path: str, mtime: float) -> pd.DataFrame:  # mtime busts the cache when the file changes
    return load_results(Path(path))


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
def sidebar() -> tuple[TestParams, str, bool]:
    sb = st.sidebar
    sb.title("📶 TCP vs UDP")
    req = sb.selectbox("Emulation mode", ["auto", "software", "netem", "direct"],
                       help="auto = tc netem if available (Linux + root), else the software emulator")
    mode, reason = detect(req)
    badge = {"netem": "🟢", "software": "🟡", "direct": "⚪"}[mode]
    sb.markdown(f"**Active:** {badge} `{mode}`")
    sb.caption(reason)

    sb.subheader("Test parameters")
    proto = sb.radio("Protocol (Live Test)", ["TCP", "UDP"], horizontal=True)
    size = sb.select_slider("Packet size (bytes)", options=config.PACKET_SIZES + [4000, 8000], value=1024)
    packets = sb.number_input("Packets per client", 10, 100_000, config.DASH_DEFAULT_PACKETS, step=100)
    rate = sb.number_input("Send rate per client (pps, 0 = unlimited)", 0, 100_000, config.DASH_DEFAULT_RATE_PPS,
                           step=100)
    sb.subheader("Network conditions")
    loss = sb.slider("Packet loss (%)", 0.0, 30.0, 0.0, 0.5)
    delay = sb.slider("One-way delay (ms)", 0, 500, 0, 10)
    jitter = sb.slider("Jitter ± (ms)", 0, 100, int(delay * config.JITTER_RATIO), 1)
    bw = sb.selectbox("Bandwidth limit", [0.0, 100.0, 10.0, 5.0, 1.0], format_func=fmt_bw)
    clients = sb.select_slider("Concurrent clients", options=[1, 2, 5, 10, 20], value=1)
    log_csv = sb.checkbox("Append runs to results/results.csv", value=False)
    params = TestParams(proto, int(size), int(packets), int(rate), int(clients), loss, float(delay), bw,
                        float(jitter))
    return params, mode, log_csv


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def run_with_live_charts(params: TestParams, mode: str) -> dict[str, Any]:
    """Run a test in a worker thread while redrawing live charts."""
    get_servers()
    q: queue.Queue[dict] = queue.Queue()
    out: dict[str, Any] = {}
    t = threading.Thread(target=lambda: out.update(execute_run(params, mode, DASHBOARD_PORTS, progress_cb=q.put)),
                         daemon=True)
    t.start()
    hist: dict[str, list[float]] = {"t": [], "tp": [], "rtt_t": [], "rtt": [], "loss": []}
    status = st.status(f"Running {params.protocol} test...", expanded=True)
    c1, c2, c3 = status.columns(3)
    ph = [c1.empty(), c2.empty(), c3.empty()]
    n = 0
    while t.is_alive() or not q.empty():
        changed = False
        while not q.empty():
            s = q.get()
            hist["t"].append(s["elapsed_s"])
            hist["tp"].append(s["throughput_mbps"])
            hist["loss"].append(s["est_loss_pct"])
            for r in s["rtts"][-200:]:
                hist["rtt_t"].append(s["elapsed_s"])
                hist["rtt"].append(r)
            changed = True
        if changed:
            n += 1
            color = COLORS[params.protocol]
            ph[0].plotly_chart(_live_fig(hist["t"], hist["tp"], "Throughput (ACKed, Mbps)", color),
                               width="stretch", key=f"tp{n}")
            ph[1].plotly_chart(_live_fig(hist["rtt_t"], hist["rtt"], "RTT samples (ms)", color, markers=True),
                               width="stretch", key=f"rtt{n}")
            ph[2].plotly_chart(_live_fig(hist["t"], hist["loss"], "Unacknowledged so far (%)", color),
                               width="stretch", key=f"loss{n}")
        time.sleep(config.DASH_REFRESH_S)
    t.join()
    ok = out.get("status") == "ok"
    status.update(label=f"{params.protocol} test {'finished' if ok else 'failed'}",
                  state="complete" if ok else "error", expanded=True)
    return out


def _live_fig(x: list[float], y: list[float], title: str, color: str, markers: bool = False) -> go.Figure:
    fig = go.Figure(go.Scatter(x=x, y=y, mode="markers" if markers else "lines",
                               line={"color": color, "width": 2}, marker={"color": color, "size": 4}))
    fig.update_layout(title=title, height=260, margin=dict(l=10, r=10, t=40, b=10),
                      xaxis_title="Elapsed (s)", showlegend=False)
    return fig


def metric_cards(row: dict[str, Any], cols: int = 4) -> None:
    def v(k: str, nd: int = 2, unit: str = "") -> str:
        x = row.get(k)
        return "-" if x is None or x == "" else (f"{x:.{nd}f}{unit}" if isinstance(x, float) else f"{x}{unit}")

    cards = [
        ("Throughput", v("throughput_mbps", 2, " Mbps")), ("Goodput", v("goodput_mbps", 2, " Mbps")),
        ("Mean RTT", v("rtt_avg_ms", 2, " ms")), ("p95 RTT", v("rtt_p95_ms", 2, " ms")),
        ("Jitter", v("jitter_ms", 3, " ms")), ("Packet loss", v("loss_pct", 2, " %")),
        ("Delivery ratio", v("delivery_ratio_pct", 2, " %")), ("Completion", v("completion_time_s", 2, " s")),
        ("Sent / received", f"{row.get('sent', '-')} / {row.get('received', '-')}"),
        ("Out-of-order / dup", f"{row.get('out_of_order', '-')} / {row.get('duplicates', '-')}"),
        ("Retransmissions", f"{v('retransmissions', 0)} ({row.get('retrans_source', '')})"),
        ("Emulator drops", v("emu_dropped", 0)),
    ]
    grid = st.columns(cols)
    for i, (label, val) in enumerate(cards):
        grid[i % cols].metric(label, val)


def winner_badge(metric: str, tcp: Any, udp: Any) -> str:
    try:
        w = concl.winner(float(tcp), float(udp), COMPARE_METRICS[metric])
    except (TypeError, ValueError):
        return "n/a"
    return {"TCP": ":blue-background[**TCP wins**]", "UDP": ":orange-background[**UDP wins**]",
            "tie": ":gray-background[tie]"}.get(w, "n/a")


def filter_widgets(df: pd.DataFrame, key: str, defaults: dict[str, list] | None = None) -> pd.DataFrame:
    cols = st.columns(len(FACTORS))
    for c, (f, label) in zip(cols, FACTORS.items()):
        opts = sorted(df[f].dropna().unique().tolist())
        default = (defaults or {}).get(f, opts)
        default = [d for d in default if d in opts] or opts
        sel = c.multiselect(label, opts, default=default, key=f"{key}_{f}")
        df = df[df[f].isin(sel)]
    return df


# --------------------------------------------------------------------------- #
# Tabs
# --------------------------------------------------------------------------- #
def tab_live(params: TestParams, mode: str, log_csv: bool) -> None:
    st.subheader("Live test")
    st.caption(f"{params.protocol} · {params.packet_size} B × {params.num_packets} packets × {params.num_clients} "
               f"client(s) @ {params.send_rate or 'unlimited'} pps · {params.conditions.label()} · mode `{mode}`")
    if st.button("▶ Run test", type="primary"):
        row = run_with_live_charts(params, mode)
        st.session_state["live_row"] = row
        if log_csv and row.get("status") == "ok":
            append_row(row)
            st.toast(f"Run {row['run_id']} appended to results.csv")
    row = st.session_state.get("live_row")
    if row:
        if row.get("status") != "ok":
            st.error(f"Run failed: {row.get('error')}")
        st.markdown(f"**Result: {row['protocol']}** ({row['mode']}, run `{row['run_id']}`)")
        metric_cards(row)


def tab_compare(params: TestParams, mode: str, log_csv: bool) -> None:
    st.subheader("TCP vs UDP under the same conditions")
    st.caption(f"{params.packet_size} B × {params.num_packets} packets × {params.num_clients} client(s) · "
               f"{params.conditions.label()} · mode `{mode}`")
    if st.button("⚖ Run both back to back", type="primary"):
        get_servers()
        rows = {}
        bar = st.progress(0.0, "Running TCP...")
        for i, proto in enumerate(["TCP", "UDP"]):
            p = TestParams(proto, params.packet_size, params.num_packets, params.send_rate, params.num_clients,
                           params.loss_pct, params.delay_ms, params.bandwidth_mbps, params.jitter_ms)
            rows[proto] = execute_run(p, mode, DASHBOARD_PORTS)
            if log_csv and rows[proto].get("status") == "ok":
                append_row(rows[proto])
            bar.progress((i + 1) / 2, "Running UDP..." if i == 0 else "Done")
        st.session_state["compare"] = rows
    rows = st.session_state.get("compare")
    if not rows:
        st.info("Set conditions in the sidebar and press the button.")
        return
    tcp, udp = rows["TCP"], rows["UDP"]
    for r in (tcp, udp):
        if r.get("status") != "ok":
            st.error(f"{r['protocol']} run failed: {r.get('error')}")
    table = []
    for m, hib in COMPARE_METRICS.items():
        table.append({"Metric": METRICS[m] + (" ↑" if hib else " ↓"), "TCP": tcp.get(m), "UDP": udp.get(m),
                      "Winner": winner_badge(m, tcp.get(m), udp.get(m))})
    c1, c2 = st.columns(2)
    for col, r, name in ((c1, tcp, "TCP"), (c2, udp, "UDP")):
        with col:
            st.markdown(f"#### :{'blue' if name == 'TCP' else 'orange'}[{name}]")
            metric_cards(r, cols=3)
    st.markdown("#### Winner per metric (↑ higher is better, ↓ lower is better)")
    md = "| Metric | TCP | UDP | Winner |\n|---|---:|---:|---|\n" + "\n".join(
        f"| {t['Metric']} | {_num(t['TCP'])} | {_num(t['UDP'])} | {t['Winner']} |" for t in table)
    st.markdown(md)
    bars = pd.DataFrame([{"Metric": METRICS[m], "Protocol": p, "Value": rows[p].get(m)}
                         for m in ("throughput_mbps", "rtt_avg_ms", "jitter_ms", "loss_pct") for p in ("TCP", "UDP")])
    fig = px.bar(bars, x="Protocol", y="Value", color="Protocol", facet_col="Metric", color_discrete_map=COLORS,
                 height=320)
    fig.update_yaxes(matches=None, showticklabels=True)
    fig.for_each_annotation(lambda a: a.update(text=a.text.split("=")[-1]))
    st.plotly_chart(fig, width="stretch")


def _num(v: Any) -> str:
    return "-" if v is None or v == "" else (f"{v:.3f}" if isinstance(v, float) else str(v))


def pick_csv(key: str) -> pd.DataFrame | None:
    files = csv_files()
    if not files:
        st.warning("No results yet. Run `make quick` (or the Live Test with 'append to CSV') first.")
        return None
    f = st.selectbox("Results file", files, format_func=lambda p: p.name, key=key)
    df = load_csv(str(f), f.stat().st_mtime)
    if df.empty:
        st.warning(f"{f.name} has no successful runs.")
        return None
    return df


def tab_explorer() -> None:
    st.subheader("Results explorer")
    df = pick_csv("explorer_csv")
    if df is None:
        return
    with st.expander("Filters", expanded=True):
        fdf = filter_widgets(df, "ex")
    st.caption(f"{len(fdf)} of {len(df)} runs selected")
    if fdf.empty:
        return
    c1, c2, c3 = st.columns(3)
    x = c1.selectbox("X axis", [f for f in FACTORS if f != "protocol"], index=1, format_func=FACTORS.get)
    y = c2.selectbox("Y axis", list(METRICS), format_func=METRICS.get)
    kind = c3.radio("Chart", ["Mean ± std", "Box", "Scatter"], horizontal=True)
    if kind == "Mean ± std":
        g = fdf.groupby(["protocol", x])[y].agg(["mean", "std", "count"]).reset_index().fillna(0)
        fig = px.line(g, x=x, y="mean", error_y="std", color="protocol", markers=True, color_discrete_map=COLORS,
                      labels={"mean": METRICS[y], x: FACTORS[x]}, hover_data=["count"])
    elif kind == "Box":
        fig = px.box(fdf, x=x, y=y, color="protocol", color_discrete_map=COLORS,
                     labels={y: METRICS[y], x: FACTORS[x]})
    else:
        fig = px.scatter(fdf, x=x, y=y, color="protocol", color_discrete_map=COLORS,
                         hover_data=["combo_id", "repeat"], labels={y: METRICS[y], x: FACTORS[x]})
    fig.update_layout(height=450, legend_title_text="")
    st.plotly_chart(fig, width="stretch")
    st.dataframe(fdf[[c for c in CSV_COLUMNS if c in fdf]], width="stretch", height=300)
    st.download_button("⬇ Download filtered CSV", fdf.to_csv(index=False).encode(), "filtered_results.csv",
                       "text/csv")


def tab_heatmaps() -> None:
    st.subheader("Matrix: loss × delay heatmaps")
    df = pick_csv("heat_csv")
    if df is None:
        return
    c1, c2, c3, c4 = st.columns(4)
    metric = c1.selectbox("Metric", list(METRICS), format_func=METRICS.get, key="hm_metric")

    def pick(col: st.delta_generator.DeltaGenerator, f: str) -> Any:
        opts = ["(average)"] + sorted(df[f].unique().tolist())
        base = config.BASELINE[f]
        return col.selectbox(FACTORS[f], opts, index=opts.index(base) if base in opts else 0, key=f"hm_{f}")

    sel = {f: pick(c, f) for c, f in ((c2, "packet_size"), (c3, "bandwidth_mbps_cfg"), (c4, "num_clients"))}
    d = df
    for f, v in sel.items():
        if v != "(average)":
            d = d[d[f] == v]
    if d.empty:
        st.info("No runs for this selection.")
        return
    cols = st.columns(2)
    for col, proto in zip(cols, ["TCP", "UDP"]):
        p = d[d["protocol"] == proto].pivot_table(index="loss_pct_cfg", columns="delay_ms_cfg", values=metric,
                                                  aggfunc="mean")
        if p.empty:
            col.info(f"No {proto} data")
            continue
        fig = px.imshow(p, text_auto=".2f", aspect="auto", color_continuous_scale="Blues",
                        labels={"x": "One-way delay (ms)", "y": "Loss (%)", "color": METRICS[metric]},
                        x=[f"{c:g}" for c in p.columns], y=[f"{i:g}" for i in p.index])
        fig.update_layout(title=f"{proto}: {METRICS[metric]}", height=400)
        col.plotly_chart(fig, width="stretch")
    st.markdown("#### Full matrix: mean per combination")
    keys = ["protocol", "packet_size", "loss_pct_cfg", "delay_ms_cfg", "bandwidth_mbps_cfg", "num_clients"]
    piv = df.groupby(keys)[metric].agg(["mean", "std", "count"]).reset_index()
    st.dataframe(piv, width="stretch", height=300)
    plots = sorted(config.PLOTS_DIR.glob("*.png"))
    if plots:
        with st.expander(f"Static plots from the analyzer ({len(plots)})"):
            for i in range(0, len(plots), 2):
                cc = st.columns(2)
                for c, pth in zip(cc, plots[i:i + 2]):
                    c.image(str(pth), caption=pth.name)


def tab_conclusion() -> None:
    st.subheader("Conclusion")
    files = csv_files()
    if files and st.button("↻ Regenerate from results file"):
        f = files[0]
        df = load_results(f)
        if df.empty:
            st.warning("No successful runs to analyse.")
        else:
            config.CONCLUSION_MD.parent.mkdir(parents=True, exist_ok=True)
            config.CONCLUSION_MD.write_text(concl.build_markdown(df))
            st.toast(f"conclusion.md regenerated from {f.name}")
    if config.CONCLUSION_MD.exists():
        st.markdown(config.CONCLUSION_MD.read_text())
    else:
        st.info("No conclusion yet - run `make report` or press the button above.")


def tab_about(mode: str) -> None:
    st.subheader("About")
    c1, c2 = st.columns([3, 2])
    c1.markdown("""
**TCP** (Transmission Control Protocol) is connection-oriented and reliable: it numbers bytes, retransmits
lost segments, delivers data in order and adapts its rate with congestion control. The cost is extra
latency, head-of-line blocking (one lost segment delays everything behind it) and throughput that falls
sharply when loss and round-trip time grow (≈ MSS / (RTT·√loss)).

**UDP** (User Datagram Protocol) is connectionless: each datagram is independent, there is no
retransmission, no ordering and no congestion control. It has minimal latency and keeps the
application's sending rate, but lost and reordered packets are simply lost and reordered.

**How this system measures them fairly**

* The same 15-byte header (seq, send timestamp ns, length, flags) is used for both; TCP adds a
  4-byte length prefix because it is a byte stream.
* The server echoes a tiny ACK per packet → client-side **RTT**; it tracks gaps (**loss**),
  **duplicates**, **reordering**, RFC 3550 **jitter** and one-way delay, and returns them on END.
* Conditions come from **tc netem** (Linux) or the built-in **software emulator** (proxy with
  delay/jitter, loss, rate limiting with a bounded queue; TCP loss modelled as a stall + Reno cwnd halving).
""")
    c2.markdown(f"**Active emulation mode:** `{mode}`")
    c2.markdown("""
| Field | Size |
|---|---|
| Sequence number | 4 B |
| Send timestamp (ns) | 8 B |
| Payload length | 2 B |
| Flags (DATA/ACK/END) | 1 B |
| Payload (random) | N B |
""")
    st.markdown("#### Architecture")
    st.graphviz_chart("""
digraph G {
  rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#f2f1ed", color="#bdbcb6", fontname="Helvetica"];
  edge [color="#52514e", fontname="Helvetica", fontsize=10];
  runner [label="Harness\\nrunner.py / single_run.py"]; dash [label="Streamlit\\ndashboard"];
  clients [label="TCP / UDP clients\\n(N threads)"];
  emu [label="Network emulator\\ntc netem (Linux)\\nor software proxy", fillcolor="#fdebd9"];
  srv [label="TCP / UDP servers\\n(echo + receiver stats)"];
  csv [label="results.csv", shape=cylinder]; ana [label="analyzer.py\\nconclusion.py"];
  out [label="plots/ · conclusion.md\\nreport.md"];
  runner -> clients; dash -> clients;
  clients -> emu [label="DATA / END"]; emu -> srv; srv -> emu [label="ACK / stats"]; emu -> clients;
  runner -> csv [label="metrics row"]; csv -> ana -> out; csv -> dash [style=dashed];
}
""")


# --------------------------------------------------------------------------- #
def main() -> None:
    params, mode, log_csv = sidebar()
    st.title("TCP vs UDP Performance Comparison")
    tabs = st.tabs(["🔴 Live Test", "⚖ Compare", "🔎 Results Explorer", "🗺 Heatmaps / Matrix", "📝 Conclusion",
                    "ℹ About"])
    with tabs[0]:
        tab_live(params, mode, log_csv)
    with tabs[1]:
        tab_compare(params, mode, log_csv)
    with tabs[2]:
        tab_explorer()
    with tabs[3]:
        tab_heatmaps()
    with tabs[4]:
        tab_conclusion()
    with tabs[5]:
        tab_about(mode)


main()
