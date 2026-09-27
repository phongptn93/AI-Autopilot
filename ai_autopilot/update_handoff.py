"""Install an update from OUTSIDE the running process (Windows).

Why this exists: on Windows the in-process update ran ``pip install --upgrade`` while
the autopilot itself was still running. pip removes the old package and then replaces
``Scripts\\ai-autopilot.exe`` — the executable of the running process, which Windows
keeps locked. pip failed half way ([WinError 32]), and the still-running process was
left serving from a package whose files (templates included) had been partly removed:
every page answered ``TemplateNotFound``.

The fix is to never install over ourselves. The autopilot writes the script below to a
temp file, starts it as a detached process, and exits. The helper waits until the
autopilot's process is gone — its files and its .exe are then free — runs pip (retrying
while Windows releases the launcher), verifies the version from a clean interpreter,
writes the outcome to a status file, and starts the autopilot again. If pip fails, pip
rolls the uninstall back (nothing is locked any more), so the OLD version still starts
and the dashboard shows why the update failed.

``HELPER`` is plain source with no import of ``ai_autopilot``: it runs while that very
package is being replaced.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HELPER = r'''
import json, os, subprocess, sys, time

def alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(h, 0) == 258   # WAIT_TIMEOUT
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def main():
    args = json.loads(sys.argv[1])
    status_path = args["status"]
    def write(**kw):
        data = {"target": args["target"], "finished": time.time(), **kw}
        with open(status_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    print("AI Autopilot update -> v%s" % args["target"], flush=True)
    deadline = time.time() + args.get("wait_seconds", 120)
    while alive(args["parent"]) and time.time() < deadline:
        time.sleep(0.5)
    if alive(args["parent"]):
        waited = args.get("wait_seconds", 120)
        write(ok=False, error="the autopilot did not exit within %ss" % waited)
        return
    out, ok = "", False
    for attempt in range(args.get("attempts", 6)):
        # The .exe launcher can outlive its python child by a moment; a locked file is
        # worth another try, anything else is a real failure.
        p = subprocess.run(args["pip"], capture_output=True, text=True)
        out = (p.stdout or "") + (p.stderr or "")
        print(out[-2000:], flush=True)
        if p.returncode == 0:
            ok = True
            break
        if "WinError 32" not in out and "being used by another process" not in out:
            break
        time.sleep(args.get("retry_seconds", 3))
    version = ""
    try:
        v = subprocess.run(args["verify"], capture_output=True, text=True, timeout=60)
        version = (v.stdout or "").strip()
    except Exception as exc:
        out += "\nverify failed: %s" % exc
    if ok and version == args["target"]:
        write(ok=True, version=version)
    else:
        write(ok=False, version=version, error=out[-1500:] or "pip failed")
    flags = 0
    if os.name == "nt" and args.get("console", True):
        flags = subprocess.CREATE_NEW_CONSOLE
    cwd = args.get("cwd") or None
    env = dict(os.environ)
    if args.get("config_file"):
        env["AUTOPILOT_CONFIG_FILE"] = args["config_file"]   # the SAME config, always
    subprocess.Popen(args["relaunch"], close_fds=True, creationflags=flags, cwd=cwd, env=env)

if __name__ == "__main__":
    main()
'''


def pin_config_path() -> str:
    """Make ``AUTOPILOT_CONFIG_FILE`` absolute in this process's environment (inherited by
    whatever it starts) and return it. The default is the RELATIVE ``config.yaml``, so a
    restarted process that lands in another directory would find no config and prompt
    for a new dashboard password — reading as if the old config had been wiped."""
    raw = os.getenv("AUTOPILOT_CONFIG_FILE", "config.yaml")
    absolute = str(Path(raw).resolve())
    os.environ["AUTOPILOT_CONFIG_FILE"] = absolute
    return absolute


def status_path() -> Path:
    """Where the helper leaves the outcome for the next start to read."""
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ai-autopilot"
    base.mkdir(parents=True, exist_ok=True)
    return base / "update-status.json"


def plan(target: str, wheel_url: str, *, parent: int | None = None) -> dict:
    """The helper's arguments — every command explicit, so a test can swap them."""
    config_file = pin_config_path()
    return {
        "target": target,
        "config_file": config_file,
        "parent": os.getpid() if parent is None else parent,
        "status": str(status_path()),
        "pip": [sys.executable, "-m", "pip", "install", "--upgrade", wheel_url],
        "verify": [sys.executable, "-c",
                   "import importlib.metadata as m;print(m.version('ai-autopilot'))"],
        "relaunch": [sys.executable, "-m", "ai_autopilot"],
        "cwd": os.getcwd(),
        "wait_seconds": 120,
        "attempts": 6,
        "retry_seconds": 3,
    }


def launch(args: dict) -> int:
    """Write the helper and start it detached; its pid. The caller exits right after."""
    fd, path = tempfile.mkstemp(prefix="ai-autopilot-update-", suffix=".py")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(HELPER)
    with open(args["status"], "w", encoding="utf-8") as f:
        json.dump({"target": args["target"], "pending": True}, f)
    flags = 0
    if sys.platform == "win32":
        # Its own console: the operator watches pip run, and closing the old console
        # does not take the helper with it.
        flags = subprocess.CREATE_NEW_CONSOLE
    proc = subprocess.Popen(  # noqa: S603 — our interpreter, our script
        [sys.executable, path, json.dumps(args)], close_fds=True, creationflags=flags,
        cwd=args.get("cwd") or None,
    )
    return proc.pid


def read_outcome() -> dict | None:
    """The last handoff's outcome, consumed once. None when there is none to report."""
    p = status_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("pending"):
        # Written by launch() and never overwritten: the helper died before finishing.
        data = {"target": data.get("target", ""), "ok": False,
                "error": "the update helper did not report back"}
    with contextlib.suppress(OSError):
        p.unlink()
    return data
