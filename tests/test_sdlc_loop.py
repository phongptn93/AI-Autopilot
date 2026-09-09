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
    # implement, review and pr each run a skill — review is not the exception it was.
    ex = FakeExecutor(
        runs=[FakeRun("implemented"), FakeRun("reviewed"), FakeRun(f"opened {_PR}")]
    )
    ado, repo = FakeAdo(), FakeRepo()
    engine = _engine(_cfg(), ex, FakeReviewer([ReviewResult(passed=True)]), ado, repo)
    res = await engine.run(_item())

    assert res.success and not res.needs_human
    assert res.pr_url == _PR
    assert repo.store == {}                       # cleared on success
    assert ex.acquired == 1 and ex.released == 1  # scratch once
    assert len(ado.comments) == 3                 # a badge per dev stage (implement/review/pr)


async def test_review_failure_exhausts_budget_and_escalates():
    ex = FakeExecutor(runs=[FakeRun("implemented")])          # review reruns on each revise
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
    ex = FakeExecutor(runs=[FakeRun("reviewed"), FakeRun(f"opened {_PR}")])
    engine = _engine(_cfg(), ex, FakeReviewer([ReviewResult(passed=True)]), FakeAdo(), repo)
    res = await engine.run(_item())

    assert res.success and res.pr_url == _PR
    # Resumed at review → implement was skipped; review and pr each run their skill.
    assert len(ex.run_prompts()) == 2
    assert not any("'implement'" in prompt for prompt in ex.run_prompts())


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


# ── Board wiring: the state says which role, and whether it self-starts ──────────


def _wired():
    """A central machine: one box, every role, roles told apart by ADO state."""
    from ai_autopilot.config import Settings

    return Settings(
        trigger_states=["New", "Active", "Ready for Development", "Ready for Testing"],
        sdlc_stage_wiring={
            "analyze": {"queue_state": "Ready for Analysis", "auto": True},
            "implement": {"queue_state": "Ready for Development", "auto": True},
            "test": {"queue_state": "Ready for Testing", "working_state": "In Testing",
                     "auto": False},
        },
    )


def test_the_state_an_item_stands_in_picks_the_role():
    """A central machine runs every role, so the role cannot come from the machine —
    and a Bug is a Bug at every step, so it cannot come from the type either."""
    from ai_autopilot.execution.sdlc_plan import profile_for_state, resolve_profile_name

    cfg = _wired()
    assert profile_for_state("Ready for Testing", cfg) == "qc"
    assert profile_for_state("ready for development", cfg) == "dev"   # case-insensitive
    assert profile_for_state("Closed", cfg) == ""                     # nobody claims it
    assert resolve_profile_name([], "Bug", cfg, state="Ready for Testing") == "qc"
    # An explicit per-item tag is somebody stating an intention — it outranks the state.
    assert resolve_profile_name(["sdlc:ba"], "Bug", cfg, state="Ready for Testing") == "ba"
    # Nothing wired → unchanged behaviour.
    from ai_autopilot.config import Settings
    assert resolve_profile_name([], "Bug", Settings(), state="Ready for Testing") == "full"


def test_only_the_entry_stage_claims_a_state():
    """`full` passes through `test` on its way, but QC's door is not full's door."""
    from ai_autopilot.execution.sdlc_plan import profile_for_state, profile_stages

    cfg = _wired()
    assert "test" in [s.name for s in profile_stages("full", cfg)]
    assert profile_for_state("Ready for Testing", cfg) == "qc"


def test_auto_decides_which_hand_offs_self_start():
    """The autonomy dial sits next to the state it governs — which is the whole fix
    for a QC hand-off silently becoming a trigger and reworking finished items."""
    cfg = _wired()
    active = cfg.effective_trigger_states
    assert "Ready for Development" in active     # wired auto:true
    assert "Ready for Testing" not in active     # wired auto:false — waits for ▶ Run
    assert "New" in active and "Active" in active  # untouched entries survive


