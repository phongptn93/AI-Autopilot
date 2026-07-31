"""Tests for the configuration doctor.

Each case is a configuration that passes ``health.py`` (ADO reachable, Claude reachable,
disk fine) yet can never actually work — which is exactly the gap the doctor exists to
close. Most were diagnosed by hand first.
"""

from __future__ import annotations

from ai_autopilot import doctor
from ai_autopilot.config import Settings

_SANE = {
    "ado_organization": "https://dev.azure.com/org",
    "ado_project": "Proj",
    "ado_pat": "pat",
    "health_host": "127.0.0.1",
}


def _titles(findings, level=None) -> list[str]:
    return [f.title for f in findings if level is None or f.level == level]


def _diagnose(**overrides):
    return doctor.diagnose(Settings(**{**_SANE, **overrides}))


def test_missing_ado_credentials_is_an_error():
    found = _diagnose(ado_pat="", ado_project="")
    assert "Azure DevOps connection incomplete" in _titles(found, doctor.ERROR)


def test_no_trigger_means_nothing_is_ever_picked_up():
    found = _diagnose(trigger_tag="", trigger_states=[])
    assert "Nothing can ever be picked up" in _titles(found, doctor.ERROR)


def test_concurrency_without_worktrees_is_an_error():
    """config.py says worktrees are required for safe max_concurrent > 1; without them
    parallel runs share one checkout and corrupt each other's branches."""
    found = _diagnose(max_concurrent=4, use_worktrees=False)
    assert "Concurrent tasks share one checkout" in _titles(found, doctor.ERROR)
    ok = _diagnose(max_concurrent=4, use_worktrees=True)
    assert "Concurrent tasks share one checkout" not in _titles(ok)


def test_teams_bot_enabled_but_unconfigured():
    """The trap that cost the most time: build_agent() returns None, so the bot is
    silently ABSENT rather than visibly broken."""
    found = _diagnose(teams_agent_enabled=True)
    titles = _titles(found, doctor.ERROR)
    assert "Two-way Teams bot enabled but not configured" in titles
    assert "Teams can never reach the bot" in titles      # health_host is loopback


def test_configured_bot_on_a_public_host_passes():
    found = _diagnose(
        teams_agent_enabled=True, teams_agent_app_id="id",
        teams_agent_app_secret="s", teams_agent_tenant_id="t",
        health_host="0.0.0.0", dashboard_auth_token="x", webhook_secret="y",
    )
    assert "Teams bot configuration complete" in _titles(found, doctor.OK)


def test_exposed_dashboard_without_password_is_an_error():
    found = _diagnose(health_host="0.0.0.0", dashboard_auth_token="",
                      dashboard_auth_password_hash="")
    assert "Dashboard reachable from the network with no password" in _titles(found, doctor.ERROR)


def test_loopback_dashboard_without_password_is_fine():
    assert "Dashboard reachable from the network with no password" not in _titles(_diagnose())


def test_unknown_effort_is_flagged_because_it_is_silently_ignored():
    found = _diagnose(claude_effort_chat="verylow")
    assert "Unknown reasoning-effort value" in _titles(found, doctor.WARN)
    assert "Reasoning effort valid" in _titles(_diagnose(claude_effort_chat=""), doctor.OK)


def test_unattended_without_guardrails_warns():
    found = _diagnose(autonomy_level="unattended", test_gate_enabled=False,
                      policy_protected_paths=[], auto_review_enabled=False)
    assert "Unattended autonomy with few guardrails" in _titles(found, doctor.WARN)


def test_no_notification_channel_warns():
    found = _diagnose(teams_webhook_url="", teams_webhook_urls=[], teams_agent_enabled=False)
    assert "The autopilot cannot tell anyone anything" in _titles(found, doctor.WARN)


def test_advisory_command_that_can_never_match_warns():
    """A typo'd advisory command silently downgrades to ACTION — it changes code."""
    found = _diagnose(comment_command="/ai, /review", comment_advisory_commands="/reviw")
    assert "Advisory command not in the accepted command list" in _titles(found, doctor.WARN)


def test_no_advisory_command_warns():
    found = _diagnose(comment_command="/ai", comment_advisory_commands="")
    assert "No advisory command configured" in _titles(found, doctor.WARN)


def test_missing_workspace_directory_is_an_error(tmp_path):
    found = _diagnose(workspace_directory=str(tmp_path / "nope"))
    assert "Workspace directory does not exist" in _titles(found, doctor.ERROR)


def test_allowed_repo_absent_from_workspace_is_an_error(tmp_path):
    (tmp_path / ".claude").mkdir()
    found = _diagnose(workspace_directory=str(tmp_path), allowed_repos=["Ghost"])
    assert "allowed_repos not found in the workspace" in _titles(found, doctor.ERROR)


