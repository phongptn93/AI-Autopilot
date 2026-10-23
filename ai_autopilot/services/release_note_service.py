"""Builds the release note from local history and posts it to Teams on a schedule.

Gathering lives here because the dashboard page and the scheduled post must read the
same note — two gatherers would let the chat message and the page disagree about the
same week. The rules themselves are in :mod:`ai_autopilot.release_notes`.

The schedule borrows the bot digest's contract (``teams_agent._digest_loop``): a fixed
time of day in the team's ``timezone``, optional weekdays, and a post that falls inside
quiet hours is DROPPED rather than held — a release note is a snapshot, and delivering
Monday's at Tuesday 08:00 would be worse than the next one on time.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ai_autopilot import release_notes
from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.models import WorkItemInfo
from ai_autopilot.notifications.base import NotificationMessage, NotificationType
from ai_autopilot.scheduling import QuietHours, parse_days, resolve_tz, seconds_until

_log = get_logger("services.release_note")
MAX_WINDOW_DAYS = 90


def page_url(config, days: int) -> str:
    """The dashboard page for this note, or "" when there is no public URL to link."""
    base = (getattr(config, "dashboard_public_url", "") or "").rstrip("/")
    return f"{base}/dashboard/release-notes?days={days}" if base else ""


@dataclass(frozen=True)
class Schedule:
    """One scheduled post. ``daily`` notes cover the time since the previous send day;
    the weekly one covers ``release_note_window_days``."""

    name: str        # "weekly" | "daily" — for the log and the task name
    at: str          # HH:MM in ``timezone``
    days: str        # "Mon,Tue" — blank = every day
    daily: bool = False


def schedules(config) -> list[Schedule]:
    """The posts this machine is configured to send, weekly first."""
    out: list[Schedule] = []
    if config.release_note_enabled:
        out.append(Schedule("weekly", config.release_note_at, config.release_note_days))
    if config.release_note_daily_enabled:
        out.append(Schedule("daily", config.release_note_daily_at,
                            config.release_note_daily_days, daily=True))
    return out


def is_send_day(days: str, now: datetime) -> bool:
    """Is ``now`` (local) one of ``days``? Blank = every day."""
    return not (days or "").strip() or now.weekday() in parse_days(days)


def due_today(config, now: datetime) -> bool:
    """The weekly schedule's send-day test (kept for callers of the single schedule)."""
    return is_send_day(config.release_note_days, now)


def since_previous_send_day(days: str, now: datetime) -> int:
    """Days back to the previous send day — the morning brief's window.

    Monday's brief on a Mon–Fri schedule covers Friday 08:00 → Monday 08:00, so what
    shipped over the weekend is reported once instead of never. Blank days = 1.
    """
    allowed = parse_days(days) if (days or "").strip() else set(range(7))
    for back in range(1, 8):
        if (now - timedelta(days=back)).weekday() in allowed:
            return back
    return 1


def morning_title(note) -> str:
    """The morning brief's heading: today's date, and the span when it is not one day."""
    today = note.end.strftime("%d/%m")
    span = "" if note.days <= 1 else f" (gồm {note.days} ngày từ {note.start.strftime('%d/%m')})"
    return f"☀️ Bản tin sáng {today} — {release_notes.counts_text(note)}{span}"


