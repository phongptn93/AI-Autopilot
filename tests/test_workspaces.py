"""Workspaces: resolution, validation, and the view scoping they drive.

The failure this guards is quiet in both directions. Get resolution wrong and a
project builds in the wrong folder — the run succeeds, against the wrong repos. Get
scoping wrong and the dashboard either hides work that exists or shows another
workspace's work as this one's, and either way the operator acts on a false picture.
"""

from __future__ import annotations

from ai_autopilot import workspaces
from ai_autopilot.config import Settings


def _cfg(**over) -> Settings:
    base = dict(ado_organization="https://dev.azure.com/o", ado_project="Khatoco")
    base.update(over)
    return Settings(**base)


def _form(rows: list[dict]) -> dict:
    """Build the flat ``ws{i}_*`` payload the page posts."""
    out: dict = {"ws_count": str(len(rows))}
    for index, row in enumerate(rows):
        for key, value in row.items():
            out[f"ws{index}_{key}"] = value
    return out


# ── resolution ───────────────────────────────────────────────────────────────


def test_default_workspace_comes_first_and_is_marked():
    views = workspaces.resolve(_cfg(workspace_directory="C:/ws", base_branch="development"))
    assert len(views) == 1
    assert views[0].is_default and views[0].id == workspaces.DEFAULT_ID
    assert views[0].directory == "C:/ws"
    assert views[0].base_branch == "development"


def test_configured_workspaces_follow_the_default():
    cfg = _cfg(workspaces=[
        {"name": "CMMS", "ado_projects": ["CMMS"], "workspace_directory": "D:/cmms",
         "base_branch": "main"},
    ])
    views = workspaces.resolve(cfg)
    assert [v.id for v in views] == ["default", "cmms"]
    assert views[1].base_branch == "main"


def test_each_workspace_keeps_its_own_base_branch():
    cfg = _cfg(
        base_branch="development",
        workspaces=[{"name": "A", "ado_projects": ["A"], "workspace_directory": "D:/a",
                     "base_branch": "main"}],
    )
    assert [v.base_branch for v in workspaces.resolve(cfg)] == ["development", "main"]


def test_a_workspace_can_hold_several_projects():
    cfg = _cfg(workspaces=[
        {"name": "Nhà máy 2", "ado_projects": ["CMMS", "IIoT"], "workspace_directory": "D:/x"},
    ])
    ws = workspaces.resolve(cfg)[1]
    assert ws.projects == ["CMMS", "IIoT"]
    assert ws.serves("cmms") and ws.serves("IIoT") and not ws.serves("Khatoco")


def test_the_default_does_not_also_claim_a_project_another_workspace_owns():
    # Otherwise the same items appear under two workspaces in the selector.
    cfg = _cfg(
        ado_projects=["CMMS"],
        workspaces=[{"name": "CMMS", "ado_projects": ["CMMS"], "workspace_directory": "D:/c"}],
    )
    default, cmms = workspaces.resolve(cfg)
    assert default.projects == ["Khatoco"]
    assert cmms.projects == ["CMMS"]


def test_ids_fold_vietnamese_accents_and_stay_unique():
    cfg = _cfg(workspaces=[
        {"name": "Nhà máy", "ado_projects": ["A"], "workspace_directory": "D:/a"},
        {"name": "Nhà máy", "ado_projects": ["B"], "workspace_directory": "D:/b"},
    ])
    ids = [v.id for v in workspaces.resolve(cfg)]
    assert ids == ["default", "nha-may", "nha-may-2"]


def test_legacy_one_liners_still_resolve():
    cfg = _cfg(workspace_map=["Sensor = E:/sensor | base=main"])
    views = workspaces.resolve(cfg)
    assert views[1].projects == ["Sensor"] and views[1].base_branch == "main"


def test_a_structured_workspace_beats_a_legacy_line_for_the_same_project():
    cfg = _cfg(
        workspaces=[{"name": "Real", "ado_projects": ["CMMS"], "workspace_directory": "D:/real"}],
        workspace_map=["CMMS = E:/stale"],
    )
    assert [v.directory for v in workspaces.resolve(cfg)] == ["", "D:/real"]


# ── scoping ──────────────────────────────────────────────────────────────────


def test_no_selection_means_every_project():
    assert workspaces.scope_projects(_cfg(), "all") is None
    assert workspaces.scope_projects(_cfg(), "") is None


def test_an_unknown_workspace_falls_back_to_everything():
    assert workspaces.scope_projects(_cfg(), "ghost") is None


