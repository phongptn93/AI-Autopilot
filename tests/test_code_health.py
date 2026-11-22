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
