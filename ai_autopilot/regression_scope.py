"""Regression scope: which earlier work — and its test cases — does this change touch?

QC's hardest question on every PR is "what else could this break, and which existing
cases should I rerun?". Running everything is too slow; running only the new item's
cases misses the neighbour it broke. The autopilot already records every file each run
changed (``executions.files_changed``), so the answer is data: earlier work items that
changed the **same files** (strong) or the **same modules** (weaker), and the test cases
ADO links to them (``Tested By``).

Paths are compared exactly as runs record them (usually prefixed with the repo, since
the agent works from the workspace root), so two repos' ``src/app`` never collide.
Pure functions; the poller gathers and posts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from html import escape as esc

from ai_autopilot.code_health import module_of

TESTED_BY = "Microsoft.VSTS.Common.TestedBy-Forward"
FILE_WEIGHT = 3      # one shared file outweighs a couple of shared folders


def _norm(path: str) -> str:
    p = (path or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def _module(path: str) -> str:
    """Three segments deep: with the repo prefix that is the repo's two-level module."""
    parts = path.split("/")
    return module_of("/".join(parts[1:])) if len(parts) > 3 else module_of(path)


def _key(path: str) -> tuple[str, str]:
    """(repo prefix, module) — the repo keeps two repos' ``src/app`` apart."""
    return (path.split("/")[0] if "/" in path else "", _module(path))


@dataclass
class PastWork:
    work_item_id: int
    title: str
    files: set[str]


def history_from_records(records: list) -> list[PastWork]:
    """Execution rows → one entry per work item, files merged across its runs."""
    merged: dict[int, PastWork] = {}
    for r in records:
        try:
            files = json.loads(getattr(r, "files_changed", None) or "[]")
        except (TypeError, ValueError):
            continue
        files = {_norm(f) for f in files if isinstance(f, str) and f.strip()}
        if not files:
            continue
        wid = int(getattr(r, "work_item_id", 0) or 0)
        entry = merged.setdefault(wid, PastWork(wid, getattr(r, "title", "") or "", set()))
        entry.files |= files
    return list(merged.values())


@dataclass
class Related:
    work_item_id: int
    title: str
    shared_files: list[str] = field(default_factory=list)
    shared_modules: list[str] = field(default_factory=list)
    test_cases: list[tuple[int, str]] = field(default_factory=list)

    @property
    def score(self) -> int:
        return FILE_WEIGHT * len(self.shared_files) + len(self.shared_modules)


def related(files: list[str], history: list[PastWork], *, exclude: int = 0,
            limit: int = 10) -> list[Related]:
    changed = {_norm(f) for f in files if f}
    if not changed:
        return []
    changed_keys = {_key(f) for f in changed}
    out: list[Related] = []
    for past in history:
        if past.work_item_id == exclude:
            continue
        same = sorted(changed & past.files)
        # Modules shared beyond the files already shared — not counted twice.
        shared_keys = ({_key(f) for f in past.files} & changed_keys) - {_key(f) for f in same}
        mods = sorted(module for _repo, module in shared_keys)
        if same or mods:
            out.append(Related(past.work_item_id, past.title, same, mods))
    out.sort(key=lambda r: (-r.score, -r.work_item_id))
    return out[:limit]


def render(items: list[Related]) -> str:
    """The PR comment. Cases first — they are what QC acts on."""
    cases = {cid: title for r in items for cid, title in r.test_cases}
    rows = ["<div><b>🧪 Phạm vi regression gợi ý</b> — việc trước đây từng sửa cùng chỗ "
            "(từ lịch sử chạy của autopilot):<br>"]
    if cases:
        rows.append(f"<b>Test case nên chạy lại ({len(cases)}):</b><ul>")
        rows += [f"<li>#{cid} {esc(title)}</li>" for cid, title in list(cases.items())[:15]]
        rows.append("</ul>")
    rows.append("<b>Việc liên quan:</b><ul>")
    for r in items:
        where = (f"cùng file: <code>{esc(r.shared_files[0])}</code>"
                 + (f" +{len(r.shared_files) - 1}" if len(r.shared_files) > 1 else "")
                 if r.shared_files else
                 f"cùng module: <code>{esc(r.shared_modules[0])}</code>")
        tc = f" · {len(r.test_cases)} test case" if r.test_cases else " · chưa có test case"
        rows.append(f"<li>#{r.work_item_id} {esc(r.title)} — {where}{tc}</li>")
    rows.append("</ul></div>")
    return "".join(rows)
