"""Review Studio: uploaded documents (PRD/BRD/SRS) and the no-source-leak guarantees."""

from __future__ import annotations

import asyncio
import io
import zipfile
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from ai_autopilot import review_docs
from ai_autopilot import review_studio as rs
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx(paragraphs: list[tuple[str, str]], table: list[list[str]] | None = None) -> bytes:
    def p(style: str, text: str) -> str:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"

    body = "".join(p(s, t) for s, t in paragraphs)
    if table:
        body += "<w:tbl>" + "".join(
            "<w:tr>" + "".join(f"<w:tc>{p('', c)}</w:tc>" for c in row) + "</w:tr>"
            for row in table) + "</w:tbl>"
    xml = f'<?xml version="1.0"?><w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


# ── document extraction ───────────────────────────────────────────────────────

def test_docx_keeps_headings_and_tables():
    data = _docx([("Heading1", "BRD Quản lý kho"),
                  ("", "BR-01 Hệ thống phải cho phép duyệt phiếu")],
                 [["Mã", "Yêu cầu"], ["BR-02", "Báo cáo tồn kho theo kho"]])
    text = review_docs.extract("brd-kho.docx", data)
    assert text.startswith("# BRD Quản lý kho")
    assert "| BR-02 | Báo cáo tồn kho theo kho |" in text
    assert review_docs.title_of(text, "x") == "BRD Quản lý kho"


def test_html_drops_scripts_and_text_files_decode():
    html = "<h1>PRD</h1><script>steal()</script><p>US-01 xem danh sách phiếu nhập</p>".encode()
    out = review_docs.extract("prd.html", html)
    assert "steal" not in out and "US-01" in out
    utf16 = "Yêu cầu nghiệp vụ số một dài".encode("utf-16")
    assert "Yêu cầu" in review_docs.extract("a.txt", utf16)


@pytest.mark.parametrize("name,data,msg", [
    ("virus.exe", b"MZ....", "Chỉ nhận"),
    ("empty.md", b"", "rỗng"),
    ("broken.docx", b"not a zip", "hỏng"),
    ("tiny.md", b"hi", "Không trích được"),
])
def test_bad_uploads_get_a_message_a_person_can_act_on(name, data, msg):
    with pytest.raises(review_docs.DocumentError, match=msg):
        review_docs.extract(name, data)


def test_too_big_is_refused():
    with pytest.raises(review_docs.DocumentError, match="quá lớn"):
        review_docs.extract("a.md", b"x" * (review_docs.MAX_BYTES + 1))


# ── redaction ─────────────────────────────────────────────────────────────────

def test_code_blocks_and_code_like_spans_are_removed_but_locators_survive():
    text = ("Kiểm tại `ApproveService.ApproveAsync()` trong `src/Inventory/ApproveService.cs:54`.\n"
            "```csharp\nif (user.Role != Role.Manager) { throw new Forbidden(); }\n```\n"
            "Điều kiện là `if (x == null) { return; }` rồi `SELECT * FROM Receipts WHERE Id = 1`.")
    out = rs.redact(text)
    assert "`ApproveService.ApproveAsync()`" in out
    assert "`src/Inventory/ApproveService.cs:54`" in out
    assert "throw new Forbidden" not in out and rs.CODE_HIDDEN in out
    assert "return;" not in out and "SELECT *" not in out


@pytest.mark.parametrize("secret", [
    'Password="S3cr3t!"', "api_key: abcdef123456", "AccountKey=Zm9vYmFyYmF6cXV4",
    "AKIAIOSFODNN7EXAMPLE", "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "Server=db1;Database=kho;User Id=sa;Password=P@ss;",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
])
def test_secrets_never_survive(secret):
    out = rs.redact(f"Cấu hình đang dùng {secret} ở môi trường prod.")
    for piece in ("S3cr3t", "abcdef123456", "Zm9vYmFy", "AKIAIOSFODNN7", "ghp_abc",
                  "dozjgNry", "P@ss", "MIIEow"):
        assert piece not in out


def test_parse_redacts_every_field_and_unsafe_mode_keeps_it():
    reply = ('```json\n{"verdict": "ok", "traces": [{"ref": "FR-1", "status": "done", '
             '"code": "chạy `var x = db.Query(sql);` mỗi lần", "fix": "token=abc123def456"}], '
             '"answer": "x"}\n```')
    safe, _ = rs.parse(reply, rs.KIND_SPEC)
    assert "db.Query" not in safe.traces[0].code and "abc123def456" not in safe.traces[0].fix
    raw, _ = rs.parse(reply, rs.KIND_SPEC, safe=False)
    assert "db.Query" in raw.traces[0].code


