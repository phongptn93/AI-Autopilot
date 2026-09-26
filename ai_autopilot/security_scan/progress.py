"""In-process registry of scans that are running RIGHT NOW, and how far they are.

The Security page used to say "scanning in the background" and nothing else — for the
ten minutes a deep scan takes, that is indistinguishable from a page that forgot. This
keeps, per repo, the stage the runner is in and what each tool has reported so far, so
the page can say "semgrep done (12) · AI review running 3m" and link to the live feed.

Process-local on purpose: it is the runner and the page in the same process; a scan
run by the CLI in another process shows up through the stored ``SecurityScan`` row
(``status: running``) instead.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class ScanProgress:
    repo: str
    trigger: str = ""
    stage: str = "starting"          # starting | scanners | ai | verify | storing | done
    started: float = field(default_factory=time.monotonic)
    tools: dict[str, str] = field(default_factory=dict)   # tool → label so far
    scan_id: int = 0

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def label(self) -> str:
        done = ", ".join(f"{k} {v}" for k, v in self.tools.items())
        head = {"starting": "starting", "scanners": "running scanners",
                "ai": "AI review running", "verify": "building PoCs",
                "storing": "storing results", "done": "done"}.get(self.stage, self.stage)
        return f"{head} · {self.elapsed:.0f}s" + (f" · {done}" if done else "")


_RUNNING: dict[str, ScanProgress] = {}


def start(repo: str, trigger: str = "") -> ScanProgress:
    p = ScanProgress(repo=repo, trigger=trigger)
    _RUNNING[repo] = p
    return p


def get(repo: str) -> ScanProgress | None:
    return _RUNNING.get(repo)


def finish(repo: str) -> None:
    _RUNNING.pop(repo, None)


def running() -> dict[str, ScanProgress]:
    return dict(_RUNNING)
