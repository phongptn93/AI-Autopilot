"""Tests for spec_impact: spec edit -> which test cases go stale."""

from __future__ import annotations

from ai_autopilot import spec_impact as si


def ac(i: str, t: str) -> dict[str, str]:
    return {"id": i, "text": t}


def test_identical_specs_have_no_changes() -> None:
    """Re-saving an unchanged spec must not raise a SPEC-CHANGED alarm."""
    old = [ac("AC-01", "Hiển thị thông báo lỗi"), ac("AC-02", "Lưu thành công")]
    imp = si.diff(old, list(old))
    assert not imp.has_changes
    assert imp.unchanged == 2


def test_case_and_whitespace_only_edit_is_not_a_change() -> None:
    """A re-flowed or re-cased paragraph must not wake QC up."""
    imp = si.diff([ac("AC-01", "Hiển thị  thông báo lỗi")], [ac("AC-01", "hiển thị thông\nbáo LỖI ")])
    assert not imp.has_changes
    assert imp.unchanged == 1


def test_text_edit_with_same_id_is_changed_with_before_after() -> None:
    """A real wording change must keep both versions so QC sees what moved."""
    imp = si.diff([ac("AC-01", "Tối đa 3 lần")], [ac("AC-01", "Tối đa 5 lần")])
    assert imp.changed == [{"id": "AC-01", "before": "Tối đa 3 lần", "after": "Tối đa 5 lần"}]
    assert imp.changed_ids == ["AC-01"]


def test_new_ac_is_added() -> None:
    """A new criterion has no test case yet; it is reported as added, not changed."""
    imp = si.diff([ac("AC-01", "a")], [ac("AC-01", "a"), ac("AC-02", "b")])
    assert imp.added == [ac("AC-02", "b")]
    assert not imp.changed and not imp.removed


def test_deleted_ac_is_removed() -> None:
    """A deleted AC is the worst case: its test cases test something that no longer exists."""
    imp = si.diff([ac("AC-01", "a"), ac("AC-02", "b")], [ac("AC-01", "a")])
    assert imp.removed == [ac("AC-02", "b")]
    assert imp.changed_ids == ["AC-02"]


def test_reordering_is_not_a_change() -> None:
    """Moving criteria around in the document changes nothing about behaviour."""
    a, b = ac("AC-01", "một"), ac("AC-02", "hai")
    assert not si.diff([a, b], [b, a]).has_changes


def test_id_zero_padding_matches_across_snapshots() -> None:
    """AC-1 in one version and AC-01 in the other is the same criterion."""
    imp = si.diff([ac("AC-1", "x")], [ac("AC-01", "x")])
    assert not imp.has_changes and imp.unchanged == 1


def test_unnamed_identical_text_is_unchanged() -> None:
    """Unnamed items with equal text (modulo case) are the same criterion."""
    imp = si.diff([ac("", "Người dùng thấy danh sách")], [ac("", "người dùng thấy  danh sách")])
    assert not imp.has_changes


def test_unnamed_small_typo_fix_is_changed() -> None:
    """Rule: similarity >= 0.85 pairs the items, but different text still counts as changed."""
    old = [ac("", "Hệ thống hiển thị thông báo khi mật khẩu sai")]
    new = [ac("", "Hệ thống hiển thị thông báo khi mật khẩu sai!")]
    imp = si.diff(old, new)
    assert len(imp.changed) == 1 and not imp.added and not imp.removed
    assert imp.changed[0]["id"] == ""


def test_unnamed_dissimilar_text_is_removed_and_added() -> None:
    """Below the similarity threshold the items are different criteria, not an edit."""
    imp = si.diff([ac("", "Xuất báo cáo ra Excel")], [ac("", "Khoá tài khoản sau 5 lần sai")])
    assert len(imp.removed) == 1 and len(imp.added) == 1 and not imp.changed


def test_changed_ids_ignore_unnamed() -> None:
    """Unnamed changes cannot be linked to a case by id, so they are not in changed_ids."""
    imp = si.diff([ac("", "abcdefghij klm")], [ac("", "abcdefghij klmn")])
    assert imp.changed and imp.changed_ids == []


def test_affected_id_normalisation() -> None:
    """AC-1 in a case must match AC-01 in the spec, or the case is wrongly skipped."""
    imp = si.diff([ac("AC-01", "a"), ac("AC-02", "b")], [ac("AC-01", "A2"), ac("AC-02", "b")])
    out = si.affected_cases(imp, [{"title": "TC login", "ac": "AC-1"}, {"title": "TC2", "ac": "AC-2"}])
    assert [c["title"] for c in out] == ["TC login"]
    assert "AC-1" in out[0]["reason"]


def test_title_mention_affected_only_when_that_ac_changed() -> None:
    """A title citing AC-02 is flagged when AC-02 changes, not when only AC-01 does."""
    cases = [{"title": "Kiểm tra AC-02 hiển thị"}, {"title": "Kiểm tra ac-01"}]
    imp = si.diff([ac("AC-01", "a"), ac("AC-02", "b")], [ac("AC-01", "a"), ac("AC-02", "c")])
    assert [c["title"] for c in si.affected_cases(imp, cases)] == ["Kiểm tra AC-02 hiển thị"]
    imp2 = si.diff([ac("AC-01", "a"), ac("AC-02", "b")], [ac("AC-01", "z"), ac("AC-02", "b")])
    assert [c["title"] for c in si.affected_cases(imp2, cases)] == ["Kiểm tra ac-01"]


