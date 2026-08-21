"""Tests for the unified-diff parser (pure — no git, no ADO)."""

from __future__ import annotations

from ai_autopilot import diffs
from ai_autopilot.diffs import parse_unified_diff

MODIFIED = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,6 +10,7 @@ def handler():
     ctx = build()
-    return ctx.run()
+    if ctx.ready:
+        return ctx.run()
     # trailing
"""


def test_modified_file_keeps_line_numbers_on_both_sides():
    """The gutters are the point: "which line" is the question a reviewer opens with."""
    diff = parse_unified_diff(MODIFIED)
    assert len(diff.files) == 1
    f = diff.files[0]
    assert f.path == "src/app.py" and f.status == "modified"
    assert (f.added, f.removed) == (2, 1)

    body = [ln for ln in f.lines if ln.kind != "meta"]
    kinds = [ln.kind for ln in body]
    assert kinds == ["ctx", "del", "add", "add", "ctx"]
    # old side advances on ctx/del, new side on ctx/add — the numbers must not drift.
    assert [ln.old_no for ln in body] == [10, 11, None, None, 12]
    assert [ln.new_no for ln in body] == [10, None, 11, 12, 13]
    assert diff.added == 2 and diff.removed == 1


def test_added_and_deleted_files_are_told_apart():
    text = """diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+one
+two
diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1,1 +0,0 @@
-bye
"""
    files = {f.path: f for f in parse_unified_diff(text).files}
    assert files["new.py"].status == "added" and files["new.py"].added == 2
    assert files["gone.py"].status == "deleted" and files["gone.py"].removed == 1


def test_rename_without_content_change_is_still_reported():
    """A pure rename has headers and no hunks. Dropping it would hide exactly the kind
    of change a reviewer must not miss."""
    text = """diff --git a/old/name.py b/new/name.py
similarity index 100%
rename from old/name.py
rename to new/name.py
"""
    f = parse_unified_diff(text).files[0]
    assert f.status == "renamed" and f.is_renamed
    assert f.old_path == "old/name.py" and f.path == "new/name.py"
    assert f.lines == []


def test_binary_file_claims_no_line_counts():
    """"+0 −0" on a changed binary would read as "nothing changed" — the opposite of
    the truth."""
    text = """diff --git a/logo.png b/logo.png
index 111..222 100644
Binary files a/logo.png and b/logo.png differ
"""
    f = parse_unified_diff(text).files[0]
    assert f.status == "binary" and (f.added, f.removed) == (0, 0)


def test_no_newline_marker_is_not_rendered_as_a_line():
    text = """diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-old
\\ No newline at end of file
+new
"""
    f = parse_unified_diff(text).files[0]
    assert [ln.text for ln in f.lines if ln.kind in ("add", "del")] == ["old", "new"]


def test_a_huge_file_is_cut_but_its_counts_stay_honest(monkeypatch):
    """A generated migration or lockfile can be a hundred thousand lines. The body is
    cut; the header must still say how big the change really was."""
    monkeypatch.setattr(diffs, "MAX_LINES_PER_FILE", 10)
    body = "".join(f"+line {i}\n" for i in range(200))
    f = parse_unified_diff(
        f"diff --git a/big.sql b/big.sql\n--- a/big.sql\n+++ b/big.sql\n@@ -0,0 +1,200 @@\n{body}"
    ).files[0]
    assert f.truncated is True
    assert len(f.lines) <= 11                 # the hunk header plus the cap
    assert f.added == 200                     # counted every line, rendered a few


def test_too_many_files_are_dropped_visibly(monkeypatch):
    monkeypatch.setattr(diffs, "MAX_FILES", 2)
    text = "".join(
        f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n@@ -1 +1 @@\n+x\n"
        for i in range(5)
    )
    diff = parse_unified_diff(text)
    assert len(diff.files) == 2
    assert diff.truncated_files == 3          # said out loud, not silently swallowed


def test_empty_and_garbage_input_do_not_raise():
    assert parse_unified_diff("").is_empty
    assert parse_unified_diff("not a diff at all\n@@ stray hunk @@\n+x").is_empty
