"""Proving a lesson works: holding, ignored, and the rule that needs a hook."""

from __future__ import annotations

from datetime import datetime

from starlette.testclient import TestClient

from ai_autopilot import lesson_evidence as ev
from ai_autopilot import lessons
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings

NOW = datetime(2026, 10, 21)
RULE = "Dùng ILogger, không Console.WriteLine"


def _occur(ws: str, text: str, repo: str = "Api", wid: int = 1, pr: int = 0) -> None:
    """One quality signal, the way QualityLog records it."""
    lessons.record_lessons(ws, repo, [text], now=NOW,
                           evidence={"kind": "pr_revision", "work_item_id": wid, "pr_id": pr})


def _verdict(ws: str, text: str, repo: str = "Api") -> ev.Verdict:
    return ev.assess(ws)[(repo, lessons.normalize(text))].verdict


def test_first_occurrence_is_not_counted_as_ignored(tmp_path):
    ws = str(tmp_path)
    _occur(ws, "Validate DTOs")
    st = ev.stats(ws, "Api", "Validate DTOs")
    assert st.ignored == 0 and st.first_seen == "2026-10-21"
    assert _verdict(ws, "Validate DTOs").name == ev.UNPROVEN


def test_a_lesson_carried_in_enough_quiet_runs_is_holding(tmp_path):
    ws = str(tmp_path)
    _occur(ws, "Validate DTOs")
    for wid in range(10, 10 + ev.HOLD_AFTER):
        assert ev.expose(ws, ["Api"], [wid]) == 1
    v = _verdict(ws, "Validate DTOs")
    assert v.name == ev.HOLDING and "chưa phải bằng chứng" in v.reason   # a signal, not proof
    _occur(ws, "Validate DTOs", wid=12)        # a run that carried it repeated it
    st = ev.stats(ws, "Api", "Validate DTOs")
    assert st.quiet_runs == 0 and st.ignored == 1 and st.exposures == ev.HOLD_AFTER
    assert st.evidence[-1]["carried"] is True
    assert _verdict(ws, "Validate DTOs").name == ev.UNPROVEN


def test_a_repeat_by_a_run_that_never_carried_the_lesson_is_not_held_against_it(tmp_path):
    # Concurrent runs: #2 started before #1's lesson existed, then made the same mistake.
    ws = str(tmp_path)
    _occur(ws, "Validate DTOs", wid=1)
    ev.expose(ws, ["Api"], [3])                # run #3 starts and carries it
    _occur(ws, "Validate DTOs", wid=2)         # #2 never saw it: not ignored
    assert ev.stats(ws, "Api", "Validate DTOs").ignored == 0
    _occur(ws, "Validate DTOs", wid=3)         # #3 carried it and still repeated it
    st = ev.stats(ws, "Api", "Validate DTOs")
    assert st.ignored == 1 and [e.get("carried", False) for e in st.evidence] == [
        False, False, True]


def test_a_batch_run_marks_every_item_as_a_carrier(tmp_path):
    ws = str(tmp_path)
    _occur(ws, "Validate DTOs", wid=1)
    ev.expose(ws, ["Api"], [5, 6])
    _occur(ws, "Validate DTOs", wid=6)
    assert ev.stats(ws, "Api", "Validate DTOs").ignored == 1


def test_exposure_is_scoped_to_the_runs_repos_but_rules_ride_on_every_run(tmp_path):
    ws = str(tmp_path)
    _occur(ws, "api guess", repo="Api")
    _occur(ws, "fe guess", repo="Web")
    lessons.add(ws, "Web", RULE)
    ev.expose(ws, ["Api"])
    assert ev.stats(ws, "Api", "api guess").exposures == 1
    assert ev.stats(ws, "Web", "fe guess").exposures == 0       # its skill was not named
    assert ev.stats(ws, "Web", RULE).exposures == 1             # rules load everywhere


def test_a_guess_that_keeps_recurring_is_ignored_and_asks_for_promotion(tmp_path):
    ws = str(tmp_path)
    for wid in (1, 2, 3):
        _occur(ws, "Validate DTOs", wid=wid)
    v = _verdict(ws, "Validate DTOs")
    assert v.name == ev.IGNORED and "2 lần" in v.reason


def test_a_rule_that_is_still_broken_needs_enforcement_not_more_prose(tmp_path):
    ws = str(tmp_path)
    _occur(ws, RULE, wid=1)
    _occur(ws, RULE, wid=2)                 # repeats while it is only a guess…
    assert lessons.promote(ws, "Api", RULE)
    assert _verdict(ws, RULE).name != ev.ENFORCE
    _occur(ws, RULE, wid=3, pr=30)
    _occur(ws, RULE, wid=4, pr=40)          # …and twice more after it became a rule
    a = ev.assess(ws)[("Api", lessons.normalize(RULE))]
    assert a.stats.ignored_as_rule == 2 and a.verdict.name == ev.ENFORCE
    assert RULE in a.prompt and "#4 (PR !40)" in a.prompt and "hook" in a.prompt.lower()
    assert ev.health(ev.assess(ws))[ev.ENFORCE] == 1


