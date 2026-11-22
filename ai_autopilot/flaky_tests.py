"""The flaky-test ledger: tests that failed and then passed on an immediate rerun.

A flaky test is worse than a failing one. It turns a good change red, teaches people
to press "rerun" without reading, and hides the day the test fails for real. The test
gate (``execution.test_gate``) reruns a red suite once; every failure signature that
disappears on the rerun is recorded here, per repo, with how often and when.

Kept beside the security suppressions in ``<workspace>/.autopilot/`` — it is a fact
about the team's code, readable and diffable, not machine state — and written with a
replace-on-write so a crash mid-save cannot leave half a file.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

FILE_NAME = "flaky-tests.json"


@dataclass(frozen=True)
class FlakyTest:
    repo: str
    signature: str          # failure_signatures() line, e.g. "test tests/x.py::test_y"
    count: int
    first_seen: datetime
    last_seen: datetime


def ledger_path(workspace: str) -> Path | None:
    return Path(workspace) / ".autopilot" / FILE_NAME if workspace else None


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(workspace: str, repo: str, signatures: list[str],
           now: datetime | None = None) -> None:
    """Add one sighting of each signature. Best-effort: never raises."""
    path = ledger_path(workspace)
    if path is None or not signatures:
        return
    stamp = (now or datetime.now(UTC)).isoformat()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _load(path)
        per_repo = data.setdefault(repo or "(repo)", {})
        for sig in signatures:
            entry = per_repo.setdefault(sig, {"count": 0, "first_seen": stamp})
            entry["count"] = int(entry.get("count", 0)) + 1
            entry["last_seen"] = stamp
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".flaky-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)


def _dt(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def load(workspace: str) -> list[FlakyTest]:
    """Every recorded flaky test, most frequent first."""
    path = ledger_path(workspace)
    if path is None:
        return []
    out: list[FlakyTest] = []
    for repo, sigs in _load(path).items():
        if not isinstance(sigs, dict):
            continue
        for sig, entry in sigs.items():
            if not isinstance(entry, dict):
                continue
            out.append(FlakyTest(repo, sig, int(entry.get("count", 0) or 0),
                                 _dt(entry.get("first_seen")), _dt(entry.get("last_seen"))))
    out.sort(key=lambda f: (-f.count, -f.last_seen.timestamp(), f.repo, f.signature))
    return out
