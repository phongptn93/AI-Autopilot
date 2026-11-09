"""Read-only probes for the "hidden half" of running software: disk and certificates.

Neither is a code bug. A full volume turns every database on it read-only; an expired
certificate makes a healthy system look unsafe for a morning. Both give no warning unless
somebody looks, so this looks — on a schedule, with a number and a threshold.

    python scripts/ops_probe.py --disk /var/opt/mssql --cert app.example.com:443

Exit code: 0 all fine, 1 at least one WARN, 2 at least one CRITICAL, 3 a probe could not run.
A probe that cannot run is its own exit code on purpose: "could not check" must never read
as "fine". Standard library only.
"""

from __future__ import annotations

import argparse
import shutil
import socket
import ssl
import sys
from datetime import UTC, datetime

OK, WARN, CRIT, UNKNOWN = "ok", "warn", "critical", "unknown"
_EXIT = {OK: 0, WARN: 1, CRIT: 2, UNKNOWN: 3}


def classify_disk(used_pct: float, warn: float = 80.0, crit: float = 90.0) -> str:
    return CRIT if used_pct >= crit else WARN if used_pct >= warn else OK


def classify_cert(days_left: int, warn: int = 30, crit: int = 7) -> str:
    return CRIT if days_left <= crit else WARN if days_left <= warn else OK


def disk_used_pct(path: str) -> float:
    usage = shutil.disk_usage(path)
    return 100.0 * usage.used / usage.total if usage.total else 0.0


def cert_days_left(host: str, port: int = 443, timeout: float = 10.0,
                   now: datetime | None = None) -> int:
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock, \
            ctx.wrap_socket(sock, server_hostname=host) as tls:
        cert = tls.getpeercert() or {}
    expires = datetime.strptime(str(cert["notAfter"]), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
    return (expires - (now or datetime.now(UTC))).days


def parse_hostport(spec: str) -> tuple[str, int]:
    host, _, port = spec.partition(":")
    return host, int(port) if port else 443


def run(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Read-only disk and certificate probes.")
    p.add_argument("--disk", action="append", default=[], metavar="PATH")
    p.add_argument("--cert", action="append", default=[], metavar="HOST[:PORT]")
    p.add_argument("--disk-warn", type=float, default=80.0)
    p.add_argument("--disk-crit", type=float, default=90.0)
    p.add_argument("--cert-warn", type=int, default=30)
    p.add_argument("--cert-crit", type=int, default=7)
    args = p.parse_args(argv)
    if not (args.disk or args.cert):
        p.error("give at least one --disk or --cert")

    worst = OK
    rank = [OK, WARN, CRIT, UNKNOWN]

    def report(level: str, line: str) -> None:
        nonlocal worst
        if rank.index(level) > rank.index(worst) and not (worst == CRIT and level == UNKNOWN):
            worst = level
        print(f"[{level.upper():<8}] {line}")

    for path in args.disk:
        try:
            pct = disk_used_pct(path)
            level = classify_disk(pct, args.disk_warn, args.disk_crit)
            report(level, f"disk {path}: {pct:.1f}% used")
        except OSError as exc:
            report(UNKNOWN, f"disk {path}: cannot read ({exc})")
    for spec in args.cert:
        try:
            host, port = parse_hostport(spec)
            days = cert_days_left(host, port)
            report(classify_cert(days, args.cert_warn, args.cert_crit),
                   f"cert {spec}: {days} day(s) left")
        except (OSError, ValueError, KeyError) as exc:
            report(UNKNOWN, f"cert {spec}: cannot check ({exc})")
    return _EXIT[worst]


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
