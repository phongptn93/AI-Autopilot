"""The story of one work item — every recorded thing that happened to it, in order.

The task room already had the pieces, each in its own tab: runs in one list, state
changes in another, quality events on a different page, spec drift on a fourth, the
audit log on a fifth. Each is correct and none of them answers the question a reviewer
actually asks when they open the item: *what happened, in what order, and why?* That
answer is a merge by time, and a merge by time is exactly what nobody does by hand.

Pure on purpose (rows in → events out), like ``analytics.compute_analytics``: the
route gathers, this module only orders and words. That keeps it testable without a
database and keeps the wording in one place — the page and any future digest read the
same sentence for the same fact.

Every input is loosely typed (``getattr`` with defaults) because rows come from several
tables written across many releases; a column a row predates must cost one detail, not
the whole story.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ai_autopilot.services.pr_feedback import parse_pr_url

#: QualityEvent kind for a human's 1-tap verdict on a run (value +1 / -1).
HUMAN_FEEDBACK = "human_feedback"
#: How a feedback row names the run it is about — kept in ``QualityEvent.stage``, the
#: one free short column, so no schema change is needed (see dashboard.routes.feedback).
FEEDBACK_STAGE_PREFIX = "exec:"

# Fleet command kind for "run this item" (mirrors ai_autopilot.fleet.CMD_RUN_ITEM;
# repeated rather than imported so this module stays free of the fleet machinery).
_CMD_RUN_ITEM = "run_item"


@dataclass
class TimelineEvent:
    """One line of the story."""

    at: datetime
    kind: str                 # run_start | run_end | quality | feedback | drift | ...
    icon: str
    title: str
    detail: str = ""
    tone: str = ""            # ok | warn | bad | "" — drives the dot colour only
    links: list[tuple[str, str]] = field(default_factory=list)   # (label, url)


@dataclass
class Deviation:
    """A place the agent decided something the work item did not say."""

    where: str = ""
    summary: str = ""
    spec_says: str = ""
    code_does: str = ""
    decided: str = ""


@dataclass
class Rationale:
    """The "why did it decide that" panel: the agent's own words, plus the evidence."""

    run_id: int = 0
    run_status: str = ""
    summary: str = ""          # the run's output excerpt (its own account of the work)
    error: str = ""
    truncated: bool = False
    deviations: list[Deviation] = field(default_factory=list)
    score: int | None = None   # recomputed from the stored run — see :func:`rationale`
    grade: str = ""
    gate: str = ""
    score_reasons: list[str] = field(default_factory=list)
    vote: int = 0              # latest human feedback on that run: +1 / -1 / 0 = none
    vote_reason: str = ""

    @property
    def empty(self) -> bool:
        return not (self.summary or self.error or self.deviations or self.score is not None)


def _naive_utc(at: datetime | None) -> datetime | None:
    """Compare everything as naive UTC. The tables disagree: some columns are written
    with ``datetime.now(UTC)`` and come back aware on one backend and naive on another,
    others are written naive on purpose. Sorting a mix raises."""
    if at is None:
        return None
    if at.tzinfo is not None:
        return at.astimezone(UTC).replace(tzinfo=None)
    return at


def _status(run) -> str:
    st = getattr(run, "status", "")
    return str(getattr(st, "value", st) or "")


def _pr_urls(run) -> list[str]:
    urls = [run.pr_url] if getattr(run, "pr_url", None) else []
    with contextlib.suppress(ValueError, TypeError):
        urls += [u for u in json.loads(getattr(run, "pr_urls", None) or "[]")
                 if isinstance(u, str)]
    return [u for u in dict.fromkeys(urls) if u]


def _pr_label(url: str) -> str:
    parsed = parse_pr_url(url)
    return f"PR {parsed[0]} !{parsed[1]}" if parsed else "PR"


def _files(run) -> list[str]:
    try:
        raw = json.loads(getattr(run, "files_changed", None) or "[]")
    except (ValueError, TypeError):
        return []
    return [f for f in raw if isinstance(f, str)] if isinstance(raw, list) else []


def _mmss(seconds: float) -> str:
    total = max(0, int(seconds or 0))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def _run_detail(run) -> str:
    """The run's numbers as one muted line. Unknowns are left out, never shown as 0 —
    a NULL cost means "not recorded", and "$0.00" would be a claim it was free."""
    bits: list[str] = []
    role = getattr(run, "profile", None) or ""
    skill = getattr(run, "skill_used", "") or ""
    if role:
        bits.append(f"vai {role}")
    if skill:
        bits.append(skill if len(skill) <= 40 else skill[:37] + "…")
    if getattr(run, "model_used", None):
        bits.append(str(run.model_used))
    if getattr(run, "duration_seconds", 0):
        bits.append(_mmss(run.duration_seconds))
    if getattr(run, "cost_tokens", 0):
        bits.append(f"{run.cost_tokens:,} token")
    if getattr(run, "cost_usd", None) is not None:
        bits.append(f"${run.cost_usd:.2f}")
    files = _files(run)
    if files:
        bits.append(f"{len(files)} file")
    total = getattr(run, "tests_total", 0) or 0
    if total:
        failed = getattr(run, "tests_failed", 0) or 0
        bits.append(f"test {total - failed}/{total} đạt")
    return " · ".join(bits)


