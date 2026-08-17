"""Multi-project + multi-workspace: routing a work item to its project and folder.

One autopilot connection (one org, one PAT) can poll several work-item projects, and
each project can run in its own workspace folder. The failure these tests guard is
silent in production: a run executes in the WRONG repository, or a comment is posted
against a project that does not contain the item, and nothing raises.
"""

from __future__ import annotations

import asyncio

import pytest

from ai_autopilot.ado.client import AdoClient
from ai_autopilot.config import Settings, format_workspace_line, parse_workspace_line
from ai_autopilot.models import WorkItemInfo

ORG = "https://dev.azure.com/org"


def _cfg(**over) -> Settings:
    return Settings(ado_organization=ORG, **over)


def _client(**over) -> AdoClient:
    # The URL/WIQL helpers under test never touch http/auth.
    return AdoClient(http=None, auth=None, config=_cfg(**over))


# ── config: which projects, which workspace ──────────────────────────────────


def test_single_project_config_is_unchanged():
    cfg = _cfg(ado_project="Khatoco")
    assert cfg.effective_ado_projects == ["Khatoco"]
    assert cfg.effective_workspaces == []
    # No workspace of its own → the SAME object, so nothing is copied per run.
    assert cfg.scoped_for_project("Khatoco") is cfg


def test_extra_projects_are_polled_default_first():
    cfg = _cfg(ado_project="Khatoco", ado_projects=["CMMS", "IIoT"])
    assert cfg.effective_ado_projects == ["Khatoco", "CMMS", "IIoT"]


def test_projects_are_deduplicated_case_insensitively():
    cfg = _cfg(
        ado_project="Khatoco",
        ado_projects=["khatoco", "CMMS"],
        workspace_map=["CMMS = C:/ws/cmms"],
    )
    assert cfg.effective_ado_projects == ["Khatoco", "CMMS"]


def test_workspace_line_parses_projects_folder_and_overrides():
    ws = parse_workspace_line("CMMS, IIoT = C:/ws/cmms | base=main | code=CmmsCode | repos=Api,Web")
    assert ws is not None
    assert ws.ado_projects == ["CMMS", "IIoT"]
    assert ws.workspace_directory == "C:/ws/cmms"
    assert ws.base_branch == "main"
    assert ws.code_project == "CmmsCode"
    assert ws.allowed_repos == ["Api", "Web"]


@pytest.mark.parametrize("line", ["", "   ", "# a comment", "no-equals-sign", "= C:/ws"])
def test_unusable_workspace_lines_are_dropped_not_guessed(line):
    # A half-parsed line would route work items into the wrong repo, silently.
    assert parse_workspace_line(line) is None


def test_workspace_line_round_trips():
    line = "CMMS = C:/ws/cmms | base=main | code=CmmsCode"
    ws = parse_workspace_line(line)
    assert parse_workspace_line(format_workspace_line(ws)) == ws


def test_scoped_settings_apply_workspace_overrides():
    cfg = _cfg(
        ado_project="Khatoco",
        workspace_directory="C:/ws/khatoco",
        base_branch="development",
        workspace_map=["CMMS = C:/ws/cmms | base=main"],
    )
    scoped = cfg.scoped_for_project("CMMS")
    assert scoped.ado_project == "CMMS"
    assert scoped.workspace_directory == "C:/ws/cmms"
    assert scoped.base_branch == "main"
    # The root config is untouched — scoping is a per-run view, not a mutation.
    assert cfg.workspace_directory == "C:/ws/khatoco"
    assert cfg.base_branch == "development"


def test_blank_override_inherits_rather_than_blanking():
    cfg = _cfg(
        ado_project="Khatoco",
        base_branch="development",
        allowed_repos=["Backend"],
        workspace_map=["CMMS = C:/ws/cmms"],   # no base=, no repos=
    )
    scoped = cfg.scoped_for_project("CMMS")
    assert scoped.base_branch == "development"
    assert scoped.allowed_repos == ["Backend"]


def test_project_without_workspace_keeps_the_root_workspace():
    cfg = _cfg(
        ado_project="Khatoco",
        ado_projects=["IIoT"],
        workspace_directory="C:/ws/khatoco",
    )
    scoped = cfg.scoped_for_project("IIoT")
    assert scoped.workspace_directory == "C:/ws/khatoco"
    assert scoped.ado_project == "IIoT"        # …but the item's own project is respected


