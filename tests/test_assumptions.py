"""Assumption ledger: open questions of a spec, answered by key."""

from __future__ import annotations

from ai_autopilot import assumptions as asm

SPEC = """# Xuất công nợ

## Acceptance Criteria
- AC-01: Khi bấm Xuất thì tải về xlsx

## Câu hỏi mở
- [ ] Kỳ báo cáo tối đa bao lâu?
- [x] Định dạng tệp: xlsx
- Có cần chữ ký số không?

## Giả định
- Khách thanh toán theo tháng
- [ ] Chỉ dùng VND

## Ghi chú
Hạn mức: TBD
```
- TBD trong khối code
```
"""


def texts(qs):
    return [q.text for q in qs]


def test_open_questions_and_assumptions_are_extracted_in_order():
    qs = asm.extract(SPEC)
    assert texts(qs) == [
        "Kỳ báo cáo tối đa bao lâu?",
        "Có cần chữ ký số không?",
        "Khách thanh toán theo tháng",
        "Chỉ dùng VND",
        "Hạn mức: TBD",
    ]


def test_a_ticked_checkbox_is_settled_and_not_a_question():
    assert "Định dạng tệp: xlsx" not in texts(asm.extract(SPEC))


def test_kinds_separate_questions_from_assumptions():
    by = {q.text: q.kind for q in asm.extract(SPEC)}
    assert by["Kỳ báo cáo tối đa bao lâu?"] == asm.OPEN_QUESTION
    assert by["Khách thanh toán theo tháng"] == asm.ASSUMPTION
    assert by["Hạn mức: TBD"] == asm.OPEN_QUESTION


def test_a_marker_outside_any_question_section_still_counts():
    qs = asm.extract("## Mô tả\nSố lượng tối đa: TBD\n")
    assert texts(qs) == ["Số lượng tối đa: TBD"]


def test_text_in_code_fences_is_ignored():
    assert all("khối code" not in q.text for q in asm.extract(SPEC))


def test_lines_carry_their_position_in_the_source():
    q = asm.extract(SPEC)[0]
    assert SPEC.splitlines()[q.line - 1].endswith("bao lâu?")


def test_the_same_question_written_twice_is_one_question():
    qs = asm.extract("## Câu hỏi mở\n- Hạn mức là bao nhiêu?\n- hạn mức là  bao nhiêu?\n")
    assert len(qs) == 1


def test_empty_and_missing_input_give_no_questions():
    assert asm.extract("") == [] and asm.extract(None) == []  # type: ignore[arg-type]


# ── keys ───────────────────────────────────────────────────────────────────────


def test_key_ignores_case_and_spacing_but_not_wording():
    assert asm.key_for("Hạn mức  là gì?") == asm.key_for("hạn mức là gì?")
    assert asm.key_for("Hạn mức là gì?") != asm.key_for("Hạn mức là bao nhiêu?")


def test_key_survives_reordering_of_the_spec():
    """The key comes from the wording, not the line number: moving the section must not
    reopen a question that was already answered."""
    before = {q.text: q.key for q in asm.extract(SPEC)}
    moved = "## Mở đầu\nđoạn mới\n\n" + SPEC
    after = {q.text: q.key for q in asm.extract(moved)}
    assert before == after


def test_a_reworded_question_is_a_new_open_question():
    a = asm.extract("## Câu hỏi mở\n- Kỳ tối đa bao lâu?\n")[0]
    b = asm.extract("## Câu hỏi mở\n- Kỳ tối đa mấy tháng?\n")[0]
    assert a.key != b.key


# ── the ledger ─────────────────────────────────────────────────────────────────


def test_an_answered_question_leaves_the_open_list():
    qs = asm.extract(SPEC)
    answers = {qs[0].key: {"answer": "12 tháng", "answered_by": "Lan"}}
    led = asm.ledger(qs, answers)
    assert led.total == len(qs)
    assert qs[0] not in led.open
    assert led.answered[0][0] == qs[0] and led.answered[0][1]["answer"] == "12 tháng"
    assert led.open_count == len(qs) - 1


def test_a_blank_answer_does_not_close_a_question():
    qs = asm.extract(SPEC)
    led = asm.ledger(qs, {qs[0].key: {"answer": "   "}})
    assert qs[0] in led.open and not led.answered


def test_an_answer_for_a_question_no_longer_in_the_spec_is_ignored():
    qs = asm.extract(SPEC)
    led = asm.ledger(qs, {"deadbeef0000": {"answer": "x"}})
    assert led.open_count == len(qs)


# ── the comment ────────────────────────────────────────────────────────────────


def test_comment_has_the_stable_prefix_and_both_sides():
    q = asm.extract("## Câu hỏi mở\n- Kỳ tối đa bao lâu?\n")[0]
    out = asm.render_answer_comment("Công nợ", q, "12 tháng", "Lan")
    assert out.startswith("<div><b>" + asm.COMMENT_PREFIX)
    assert "Kỳ tối đa bao lâu?" in out and "12 tháng" in out and "Lan" in out


def test_comment_escapes_what_people_typed():
    q = asm.Question("k", "<img src=x onerror=1>", asm.OPEN_QUESTION, 1)
    out = asm.render_answer_comment("<b>t</b>", q, "<script>alert(1)</script>", "<i>x</i>")
    assert "<script>" not in out and "<img" not in out and "<b>t</b>" not in out
    assert "&lt;script&gt;" in out


def test_assumption_is_labelled_as_such():
    q = asm.Question("k", "Dùng VND", asm.ASSUMPTION, 1)
    assert "Giả định" in asm.render_answer_comment("", q, "Đúng")
