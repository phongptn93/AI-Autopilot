"""Check for a newer release, and apply one when a human asks.

Two halves that must not be confused. The **check** runs on a slow timer and only ever
writes to a field on this object — it cannot change anything about the machine. The
**apply** runs once, when somebody presses the button, and is the only code in this
project that stops the process.

The apply is written around one fact: restarting mid-run is not free. The poller marks
every execution still RUNNING as FAILED with "Interrupted (process restarted)" when it
comes back up, so a careless restart both loses the work and writes a lie into its
history. So it drains first, and if it cannot drain it gives up rather than cutting in.

And it verifies before it restarts. ``pip install`` exiting 0 is not proof the new code
is on disk — a wheel can resolve to the version already installed, an index can serve a
stale artifact, a permission error can be swallowed by a wrapper. Restarting on an
unverified install is how a machine ends up "updated" to exactly what it was running,
with nothing in the log to say so.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field

from ai_autopilot import updates
from ai_autopilot.container import Container
from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.proc import spawn_kwargs, terminate_tree

# pip over the network on a slow link, plus wheel install. Generous: the cost of it
# being too short is a half-applied update.
_INSTALL_TIMEOUT_SECONDS = 900
_VERIFY_TIMEOUT_SECONDS = 60
# How often the drain loop asks "is anything still running".
_DRAIN_POLL_SECONDS = 5
# How long after boot the first check happens. Long enough that a short-lived process
# never makes the request, short enough that a real install notices a release promptly.
_FIRST_CHECK_DELAY_SECONDS = 30


@dataclass
class UpdateJob:
    """What an in-flight (or finished) update is doing, for the page to read."""

    # idle | draining | installing | verifying | restarting | failed | done
    state: str = "idle"
    detail: str = ""
    target: str = ""
    started_at: float = field(default_factory=time.time)

    @property
    def running(self) -> bool:
        return self.state in ("draining", "installing", "verifying", "restarting")


class UpdaterService:
    """Owns the release check and, on request, the update itself."""

    def __init__(self, c: Container) -> None:
        self._c = c
        self._config = c.config
        self._log = get_logger("services.updater")
        self._task: asyncio.Task | None = None
        #: Newest release seen, or None. Read by the dashboard; never blocks a page.
        self.latest: updates.Release | None = None
        self.checked_at: float = 0.0
        self.job = UpdateJob()

    # ── the check ────────────────────────────────────────────────────────────

    def start(self) -> None:
        # Sync, like every other service here: app.py calls start() and awaits stop().
        if self._task is None and self._config.update_check_enabled:
            self._task = asyncio.create_task(self._run(), name="update-check")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        # Settle first. A process that lives for seconds — a test spinning up the app, a
        # `doctor` run, a crash loop restarting — has no business phoning GitHub at all,
        # and checking on the startup path put an outbound request with a 20s ceiling in
        # front of every one of them (measured: ~1s added to each of 88 app tests, and a
        # sandbox with no network would have paid the full timeout). Nothing here is
        # urgent: a release published yesterday can be noticed half a minute after boot.
        await asyncio.sleep(_FIRST_CHECK_DELAY_SECONDS)
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a check must never kill its loop
                self._log.info("update check errored", error=describe_exc(exc))
            hours = max(1, int(self._config.update_check_interval_hours or 6))
            await asyncio.sleep(hours * 3600)

    async def check(self) -> updates.Release | None:
        from ai_autopilot import __version__

        release = await updates.latest_release(self._c.http, self._config.update_repo)
        self.checked_at = time.time()
        self.latest = release
        if release and updates.newer(__version__, release.version):
            self._log.info(
                "a newer release is available", running=__version__,
                latest=release.version, installable=release.installable,
            )
        return release

    @property
    def available(self) -> bool:
        """Is there a newer release than the one running?"""
        from ai_autopilot import __version__

        return bool(self.latest and updates.newer(__version__, self.latest.version))

    # ── the apply ────────────────────────────────────────────────────────────

    def blocked(self) -> str:
        """Why this machine may not apply an update, or ''."""
        return updates.install_block()

    async def apply(self, poller=None) -> None:
        """Drain, install, verify, restart. Never raises into the caller.

        ``poller`` is passed in rather than looked up: it lives on ``app.state``, not on
        the container, and a silent ``getattr`` miss here would mean an update that
        never drained and restarted straight through whatever was running.
        """
        try:
            await self._apply(poller)
        except Exception as exc:  # noqa: BLE001 — the button must not 500
            self._fail(f"cập nhật lỗi: {describe_exc(exc)}")

    async def _apply(self, poller=None) -> None:
        release = self.latest
        if not self.available or release is None:
            return self._fail("không có bản mới nào để cài")
        if not release.installable:
            return self._fail(
                f"bản v{release.version} không kèm file .whl — không có gì để cài"
            )
        block = self.blocked()
        if block:
            return self._fail(f"bản cài này không tự cập nhật được ({block})")

        self.job = UpdateJob(state="draining", target=release.version,
                             detail="đang đợi các task hiện tại chạy xong")
        self._log.info("update: draining before restart", target=release.version)
        if not await self._drain(poller):
            return self._fail(
                "vẫn còn task đang chạy sau "
                f"{self._config.update_drain_timeout_minutes} phút — huỷ cập nhật "
                "(không cắt ngang run đang chạy)"
            )

        self.job.state = "installing"
        self.job.detail = "pip install"
        ok, output = await self._pip_install(release.wheel_url)
        if not ok:
            return self._fail(f"pip install thất bại: {output[-400:]}")

        self.job.state = "verifying"
        self.job.detail = "kiểm tra version vừa cài"
        installed = await self._installed_version()
        if installed != release.version:
            # The whole reason this step exists. Restarting here would bring the machine
            # back on exactly the code it was already running, reported as a success.
            return self._fail(
                f"cài xong nhưng version trên đĩa vẫn là {installed or 'không đọc được'}, "
                f"không phải {release.version} — KHÔNG khởi động lại"
            )

        await self._audit("update.applied", f"{release.version} installed")
        self.job.state = "restarting"
        self.job.detail = f"khởi động lại vào v{release.version}"
        self._log.info("update installed — restarting", version=release.version)
        # Give the redirect a moment to reach the browser before the process goes.
        await asyncio.sleep(1.0)
        self._restart()

    async def _drain(self, poller=None) -> bool:
        """Stop taking new work and wait for what is running. False = gave up."""
        if poller is not None:
            poller.draining = True
        # 0 is a real answer, not a missing one: "only update while nothing is running".
        # Clamping it up to a minute would have made that setting quietly mean its
        # opposite — wait, when the operator asked not to.
        minutes = self._config.update_drain_timeout_minutes
        deadline = time.monotonic() + max(0, int(minutes if minutes is not None else 60)) * 60
        try:
            # Idle is checked BEFORE the clock, so a zero timeout on an idle machine
            # updates immediately instead of reading as "already out of time".
            while True:
                if poller is None or poller.is_idle:
                    return True
                if time.monotonic() >= deadline:
                    return False
                await asyncio.sleep(_DRAIN_POLL_SECONDS)
        finally:
            # Whatever happened, a machine that is not restarting must take work again.
            if poller is not None and self.job.state != "restarting":
                poller.draining = False

    async def _pip_install(self, wheel_url: str) -> tuple[bool, str]:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", wheel_url]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **spawn_kwargs(),
            )
        except Exception as exc:  # noqa: BLE001
            return False, describe_exc(exc)
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_INSTALL_TIMEOUT_SECONDS
            )
        except TimeoutError:
            await terminate_tree(proc)
            return False, f"quá {_INSTALL_TIMEOUT_SECONDS}s"
        text = (out or b"").decode("utf-8", "replace")
        return proc.returncode == 0, text

    async def _installed_version(self) -> str:
        """The version pip would import NOW, read in a clean child process.

        In-process ``importlib.metadata`` would answer from this process's own already
        imported distribution metadata, which is exactly the stale answer that makes the
        check worthless.
        """
        code = (
            "import importlib.metadata as m;"
            "print(m.version('ai-autopilot'))"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                **spawn_kwargs(),
            )
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=_VERIFY_TIMEOUT_SECONDS
            )
        except Exception:  # noqa: BLE001 — unreadable is "not verified", not a crash
            return ""
        return (out or b"").decode("utf-8", "replace").strip()

    def _restart(self) -> None:
        """Bring the process back on the new code. Does not return on success."""
        mode = (self._config.update_restart_mode or "auto").strip().lower()
        if mode == "auto":
            # Windows has no real exec: the CRT emulates it with CreateProcess + exit,
            # so the pid changes and whatever launched us (run.bat, a console) sees the
            # command finish. A detached respawn is the honest version of that.
            mode = "spawn" if sys.platform == "win32" else "exec"
        argv = [sys.executable, "-m", "ai_autopilot"]
        try:
            if mode == "exec":
                os.execv(sys.executable, argv)        # noqa: S606 — our own interpreter
            elif mode == "spawn":
                flags = 0
                if sys.platform == "win32":
                    flags = subprocess.CREATE_NEW_CONSOLE
                subprocess.Popen(argv, close_fds=True, creationflags=flags)  # noqa: S603
                os._exit(0)
            else:                                     # "exit" — a supervisor restarts us
                os._exit(0)
        except Exception as exc:  # noqa: BLE001
            self._fail(f"cài xong nhưng khởi động lại thất bại: {describe_exc(exc)}")

    # ── bookkeeping ──────────────────────────────────────────────────────────

    def _fail(self, detail: str) -> None:
        self.job.state = "failed"
        self.job.detail = detail
        self._log.error("update failed", detail=detail, target=self.job.target)
        # A failed update is a machine sitting on old code believing it asked not to be —
        # worth an operator's attention. A merely AVAILABLE update is not: that would be
        # a notification every six hours forever.
        asyncio.create_task(self._announce_failure(detail))

    async def _announce_failure(self, detail: str) -> None:
        await self._audit("update.failed", detail)
        notifier = getattr(self._c, "notifier", None)
        if notifier is None:
            return
        try:
            await notifier.broadcast_digest(
                "⬆️ Cập nhật thất bại", f"Máy vẫn đang chạy bản cũ. {detail}"
            )
        except Exception as exc:  # noqa: BLE001
            self._log.info("could not announce update failure", error=describe_exc(exc))

    async def _audit(self, action: str, detail: str) -> None:
        repo = getattr(self._c, "audit_repo", None)
        if repo is None:
            return
        try:
            await repo.record(
                actor="dashboard", source="dashboard", action=action,
                target=self.job.target, detail=detail,
            )
        except Exception:  # noqa: BLE001 — audit is best-effort, like everywhere else
            return
