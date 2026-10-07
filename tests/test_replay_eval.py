"""Tests for replay evals.

A replay grades the agent against work a human already shipped, so its own mistakes
are expensive in a quiet way: a harvest that picks the wrong base commit grades the
agent against a task it was never given, and a worktree that is not removed piles up
until the repo's own `git worktree list` is unreadable. Every test aims at one of those.

No model is called — the agent and the test runner are injected.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ai_autopilot import replay_cli
from ai_autopilot import replay_eval as rp
from ai_autopilot.evals import load_cases


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """main: init → a squash naming #123 → a merge of a feature branch naming AB#456."""
    r = tmp_path / "ws" / "DemoRepo"
    r.mkdir(parents=True)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "Tester")
    _git(r, "config", "commit.gpgsign", "false")
    _commit(r, {"app.py": "x = 1\n", "README.md": "demo\n"}, "initial commit")
    _commit(r, {"app.py": "x = 2\n", "lib/util.py": "y = 1\n"},
            "Merged PR 7: fix rounding #123\n\nRound half up in invoices.")
    _commit(r, {"notes.txt": "no ref\n"}, "chore: tidy")
    _git(r, "checkout", "-q", "-b", "feature")
    _commit(r, {"feature.py": "f = 1\n"}, "wip")
    _commit(r, {"feature.py": "f = 2\n", "app.py": "x = 3\n"}, "more wip")
    _git(r, "checkout", "-q", "main")
    _git(r, "merge", "-q", "--no-ff", "feature", "-m", "Merge feature: export CSV AB#456")
    return r


# ── case format ──


def test_case_parses_full_and_shorthand_forms():
    full = rp.ReplayCase.from_dict({
        "kind": "replay", "name": "a", "repo": "R", "base_commit": "abc",
        "task": {"title": "T", "description": "D"},
        "expected_files": ["./src/a.py", "src\\b.py"], "timeout_minutes": 5,
    })
    assert (full.title, full.description, full.timeout_minutes) == ("T", "D", 5)
    assert full.expected_files == ["src/a.py", "src/b.py"]
    assert not full.problems()

    short = rp.ReplayCase.from_dict({"repo": "R", "base_commit": "abc",
                                     "task": "Title line\nbody text"})
    assert (short.title, short.description) == ("Title line", "body text")
    assert short.timeout_minutes == 30 and short.test_command == ""

    assert rp.ReplayCase.from_dict({"task": "x"}).problems() == ["no repo", "no base_commit"]


def test_round_trip_through_yaml(tmp_path: Path):
    case = rp.ReplayCase(name="c1", repo="R", base_commit="abc", title="T",
                         description="D", expected_files=["a.py"], work_item_id=9)
    (tmp_path / "c1.yaml").write_text(rp.dump_case_yaml(case), encoding="utf-8")
    [back] = rp.load_replay_cases(tmp_path)
    assert back.to_dict() == case.to_dict()


def test_loader_skips_examples_other_kinds_and_bad_files(tmp_path: Path):
    (tmp_path / "ex.yaml").write_text(
        "kind: replay\nexample: true\nname: ex\nrepo: R\nbase_commit: a\ntask: t\n",
        encoding="utf-8")
    (tmp_path / "cfg.yaml").write_text("name: cfg\nprompt: hi\n", encoding="utf-8")
    (tmp_path / "bad.yaml").write_text("kind: [unclosed\n", encoding="utf-8")
    (tmp_path / "ok.yaml").write_text(
        "kind: replay\nname: ok\nrepo: R\nbase_commit: a\ntask: t\n", encoding="utf-8")
    assert [c.name for c in rp.load_replay_cases(tmp_path)] == ["ok"]
    assert [c.name for c in rp.load_replay_cases(tmp_path, include_examples=True)] == [
        "ex", "ok"]
    assert [c.name for c in rp.load_replay_cases(tmp_path, name="ex",
                                                 include_examples=True)] == ["ex"]


def test_shipped_example_is_valid_and_invisible_to_both_suites():
    root = Path(__file__).resolve().parents[1] / "evals"
    assert rp.load_replay_cases(root / "replay") == []
    [ex] = rp.load_replay_cases(root / "replay", include_examples=True)
    assert ex.example and not ex.problems() and ex.expected_files
    # The configuration suite shares the evals/ tree and must not pick it up.
    assert all("example" not in c.name for c in load_cases(root))


# ── harvest ──


def test_work_item_refs():
    assert rp.work_item_refs("fix AB#12 and #34 (#56), issue#9 &#77; #12") == [12, 34, 56]
    assert rp.work_item_refs("no reference here") == []