def test_structured_workspace_wins_over_a_one_liner_for_the_same_project():
    cfg = _cfg(
        ado_project="Khatoco",
        workspaces=[{"ado_projects": ["CMMS"], "workspace_directory": "C:/structured"}],
        workspace_map=["CMMS = C:/from-the-line"],
    )
    assert cfg.workspace_for("CMMS").workspace_directory == "C:/structured"


def test_workspace_trigger_tag_reaches_the_poll_query():
    # An override that never reached the WIQL would disable the workspace it configures.
    cfg = _cfg(ado_project="Khatoco", trigger_tag="host-autopilot",
               workspace_map=["CMMS = C:/ws/cmms | tag=cmms-autopilot"])
    assert cfg.effective_trigger_tags == ["host-autopilot", "cmms-autopilot"]


def test_code_project_falls_back_to_the_items_own_project():
    # With several work-item projects, one global code_project would send every
    # project's PRs to the same repo host.
    cfg = _cfg(ado_project="Khatoco", ado_projects=["CMMS"])
    assert cfg.code_project_for("CMMS") == "CMMS"
    assert cfg.code_project_for("") == "Khatoco"


def test_workspace_code_project_beats_the_global_one():
    cfg = _cfg(ado_project="Khatoco", code_project="GlobalCode",
               workspace_map=["CMMS = C:/ws/cmms | code=CmmsCode"])
    assert cfg.code_project_for("CMMS") == "CmmsCode"
    assert cfg.code_project_for("Khatoco") == "GlobalCode"


# ── ADO client: one query for every project, right URL per item ──────────────


def test_project_clause_uses_equality_for_one_project():
    assert _client(ado_project="Khatoco")._project_clause() == (
        "[System.TeamProject] = 'Khatoco'"
    )


def test_project_clause_uses_in_for_several_projects():
    clause = _client(ado_project="Khatoco", ado_projects=["CMMS"])._project_clause()
    assert clause == "[System.TeamProject] IN ('Khatoco', 'CMMS')"


def test_project_clause_escapes_quotes():
    clause = _client(ado_project="O'Brien", ado_projects=["B"])._project_clause()
    assert "'O''Brien'" in clause


def test_project_clause_with_nothing_configured_matches_nothing():
    # Must stay a valid predicate: an empty string would splice a dangling AND into
    # every caller's WIQL and fail the whole query instead of returning no rows.
    assert _client()._project_clause() == "[System.TeamProject] = ''"


def test_reads_and_updates_are_organization_scoped():
    c = _client(ado_project="Khatoco", ado_projects=["CMMS"])
    assert c._org_url("wit/workitems/5") == f"{ORG}/_apis/wit/workitems/5"


def test_project_scoped_url_uses_the_given_project():
    c = _client(ado_project="Khatoco")
    assert c._url("wit/comments", "CMMS") == f"{ORG}/CMMS/_apis/wit/comments"
    assert c._url("wit/comments") == f"{ORG}/Khatoco/_apis/wit/comments"


def test_git_url_follows_the_work_items_project():
    c = _client(ado_project="Khatoco", ado_projects=["CMMS"])
    assert c._git_url("git/repositories", "CMMS") == f"{ORG}/CMMS/_apis/git/repositories"


def test_mapping_records_the_items_project():
    item = AdoClient._map({"id": 7, "fields": {"System.TeamProject": "CMMS",
                                               "System.Title": "t"}})
    assert item.project == "CMMS"


def test_project_memo_answers_without_a_lookup():
    c = _client(ado_project="Khatoco", ado_projects=["CMMS"])
    c._remember_projects([WorkItemInfo(id=7, project="CMMS")])
    assert asyncio.run(c._project_for(7)) == "CMMS"


def test_project_lookup_is_skipped_when_only_one_project_is_configured():
    # No memo entry and no HTTP client — proving nothing is fetched.
    c = _client(ado_project="Khatoco")
    assert asyncio.run(c._project_for(999)) == "Khatoco"


def test_project_memo_is_bounded():
    from ai_autopilot.ado.client import _MAX_PROJECT_MEMO

    c = _client(ado_project="Khatoco", ado_projects=["CMMS"])
    c._remember_projects(
        [WorkItemInfo(id=i, project="CMMS") for i in range(_MAX_PROJECT_MEMO + 50)]
    )
    assert len(c._item_projects) == _MAX_PROJECT_MEMO
    assert 0 not in c._item_projects          # oldest dropped…
    assert _MAX_PROJECT_MEMO + 49 in c._item_projects   # …newest kept
