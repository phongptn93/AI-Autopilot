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


def due_today(config, now: datetime) -> bool:
    """Is ``now`` (local) one of the configured send days? Blank days = every day."""
    days = parse_days(config.release_note_days)
    return not (config.release_note_days or "").strip() or now.weekday() in days


class ReleaseNoteService:
    """The page's builder and, when enabled, the scheduled Teams post."""

    def __init__(self, c) -> None:
        self._c = c
        self._config = c.config
        self._task: asyncio.Task | None = None

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
        released = release_notes.released_between(
            changes, start, end, list(cfg.release_note_states or []))
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
        return built

    # ── posting ─────────────────────────────────────────────────────────────

    async def post(self, note: release_notes.ReleaseNote) -> bool:
        """Post ``note`` to every channel THROUGH the notifier — so the alert policy and
        the quiet window apply, as for every other sender. True when posted."""
        from ai_autopilot.dashboard.common import work_item_link_base

        if self._config.dry_run:
            _log.info("[DRY-RUN] would post release note", items=note.total)
            return False
        heading, text = release_notes.render_teams(
            note, link_base=work_item_link_base(self._config))
        url = page_url(self._config, note.days)
        await self._c.notifier.notify(NotificationMessage(
            work_item=WorkItemInfo(id=0), type=NotificationType.INFO,
            heading=heading, text=text,
            actions=[("📣 Mở release note đầy đủ", url)] if url else [],
        ))
        _log.info("release note posted", items=note.total, days=note.days)
        return True

    # ── schedule ────────────────────────────────────────────────────────────

    def start(self) -> None:
        cfg = self._config
        if not cfg.release_note_enabled:
            return
        if not cfg.delivery_history_enabled:
            _log.warning("release note schedule off — delivery_history_enabled is false, "
                         "so there is no state history to build it from")
            return
        self._task = asyncio.create_task(self._run(), name="release-note")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        cfg = self._config
        tz = resolve_tz(cfg.timezone)

        def _now() -> datetime:
            return datetime.now(tz) if tz else datetime.now()  # noqa: DTZ005 — host fallback

        _log.info("release note schedule started", at=cfg.release_note_at,
                  days=cfg.release_note_days or "every day")
        while True:
            try:
                delay = seconds_until(cfg.release_note_at.strip(), _now())
                if delay is None:
                    # A constant that does not parse will not parse next time either.
                    _log.error("release note schedule off — release_note_at is not HH:MM",
                               value=cfg.release_note_at)
                    return
                await asyncio.sleep(delay)
                await self.run_once(_now())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad cycle must not kill the loop
                _log.error("release note cycle failed", error=describe_exc(exc))

    async def run_once(self, now: datetime) -> str:
        """One scheduled firing. Returns what happened, for the log and the tests."""
        cfg = self._config
        if not due_today(cfg, now):
            return "not-today"
        if cfg.digest_respect_quiet_hours and QuietHours(cfg).is_quiet():
            _log.info("release note skipped — outside notify hours")
            return "quiet"
        note = await self.build()
        if note.error and not note.groups:
            return "error"
        if not note.total and cfg.digest_skip_when_empty:
            _log.info("release note skipped — nothing delivered", days=note.days)
            return "empty"
        return "sent" if await self.post(note) else "dry-run"