def _run_events(run, history_url: str) -> list[TimelineEvent]:
    out: list[TimelineEvent] = []
    started = _naive_utc(getattr(run, "started_at", None))
    rid = getattr(run, "id", 0) or 0
    hist = [("Lịch sử", history_url)] if history_url else []
    if started is not None:
        role = getattr(run, "profile", None) or ""
        out.append(TimelineEvent(
            at=started, kind="run_start", icon="▶️",
            title=f"Bắt đầu run #{rid}" + (f" — vai {role}" if role else ""),
            detail=getattr(run, "skill_used", "") or "", links=list(hist),
        ))
    done = _naive_utc(getattr(run, "completed_at", None))
    status = _status(run)
    if done is None:
        return out
    prs = _pr_urls(run)
    links = [(_pr_label(u), u) for u in prs] + hist
    if status == "Success":
        title = f"Run #{rid} hoàn tất" + (f", mở {len(prs)} PR" if prs else "")
        icon, tone = "✅", "ok"
    else:
        title = f"Run #{rid} chưa hoàn tất"
        icon, tone = "❌", "bad"
    detail = _run_detail(run)
    err = (getattr(run, "error", None) or "").strip()
    if err and status != "Success":
        detail = (detail + " · " if detail else "") + _clip(err, 220)
    out.append(TimelineEvent(at=done, kind="run_end", icon=icon, title=title,
                             detail=detail, tone=tone, links=links))
    return out


def _quality_event(ev, pr_urls: dict[int, str]) -> TimelineEvent | None:
    at = _naive_utc(getattr(ev, "at", None))
    if at is None:
        return None
    kind = getattr(ev, "kind", "") or ""
    value = int(getattr(ev, "value", 0) or 0)
    actor = getattr(ev, "actor", "") or ""
    detail = _clip(getattr(ev, "detail", "") or "", 240)
    pr_id = int(getattr(ev, "pr_id", 0) or 0)
    links = [(f"PR !{pr_id}", pr_urls[pr_id])] if pr_id and pr_id in pr_urls else []
    if kind == HUMAN_FEEDBACK:
        good = value > 0
        return TimelineEvent(
            at=at, kind="feedback", icon="👍" if good else "👎",
            title="Người review đánh giá: " + ("tốt" if good else "chưa tốt"),
            detail=detail, tone="ok" if good else "bad", links=links,
        )
    words = {
        "execution_retry": ("🔁", f"Thử lại lần {value}", "warn"),
        "pr_revision": ("✏️", f"Sửa PR theo yêu cầu review (vòng {value})", "warn"),
        "sdlc_iteration": ("🔄", f"Quay lại vòng SDLC {value}"
                           + (f" — {ev.stage}" if getattr(ev, "stage", "") else ""), "warn"),
        "review_finding": ("🔍", f"Auto-review tìm thấy {max(1, value)} vấn đề", "warn"),
        "test_failed": ("🧪", "Test fail", "bad"),
        "reopened": ("↩️", "Item bị kéo lại trạng thái chạy", "warn"),
    }
    if kind == "review_vote":
        tone = "ok" if value > 0 else ("bad" if value < 0 else "")
        icon = "👍" if value > 0 else ("👎" if value < 0 else "🗳️")
        return TimelineEvent(at=at, kind="quality", icon=icon,
                             title=f"{actor or 'Reviewer'} vote {value:+d} trên PR",
                             detail=detail, tone=tone, links=links)
    icon, title, tone = words.get(kind, ("📌", kind or "sự kiện chất lượng", ""))
    if actor and actor != "autopilot" and kind != "review_vote":
        detail = (f"{actor} · " if detail else actor) + detail
    return TimelineEvent(at=at, kind="quality", icon=icon, title=title,
                         detail=detail, tone=tone, links=links)


