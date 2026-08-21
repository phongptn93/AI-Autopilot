"""Tests for the editable-settings form helpers."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
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


# ── Export field-set invariants ────────────────────────────────────────────────
# Both exports derive from `model_dump`, so EVERY new Settings field is exported
# automatically. That is the right default for config knobs and the wrong one for
# credentials — these lock the boundary so a future secret can't ride along silently.

# `(^|_)pat$` is anchored on purpose: a loose `_pat` also matches
# `policy_protected_pat|hs`, which is a list of globs a teammate SHOULD receive.
_SECRETISH = re.compile(r"(^|_)pat$|token|secret|password", re.I)
# Fields whose NAME looks secret but which hold no credential.
_NOT_ACTUALLY_SECRET = {
    "daily_budget_tokens", "conflict_ai_min_token_len", "cost_alert_threshold_tokens",
}


def _real_secret_keys() -> set[str]:
    return {
        k for k in Settings.model_fields
        if _SECRETISH.search(k) and k not in _NOT_ACTUALLY_SECRET
    }


def test_shareable_export_contains_no_credential():
    """The plain export is meant to be handed to a teammate. One secret slipping in makes
    it a credential leak, so this asserts over the whole model rather than a fixed list."""
    exported = set(settings_form.export_settings(Settings()))
    leaked = _real_secret_keys() & exported
    assert not leaked, f"secret fields present in the shareable export: {sorted(leaked)}"


def test_shareable_export_omits_machine_specific_paths():
    """Sharing these pins a teammate to this host's filesystem, ports and per-host tag."""
    exported = set(settings_form.export_settings(Settings()))
    for key in ("workspace_directory", "database_url", "health_port", "trigger_tag", "repos"):
        assert key not in exported, key


def test_full_export_does_carry_the_credentials():
    """Its whole purpose is backup / machine migration — a full export without the PAT
    would restore an autopilot that cannot talk to ADO."""
    full = set(settings_form.export_full_settings(Settings()))
    for key in ("ado_pat", "teams_agent_app_secret", "smtp_password", "webhook_secret"):
        assert key in full, key


def test_neither_export_carries_its_own_key_material():
    """The export password would be circular (you need it to decrypt), and the dashboard
    hash is deliberately withheld so a restore cannot clobber the target host's own
    password — see import_full_settings."""
    shareable = set(settings_form.export_settings(Settings()))
    full = set(settings_form.export_full_settings(Settings()))
    for key in ("config_export_password", "dashboard_auth_password_hash"):
        assert key not in shareable, key
        assert key not in full, key


def test_full_export_is_actually_encrypted_and_round_trips():
    cfg = Settings(ado_pat="super-secret-pat")
    blob = settings_form.export_full_encrypted(cfg, "pw")
    assert b"super-secret-pat" not in blob          # not sitting in plaintext
    restored = settings_form.import_full_settings(blob, "pw", set(Settings.model_fields))
    assert restored["ado_pat"] == "super-secret-pat"
    with pytest.raises(ValueError):
        settings_form.import_full_settings(blob, "wrong", set(Settings.model_fields))


# ── Teams channel rows ────────────────────────────────────────────────────────

def test_parse_webhook_channels_reads_name_url_and_active():
    rows = settings_form.parse_webhook_channels({
        "wh_count": "2",
        "wh0_name": " #dev ", "wh0_url": " https://a.example/w/1 ", "wh0_active": "on",
        "wh1_name": "#qc", "wh1_url": "https://b.example/w/2",      # active unticked
    })
    assert rows == [
        {"name": "#dev", "url": "https://a.example/w/1", "active": True},
        {"name": "#qc", "url": "https://b.example/w/2", "active": False},
    ]


