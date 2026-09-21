"""One machine, two Azure DevOps organizations.

Every workspace shared the machine's single org and PAT. A workspace can now declare
its own, and its work items are then read and written through a separate client.

The tests that matter are the routing ones. An item discovered in org B whose state is
then updated on org A either 404s or — far worse, and the reason this is not a nicety
— lands on a same-named project in the WRONG organization.
"""

from __future__ import annotations

from types import SimpleNamespace

from ai_autopilot.ado.client import AdoClient
from ai_autopilot.config import Settings, WorkspaceConfig
from ai_autopilot.container import Container

ROOT = "https://dev.azure.com/main-org"
OTHER = "https://dev.azure.com/other-org"


def _settings(**over) -> Settings:
    return Settings(
        dry_run=True, ado_organization=ROOT, ado_pat="ROOT-PAT",
        ado_project="DxFactory", database_url="sqlite+aiosqlite:///:memory:", **over,
    )


def _with_second_org() -> Settings:
    return _settings(workspaces=[WorkspaceConfig(
        name="Khatoco", ado_projects=["Khatoco"],
        ado_organization=OTHER, ado_pat="OTHER-PAT",
        workspace_directory="/tmp/khatoco",
    )])


# ── the scoped settings carry the connection ─────────────────────────────────


def test_a_workspace_without_its_own_org_inherits_the_machines():
    """Blank is the default and it must stay a true no-op: an install that has never
    heard of a second organization behaves exactly as before."""
    cfg = _settings(workspaces=[WorkspaceConfig(name="B", ado_projects=["Other"])])
    scoped = cfg.scoped_for_project("Other")
    assert scoped.ado_organization == ROOT
    assert scoped.ado_pat == "ROOT-PAT"


def test_a_workspace_with_its_own_org_overrides_both():
    scoped = _with_second_org().scoped_for_project("Khatoco")
    assert scoped.ado_organization == OTHER
    assert scoped.ado_pat == "OTHER-PAT"


def test_the_root_config_is_not_mutated_by_scoping():
    cfg = _with_second_org()
    cfg.scoped_for_project("Khatoco")
    assert cfg.ado_organization == ROOT and cfg.ado_pat == "ROOT-PAT"


# ── the container builds and routes a second client ──────────────────────────


def _container(cfg: Settings) -> Container:
    c = Container(cfg)
    c.build_providers()
    return c


def test_a_second_organization_gets_its_own_client():
    c = _container(_with_second_org())
    theirs = c.ado_for("Khatoco")
    assert isinstance(theirs, AdoClient)
    assert theirs is not c.ado
    assert theirs._base == OTHER                    # pointed at the other org


def test_projects_nobody_claimed_still_use_the_machines_connection():
    c = _container(_with_second_org())
    assert c.ado_for("DxFactory") is c.ado
    assert c.ado_for("") is c.ado
    assert c.ado_for("never-heard-of-it") is c.ado


def test_no_second_client_is_built_when_no_workspace_asks_for_one():
    """The common install must allocate nothing and route nowhere new."""
    c = _container(_settings(workspaces=[WorkspaceConfig(name="B", ado_projects=["Other"])]))
    assert c.providers == {}
    assert c.ado_for("Other") is c.ado


def test_ado_for_never_hands_back_a_jira_client():
    """``provider_for`` may return Jira, which has no repositories and no builds —
    so the ADO-specific call sites need a lookup that cannot return one."""
    cfg = _settings(workspaces=[WorkspaceConfig(
        name="J", ado_projects=["JiraProj"], provider="jira",
        jira_url="https://acme.atlassian.net", jira_project="JP",
    )])
    c = _container(cfg)
    assert not isinstance(c.provider_for("JiraProj"), AdoClient)   # tracker-agnostic
    assert c.ado_for("JiraProj") is c.ado                          # ADO-only calls


# ── writes land in the organization that owns the item ───────────────────────


