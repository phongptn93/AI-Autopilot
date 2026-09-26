"""A report you cannot act on is a document.

An audit's findings already carry a title, a severity, a file and a line — everything a
work item needs — and re-typing that by hand is where they stop being acted on at all.
The other half of the same problem: the answer itself was printed as Markdown SOURCE,
so a severity-ranked audit reached the screen with its `##` and `**` still in it and
the ranking that is the whole point of the document was invisible.
"""

from __future__ import annotations

import asyncio

from starlette.testclient import TestClient

from ai_autopilot import reports
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.data import Database, LoopReportRepository

FINDINGS = [
    {"severity": "critical", "title": "Hardcoded AES fallback key",
     "detail": "LegacyDevFallbackKey is used in production", "file": "TenantHelper.cs",
     "line": 18, "agent": "sec-secrets"},
    {"severity": "high", "title": "No tenant filter on User CRUD",
     "detail": "GetDetailAsync filters only by Id", "file": "UserService.cs",
     "line": 78, "agent": "sec-auth"},
    {"severity": "low", "title": "Commented-out OneSignal key",
     "detail": "", "file": "secret.yaml", "line": 0, "agent": "sec-secrets"},
]
BODY = "## Summary\n\n**Bad things** in `Helper.cs`.\n\n- one\n- two"


def _settings(tmp_path) -> Settings:
    cfg = Settings(
        dry_run=True, trigger_tag="vm-autopilot",
        ado_project="DxFactory", ado_projects=["Khatoco"],
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'r.db'}",
    )
    cfg.dashboard_auth_password_hash = ""
    return cfg


def _seed(cfg: Settings) -> int:
    """Write one finished report straight into the database the app will read.

    A real ``Report`` rather than a stand-in: the repository reads ``loop``, ``repo``
    and ``summary`` too, and a namespace that happens to satisfy today's reads would
    quietly stop covering tomorrow's.
    """
    async def go() -> int:
        db = Database(cfg.database_url)
        await db.create_all()
        report = reports.Report(
            loop="security-audit", summary="4 findings", body_md=BODY,
            findings=[reports.Finding(**f) for f in FINDINGS],
            status="success", project="DxFactory", repo="Backend-Fresh",
            duration_seconds=12.0, agents=["sec-secrets", "sec-auth"],
        )
        report_id = await LoopReportRepository(db).save(report)
        await db.dispose()
        return report_id

    return asyncio.run(go())


def _client(cfg, created: list[dict], *, fail: bool = False) -> TestClient:
    client = TestClient(create_app(cfg))

    async def fake_create(**kwargs):
        created.append(kwargs)
        return 0 if fail else 9000 + len(created)

    client.__enter__()
    client.app.state.container.ado.create_work_item = fake_create
    return client


def test_the_body_is_rendered_html_not_markdown_source(tmp_path):
    cfg = _settings(tmp_path)
    report_id = _seed(cfg)
    client = _client(cfg, [])
    try:
        page = client.get(f"/dashboard/reports/{report_id}").text
        assert "<h2>Summary</h2>" in page
        assert "<strong>Bad things</strong>" in page
        assert "<code>Helper.cs</code>" in page
        assert "<li>one</li>" in page
        assert "## Summary" not in page          # the source must be gone
    finally:
        client.__exit__(None, None, None)


def test_picking_findings_files_one_work_item_each(tmp_path):
    """One item per finding: they are fixed by different people at different times,
    and a single item holding nine findings is closed when the easiest one is done."""
    cfg = _settings(tmp_path)
    report_id = _seed(cfg)
    created: list[dict] = []
    client = _client(cfg, created)
    try:
        page = client.get(f"/dashboard/reports/{report_id}").text
        assert 'name="finding"' in page and "Tạo work item" in page
        assert "DxFactory" in page and "Khatoco" in page   # workspace → project picker

        answer = client.post(
            f"/dashboard/reports/{report_id}/file",
            data={"finding": ["0", "2"], "project": "Khatoco", "item_type": "Bug"},
            follow_redirects=True,
        )
        assert answer.status_code == 200
    finally:
        client.__exit__(None, None, None)

    assert len(created) == 2
    assert created[0]["title"].startswith("[critical] Hardcoded AES fallback key")
    assert created[1]["title"].startswith("[low] Commented-out OneSignal key")
    assert all(k["project"] == "Khatoco" and k["item_type"] == "Bug" for k in created)
    assert "TenantHelper.cs:18" in created[0]["description"]
    assert created[0]["tag"] == "vm-autopilot"


def test_a_finding_that_cannot_be_filed_is_reported_not_swallowed(tmp_path):
    cfg = _settings(tmp_path)
    report_id = _seed(cfg)
    created: list[dict] = []
    client = _client(cfg, created, fail=True)
    try:
        page = client.post(f"/dashboard/reports/{report_id}/file",
                           data={"finding": "1", "item_type": "Task"},
                           follow_redirects=True).text
        assert "Không tạo được work item nào" in page
    finally:
        client.__exit__(None, None, None)
    assert len(created) == 1                      # it was attempted, and it failed