def test_parse_webhook_channels_drops_the_blank_row_and_deletions():
    """The page renders one empty row so a channel can be added without a round trip; an
    untouched row must vanish rather than become an error."""
    rows = settings_form.parse_webhook_channels({
        "wh_count": "3",
        "wh0_name": "#dev", "wh0_url": "https://a.example/w/1", "wh0_active": "on",
        "wh1_name": "#gone", "wh1_url": "https://b.example/w/2", "wh1_delete": "on",
        "wh2_name": "", "wh2_url": "",                              # the blank row
    })
    assert [r["name"] for r in rows] == ["#dev"]


def test_parse_webhook_channels_keeps_a_named_row_only_when_it_has_a_url():
    """A name with no URL notifies nothing, so it is not worth storing."""
    assert settings_form.parse_webhook_channels(
        {"wh_count": "1", "wh0_name": "#dev", "wh0_url": "   "}
    ) == []


def test_parse_webhook_channels_survives_a_bogus_count():
    assert settings_form.parse_webhook_channels({}) == []
    assert settings_form.parse_webhook_channels({"wh_count": "nope"}) == []


def test_the_old_bare_url_list_is_no_longer_a_form_field():
    """It must not be in FIELDS: parse_form would then emit an empty list for it and WIPE a
    legacy multi-channel setup the first time anyone saved the page."""
    assert "teams_webhook_urls" not in {f.key for f in settings_form.FIELDS}
    assert "teams_webhook_urls" not in settings_form.parse_form({})


def test_every_form_field_exists_on_settings():
    """A control that saves to a key `Settings` does not have is a control that does
    nothing — and the page gives no sign of it. This is the check that catches the typo,
    and the reason the whole session's new settings were reachable only by hand-editing
    YAML until they were added here."""
    unknown = sorted(
        f.key for f in settings_form.FIELDS
        if f.key not in Settings.model_fields and f.key != "dashboard_auth_password"
    )
    assert unknown == []          # dashboard_auth_password is hashed into *_hash on save


def test_time_windows_and_guards_are_editable_in_the_ui():
    """These decide when the autopilot may work and when it may interrupt someone. If
    they are not on the page, the only way to set them is to edit YAML on the server."""
    keys = {f.key for f in settings_form.FIELDS}
    for key in (
        "timezone", "schedule_start", "schedule_end", "schedule_days",
        "notify_hours_start", "notify_hours_end", "notify_days", "notify_quiet_max_held",
        "spec_drift_enabled", "spec_drift_tag", "pr_require_work_item_link",
        "process_health_enabled", "interactive_close_on", "interactive_resume_on_rework",
    ):
        assert key in keys, key


def test_parse_form_round_trips_a_quiet_hours_setup():
    updates = settings_form.parse_form({
        "timezone": " Asia/Ho_Chi_Minh ",
        "notify_hours_start": "08:00",
        "notify_hours_end": "18:00",
        "notify_days": "Mon,Tue,Wed,Thu,Fri",
        "notify_quiet_max_held": "150",
        "process_health_adhoc_threshold_pct": "27.5",
        "interactive_close_on": "pr_closed",
        "spec_drift_enabled": "on",          # checkbox present
        # pr_require_work_item_link absent → unchecked → False
    })
    cfg = Settings(**{k: v for k, v in updates.items() if k in Settings.model_fields})
    assert cfg.timezone == "Asia/Ho_Chi_Minh"          # trimmed
    assert cfg.notify_quiet_max_held == 150
    assert cfg.process_health_adhoc_threshold_pct == 27.5   # float kind, not truncated
    assert cfg.spec_drift_enabled is True
    assert cfg.pr_require_work_item_link is False

    from ai_autopilot.scheduling import QuietHours
    assert QuietHours(cfg).enabled is True


def test_float_field_ignores_a_blank_or_broken_value():
    """Blank must keep the existing value rather than send "" to a float field."""
    assert "process_health_adhoc_threshold_pct" not in settings_form.parse_form(
        {"process_health_adhoc_threshold_pct": "  "}
    )
    assert "process_health_adhoc_threshold_pct" not in settings_form.parse_form(
        {"process_health_adhoc_threshold_pct": "thirty"}
    )
