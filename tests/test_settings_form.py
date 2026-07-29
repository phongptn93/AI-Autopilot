"""Tests for the editable-settings form helpers."""

from __future__ import annotations

from pathlib import Path

import yaml

from ai_autopilot.config import Settings
from ai_autopilot.dashboard import settings_form


def test_parse_form_coerces_types():
    form = {
        "workspace_directory": "/ws",
        "poll_interval_seconds": "45",
        "max_concurrent": "3",
        "autonomy_level": "unattended",
        "repo_descriptions": "squad-a, squad-b\nsquad-c",  # list kind: comma + newline split
        # checkboxes present → True; absent keys → False
        "auto_review_enabled": "on",
    }
    updates = settings_form.parse_form(form)
    assert updates["workspace_directory"] == "/ws"
    assert updates["poll_interval_seconds"] == 45
    assert updates["max_concurrent"] == 3
    assert updates["autonomy_level"] == "unattended"
    assert updates["repo_descriptions"] == ["squad-a", "squad-b", "squad-c"]
    assert updates["auto_review_enabled"] is True
    assert updates["dry_run"] is False  # checkbox not in form


def test_parse_states_combines_checkboxes_and_manual():
    form = {
        "_all_states__trigger_states": "New,Active,Done",
        "trigger_states__New": "on",
        "trigger_states__Active": "on",
        # "Done" not ticked → excluded
        "trigger_states__manual": "Doing\nĐang làm, Active",  # "Active" dup dropped
    }
    assert settings_form.parse_states(form, "trigger_states") == [
        "New", "Active", "Doing", "Đang làm"
    ]


def test_parse_states_empty():
    assert settings_form.parse_states({}, "trigger_states") == []


def test_parse_form_trigger_states_is_stateset():
    # No checkboxes/manual → empty list (stateset, not the old free-text field).
    assert settings_form.parse_form({})["trigger_states"] == []


def test_parse_repos_whitelist():
    form = {"_all_repos": "Backend-Fresh,Micro-Frontend,Secret",
            "repo__Backend-Fresh": "on", "repo__Micro-Frontend": "on"}
    assert settings_form.parse_repos(form) == ["Backend-Fresh", "Micro-Frontend"]
    assert settings_form.parse_repos({"_all_repos": ""}) == []


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


def test_export_settings_excludes_secrets_and_machine_specific():
    config = Settings(
        ado_pat="super-secret", ado_project="MyProj", workspace_directory="/local/ws",
        trigger_tag="myhost-autopilot", resolved_state="Closed",
        dashboard_auth_token="dash-token", webhook_secret="hook-secret",
    )
    exported = settings_form.export_settings(config)
    # secrets + machine-specific keys are dropped
    assert "ado_pat" not in exported
    assert "workspace_directory" not in exported
    assert "trigger_tag" not in exported
    # regression: web-surface secrets must NOT leak into the shareable export
    assert "dashboard_auth_token" not in exported
    assert "dashboard_auth_password_hash" not in exported
    assert "webhook_secret" not in exported
    assert "config_export_password" not in exported
    # shareable config is kept
    assert exported["ado_project"] == "MyProj"
    assert exported["resolved_state"] == "Closed"


def test_export_full_settings_includes_secrets_but_not_mechanism_keys():
    config = Settings(
        ado_pat="super-secret", smtp_password="smtp-pw", ado_project="MyProj",
        config_export_password="pw", dashboard_auth_password_hash="pbkdf2_sha256$1$a$b",
    )
    full = settings_form.export_full_settings(config)
    # the full export deliberately carries secrets (for encrypted backup)
    assert full["ado_pat"] == "super-secret"
    assert full["smtp_password"] == "smtp-pw"
    assert full["ado_project"] == "MyProj"
    # ...but not the export/auth mechanism's own material
    assert "config_export_password" not in full
    assert "dashboard_auth_password_hash" not in full


def test_export_full_encrypted_round_trips():
    from ai_autopilot import security

    config = Settings(ado_pat="super-secret", ado_project="MyProj")
    blob = settings_form.export_full_encrypted(config, "pw-123")
    restored = yaml.safe_load(security.decrypt_bytes(blob, "pw-123").decode("utf-8"))
    assert restored["ado_pat"] == "super-secret"
    assert restored["ado_project"] == "MyProj"


def test_import_full_settings_restores_secrets():
    config = Settings(ado_pat="top-secret", ado_project="MyProj", smtp_password="smtp-pw")
    blob = settings_form.export_full_encrypted(config, "k")
    updates = settings_form.import_full_settings(blob, "k", set(Settings.model_fields))
    # full restore keeps secrets (unlike the shareable import_settings)
    assert updates["ado_pat"] == "top-secret"
    assert updates["smtp_password"] == "smtp-pw"
    assert updates["ado_project"] == "MyProj"


def test_import_full_settings_wrong_password_raises():
    import pytest

    blob = settings_form.export_full_encrypted(Settings(ado_pat="x"), "right")
    with pytest.raises(ValueError):
        settings_form.import_full_settings(blob, "wrong", set(Settings.model_fields))


def test_import_settings_keeps_known_drops_secrets_and_unknown():
    valid = set(Settings.model_fields)
    raw = (
        "ado_project: Imported\nresolved_state: Done\n"
        "ado_pat: leaked\nworkspace_directory: /their/ws\n"
        "trigger_tag: theirhost-autopilot\nbogus_key: 1\n"
    )
    updates = settings_form.import_settings(raw, valid)
    assert updates == {"ado_project": "Imported", "resolved_state": "Done"}


def test_import_settings_rejects_non_mapping():
    import pytest

    with pytest.raises(ValueError):
        settings_form.import_settings("- just\n- a\n- list", set(Settings.model_fields))