def test_filing_nothing_says_so(tmp_path):
    cfg = _settings(tmp_path)
    report_id = _seed(cfg)
    created: list[dict] = []
    client = _client(cfg, created)
    try:
        page = client.post(f"/dashboard/reports/{report_id}/file",
                           data={"item_type": "Bug"}, follow_redirects=True).text
        assert "Chưa tick finding nào" in page
    finally:
        client.__exit__(None, None, None)
    assert created == []


def test_an_unknown_work_item_type_is_refused(tmp_path):
    cfg = _settings(tmp_path)
    report_id = _seed(cfg)
    created: list[dict] = []
    client = TestClient(create_app(cfg), raise_server_exceptions=False)
    client.__enter__()
    try:
        answer = client.post(f"/dashboard/reports/{report_id}/file",
                             data={"finding": "0", "item_type": "Epic<script>"})
        assert answer.status_code == 422
    finally:
        client.__exit__(None, None, None)
    assert created == []


# ── The type picker belongs to the PROJECT, not to Azure DevOps ──────────────
#
# Work-item types come from the project's PROCESS TEMPLATE: Agile defines User Story
# and no Product Backlog Item, Scrum the reverse, CMMI defines Requirement. The picker
# offered one fixed Bug/Task/Issue/User Story for every project, so on TLCL-DxFac it
# named types the project would reject — and validation checked the same fixed tuple,
# so the rejection arrived later as an opaque 400, once per finding.

TYPES_BY_TEMPLATE = {
    "DxFactory": ["Bug", "Product Backlog Item", "Impediment"],      # Scrum
    "Khatoco": ["Bug", "Task", "User Story", "Issue"],               # Agile
}


def _typed_client(cfg, created: list[dict], *, boom: bool = False) -> TestClient:
    client = _client(cfg, created)
    asked: list[str] = []

    async def fake_types(project=""):
        if boom:
            raise RuntimeError("ADO unreachable")
        asked.append(project)
        return list(TYPES_BY_TEMPLATE.get(project, []))

    client.app.state.container.ado.get_work_item_types = fake_types
    client.asked = asked          # noqa: SLF001 — test handle
    return client


def test_each_project_is_offered_its_own_work_item_types(tmp_path):
    cfg = _settings(tmp_path)
    report_id = _seed(cfg)
    client = _typed_client(cfg, [])
    try:
        page = client.get(f"/dashboard/reports/{report_id}").text
        # Both templates reach the page, keyed by project, so switching the Project
        # dropdown switches what Loại offers.
        assert "Product Backlog Item" in page and "Impediment" in page
        assert "User Story" in page
        assert '"DxFactory"' in page and '"Khatoco"' in page
        assert set(client.asked) == {"DxFactory", "Khatoco"}
    finally:
        client.__exit__(None, None, None)


def test_filing_refuses_a_type_that_project_does_not_define(tmp_path):
    """"User Story" passed validation on a Scrum project because the check was against
    a hardcoded tuple. The project rejected it afterwards, per finding, as a 400 the
    operator never saw."""
    cfg = _settings(tmp_path)
    report_id = _seed(cfg)
    created: list[dict] = []
    client = _typed_client(cfg, created)
    try:
        answer = client.post(
            f"/dashboard/reports/{report_id}/file",
            data={"finding": ["0"], "project": "DxFactory", "item_type": "User Story"},
        )
        assert answer.status_code == 422
        assert created == []                    # and nothing was filed
    finally:
        client.__exit__(None, None, None)


def test_filing_accepts_a_type_that_project_does_define(tmp_path):
    cfg = _settings(tmp_path)
    report_id = _seed(cfg)
    created: list[dict] = []
    client = _typed_client(cfg, created)
    try:
        answer = client.post(
            f"/dashboard/reports/{report_id}/file",
            data={"finding": ["0"], "project": "DxFactory",
                  "item_type": "Product Backlog Item"},
            follow_redirects=True,
        )
        assert answer.status_code == 200
    finally:
        client.__exit__(None, None, None)
    assert [k["item_type"] for k in created] == ["Product Backlog Item"]


def test_an_unreachable_ado_still_lets_you_file(tmp_path):
    """An outage must not make filing impossible: fall back to the types every process
    template has rather than offering an empty dropdown and refusing every submission."""
    cfg = _settings(tmp_path)
    report_id = _seed(cfg)
    created: list[dict] = []
    client = _typed_client(cfg, created, boom=True)
    try:
        page = client.get(f"/dashboard/reports/{report_id}").text
        assert "Bug" in page
        answer = client.post(
            f"/dashboard/reports/{report_id}/file",
            data={"finding": ["0"], "project": "DxFactory", "item_type": "Bug"},
            follow_redirects=True,
        )
        assert answer.status_code == 200
    finally:
        client.__exit__(None, None, None)
    assert [k["item_type"] for k in created] == ["Bug"]
