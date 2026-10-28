#!/usr/bin/env bash
# Apply / clear Linux tc-netem impairments on an interface.
#
# Usage:
#   netem.sh apply <iface> <delay_ms> <jitter_ms> <loss_pct> <rate_mbps|0> [limit_pkts]
#   netem.sh clear <iface>
#   netem.sh show  <iface>
#   netem.sh check <iface>
#
# Examples:
#   sudo ./netem.sh apply lo 50 5 1 10      # 50ms +-5ms delay, 1% loss, 10 Mbit/s
#   sudo ./netem.sh clear lo
#
# Note: on 'lo' the qdisc affects BOTH directions of loopback traffic, so the
# round-trip delay is 2 x delay_ms.
set -euo pipefail

usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

[[ $# -lt 2 ]] && usage
cmd="$1"; iface="$2"

if ! command -v tc >/dev/null 2>&1; then
  echo "error: 'tc' not found (install iproute2)" >&2; exit 2
fi

case "$cmd" in
  apply)
    [[ $# -lt 6 ]] && usage
    delay="$3"; jitter="$4"; loss="$5"; rate="$6"; limit="${7:-100}"
    args=(delay "${delay}ms")
    if awk "BEGIN{exit !($jitter > 0)}"; then args+=("${jitter}ms"); fi
    if awk "BEGIN{exit !($loss > 0)}"; then args+=(loss "${loss}%"); fi
    if awk "BEGIN{exit !($rate > 0)}"; then args+=(rate "${rate}mbit"); fi
    args+=(limit "$limit")
    tc qdisc replace dev "$iface" root netem "${args[@]}"
    echo "applied on $iface: netem ${args[*]}"
    ;;
  clear)
    tc qdisc del dev "$iface" root 2>/dev/null || true
    echo "cleared $iface"
    ;;
  show)
    tc qdisc show dev "$iface"
    ;;
  check)
    tc qdisc show dev "$iface" >/dev/null
    echo "ok"
    ;;
  *) usage ;;
esac