def test_real_repo_has_consistent_versions_and_a_valid_manifest():
    """Guards the two release bugs found by hand: __version__ drifting behind pyproject,
    and a Teams command menu over Teams' 10-command limit."""
    found = doctor.diagnose(Settings(**_SANE))
    titles = _titles(found)
    assert "Version drifted between files" not in titles
    assert "Teams command menu over the limit" not in titles
    assert "Teams command titles missing the leading '/'" not in titles


def test_render_lists_problems_first_and_names_next_actions():
    findings = [
        doctor.Finding(doctor.OK, "fine"),
        doctor.Finding(doctor.WARN, "meh", "detail", "do this"),
        doctor.Finding(doctor.ERROR, "broken", "detail", "fix this"),
    ]
    text = doctor.render(findings)
    assert text.index("Must fix") < text.index("Worth fixing") < text.index("Passing")
    assert "Do these first" in text and "1. broken" in text
    assert "/health" in text          # points at the runtime checks it does NOT do


def test_render_says_so_when_everything_passes():
    text = doctor.render([doctor.Finding(doctor.OK, "fine")])
    assert "Nothing to fix" in text


def test_repeat_reminder_without_a_first_reminder_is_a_no_op():
    """The repeat clock only starts after a first reminder, so a repeat value with
    reminders switched off does nothing at all — and reads as if it does."""
    found = _diagnose(
        pr_reviewer_tracking_enabled=True,
        pr_reviewer_reminder_hours=0,
        pr_reviewer_reminder_repeat_hours=24,
    )
    assert "Repeat reminders can never fire" in _titles(found, doctor.WARN)


def test_reminders_configured_but_tracking_off_is_flagged():
    found = _diagnose(
        pr_reviewer_tracking_enabled=False,
        pr_reviewer_reminder_hours=24,
    )
    assert "Reviewer reminders configured but tracking is off" in _titles(found, doctor.WARN)


def test_reminder_cadence_is_reported_when_coherent():
    found = _diagnose(
        pr_reviewer_tracking_enabled=True,
        pr_reviewer_reminder_hours=24,
        pr_reviewer_reminder_repeat_hours=6,
    )
    assert "Reviewer reminders: first at 24h, then every 6h after" in _titles(found, doctor.OK)


def test_report_survives_a_console_that_cannot_encode_icons(capsys, monkeypatch):
    """A cp1252 Windows console raises on "✅"; the doctor must still print its report
    instead of dying with a traceback while diagnosing."""
    import io
    import sys as _sys

    class Cp1252Out(io.StringIO):
        def write(self, s):
            s.encode("cp1252")  # raises UnicodeEncodeError on the icons
            return super().write(s)

    monkeypatch.setattr(_sys, "stdout", Cp1252Out())
    doctor._emit(f"{doctor.OK_ICON} fine\n{doctor.ERROR_ICON} broken\n→ fix it")
    written = _sys.stdout.getvalue()
    assert "[ok] fine" in written
    assert "[X] broken" in written
    assert "-> fix it" in written


def test_auto_review_without_tracking_can_never_fire():
    """Detecting that the bot was added as a reviewer is the tracker's job; with tracking
    off the tracker never starts, so auto-review is dead config."""
    found = _diagnose(pr_auto_review_on_added=True, pr_reviewer_tracking_enabled=False)
    assert "Auto-review can never fire" in _titles(found, doctor.WARN)


def test_pasted_quotes_in_bot_identity_are_flagged():
    """It is compared verbatim, so a stray quote means the bot never recognises itself —
    and never auto-reviews, silently."""
    found = _diagnose(pr_reviewer_tracking_enabled=True, pr_bot_identity='"bot@x.vn"')
    assert "pr_bot_identity has stray whitespace or quotes" in _titles(found, doctor.WARN)
    found = _diagnose(pr_reviewer_tracking_enabled=True, pr_bot_identity="bot@x.vn ")
    assert "pr_bot_identity has stray whitespace or quotes" in _titles(found, doctor.WARN)
    clean = _diagnose(pr_reviewer_tracking_enabled=True, pr_bot_identity="bot@x.vn")
    assert "pr_bot_identity has stray whitespace or quotes" not in _titles(clean)


def test_pr_review_reports_whether_its_concurrency_is_shared():
    shared = _diagnose(pr_reviewer_tracking_enabled=True, max_concurrent=3)
    assert any("shared with execution" in t for t in _titles(shared, doctor.OK))
    own = _diagnose(pr_reviewer_tracking_enabled=True, max_concurrent=3,
                    pr_review_max_concurrent=1)
    assert any("max 1 parallel" in t and "shared" not in t for t in _titles(own, doctor.OK))


