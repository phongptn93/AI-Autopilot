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


# ── What a finding became, and refusing to file it twice ────────────────────


def _filed_ids(cfg) -> list[int]:
    """The work-item id recorded against each finding, read back off the report."""
    import json

    async def go():
        db = Database(cfg.database_url)
        rows = await LoopReportRepository(db).recent(limit=1)
        data = json.loads(rows[0].findings_json or "[]")
        await db.dispose()
        return [int(f.get("work_item_id") or 0) for f in data]

    return asyncio.run(go())


def test_a_filed_finding_records_the_work_item_it_became(tmp_path):
    """Written into the finding's own entry, not a side table: two answers to "has this
    been filed" drift the moment a report is re-saved, and a finding carrying its own id
    cannot disagree with itself."""
    cfg = _settings(tmp_path)
    report_id = _seed(cfg)
    created: list[dict] = []
    client = _client(cfg, created)
    try:
        client.post(f"/dashboard/reports/{report_id}/file",
                    data={"finding": ["0", "2"], "project": "Khatoco", "item_type": "Bug"},
                    follow_redirects=True)
    finally:
        client.__exit__(None, None, None)
    ids = _filed_ids(cfg)
    assert ids[0] and ids[2]          # both picked findings carry an id…
    assert ids[0] != ids[2]           # …a different one each, one item per finding
    assert ids[1] == 0                # the one nobody picked is untouched


def test_the_page_shows_the_work_item_and_locks_the_row(tmp_path):
    """Locked, not hidden. Hiding acted-on findings would make the audit look shorter
    every time somebody did something about it."""
    cfg = _settings(tmp_path)
    report_id = _seed(cfg)
    client = _client(cfg, [])
    try:
        client.post(f"/dashboard/reports/{report_id}/file",
                    data={"finding": ["0"], "project": "Khatoco", "item_type": "Bug"},
                    follow_redirects=True)
        page = client.get(f"/dashboard/reports/{report_id}").text
    finally:
        client.__exit__(None, None, None)
    import re

    assert "đã tạo" in page
    assert 'name="refile" value="0"' in page          # the per-row unlock

    def pick_tag(index: int) -> str:
        found = re.search(rf'<input[^>]*class="pick"[^>]*value="{index}"[^>]*>', page)
        assert found, f"no pick checkbox rendered for finding {index}"
        return found.group(0)

    assert "disabled" in pick_tag(0)          # filed → locked
    assert "disabled" not in pick_tag(1)      # untouched → still selectable


def test_filing_the_same_finding_again_is_refused(tmp_path):
    """Silently re-filing is how an audit ends up with one finding open three times,
    each with its own half-finished discussion."""
    cfg = _settings(tmp_path)
    report_id = _seed(cfg)
    created: list[dict] = []
    client = _client(cfg, created)
    try:
        for _ in range(2):
            client.post(f"/dashboard/reports/{report_id}/file",
                        data={"finding": ["0"], "project": "Khatoco", "item_type": "Bug"},
                        follow_redirects=True)
    finally:
        client.__exit__(None, None, None)
    assert len(created) == 1          # the second POST created nothing


def test_refile_is_honoured_when_the_reader_asks_for_it(tmp_path):
    """The lock is a guard, not a wall: sometimes the first item was wrong."""
    cfg = _settings(tmp_path)
    report_id = _seed(cfg)
    created: list[dict] = []
    client = _client(cfg, created)
    try:
        client.post(f"/dashboard/reports/{report_id}/file",
                    data={"finding": ["0"], "project": "Khatoco", "item_type": "Bug"},
                    follow_redirects=True)
        client.post(f"/dashboard/reports/{report_id}/file",
                    data={"finding": ["0"], "refile": ["0"],
                          "project": "Khatoco", "item_type": "Bug"},
                    follow_redirects=True)
    finally:
        client.__exit__(None, None, None)
    assert len(created) == 2
    assert _filed_ids(cfg)[0] == 9002      # the row now points at the NEWEST item


def test_the_guard_is_server_side_not_only_a_disabled_checkbox(tmp_path):
    """A disabled input is a suggestion, and the same POST can arrive from curl."""
    cfg = _settings(tmp_path)
    report_id = _seed(cfg)
    created: list[dict] = []
    client = _client(cfg, created)
    try:
        client.post(f"/dashboard/reports/{report_id}/file",
                    data={"finding": ["0"], "project": "Khatoco", "item_type": "Bug"},
                    follow_redirects=True)
        # No `refile`, exactly what a hand-rolled POST bypassing the UI would send.
        client.post(f"/dashboard/reports/{report_id}/file",
                    data={"finding": ["0"], "project": "Khatoco", "item_type": "Bug"},
                    follow_redirects=True)
    finally:
        client.__exit__(None, None, None)
    assert len(created) == 1


# ── One item per finding, or one for all of them ────────────────────────────


def test_mode_one_files_a_single_item_for_every_picked_finding(tmp_path):
    """Six sequential-loop findings in one service are one refactor. Six items for it is
    six people reading the same context and three of them rewriting the same method."""
    cfg = _settings(tmp_path)
    report_id = _seed(cfg)
    created: list[dict] = []
    client = _client(cfg, created)
    try:
        client.post(f"/dashboard/reports/{report_id}/file",
                    data={"finding": ["0", "1", "2"], "mode": "one",
                          "project": "Khatoco", "item_type": "Bug"},
                    follow_redirects=True)
    finally:
        client.__exit__(None, None, None)
    assert len(created) == 1
    body = created[0]["description"]
    for title in ("Hardcoded AES fallback key", "No tenant filter on User CRUD",
                  "Commented-out OneSignal key"):
        assert title in body               # every finding is IN the one item
    # All three rows point at that single item: the mapping is many-to-one by design.
    assert _filed_ids(cfg) == [9001, 9001, 9001]


def test_the_combined_title_carries_the_WORST_severity(tmp_path):
    """Titled with the mildest of the six, it is an item nobody prioritises correctly."""
    cfg = _settings(tmp_path)
    report_id = _seed(cfg)
    created: list[dict] = []
    client = _client(cfg, created)
    try:
        client.post(f"/dashboard/reports/{report_id}/file",
                    data={"finding": ["1", "2"], "mode": "one",     # high + low
                          "project": "Khatoco", "item_type": "Bug"},
                    follow_redirects=True)
    finally:
        client.__exit__(None, None, None)
    assert created[0]["title"].startswith("[high]")
    assert "2 finding" in created[0]["title"]


def test_mode_defaults_to_one_item_per_finding(tmp_path):
    """The default must stay the shape the audit screen was built around."""
    cfg = _settings(tmp_path)
    report_id = _seed(cfg)
    created: list[dict] = []
    client = _client(cfg, created)
    try:
        client.post(f"/dashboard/reports/{report_id}/file",
                    data={"finding": ["0", "1"], "project": "Khatoco", "item_type": "Bug"},
                    follow_redirects=True)
    finally:
        client.__exit__(None, None, None)
    assert len(created) == 2
