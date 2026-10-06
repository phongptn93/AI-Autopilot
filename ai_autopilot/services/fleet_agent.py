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
import platform
import shutil
import socket
import time
from collections import deque
from datetime import UTC, datetime

import httpx

from ai_autopilot import fleet, lessons
from ai_autopilot.config import config_file_path
from ai_autopilot.container import Container
from ai_autopilot.dashboard import settings_form
from ai_autopilot.logging_config import describe_exc, get_logger


class FleetAgentService:
    """Heartbeat + config pull every ``fleet_sync_interval_minutes``, and the central's
    command queue every ``fleet_command_poll_seconds``."""

    # Class-level so a partially-built instance (tests build one without __init__)
    # still reads as "not attached" rather than raising mid-heartbeat.
    _poller = None
    _updater = None
    _started: float | None = None

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
        #: Drafts waiting for a human at the CENTRE — shown on this worker's page so a
        #: contributor can see their lesson is queued rather than lost.
        self.knowledge_pending: int = 0
        self._started = time.monotonic()
        self._cmd_task: asyncio.Task | None = None
        # Outcomes not yet reported back. Kept until the central has acknowledged them
        # by answering the next ask, so a failed round trip does not lose a result.
        self._results: list[fleet.CommandResult] = []
        #: The last few commands this machine handled — shown on its own Fleet page,
        #: because "why did my machine stop picking up work" is asked AT the machine.
        self.recent_commands: deque[dict] = deque(maxlen=15)
        self.last_command_poll_at: datetime | None = None
        self.last_command_poll_ok: bool | None = None

    def attach(self, *, poller=None, updater=None) -> None:
        """Give the agent the services its commands act on.

        Passed in rather than found: they live on ``app.state``, and a silent lookup
        miss here would make "pause" report success while the poller kept polling.
        """
        self._poller, self._updater = poller, updater

    @property
    def worker_name(self) -> str:
        """What this machine calls itself. Hostname unless overridden — a name is how
        the central keeps one machine's history together across restarts."""
        return (self._config.fleet_worker_name or "").strip() or socket.gethostname()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        if self._cmd_task is None:
            self._cmd_task = asyncio.create_task(self._command_run(), name="fleet-commands")

    async def stop(self) -> None:
        for name in ("_task", "_cmd_task"):
            task = getattr(self, name)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                setattr(self, name, None)

    # ── remote control ───────────────────────────────────────────────────────

    async def _command_run(self) -> None:
        # Settle first so the startup beat (which may rewrite config) lands before the
        # first command is acted on.
        await asyncio.sleep(5)
        while True:
            try:
                await self.poll_commands()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the loop must outlive any one ask
                self._log.warning("fleet command poll failed", error=describe_exc(exc))
            await asyncio.sleep(max(15, int(self._config.fleet_command_poll_seconds or 60)))

    async def poll_commands(self) -> int:
        """Report finished commands, fetch and run the next ones. Returns how many ran.

        Never raises on a network error, like ``beat``: a central that is down means no
        new commands, not a dead loop.
        """
        cfg = self._config
        base = (cfg.fleet_central_url or "").strip().rstrip("/")
        if not base or not (cfg.fleet_token or "").strip():
            return 0
        self.last_command_poll_at = datetime.now(UTC)
        sending = list(self._results)
        try:
            resp = await self._c.http.post(
                f"{base}/api/fleet/commands",
                json=fleet.CommandExchange(
                    worker=self.worker_name, results=sending,
                ).model_dump(mode="json"),
                headers={fleet.TOKEN_HEADER: cfg.fleet_token},
            )
        except httpx.HTTPError as exc:
            self._log.debug("fleet command poll unreachable", error=describe_exc(exc))
            self.last_command_poll_ok = False
            return 0
        if resp.status_code == 404:
            # A central on an older build has no queue. Nothing to do, nothing to nag.
            self.last_command_poll_ok = None
            return 0
        if resp.status_code >= 400:
            self.last_command_poll_ok = False
            self._log.debug("fleet command poll refused", status=resp.status_code)
            return 0
        self.last_command_poll_ok = True
        # Delivered: drop exactly what was sent, keep anything added meanwhile.
        self._results = self._results[len(sending):]
        ran = 0
        for raw in (resp.json() or {}).get("commands") or []:
            try:
                cmd = fleet.Command(**raw)
            except Exception:  # noqa: BLE001 — a malformed entry is skipped, not fatal
                continue
            ok, detail = await self.execute(cmd)
            ran += 1
            self._results.append(fleet.CommandResult(id=cmd.id, ok=ok, detail=detail[:900]))
            self.recent_commands.appendleft({
                "id": cmd.id, "kind": cmd.kind, "args": cmd.args, "ok": ok,
                "detail": detail, "at": datetime.now(UTC),
            })
            self._log.info("fleet command handled", id=cmd.id, kind=cmd.kind, ok=ok,
                           detail=detail[:200])
            with contextlib.suppress(Exception):
                await self._c.audit_repo.record(
                    actor=f"fleet:{(cfg.fleet_central_url or '')[:80]}", source="fleet",
                    action=f"fleet.command.{cmd.kind}",
                    target=str(cmd.args.get("id") or self.worker_name)[:300],
                    detail=("ok: " if ok else "refused: ") + detail[:500],
                )
        return ran

    async def execute(self, cmd: fleet.Command) -> tuple[bool, str]:
        """Carry out one command. Returns (ok, what happened — in words for the page)."""
        cfg = self._config
        if not cfg.fleet_accept_commands:
            return False, "Máy trạm đã tắt 'Nhận lệnh từ trung tâm'."
        kind = (cmd.kind or "").strip()
        if kind == fleet.CMD_PAUSE:
            return self.pause(str(cmd.args.get("reason") or "tạm dừng từ trung tâm"))
        if kind == fleet.CMD_RESUME:
            return self.resume()
        if kind == fleet.CMD_SYNC:
            ok = await self.beat()
            return ok, self.last_detail
        if kind == fleet.CMD_UPDATE:
            return await self._remote_update(str(cmd.args.get("version") or ""))
        if kind == fleet.CMD_RUN_ITEM:
            return await self._run_item(cmd.args.get("id"))
        return False, f"Lệnh '{kind}' không được bản v{self._version()} hỗ trợ."

    def pause(self, reason: str = "") -> tuple[bool, str]:
        if self._poller is None:
            return False, "Tiến trình này không chạy poller — không có gì để tạm dừng."
        self._poller.paused = True
        self._poller.paused_reason = (reason or "").strip()[:200]
        return True, "Đã tạm dừng nhận việc mới; run đang chạy vẫn chạy tiếp."

    def resume(self) -> tuple[bool, str]:
        if self._poller is None:
            return False, "Tiến trình này không chạy poller."
        was = bool(getattr(self._poller, "paused", False))
        self._poller.paused, self._poller.paused_reason = False, ""
        return True, "Đã tiếp tục nhận việc." if was else "Máy vốn không tạm dừng."

    async def _remote_update(self, version: str) -> tuple[bool, str]:
        if not self._config.fleet_accept_remote_update:
            return False, "Máy trạm không cho phép cập nhật từ xa (fleet_accept_remote_update)."
        updater = self._updater
        if updater is None:
            return False, "Tiến trình này không có bộ cập nhật."
        if updater.job.running:
            return True, "Đang cập nhật sẵn rồi."
        block = updater.blocked()
        if block:
            return False, f"Bản cài này không tự cập nhật được ({block})."
        try:
            await asyncio.wait_for(updater.check(), timeout=25)
        except Exception as exc:  # noqa: BLE001
            return False, f"Không hỏi được GitHub: {describe_exc(exc)}"
        latest = getattr(updater.latest, "version", "") or ""
        if not updater.available:
            return True, f"Đã ở bản mới nhất (v{self._version()})."
        if version and fleet.version_tuple(latest) < fleet.version_tuple(version):
            return False, f"GitHub chỉ có v{latest}, chưa có v{version}."
        # Detached like the dashboard button: draining can take most of an hour, and the
        # outcome is visible anyway — the next heartbeat carries the new version.
        self._update_task = asyncio.create_task(updater.apply(self._poller), name="update-apply")
        return True, f"Bắt đầu cập nhật lên v{latest} (đợi run đang chạy xong rồi cài)."

    async def _run_item(self, raw_id) -> tuple[bool, str]:
        try:
            item_id = int(raw_id)
        except (TypeError, ValueError):
            return False, "Thiếu mã work item."
        if item_id <= 0:
            return False, "Mã work item không hợp lệ."
        if self._config.dry_run:
            return False, "Máy trạm đang dry_run — không ghi gì lên tracker."
        if not self._config.has_tracker_auth:
            return False, "Máy trạm chưa có thông tin đăng nhập tracker."
        from ai_autopilot.services import planning_analyzer

        started = await planning_analyzer.start_items(self._c, [item_id])
        if not started:
            return False, f"Không tìm thấy #{item_id} trong dự án máy này theo dõi."
        tag = self._config.trigger_tag or ""
        note = " (đang tạm dừng — sẽ chạy khi tiếp tục)" if getattr(
            self._poller, "paused", False) else ""
        return True, f"Đã nhận #{item_id} (gắn tag {tag}){note}."

    @staticmethod
    def _version() -> str:
        from ai_autopilot import __version__

        return __version__

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
        learned = await self._exchange_knowledge(base)
        self.last_ok, self.last_applied = True, applied
        parts = []
        if applied:
            parts.append(f"Đã nhận {len(applied)} thiết lập mới.")
        if learned:
            parts.append(f"Nhận {learned} tri thức từ đội.")
        self.last_detail = " ".join(parts) or "Đã khớp với trung tâm."
        return True

    async def _exchange_knowledge(self, base: str) -> int:
        """Contribute what this machine learned, take back what the fleet approved.

        Returns how many lines were NEW here. Failures are swallowed on purpose: a
        centre that is old (404 — no knowledge endpoint yet) or briefly unhappy must
        not turn a successful heartbeat into a failed one. Configuration sync is the
        heartbeat's job and it has already succeeded by this point.
        """
        cfg = self._config
        if not cfg.fleet_knowledge_sync:
            return 0
        workspace = (cfg.workspace_directory or "").strip()
        if not workspace:
            return 0                       # nothing to contribute, nowhere to put a reply
        try:
            payload = fleet.KnowledgeExchange(
                worker=self.worker_name,
                items=[fleet.KnowledgeLine(**row) for row in lessons.contributions(workspace)],
            )
            resp = await self._c.http.post(
                f"{base}/api/fleet/knowledge", json=payload.model_dump(mode="json"),
                headers={fleet.TOKEN_HEADER: cfg.fleet_token},
            )
            if resp.status_code == 404:
                return 0                   # central on an older build — nothing to say
            if resp.status_code >= 400:
                self._log.debug("fleet knowledge refused", status=resp.status_code)
                return 0
            body = resp.json() or {}
            # Only lines the centre approved come back, so anything here is vetted.
            incoming = [
                (str(row.get("repo") or ""), str(row.get("text") or ""))
                for row in (body.get("items") or [])
            ]
            self.knowledge_pending = int(body.get("pending") or 0)
            added = lessons.apply_fleet(workspace, incoming, mode=cfg.fleet_knowledge_accept)
            if added:
                self._log.info("fleet knowledge applied", new=added, total=len(incoming))
            return added
        except Exception as exc:  # noqa: BLE001 — never fail a good heartbeat over this
            self._log.debug("fleet knowledge exchange failed", error=describe_exc(exc))
            return 0

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
        health = await self.build_health()
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
            health=health,
        )

    async def build_health(self) -> fleet.WorkerHealth:
        """What the centre needs to judge this machine from afar. Every probe is
        best-effort: a failed one leaves its field at "unknown", never the report."""
        cfg = self._config
        poller = self._poller
        if poller is None:
            state = fleet.POLLER_ABSENT
        elif getattr(poller, "draining", False):
            state = fleet.POLLER_DRAINING
        elif getattr(poller, "paused", False):
            state = fleet.POLLER_PAUSED
        else:
            state = fleet.POLLER_RUNNING
        health = fleet.WorkerHealth(
            uptime=int(time.monotonic() - self._started) if self._started else 0,
            poller=state,
            paused_reason=getattr(poller, "paused_reason", "") or "",
            capacity=int(cfg.max_concurrent or 1),
            platform=f"{platform.system()} {platform.release()}".strip(),
            dry_run=bool(cfg.dry_run),
            accepts_commands=bool(cfg.fleet_accept_commands),
        )
        with contextlib.suppress(Exception):
            health.tracker_ok = bool(cfg.has_tracker_auth)
        with contextlib.suppress(Exception):
            usage = shutil.disk_usage((cfg.workspace_directory or "").strip() or ".")
            health.disk_free_gb = round(usage.free / 1024**3, 1)
            health.disk_total_gb = round(usage.total / 1024**3, 1)
        with contextlib.suppress(Exception):
            rows, _ = await self._c.execution_repo.search(limit=30)
            finished = [r for r in rows if r.completed_at is not None]
            for r in finished:
                if getattr(r.status, "value", str(r.status)) == "Success":
                    break
                health.fail_streak += 1
            failed = next((r for r in finished
                           if getattr(r.status, "value", str(r.status)) != "Success"), None)
            if failed is not None:
                health.last_error = (failed.error or "")[:300]
                health.last_error_item = int(failed.work_item_id or 0)
        return health

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