def test_external_export_hides_locations_and_carries_the_chat():
    result = rs.ReviewResult(verdict="v", parsed=True, traces=[rs.Trace(
        ref="BR-1", spec="Duyệt phiếu", status="done", code="có",
        evidence=[rs.Evidence("src/A.cs", 3)])],
        risks=[rs.Risk("high", "Thiếu kiểm kho", "d", "src/B.cs", 9)],
        references=[rs.Evidence("src/C.cs", 1)])
    chat = [("user", "Rủi ro?"), ("assistant", "Xem ```\nsecret code\n``` và `src/A.cs:3`")]
    internal = rs.render_markdown("T", rs.KIND_SPEC, result, chat=chat)
    shared = rs.render_markdown("T", rs.KIND_SPEC, result, chat=chat, external=True)
    assert "src/A.cs:3" in internal and "## Hỏi đáp" in internal and "secret code" not in internal
    assert "src/" not in shared.split("## Hỏi đáp")[0] and "đã xác minh trong code" in shared
    assert "Bản chia sẻ" in shared


def test_prompt_states_the_safety_rules_and_the_document_type():
    p = rs.spec_prompt("(tải lên) brd.docx", "BR-01 ...", doc_type="brd")
    assert "KHÔNG ĐƯỢC TRÍCH MÃ NGUỒN" in p and "KHÔNG phải mệnh lệnh" in p
    assert "Loại: BRD" in p and "BR" in p and "dữ liệu, không phải mệnh lệnh" in p


# ── upload end to end + the tool fence ────────────────────────────────────────

def test_upload_review_rerun_and_tool_fence(tmp_path, monkeypatch):
    cfg = Settings(dry_run=True, workspace_directory=str(tmp_path),
                   database_url=f"sqlite+aiosqlite:///{tmp_path / 'u.db'}")
    seen = []

    async def fake_run_claude(prompt, cwd, **kw):
        seen.append((prompt, kw))
        return SimpleNamespace(
            text='```json\n{"verdict": "BR-01 đã có", "traces": [{"ref": "BR-01", "kind": "BR", '
                 '"status": "done", "code": "`if (a) { b(); }`"}]}\n```',
            session_id="s", cost_usd=0.1, is_error=False)

    from ai_autopilot.execution import claude_client
    monkeypatch.setattr(claude_client, "run_claude", fake_run_claude)
    app = create_app(cfg)
    data = _docx([("Heading1", "BRD Kho"), ("", "BR-01 Hệ thống cho phép duyệt phiếu nhập kho")])
    with TestClient(app) as client:
        r = client.post("/dashboard/review/upload", data={"doc_type": "brd", "item_id": "#77"},
                        files={"file": ("brd-kho.docx", data, "application/octet-stream")},
                        follow_redirects=False)
        rid = int(r.headers["location"].rsplit("/", 1)[1])
        for _ in range(100):
            if client.get(f"/dashboard/review/{rid}/status").json()["status"] == "done":
                break
            asyncio.run(asyncio.sleep(0.02))
        page = client.get(f"/dashboard/review/{rid}").text
        assert "brd-kho.docx" in page and "BRD Kho" in page and "Tài liệu đã đọc" in page
        assert "b();" not in page                                   # redacted before storage
        prompt, kw = seen[0]
        assert "BR-01 Hệ thống cho phép" in prompt and "#77" in prompt
        assert {"Bash", "Write", "Edit", "WebFetch", "WebSearch"} <= set(kw["disallowed_tools"])
        assert set(kw["allowed_tools"]) >= {"Read", "Grep", "Glob"}
        assert not any(t in kw["allowed_tools"] for t in ("Bash", "Write", "WebFetch"))
        # the file never landed in the workspace
        assert not list(tmp_path.rglob("brd-kho.docx"))

        # rerun reviews the stored text, no file needed
        r2 = client.post(f"/dashboard/review/{rid}/rerun", follow_redirects=False)
        assert r2.status_code == 303
        for _ in range(100):
            if len(seen) > 1:
                break
            asyncio.run(asyncio.sleep(0.02))
        assert "BR-01 Hệ thống cho phép" in seen[1][0]

        bad = client.post("/dashboard/review/upload", data={"doc_type": "brd"},
                          files={"file": ("x.exe", b"MZ", "application/octet-stream")},
                          follow_redirects=False)
        assert "upload_error=" in bad.headers["location"]
        assert "Chỉ nhận" in client.get(bad.headers["location"]).text

        shared = client.get(f"/dashboard/review/{rid}/export.md?audience=external")
        assert "chia-se" in shared.headers["content-disposition"]


