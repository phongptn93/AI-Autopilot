"""Spec readiness: is a specification precise enough to hand to an agent?"""

from __future__ import annotations

from ai_autopilot import spec_readiness as sr

GOOD = """# Xuất báo cáo công nợ

## Mục tiêu
Kế toán xuất được công nợ theo khách hàng.

## Functional Requirements
| FR | Mô tả |
|---|---|
| FR-01 | Hệ thống cho phép chọn khách hàng và kỳ để xuất báo cáo |
| FR-02 | Hệ thống ghi nhận người xuất và thời điểm xuất |

## Acceptance Criteria
- AC-01 (FR-01): Khi chọn khách hàng và kỳ rồi bấm Xuất, thì hệ thống tải về tệp xlsx
- AC-02 (FR-02): Khi xuất xong thì bản ghi nhật ký hiển thị tên người xuất và giờ xuất
- AC-03 (FR-01): Khi kỳ không có dữ liệu thì hệ thống báo lỗi "Không có dữ liệu"

## Edge cases
- Kỳ rỗng, khách hàng bị khoá

## Bảo mật & Phân quyền
Chỉ vai trò Kế toán trưởng được xuất công nợ.
"""


def codes(r: sr.Readiness) -> list[str]:
    return [f.code for f in r.findings]


# ── the verdict ────────────────────────────────────────────────────────────────


def test_a_precise_spec_is_ready():
    r = sr.check(GOOD)
    assert r.verdict == sr.READY and r.ready
    assert r.score >= 80
    assert r.stats["ac"] == 3 and r.stats["ac_verifiable"] == 3


def test_empty_text_is_not_ready_and_does_not_raise():
    for text in ("", "   \n\n", None):
        r = sr.check(text)  # type: ignore[arg-type]
        assert r.verdict == sr.NOT_READY
        assert "no_ac" in codes(r)


def test_no_acceptance_criteria_is_critical_and_blocks_readiness():
    """Without a single AC there is nothing to verify the work against — whatever else
    the document does well must not be able to outvote that."""
    r = sr.check("# Tính năng\n\n## Mô tả\nHệ thống cho phép người dùng đăng nhập.\n")
    assert r.verdict == sr.NOT_READY
    assert r.critical and r.critical[0].code == "no_ac"


def test_ready_min_is_the_bar_for_a_clean_spec():
    r = sr.check(GOOD, ready_min=101)
    assert r.verdict == sr.NEEDS_WORK          # no critical finding, score below the bar


def test_score_stays_within_bounds():
    awful = "# x\n## AC\n" + "\n".join(f"- AC-{i}: nhanh, thân thiện, v.v TBD" for i in range(1, 30))
    r = sr.check(awful)
    assert 0 <= r.score <= 100
    assert sr.check(GOOD).score <= 100


# ── criteria nobody can check ──────────────────────────────────────────────────


def test_given_when_then_in_english_is_verifiable():
    assert sr._is_verifiable("Given a locked account, when the user logs in, then show an error")


def test_khi_thi_in_vietnamese_is_verifiable():
    assert sr._is_verifiable("Khi đăng nhập sai 5 lần thì khoá tài khoản")
    assert sr._is_verifiable("Nếu thiếu họ tên thì hệ thống từ chối lưu")


def test_a_concrete_observable_result_is_verifiable():
    assert sr._is_verifiable("Hệ thống hiển thị thông báo lỗi khi tệp lớn hơn 5MB")
    assert sr._is_verifiable("API returns 404 for an unknown order")


def test_an_opinion_is_not_verifiable():
    assert not sr._is_verifiable("Trang web nên đẹp")
    assert not sr._is_verifiable("Hệ thống hoạt động ổn định")


def test_the_digits_in_an_ac_id_do_not_make_it_measurable():
    """"AC-02" contains a number. Counting it as "a concrete number" let every named
    criterion through — the mistake this guards against."""
    assert not sr._is_verifiable("AC-02: Màn hình phải nhanh và thân thiện")
    assert not sr._is_verifiable("AC-07 phải hoạt động ổn định")
    assert not sr._is_verifiable("AC-1 (FR-3): Giao diện đẹp mắt")