def test_harvest_builds_cases_from_history(repo: Path):
    cases = rp.harvest(repo, limit=10)
    by_wi = {c.work_item_id: c for c in cases}
    assert set(by_wi) == {123, 456}

    merge = by_wi[456]
    merge_sha = _git(repo, "rev-parse", "HEAD")
    assert merge.merge_commit == merge_sha
    assert merge.base_commit == _git(repo, "rev-parse", "HEAD^1")   # first parent
    assert sorted(merge.expected_files) == ["app.py", "feature.py"]
    assert merge.title == "Merge feature: export CSV AB#456"
    assert merge.repo == "DemoRepo" and merge.name.startswith("demorepo-wi456-")

    squash = by_wi[123]
    assert squash.base_commit == _git(repo, "rev-parse", "HEAD~3")  # the initial commit
    assert sorted(squash.expected_files) == ["app.py", "lib/util.py"]
    assert squash.title == "fix rounding #123"                       # "Merged PR 7:" gone
    assert squash.description == "Round half up in invoices."
    assert squash.expected_diff_lines == 3

    # Newest first, and the limit is honoured.
    assert [c.work_item_id for c in rp.harvest(repo, limit=1)] == [456]


def test_harvest_tracker_enriches_and_its_failure_falls_back(repo: Path):
    async def fetch(ids):
        return {123: ("Làm tròn hoá đơn", "AC: làm tròn lên")}

    cases = {c.work_item_id: c for c in rp.harvest(repo, fetch_tasks=fetch)}
    assert cases[123].title == "Làm tròn hoá đơn"
    assert "AC: làm tròn lên" in cases[123].description
    assert "fix rounding #123" in cases[123].description            # commit kept
    assert cases[456].title.startswith("Merge feature")             # not returned → as is

    async def broken(ids):
        raise RuntimeError("401")

    assert {c.title for c in rp.harvest(repo, fetch_tasks=broken)} == {
        "fix rounding #123", "Merge feature: export CSV AB#456"}


