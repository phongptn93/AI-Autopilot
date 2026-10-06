"""Requirements & specs: find specs, read one safely, write a requirement, approve a spec."""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import quote

import pytest
from starlette.testclient import TestClient

from ai_autopilot import spec_library
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    feat = ws / "specs" / "active" / "inventory-export"
    feat.mkdir(parents=True)
    (feat / "inventory-export.md").write_text(
        "# Xuất báo cáo tồn kho\n\nADO: #8530\n\n## AC\n| # | Nội dung |\n|---|---|\n"
        "| 1 | <script>alert(1)</script> |\n", encoding="utf-8")
    (feat / "inventory-export-mockup.html").write_text(
        "<html><head><title>Mockup tồn kho</title></head><body>ui</body></html>", encoding="utf-8")
    (ws / "secrets.md").write_text("# not a spec\nPAT=xyz", encoding="utf-8")
    (ws / "specs" / "notes.txt").write_text("not a spec type", encoding="utf-8")
    return ws


# ── the library ──────────────────────────────────────────────────────────────


def test_discovery_finds_specs_and_links_the_folder_to_its_item(workspace):
    specs = {s.rel: s for s in spec_library.discover(str(workspace))}
    md = "specs/active/inventory-export/inventory-export.md"
    html = "specs/active/inventory-export/inventory-export-mockup.html"
    assert set(specs) == {md, html}
    assert specs[md].title == "Xuất báo cáo tồn kho" and specs[md].item_id == 8530
    # The mockup never names the item; its folder-mate does.
    assert specs[html].item_id == 8530 and specs[html].title == "Mockup tồn kho"
    assert specs[md].siblings == [html]


@pytest.mark.parametrize("rel", [
    "secrets.md",                       # in the workspace, not in a spec folder
    "../outside.md",                    # escapes the workspace
    "specs/notes.txt",                  # wrong type
    "specs/active/missing.md",          # does not exist
])
def test_only_spec_files_can_be_opened(workspace, rel):
    (workspace.parent / "outside.md").write_text("x", encoding="utf-8")
    assert spec_library.resolve(str(workspace), rel) is None


@pytest.mark.parametrize("text,want", [
    ("ADO: #8530", 8530), ("Work item: 1234", 1234), ("work_item_id = 777", 777),
    ("see https://dev.azure.com/o/p/_workitems/edit/4321", 4321), ("[#555]", 555),
    ("Version 2.0 · 2026", 0),
])
def test_item_refs(text, want):
    assert spec_library.item_ref(text) == want


# ── the pages ────────────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, workspace):
    cfg = Settings(dry_run=False, database_url=f"sqlite+aiosqlite:///{tmp_path / 'r.db'}",
                   workspace_directory=str(workspace), ado_organization="https://dev.azure.com/o")
    with TestClient(create_app(cfg)) as c:
        calls: list = []

        async def create(title, item_type, parent, tag, description="", project=""):
            calls.append(("create", title, item_type, tag, description))
            return 9001

        async def rec(name, *a, **k):
            calls.append((name, *a))
            return True

        async def get_item(item_id):
            return SimpleNamespace(id=item_id, tags=["SDLC:ba", "other"], state="New",
                                   work_item_type="User Story", project="")

        c.app.state.container.ado = SimpleNamespace(
            create_work_item=create, get_work_item=get_item,
            add_tag=lambda i, t: rec("add_tag", i, t),
            remove_tag=lambda i, t: rec("remove_tag", i, t),
            add_comment=lambda i, t: rec("comment", i, t),
            update_state=lambda i, s: rec("state", i, s),
            refresh=lambda: None,
        )
        c.calls = calls
        yield c


def test_the_page_lists_specs(client):
    html = client.get("/dashboard/requirements").text
    assert "Xuất báo cáo tồn kho" in html and "#8530" in html and "Yêu cầu mới" in html


def test_a_markdown_spec_renders_escaped(client):
    path = quote("specs/active/inventory-export/inventory-export.md")
    html = client.get(f"/dashboard/requirements/spec?path={path}").text
    assert "<table" in html
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert "#8530" in html and "inventory-export-mockup.html" in html


def test_a_secret_cannot_be_read_through_the_reader(client):
    assert client.get("/dashboard/requirements/spec?path=secrets.md").status_code == 404
    assert client.get("/dashboard/requirements/raw?path=secrets.md").status_code == 404


def test_the_html_mockup_is_served_sandboxed(client):
    path = quote("specs/active/inventory-export/inventory-export-mockup.html")
    resp = client.get(f"/dashboard/requirements/raw?path={path}")
    assert resp.status_code == 200 and "sandbox" in resp.headers["content-security-policy"]


def test_a_requirement_becomes_a_work_item_with_its_criteria(client):
    client.post("/dashboard/requirements", data={
        "title": "Xuất tồn kho", "type": "User Story", "need": "Kho cần **file Excel**",
        "criteria": "- Lọc theo kho\n- Có tổng theo nhóm",
    }, follow_redirects=False)
    create = next(c for c in client.calls if c[0] == "create")
    assert create[3] == "requirement-draft"
    assert "<strong>file Excel</strong>" in create[4]
    assert "<li>Lọc theo kho</li>" in create[4] and "<li>Có tổng theo nhóm</li>" in create[4]


def test_analyse_now_pins_the_ba_role_and_starts_it(client):
    client.post("/dashboard/requirements", data={"title": "X", "analyse": "on"},
                follow_redirects=False)
    tags = [c[2] for c in client.calls if c[0] == "add_tag"]
    assert "sdlc:ba" in tags
    create = next(c for c in client.calls if c[0] == "create")
    assert create[3] == ""                  # not a draft


def test_approving_hands_the_item_to_dev(client):
    client.post("/dashboard/requirements/review", data={
        "path": "specs/active/inventory-export/inventory-export.md", "item_id": "8530",
        "decision": "approve", "start_dev": "on",
    }, follow_redirects=False)
    comments = [c[2] for c in client.calls if c[0] == "comment"]
    assert any("SPEC APPROVED" in t for t in comments)
    assert ("remove_tag", 8530, "SDLC:ba") in client.calls      # ADO's own casing
    assert ("add_tag", 8530, "sdlc:dev") in client.calls


def test_feedback_needs_words_and_leaves_a_comment(client):
    resp = client.post("/dashboard/requirements/review", data={
        "path": "x.md", "item_id": "8530", "decision": "feedback", "note": "",
    }, follow_redirects=False)
    assert "req_feedback_empty" in resp.headers.get("set-cookie", "")
    client.post("/dashboard/requirements/review", data={
        "path": "x.md", "item_id": "8530", "decision": "feedback", "note": "AC-2 thiếu <b>",
    }, follow_redirects=False)
    comment = next(c[2] for c in client.calls if c[0] == "comment")
    assert "SPEC FEEDBACK" in comment and "&lt;b&gt;" in comment
