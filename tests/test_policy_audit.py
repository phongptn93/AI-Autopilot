"""Tests for the policy engine (hard change guardrails) and the audit trail."""

from __future__ import annotations

import pytest

from ai_autopilot import policy
from ai_autopilot.data import Database
from ai_autopilot.data.repository import AuditRepository


# ── policy engine ─────────────────────────────────────────────────────────────
def test_protected_path_blocks_subtree():
    v = policy.check_changes(
        ["k8s/deploy/nois-api.yaml", "src/app.py"], protected_paths=["k8s/*"]
    )
    assert len(v) == 1 and "k8s/*" in v[0] and "nois-api.yaml" in v[0]


def test_protected_basename_pattern_matches_nested():
    v = policy.check_changes(["config/prod.env"], protected_paths=["*.env"])
    assert len(v) == 1


def test_windows_paths_and_case_are_normalised():
    v = policy.check_changes(["K8S\\Deploy.YAML"], protected_paths=["k8s/*"])
    assert len(v) == 1


def test_blast_radius_cap():
    files = [f"src/f{i}.py" for i in range(11)]
    v = policy.check_changes(files, protected_paths=[], max_files=10)
    assert len(v) == 1 and "11 files" in v[0]


def test_policy_off_means_no_violations():
    assert policy.check_changes(["k8s/x.yaml"], protected_paths=[], max_files=0) == []
    assert policy.check_changes([], protected_paths=["k8s/*"], max_files=5) == []


# ── audit trail ───────────────────────────────────────────────────────────────
@pytest.fixture
async def audit(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}")
    await db.create_all()
    yield AuditRepository(db)
    await db.dispose()


async def test_audit_record_and_recent(audit):
    await audit.record(actor="a@x.com", source="teams", action="ticket.created",
                       target="#42", detail="SSO bug")
    await audit.record(actor="dashboard", source="dashboard", action="config.updated",
                       target="trigger_tag")
    events = await audit.recent()
    assert len(events) == 2
    assert events[0].action == "config.updated"          # newest first
    assert events[1].actor == "a@x.com" and events[1].target == "#42"


async def test_audit_action_prefix_filter(audit):
    await audit.record(actor="x", source="teams", action="ticket.created")
    await audit.record(actor="x", source="dashboard", action="config.exported_full")
    only_config = await audit.recent(action="config.")
    assert [e.action for e in only_config] == ["config.exported_full"]