def test_harvest_cli_writes_and_keeps_existing(repo: Path, tmp_path: Path, capsys):
    class Cfg:
        workspace_directory = str(repo.parent)
        ado_organization = ado_pat = ""

    out = tmp_path / "out"
    assert replay_cli.harvest_main(["--repo", "DemoRepo", "--out", str(out)], Cfg()) == 0
    files = sorted(p.name for p in out.glob("*.yaml"))
    assert len(files) == 2
    edited = out / files[0]
    edited.write_text(edited.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    assert replay_cli.harvest_main(["--repo", "DemoRepo", "--out", str(out)], Cfg()) == 0
    assert edited.read_text(encoding="utf-8").endswith("# edited\n")
    assert len(rp.load_replay_cases(out)) == 2


# ── scoring ──


def _case(**kw) -> rp.ReplayCase:
    base = {"name": "c", "repo": "R", "base_commit": "abc", "title": "T",
            "expected_files": ["a.py", "b.py"]}
    base.update(kw)
    return rp.ReplayCase(**base)


def test_score_passes_on_tests_green_and_overlap():
    s = rp.score_case(_case(), touched_files=["a.py", "c.py"], test_status=rp.TEST_PASSED,
                      diff_lines=10)
    assert s.passed and not s.reasons
    assert s.files_overlap == pytest.approx(1 / 3)
    assert s.missed_files == ["b.py"] and s.extra_files == ["c.py"]


@pytest.mark.parametrize(("kwargs", "reason"), [
    ({"touched_files": [], "test_status": rp.TEST_SKIPPED}, "agent changed no files"),
    ({"touched_files": ["a.py"], "test_status": rp.TEST_FAILED}, "tests failed"),
    ({"touched_files": ["a.py"], "test_status": rp.TEST_TIMEOUT}, "tests timed out"),
    ({"touched_files": ["a.py"], "test_status": rp.TEST_PASSED,
      "conflict_files": ["a.py"]}, "conflict markers left in 1 file(s)"),
    ({"touched_files": ["z.py"], "test_status": rp.TEST_PASSED},
     "no file in common with the human change"),
    ({"touched_files": ["a.py"], "test_status": rp.TEST_PASSED, "error": "boom"},
     "run error: boom"),
])
def test_score_fails_for_each_reason(kwargs, reason):
    s = rp.score_case(_case(), **kwargs)
    assert not s.passed and reason in s.reasons


def test_skipped_tests_do_not_fail_and_unknown_expected_does_not_gate():
    s = rp.score_case(_case(), touched_files=["a.py"], test_status=rp.TEST_SKIPPED)
    assert s.passed and any("skipped" in n for n in s.notes)
    s = rp.score_case(_case(expected_files=[]), touched_files=["z.py"],
                      test_status=rp.TEST_PASSED)
    assert s.passed and s.files_overlap == 0.0


def test_diff_size_is_reported_not_gated():
    s = rp.score_case(_case(expected_diff_lines=10), touched_files=["a.py"],
                      test_status=rp.TEST_PASSED, diff_lines=500)
    assert s.passed and any("50.0x" in n for n in s.notes)


def test_conflict_markers_ignore_markdown_underlines():
    assert rp.find_conflict_markers({
        "doc.md": "Title\n=======\n",
        "bad.py": "a\n<<<<<<< HEAD\nb\n=======\nc\n>>>>>>> other\n",
        "ok.py": "x = '<<<<<<<'\n",
    }) == ["bad.py"]


def test_jaccard_edges():
    assert rp.jaccard([], []) == 1.0
    assert rp.jaccard(["a"], []) == 0.0
    assert rp.jaccard(["./a", "b"], ["a", "b"]) == 1.0


def test_pass_rate_empty_is_zero_and_report_lists_reasons():
    assert rp.pass_rate([]) == 0.0
    s = rp.score_case(_case(), touched_files=[], test_status=rp.TEST_SKIPPED)
    text = rp.format_replay_report([s], 0.5)
    assert "[FAIL] c" in text and "agent changed no files" in text
    assert "proves nothing" in rp.format_replay_report([], 1.0)


# ── running: worktree lifecycle with a mocked agent ──


def _worktrees(repo: Path) -> int:
    return _git(repo, "worktree", "list", "--porcelain").count("worktree ")


async def test_replay_runs_in_worktree_at_base_and_always_cleans_up(repo: Path,
                                                                      tmp_path: Path):
    [case] = [c for c in rp.harvest(repo) if c.work_item_id == 123]
    seen: dict[str, str] = {}

    async def agent(brief, cwd, timeout_s):
        seen["cwd"] = cwd
        seen["head"] = _git(Path(cwd), "rev-parse", "HEAD")
        seen["brief"] = brief
        Path(cwd, "app.py").write_text("x = 2\n", encoding="utf-8")
        Path(cwd, "lib").mkdir(exist_ok=True)
        Path(cwd, "lib", "util.py").write_text("y = 1\n", encoding="utf-8")
        return "done"

    async def tests(cmd, cwd, timeout_s):
        assert cmd == "custom-test" and Path(cwd, "lib", "util.py").is_file()
        return rp.TEST_PASSED, "ok"

    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    before = _worktrees(repo)
    score = await rp.run_replay_case(case, workspace=repo.parent, agent=agent,
                                     test_command_for=lambda r, w: "custom-test",
                                     test_runner=tests, tmp_root=tmp_root)
    assert seen["head"] == case.base_commit
    assert "do not push" in seen["brief"].lower() and case.title in seen["brief"]
    assert score.passed, score.reasons
    assert score.files_overlap == 1.0 and score.tests == rp.TEST_PASSED
    assert not Path(seen["cwd"]).exists() and list(tmp_root.iterdir()) == []
    assert _worktrees(repo) == before
    assert _git(repo, "status", "--porcelain") == ""                 # source repo untouched


async def test_worktree_removed_when_agent_crashes(repo: Path, tmp_path: Path):
    [case] = [c for c in rp.harvest(repo) if c.work_item_id == 456]
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    before = _worktrees(repo)

    async def agent(brief, cwd, timeout_s):
        Path(cwd, "app.py").write_text("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> x\n",
                                       encoding="utf-8")
        raise RuntimeError("model fell over")

    score = await rp.run_replay_case(case, workspace=repo.parent, agent=agent,
                                     test_runner=None, tmp_root=tmp_root)  # type: ignore[arg-type]
    assert not score.passed
    assert any("model fell over" in r for r in score.reasons)
    assert score.conflict_files == ["app.py"]
    assert list(tmp_root.iterdir()) == [] and _worktrees(repo) == before


async def test_unknown_commit_and_missing_repo_fail_cleanly(repo: Path, tmp_path: Path):
    async def agent(brief, cwd, timeout_s):  # pragma: no cover - must not be reached
        raise AssertionError("agent should not run")

    bad = _case(repo="DemoRepo", base_commit="deadbeef" * 5)
    s = await rp.run_replay_case(bad, workspace=repo.parent, agent=agent, tmp_root=tmp_path)
    assert not s.passed and "could not create worktree" in s.error
    assert [p for p in tmp_path.iterdir() if p.name.startswith("replay-")] == []

    s = await rp.run_replay_case(_case(repo="Nope"), workspace=repo.parent, agent=agent)
    assert "not found" in s.error


def test_replay_cli_exit_code_and_json(repo: Path, tmp_path: Path):
    class Cfg:
        workspace_directory = str(repo.parent)
        ado_organization = ado_pat = ""

        def test_command_for(self, repo_name):
            return ""

    cases_dir = tmp_path / "cases"
    rp.write_cases(rp.harvest(repo), cases_dir)

    async def lazy_agent(brief, cwd, timeout_s):
        return "I changed nothing"

    out = tmp_path / "r.json"
    code = replay_cli.replay_main([str(cases_dir), "--json", str(out)], Cfg(), lazy_agent)
    assert code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["total"] == 2 and data["passed"] == 0 and data["pass_rate"] == 0.0
    assert replay_cli.replay_main([str(cases_dir), "--min-pass-rate", "0"], Cfg(),
                                  lazy_agent) == 0
    assert replay_cli.replay_main([str(cases_dir), "--case", "nope"], Cfg(), lazy_agent) == 1