def test_a_wired_stage_names_its_own_working_state():
    """Two roles at once on one machine both reading 'Active' is a board that cannot
    say who is holding the item."""
    from ai_autopilot.execution.sdlc_plan import working_state_for

    cfg = _wired()
    assert working_state_for("qc", cfg) == "In Testing"
    assert working_state_for("dev", cfg) == ""      # falls back to the global one


def test_hand_off_is_keyed_by_profile_not_by_stage():
    """`dev` and `full` both end at the `pr` stage but hand to different roles, so
    where a profile hands off cannot be written on the stage they share."""
    from ai_autopilot.config import Settings
    from ai_autopilot.execution.sdlc_plan import handoff_state, profile_stages

    cfg = Settings(sdlc_profile_states={"dev": "Ready for Testing"}, resolved_state="Resolved")
    assert profile_stages("dev", cfg)[-1].name == profile_stages("full", cfg)[-1].name == "pr"
    assert handoff_state("dev", cfg) == "Ready for Testing"
    assert handoff_state("full", cfg) == "Resolved"


def test_collision_check_covers_every_profile_not_just_one():
    """The old check read the pinned/default profile only — right for one machine per
    role, blind on a central machine that runs them all."""
    from ai_autopilot.config import Settings
    from ai_autopilot.execution.sdlc_plan import handoff_collides, handoff_collisions

    cfg = Settings(
        trigger_states=["New", "Ready for Testing"],
        sdlc_default_profile="ba",                      # NOT the profile that collides
        sdlc_profile_states={"dev": "Ready for Testing"},
    )
    assert ("dev", "Ready for Testing") in handoff_collisions(cfg)
    assert handoff_collides(cfg)
    assert not handoff_collides(Settings(trigger_states=["New"]))


async def test_pressing_run_on_a_waiting_stage_keeps_the_role():
    """A queue state marked auto=false is absent from the poll query on purpose, so a
    manual start cannot go through the state: moving the item into a trigger state to
    make it pollable erases the very thing that says which role is due, and QC
    pressing Run would get the whole pipeline instead of the test stage."""
    from types import SimpleNamespace

    from ai_autopilot.config import Settings
    from ai_autopilot.services.planning_analyzer import start_items

    cfg = _wired()
    assert "Ready for Testing" not in cfg.effective_trigger_states   # waits for a person

    tags: list[tuple[int, str]] = []
    states: list[tuple[int, str]] = []
    ado = SimpleNamespace(
        get_work_item=lambda iid: _awaited(SimpleNamespace(
            id=iid, state="Ready for Testing", tags=["vm-autopilot"])),
        add_tag=lambda iid, t: _record(tags, (iid, t)),
        update_state=lambda iid, st: _record(states, (iid, st)),
    )
    assert await start_items(SimpleNamespace(config=cfg, ado=ado), [7]) == 1
    assert states == []                                   # the role state is untouched
    assert (7, cfg.stage_entry_tag) in tags               # released by the one-shot tag

    # An ordinary item (no wired state) still gets moved into a trigger state.
    plain = Settings(trigger_states=["New"])
    tags2: list[tuple[int, str]] = []
    states2: list[tuple[int, str]] = []
    ado2 = SimpleNamespace(
        get_work_item=lambda iid: _awaited(SimpleNamespace(id=iid, state="Closed", tags=[])),
        add_tag=lambda iid, t: _record(tags2, (iid, t)),
        update_state=lambda iid, st: _record(states2, (iid, st)),
    )
    await start_items(SimpleNamespace(config=plain, ado=ado2), [8])
    assert states2 == [(8, "New")]


async def _awaited(value):
    return value


async def _record(sink, entry):
    sink.append(entry)


