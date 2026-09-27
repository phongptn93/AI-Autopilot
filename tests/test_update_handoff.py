"""The Windows self-update must never install over the running process.

Field bug: `pip install --upgrade` ran inside the live autopilot; pip could not replace
the locked `Scripts\\ai-autopilot.exe` ([WinError 32]), failed half way, and the old
process kept serving from a gutted package — every page raised TemplateNotFound.

These tests run the REAL helper script with stand-in commands: it waits for the parent
to exit, retries pip while the file is locked, verifies, reports, and relaunches.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ai_autopilot import update_handoff
from ai_autopilot.config import Settings


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    # plan() pins AUTOPILOT_CONFIG_FILE in os.environ; registering it here makes
    # monkeypatch restore it, so no test leaks a pinned path into the next.
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", "config.yaml")


def _run_helper(tmp_path: Path, *, pip_script: str, version: str, target: str,
                parent: int = 0) -> tuple[dict, Path]:
    helper = tmp_path / "helper.py"
    helper.write_text(update_handoff.HELPER, encoding="utf-8")
    marker = tmp_path / "relaunched.txt"
    args = {
        "target": target, "parent": parent, "status": str(update_handoff.status_path()),
        "pip": [sys.executable, "-c", pip_script],
        "verify": [sys.executable, "-c", f"print('{version}')"],
        "relaunch": [sys.executable, "-c",
                     "import os; open(r'%s', 'w').write(os.environ.get('AUTOPILOT_CONFIG_FILE', ''))"
                     % marker],
        "config_file": str(tmp_path / "real" / "config.yaml"),
        "cwd": str(tmp_path), "wait_seconds": 20, "attempts": 4, "retry_seconds": 0.2,
        "console": False,
    }
    subprocess.run([sys.executable, str(helper), json.dumps(args)], check=True, timeout=60)
    for _ in range(100):                       # relaunch is detached: give it a moment
        if marker.exists():
            break
        time.sleep(0.1)
    return json.loads(Path(args["status"]).read_text(encoding="utf-8")), marker


def test_helper_waits_for_the_parent_then_installs_and_relaunches(tmp_path):
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
    started = time.monotonic()
    status, marker = _run_helper(tmp_path, pip_script="print('Successfully installed')",
                                 version="9.9.9", target="9.9.9", parent=parent.pid)
    assert time.monotonic() - started >= 1.0          # it did wait for the parent to go
    assert status["ok"] is True and status["version"] == "9.9.9"
    assert marker.exists()                             # the autopilot was started again
    parent.wait(5)


def test_helper_retries_while_the_launcher_is_locked(tmp_path):
    counter = tmp_path / "n.txt"
    pip = (
        "import sys, pathlib\n"
        f"p = pathlib.Path(r'{counter}')\n"
        "n = int(p.read_text()) if p.exists() else 0\n"
        "p.write_text(str(n + 1))\n"
        "if n < 2:\n"
        "    print('[WinError 32] The process cannot access the file because it is "
        "being used by another process'); sys.exit(1)\n"
        "print('ok')\n"
    )
    status, _ = _run_helper(tmp_path, pip_script=pip, version="9.9.9", target="9.9.9")
    assert status["ok"] is True and counter.read_text() == "3"   # two locked, third works


def test_a_failed_install_still_restarts_the_old_version_and_says_why(tmp_path):
    pip = "import sys; print('ERROR: No matching distribution'); sys.exit(1)"
    status, marker = _run_helper(tmp_path, pip_script=pip, version="2.57.0", target="2.58.0")
    assert status["ok"] is False and "No matching distribution" in status["error"]
    assert marker.exists()                   # the machine comes back, on the old version


def test_installed_but_wrong_version_is_a_failure(tmp_path):
    status, _ = _run_helper(tmp_path, pip_script="print('ok')", version="2.57.0",
                            target="2.58.0")
    assert status["ok"] is False and status["version"] == "2.57.0"


def test_a_helper_that_never_reported_is_a_failure_not_silence():
    p = update_handoff.status_path()
    p.write_text(json.dumps({"target": "2.58.0", "pending": True}), encoding="utf-8")
    out = update_handoff.read_outcome()
    assert out["ok"] is False and "did not report" in out["error"]
    assert update_handoff.read_outcome() is None       # consumed once


# ── the service ─────────────────────────────────────────────────────────────

def _updater(**cfg):
    from types import SimpleNamespace

    from ai_autopilot.services.updater import UpdaterService

    async def _noop(*a, **k):
        return None

    c = SimpleNamespace(config=Settings(**cfg), http=None,
                        audit_repo=SimpleNamespace(record=_noop))
    return UpdaterService(c)


async def test_windows_spawn_mode_hands_off_and_never_pips_in_process(monkeypatch):
    from ai_autopilot import updates

    svc = _updater(update_restart_mode="spawn", update_drain_timeout_minutes=0)
    svc.latest = updates.Release(version="99.0.0", wheel_url="https://x/ai-99.0.0.whl")
    monkeypatch.setattr(updates, "install_block", lambda *a, **k: "")
    launched: list[dict] = []
    monkeypatch.setattr(update_handoff, "launch", lambda args: launched.append(args) or 4242)

    async def never(*a, **k):
        raise AssertionError("pip must not run inside the live process on Windows")

    svc._pip_install = never
    exited: list[int] = []
    monkeypatch.setattr("ai_autopilot.services.updater.os._exit", exited.append)
    await svc._apply(None)
    assert launched and launched[0]["target"] == "99.0.0"
    assert launched[0]["pip"][-1] == "https://x/ai-99.0.0.whl"
    assert exited == [0] and svc.job.state == "restarting"


def test_startup_reports_a_failed_handoff_on_the_dashboard():
    update_handoff.status_path().write_text(json.dumps(
        {"target": "2.58.0", "ok": False, "error": "[WinError 5] Access is denied"}),
        encoding="utf-8")
    svc = _updater(update_check_enabled=False)
    svc._report_handoff()
    assert svc.job.state == "failed" and "Access is denied" in svc.job.detail


def test_the_restarted_process_reads_the_same_config_file(tmp_path):
    """A relaunch in another directory found no config and prompted for a new dashboard
    password — which read as "the update wiped my config". It is pinned absolutely."""
    _, marker = _run_helper(tmp_path, pip_script="print('ok')", version="9.9.9", target="9.9.9")
    assert marker.read_text(encoding="utf-8") == str(tmp_path / "real" / "config.yaml")


def test_plan_pins_an_absolute_config_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", "config.yaml")
    args = update_handoff.plan("9.9.9", "https://x/ai-9.9.9.whl", parent=1)
    assert Path(args["config_file"]).is_absolute()
    assert args["config_file"] == str((tmp_path / "config.yaml").resolve())
    import os
    assert os.environ["AUTOPILOT_CONFIG_FILE"] == args["config_file"]


def test_password_prompt_says_which_config_it_could_not_find(tmp_path, monkeypatch, capsys):
    from ai_autopilot import __main__ as main

    monkeypatch.chdir(tmp_path)                        # a folder with no config.yaml
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", "config.yaml")
    monkeypatch.setattr(main.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(main.getpass, "getpass", lambda prompt="": "")   # give up
    main._ensure_dashboard_password()
    out = capsys.readouterr().out
    assert "WARNING: no config file at" in out and str(tmp_path) in out
    assert not (tmp_path / "config.yaml").exists()     # nothing created by giving up
