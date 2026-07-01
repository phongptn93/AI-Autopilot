"""Tests for AdoClient URL scoping (work-item vs code project)."""

from __future__ import annotations

from ai_autopilot.ado.client import AdoClient
from ai_autopilot.config import Settings


def _client(**over) -> AdoClient:
    cfg = Settings(ado_organization="https://dev.azure.com/org", **over)
    return AdoClient(http=None, auth=None, config=cfg)  # URL helpers don't touch http/auth


def test_git_url_falls_back_to_ado_project():
    c = _client(ado_project="WorkItems")
    assert c._git_url("git/repositories") == "https://dev.azure.com/org/WorkItems/_apis/git/repositories"


def test_git_url_uses_code_project_when_set():
    c = _client(ado_project="WorkItems", code_project="DxFactory")
    # git/PR/build scoped to the code project…
    assert c._git_url("git/repositories") == "https://dev.azure.com/org/DxFactory/_apis/git/repositories"
    # …while work-item URLs stay on the work-item project
    assert c._url("wit/workitems/5") == "https://dev.azure.com/org/WorkItems/_apis/wit/workitems/5"
