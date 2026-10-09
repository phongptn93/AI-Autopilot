"""`<stage_entry_tag>:<role>` — one run-now convention per role, no configuration.

Before this, forcing a role by tag needed that role's own ``entry_tag`` typed on the
Roles page; the shared tag only ever ran the role the item's STATE named. So "run QC on
this now" on an item sitting in a state no role waits in was impossible by tag alone.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ai_autopilot.config import SdlcRole, Settings
from ai_autopilot.execution.sdlc_plan import (
    resolve_profile_name,
    run_now_role_tag,
    run_now_tags,
)
from ai_autopilot.models import WorkItemInfo
from ai_autopilot.services.poller import AdoPollerService
from tests.test_poller_agent import _FakeAdo
from tests.test_run_now_lease import _SharedAdo

_ROLES = {
    "dev": SdlcRole(stages=["implement"], waits_in="Ready for Development"),
    "qc": SdlcRole(stages=["test"], waits_in="Ready for Testing"),
    "ba": SdlcRole(stages=["analyze"], entry_tag="BA-Now"),
}


class _NullState:
    async def set(self, *_a, **_kw):
        return None


def _svc(cfg: Settings, ado) -> tuple[AdoPollerService, list[int]]:
    svc = AdoPollerService.__new__(AdoPollerService)
    svc._config = cfg
    svc._c = SimpleNamespace(ado=ado, state_repo=_NullState())
    svc._live, svc._processed = {}, {}
    quiet = lambda *a, **k: None  # noqa: E731
    svc._log = SimpleNamespace(info=quiet, warning=quiet, debug=quiet, error=quiet)
    started: list[int] = []

    async def fake_process(item):
        started.append(item.id)

    svc._process = fake_process
    return svc, started


async def _sweep(svc) -> None:
    await svc._reconcile_stage_entries()
    await asyncio.sleep(0)     # the dispatch is a create_task; let it start


# ── the helper ───────────────────────────────────────────────────────────────


def test_helper_lists_the_shared_tag_one_per_role_and_explicit_tags():
    cfg = Settings(stage_entry_tag="Autopilot-Run", sdlc_roles=_ROLES)
    assert run_now_tags(cfg) == {
        "autopilot-run": None,            # shared: the state picks the role
        "autopilot-run:ba": "ba",
        "autopilot-run:dev": "dev",
        "autopilot-run:qc": "qc",
        "ba-now": "ba",                   # the explicit entry_tag still works
    }
    assert run_now_role_tag("qc", cfg) == "Autopilot-Run:qc"


def test_every_derived_role_gets_the_convention_without_a_roles_page():
    """An install that never saved the Roles page still has roles (derived from the
    built-in profiles), and every one of them must be reachable by tag."""
    cfg = Settings(stage_entry_tag="autopilot-run", sdlc_roles={})
    tags = run_now_tags(cfg)
    for role in ("full", "dev", "ba", "qc", "review", "design"):
        assert tags[f"autopilot-run:{role}"] == role


def test_no_shared_tag_means_no_convention_but_explicit_tags_survive():
    cfg = Settings(stage_entry_tag="", sdlc_roles=_ROLES)
    assert run_now_tags(cfg) == {"ba-now": "ba"}


def test_a_derived_tag_never_lands_on_a_trigger_tag_or_the_pin_namespace():
    """Both clashes are destructive (the sweep removes what it matched), and a derived tag
    is never typed, so the settings form cannot refuse it — the helper must."""
    cfg = Settings(stage_entry_tag="sdlc", trigger_tags=["sdlc:dev"], sdlc_roles=_ROLES)
    tags = run_now_tags(cfg)
    assert not any(t.startswith("sdlc:") for t in tags)
    assert tags == {"sdlc": None, "ba-now": "ba"}


def test_an_explicit_entry_tag_beats_a_derived_one():
    roles = {**_ROLES, "dev": SdlcRole(stages=["implement"], entry_tag="autopilot-run:qc")}
    cfg = Settings(stage_entry_tag="autopilot-run", sdlc_roles=roles)
    assert run_now_tags(cfg)["autopilot-run:qc"] == "dev"


# ── the sweep ────────────────────────────────────────────────────────────────


async def test_a_role_tag_runs_that_role_from_a_state_nobody_waits_in():
    cfg = Settings(stage_entry_tag="autopilot-run", sdlc_default_profile="full",
                   sdlc_roles=_ROLES)
    ado = _FakeAdo()
    item = WorkItemInfo(id=7, title="t", work_item_type="Requirement",
                        state="Ready for UAT", tags=["Autopilot-Run:QC", "TLLA"])
    ado.tagged_items = [item]
    svc, started = _svc(cfg, ado)

    await _sweep(svc)

    assert started == [7]
    # Each tag is its own WIQL clause: CONTAINS matches whole tags only.
    assert "autopilot-run:qc" in ado.tagged_any_queries[0]
    assert (7, "Autopilot-Run:QC") in ado.removed           # consumed, original casing
    assert (7, "sdlc:qc") in ado.tags                       # the role is pinned
    assert resolve_profile_name(item.tags, "Requirement", cfg, state="Ready for UAT") == "qc"


async def test_a_role_tag_outranks_the_shared_tag_on_the_same_item():
    cfg = Settings(stage_entry_tag="autopilot-run", sdlc_roles=_ROLES)
    ado = _FakeAdo()
    ado.tagged_items = [WorkItemInfo(id=8, title="t", work_item_type="Task",
                                     state="Ready for Development",
                                     tags=["autopilot-run", "autopilot-run:qc"])]
    svc, started = _svc(cfg, ado)
    await _sweep(svc)
    assert started == [8]
    assert (8, "autopilot-run:qc") in ado.removed and (8, "sdlc:qc") in ado.tags


async def test_an_unknown_role_suffix_is_ignored():
    cfg = Settings(stage_entry_tag="autopilot-run", sdlc_roles=_ROLES)
    ado = _FakeAdo()
    ado.tagged_items = [WorkItemInfo(id=9, title="t", work_item_type="Task",
                                     state="Ready for UAT", tags=["autopilot-run:nobody"])]
    svc, started = _svc(cfg, ado)
    await _sweep(svc)
    assert "autopilot-run:nobody" not in ado.tagged_any_queries[0]
    assert started == [] and ado.removed == [] and ado.tags == []


async def test_an_explicit_entry_tag_still_forces_its_role():
    cfg = Settings(stage_entry_tag="autopilot-run", sdlc_roles=_ROLES)
    ado = _FakeAdo()
    ado.tagged_items = [WorkItemInfo(id=10, title="t", work_item_type="Task",
                                     state="Ready for UAT", tags=["ba-now"])]
    svc, started = _svc(cfg, ado)
    await _sweep(svc)
    assert started == [10]
    assert (10, "ba-now") in ado.removed and (10, "sdlc:ba") in ado.tags


async def test_a_role_tag_is_leased_in_a_fleet_but_an_explicit_one_is_not():
    """Every machine derives the same `<shared>:<role>` tags, so two can race for one
    item exactly as with the shared tag. An explicit entry_tag is per-machine config."""
    base = dict(stage_entry_tag="autopilot-run", fleet_role="worker",
                fleet_worker_name="vm-a", run_now_claim_settle_seconds=0, sdlc_roles=_ROLES)

    ado = _SharedAdo([])
    ado.tagged_items = [WorkItemInfo(id=11, title="t", work_item_type="Task",
                                     state="Ready for UAT", tags=["autopilot-run:qc"])]
    svc, started = _svc(Settings(**base), ado)
    await _sweep(svc)
    assert started == [11] and any("🔒" in text for _i, text in ado.comments)

    ado = _SharedAdo([])
    ado.tagged_items = [WorkItemInfo(id=12, title="t", work_item_type="Task",
                                     state="Ready for UAT", tags=["ba-now"])]
    svc, started = _svc(Settings(**base), ado)
    await _sweep(svc)
    assert started == [12] and ado.comments == []
