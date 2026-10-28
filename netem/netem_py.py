"""Python wrapper around ``netem.sh`` (Linux ``tc qdisc ... netem``).

Use :class:`NetemController` as a context manager so impairments are always
removed, even if the test crashes::

    with NetemController("lo") as nc:
        nc.apply(Conditions(delay_ms=50, loss_pct=1))
        ...  # run traffic
    # qdisc cleared here (also on exceptions, SIGTERM and interpreter exit)
"""

from __future__ import annotations

import argparse
import atexit
import os
import platform
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import FrameType, TracebackType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import config  # noqa: E402


@dataclass(frozen=True)
class Conditions:
    """Network impairment settings (0 = none)."""

    delay_ms: float = 0.0
    jitter_ms: float | None = None   # None -> delay_ms * config.JITTER_RATIO
    loss_pct: float = 0.0
    rate_mbps: float = 0.0           # 0 = unlimited

    @property
    def effective_jitter_ms(self) -> float:
        return self.delay_ms * config.JITTER_RATIO if self.jitter_ms is None else self.jitter_ms

    def label(self) -> str:
        bw = "unlimited" if not self.rate_mbps else f"{self.rate_mbps:g}Mbps"
        return f"delay={self.delay_ms:g}ms jitter={self.effective_jitter_ms:g}ms loss={self.loss_pct:g}% bw={bw}"

    def is_clean(self) -> bool:
        return not (self.delay_ms or self.loss_pct or self.rate_mbps or self.effective_jitter_ms)


def _sudo_prefix() -> list[str]:
    if os.geteuid() == 0:
        return []
    return ["sudo", "-n"] if shutil.which("sudo") else []


def netem_available(iface: str = config.DEFAULT_IFACE, probe: bool = True) -> tuple[bool, str]:
    """Check whether netem can be used: Linux, ``tc`` present, root/sudo, module loads.

    Returns ``(available, reason)``.
    """
    if platform.system() != "Linux":
        return False, f"netem needs Linux (this is {platform.system()})"
    if not shutil.which("tc"):
        return False, "'tc' not found (apt install iproute2)"
    if os.geteuid() != 0:
        if not shutil.which("sudo"):
            return False, "not root and sudo missing"
        if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode != 0:
            return False, "not root and passwordless sudo unavailable (run with sudo)"
    if probe:
        try:
            ctl = NetemController(iface, register_cleanup=False)
            ctl.apply(Conditions())
            ctl.clear()
        except (RuntimeError, OSError) as exc:
            return False, f"netem probe failed: {exc}"
    return True, f"tc netem on {iface}"


class NetemController:
    """Apply/clear netem on one interface; restores the interface on exit."""

    def __init__(self, iface: str = config.DEFAULT_IFACE, register_cleanup: bool = True) -> None:
        self.iface = iface
        self.active = False
        if register_cleanup:
            atexit.register(self.clear)
            for sig in (signal.SIGTERM, signal.SIGHUP):
                try:
                    prev = signal.getsignal(sig)
                    signal.signal(sig, self._make_handler(prev))
                except (ValueError, OSError):  # not main thread / unsupported
                    pass

    def _make_handler(self, prev: object):
        def handler(signum: int, frame: FrameType | None) -> None:
            self.clear()
            if callable(prev):
                prev(signum, frame)
            else:
                raise SystemExit(128 + signum)
        return handler

    def _run(self, *args: str) -> str:
        cmd = [*_sudo_prefix(), "bash", str(config.NETEM_SCRIPT), *args]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"netem.sh {' '.join(args)} failed")
        return proc.stdout.strip()

    def apply(self, cond: Conditions) -> str:
        """Install (replace) the netem qdisc with the given conditions."""
        out = self._run("apply", self.iface, f"{cond.delay_ms:g}", f"{cond.effective_jitter_ms:g}",
                        f"{cond.loss_pct:g}", f"{cond.rate_mbps:g}", str(config.NETEM_LIMIT_PKTS))
        self.active = True
        return out

    def clear(self) -> None:
        """Remove the qdisc (idempotent, never raises)."""
        if not self.active:
            return
        try:
            self._run("clear", self.iface)
        except (RuntimeError, OSError, subprocess.SubprocessError):
            pass
        self.active = False

    def show(self) -> str:
        return self._run("show", self.iface)

    def __enter__(self) -> "NetemController":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.clear()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Apply/clear tc netem conditions (Linux, needs root).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("apply", help="apply conditions")
    a.add_argument("--delay", type=float, default=0, help="ms")
    a.add_argument("--jitter", type=float, default=None, help="ms (default delay*%.2f)" % config.JITTER_RATIO)
    a.add_argument("--loss", type=float, default=0, help="percent")
    a.add_argument("--rate", type=float, default=0, help="Mbps, 0 = unlimited")
    for name in ("clear", "show", "check"):
        sub.add_parser(name)
    ap.add_argument("--iface", default=config.DEFAULT_IFACE)
    args = ap.parse_args(argv)

    if args.cmd == "check":
        ok, reason = netem_available(args.iface)
        print(("available: " if ok else "unavailable: ") + reason)
        sys.exit(0 if ok else 1)
    ctl = NetemController(args.iface, register_cleanup=False)
    if args.cmd == "apply":
        print(ctl.apply(Conditions(args.delay, args.jitter, args.loss, args.rate)))
    elif args.cmd == "clear":
        ctl.active = True
        ctl.clear()
        print(f"cleared {args.iface}")
    else:
        print(ctl.show())


if __name__ == "__main__":
    main()
