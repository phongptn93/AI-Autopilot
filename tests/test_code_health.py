"""Code health: hotspots, knowledge concentration and debt markers, read from git."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ai_autopilot import code_health
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.dashboard.routes.code_health import repo_choices

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

BASE = datetime.now(UTC) - timedelta(days=20)


def _git(repo: Path, *args: str, author=("Alice", "alice@x.vn"), when: datetime = BASE):
    env = {**os.environ,
           "GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1],
           "GIT_COMMITTER_NAME": author[0], "GIT_COMMITTER_EMAIL": author[1],
           "GIT_AUTHOR_DATE": when.isoformat(), "GIT_COMMITTER_DATE": when.isoformat()}
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                   capture_output=True)


def _commit(repo: Path, path: str, text: str, author=("Alice", "alice@x.vn"),
            when: datetime = BASE) -> None:
    f = repo / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A", author=author, when=when)
    _git(repo, "commit", "-q", "-m", f"edit {path}", author=author, when=when)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "ws" / "orders-api"
    r.mkdir(parents=True)
    _git(r, "init", "-q")
    _git(r, "config", "core.autocrlf", "false")
    body = "\n".join(f"line {i}" for i in range(200))
    for i in range(6):   # Alice owns src/core and keeps changing one big file
        _commit(r, "src/core/orders.py", body + f"\n# rev {i}\n# TODO: split this\n",
                when=BASE + timedelta(hours=i))
    _commit(r, "src/web/view.py", "x = 1  # FIXME: wrong unit\n", author=("Bob", "bob@x.vn"),
            when=BASE + timedelta(days=1))
    _commit(r, "package-lock.json", '{"lockfileVersion": 3}\n' * 50,
            when=BASE + timedelta(days=2))
    return r


def test_hotspots_rank_the_file_changed_most_and_ignore_lockfiles(repo):
    report = code_health.analyze(repo, days=90)
    assert report.status == "ok"
    assert report.commits == 8 and report.contributors == 2
    paths = [h.path for h in report.hotspots]
    assert paths[0] == "src/core/orders.py"
    assert "package-lock.json" not in paths
    top = report.hotspots[0]
    assert top.commits == 6 and top.lines > 200 and top.authors == 1


def test_one_person_module_is_flagged_and_turns_to_away_after_a_month(repo):
    report = code_health.analyze(repo, days=90)
    core = next(m for m in report.modules if m.module == "src/core")
    assert core.main_author == "Alice" and core.main_share == 1.0
    assert core.risk == "concentrated"
    web = next(m for m in report.modules if m.module == "src/web")
    assert web.risk == ""   # one commit is a new folder, not a silo

    later = code_health.analyze(repo, days=90, now=datetime.now(UTC) + timedelta(days=40))
    assert next(m for m in later.modules if m.module == "src/core").risk == "away"
    assert later.risky_modules[0].module == "src/core"


def test_debt_markers_are_counted_and_most_urgent_listed_first(repo):
    report = code_health.analyze(repo, days=90)
    assert report.marker_counts == {"FIXME": 1, "TODO": 1}
    assert report.markers[0].kind == "FIXME"
    assert report.markers[0].path == "src/web/view.py"


def test_a_folder_that_is_not_a_repo_reads_unknown_never_clean(tmp_path):
    report = code_health.analyze(tmp_path, days=90)
    assert report.status == "unknown" and report.reason
    assert report.hotspots == [] and report.marker_total == 0


def test_rename_paths_resolve_to_the_new_name():
    text = ("\x1eabc\x1fAlice\x1fA@X.VN\x1f1700000000\n\n"
            "3\t1\tsrc/{old => new}/a.py\n2\t0\tb.py => c.py\n-\t-\tlogo.png\n")
    [c] = code_health.parse_log(text)
    assert [p for p, _ in c.files] == ["src/new/a.py", "c.py"]
    assert c.email == "a@x.vn"


def test_module_is_two_folders_deep():
    assert code_health.module_of("src/orders/api/x.py") == "src/orders"
    assert code_health.module_of("docs/readme.md") == "docs"
    assert code_health.module_of("setup.py") == "(gốc repo)"


@pytest.fixture
def client(repo, tmp_path):
    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'c.db'}",
                   workspace_directory=str(repo.parent))
    with TestClient(create_app(cfg)) as c:
        yield c


def test_page_shows_the_workspace_repo(client):
    resp = client.get("/dashboard/code-health?repo=orders-api&days=90")
    assert resp.status_code == 200
    assert "src/core/orders.py" in resp.text
    assert "Chỉ một người nắm" in resp.text
    assert "FIXME" in resp.text


def test_page_refuses_a_repo_outside_the_workspace(client, tmp_path):
    resp = client.get("/dashboard/code-health", params={"repo": str(tmp_path)})
    assert resp.status_code == 200
    assert "Không có repo tên" in resp.text
    assert "src/core/orders.py" not in resp.text


def test_choices_come_only_from_configured_workspaces(repo):
    cfg = Settings(dry_run=True, workspace_directory=str(repo.parent))
    assert [r.key for r in repo_choices(cfg)] == ["orders-api"]


# ── ownership hints on the PRs the autopilot opens ──────────────────────────


def test_hints_name_the_owner_and_the_hotspot_the_pr_touches(repo):
    report = code_health.analyze(repo, days=90)
    hints = code_health.review_hints(report, ["orders-api/src/core/orders.py"], "orders-api")
    assert hints.worth_saying
    assert [h.path for h in hints.hotspots] == ["src/core/orders.py"]
    assert [m.module for m in hints.owners] == ["src/core"]
    text = code_health.render_hints(hints, report.days)
    assert "Alice" in text and "chỉ một người nắm" in text and "src/core/orders.py" in text


def test_a_quiet_change_says_nothing(repo):
    report = code_health.analyze(repo, days=90)
    hints = code_health.review_hints(report, ["docs/new.md"], "orders-api")
    assert not hints.worth_saying


def test_hint_text_is_escaped():
    m = code_health.ModuleKnowledge("src/<x>", 9, 1, "<script>bad</script>", 1.0, None,
                                    "concentrated")
    text = code_health.render_hints(code_health.ReviewHints(owners=[m]), 90)
    assert "<script>" not in text and "&lt;script&gt;" in text


def test_repo_path_for_matches_the_pr_repo_name(repo):
    cfg = Settings(dry_run=True, workspace_directory=str(repo.parent))
    assert code_health.repo_path_for(cfg, "ORDERS-API") == str(repo.resolve())
    assert code_health.repo_path_for(cfg, "elsewhere") == ""


class _Log:
    def __init__(self):
        self.warned = []

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        self.warned.append(a)


class _Ado:
    def __init__(self, fail=False):
        self.comments, self.fail = [], fail

    async def add_pull_request_comment(self, repo, pr_id, text, **_):
        if self.fail:
            raise RuntimeError("ADO down")
        self.comments.append((repo, pr_id, text))
        return True


_ITEM = __import__('types').SimpleNamespace(id=7)


def _poller(cfg, ado):
    from types import SimpleNamespace
    return SimpleNamespace(_config=cfg, _c=SimpleNamespace(ado=ado), _log=_Log())


def _result(files):
    from ai_autopilot.models import ExecutionResult
    return ExecutionResult(
        work_item_id=1, success=True, files_changed=files,
        pr_url="https://dev.azure.com/org/proj/_git/orders-api/pullrequest/42")


@pytest.mark.asyncio
async def test_poller_posts_hints_only_when_worth_saying(repo):
    from ai_autopilot.services.poller import AdoPollerService
    cfg = Settings(dry_run=False, workspace_directory=str(repo.parent))
    ado = _Ado()
    p = _poller(cfg, ado)
    await AdoPollerService._post_ownership_hints(p, _ITEM, _result(["src/core/orders.py"]))
    await AdoPollerService._post_ownership_hints(p, _ITEM, _result(["docs/x.md"]))
    assert [(r, n) for r, n, _ in ado.comments] == [("orders-api", 42)]


@pytest.mark.asyncio
async def test_poller_hint_failure_never_fails_the_run(repo):
    from ai_autopilot.services.poller import AdoPollerService
    cfg = Settings(dry_run=False, workspace_directory=str(repo.parent))
    p = _poller(cfg, _Ado(fail=True))
    await AdoPollerService._post_ownership_hints(p, _ITEM, _result(["src/core/orders.py"]))
    assert p._log.warned


@pytest.mark.asyncio
async def test_poller_hints_can_be_switched_off(repo):
    from ai_autopilot.services.poller import AdoPollerService
    cfg = Settings(dry_run=False, workspace_directory=str(repo.parent), pr_ownership_hints=False)
    ado = _Ado()
    await AdoPollerService._post_ownership_hints(_poller(cfg, ado), _ITEM,
                                                 _result(["src/core/orders.py"]))
    assert ado.comments == []