def test_an_unverifiable_criterion_is_reported_with_its_line():
    text = "## Acceptance Criteria\n- AC-01: Khi bấm Lưu thì hiển thị thông báo\n- AC-02: Giao diện đẹp\n"
    r = sr.check(text)
    bad = [f for f in r.findings if f.code == "ac_not_verifiable"]
    assert len(bad) == 1 and bad[0].line == 3 and "AC-02" in bad[0].message
    assert r.stats["ac_verifiable"] == 1


# ── finding the criteria ───────────────────────────────────────────────────────


def test_criteria_are_found_in_lists_tables_and_bare_ids():
    text = """## Acceptance Criteria
| AC | Mô tả |
|---|---|
| AC-01 | Khi bấm Lưu thì hiển thị thông báo |
- Khi nhập sai thì báo lỗi
1. Khi xoá thì hỏi xác nhận
## Ghi chú
AC-09: Khi hết hạn thì khoá
"""
    acs = sr.extract_acs(text)
    assert [a.id for a in acs] == ["AC-01", "", "", "AC-09"]
    assert acs[0].line == 4


def test_a_table_header_row_is_not_a_criterion():
    acs = sr.extract_acs("## Acceptance Criteria\n| AC | Mô tả |\n|---|---|\n| AC-01 | Khi a thì b |\n")
    assert [a.id for a in acs] == ["AC-01"]


def test_text_inside_code_fences_is_ignored():
    text = "## Acceptance Criteria\n```\n- AC-01: TBD nhanh\n```\n- AC-02: Khi a thì b\n"
    assert [a.id for a in sr.extract_acs(text)] == ["AC-02"]


# ── vague words ────────────────────────────────────────────────────────────────


def test_vague_words_in_a_criterion_are_flagged():
    r = sr.check("## Acceptance Criteria\n- AC-01: Khi mở trang thì tải nhanh chóng và thân thiện\n")
    f = [x for x in r.findings if x.code == "vague_term"]
    assert f and "nhanh chóng" in f[0].message and "thân thiện" in f[0].message


def test_vague_words_in_prose_outside_requirements_are_left_alone():
    """A checker that flags every "nhanh" in a background paragraph gets ignored."""
    text = GOOD.replace("Kế toán xuất được công nợ theo khách hàng.",
                        "Kế toán cần làm việc nhanh và hợp lý hơn.")
    assert "vague_term" not in codes(sr.check(text))


def test_vague_match_is_whole_word_only():
    r = sr.check("## Acceptance Criteria\n- AC-01: Khi bấm Lưu thì hiển thị 'Nhanh gọn' nhanhnhẹn\n")
    # "nhanhnhẹn" must not be read as "nhanh"; only the standalone word counts
    f = [x for x in r.findings if x.code == "vague_term"]
    assert len(f) == 1


# ── orphan requirements ────────────────────────────────────────────────────────


def test_an_fr_no_criterion_refers_to_is_an_orphan():
    text = GOOD.replace("(FR-02)", "").replace("| FR-02 |", "| FR-02 |")
    r = sr.check(text)
    assert [f for f in r.findings if f.code == "orphan_fr"]
    assert r.stats["fr_without_ac"] == 1


def test_no_orphans_when_every_fr_is_referenced():
    assert "orphan_fr" not in codes(sr.check(GOOD))


def test_criteria_that_never_name_an_fr_only_get_an_info_note():
    text = GOOD.replace("(FR-01)", "").replace("(FR-02)", "")
    r = sr.check(text)
    assert "orphan_fr" not in codes(r)
    note = [f for f in r.findings if f.code == "ac_not_traceable"]
    assert note and note[0].severity == sr.INFO


def test_leading_zeros_do_not_hide_a_reference():
    assert "orphan_fr" not in codes(sr.check(GOOD.replace("FR-01", "FR-1").replace("(FR-1)", "(FR-01)")))


# ── open questions ─────────────────────────────────────────────────────────────


def test_tbd_and_unchecked_assumptions_are_open_questions():
    text = GOOD + "\n## Câu hỏi mở\n- [ ] Kỳ báo cáo tối đa bao lâu?\n- [x] Định dạng tệp: xlsx\n- Hạn mức: TBD\n"
    r = sr.check(text)
    assert r.stats["open_questions"] == 2
    assert all(f.severity == sr.WARNING for f in r.findings if f.code == "open_question")