def test_selecting_a_workspace_returns_only_its_projects():
    cfg = _cfg(workspaces=[
        {"name": "CMMS", "ado_projects": ["CMMS", "IIoT"], "workspace_directory": "D:/c"},
    ])
    assert workspaces.scope_projects(cfg, "cmms") == ["CMMS", "IIoT"]


def test_an_empty_workspace_scopes_to_nothing_not_to_everything():
    # [] and None are opposites here: an unconfigured workspace must look empty, not busy.
    cfg = _cfg(workspaces=[{"name": "Trống", "ado_projects": [], "workspace_directory": "D:/t"}])
    assert workspaces.scope_projects(cfg, "trong") == []


# ── form parsing & validation ────────────────────────────────────────────────


def test_parse_round_trips_a_default_and_a_second_workspace():
    views, errors = workspaces.parse_form(_form([
        {"is_default": "1", "name": "Chính", "projects": "Khatoco\nLegacy",
         "directory": "C:/ws", "base_branch": "development"},
        {"is_default": "0", "name": "CMMS", "projects": "CMMS", "directory": "D:/c",
         "base_branch": "main", "enabled": "on"},
    ]))
    assert errors == []
    updates = workspaces.to_settings_updates(views)
    assert updates["ado_project"] == "Khatoco"        # first project stays the default
    assert updates["ado_projects"] == ["Legacy"]
    assert updates["workspace_directory"] == "C:/ws"
    assert updates["workspaces"][0]["base_branch"] == "main"
    assert updates["workspace_map"] == []             # migrated, not left to resurrect


def test_saved_config_resolves_back_to_what_was_entered():
    views, _ = workspaces.parse_form(_form([
        {"is_default": "1", "name": "Chính", "projects": "Khatoco", "directory": "C:/ws"},
        {"is_default": "0", "name": "CMMS", "projects": "CMMS", "directory": "D:/c",
         "base_branch": "main", "enabled": "on"},
    ]))
    cfg = _cfg(**workspaces.to_settings_updates(views))
    assert [(v.label, v.directory) for v in workspaces.resolve(cfg)] == [
        ("Chính", "C:/ws"), ("CMMS", "D:/c"),
    ]


def test_a_project_claimed_twice_is_rejected():
    # Whichever workspace won would be arbitrary, and the run would look successful.
    _, errors = workspaces.parse_form(_form([
        {"is_default": "1", "name": "Chính", "projects": "Khatoco", "directory": "C:/ws"},
        {"is_default": "0", "name": "A", "projects": "Khatoco", "directory": "D:/a"},
    ]))
    assert any("Khatoco" in e and "một workspace" in e for e in errors)


def test_a_workspace_with_no_project_is_rejected():
    _, errors = workspaces.parse_form(_form([
        {"is_default": "1", "name": "Chính", "projects": "Khatoco", "directory": "C:/ws"},
        {"is_default": "0", "name": "Rỗng", "directory": "D:/x"},
    ]))
    assert any("chưa gán ADO project" in e for e in errors)


def test_a_workspace_with_no_folder_is_rejected():
    _, errors = workspaces.parse_form(_form([
        {"is_default": "1", "name": "Chính", "projects": "Khatoco", "directory": "C:/ws"},
        {"is_default": "0", "name": "A", "projects": "CMMS"},
    ]))
    assert any("chưa có thư mục" in e for e in errors)


def test_an_untouched_blank_row_is_ignored_not_reported():
    views, errors = workspaces.parse_form(_form([
        {"is_default": "1", "name": "Chính", "projects": "Khatoco", "directory": "C:/ws"},
        {"is_default": "0"},
    ]))
    assert errors == [] and len(views) == 1


def test_deleting_a_row_drops_it():
    views, errors = workspaces.parse_form(_form([
        {"is_default": "1", "name": "Chính", "projects": "Khatoco", "directory": "C:/ws"},
        {"is_default": "0", "name": "Cũ", "projects": "CMMS", "directory": "D:/c",
         "delete": "on"},
    ]))
    assert errors == [] and [v.label for v in views] == ["Chính"]


def test_the_default_cannot_be_deleted():
    # It has no delete box on the page; a hand-crafted POST must not remove it either.
    views, errors = workspaces.parse_form(_form([
        {"is_default": "1", "name": "Chính", "projects": "Khatoco", "directory": "C:/ws",
         "delete": "on"},
    ]))
    assert errors == [] and views[0].is_default


def test_a_post_without_the_default_is_refused():
    _, errors = workspaces.parse_form(_form([
        {"is_default": "0", "name": "A", "projects": "CMMS", "directory": "D:/c"},
    ]))
    assert any("mặc định" in e for e in errors)
