"""Tests for the customer confirmation page and the acceptance record."""

from __future__ import annotations

import re

from ai_autopilot import customer_pack as cp

SPEC = """# Xuất báo cáo công nợ

## Mục tiêu

Cho phép kế toán **xuất** báo cáo công nợ theo [khách hàng](http://x.y/doc) ra Excel.

Phần hai của mục tiêu.

## Functional Requirements

- FR-01: Có nút Xuất Excel.

## Acceptance Criteria

| ID | Mô tả |
|---|---|
| AC-01 | Khi kế toán bấm `Xuất Excel` thì file tải về có đủ cột |
| AC-02 | Khi không có dữ liệu thì hiển thị thông báo "Không có dữ liệu" |

- AC-03: Khi người dùng không có quyền thì hệ thống từ chối

## Câu hỏi mở

- Có cần xuất theo từng chi nhánh không?
- Định dạng ngày là gì?

## Ngoài phạm vi

- Gửi email tự động

## Bảo mật

- Chỉ kế toán trưởng được xuất
"""


def _full(**kw: object) -> str:
    return cp.render_confirmation_html(cp.summarise_spec(SPEC), **kw)  # type: ignore[arg-type]


def test_summarise_realistic_spec() -> None:
    """A realistic Vietnamese spec must fill every block, or the page misses something."""
    c = cp.summarise_spec(SPEC)
    assert c.title == "Xuất báo cáo công nợ"
    assert c.intent[0].startswith("Cho phép kế toán xuất báo cáo")
    assert len(c.intent) == 2
    assert len(c.scenarios) == 3
    assert c.assumptions == ["Có cần xuất theo từng chi nhánh không?", "Định dạng ngày là gì?"]
    assert c.out_of_scope == ["Gửi email tự động"]
    assert c.security_notes == ["Chỉ kế toán trưởng được xuất"]


def test_scenarios_keep_id_prefix_and_are_not_truncated() -> None:
    """Customer must be able to quote 'AC-02'; cutting a long line would change its meaning."""
    long = "Khi " + "rất dài " * 80 + "thì xong"
    c = cp.summarise_spec(f"# T\n\n## Acceptance Criteria\n\n- AC-01: {long}\n")
    assert c.scenarios == [f"AC-01: {long}"]
    c2 = cp.summarise_spec(SPEC)
    assert c2.scenarios[0].startswith("AC-01: Khi kế toán bấm Xuất Excel")


def test_title_precedence() -> None:
    """Explicit title beats H1, H1 beats the default."""
    assert cp.summarise_spec("# Từ H1\n", title="Tự đặt").title == "Tự đặt"
    assert cp.summarise_spec("# Từ H1\n").title == "Từ H1"
    assert cp.summarise_spec("không có tiêu đề").title == "Yêu cầu"


def test_unchecked_checkbox_becomes_assumption_anywhere() -> None:
    """An unticked box in any section is an open point the customer must see."""
    c = cp.summarise_spec("# T\n\n## Ghi chú\n\n- [ ] Xác nhận mẫu in\n- [x] Đã chốt font\n")
    assert c.assumptions == ["Xác nhận mẫu in"]


def test_unchecked_in_acceptance_section_is_not_assumption() -> None:
    """A checkbox under Acceptance Criteria is a criterion; it must not be asked as a question."""
    c = cp.summarise_spec("# T\n\n## Acceptance Criteria\n\n- [ ] AC-01: Khi A thì B\n")
    assert c.assumptions == []
    assert c.scenarios == ["AC-01: Khi A thì B"]


def test_emphasis_and_links_stripped() -> None:
    """Raw markdown on a customer's paper looks like a bug."""
    c = cp.summarise_spec("# **Đậm** `mã`\n\n## Mục tiêu\n\n__Xem__ [tại đây](http://a.b) nhé\n")
    assert c.title == "Đậm mã"
    assert c.intent == ["Xem tại đây nhé"]


def test_intent_falls_back_to_first_paragraph() -> None:
    """With no recognisable heading the page still says something about the intent."""
    c = cp.summarise_spec("# Tiêu đề\n\nĐoạn mở đầu.\n\n## Khác\n\nĐoạn sau.\n")
    assert c.intent == ["Đoạn mở đầu."]


def test_empty_and_garbage_do_not_raise() -> None:
    """The renderer is fed work-item text of any quality."""
    for junk in ("", "   \n\n", "|||\n---\n```", "####", "\x00\x01 �", "- [ ]", "AC-"):
        c = cp.summarise_spec(junk)
        assert isinstance(c.title, str)
        cp.render_confirmation_html(c)
    assert cp.summarise_spec(None).title == "Yêu cầu"  # type: ignore[arg-type]


def test_confirmation_is_full_document() -> None:
    """Printed from a browser: needs doctype, charset and Vietnamese language."""
    h = _full()
    assert h.startswith("<!doctype html>")
    assert 'lang="vi"' in h
    assert '<meta charset="utf-8">' in h
    assert "@page" in h and "A4" in h


