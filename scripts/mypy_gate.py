"""Fail CI on *new* mypy errors while the existing backlog is paid down.

The package carries a backlog of type errors from before mypy was enforced. Fixing
them all in one go is a large, risky change, so CI compares against a committed
baseline instead: an error already listed there is tolerated, anything new fails.

Errors are keyed by ``file + error code + message`` (no line numbers), so editing
code above an old error does not make it look new.

    python scripts/mypy_gate.py            # check (CI)
    python scripts/mypy_gate.py --update   # rewrite the baseline after fixing errors
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "mypy-baseline.txt"
_ERROR = re.compile(r"^(?P<file>[^:]+):\d+(?::\d+)?: error: (?P<msg>.*)$")


def current_errors() -> Counter[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "ai_autopilot", "--no-error-summary",
         "--hide-error-context", "--no-pretty", "--show-error-codes"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode not in (0, 1):  # 2 = mypy itself crashed / bad config
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit(f"mypy failed to run (exit {proc.returncode})")
    errors: Counter[str] = Counter()
    for line in proc.stdout.splitlines():
        m = _ERROR.match(line.strip())
        if m:
            errors[f"{m['file'].replace(chr(92), '/')}: {m['msg']}"] += 1
    # Exit 1 with no error lines is not "clean": `python -m mypy` without mypy installed
    # also exits 1, and reading that as zero errors invites `--update` to wipe the baseline.
    if proc.returncode == 1 and not errors:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit("mypy exited 1 but reported no errors — is mypy installed?")
    return errors


def read_baseline() -> Counter[str]:
    if not BASELINE.exists():
        return Counter()
    return Counter(ln for ln in BASELINE.read_text(encoding="utf-8").splitlines() if ln.strip())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--update", action="store_true", help="rewrite the baseline")
    args = ap.parse_args()

    now = current_errors()
    if args.update:
        BASELINE.write_text("".join(f"{k}\n" for k in sorted(now.elements())), encoding="utf-8")
        print(f"baseline written: {sum(now.values())} known errors")
        return 0

    base = read_baseline()
    new = now - base
    fixed = base - now
    print(f"mypy: {sum(now.values())} errors, baseline {sum(base.values())}")
    if fixed:
        print(f"{sum(fixed.values())} baseline errors are gone — run "
              "`python scripts/mypy_gate.py --update` to lock that in.")
    if new:
        print(f"\n{sum(new.values())} NEW mypy error(s):")
        for k in sorted(new.elements()):
            print(f"  {k}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