def test_a_checked_item_is_not_an_open_question():
    text = GOOD + "\n## Giả định\n- [x] Đã chốt với khách\n"
    assert "open_question" not in codes(sr.check(text))


# ── security ───────────────────────────────────────────────────────────────────


def test_security_section_is_demanded_when_the_spec_touches_money_and_permissions():
    text = GOOD.replace("## Bảo mật & Phân quyền\nChỉ vai trò Kế toán trưởng được xuất công nợ.\n", "")
    text += "\nNgười dùng cần phân quyền và xem dữ liệu thanh toán.\n"
    assert "no_security" in codes(sr.check(text))


def test_one_passing_keyword_does_not_demand_a_security_section():
    text = GOOD.replace("## Bảo mật & Phân quyền\nChỉ vai trò Kế toán trưởng được xuất công nợ.\n", "")
    text += "\nHiển thị cột tiền.\n"
    assert "no_security" not in codes(sr.check(text))


def test_a_security_section_satisfies_the_check():
    assert "no_security" not in codes(sr.check(GOOD + "\nNgười dùng đăng nhập bằng mật khẩu.\n"))


# ── edge cases ─────────────────────────────────────────────────────────────────


def test_missing_edge_cases_is_an_info_finding():
    text = GOOD.replace("## Edge cases\n- Kỳ rỗng, khách hàng bị khoá\n", "")
    f = [x for x in sr.check(text).findings if x.code == "no_edge_cases"]
    assert f and f[0].severity == sr.INFO


# ── findings are ordered and capped ────────────────────────────────────────────


def test_findings_come_most_severe_first():
    r = sr.check("# x\n- TBD\n")
    order = [f.severity for f in r.findings]
    assert order == sorted(order, key={"critical": 0, "warning": 1, "info": 2}.get)


def test_a_flood_of_vague_words_is_capped_but_still_counted():
    text = "## Acceptance Criteria\n" + "\n".join(f"- AC-{i:02d}: Khi a thì nhanh" for i in range(1, 30))
    r = sr.check(text)
    assert len([f for f in r.findings if f.code == "vague_term"]) == 8
    assert r.stats["vague"] == 29


# ── html specs ─────────────────────────────────────────────────────────────────


def test_html_is_reduced_to_lines_a_reader_would_see():
    html = ("<h2>Acceptance Criteria</h2><ul><li>AC-01: Khi bấm Lưu thì hiển thị OK</li>"
            "<li>AC-02: Giao diện đẹp</li></ul><script>alert('AC-99')</script>")
    text = sr.plain_text_from_html(html)
    assert "alert" not in text
    r = sr.check(text)
    assert r.stats["ac"] == 2 and r.stats["ac_verifiable"] == 1


# ── rendering ──────────────────────────────────────────────────────────────────


def test_markdown_lists_every_finding_and_escapes_pipes():
    r = sr.check("## Acceptance Criteria\n- AC-01: Khi a | b thì nhanh\n")
    md = sr.render_markdown(r, "Báo cáo")
    assert "Báo cáo" in md and "|" in md
    assert "a \\| b" in md or "a | b" not in md.split("Vấn đề")[-1].replace("\\|", "")


def test_comment_carries_the_stable_prefix_and_escapes_the_spec_text():
    r = sr.check('## Acceptance Criteria\n- AC-01: <script>alert(1)</script> nhanh\n')
    out = sr.render_comment(r, "<b>x</b>")
    assert out.startswith("<div><b>" + sr.COMMENT_PREFIX)
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "<b>x</b>" not in out


def test_comment_is_capped_for_a_long_report():
    """Eight vague words, eight open questions and an edge-case note is 17 findings; the
    comment shows twelve and says how many it left out."""
    text = "## Acceptance Criteria\n" + "\n".join(f"- AC-{i:02d}: Khi a thì nhanh" for i in range(1, 30))
    text += "\n## Câu hỏi mở\n" + "\n".join(f"- TBD {i}" for i in range(10))
    out = sr.render_comment(sr.check(text))
    assert out.count("<li>") <= 12
    assert "mục nữa" in out
