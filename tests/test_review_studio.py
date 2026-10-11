"""🧭 Review Studio: contract parsing, scoring, renderings, persistence and pages.

The agent itself is faked — what is pinned here is what a review MEANS: which rows
count as gaps, how coverage is scored, what reaches ADO, and that a reply without the
contract is shown instead of lost.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from starlette.testclient import TestClient

from ai_autopilot import review_studio as rs
from ai_autopilot.app import create_app
from ai_autopilot.config import ScheduledLoop, Settings
from ai_autopilot.services import review_studio as svc_mod

_REPLY = """Phân tích ngắn: module duyệt phiếu đã có phần lớn.

```json
{"verdict": "Đã có 2/4 mục, 1 mục trái spec",
 "summary": "Rà 4 mục.",
 "traces": [
  {"ref": "FR-01", "kind": "FR", "spec": "Cho phép duyệt phiếu", "status": "done",
   "code": "ApproveAsync", "evidence": [{"file": "src/Approve.cs", "line": 42}]},
  {"ref": "FR-02", "kind": "FR", "spec": "Gửi mail khi duyệt", "status": "missing",
   "code": "", "evidence": [], "severity": "high", "fix": "Thêm gửi mail"},
  {"ref": "AC-01", "kind": "AC", "spec": "Chỉ trưởng kho duyệt", "status": "Partial",
   "code": "Kiểm role nhưng thiếu kho", "evidence": ["src/Perm.cs:10"]},
  {"ref": "AC-02", "kind": "weird", "spec": "Sắp xếp mới nhất trước", "status": "conflict",
   "code": "Sắp xếp cũ nhất trước"}
 ],
 "questions": [{"ref": "AC-01", "text": "Phó kho có được duyệt?", "why": "spec không nói",
                "options": ["Có", "Không"]}],
 "risks": [{"severity": "high", "title": "Thiếu authorize", "detail": "API mở", "file": "a.cs"}],
 "extras": ["Có export Excel không có trong spec (src/Export.cs:5)"]}
```"""


def test_parse_reads_the_last_contract_and_normalises_values():
    result, prose = rs.parse(_REPLY, rs.KIND_SPEC)
    assert result.parsed and prose.startswith("Phân tích ngắn")
    assert [t.status for t in result.traces] == ["done", "missing", "partial", "conflict"]
    assert result.traces[2].evidence[0].where == "src/Perm.cs:10"   # "file:line" string form
    assert result.traces[3].kind == "NOTE"                          # unknown kind folded
    assert result.questions[0].options == ["Có", "Không"]
    assert result.extras and result.risks[0].severity == "high"


def test_coverage_counts_partial_as_half_and_gaps_are_the_rows_to_act_on():
    result, _ = rs.parse(_REPLY, rs.KIND_SPEC)
    assert result.score(rs.KIND_SPEC) == 37.5          # (1 + 0.5) / 4
    assert rs.gaps(result, rs.KIND_SPEC) == [1, 2, 3]


def test_report_score_is_share_still_valid_ignoring_unclear():
    reply = '```json\n{"traces": [' + ",".join(
        f'{{"ref": "#{i}", "status": "{s}"}}' for i, s in
        enumerate(["confirmed", "fixed", "false_positive", "unclear"])) + "]}\n```"
    result, _ = rs.parse(reply, rs.KIND_REPORT)
    assert result.score(rs.KIND_REPORT) == round(100 / 3, 1)
    assert rs.gaps(result, rs.KIND_REPORT) == [0]
    # a spec status word is not valid for a report
    bad, _ = rs.parse('```json\n{"traces": [{"status": "done"}]}\n```', rs.KIND_REPORT)
    assert bad.traces[0].status == "unclear"


def test_a_reply_without_the_contract_is_kept_not_lost():
    result, prose = rs.parse("Chỉ là văn bản, không có JSON.", rs.KIND_SPEC)
    assert not result.parsed and result.answer == prose
    assert result.score(rs.KIND_SPEC) is None           # no matrix → no fake 0%


def test_raw_newlines_inside_json_strings_still_parse():
    nl = chr(10)
    reply = ("```json" + nl + '{"verdict": "v", "answer": "| a | b |' + nl + "|---|---|" + nl
             + '| 1 | 2 |"}' + nl + "```")
    result, _ = rs.parse(reply, rs.KIND_QUESTION)
    assert result.parsed and result.answer.count(nl) == 2


def test_round_trip_through_storage():
    result, _ = rs.parse(_REPLY, rs.KIND_SPEC)
    back = rs.load(result.to_json())
    assert back.traces[0].evidence[0].file == "src/Approve.cs"
    assert back.score(rs.KIND_SPEC) == result.score(rs.KIND_SPEC)


def test_ado_comment_has_the_four_parts_and_lists_only_what_needs_attention():
    result, _ = rs.parse(_REPLY, rs.KIND_SPEC)
    html = rs.render_ado_comment("Duyệt phiếu", result, link="http://x/review/1")
    for part in ("1. Kết luận", "2. Mục spec cần xử lý", "3. Hành vi ngoài spec", "4. Cần BA"):
        assert part in html
    assert "Gửi mail khi duyệt" in html and "Cho phép duyệt phiếu" not in html   # done row omitted
    assert "37.5%" in html and "<script" not in html


def test_markdown_export_escapes_pipes_and_carries_evidence():
    result, _ = rs.parse(_REPLY.replace("Cho phép duyệt phiếu", "A | B"), rs.KIND_SPEC)
    md = rs.render_markdown("T", rs.KIND_SPEC, result)
    assert "A \\| B" in md and "`src/Approve.cs:42`" in md and "## Câu hỏi cho BA" in md


def test_html_in_agent_text_is_escaped_in_the_ado_comment():
    result = rs.ReviewResult(verdict="<img src=x onerror=alert(1)>", parsed=True)
    assert "<img" not in rs.render_ado_comment("t", result)


def test_review_loop_mode_is_its_own_thing():
    loop = ScheduledLoop(name="r", mode="review", cron="1 1 * * 1", prompt="x")
    assert loop.is_review and not loop.is_report


# ── service + pages, with the agent faked ─────────────────────────────────────

def _app(tmp_path, monkeypatch, reply=_REPLY):
    spec_dir = tmp_path / "ws" / "specs" / "approve"
    spec_dir.mkdir(parents=True)
    (spec_dir / "approve-spec.md").write_text("# Duyệt phiếu\nADO: #4242\n\nFR-01 ...", "utf-8")
    cfg = Settings(dry_run=True, workspace_directory=str(tmp_path / "ws"),
                   database_url=f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
    calls = []

    async def fake_agent(self, prompt, *, resume=None, turns=None, cwd=None):
        calls.append((prompt, resume))
        text = "Câu trả lời tiếp theo `src/a.cs:1`" if resume else reply
        return SimpleNamespace(text=text, session_id="sess-1", cost_usd=0.12, is_error=False)

    monkeypatch.setattr(svc_mod.ReviewStudio, "_agent", fake_agent)
    return create_app(cfg), calls


def _wait(client, rid, want=("done", "failed")):
    for _ in range(100):
        if client.get(f"/dashboard/review/{rid}/status").json()["status"] in want:
            return
        asyncio.run(asyncio.sleep(0.02))
    raise AssertionError("review did not finish")


def test_spec_review_end_to_end(tmp_path, monkeypatch):
    app, calls = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        hub = client.get("/dashboard/review").text
        assert "Duyệt phiếu" in hub and "specs/approve/approve-spec.md" in hub
        r = client.post("/dashboard/review/start", data={
            "kind": "spec", "target": "specs/approve/approve-spec.md", "focus": "phân quyền"},
            follow_redirects=False)
        rid = int(r.headers["location"].rsplit("/", 1)[1])
        _wait(client, rid)
        page = client.get(f"/dashboard/review/{rid}").text
        assert "38%" in page and "Gửi mail khi duyệt" in page and "Phó kho có được duyệt?" in page
        assert "src/Approve.cs:42" in page and "#4242" in page
        # the prompt carried the spec, the linked item and the focus
        assert "FR-01" in calls[0][0] and "#4242" in calls[0][0] and "phân quyền" in calls[0][0]

        md = client.get(f"/dashboard/review/{rid}/export.md").text
        assert md.startswith("# 🧭 Rà soát: Duyệt phiếu")

        # follow-up resumes the same agent session
        client.post(f"/dashboard/review/{rid}/ask", data={"question": "Rủi ro nhất?"})
        for _ in range(100):
            if not client.get(f"/dashboard/review/{rid}/status").json()["pending"]:
                break
            asyncio.run(asyncio.sleep(0.02))
        assert calls[-1][1] == "sess-1"
        assert "Câu trả lời tiếp theo" in client.get(f"/dashboard/review/{rid}").text


def test_filing_and_comment_go_to_ado_with_the_gap_tag(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, monkeypatch)
    created, comments = [], []
    with TestClient(app) as client:
        c = app.state.container

        async def fake_create(**kw):
            created.append(kw)
            return 900 + len(created)

        async def fake_comment(item_id, html):
            comments.append((item_id, html))
            return True

        monkeypatch.setattr(c.ado, "create_work_item", fake_create)
        monkeypatch.setattr(c.ado, "add_comment", fake_comment)
        r = client.post("/dashboard/review/start", data={
            "kind": "spec", "target": "specs/approve/approve-spec.md"}, follow_redirects=False)
        rid = int(r.headers["location"].rsplit("/", 1)[1])
        _wait(client, rid)
        client.post(f"/dashboard/review/{rid}/file", data={"row": ["1", "3"], "item_type": "Task"})
        assert len(created) == 2 and all(k["tag"] == "review-gap" for k in created)
        assert created[0]["parent_id"] == 4242
        # filed rows are not filed twice
        client.post(f"/dashboard/review/{rid}/file", data={"row": ["1"], "item_type": "Task"})
        assert len(created) == 2
        stored = json.loads(client.get(f"/dashboard/review/{rid}/result.json").text)
        assert stored["result"]["traces"][1]["filed_as"] == 901

        client.post(f"/dashboard/review/{rid}/comment", data={"item_id": "4242"})
        assert comments and comments[0][0] == 4242 and "1. Kết luận" in comments[0][1]


def test_question_and_bad_input(tmp_path, monkeypatch):
    reply = '```json\n{"verdict": "Ở PermissionService", "answer": "**Kiểm** tại X", ' \
            '"references": [{"file": "src/P.cs", "line": 7}]}\n```'
    app, _ = _app(tmp_path, monkeypatch, reply)
    with TestClient(app) as client:
        r = client.post("/dashboard/review/start", data={"kind": "question",
                                                         "question": "Quyền kiểm ở đâu?"},
                        follow_redirects=False)
        rid = int(r.headers["location"].rsplit("/", 1)[1])
        _wait(client, rid)
        page = client.get(f"/dashboard/review/{rid}").text
        assert "<strong>Kiểm</strong>" in page and "src/P.cs:7" in page
        bad = client.post("/dashboard/review/start", data={"kind": "spec", "target": "../etc"},
                          follow_redirects=False)
        assert bad.headers["location"] == "/dashboard/review"


def test_restart_fails_what_was_left_running(tmp_path):
    from ai_autopilot.container import Container

    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")

    async def go():
        c = Container(cfg)
        await c.startup()
        rid = await c.review_repo.create(kind="spec", target="a", title="a")
        await c.review_repo.update(rid, status="running")
        assert await c.review_repo.fail_unfinished() == 1
        row = await c.review_repo.get(rid)
        await c.shutdown()
        return row

    row = asyncio.run(go())
    assert row.status == "failed" and "Interrupted" in row.error
