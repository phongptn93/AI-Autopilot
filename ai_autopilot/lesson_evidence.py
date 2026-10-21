"""Whether a lesson WORKS — the evidence behind each line in the lessons store.

:mod:`ai_autopilot.lessons` remembers what went wrong and puts it in front of the next
run. What it could not say is whether that helped. ``count`` mixes two different facts:
how often the thing happened BEFORE anyone knew, and how often it happened AGAIN while
the lesson was already sitting in the skill or the rule file. Only the second says
anything about the lesson, and it is the one that decides what to do next:

============  ==============================================================  ==========
verdict       what the record shows                                           next step
============  ==============================================================  ==========
unproven      not carried in enough runs yet to say anything                  wait
holding       carried in ``HOLD_AFTER`` runs since it last happened, and the  keep
              mistake has not come back
ignored       a machine-learned line that recurred ``IGNORED_AT`` times while  promote to
              the skill already carried it — the skill is not preventing it   a rule
enforce       a RULE (loaded on every run) that still recurred                 hook / gate
              ``ENFORCE_AT`` times — prose is not preventing it either
============  ==============================================================  ==========

``enforce`` is the rung the ladder was missing. Promoting a line to a rule moves it into
always-on prose, and when always-on prose is ignored the remedy is not more prose: it is
something in the tool loop the model cannot skip — a hook, a test-gate check, a scanner
rule. That reading, and the cue / lesson / evidence shape of the record, come from
Harness² (google-research/harness2), whose library keeps only evidence-supported lessons
and whose editors reach for a plugin exactly when "clear prose is skipped".

Two counters feed it, both cheap and both local:

* :func:`expose` — once per real run, for every lesson the brief carried (the repos'
  skills, plus every rule, since rules are loaded on every run);
* :func:`observe` — once per recorded quality signal, saying whether the lesson already
  existed (``live``) and whether it was a rule (``pinned``) at that moment.

Stored beside the lesson files as ``_evidence.json`` — a sidecar, so the hand-editable
``<repo>.md`` format does not change and a deleted sidecar costs only history. Best
effort everywhere: evidence must never break a run or a recording.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_autopilot import lessons

UNPROVEN, HOLDING, IGNORED, ENFORCE = "unproven", "holding", "ignored", "enforce"
#: Runs that must carry a lesson, without the mistake coming back, before it is
#: credited with preventing it. Low enough to say something within a week of normal
#: traffic, high enough that two quiet runs are not taken as proof.
HOLD_AFTER = 5
#: Recurrences while the skill already carried the line. Matches lessons.RECURRING_AT
#: (count 3 = first occurrence + 2 repeats) so the two pages never disagree.
IGNORED_AT = lessons.RECURRING_AT - 1
#: Recurrences AFTER the line became a rule. Two, not one: a single repeat can be a run
#: that started before the rule landed.
ENFORCE_AT = 2
#: How many occurrences to keep per lesson — enough to point at the PRs, not a log.
_EVIDENCE_KEPT = 5
_FILE = "_evidence.json"
_lock = threading.Lock()


@dataclass(frozen=True)
class Stats:
    """What the record says about one lesson."""

    exposures: int = 0          # runs whose brief carried it
    quiet_runs: int = 0         # of those, since it last happened
    ignored: int = 0            # recurrences while it was already stored
    ignored_as_rule: int = 0    # recurrences while it was a rule
    first_seen: str = ""
    evidence: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Verdict:
    name: str
    reason: str


def _path(workspace: str) -> Path:
    return Path(workspace) / ".autopilot" / "lessons" / _FILE


def _load(workspace: str) -> dict[str, dict[str, dict[str, Any]]]:
    try:
        data = json.loads(_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(workspace: str, data: dict) -> None:
    """Atomic replace: a crash mid-write must leave the old file, never half a file."""
    path = _path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".evidence-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _row(data: dict, repo: str, key: str) -> dict[str, Any]:
    return data.setdefault(repo, {}).setdefault(key, {})


def observe(
    workspace: str, repo: str, text: str, *, live: bool, pinned: bool, kind: str = "",
    work_item_id: int = 0, pr_id: int = 0, now: datetime | None = None,
) -> None:
    """Record one occurrence of ``text`` in ``repo``.

    ``live`` — the lesson already existed when this happened, so the run that repeated
    the mistake had it available. ``pinned`` — it was a rule at that moment.
    """
    key = lessons.normalize(text)
    if not workspace or not repo or not key:
        return
    stamp = (now or datetime.now()).date().isoformat()
    with _lock, contextlib.suppress(Exception):
        data = _load(workspace)
        row = _row(data, repo, key)
        row.setdefault("first_seen", stamp)
        if live:
            row["ignored"] = int(row.get("ignored", 0)) + 1
            if pinned:
                row["ignored_as_rule"] = int(row.get("ignored_as_rule", 0)) + 1
        row["quiet_runs"] = 0
        trail = list(row.get("evidence") or [])
        trail.append({k: v for k, v in (
            ("date", stamp), ("kind", kind), ("work_item_id", work_item_id),
            ("pr_id", pr_id), ("as_rule", pinned and live),
        ) if v})
        row["evidence"] = trail[-_EVIDENCE_KEPT:]
        _save(workspace, data)


def expose(workspace: str, repos: list[str]) -> int:
    """Count one run against every lesson its brief carried. Returns how many.

    Carried = the machine-learned, non-stale lines of ``repos`` and the shared bucket
    (the brief names those repos' skills), plus EVERY rule, because the rule file is
    loaded on every run whatever the work item touches.
    """
    if not workspace:
        return 0
    scoped = set(repos) | {lessons.SHARED_BUCKET}
    carried: list[tuple[str, str]] = []
    for repo in lessons.list_repos(workspace):
        for le in lessons.entries(workspace, repo):
            if le.pinned or (repo in scoped and not le.stale()):
                carried.append((repo, lessons.normalize(le.text)))
    if not carried:
        return 0
    with _lock, contextlib.suppress(Exception):
        data = _load(workspace)
        for repo, key in carried:
            row = _row(data, repo, key)
            row["exposures"] = int(row.get("exposures", 0)) + 1
            row["quiet_runs"] = int(row.get("quiet_runs", 0)) + 1
        _save(workspace, data)
    return len(carried)


def stats(workspace: str, repo: str, text: str) -> Stats:
    """The record for one lesson; an empty Stats when there is none."""
    return _to_stats(_load(workspace).get(repo, {}).get(lessons.normalize(text)) or {})


def all_stats(workspace: str) -> dict[tuple[str, str], Stats]:
    """Every record at once, keyed by (repo, normalized text) — one file read."""
    return {
        (repo, key): _to_stats(row)
        for repo, rows in _load(workspace).items() if isinstance(rows, dict)
        for key, row in rows.items() if isinstance(row, dict)
    }


def _to_stats(row: dict[str, Any]) -> Stats:
    def num(name: str) -> int:
        try:
            return max(0, int(row.get(name, 0)))
        except (TypeError, ValueError):
            return 0

    trail = row.get("evidence") or []
    return Stats(
        exposures=num("exposures"), quiet_runs=num("quiet_runs"), ignored=num("ignored"),
        ignored_as_rule=num("ignored_as_rule"), first_seen=str(row.get("first_seen") or ""),
        evidence=tuple(e for e in trail if isinstance(e, dict)),
    )


def verdict(lesson: lessons.Lesson, st: Stats) -> Verdict:
    """What the record says to do with ``lesson``. Pure.

    Lessons learned before evidence was kept have no record of being ignored; for
    those, a machine-learned line that is already ``recurring`` by count is read as
    ignored, so the promote advice does not disappear on upgrade.
    """
    if lesson.pinned and st.ignored_as_rule >= ENFORCE_AT:
        return Verdict(ENFORCE, (
            f"Đã là quy tắc (nạp ở MỌI run) mà vẫn tái diễn {st.ignored_as_rule} lần — "
            "viết thêm chữ sẽ không ăn. Chuyển thành kiểm tra máy: hook, test gate "
            "hoặc rule của security scan."))
    if not lesson.pinned and (st.ignored >= IGNORED_AT or lesson.recurring):
        n = st.ignored or max(0, lesson.count - 1)
        return Verdict(IGNORED, (
            f"Đã nằm trong skill mà vẫn tái diễn {n} lần — skill không ngăn được. "
            "Nâng thành quy tắc để mọi run đều nạp."))
    if st.quiet_runs >= HOLD_AFTER:
        return Verdict(HOLDING, (
            f"Đã đi cùng {st.quiet_runs} run kể từ lần gặp cuối mà lỗi không quay lại."))
    return Verdict(UNPROVEN, (
        f"Mới đi cùng {st.quiet_runs}/{HOLD_AFTER} run kể từ lần gặp cuối — chưa đủ để "
        "kết luận."))


def enforcement_prompt(repo: str, lesson: lessons.Lesson, st: Stats) -> str:
    """A ready-to-paste task for turning an ignored rule into a mechanical check.

    Drafted for a person to hand to an agent, never applied by the autopilot: a hook
    runs on every tool call of every run, so writing one is a change somebody reviews.
    """
    where = "mọi repo" if repo == lessons.SHARED_BUCKET else f"repo `{repo}`"
    refs = ", ".join(
        f"#{e['work_item_id']}" + (f" (PR !{e['pr_id']})" if e.get("pr_id") else "")
        for e in st.evidence if e.get("work_item_id")
    ) or "không có tham chiếu"
    return (
        f"Quy tắc sau đã nằm trong .claude/rules (nạp ở mọi run) nhưng vẫn bị vi phạm "
        f"{st.ignored_as_rule} lần trên {where}:\n\n«{lesson.text}»\n\n"
        f"Các lần gần nhất: {refs}.\n\n"
        "Đừng viết thêm hướng dẫn. Hãy biến nó thành một kiểm tra MÁY mà run không thể "
        "bỏ qua, chọn cách rẻ nhất phát hiện được vi phạm:\n"
        "1. Hook Claude Code trong .claude/settings.json — PreToolUse chặn lệnh/sửa file "
        "sai kèm lý do dạy cách đúng, hoặc PostToolUse kiểm ngay sau khi ghi file;\n"
        "2. Một test hoặc lệnh trong test gate của repo, fail khi vi phạm;\n"
        "3. Một rule tuỳ biến cho `ai-autopilot scan` nếu đây là mẫu mã nguồn.\n"
        "Kiểm tra phải KHÔNG chặn việc hợp lệ. Viết kèm test chứng minh nó bắt đúng vi "
        "phạm cũ và để yên code đúng."
    )


@dataclass(frozen=True)
class Assessment:
    """One lesson as the Learning page and the inbox show it."""

    repo: str
    lesson: lessons.Lesson
    stats: Stats
    verdict: Verdict
    prompt: str = ""            # only for ENFORCE: the task to hand to an agent


def assess(workspace: str) -> dict[tuple[str, str], Assessment]:
    """Every stored lesson with its verdict, keyed by (repo, normalized text).

    One read of the sidecar and of each lesson file — the page and the inbox call this
    once per render, not once per row.
    """
    if not workspace:
        return {}
    records = all_stats(workspace)
    out: dict[tuple[str, str], Assessment] = {}
    for repo in lessons.list_repos(workspace):
        for le in lessons.entries(workspace, repo):
            key = (repo, lessons.normalize(le.text))
            st = records.get(key, Stats())
            v = verdict(le, st)
            out[key] = Assessment(
                repo=repo, lesson=le, stats=st, verdict=v,
                prompt=enforcement_prompt(repo, le, st) if v.name == ENFORCE else "",
            )
    return out


def health(assessed: dict[tuple[str, str], Assessment]) -> dict[str, int]:
    """How many lessons sit on each rung."""
    counts = {UNPROVEN: 0, HOLDING: 0, IGNORED: 0, ENFORCE: 0}
    for a in assessed.values():
        counts[a.verdict.name] += 1
    return counts


def rename(workspace: str, repo: str, old: str, new: str) -> None:
    """Keep a lesson's record when its wording is edited."""
    src, dst = lessons.normalize(old), lessons.normalize(new)
    if not workspace or not src or not dst or src == dst:
        return
    with _lock, contextlib.suppress(Exception):
        data = _load(workspace)
        rows = data.get(repo) or {}
        if src in rows:
            rows[dst] = rows.pop(src)
            _save(workspace, data)


def forget(workspace: str, repo: str, text: str = "") -> None:
    """Drop one lesson's record, or a whole repo's when ``text`` is empty."""
    if not workspace or not repo:
        return
    with _lock, contextlib.suppress(Exception):
        data = _load(workspace)
        if not text:
            changed = data.pop(repo, None) is not None
        else:
            changed = (data.get(repo) or {}).pop(lessons.normalize(text), None) is not None
        if changed:
            _save(workspace, data)