def test_a_state_that_names_two_profiles_is_refused_not_guessed():
    """`analyze` is the first stage of BOTH `ba` and `full`, so wiring it to a state
    does not say which one to run. Breaking the tie by iteration order would have
    picked the one-stage `ba` and quietly never run the pipeline."""
    from ai_autopilot.config import SdlcStageWiring, Settings
    from ai_autopilot.execution.sdlc_plan import profile_for_state, profile_map

    opens = [p for p, ss in profile_map(Settings()).items() if ss and ss[0] == "analyze"]
    assert set(opens) == {"ba", "full"}          # the tie is real, not hypothetical

    ambiguous = Settings(sdlc_stage_wiring={
        "analyze": SdlcStageWiring(queue_state="Ready for Analysis"),
    })
    assert profile_for_state("Ready for Analysis", ambiguous) == ""   # refuses to guess

    named = Settings(sdlc_stage_wiring={
        "analyze": SdlcStageWiring(queue_state="Ready for Analysis", runs_profile="full"),
    })
    assert profile_for_state("Ready for Analysis", named) == "full"

    # A stage only one profile starts at needs no choice.
    assert profile_for_state("Ready for Testing", Settings(sdlc_stage_wiring={
        "test": SdlcStageWiring(queue_state="Ready for Testing"),
    })) == "qc"


def test_doctor_names_the_unresolved_tie():
    from ai_autopilot.config import SdlcStageWiring, Settings
    from ai_autopilot.doctor import check_relay_wiring

    warn = check_relay_wiring(Settings(sdlc_stage_wiring={
        "analyze": SdlcStageWiring(queue_state="Ready for Analysis"),
    }))
    assert {f.level for f in warn} == {"warn"}
    assert "Ready for Analysis" in warn[0].title

    ok = check_relay_wiring(Settings(sdlc_stage_wiring={
        "analyze": SdlcStageWiring(queue_state="Ready for Analysis", runs_profile="full"),
    }))
    assert {f.level for f in ok} == {"ok"}
    assert check_relay_wiring(Settings()) == []     # nothing wired → nothing to say


async def test_review_stage_runs_a_skill_and_is_still_gated():
    """Review used to skip straight to the gate: its goal never reached an agent and
    the workspace's review skills were never chosen. It must do both now."""
    ex = FakeExecutor(
        runs=[FakeRun("implemented"), FakeRun("reviewed"), FakeRun(f"opened {_PR}")]
    )
    engine = _engine(_cfg(), ex, FakeReviewer([ReviewResult(passed=True)]), FakeAdo(), FakeRepo())
    res = await engine.run(_item())

    assert res.success
    prompts = ex.run_prompts()
    assert len(prompts) == 3                       # implement, review, pr
    review_prompt = prompts[1]
    assert "'review'" in review_prompt
    # The goal reaches the agent — including the half the hard-coded gate never asked.
    assert "correctness and security" in review_prompt
    assert "Choose and run the most appropriate skill(s)" in review_prompt


async def test_review_stage_bills_both_its_calls():
    """The stage makes two model calls (its own skill + the gate); billing one of
    them understated every SDLC run."""
    ex = FakeExecutor(runs=[
        FakeRun("implemented", tokens=5), FakeRun("reviewed", tokens=7),
        FakeRun(f"opened {_PR}", tokens=5),
    ])
    gate = FakeRun("- [None] no issues found", tokens=11)
    reviewer = FakeReviewer([ReviewResult(passed=True, run=gate)])
    engine = _engine(_cfg(), ex, reviewer, FakeAdo(), FakeRepo())
    res = await engine.run(_item())

    assert res.success
    assert res.cost_tokens == 5 + 7 + 5 + 11       # the gate call is billed too


async def test_artifact_skill_is_asked_for_when_a_stage_declares_one():
    """``artifact_skill`` was config nothing read — three stages set it and no code
    path ever ran it."""
    eng = _engine(_cfg(), FakeExecutor(), FakeReviewer(), FakeAdo(), FakeRepo())
    assert "bugfix-report" in eng._stage_prompt(_item(), CATALOG["review"], "feature/1-x")


async def test_no_artifact_line_when_a_stage_declares_none():
    eng = _engine(_cfg(), FakeExecutor(), FakeReviewer(), FakeAdo(), FakeRepo())
    p = eng._stage_prompt(_item(), CATALOG["implement"], "feature/1-x")
    assert "skill to produce its report" not in p