def test_lessons_from_before_evidence_keep_their_promote_advice(tmp_path):
    ws = str(tmp_path)
    for _ in range(3):                                      # recorded without evidence
        lessons.record_lessons(ws, "Api", ["Old recurring"], now=NOW)
    assert ev.stats(ws, "Api", "Old recurring").exposures == 0
    assert _verdict(ws, "Old recurring").name == ev.IGNORED


def test_a_human_typing_a_rule_is_not_an_occurrence(tmp_path):
    ws = str(tmp_path)
    lessons.add(ws, "Api", RULE)
    lessons.add(ws, "Api", RULE)
    assert ev.stats(ws, "Api", RULE).ignored == 0


def test_edit_keeps_the_record_and_delete_or_clear_drops_it(tmp_path):
    ws = str(tmp_path)
    _occur(ws, "Validate DTOs")
    _occur(ws, "Validate DTOs", wid=2)
    assert lessons.edit(ws, "Api", "Validate DTOs", "Validate every DTO")
    assert ev.stats(ws, "Api", "Validate every DTO").ignored == 1
    assert lessons.delete(ws, "Api", "Validate every DTO")
    assert ev.stats(ws, "Api", "Validate every DTO") == ev.Stats()
    _occur(ws, "another")
    assert lessons.clear(ws, "Api")
    assert ev.all_stats(ws) == {}


def test_a_corrupt_sidecar_reads_as_no_evidence(tmp_path):
    ws = str(tmp_path)
    _occur(ws, "x")
    (tmp_path / ".autopilot" / "lessons" / "_evidence.json").write_text("{nope")
    assert ev.stats(ws, "Api", "x") == ev.Stats()
    _occur(ws, "x")                                         # and recording recovers
    assert ev.stats(ws, "Api", "x").ignored == 1


def test_the_sidecar_is_not_mistaken_for_a_repo(tmp_path):
    ws = str(tmp_path)
    _occur(ws, "x")
    assert lessons.list_repos(ws) == ["Api"]


def test_quality_log_passes_the_work_item_and_pr_as_evidence(tmp_path):
    import asyncio

    from ai_autopilot.data import QualityKind
    from ai_autopilot.learning import QualityLog

    class Repo:
        async def record(self, **_):
            return None

    ws = str(tmp_path)
    cfg = Settings(workspace_directory=ws, learning_loop_enabled=True)
    log = QualityLog(Repo(), cfg, repos_provider=lambda: ["Api"])
    ask = "dùng ILogger thay cho Console.WriteLine ở mọi service mới"
    asyncio.run(log.record(work_item_id=77, kind=QualityKind.PR_REVISION, pr_id=9,
                           detail=ask, actor="human"))
    [row] = [a for a in ev.assess(ws).values() if a.repo == "Api"]
    assert row.stats.evidence[-1]["work_item_id"] == 77
    assert row.stats.evidence[-1]["pr_id"] == 9


def test_the_page_and_inbox_show_the_rule_that_needs_a_hook(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    for wid in (1, 2):
        _occur(str(ws), RULE, wid=wid)
    lessons.promote(str(ws), "Api", RULE)
    for wid in (3, 4):
        _occur(str(ws), RULE, wid=wid)
    cfg = Settings(dry_run=True, workspace_directory=str(ws), learning_loop_enabled=True,
                   database_url=f"sqlite+aiosqlite:///{tmp_path / 'l.db'}")
    with TestClient(create_app(cfg)) as client:
        html = client.get("/dashboard/learning").text
        assert "Cần ép bằng máy" in html and "vẫn vi phạm" in html
        assert "Đừng viết thêm hướng dẫn" in html
        inbox = client.get("/dashboard/inbox").text
        assert "Quy tắc vẫn bị vi phạm 2 lần" in inbox


def test_a_real_run_counts_exposure_but_rendering_the_page_does_not(tmp_path):
    from ai_autopilot.execution.auto_reviewer import AutoReviewer
    from ai_autopilot.execution.claude_executor import ClaudeExecutor

    ws = tmp_path / "ws"
    ws.mkdir()
    _occur(str(ws), "Validate DTOs")
    cfg = Settings(dry_run=True, workspace_directory=str(ws), learning_loop_enabled=True,
                   database_url=f"sqlite+aiosqlite:///{tmp_path / 'l.db'}")
    with TestClient(create_app(cfg)) as client:
        client.get("/dashboard/learning")
    assert ev.stats(str(ws), "Api", "Validate DTOs").exposures == 0
    ClaudeExecutor(cfg, AutoReviewer(cfg))._lessons_injected(["Api"], [42])
    assert ev.stats(str(ws), "Api", "Validate DTOs").exposures == 1
    _occur(str(ws), "Validate DTOs", wid=42)        # the run it rode with repeated it
    assert ev.stats(str(ws), "Api", "Validate DTOs").ignored == 1
    off = Settings(workspace_directory=str(ws), learning_loop_enabled=False)
    ClaudeExecutor(off, AutoReviewer(off))._lessons_injected(["Api"])
    assert ev.stats(str(ws), "Api", "Validate DTOs").exposures == 1   # loop off: nothing
