"""Trigger clarity: explain one item's pickup, show state sources, split command rights.

The trigger rules live in three places (WIQL, poller filters, role doors). These tests
pin the explainer to the SAME rules the poller applies, and pin that the command-rights
split changes nothing by default.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from ai_autopilot import trigger_check
from ai_autopilot.app import create_app
from ai_autopilot.config import SdlcRole, Settings
from ai_autopilot.dashboard import settings_form
from ai_autopilot.models import WorkItemInfo

_ROLES = {
    "dev": SdlcRole(stages=["implement"], waits_in="Ready for Development", auto=True),
    "qc": SdlcRole(stages=["test"], waits_in="Ready for Testing", auto=False),
}


def _cfg(**kw) -> Settings:
    base = dict(dry_run=True, trigger_tag="vm-autopilot", assignee_trigger_tag="ai-autopilot",
                assignee_trigger_user="dev@nois.vn", trigger_states=["New", "Ready for Testing"],
                ado_project="Khatoco", sdlc_roles=_ROLES, stage_entry_tag="autopilot-run")
    base.update(kw)
    return Settings(**base)


def _item(**kw) -> WorkItemInfo:
    base = dict(id=42, title="t", project="Khatoco", work_item_type="Task", state="New",
                tags=[], assigned_to="Dev One", assigned_to_email="dev@nois.vn")
    base.update(kw)
    return WorkItemInfo(**base)


def _check(report, label):
    return next(c for c in report.checks if c.label == label)


# ── explain() ──────────────────────────────────────────────────────────────────

def test_the_machine_tag_owns_an_item_whoever_it_is_assigned_to():
    r = trigger_check.explain(_item(tags=["vm-autopilot"], assigned_to_email="x@y.z",
                                    assigned_to="Someone Else"), _cfg())
    assert r.picked and "SẼ nhận" in r.headline


def test_the_shared_tag_needs_the_owner_as_assignee():
    cfg = _cfg()
    assert trigger_check.explain(_item(tags=["ai-autopilot"]), cfg).picked
    r = trigger_check.explain(_item(tags=["ai-autopilot"], assigned_to="Other Person",
                                    assigned_to_email="other@nois.vn"), cfg)
    assert not r.picked
    own = _check(r, "Thuộc về máy này")
    assert own.ok is False and "không khớp" in own.detail


def test_a_manual_role_door_drops_a_ticked_state():
    # "Ready for Testing" is ticked, but QC waits there and is NOT auto → not polled.
    r = trigger_check.explain(_item(tags=["vm-autopilot"], state="Ready for Testing"), _cfg())
    assert not r.picked
    st = _check(r, "State được poll")
    assert st.ok is False and "qc" in st.detail and "KHÔNG tự" in st.detail


def test_an_auto_role_door_is_polled_without_being_ticked():
    r = trigger_check.explain(_item(tags=["vm-autopilot"], state="Ready for Development"), _cfg())
    assert r.picked and r.role == "dev"
    assert "dev" in _check(r, "State được poll").detail


def test_a_hold_tag_blocks_pickup():
    r = trigger_check.explain(_item(tags=["vm-autopilot", "autopilot-hold"]), _cfg())
    assert not r.picked and _check(r, "Không bị giữ lại").ok is False


def test_a_run_now_tag_bypasses_ownership_and_state():
    # Not this machine's tag, a state nobody polls — the run-now sweep still takes it.
    r = trigger_check.explain(_item(tags=["autopilot-run:qc"], state="Closed",
                                    assigned_to_email="x@y.z", assigned_to="X"), _cfg())
    assert r.picked and r.role == "qc" and r.run_now == "autopilot-run:qc"


def test_a_role_run_now_tag_beats_the_shared_one():
    r = trigger_check.explain(_item(tags=["autopilot-run", "autopilot-run:qc"]), _cfg())
    assert r.run_now == "autopilot-run:qc" and r.role == "qc"


def test_another_project_is_reported():
    r = trigger_check.explain(_item(tags=["vm-autopilot"], project="Other"), _cfg())
    assert not r.picked and _check(r, "Project được quét").ok is False


# ── summary / state sources ───────────────────────────────────────────────────

def test_summary_names_both_ways_and_the_effective_states():
    s = trigger_check.summary(_cfg())
    assert "`vm-autopilot`" in s and "`ai-autopilot`" in s and "dev@nois.vn" in s
    assert "Ready for Development" in s and "Ready for Testing" not in s


def test_state_sources_label_each_rule():
    src = {x.state: x for x in trigger_check.state_sources(_cfg())}
    assert src["New"].source == "manual" and src["New"].polled
    assert src["Ready for Testing"].source == "role-manual" and not src["Ready for Testing"].polled
    dev = src["Ready for Development"]
    assert dev.source == "role-auto" and dev.polled


# ── command rights split ──────────────────────────────────────────────────────

def test_owner_commands_by_default_exactly_as_before():
    cfg = _cfg(command_users=["lead@nois.vn"])
    assert cfg.owner_can_command is True
    assert cfg.command_allowlist == ["dev@nois.vn", "lead@nois.vn"]


def test_owner_can_be_excluded_from_commanding_but_still_owns_items():
    cfg = _cfg(command_users=["lead@nois.vn"], owner_can_command=False)
    assert cfg.command_allowlist == ["lead@nois.vn"]
    # ownership untouched
    assert "dev@nois.vn" in cfg.effective_command_users
    assert trigger_check.explain(_item(tags=["ai-autopilot"]), cfg).picked


def test_excluding_the_owner_with_nobody_listed_does_not_open_the_gate():
    # An empty roster means "anyone"; turning the owner off must not produce that.
    cfg = _cfg(owner_can_command=False)
    assert cfg.command_allowlist == ["dev@nois.vn"]


def test_command_fields_live_in_their_own_section_and_stay_local():
    sec = {f.key: f.section for f in settings_form.FIELDS}
    for key in ("owner_can_command", "command_users", "commands_from_anyone"):
        assert sec[key] == "🔐 Quyền ra lệnh", key
    assert "owner_can_command" in settings_form.MACHINE_LOCAL


# ── pages ─────────────────────────────────────────────────────────────────────

def test_settings_shows_the_summary_card_and_state_sources(tmp_path):
    cfg = _cfg(database_url=f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    with TestClient(create_app(cfg)) as client:
        html = client.get("/dashboard/settings").text
    assert "Máy này nhận việc của ai" in html
    assert "<code>vm-autopilot</code>" in html
    assert 'id="triggers"' in html and 'id="commands"' in html
    assert "🙋 qc · không poll" in html
    # state checkboxes keep their names, so saving writes what it always did
    assert 'name="trigger_states__Ready for Testing"' in html


def test_trigger_check_page_without_an_id_shows_the_rules(tmp_path):
    cfg = _cfg(database_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    with TestClient(create_app(cfg)) as client:
        html = client.get("/dashboard/trigger-check").text
        bad = client.get("/dashboard/trigger-check?id=abc").text
    assert "Máy này nhận việc của ai" in html and "autopilot-run:qc" in html
    assert "không phải số work item" in bad
