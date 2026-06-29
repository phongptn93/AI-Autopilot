"""Tests for the editable-settings form helpers."""

from __future__ import annotations

from pathlib import Path

import yaml

from ai_autopilot.config import Settings
from ai_autopilot.dashboard import settings_form


def test_parse_form_coerces_types():
    form = {
        "repo_working_directory": "/repos/app",
        "poll_interval_seconds": "45",
        "max_concurrent": "3",
        "autonomy_level": "unattended",
        # checkboxes present → True; absent keys → False
        "use_worktrees": "on",
    }
    updates = settings_form.parse_form(form)
    assert updates["repo_working_directory"] == "/repos/app"
    assert updates["poll_interval_seconds"] == 45
    assert updates["max_concurrent"] == 3
    assert updates["autonomy_level"] == "unattended"
    assert updates["use_worktrees"] is True
    assert updates["dry_run"] is False  # checkbox not in form


def test_parse_form_skips_blank_password_and_int():
    form = {"ado_pat": "  ", "poll_interval_seconds": ""}
    updates = settings_form.parse_form(form)
    assert "ado_pat" not in updates
    assert "poll_interval_seconds" not in updates


def test_parse_form_keeps_nonblank_password():
    updates = settings_form.parse_form({"ado_pat": "secret-token"})
    assert updates["ado_pat"] == "secret-token"


def test_save_to_yaml_merges(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("ado_project: Old\nkeep_me: yes\n", encoding="utf-8")

    settings_form.save_to_yaml(path, {"ado_project": "New", "trigger_tag": "go"})

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["ado_project"] == "New"   # overwritten
    assert data["trigger_tag"] == "go"    # added
    assert data["keep_me"] is True        # preserved


def test_save_to_yaml_creates_file(tmp_path: Path):
    path = tmp_path / "new.yaml"
    settings_form.save_to_yaml(path, {"trigger_tag": "x"})
    assert path.exists()
    assert yaml.safe_load(path.read_text())["trigger_tag"] == "x"


def test_apply_to_config_mutates_live_settings():
    config = Settings()
    settings_form.apply_to_config(config, {"trigger_tag": "deploy", "max_concurrent": 4})
    assert config.trigger_tag == "deploy"
    assert config.max_concurrent == 4
