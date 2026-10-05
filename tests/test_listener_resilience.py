"""The HTTP listener must outlive a client that dies mid-accept.

Regression test for the "dashboard only loads after it hangs" report: on Windows a
single ``[WinError 64]`` from ``AcceptEx`` made CPython close the listening socket, and
nothing ever re-opened it — the process stayed up while every later request hung at TCP
connect. See ``_keep_listener_alive_on_accept_error``.

Platform-independent on purpose: the fault is in an accept callback, so a stub proactor
reproduces it exactly and the guard stays covered on the Linux CI that never sees
``AcceptEx``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ai_autopilot.app import (
    _keep_listener_alive_on_accept_error,
    _quiet_proactor_connection_reset,
)


class _Log:
    """Records what the guard said, so a test can assert it is not silent."""

    def __init__(self) -> None:
        self.warnings: list[dict] = []
        self.debugs: list[dict] = []

    def warning(self, msg, **kw) -> None:
        self.warnings.append({"msg": msg, **kw})

    def debug(self, msg, **kw) -> None:
        self.debugs.append({"msg": msg, **kw})


class _Listener:
    """A listening socket. ``fileno() == -1`` is how asyncio spells "closed"."""

    def __init__(self) -> None:
        self._fd = 7

    def fileno(self) -> int:
        return self._fd

    def close(self) -> None:
        self._fd = -1


class _Proactor:
    """Stands in for ``IocpProactor``: hands out the queued accept results in order."""

    def __init__(self, results: list) -> None:
        self._results = results
        self.calls = 0

    def accept(self, listener):
        self.calls += 1
        outcome = self._results.pop(0)
        future = asyncio.get_running_loop().create_future()
        if isinstance(outcome, BaseException):
            future.set_exception(outcome)
        else:
            future.set_result(outcome)
        return future


def _win_error(code: int) -> OSError:
    exc = OSError(22, "The specified network name is no longer available")
    exc.winerror = code
    return exc


@pytest.mark.asyncio
async def test_failed_accept_retries_instead_of_killing_the_listener():
    """A dropped connection costs one accept, not the listener."""
    proactor = _Proactor([_win_error(64), ("conn", ("10.0.0.2", 51234))])
    log = _Log()

    _keep_listener_alive_on_accept_error(log, loop=SimpleNamespace(_proactor=proactor))
    listener = _Listener()
    conn, addr = await proactor.accept(listener)

    assert (conn, addr) == ("conn", ("10.0.0.2", 51234))
    assert proactor.calls == 2                 # retried rather than gave up
    assert listener.fileno() == 7              # and never closed the listening socket
    # Routine on an Internet-facing port, and handled: recorded, not a warning.
    assert not log.warnings
    assert log.debugs and log.debugs[0]["winerror"] == 64


@pytest.mark.asyncio
async def test_accept_error_on_a_closed_listener_propagates():
    """Shutdown must still end the accept loop — the guard is not a hang machine."""
    proactor = _Proactor([_win_error(64)])

    _keep_listener_alive_on_accept_error(_Log(), loop=SimpleNamespace(_proactor=proactor))
    listener = _Listener()
    listener.close()

    with pytest.raises(OSError):
        await proactor.accept(listener)
    assert proactor.calls == 1                 # no retry against a socket that is gone


@pytest.mark.asyncio
async def test_guard_is_idempotent_and_inert_without_a_proactor():
    """Two lifespans in one process must not stack wrappers; POSIX must not break."""
    proactor = _Proactor([("conn", ("127.0.0.1", 1))])
    loop = SimpleNamespace(_proactor=proactor)

    _keep_listener_alive_on_accept_error(_Log(), loop=loop)
    wrapped = proactor.accept
    _keep_listener_alive_on_accept_error(_Log(), loop=loop)
    assert proactor.accept is wrapped          # wrapped once, not twice

    # A selector loop (POSIX) has no proactor at all: nothing to do, and no crash.
    _keep_listener_alive_on_accept_error(_Log(), loop=SimpleNamespace())


@pytest.mark.asyncio
async def test_peer_gone_errors_are_demoted_but_real_ones_are_not():
    """The log stays readable for "the client left" and loud for everything else."""
    log = _Log()
    loop = asyncio.get_running_loop()
    seen: list = []
    loop.set_exception_handler(lambda _loop, ctx: seen.append(ctx))

    _quiet_proactor_connection_reset(log)
    handler = loop.get_exception_handler()

    handler(loop, {"message": "Task exception was never retrieved",
                   "exception": _win_error(64)})
    assert seen == [] and log.debugs                 # swallowed, with a debug trail

    boom = ValueError("a real bug")
    handler(loop, {"message": "boom", "exception": boom})
    assert [c["exception"] for c in seen] == [boom]  # delegated to the previous handler


@pytest.mark.asyncio
async def test_a_storm_of_dropped_accepts_warns_once():
    """Fifty in a minute is a scan or a network fault — worth one line, not fifty."""
    from ai_autopilot import app as app_mod

    drops = [_win_error(64)] * (app_mod._ACCEPT_STORM_THRESHOLD + 5)
    proactor = _Proactor([*drops, ("conn", ("10.0.0.2", 1))])
    log = _Log()
    _keep_listener_alive_on_accept_error(log, loop=SimpleNamespace(_proactor=proactor))
    await proactor.accept(_Listener())

    assert len(log.warnings) == 1
    assert log.warnings[0]["count"] == app_mod._ACCEPT_STORM_THRESHOLD


def test_uvicorn_invalid_request_is_counted_not_warned(tmp_path):
    """A TLS handshake or a scanner's bytes on the plain port: nothing failed."""
    import logging

    from ai_autopilot import metrics
    from ai_autopilot.logging_config import configure_logging

    configure_logging(level="INFO", log_dir=str(tmp_path))
    counter = metrics.HTTP_CLIENT_DROPPED_TOTAL.labels(stage="invalid_request")
    before = counter._value.get()
    seen: list[logging.LogRecord] = []

    class _Grab(logging.Handler):
        def emit(self, record):
            seen.append(record)

    grab = _Grab()
    logging.getLogger().addHandler(grab)
    try:
        logging.getLogger("uvicorn.error").warning("Invalid HTTP request received.")
        logging.getLogger("uvicorn.error").warning("something else went wrong")
    finally:
        logging.getLogger().removeHandler(grab)

    assert counter._value.get() == before + 1
    assert [r.getMessage() for r in seen] == ["something else went wrong"]
