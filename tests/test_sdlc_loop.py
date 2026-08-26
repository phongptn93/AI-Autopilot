"""Integration tests for SdlcLoopEngine with in-memory fakes (no git / no SDK)."""

from __future__ import annotations

from types import SimpleNamespace

from ai_autopilot.config import SdlcStage, Settings
from ai_autopilot.execution.auto_reviewer import ReviewResult
from ai_autopilot.execution.sdlc_loop import SdlcLoopEngine
from ai_autopilot.execution.sdlc_plan import CATALOG, StageSignals
from ai_autopilot.models import TaskCategory, WorkItemInfo

_PR = "https://dev.azure.com/o/p/_git/r/pullrequest/123"


class FakeRun:
    """Stands in for a ClaudeRun. Carries the full usage shape, not just the total:
    the loop now accumulates the breakdown so History can say where the tokens went,
    and a double missing those fields would pass here while the real path failed."""

    def __init__(self, text="ok", is_error=False, tokens=10):
        self.text, self.is_error, self.total_tokens, self.cost_usd = text, is_error, tokens, None
        self.input_tokens, self.output_tokens = tokens, 0
        self.cache_read_tokens = self.cache_creation_tokens = 0
        self.models: dict[str, int] = {}


class FakeExecutor:
    def __init__(self, runs=None, commit_files=None):
        self._runs = list(runs or [])
        self._commit_files = commit_files if commit_files is not None else {"BE": ["a.py"]}
        self.calls: list[tuple[str, str]] = []
        self.acquired = self.released = 0

    def _allowed_repos(self, workspace):
        return ["Backend-Fresh"]

    async def _acquire_agent_scratch(self, item_id, repos):
        self.acquired += 1
        return "scratch"

    async def release_scratch(self, run_dir):
        self.released += 1

    async def prepare_stage_branch(self, scratch, repos, branch):
        self.calls.append(("branch", branch))

    async def stage_commit(self, scratch, repos, message):
        self.calls.append(("commit", message))
        return dict(self._commit_files)

    async def push_stage_branch(self, scratch, repos, branch):
        self.calls.append(("push", branch))

    async def _run_claude(self, prompt, run_dir, on_event=None):
        self.calls.append(("run", prompt))
        if on_event:                       # exercise the activity-stream callback
            on_event("… working")
        return self._runs.pop(0) if self._runs else FakeRun()

    def run_prompts(self):
        return [c[1] for c in self.calls if c[0] == "run"]


class FakeReviewer:
    def __init__(self, results=None):
        self._results = list(results or [])

    async def review(self, work_dir):
        return self._results.pop(0) if self._results else ReviewResult(passed=True)


class FakeAdo:
    def __init__(self):
        self.comments, self.states = [], []

    async def add_comment(self, work_item_id, html):
        self.comments.append((work_item_id, html))

    async def update_state(self, work_item_id, state):
        self.states.append((work_item_id, state))


class FakeRouter:
    def route(self, item):
        return f"/crud-full-stack {item.id}"


class FakeRepo:
    def __init__(self):
        self.store: dict[int, SimpleNamespace] = {}

    async def load(self, work_item_id):
        return self.store.get(work_item_id)

    async def save(self, work_item_id, *, profile, stage_index, iterations, branch, signals_json):
        self.store[work_item_id] = SimpleNamespace(
            work_item_id=work_item_id, profile=profile, stage_index=stage_index,
            iterations=iterations, branch=branch, signals_json=signals_json,
        )

    async def clear(self, work_item_id):
        self.store.pop(work_item_id, None)


def _engine(cfg, executor, reviewer, ado, repo):
    return SdlcLoopEngine(executor, reviewer, ado, FakeRouter(), cfg, repo)


def _item():
    return WorkItemInfo(
        id=1, title="Add spare part API", work_item_type="Task",
        category=TaskCategory.BACKEND_TASK,
    )


def _cfg(**over):
    return Settings(sdlc_loop_enabled=True, sdlc_profile="dev", dry_run=False, **over)


async def test_happy_path_runs_dev_profile_and_opens_pr():
    ex = FakeExecutor(runs=[FakeRun("implemented"), FakeRun(f"opened {_PR}")])
    ado, repo = FakeAdo(), FakeRepo()
    engine = _engine(_cfg(), ex, FakeReviewer([ReviewResult(passed=True)]), ado, repo)
    res = await engine.run(_item())

    assert res.success and not res.needs_human
    assert res.pr_url == _PR
    assert repo.store == {}                       # cleared on success
    assert ex.acquired == 1 and ex.released == 1  # scratch once
    assert len(ado.comments) == 3                 # a badge per dev stage (implement/review/pr)