def test_state_sync_writes_to_the_items_own_organization():
    """The failure this prevents is silent: a state update for an item in org B, sent
    to org A, lands on a same-named project there or 404s."""
    from ai_autopilot.services.state_sync import StateSyncService

    c = _container(_with_second_org())
    svc = StateSyncService.__new__(StateSyncService)
    svc._c, svc._config = c, c.config

    theirs = svc._ado(SimpleNamespace(project="Khatoco"))
    ours = svc._ado(SimpleNamespace(project="DxFactory"))

    assert theirs._base == OTHER
    assert ours is c.ado
    assert svc._ado(None) is c.ado                 # no project known → the machine's
    assert svc._ado("Khatoco") is theirs           # a bare project name works too


def test_a_container_without_the_helper_still_answers():
    """``_ado`` is reached through getattr so a stand-in container in another test
    does not have to grow the method."""
    from ai_autopilot.services.state_sync import StateSyncService

    svc = StateSyncService.__new__(StateSyncService)
    sentinel = object()
    svc._c = SimpleNamespace(ado=sentinel)
    assert svc._ado(SimpleNamespace(project="anything")) is sentinel


# ── the boundary has to be said out loud ─────────────────────────────────────


def test_doctor_names_what_does_not_follow_the_item_to_the_other_org():
    """The failure is quiet: the babysitter finds no pull requests in an org it never
    looks at, which reads exactly like "nobody opened one"."""
    from ai_autopilot.doctor import check_second_organization

    from ai_autopilot.doctor import ERROR

    findings = check_second_organization(_with_second_org())
    assert any("Pull requests" in f.detail for f in findings)
    assert all(f.level != ERROR for f in findings)        # configured correctly


def test_doctor_calls_out_an_organization_with_no_credential():
    from ai_autopilot.doctor import check_second_organization

    cfg = _settings(workspaces=[WorkspaceConfig(
        name="Khatoco", ado_projects=["Khatoco"], ado_organization=OTHER,
    )])
    from ai_autopilot.doctor import ERROR

    findings = check_second_organization(cfg)
    assert any(f.level == ERROR and "no PAT" in f.title for f in findings)


def test_doctor_is_silent_when_every_workspace_shares_the_connection():
    from ai_autopilot.doctor import check_second_organization

    assert check_second_organization(_settings()) == []


# ── the PAT must survive a save that never rendered it ───────────────────────


def test_saving_the_page_does_not_erase_a_stored_pat():
    """The page never renders a PAT, so the form cannot send one back — and the save
    rewrites `workspaces` wholesale. Without this, saving for any reason at all wipes
    the credential and the only symptom is polls that quietly return nothing."""
    from ai_autopilot import workspaces as ws_mod

    cfg = _with_second_org()
    views = ws_mod.resolve(cfg)
    updates = ws_mod.carry_secrets(ws_mod.to_settings_updates(views), cfg)
    saved = next(w for w in updates["workspaces"] if w["name"] == "Khatoco")
    assert saved["ado_pat"] == "OTHER-PAT"
    assert saved["ado_organization"] == OTHER


def test_a_newly_typed_pat_wins_over_the_stored_one():
    from ai_autopilot import workspaces as ws_mod

    cfg = _with_second_org()
    views = ws_mod.resolve(cfg)
    target = next(v for v in views if not v.is_default)
    target.ado_pat = "TYPED-JUST-NOW"
    updates = ws_mod.carry_secrets(ws_mod.to_settings_updates(views), cfg)
    saved = next(w for w in updates["workspaces"] if w["name"] == "Khatoco")
    assert saved["ado_pat"] == "TYPED-JUST-NOW"


def test_the_page_never_puts_a_stored_pat_into_the_view():
    from ai_autopilot import workspaces as ws_mod

    target = next(v for v in ws_mod.resolve(_with_second_org()) if not v.is_default)
    assert target.ado_pat == ""          # nothing to leak into the HTML
    assert target.ado_pat_set is True    # but the page can say one exists
