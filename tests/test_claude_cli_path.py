"""Which `claude` binary the Agent SDK runs.

On Windows an npm install leaves only a `claude.CMD` shim on PATH, and the SDK refuses
batch scripts — every SDK run failed with "Refusing to execute batch script". The
resolver hands the SDK a native claude.exe instead, when one is on the machine.
"""

from __future__ import annotations

from ai_autopilot.config import Settings
from ai_autopilot.doctor import ERROR, OK, check_claude_cli
from ai_autopilot.execution import claude_client as cc


def _windows(monkeypatch, tmp_path, on_path: str):
    monkeypatch.setattr(cc.os, "name", "nt")
    monkeypatch.setattr(cc.shutil, "which", lambda _b: on_path)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    cc.configure_cli("")


def _extension(home, version: str):
    exe = (home / ".vscode" / "extensions" / f"anthropic.claude-code-{version}-win32-x64"
           / "resources" / "native-binary" / "claude.exe")
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    return exe


def test_a_batch_shim_on_path_is_replaced_by_the_newest_native_binary(monkeypatch, tmp_path):
    _windows(monkeypatch, tmp_path, r"C:\npm\claude.CMD")
    _extension(tmp_path, "2.1.9")
    newest = _extension(tmp_path, "2.1.283")          # 283 > 9 numerically, not as text
    assert cc.cli_path_for_sdk() == str(newest)


def test_the_official_installer_binary_is_preferred(monkeypatch, tmp_path):
    _windows(monkeypatch, tmp_path, r"C:\npm\claude.cmd")
    _extension(tmp_path, "2.1.283")
    native = tmp_path / ".local" / "bin" / "claude.exe"
    native.parent.mkdir(parents=True)
    native.write_text("", encoding="utf-8")
    assert cc.cli_path_for_sdk() == str(native)


def test_a_real_executable_on_path_is_left_to_the_sdk(monkeypatch, tmp_path):
    _windows(monkeypatch, tmp_path, r"C:\Users\x\.local\bin\claude.exe")
    _extension(tmp_path, "2.1.283")
    assert cc.cli_path_for_sdk() is None


def test_an_explicit_path_wins_and_a_missing_one_falls_back(monkeypatch, tmp_path):
    _windows(monkeypatch, tmp_path, r"C:\npm\claude.CMD")
    mine = tmp_path / "tools" / "claude.exe"
    mine.parent.mkdir()
    mine.write_text("", encoding="utf-8")
    cc.configure_cli(f'"{mine}"')                     # quotes pasted from Explorer
    assert cc.cli_path_for_sdk() == str(mine)
    cc.configure_cli(str(tmp_path / "gone.exe"))
    fallback = _extension(tmp_path, "2.1.283")
    assert cc.cli_path_for_sdk() == str(fallback)
    cc.configure_cli("")


def test_doctor_names_a_batch_shim_with_no_native_binary_as_an_error(monkeypatch, tmp_path):
    _windows(monkeypatch, tmp_path, r"C:\npm\claude.CMD")
    monkeypatch.setattr("ai_autopilot.doctor.shutil.which", lambda _b: r"C:\npm\claude.CMD")
    [f] = check_claude_cli(Settings())
    assert f.level == ERROR and "batch shim" in f.title and "install.ps1" in f.fix
    exe = _extension(tmp_path, "2.1.283")
    [f] = check_claude_cli(Settings())
    assert f.level == OK and str(exe) in f.title