async def test_review_failure_exhausts_budget_and_escalates():
    ex = FakeExecutor(runs=[FakeRun("implemented")])          # only implement uses _run_claude
    fails = [ReviewResult(passed=False, critical_issues=["SQLi"])] * 5
    ado, repo = FakeAdo(), FakeRepo()
    engine = _engine(_cfg(sdlc_max_iterations=2), ex, FakeReviewer(fails), ado, repo)
    res = await engine.run(_item())

    assert res.needs_human and not res.success
    assert repo.store and repo.store[1].iterations == 2       # escalate state persisted
    assert ex.released == 1


async def test_resume_skips_completed_stages():
    # Persisted at stage_index=1 (review) → implement must NOT re-run.
    repo = FakeRepo()
    repo.store[1] = SimpleNamespace(
        work_item_id=1, profile="dev", stage_index=1, iterations=0,
        branch="feature/1-x", signals_json=StageSignals(files_changed=1).to_json(),
    )
    ex = FakeExecutor(runs=[FakeRun(f"opened {_PR}")])   # only the pr stage runs a skill
    engine = _engine(_cfg(), ex, FakeReviewer([ReviewResult(passed=True)]), FakeAdo(), repo)
    res = await engine.run(_item())

    assert res.success and res.pr_url == _PR
    # Resumed at review → implement was skipped; only the pr stage runs a skill.
    assert len(ex.run_prompts()) == 1
    assert "implement" not in ex.run_prompts()[0]


async def test_stage_prompt_lets_ai_choose_by_default():
    eng = _engine(_cfg(), FakeExecutor(), FakeReviewer(), FakeAdo(), FakeRepo())
    p = eng._stage_prompt(_item(), CATALOG["implement"], "feature/1-x")
    assert "Choose and run the most appropriate skill" in p
    assert "Implement the work item" in p


async def test_stage_prompt_pins_skill_when_set():
    eng = _engine(_cfg(), FakeExecutor(), FakeReviewer(), FakeAdo(), FakeRepo())
    stage = SdlcStage(name="impl", role="dev", skill="/implement-task-be {id}")
    p = eng._stage_prompt(_item(), stage, "feature/1-x")
    assert "/implement-task-be 1" in p


async def test_dev_profile_handoff_state_configured():
    # The engine itself doesn't set the handoff state (the poller does); assert the
    # dev profile completes cleanly so the poller can apply "Ready to Test".
    ex = FakeExecutor(runs=[FakeRun("impl"), FakeRun(f"pr {_PR}")])
    res = await _engine(
        _cfg(sdlc_profile_states={"dev": "Ready to Test"}),
        ex, FakeReviewer([ReviewResult(passed=True)]), FakeAdo(), FakeRepo(),
    ).run(_item())
    assert res.success and res.skill_used == "sdlc:dev"


async def test_stage_prompt_injects_human_guidance():
    eng = _engine(_cfg(), FakeExecutor(), FakeReviewer(), FakeAdo(), FakeRepo())
    item = _item()
    item.pending_comment = "use the CMMS repo, not DxFac"       # human steered via a comment
    p = eng._stage_prompt(item, CATALOG["implement"], "feature/1-x")
    assert "use the CMMS repo, not DxFac" in p
    assert "highest priority" in p.lower()


async def test_stage_prompt_no_guidance_without_comment():
    eng = _engine(_cfg(), FakeExecutor(), FakeReviewer(), FakeAdo(), FakeRepo())
    p = eng._stage_prompt(_item(), CATALOG["implement"], "feature/1-x")  # no pending_comment
    assert "human guidance" not in p.lower()


async def test_human_guidance_reaches_the_stage_run_prompt():
    # End-to-end: a comment the poller attached to the item must surface in the actual
    # prompt handed to Claude for a resumed/continued SDLC stage.
    ex = FakeExecutor(runs=[FakeRun("implemented"), FakeRun(f"opened {_PR}")])
    engine = _engine(_cfg(), ex, FakeReviewer([ReviewResult(passed=True)]), FakeAdo(), FakeRepo())
    item = _item()
    item.pending_comment = "focus on input validation only"
    res = await engine.run(item)
    assert res.success
    assert any("focus on input validation only" in pr for pr in ex.run_prompts())