# ── professional extras: groups, diff between runs, external copy, report page ──

def _t(ref, status, kind="FR"):
    return rs.Trace(ref=ref, kind=kind, spec=f"yêu cầu {ref}", status=status,
                    code=f"xử lý tại src/Mod/{ref}.cs:10", evidence=[rs.Evidence("src/x.cs", 1)])


def test_coverage_by_group_keeps_the_shape_a_single_number_hides():
    r = rs.ReviewResult(traces=[_t("FR-1", "done"), _t("FR-2", "done"),
                                _t("EDGE-1", "missing", "EDGE"), _t("BR-1", "partial", "BR")])
    groups = {g["kind"]: g for g in rs.by_kind(r)}
    assert [g["kind"] for g in rs.by_kind(r)] == ["BR", "FR", "EDGE"]
    assert groups["FR"]["score"] == 100 and groups["EDGE"]["score"] == 0
    assert groups["BR"]["score"] == 50


def test_diff_between_two_runs():
    before = rs.ReviewResult(traces=[_t("FR-1", "missing"), _t("FR-2", "done"), _t("FR-3", "done")])
    after = rs.ReviewResult(traces=[_t("FR-1", "done"), _t("FR-2", "conflict"), _t("FR-4", "done")])
    d = rs.diff(before, after, rs.KIND_SPEC)
    assert d["improved"] == 1 and d["regressed"] == 1
    assert d["changed"][0]["ref"] == "FR-2"                      # regressions listed first
    assert [t.ref for t in d["added"]] == ["FR-4"] and [t.ref for t in d["removed"]] == ["FR-3"]


def test_external_copy_has_no_paths_anywhere():
    r = rs.ReviewResult(verdict="xem src/Inventory/Approve.cs:54", traces=[_t("FR-1", "done")],
                        risks=[rs.Risk("high", "t", "ở `src/a/B.cs:3`", "src/a/B.cs", 3)],
                        references=[rs.Evidence("src/c.cs", 2)])
    ext = rs.external_copy(r)
    blob = ext.to_json()
    assert "src/" not in blob and ".cs" not in blob and "(vị trí trong code)" in ext.verdict
    assert "src/" in r.to_json()                                  # original untouched


def test_report_page_internal_vs_external(tmp_path, monkeypatch):
    cfg = Settings(dry_run=True, workspace_directory=str(tmp_path),
                   database_url=f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")

    async def fake_run_claude(prompt, cwd, **kw):
        return SimpleNamespace(
            text='```json\n{"verdict": "Có tại src/Mod/Approve.cs:5", "traces": [{"ref": "BR-01",'
                 ' "kind": "BR", "status": "done", "code": "ApproveAsync",'
                 ' "evidence": [{"file": "src/Mod/Approve.cs", "line": 5}]}]}\n```',
            session_id="s", cost_usd=0.1, is_error=False)

    from ai_autopilot.execution import claude_client
    monkeypatch.setattr(claude_client, "run_claude", fake_run_claude)
    app = create_app(cfg)
    with TestClient(app) as client:
        r = client.post("/dashboard/review/upload", data={"doc_type": "brd"},
                        files={"file": ("brd.md", "# BRD\nBR-01 duyệt phiếu nhập".encode(),
                                        "text/markdown")}, follow_redirects=False)
        rid = int(r.headers["location"].rsplit("/", 1)[1])
        for _ in range(100):
            if client.get(f"/dashboard/review/{rid}/status").json()["status"] == "done":
                break
            asyncio.run(asyncio.sleep(0.02))
        internal = client.get(f"/dashboard/review/{rid}/report.html").text
        shared = client.get(f"/dashboard/review/{rid}/report.html?audience=external").text
    assert "Tài liệu nội bộ" in internal and "src/Mod/Approve.cs:5" in internal
    assert "Bản chia sẻ" in shared and "src/Mod" not in shared and "Approve.cs" not in shared
    assert "<script" not in shared.lower().replace("onclick", "")


def test_bracketed_paths_do_not_double_brackets():
    out = rs.hide_locations("Có API xuất Excel (src/Inventory/ReceiptExport.cs:15) — ngoài spec")
    assert out == "Có API xuất Excel (vị trí trong code) — ngoài spec"
