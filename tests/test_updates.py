"""Self-update: what it takes, what it refuses, and what it will not restart onto."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ai_autopilot import updates
from ai_autopilot.config import Settings
from ai_autopilot.services.updater import UpdaterService


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _Http:
    """Just enough httpx to answer one GET, or to fail the way the real one does."""

    def __init__(self, resp=None, boom: Exception | None = None):
        self._resp, self._boom = resp, boom

    async def get(self, url, **kw):
        if self._boom:
            raise self._boom
        return self._resp


def _release_payload(tag="v9.9.9", assets=None):
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/x/y/releases/tag/{tag}",
        "published_at": "2026-09-23T00:00:00Z",
        "assets": assets if assets is not None else [
            {"name": "ai_autopilot-9.9.9.tar.gz", "browser_download_url": "https://x/sdist.tar.gz"},
            {"name": "ai_autopilot-9.9.9-py3-none-any.whl", "browser_download_url": "https://x/w.whl"},
        ],
    }


# ── reading a release ────────────────────────────────────────────────────────

async def test_the_wheel_is_taken_from_the_assets_not_guessed_from_a_pattern():
    """A URL assembled from a naming convention fails at DOWNLOAD time — after the
    drain, with the poller already stopped. The asset list is the fact; the file name
    is a build detail that has changed before."""
    got = await updates.latest_release(_Http(_Resp(200, _release_payload())), "x/y")
    assert got is not None
    assert got.version == "9.9.9"                 # the leading "v" is a tag convention
    assert got.wheel_url == "https://x/w.whl"     # the .whl, not the .tar.gz next to it
    assert got.installable


async def test_a_release_with_no_wheel_is_not_installable():
    payload = _release_payload(assets=[
        {"name": "notes.txt", "browser_download_url": "https://x/notes.txt"},
    ])
    got = await updates.latest_release(_Http(_Resp(200, payload)), "x/y")
    assert got is not None and not got.installable
    assert updates.upgrade_command(got) == ""     # nothing to tell a human to run either


@pytest.mark.parametrize("resp,boom", [
    (_Resp(403, {}), None),                       # rate limited — the common one
    (_Resp(404, {}), None),
    (None, TimeoutError("no network")),
])
async def test_a_check_that_cannot_reach_github_is_no_news_not_an_error(resp, boom):
    """A machine that cannot reach GitHub is not a machine with a problem to report."""
    assert await updates.latest_release(_Http(resp, boom), "x/y") is None


async def test_a_nonsense_repo_is_not_asked_about():
    assert await updates.latest_release(_Http(_Resp(200, _release_payload())), "") is None


def test_version_comparison_reuses_the_fleet_rule():
    assert updates.newer("2.52.0", "2.53.0") is True
    assert updates.newer("2.52.0", "2.52.0") is False
    assert updates.newer("2.53.0", "2.52.0") is False      # never offer a downgrade
    assert updates.newer("2.52.0", "not-a-version") is False


# ── what this install may do ─────────────────────────────────────────────────

def test_an_editable_checkout_is_refused(tmp_path, monkeypatch):
    """`pip install --upgrade` fights a `pip install -e .` tree instead of updating it,
    and would leave two installs disagreeing about which code is running."""
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    assert updates.install_block(workspace_root=tmp_path) == updates.BLOCK_EDITABLE


def test_a_container_is_refused(monkeypatch):
    """The image is the unit. Anything pip writes here evaporates at the next restart,
    so it would report success and silently revert."""
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert updates.install_block(workspace_root=None) == updates.BLOCK_CONTAINER


def test_saying_there_is_no_checkout_is_not_the_same_as_saying_nothing():
    """`None` used to mean both "this is a wheel install" and "go look it up", so a
    caller stating the first got the second — and a wheel install was reported as
    editable."""
    assert updates.install_block(workspace_root=None) != updates.install_block()


def test_a_plain_wheel_install_is_allowed(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(updates.os.path, "exists", lambda p: False)
    assert updates.install_block(workspace_root=None) == ""


# ── applying ─────────────────────────────────────────────────────────────────

class _Poller:
    def __init__(self, busy: bool):
        self._busy = busy
        self.draining = False

    @property
    def is_idle(self) -> bool:
        return not self._busy

    def finish(self):
        self._busy = False


def _svc(**over):
    cfg = Settings(update_check_enabled=True, update_drain_timeout_minutes=1, **over)
    c = SimpleNamespace(
        config=cfg, http=None, notifier=None,
        audit_repo=SimpleNamespace(record=_noop_record),
    )
    svc = UpdaterService(c)
    svc.latest = updates.Release(version="9.9.9", wheel_url="https://x/w.whl")
    return svc


async def _noop_record(**kw):
    return None


async def test_a_successful_pip_that_did_not_change_the_version_never_restarts(monkeypatch):
    """The safety gate, and the reason it exists.

    `pip install` exiting 0 is not proof the new code is on disk: a wheel can resolve to
    what is already installed, an index can serve a stale artifact, a wrapper can swallow
    a permission error. Restarting here brings the machine back on exactly the code it
    was running and reports it as an update.
    """
    svc = _svc()
    restarted = []
    monkeypatch.setattr(svc, "blocked", lambda: "")
    monkeypatch.setattr(svc, "_pip_install", _ok_install)
    monkeypatch.setattr(svc, "_installed_version", _stale_version)
    monkeypatch.setattr(svc, "_restart", lambda: restarted.append(True))

    await svc.apply(_Poller(busy=False))

    assert restarted == [], "restarted onto an install it could not verify"
    assert svc.job.state == "failed"
    assert "KHÔNG khởi động lại" in svc.job.detail


async def _ok_install(url):
    return True, "Successfully installed"


async def _stale_version():
    return "2.52.0"                                # not the 9.9.9 that was asked for


async def test_work_in_flight_is_waited_out_not_cut_short(monkeypatch):
    """A restart marks every RUNNING execution FAILED "Interrupted (process restarted)",
    so cutting in both loses the run and writes a lie into its history."""
    svc = _svc()
    poller = _Poller(busy=True)
    monkeypatch.setattr(svc, "blocked", lambda: "")
    monkeypatch.setattr(svc, "_pip_install", _ok_install)
    monkeypatch.setattr(svc, "_installed_version", _good_version)
    monkeypatch.setattr(svc, "_restart", lambda: None)
    monkeypatch.setattr("ai_autopilot.services.updater._DRAIN_POLL_SECONDS", 0.05)

    task = asyncio.create_task(svc.apply(poller))
    await asyncio.sleep(0.2)
    assert poller.draining is True, "kept taking new work while updating"
    assert svc.job.state == "draining"             # still waiting, not installing

    poller.finish()
    await asyncio.wait_for(task, timeout=5)
    assert svc.job.state == "restarting"


async def _good_version():
    return "9.9.9"


async def test_a_drain_that_never_finishes_cancels_the_update(monkeypatch):
    """Giving up is the right answer: the alternative is interrupting somebody's run."""
    svc = _svc()
    poller = _Poller(busy=True)
    installed = []
    monkeypatch.setattr(svc, "blocked", lambda: "")
    monkeypatch.setattr(svc, "_pip_install", lambda url: installed.append(url))
    monkeypatch.setattr(svc, "_restart", lambda: None)
    monkeypatch.setattr("ai_autopilot.services.updater._DRAIN_POLL_SECONDS", 0.05)
    svc._config = Settings(update_drain_timeout_minutes=0)   # deadline already passed

    await svc.apply(poller)

    assert installed == [], "installed while a run was still going"
    assert svc.job.state == "failed" and "huỷ cập nhật" in svc.job.detail
    assert poller.draining is False, "left the machine refusing work after giving up"


async def test_a_blocked_install_is_refused_before_anything_is_touched(monkeypatch):
    svc = _svc()
    poller = _Poller(busy=False)
    monkeypatch.setattr(svc, "blocked", lambda: updates.BLOCK_EDITABLE)
    monkeypatch.setattr(svc, "_pip_install", lambda url: pytest.fail("should not install"))

    await svc.apply(poller)

    assert svc.job.state == "failed"
    assert poller.draining is False                # never even started draining


def test_the_settings_belong_to_the_machine():
    """How this install was made and how it comes back are properties of THIS disk — a
    central serving one answer would be serving the wrong one to most of a fleet."""
    from ai_autopilot.dashboard import settings_form as sf

    for key in ("update_check_enabled", "update_repo", "update_check_interval_hours",
                "update_drain_timeout_minutes", "update_restart_mode"):
        assert key in sf.MACHINE_LOCAL, key
        assert key in Settings.model_fields, key