def test_confirmation_sections_and_signature() -> None:
    """Every section the customer is promised, plus the signature sentence."""
    h = _full(item_id=123, spec_path="specs/x.md", version="1.2", generated="2026-10-31")
    for needle in (
        "Chúng tôi sẽ làm gì", "Kịch bản kiểm tra", "Cần bạn xác nhận", "Ngoài phạm vi",
        "Bảo mật &amp; phân quyền", "Work item #123", "specs/x.md", "1.2", "2026-10-31",
        "AC-01: Khi kế toán bấm Xuất Excel", "Người xác nhận:", "Chức danh:", "Ngày:",
        "Chữ ký:", "mô tả đúng", "☐ Có cần xuất theo từng chi nhánh",
    ):
        assert needle in h, needle
    assert "Chưa có tiêu chí chấp nhận" not in h


def test_optional_sections_hidden_when_empty() -> None:
    """Empty headings on a printed page invite questions."""
    h = cp.render_confirmation_html(cp.summarise_spec("# T\n\n## Acceptance Criteria\n\n- AC-01: Khi A thì B\n"))
    assert "Ngoài phạm vi" not in h
    assert "Bảo mật" not in h
    assert "Bạn sẽ thấy gì" not in h


def test_no_scenarios_shows_warning() -> None:
    """Never let a customer sign a page with nothing to confirm."""
    h = cp.render_confirmation_html(cp.summarise_spec("# T\n\nChỉ có mô tả."))
    assert "Chưa có tiêu chí chấp nhận — không thể xác nhận" in h


HOSTILE = (
    '# <script>alert(1)</script>\n\n## Mục tiêu\n\n"><img src=x onerror=alert(1)>\n\n'
    "## Acceptance Criteria\n\n- AC-01: Khi <script>alert(1)</script> thì \"><img src=x onerror=alert(1)>\n"
    "\n## Câu hỏi mở\n\n- <b onclick=evil()>x</b>\n"
)


def test_hostile_text_is_escaped_everywhere() -> None:
    """Spec text is attacker-influenced; a raw tag would run in the customer's browser."""
    c = cp.summarise_spec(HOSTILE)
    h = cp.render_confirmation_html(
        c, spec_path="<script>p</script>", version='"><x>', generated="<g>",
        mockup_url="javascript:alert(1)",
    )
    low = h.lower()
    assert "<script" not in low
    assert "<img" not in low
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in h
    assert "&lt;img src=x onerror=alert(1)&gt;" in h
    assert not re.search(r"<[^>]*\son\w+\s*=", low)


def test_no_external_resources() -> None:
    """Printed page must be self-contained: no fetch, no tracking, no remote fonts."""
    low = _full(mockup_url="https://m.example/x").lower()
    for bad in ("<link", "src=", "<script", "@import", "url(", "<iframe"):
        assert bad not in low, bad


def test_mockup_url_https_and_relative_become_link() -> None:
    """The mockup link is a convenience for the owner of the spec."""
    assert '<a href="https://m.example/a?x=1&amp;y=2">' in _full(mockup_url="https://m.example/a?x=1&y=2")
    assert '<a href="/mockups/a.html">' in _full(mockup_url="/mockups/a.html")


def test_mockup_url_dangerous_is_not_a_link() -> None:
    """javascript:/data:/protocol-relative URLs must stay inert text."""
    for bad in ("javascript:alert(1)", "data:text/html,x", "//evil.example/x", "ftp://x"):
        h = _full(mockup_url=bad)
        assert "<a " not in h, bad
        assert "Bạn sẽ thấy gì" in h
        assert bad.replace("&", "&amp;") in h


def test_no_mockup_url_no_mockup_section() -> None:
    """Do not promise a picture that is not there."""
    assert "Bạn sẽ thấy gì" not in _full()


def test_item_id_zero_has_no_hash_zero() -> None:
    """'Work item #0' on a signed page looks broken."""
    assert "#0" not in _full()
    assert "Work item" not in _full()
    assert "#0" not in cp.render_acceptance_markdown("T", [{"id": "AC-01", "result": "pass"}])
    assert "#0" not in cp.render_acceptance_html("T", [{"id": "AC-01", "result": "pass"}])


def _ac(i: int, result: str) -> dict[str, str]:
    return {"id": f"AC-{i:02d}", "text": f"Khi {i} thì ok", "result": result, "evidence": "ảnh"}


def test_acceptance_ready_matrix() -> None:
    """Ready only when all pass and nothing is undecided; reasons name what blocks it."""
    assert cp.acceptance_ready([]) == (False, ["Không có tiêu chí nào"])
    assert cp.acceptance_ready([_ac(1, "pass"), _ac(2, "pass")]) == (True, [])
    ok, reasons = cp.acceptance_ready([_ac(1, "pass"), _ac(3, "fail"), _ac(5, "blocked")])
    assert not ok and reasons == ["2 tiêu chí chưa đạt: AC-03, AC-05"]
    ok, reasons = cp.acceptance_ready([_ac(1, "")])
    assert not ok and "1 tiêu chí chưa đạt: AC-01" in reasons[0]
    ok, reasons = cp.acceptance_ready([_ac(1, "pass")], [{"summary": "s", "decision": ""}])
    assert not ok and reasons == ["1 điểm lệch spec chưa quyết"]
    ok, _ = cp.acceptance_ready([_ac(1, "pass")], [{"summary": "s", "decision": "accept"}])
    assert ok