def test_ac_field_list_parsing() -> None:
    """'AC-01, AC-03' must be read as two links, so a case covering AC-03 is caught."""
    imp = si.diff([ac("AC-03", "a")], [ac("AC-03", "b")])
    out = si.affected_cases(imp, [{"title": "T", "ac": "AC-01, AC-03"}, {"title": "U", "ac": "AC-01 AC-02"}])
    assert [c["title"] for c in out] == ["T"]


def test_token_must_be_whole() -> None:
    """AC-1 must not match inside AC-10 or inside a longer word."""
    imp = si.diff([ac("AC-1", "a"), ac("AC-10", "b")], [ac("AC-1", "A!"), ac("AC-10", "b")])
    out = si.affected_cases(imp, [{"title": "x", "ac": "AC-10"}, {"title": "y", "ac": "AC-1"}])
    assert [c["title"] for c in out] == ["y"]


def test_removed_ac_reason_says_removed() -> None:
    """QC needs to know the AC is gone, not merely reworded."""
    imp = si.diff([ac("AC-02", "b")], [])
    out = si.affected_cases(imp, [{"title": "t", "ac": "AC-02"}])
    assert "xoá" in out[0]["reason"]


def test_no_linkage_returns_every_case() -> None:
    """With no AC linkage at all we cannot say which cases are safe: review all of them."""
    imp = si.diff([ac("AC-01", "a")], [ac("AC-01", "b")])
    out = si.affected_cases(imp, [{"title": "A"}, {"title": "B"}])
    assert [c["title"] for c in out] == ["A", "B"]
    assert all(c["reason"] == si.NO_LINKAGE_REASON for c in out)


def test_no_changes_returns_nothing_even_without_linkage() -> None:
    """Unchanged spec => no work for QC, even when cases are unlinked."""
    imp = si.diff([ac("AC-01", "a")], [ac("AC-01", "a")])
    assert si.affected_cases(imp, [{"title": "A"}]) == []


def test_only_added_acs_flag_no_linked_case() -> None:
    """Adding an AC invalidates nothing that already exists."""
    imp = si.diff([ac("AC-01", "a")], [ac("AC-01", "a"), ac("AC-02", "b")])
    assert si.affected_cases(imp, [{"title": "x", "ac": "AC-01"}]) == []


def test_hash_text_stable_under_whitespace_and_distinct_otherwise() -> None:
    """Re-flowing must not change the hash; any wording or case change must."""
    assert si.hash_text("a  b\n c") == si.hash_text(" a b c ")
    assert si.hash_text("abc") != si.hash_text("abd")
    assert si.hash_text("abc") != si.hash_text("ABC")
    assert len(si.hash_text("")) == 64


def test_snapshot_from_spec_text() -> None:
    """snapshot() must expose ids and text of the criteria extract_acs finds."""
    spec = "## Acceptance Criteria\n- AC-01: Hiển thị lỗi\n- AC-02: Lưu thành công\n"
    snap = si.snapshot(spec)
    assert [s["id"] for s in snap] == ["AC-01", "AC-02"]
    assert all(s["text"] for s in snap)
    assert not si.diff(snap, si.snapshot(spec)).has_changes


def test_html_escaping_of_ac_and_case_title() -> None:
    """User text with markup must never reach a work-item comment unescaped (XSS)."""
    imp = si.diff([ac("AC-01", "a")], [ac("AC-01", "<script>alert(1)</script>")])
    html_out = si.render_comment("<script>t</script>", imp, [{"title": "<script>x</script>", "reason": "r"}])
    assert "<script>" not in html_out
    assert html_out.count("&lt;script&gt;") >= 3
    assert html_out.startswith("<p><b>" + si.COMMENT_PREFIX)


def test_comment_cap_with_more_marker() -> None:
    """A huge change must not produce an unreadable wall of text."""
    new = [ac(f"AC-{i:02d}", "x") for i in range(1, 21)]
    imp = si.diff([], new)
    out = si.render_comment("t", imp, [])
    assert out.count("<li>") == si.MAX_LIST + 1
    assert "… và 5 mục nữa" in out


def test_comment_no_changes() -> None:
    """The renderer must still produce a sane message for an empty impact."""
    out = si.render_comment("t", si.Impact(), [])
    assert si.COMMENT_PREFIX in out and "Không có thay đổi" in out


def test_markdown_table_escapes_pipe() -> None:
    """A '|' inside an AC would otherwise split the table cell and corrupt the row."""
    imp = si.diff([ac("AC-01", "a | b")], [ac("AC-01", "c | d")])
    md = si.render_markdown("t", imp, [{"title": "x | y", "reason": "r"}])
    assert "a \\| b" in md and "c \\| d" in md and "x \\| y" in md
    assert "| Loại | Mục | Hiện tại | Điều chỉnh |" in md
    assert md.startswith("**" + si.COMMENT_PREFIX)


def test_markdown_cap() -> None:
    """Markdown output must cap lists like the HTML one."""
    imp = si.diff([], [ac(f"AC-{i}", "x") for i in range(1, 19)])
    assert "… và 3 mục nữa" in si.render_markdown("t", imp, [])