def _drift_events(row) -> list[TimelineEvent]:
    out: list[TimelineEvent] = []
    where = getattr(row, "where", "") or ""
    summary = getattr(row, "summary", "") or ""
    created = _naive_utc(getattr(row, "created_at", None))
    links = [("PR", row.pr_url)] if getattr(row, "pr_url", "") else []
    if created is not None:
        out.append(TimelineEvent(
            at=created, kind="drift", icon="📐",
            title="Agent ghi nhận lệch spec" + (f" — {where}" if where else ""),
            detail=_clip(summary, 240), tone="warn", links=links,
        ))
    resolved = _naive_utc(getattr(row, "resolved_at", None))
    if resolved is not None:
        decision = getattr(row, "decision", "") or ""
        note = getattr(row, "decision_note", "") or ""
        by = getattr(row, "resolved_by", "") or ""
        out.append(TimelineEvent(
            at=resolved, kind="drift_decided", icon="✅",
            title="Đã chốt lệch spec" + (f" ({decision})" if decision else "")
            + (f" — {where}" if where else ""),
            detail=" · ".join(x for x in (by, _clip(note, 200)) if x), tone="ok",
        ))
    return out


def mentions_item(target: str, item_id: int) -> bool:
    """Does an audit ``target`` name this work item?

    Targets are free text written by many call sites: ``"4021"``, ``"#4021"``,
    ``"#4021 #4022"``, ``"repo!77"``. A bare substring match would credit item 402 with
    everything about 4021, and a bare number elsewhere is usually a PR id — so only the
    whole-field id or a ``#id`` token counts.
    """
    t = (target or "").strip()
    if not t or not item_id:
        return False
    return t == str(item_id) or re.search(rf"#{item_id}(?!\d)", t) is not None


def _audit_event(ev) -> TimelineEvent | None:
    at = _naive_utc(getattr(ev, "at", None))
    if at is None:
        return None
    action = getattr(ev, "action", "") or ""
    actor = getattr(ev, "actor", "") or ""
    source = getattr(ev, "source", "") or ""
    who = actor + (f" ({source})" if source and source != actor else "")
    return TimelineEvent(
        at=at, kind="audit", icon="🔎", title=f"{action}" + (f" — {who}" if who else ""),
        detail=_clip(getattr(ev, "detail", "") or "", 240),
    )


def _conflict_events(row) -> list[TimelineEvent]:
    out: list[TimelineEvent] = []
    pr = f"PR !{getattr(row, 'pr_id', 0)}"
    url = getattr(row, "url", "") or ""
    links = [(pr, url)] if url else []
    first = _naive_utc(getattr(row, "first_seen", None))
    if first is not None:
        attempts = int(getattr(row, "attempts", 0) or 0)
        out.append(TimelineEvent(
            at=first, kind="conflict", icon="⚔️", title=f"{pr} bị conflict với nhánh đích",
            detail=(f"agent đã thử gỡ {attempts} lần" if attempts else "")
            + (" · " + _clip(row.last_error, 160) if getattr(row, "last_error", "") else ""),
            tone="warn", links=links,
        ))
    resolved = _naive_utc(getattr(row, "resolved_at", None))
    if resolved is not None:
        by = getattr(row, "resolved_by", "") or ""
        out.append(TimelineEvent(
            at=resolved, kind="conflict", icon="🩹", title=f"{pr} hết conflict",
            detail={"agent": "agent tự gỡ", "clean": "tự sạch sau khi đích đổi",
                    "external": "người khác gỡ"}.get(by, by),
            tone="ok", links=links,
        ))
    return out


def _command_targets(cmd, item_id: int) -> bool:
    if (getattr(cmd, "kind", "") or "") != _CMD_RUN_ITEM:
        return False
    args = getattr(cmd, "args", "") or "{}"
    try:
        data = json.loads(args) if isinstance(args, str) else dict(args)
    except (ValueError, TypeError):
        return False
    try:
        return int(data.get("id") or 0) == item_id
    except (TypeError, ValueError):
        return False


