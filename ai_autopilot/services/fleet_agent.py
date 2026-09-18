"""Worker side of fleet mode: report in, pull the shared configuration, apply it.

One periodic call does both. A worker that can report is a worker that can be
configured, so there is no state where the centre can see a machine it cannot reach with
a settings change — and there is one direction of connectivity to get working, not two.

Nothing here may take the process down. A machine that cannot reach the central VM is a
machine that still has work to do with the configuration it already has; losing the
poller because a heartbeat failed would turn a network blip into an outage.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from datetime import UTC, datetime

import httpx

from ai_autopilot import fleet
from ai_autopilot.config import config_file_path
from ai_autopilot.container import Container
from ai_autopilot.dashboard import settings_form
from ai_autopilot.logging_config import describe_exc, get_logger


class FleetAgentService:
    """Heartbeat + config pull, every ``fleet_sync_interval_minutes``."""

    def __init__(self, container: Container) -> None:
        self._c = container
        self._config = container.config
        self._log = get_logger("services.fleet_agent")
        self._task: asyncio.Task | None = None
        # What the last round trip did. Kept in memory so the Fleet page can answer
        # "did this machine reach the centre, and when" without a reader having to open
        # the server's log — which on a worker is usually the one machine they are not
        # sitting at. Lost on restart, which is honest: the next beat is seconds away.
        self.last_beat_at: datetime | None = None
        self.last_ok: bool | None = None
        self.last_detail: str = ""
        self.last_applied: list[str] = []
        # What the central said it is running, and whether this machine trails it. The
        # central already showed this drift on its own page; the machine that has to be
        # updated could not see it anywhere.
        self.central_version: str = ""
        self.behind: bool = False
        self._warned_version: str = ""   # so the nag is once per version, not per beat

    @property
    def worker_name(self) -> str:
        """What this machine calls itself. Hostname unless overridden — a name is how
        the central keeps one machine's history together across restarts."""
        return (self._config.fleet_worker_name or "").strip() or socket.gethostname()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        # Beat once at startup rather than after a full interval: a machine that has just
        # been switched on is exactly when somebody is looking at the fleet page for it.
        while True:
            try:
                await self.beat()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a heartbeat must never end the loop
                self._log.warning("fleet heartbeat failed", error=describe_exc(exc))
            await asyncio.sleep(max(60, int(self._config.fleet_sync_interval_minutes) * 60))

    async def beat(self) -> bool:
        """One heartbeat. True when the central answered. Never raises on a network error.

        Returns rather than logs-and-forgets so a test (and a future "sync now" button)
        can ask whether the round trip actually worked.
        """
        cfg = self._config
        self.last_beat_at = datetime.now(UTC)
        self.last_applied = []
        base = (cfg.fleet_central_url or "").strip().rstrip("/")
        if not base or not (cfg.fleet_token or "").strip():
            # Nothing to do, and nothing to warn about every interval: doctor says this
            # once, loudly, instead of the log saying it 144 times a day.
            return self._beat_failed("Chưa khai URL trung tâm hoặc token chung.")
        report = await self.build_report()
        try:
            resp = await self._c.http.post(
                f"{base}/api/fleet/heartbeat",
                json=report.model_dump(mode="json"),
                headers={fleet.TOKEN_HEADER: cfg.fleet_token},
            )
        except httpx.HTTPError as exc:
            self._log.warning("fleet central unreachable", url=base, error=describe_exc(exc))
            return self._beat_failed(f"Không gọi được trung tâm: {describe_exc(exc)}")
        if resp.status_code == 401:
            # Named separately from other failures: a wrong token fails identically to a
            # network outage in the logs otherwise, and the fix is completely different.
            self._log.error("fleet token rejected by central — check fleet_token", url=base)
            return self._beat_failed("Trung tâm từ chối token (401) — token 2 phía chưa khớp.")
        if resp.status_code >= 400:
            self._log.warning("fleet heartbeat rejected", status=resp.status_code, url=base)
            return self._beat_failed(f"Trung tâm trả lỗi HTTP {resp.status_code}.")
        body = resp.json() or {}
        self._note_central_version(str(body.get("central_version") or ""))
        applied = await self._apply(body)
        self.last_ok, self.last_applied = True, applied
        self.last_detail = (
            f"Đã nhận {len(applied)} thiết lập mới." if applied else "Đã khớp với trung tâm."
        )
        return True

    def _note_central_version(self, central: str) -> None:
        """Compare this build against the central's, and say so when we are behind.

        The central's page has always been able to see the drift; the machine that has
        to DO something about it could not. A worker running older code may not even
        understand the settings it is being handed — it silently drops keys its
        ``Settings`` has no field for — so "the central upgraded" has to reach the
        worker's own log and its own page, with the command that fixes it.

        Said once per central version, not once per beat: at a ten-minute interval the
        nag would file 144 identical lines a day and bury everything else in the log.
        """
        from ai_autopilot import __version__

        self.central_version = central
        self.behind = fleet.is_behind(__version__, central)
        if not self.behind or central == self._warned_version:
            return
        self._warned_version = central
        self._log.warning(
            "this worker is running an OLDER build than the central — update it",
            worker_version=__version__, central_version=central,
            fix="pip install --upgrade "
                "https://github.com/phongptn93/AI-Autopilot/releases/latest/download/"
                f"ai_autopilot-{central}-py3-none-any.whl",
        )

    def _beat_failed(self, detail: str) -> bool:
        """Record why the round trip did not happen, and report it as a failure."""
        self.last_ok, self.last_detail = False, detail
        return False

    async def build_report(self) -> fleet.WorkerReport:
        """This machine's current condition, entirely derived from local state."""
        from ai_autopilot import __version__

        cfg = self._config
        running: list[fleet.RunningRun] = []
        done = failed = 0
        with contextlib.suppress(Exception):   # a DB hiccup must not cost the heartbeat
            rows, _ = await self._c.execution_repo.search(status="Running", limit=50)
            now = datetime.now(UTC)
            for r in rows:
                started = r.started_at
                if started is not None and started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                running.append(fleet.RunningRun(
                    id=r.work_item_id, title=(r.title or "")[:200],
                    role=(r.profile or ""), skill=(r.skill_used or ""),
                    elapsed=int((now - started).total_seconds()) if started else 0,
                ))
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            done, failed = await self._today_counts(midnight)
        return fleet.WorkerReport(
            name=self.worker_name,
            hostname=socket.gethostname(),
            version=__version__,
            profile=(cfg.sdlc_profile or cfg.sdlc_default_profile or ""),
            # The tags that make this machine ITS OWN: what it claims work with, and who
            # it answers for. They are exactly the settings the central does not supply,
            # so the fleet page is where you go to see what each host chose for itself.
            #
            # `stage_entry_tag` used to be in here and did not belong: it is the SHARED
            # run-now tag the central serves to everybody, so it printed the identical
            # chip on every row of a column headed "tag riêng của máy" — the one column
            # whose whole job is to show where two machines differ.
            tags=[t for t in (
                *cfg.effective_trigger_tags, cfg.assignee_trigger_tag,
            ) if t],
            config_hash=self._local_hash(),
            running=running, done_today=done, failed_today=failed,
        )

    async def _today_counts(self, since: datetime) -> tuple[int, int]:
        """Runs finished today, split success/failure. Zero when the query is unavailable
        — the heartbeat's value is liveness, and a count is not worth losing it for."""
        rows, _ = await self._c.execution_repo.search(limit=200)
        done = failed = 0
        for r in rows:
            at = r.completed_at
            if at is None:
                continue
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            if at < since:
                continue
            if getattr(r.status, "value", str(r.status)) == "Success":
                done += 1
            else:
                failed += 1
        return done, failed

    def _local_hash(self) -> str:
        """Fingerprint of the shareable part of THIS machine's config.

        Computed the same way the central computes its document's hash, so "equal" means
        "this machine is running what the centre is serving" — including for a machine
        that was configured by hand to match, which is then correctly reported as in sync
        rather than nagged forever.
        """
        _, digest = fleet.config_document(self._config)
        return digest

    async def _apply(self, payload: dict) -> list[str]:
        """Apply the central's document, minus everything this machine owns.

        Returns the keys actually written, so the caller can say what a sync DID rather
        than only that it happened — "đã đồng bộ" and "đã đổi 6 thiết lập" are different
        answers and only one of them tells you to go look at something.
        """
        document = payload.get("config")
        if not isinstance(document, dict) or not document:
            return []                   # already in sync — the common case
        updates = fleet.strip_local(document, self._config.fleet_local_keys)
        # Only the keys that actually differ: applying identical values would rewrite
        # config.yaml on every beat and fill the audit trail with changes that changed
        # nothing, which is how a real change becomes impossible to spot.
        #
        # Compared in the DOCUMENT's own shape, not against the live attribute. The
        # central sends JSON, so `sdlc_roles` arrives as a dict of dicts while this
        # machine holds a dict of `SdlcRole` — those two never compare equal, so every
        # structured setting counted as "changed" on every single beat: config.yaml
        # rewritten, an audit row filed, and the fleet page showing a worker eternally
        # out of sync over settings that were identical all along.
        mine = fleet.config_document(self._config)[0]
        changed = {k: v for k, v in updates.items() if mine.get(k) != v}
        if not changed:
            return []
        settings_form.save_to_yaml(config_file_path(), changed)
        settings_form.apply_to_config(self._config, changed)
        with contextlib.suppress(Exception):
            self._c.ado.refresh()
        self._log.info("fleet config applied", keys=sorted(changed), count=len(changed))
        await self._c.audit_repo.record(
            actor=self.worker_name, source="fleet", action="config.synced",
            target=(self._config.fleet_central_url or "")[:300],
            detail=f"{len(changed)} keys: {', '.join(sorted(changed))}",
        )
        return sorted(changed)