def test_acceptance_ready_none_decision_is_undecided() -> None:
    """A missing decision (None) must not slip through as decided."""
    ok, reasons = cp.acceptance_ready([_ac(1, "pass")], [{"summary": "s", "decision": None}])  # type: ignore[dict-item]
    assert not ok and reasons == ["1 điểm lệch spec chưa quyết"]


def test_acceptance_html_ready_verdict() -> None:
    """The positive verdict wording is what the customer signs under."""
    h = cp.render_acceptance_html("Báo cáo", [_ac(1, "pass"), _ac(2, "pass")], build="b42")
    assert "ĐỦ ĐIỀU KIỆN NGHIỆM THU" in h
    assert "CHƯA ĐỦ ĐIỀU KIỆN" not in h
    assert "2/2 tiêu chí đạt" in h
    assert "Bản build b42" in h
    assert "Đạt" in h
    assert h.count("Chữ ký:") == 2 and "BA" in h and "Khách hàng / Chủ sở hữu" in h


def test_acceptance_html_not_ready_lists_reasons() -> None:
    """A failing record must say why, not just 'no'."""
    h = cp.render_acceptance_html(
        "T", [_ac(1, "pass"), _ac(2, "fail")], drifts=[{"summary": "Đổi cột", "decision": ""}]
    )
    assert "CHƯA ĐỦ ĐIỀU KIỆN" in h
    assert "ĐỦ ĐIỀU KIỆN NGHIỆM THU" not in h
    assert "1 tiêu chí chưa đạt: AC-02" in h
    assert "1 điểm lệch spec chưa quyết" in h
    assert "1/2 tiêu chí đạt" in h
    assert "Không đạt" in h


def test_result_labels() -> None:
    """Internal result codes must never reach the customer's paper."""
    rows = [_ac(1, "pass"), _ac(2, "fail"), _ac(3, "blocked"), _ac(4, "")]
    h = cp.render_acceptance_html("T", rows)
    for label in ("Đạt", "Không đạt", "Bị chặn", "Chưa kiểm"):
        assert label in h
    assert ">blocked<" not in h


def test_drift_decision_labels() -> None:
    """Deviation decisions are shown in Vietnamese, including 'not yet decided'."""
    drifts = [
        {"summary": "a", "decision": "update_spec"},
        {"summary": "b", "decision": "fix_code"},
        {"summary": "c", "decision": "accept"},
        {"summary": "d", "decision": ""},
    ]
    h = cp.render_acceptance_html("T", [_ac(1, "pass")], drifts=drifts)
    for label in ("Cập nhật spec", "Sửa code", "Chấp nhận", "Chưa quyết"):
        assert label in h
    assert "update_spec" not in h


def test_acceptance_html_escapes_data() -> None:
    """AC text, evidence, exceptions and drift summaries all come from people."""
    bad = "<script>alert(1)</script>"
    h = cp.render_acceptance_html(
        bad,
        [{"id": bad, "text": bad, "result": "pass", "evidence": '"><img src=x onerror=1>'}],
        drifts=[{"summary": bad, "decision": "<b>"}],
        exceptions=[bad],
    )
    low = h.lower()
    assert "<script" not in low and "<img" not in low
    assert "&lt;script&gt;" in h


def test_acceptance_exceptions_listed() -> None:
    """Accepted exceptions are part of what the customer signs."""
    h = cp.render_acceptance_html("T", [_ac(1, "pass")], exceptions=["Chấp nhận trễ 1 ngày"])
    assert "Chấp nhận trễ 1 ngày" in h


def test_markdown_escapes_pipe_and_ends_with_verdict() -> None:
    """A raw '|' would split a table cell; the last line is the verdict."""
    acs = [{"id": "AC-01", "text": "a | b", "result": "pass", "evidence": "x|y"}]
    md = cp.render_acceptance_markdown("T", acs, build="b1", item_id=7)
    assert "a \\| b" in md and "x\\|y" in md
    assert "#7" in md and "b1" in md
    assert md.strip().splitlines()[-1] == "**ĐỦ ĐIỀU KIỆN NGHIỆM THU**"
    assert "| AC | Kết quả | Bằng chứng |" in md


def test_markdown_not_ready_verdict_has_reasons() -> None:
    """Markdown and HTML must reach the same verdict."""
    md = cp.render_acceptance_markdown("T", [_ac(1, "fail")], drifts=[{"summary": "s", "decision": ""}])
    last = md.strip().splitlines()[-1]
    assert last.startswith("**CHƯA ĐỦ ĐIỀU KIỆN**")
    assert "1 tiêu chí chưa đạt: AC-01" in last and "1 điểm lệch spec chưa quyết" in last


def test_acceptance_empty_acs() -> None:
    """An empty record is never 'ready'."""
    h = cp.render_acceptance_html("T", [])
    assert "CHƯA ĐỦ ĐIỀU KIỆN" in h and "Không có tiêu chí nào" in h
    assert "0/0 tiêu chí đạt" in h
