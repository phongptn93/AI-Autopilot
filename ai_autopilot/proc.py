"""Killing a subprocess that a shell launched — and the children it spawned.

``proc.kill()`` kills the process we started and nothing below it. That is enough
for :func:`asyncio.create_subprocess_exec`, and wrong for
:func:`asyncio.create_subprocess_shell`: the shell is the child, the runner is its
*grand*child, and killing the shell leaves the runner alive holding the stdout pipe
we are still reading. ``await proc.wait()`` then blocks until the runner finishes on
its own — so a timeout meant to bound a hung test suite instead waits for it, and a
runner that never exits (``ng test`` in watch mode is the one we hit) blocks forever.
Measured before this existed: a 1-second timeout returned after 25 seconds, exactly
when the grandchild happened to end.

So kill the tree, then bound the wait too — a kill that silently fails must not turn
back into the hang it was added to prevent.

Callers on POSIX must launch with ``start_new_session=True`` so the tree is a process
group we can signal; :func:`spawn_kwargs` supplies that (and is a no-op on Windows,
where the tree is found from the pid instead).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from typing import Any

from ai_autopilot.logging_config import describe_exc, get_logger

_log = get_logger("proc")

# How long to wait for a killed tree to actually go away before giving up and
# returning anyway. Reaching this means the kill did not take (a pid we may no
# longer own, a process wedged in the kernel) — rare, and never worth hanging on.
_REAP_TIMEOUT_SECONDS = 5.0


def spawn_kwargs() -> dict[str, Any]:
    """Extra kwargs that make a subprocess killable as a tree.

    ``start_new_session`` puts the child in its own process group on POSIX, which is
    what :func:`terminate_tree` signals. Windows ignores it; the tree is resolved from
    the pid by ``taskkill /T`` there.
    """
    return {"start_new_session": True}


async def terminate_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill ``proc`` and every process it spawned. Best-effort; never raises.

    Returns once the tree is gone or :data:`_REAP_TIMEOUT_SECONDS` has passed, so the
    caller is bounded either way.
    """
    pid = proc.pid
    try:
        if sys.platform == "win32":
            # /T covers the tree (cmd → runner → whatever the runner forked); /F because
            # a hung runner will not honour a polite close.
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(killer.wait(), timeout=_REAP_TIMEOUT_SECONDS)
        else:
            # SIGKILL straight away: this path only runs after a timeout has already
            # elapsed, so the process has had its chance to finish.
            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception as exc:  # noqa: BLE001 — a failed kill must not mask the timeout
        _log.warning("could not kill process tree", pid=pid, error=describe_exc(exc))

    # The pipes only close once the LAST holder exits, which is the point of killing
    # the tree — but if something survived, stop waiting rather than hang here.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=_REAP_TIMEOUT_SECONDS)
        return
    _log.warning("process tree outlived its kill", pid=pid, waited=_REAP_TIMEOUT_SECONDS)