def _command_event(cmd) -> TimelineEvent | None:
    at = _naive_utc(getattr(cmd, "created_at", None))
    if at is None:
        return None
    status = getattr(cmd, "status", "") or ""
    tone = {"done": "ok", "failed": "bad", "expired": "warn", "cancelled": ""}.get(status, "")
    by = getattr(cmd, "created_by", "") or ""
    return TimelineEvent(
        at=at, kind="dispatch", icon="📡",
        title=f"Giao cho máy {getattr(cmd, 'worker', '?')} chạy"
        + (f" ({status})" if status else ""),
        detail=" · ".join(x for x in (by, _clip(getattr(cmd, "detail", "") or "", 200)) if x),
        tone=tone,
    )


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def build_timeline(
    item_id: int,
    *,
    executions: Iterable = (),
    quality: Iterable = (),
    drifts: Iterable = (),
    audit: Iterable = (),
    conflicts: Iterable = (),
    commands: Iterable = (),
    history_url: str = "",
) -> list[TimelineEvent]:
    """Every source merged into one list, oldest first.

    Inputs may be broader than the item (a recent-conflicts page, every fleet command);
    each is filtered here so the caller can hand over what it cheaply has. Ties keep a
    stable, meaningful order: a run's start sorts before its own end even when both
    were stamped in the same second.
    """
    runs = [r for r in executions if getattr(r, "work_item_id", item_id) == item_id]
    pr_by_id: dict[int, str] = {}
    for run in runs:
        for url in _pr_urls(run):
            parsed = parse_pr_url(url)
            if parsed:
                pr_by_id.setdefault(parsed[1], url)

    events: list[TimelineEvent] = []
    for run in runs:
        events += _run_events(run, history_url)
    for ev in quality:
        if getattr(ev, "work_item_id", item_id) != item_id:
            continue
        te = _quality_event(ev, pr_by_id)
        if te is not None:
            events.append(te)
    for row in drifts:
        if getattr(row, "work_item_id", item_id) == item_id:
            events += _drift_events(row)
    for ev in audit:
        if mentions_item(getattr(ev, "target", ""), item_id):
            te = _audit_event(ev)
            if te is not None:
                events.append(te)
    for row in conflicts:
        if (getattr(row, "work_item_id", 0) == item_id
                or int(getattr(row, "pr_id", 0) or 0) in pr_by_id):
            events += _conflict_events(row)
    for cmd in commands:
        if _command_targets(cmd, item_id):
            te = _command_event(cmd)
            if te is not None:
                events.append(te)

    order = {"dispatch": 0, "run_start": 1, "run_end": 3}
    events.sort(key=lambda e: (e.at, order.get(e.kind, 2)))
    return events


def feedback_execution_id(ev) -> int:
    """The run a feedback row is about (0 when the row does not say)."""
    stage = getattr(ev, "stage", "") or ""
    if not stage.startswith(FEEDBACK_STAGE_PREFIX):
        return 0
    try:
        return int(stage[len(FEEDBACK_STAGE_PREFIX):])
    except ValueError:
        return 0


def rationale(
    executions: Iterable,
    drifts: Iterable = (),
    quality: Iterable = (),
    *,
    auto_min: int = 85,
    review_min: int = 60,
    excerpt_chars: int = 1200,
) -> Rationale:
    """Why the latest finished run did what it did, from what was stored about it.

    The score is RECOMPUTED with ``pr_scorer.score_run`` from the run's stored columns —
    the live gate's score is posted to ADO as a badge and never persisted. The stored
    row lacks two of the live inputs (auto-review verdict, CI result), which the scorer
    treats as "unknown", so this can read a little lower than the badge did. It is
    labelled as recomputed on the page rather than passed off as the original.
    """
    from ai_autopilot.execution.pr_scorer import ScoreInput, score_run

    finished = [r for r in executions if getattr(r, "completed_at", None) is not None]
    out = Rationale()
    for row in drifts:
        decided = getattr(row, "decision", "") or (
            "đã cập nhật" if getattr(row, "resolved_at", None) else "")
        out.deviations.append(Deviation(
            where=getattr(row, "where", "") or "",
            summary=getattr(row, "summary", "") or "",
            spec_says=getattr(row, "spec_says", "") or "",
            code_does=getattr(row, "code_does", "") or "",
            decided=decided,
        ))
    if not finished:
        return out
    run = max(finished, key=lambda r: _naive_utc(r.completed_at))
    out.run_id = int(getattr(run, "id", 0) or 0)
    out.run_status = _status(run)
    text = (getattr(run, "output", None) or "").strip()
    out.truncated = len(text) > excerpt_chars
    out.summary = text[:excerpt_chars]
    out.error = (getattr(run, "error", None) or "").strip()
    success = out.run_status == "Success"
    if success:
        score = score_run(
            ScoreInput(
                completed=True, has_pr=bool(_pr_urls(run)), files_changed=len(_files(run)),
                had_error=bool(out.error),
                # A role that opened no PR and changed nothing was most likely a
                # non-coding role (QC/BA); the live gate knows the role's stages, the
                # stored row does not — so do not grade it on the dev question.
                expected_pr=bool(_pr_urls(run) or _files(run)),
                tests_failed=int(getattr(run, "tests_failed", 0) or 0),
                tests_blocked=int(getattr(run, "tests_blocked", 0) or 0),
            ),
            auto_min=auto_min, review_min=review_min,
        )
        out.score, out.grade, out.gate = score.score, score.grade, score.gate
        out.score_reasons = list(score.reasons)
    votes = [q for q in quality
             if getattr(q, "kind", "") == HUMAN_FEEDBACK and feedback_execution_id(q) == out.run_id]
    if votes:
        latest = max(votes, key=lambda q: _naive_utc(q.at))
        out.vote = 1 if (latest.value or 0) > 0 else -1
        out.vote_reason = latest.detail or ""
    return out