class ReleaseNoteService:
    """The page's builder and, when enabled, the scheduled Teams post."""

    def __init__(self, c) -> None:
        self._c = c
        self._config = c.config
        self._tasks: list[asyncio.Task] = []

    # ── building ────────────────────────────────────────────────────────────

    async def build(
        self, *, days: int = 0, projects: list[str] | None = None, now: datetime | None = None,
    ) -> release_notes.ReleaseNote:
        """The note for the last ``days`` (default ``release_note_window_days``).

        ``projects``: None = every project; [] = nothing (a workspace with no projects in
        scope must show nothing, not everything). Best-effort: a failure comes back as an
        empty note with ``error`` set, so the page renders and the post is skipped.
        """
        cfg = self._config
        days = max(1, min(int(days or cfg.release_note_window_days or 7), MAX_WINDOW_DAYS))
        end = now or datetime.now(UTC)
        start = end - timedelta(days=days)
        note = release_notes.ReleaseNote(start=start, end=end, days=days)
        history = getattr(self._c, "state_history", None)
        if history is None or projects == []:
            return note
        built = await self._released(note, history, projects)
        await self._add_snapshots(built, history, projects)
        await self._add_wip(built, projects)
        return built

    async def _released(self, note, history, projects) -> release_notes.ReleaseNote:
        cfg = self._config
        start, end, days = note.start, note.end, note.days
        try:
            # Twice the window, as the Delivery page does: an item already released
            # just before the window must be SEEN to be excluded.
            changes = await history.changes_since(start - timedelta(days=days))
            note.history_since = await history.first_seen()
        except Exception as exc:  # noqa: BLE001
            _log.warning("release note: history read failed", error=describe_exc(exc))
            note.error = "Không đọc được lịch sử trạng thái."
            return note
        if projects is not None:
            wanted = {p.lower() for p in projects}
            changes = [ch for ch in changes if (ch.project or "").lower() in wanted]
        states = list(cfg.release_note_states or [])
        released = release_notes.released_between(changes, start, end, states)
        # The window before, for "+3 so với kỳ trước" — only when history covers it.
        if note.history_since is not None and \
                release_notes._utc(note.history_since) <= start - timedelta(days=days):
            note.previous_total = len(release_notes.released_between(
                changes, start - timedelta(days=days), start, states))
        if not released:
            return self._finish(note, [], released)
        try:
            items = await self._c.ado.get_work_items_by_ids(sorted(released))
        except Exception as exc:  # noqa: BLE001
            _log.warning("release note: work item read failed", error=describe_exc(exc))
            items = []
            note.error = "Không đọc được chi tiết work item từ ADO — hiện theo lịch sử đã ghi."
        # An item ADO no longer returns (deleted, no access) still shipped: fall back to
        # the title the tracker recorded rather than silently dropping it from the note.
        got = {i.id for i in items}
        titles = {ch.work_item_id: (ch.title, ch.project) for ch in changes}
        for wid in released:
            if wid not in got:
                title, project = titles.get(wid, ("", ""))
                items.append(WorkItemInfo(id=wid, title=title or f"#{wid}", project=project))
        return self._finish(note, items, released)

    def _finish(self, note, items, released) -> release_notes.ReleaseNote:
        built = release_notes.build(
            items, released, tenant_tags=list(self._config.release_note_tenant_tags or []),
            start=note.start, end=note.end, days=note.days, history_since=note.history_since,
        )
        built.error = note.error
        built.previous_total = note.previous_total
        return built

    async def _add_snapshots(self, note, history, projects) -> None:
        """Fill ``note.waiting`` and ``note.lanes``: what sits in a waiting or a role's
        state NOW — one query for all of them. Best-effort: a failed read leaves them
        empty and says so in ``note.error``."""
        cfg = self._config
        states = [s.strip() for s in (cfg.release_note_waiting_states or []) if s.strip()]
        roles = release_notes.parse_roles(cfg.release_note_roles)
        note.waiting_states = states
        note.stale_days = max(1, int(cfg.release_note_stale_days or 7))
        wanted = list(dict.fromkeys(states + [s for _, ss in roles for s in ss]))
        if not wanted:
            return
        try:
            items = await self._c.ado.get_work_items_in_states(wanted)
        except Exception as exc:  # noqa: BLE001
            _log.warning("release note: snapshot read failed", error=describe_exc(exc))
            note.error = " ".join(filter(None, [
                note.error, "Không đọc được danh sách chờ deploy / theo vai trò từ ADO."]))
            note.lanes = release_notes.build_lanes(roles, [])
            return
        if projects is not None:
            wanted = {p.lower() for p in projects}
            items = [i for i in items if (i.project or "").lower() in wanted]
        entered: dict[int, datetime] = {}
        with contextlib.suppress(Exception):
            latest = await history.latest_entries([i.id for i in items])
            current = {i.id: (i.state or "").strip().lower() for i in items}
            # Only when the history agrees on the state: a row for an older state would
            # date the wait from the wrong transition.
            entered = {wid: at for wid, (state, at) in latest.items()
                       if (state or "").strip().lower() == current.get(wid)}
        queue = release_notes.build_waiting(
            items, entered, tenant_tags=list(cfg.release_note_tenant_tags or []))
        waiting = {s.lower() for s in states}
        note.waiting = [w for w in queue if w.state.lower() in waiting]
        note.lanes = release_notes.build_lanes(roles, queue)

    async def _add_wip(self, note, projects) -> None:
        """Fill ``note.wip`` from each project's board. Best-effort, per project."""
        cfg = self._config
        if not cfg.release_note_wip_enabled:
            return
        for project in (projects if projects is not None else cfg.effective_ado_projects):
            try:
                columns = await self._c.ado.get_board_wip(project, cfg.release_note_wip_board)
            except Exception as exc:  # noqa: BLE001
                _log.warning("release note: WIP read failed", project=project,
                             error=describe_exc(exc))
                continue
            note.wip += [release_notes.WipColumn(project, name, n, limit)
                         for name, n, limit in columns]

    async def analyze(self, note: release_notes.ReleaseNote, *, kind: str = "") -> bool:
        """Ask Claude for the assessment block. True when ``note.analysis`` was set.

        Tool-less and one turn: it reads the facts handed to it and can touch nothing.
        Anything short of a well-formed, grounded answer leaves the note as it was."""
        from ai_autopilot.execution.claude_client import run_claude

        cfg = self._config
        if not cfg.release_note_ai_enabled or not note.has_news:
            return False
        prompt = release_notes.ANALYSIS_PROMPT.format(
            kind=kind or f"{note.days} ngày", facts=release_notes.analysis_facts(note))
        try:
            run = await run_claude(
                prompt, tempfile.gettempdir(), timeout_seconds=90,
                model=cfg.claude_model or None, max_turns=1, allowed_tools=[],
                effort=cfg.claude_effort_chat,
            )
        except Exception as exc:  # noqa: BLE001 — the brief goes out without it
            _log.warning("release note: analysis failed", error=describe_exc(exc))
            return False
        note.analysis = release_notes.parse_analysis(run.text or "",
                                                     release_notes.known_ids(note))
        if note.analysis is None:
            _log.warning("release note: analysis unusable", text=(run.text or "")[:200])
        return note.analysis is not None

    # ── posting ─────────────────────────────────────────────────────────────

    async def post(self, note: release_notes.ReleaseNote, *, title: str = "") -> bool:
        """Post ``note`` to every channel THROUGH the notifier — so the alert policy and
        the quiet window apply, as for every other sender. True when posted."""
        from ai_autopilot.dashboard.common import work_item_link_base

        if self._config.dry_run:
            _log.info("[DRY-RUN] would post release note", items=note.total)
            return False
        link_base = work_item_link_base(self._config)
        heading, text = release_notes.render_teams(note, link_base=link_base, title=title)
        url = page_url(self._config, note.days)
        await self._c.notifier.notify(NotificationMessage(
            work_item=WorkItemInfo(id=0), type=NotificationType.INFO,
            heading=heading, text=text,
            card=release_notes.render_card(note, link_base=link_base),
            actions=[("📣 Mở release note đầy đủ", url)] if url else [],
        ))
        _log.info("release note posted", items=note.total, days=note.days)
        return True

    # ── schedule ────────────────────────────────────────────────────────────

    def start(self) -> None:
        planned = schedules(self._config)
        if not planned:
            return
        if not self._config.delivery_history_enabled:
            _log.warning("release note schedule off — delivery_history_enabled is false, "
                         "so there is no state history to build it from")
            return
        for schedule in planned:
            self._tasks.append(asyncio.create_task(
                self._run(schedule), name=f"release-note-{schedule.name}"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    async def _run(self, schedule: Schedule) -> None:
        tz = resolve_tz(self._config.timezone)

        def _now() -> datetime:
            return datetime.now(tz) if tz else datetime.now()  # noqa: DTZ005 — host fallback

        _log.info("release note schedule started", schedule=schedule.name, at=schedule.at,
                  days=schedule.days or "every day")
        while True:
            try:
                delay = seconds_until((schedule.at or "").strip(), _now())
                if delay is None:
                    # A constant that does not parse will not parse next time either.
                    _log.error("release note schedule off — time is not HH:MM",
                               schedule=schedule.name, value=schedule.at)
                    return
                await asyncio.sleep(delay)
                await self.run_once(_now(), schedule)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad cycle must not kill the loop
                _log.error("release note cycle failed", schedule=schedule.name,
                           error=describe_exc(exc))

    async def run_once(self, now: datetime, schedule: Schedule | None = None) -> str:
        """One scheduled firing. Returns what happened, for the log and the tests.

        ``schedule`` defaults to the weekly one. ``now`` is local time (``timezone``);
        the note itself is cut on the same instant.
        """
        cfg = self._config
        schedule = schedule or Schedule("weekly", cfg.release_note_at, cfg.release_note_days)
        if not is_send_day(schedule.days, now):
            return "not-today"
        if cfg.digest_respect_quiet_hours and QuietHours(cfg).is_quiet():
            _log.info("release note skipped — outside notify hours", schedule=schedule.name)
            return "quiet"
        if schedule.daily:
            note = await self.build(days=since_previous_send_day(schedule.days, now),
                                    now=_as_utc(now))
        else:
            note = await self.build(now=_as_utc(now))
        if note.error and not note.groups:
            return "error"
        # A non-empty queue is news even when nothing shipped: "38 waiting, oldest 21
        # days" is exactly what the PM should read on a quiet morning.
        if not note.has_news and cfg.digest_skip_when_empty:
            _log.info("release note skipped — nothing delivered", schedule=schedule.name,
                      days=note.days)
            return "empty"
        await self.analyze(note, kind="sáng" if schedule.daily else "tuần")
        title = morning_title(note) if schedule.daily else ""
        return "sent" if await self.post(note, title=title) else "dry-run"


def _as_utc(now: datetime) -> datetime:
    """A local firing time as UTC; a naive one (no timezone set) is the host's clock."""
    return now.astimezone(UTC) if now.tzinfo else now.astimezone().astimezone(UTC)
