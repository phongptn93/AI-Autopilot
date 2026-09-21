"""The report renderer. Agent answers are Markdown and were shown as literal text —
a severity-ranked audit arrived on screen with its `###` and `**` still in it, so the
ranking that is the whole point of the document was invisible.

The tests that matter most are the escaping ones: these reports quote real code, real
config and real customer data, and a report that renders a `<script>` out of a file it
was auditing would be a hole in the tool that looks for holes.
"""

from __future__ import annotations

from ai_autopilot.markdown_lite import render


# ── nothing in the source may become markup ──────────────────────────────────


def test_html_in_the_source_is_shown_not_executed():
    out = render("A finding about <script>alert(1)</script> in the login page")
    assert "<script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out


def test_a_javascript_link_is_left_as_text():
    out = render("[click me](javascript:alert(1))")
    assert "href" not in out
    assert "javascript:alert(1)" in out


def test_a_data_url_is_left_as_text():
    assert "href" not in render("[x](data:text/html;base64,PHNjcmlwdD4=)")


def test_a_real_link_opens_safely():
    out = render("see [the PR](https://dev.azure.com/org/_git/repo/pullrequest/12)")
    assert 'href="https://dev.azure.com/org/_git/repo/pullrequest/12"' in out
    assert 'rel="noopener noreferrer"' in out


def test_quotes_in_a_link_cannot_break_out_of_the_attribute():
    out = render('[x](https://a.test/"onmouseover="alert(1))')
    assert 'onmouseover="alert' not in out


# ── the vocabulary the reports actually use ──────────────────────────────────


def test_headings_and_severity_lines():
    out = render("### 🔴 Critical — blocks deploy\n\nsomething bad")
    assert "<h3>🔴 Critical — blocks deploy</h3>" in out
    assert "<p>something bad</p>" in out


def test_bold_and_inline_code():
    out = render("1. **Hardcoded AES key** (`sec-secrets`) — in `Helper.cs:18`")
    assert "<strong>Hardcoded AES key</strong>" in out
    assert "<code>sec-secrets</code>" in out and "<code>Helper.cs:18</code>" in out


def test_emphasis_inside_a_code_span_stays_literal():
    """`**kwargs` is code, not bold — and code spans are resolved first for exactly
    this reason."""
    out = render("pass `**kwargs` through")
    assert "<code>**kwargs</code>" in out
    assert "<strong>" not in out


def test_numbered_and_bulleted_lists():
    out = render("- one\n- two\n\n1. first\n2. second")
    assert out.count("<li>") == 4
    assert "<ul>" in out and "<ol>" in out


def test_a_fenced_code_block_keeps_its_lines_and_loses_no_characters():
    out = render("```python\nif a < b and c > d:\n    run()\n```")
    assert "<pre><code class=\"lang-python\">" in out
    assert "if a &lt; b and c &gt; d:" in out
    assert "    run()" in out


def test_tables_render_as_tables():
    out = render("| Sev | Where |\n|---|---|\n| high | `a.cs:1` |")
    assert "<table>" in out and "<th>Sev</th>" in out
    assert "<td>high</td>" in out


def test_a_pipe_in_ordinary_text_is_not_a_table():
    """A shell command or a regex alternation contains pipes and is not a table — the
    separator row is what makes one."""
    out = render("run `grep -E 'a|b' file | wc -l` to count")
    assert "<table>" not in out


def test_blockquote_and_rule():
    out = render("> careful here\n\n---\n\nafter")
    assert "<blockquote>" in out and "<hr>" in out


def test_blank_input_renders_nothing():
    assert render("") == "" and render("   \n  ") == ""


def test_a_paragraph_keeps_its_line_breaks():
    out = render("line one\nline two")
    assert "<p>line one<br>line two</p>" in out


def test_a_wrapped_list_item_does_not_restart_the_numbering():
    """An audit's "1., 2., 3." came out "1., 1., 1.": each wrapped continuation line
    was read as a paragraph, which closed the list and opened a fresh one."""
    out = render(
        "1. **First finding** — in `a.cs:1`.\n"
        "   Anyone with repo access can read it.\n"
        "2. **Second finding** — in `b.cs:2`.\n"
    )
    assert out.count("<ol>") == 1
    assert out.count("<li>") == 2
    assert "Anyone with repo access can read it." in out.split("</li>")[0]


def test_consecutive_quoted_lines_are_one_paragraph():
    out = render("> bottom line: secrets management\n> and cross-host authorization\n")
    assert out.count("<p>") == 1
    assert "secrets management and cross-host authorization" in out


def test_the_standalone_html_report_renders_too():
    """This is the file that gets forwarded — attached to a mail, opened on a machine
    that has never heard of the dashboard. It is the last place the severity ranking
    should be invisible."""
    from ai_autopilot import reports

    report = reports.Report(
        loop="security-audit",
        body_md="## Summary\n\n**Critical** in `Helper.cs`\n\n- one\n- two",
        findings=[reports.Finding("critical", "a finding", "a.cs", 1, "", "sec")],
    )
    out = reports.render_html(report)
    assert "<h2>Summary</h2>" in out and "<strong>Critical</strong>" in out
    assert "<li>one</li>" in out
    assert "## Summary" not in out
    assert "<script>" not in out


def test_the_standalone_report_cannot_be_used_to_smuggle_markup():
    from ai_autopilot import reports

    report = reports.Report(loop="x", body_md="found <img src=x onerror=alert(1)>",
                            findings=[])
    out = reports.render_html(report)
    assert "onerror=alert(1)>" not in out
    assert "&lt;img src=x onerror=alert(1)&gt;" in out
