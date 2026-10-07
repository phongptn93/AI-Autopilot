"""Plan first: big items post a plan, wait for approval, then build following it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ai_autopilot.data import PipelineState
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from tests.test_executor_agent import _executor
from tests.test_executor_agent import _item as _exec_item
from tests.test_poller_agent import _poller


def _wi(*tags: str, item_id: int = 7) -> WorkItemInfo:
    return WorkItemInfo(id=item_id, title="Big one", work_item_type="Requirement",
                        tags=list(tags))


class _Exec:
    def __init__(self, result: ExecutionResult):
        self.calls: list[dict] = []
        self._result = result

    async def run_agent(self, item, **kw):
        self.calls.append(kw)
        return self._result

    async def dispatch_interactive(self, item, **kw):
        self.calls.append(kw)
        return True, "autopilot-7", "/ws/scratch"


async def _noop(*_a, **_k):
    return None


async def _headless(svc, c, item):
    c.router = SimpleNamespace(classify=lambda i: i)
    c.plugins = SimpleNamespace(run_post_processors=_noop)
    await svc._process_agent(item, item)


# ── which items qualify ───────────────────────────────────────────────────────


def test_plan_mode_follows_the_tags():
    svc, _c = _poller()
    assert svc._plan_mode(_wi("autopilot")) == ""
    assert svc._plan_mode(_wi("Plan-First")) == "plan"                    # case-insensitive
    assert svc._plan_mode(_wi("plan-first", "plan-approved")) == "approved"


def test_points_qualify_only_when_a_floor_is_set_and_the_tracker_reports_them():
    svc, _c = _poller(plan_first_min_points=8)
    big = _wi()
    big.story_points = 13          # not on WorkItemInfo today; read if a provider adds it
    assert svc._plan_mode(big) == "plan"
    assert svc._plan_mode(_wi()) == ""                                   # no points → tag only
    svc_off, _ = _poller()
    assert svc_off._plan_mode(big) == ""                                 # 0 = off


# ── the plan run ──────────────────────────────────────────────────────────────


async def test_a_plan_first_item_runs_plan_only_then_waits():
    svc, c = _poller(execution_mode="headless")
    c.executor = _Exec(ExecutionResult.ok(7, "agent", "plan posted"))
    item = _wi("autopilot", "plan-first")

    await _headless(svc, c, item)

    [call] = c.executor.calls
    assert call["autonomy"] == "report"
    assert "PLAN-ONLY" in call["brief_note"] and "plan-approved" in call["brief_note"]
    assert (7, "plan-pending") in c.ado.tags
    assert (7, c.config.escalation_tag) in c.ado.tags                    # held, not re-picked
    assert (7, PipelineState.NEEDS_HUMAN) in c.state_repo.calls
    assert any("chờ duyệt" in t for _i, t in c.ado.comments)


async def test_the_sdlc_engine_is_bypassed_for_the_plan():
    svc, c = _poller(execution_mode="headless", sdlc_loop_enabled=True)
    c.executor = _Exec(ExecutionResult.ok(7, "agent", "plan posted"))
    await _headless(svc, c, _wi("plan-first"))
    assert c.executor.calls and c.executor.calls[0]["autonomy"] == "report"


async def test_the_approved_run_builds_and_is_told_to_follow_the_plan():
    svc, c = _poller(execution_mode="headless")
    built = ExecutionResult.ok(7, "agent", "built")
    built.pr_url = "https://dev.azure.com/o/P/_git/Api/pullrequest/3"
    built.files_changed = ["Api/Orders.cs"]
    c.executor = _Exec(built)
    item = _wi("plan-first", "plan-approved", "plan-pending")

    await _headless(svc, c, item)

    [call] = c.executor.calls
    assert call["autonomy"] == "assisted"
    assert "Approved plan" in call["brief_note"]
    assert (7, "plan-pending") in c.ado.removed                          # cleared on start
    assert (7, "plan-pending") not in c.ado.tags
    assert (7, PipelineState.NEEDS_HUMAN) not in c.state_repo.calls


async def test_an_interactive_plan_session_opens_no_pr_and_runs_no_stages():
    svc, c = _poller()                                                  # interactive default
    c.executor = _Exec(ExecutionResult.ok(7, "agent", "x"))
    c.router = SimpleNamespace(classify=lambda i: i)
    await svc._process_agent(_wi("plan-first"), _wi("plan-first"))
    [call] = c.executor.calls
    assert call["opens_pr"] is False and call["stages"] is None
    assert call["autonomy"] == "report" and "PLAN-ONLY" in call["brief_note"]
    assert any("Chế độ lập kế hoạch" in t for _i, t in c.ado.comments)


async def test_an_interactive_plan_finishing_after_a_restart_is_still_held():
    """No in-memory record of the dispatch: the tags alone say it was a plan."""
    svc, c = _poller()
    item = _wi("plan-first")
    result = ExecutionResult.ok(7, "interactive", "plan posted")
    await svc._handle_agent_result(item, result)
    assert result.needs_human and (7, "plan-pending") in c.ado.tags


# ── approval ──────────────────────────────────────────────────────────────────


async def test_adding_the_approved_tag_releases_the_held_plan():
    svc, c = _poller()
    hold = c.config.escalation_tag
    item = _wi("autopilot", "plan-first", "plan-pending", hold, "plan-approved", item_id=21)
    c.ado.tagged_items = [item, _wi("plan-approved", item_id=22)]    # 22: never planned
    started: list[int] = []

    async def fake_process(it):
        started.append(it.id)

    svc._process = fake_process
    await svc._reconcile_plan_approvals()
    await asyncio.sleep(0)

    assert started == [21]
    assert (21, "plan-pending") in c.ado.removed and (21, hold) in c.ado.removed
    assert c.ado.tagged_any_queries == [["plan-approved"]]
    assert any("đã được duyệt" in t for i, t in c.ado.comments if i == 21)


# ── the brief ─────────────────────────────────────────────────────────────────


def test_the_brief_carries_the_note_and_honours_a_non_draft_assisted_run():
    ex = _executor()
    brief = ex._build_brief(_exec_item(), ["Api"], autonomy="assisted", draft_pr=False,
                            brief_note="## NOTE FROM CONTROL PLANE")
    assert "## NOTE FROM CONTROL PLANE" in brief
    assert "NOT draft" in brief
    draft = ex._build_brief(_exec_item(), ["Api"], autonomy="assisted", draft_pr=True)
    assert "DRAFT PR" in draft and "NOTE FROM" not in draft


# ── who posts the plan (#9448) ────────────────────────────────────────────────


async def test_the_control_plane_posts_the_plan_the_agent_returned():
    """#9448: the agent was told to post the plan itself, had no ADO tool and a git
    credential without Work Items scope (401), put the plan on an unrelated PR and
    escalated. The plan now comes back in the result file and the autopilot posts it
    with its own PAT, before the "waiting for approval" note."""
    svc, c = _poller(execution_mode="headless")
    result = ExecutionResult.ok(7, "agent", "plan written")
    result.plan = "## Scope\n- **in**: báo cáo tồn kho\n\n## Risks\n- <script>x</script>"
    c.executor = _Exec(result)

    await _headless(svc, c, _wi("autopilot", "plan-first"))

    texts = [t for _i, t in c.ado.comments]
    plan_at = next(i for i, t in enumerate(texts) if "Kế hoạch triển khai</b>" in t)
    held_at = next(i for i, t in enumerate(texts) if "chờ duyệt" in t)
    assert plan_at < held_at
    assert "<strong>in</strong>" in texts[plan_at]                      # Markdown rendered
    assert "<script>" not in texts[plan_at]                             # and escaped
    assert (7, "plan-pending") in c.ado.tags


def test_the_brief_no_longer_asks_the_agent_to_post_anything():
    svc, _c = _poller()
    plan_only, _follow = svc._plan_notes()
    assert "`plan` field" in plan_only and "Do NOT post it yourself" in plan_only
    assert "Post ONE comment" not in plan_only


def test_the_result_file_carries_the_plan(tmp_path):
    import json

    from ai_autopilot.execution.result_contract import read_result

    runs = tmp_path / ".autopilot" / "runs"
    runs.mkdir(parents=True)
    (runs / "7.json").write_text(json.dumps(
        {"status": "completed", "summary": "s", "artifacts": [], "plan": "## Scope\n- x"}),
        encoding="utf-8")
    assert read_result(str(tmp_path), 7).plan == "## Scope\n- x"
