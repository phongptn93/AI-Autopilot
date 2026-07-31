"""Tests for pre-accepting Claude Code's workspace-trust dialog on a scratch dir."""

from __future__ import annotations

import json
from pathlib import Path

from ai_autopilot.execution.claude_executor import pretrust_claude_dir


def _fake_home(tmp_path, monkeypatch, payload) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    cfg = home / ".claude.json"
    cfg.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return cfg


def test_trusts_a_new_scratch_dir(tmp_path, monkeypatch):
    cfg = _fake_home(tmp_path, monkeypatch, {"projects": {"C:/other": {"lastCost": 1}}})
    scratch = tmp_path / ".aiwt" / "agent-42"
    scratch.mkdir(parents=True)

    assert pretrust_claude_dir(str(scratch)) is True

    projects = json.loads(cfg.read_text(encoding="utf-8"))["projects"]
    key = next(k for k in projects if k.endswith("agent-42"))
    # Claude Code keys trust by absolute path with forward slashes.
    assert "\\" not in key
    assert projects[key]["hasTrustDialogAccepted"] is True
    assert projects["C:/other"] == {"lastCost": 1}  # untouched


def test_already_trusted_leaves_the_config_byte_identical(tmp_path, monkeypatch):
    scratch = tmp_path / "agent-7"
    scratch.mkdir()
    key = str(scratch.resolve()).replace("\\", "/")
    cfg = _fake_home(
        tmp_path, monkeypatch, {"projects": {key: {"hasTrustDialogAccepted": True}}}
    )
    before = cfg.read_bytes()

    assert pretrust_claude_dir(str(scratch)) is True
    assert cfg.read_bytes() == before  # no rewrite of a live config for nothing


def test_written_config_has_no_bom(tmp_path, monkeypatch):
    # A BOM would make the CLI's JSON.parse fail — it must never be written.
    cfg = _fake_home(tmp_path, monkeypatch, {"projects": {}})
    scratch = tmp_path / "agent-9"
    scratch.mkdir()

    pretrust_claude_dir(str(scratch))

    raw = cfg.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    json.loads(raw.decode("utf-8"))  # still valid JSON


def test_unreadable_config_is_survivable(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))  # no .claude.json

    assert pretrust_claude_dir(str(tmp_path)) is False  # warns, never raises


def test_no_temp_file_left_behind(tmp_path, monkeypatch):
    cfg = _fake_home(tmp_path, monkeypatch, {"projects": {}})
    scratch = tmp_path / "agent-11"
    scratch.mkdir()

    pretrust_claude_dir(str(scratch))

    assert [p.name for p in cfg.parent.iterdir()] == [".claude.json"]