def test_pr_review_group_off_is_stated_not_warned():
    """Everything off is a legitimate configuration, not a problem to nag about."""
    found = _diagnose(feedback_loop_enabled=False, pr_reviewer_tracking_enabled=False)
    assert "PR review & feedback: off (both services disabled)" in _titles(found, doctor.OK)


# ── State flows (auto transitions) ────────────────────────────────────────────

def test_one_state_for_every_type_is_flagged():
    """The live bug's class: on_merge_state is a single project-wide value, but an ADO
    state belongs to a TYPE, so it is rejected for every type that lacks it — after which
    the item is tagged done without having moved."""
    found = _diagnose(auto_transition_enabled=True, on_merge_state="Ready to Deploy")
    titles = _titles(found, doctor.WARN)
    assert "Merge transition applies one state to every work-item type" in titles


def test_grouped_flows_replace_that_warning():
    found = _diagnose(
        auto_transition_enabled=True, on_merge_state="Ready to Deploy",
        work_item_flows=[
            {"name": "Dev", "types": ["Bug", "Task"],
             "states": {"on_merge": "Ready to Deploy"}},
        ],
    )
    assert "Merge transition applies one state to every work-item type" not in _titles(found)
    assert any("State flows: 1 group" in t for t in _titles(found, doctor.OK))


def test_single_line_rollup_map_is_flagged_as_unable_to_match():
    """The other half of the roll-up defect: a map needs a line per child state, so a
    one-line map is held whenever any child is anywhere else — which is nearly always."""
    found = _diagnose(
        auto_transition_enabled=True,
        parent_rollup_map=["Ready for Testing = Implement Done"],
    )
    assert "Parent roll-up in parent_rollup_map has a single line" in _titles(found, doctor.WARN)


def test_a_complete_rollup_map_is_not_flagged():
    found = _diagnose(auto_transition_enabled=True, parent_rollup_map=[
        "Active = Active", "Ready to Review = Active", "Ready to Testing = Implement Done",
    ])
    assert not any("single line" in t for t in _titles(found))


def test_flows_configured_while_auto_transitions_are_off():
    found = _diagnose(auto_transition_enabled=False, work_item_flows=[
        {"name": "Dev", "types": ["Bug"], "states": {"on_merge": "Closed"}},
    ])
    assert "1 state flow(s) configured but auto transitions are off" in _titles(found, doctor.WARN)


def test_malformed_flow_config_is_an_error_not_a_crash():
    """A hand-edited config must be reported, not take the process down — which is also
    why work_item_flows is untyped at the pydantic layer."""
    found = _diagnose(work_item_flows=["nonsense"])
    assert "State flow config is malformed" in _titles(found, doctor.ERROR)


def test_the_flow_check_stays_offline():
    """The doctor's contract is no network. Whether a state exists on a type is a
    question only ADO can answer, so that check belongs to the Flow page — this one must
    not reach out, even given a state no project could have."""
    import ai_autopilot.doctor as mod

    calls = []
    original = mod.flows_mod.validate_flows

    def spy(flows, states_by_type):
        calls.append(states_by_type)
        return original(flows, states_by_type)

    mod.flows_mod.validate_flows = spy
    try:
        doctor.check_state_flows(Settings(work_item_flows=[
            {"name": "X", "types": ["Bug"], "states": {"on_merge": "No Such State"}},
        ]))
    finally:
        mod.flows_mod.validate_flows = original
    assert calls == [{}]      # structure only — no type information was fetched


def test_a_lone_first_name_in_an_assignee_field_is_flagged():
    """It now matches strictly, so it very likely matches nobody — which shows up as an
    autopilot that mysteriously stopped picking work up. Say it here instead."""
    found = _diagnose(auto_transition_assignee="Phong")
    titles = _titles(found, doctor.WARN)
    assert 'auto_transition_assignee="Phong" cannot identify one person' in titles
    detail = next(f for f in found if f.title in titles and "Phong" in f.title)
    assert "phong.pham@nois.vn" in detail.fix or "full email" in detail.fix


def test_every_assignee_field_is_checked_not_just_one():
    found = _diagnose(
        auto_transition_assignee="Phong", assignee_trigger_user="Nhi", command_user="Que",
    )
    flagged = [t for t in _titles(found, doctor.WARN) if "cannot identify one person" in t]
    assert len(flagged) == 3


def test_a_full_email_or_name_is_not_flagged():
    found = _diagnose(
        auto_transition_assignee="phong.pham@nois.vn", command_user="Phong Pham",
    )
    assert not any("cannot identify one person" in t for t in _titles(found))
