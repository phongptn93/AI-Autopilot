"""Audit reports: the contract with the agent, and what happens when it is not kept.

The findings block is JSON produced by a model, so every test here is really the same
question asked in different ways: does a report survive its machine-readable half being
wrong? Losing the agent's whole answer because a brace was missing is the one failure
this module exists to prevent.
"""

from __future__ import annotations

from ai_autopilot import reports


def _block(payload: str) -> str:
    return f"Here is what I found.\n\n```json\n{payload}\n```"


def test_findings_are_parsed_and_sorted_worst_first():
    text = _block(
        '{"summary": "two problems",'
        ' "findings": [{"severity": "low", "title": "typo", "file": "a.py"},'
        '              {"severity": "critical", "title": "SQL injection",'
        '               "file": "db.py", "line": "42", "agent": "agent-security-reviewer"}]}'
    )
    summary, findings = reports.parse_findings(text)

    assert summary == "two problems"
    # Worst first: the page and the HTML both render in list order, and a critical
    # finding below three nits is a critical finding nobody reads.
    assert [f.severity for f in findings] == ["critical", "low"]
    assert findings[0].line == 42          # "42" from JSON is still a line number
    assert findings[0].agent == "agent-security-reviewer"


def test_severity_spellings_are_normalised_not_dropped():
    """Models are consistent about meaning and inconsistent about spelling."""
    text = _block(
        '{"findings": [{"severity": "BLOCKER", "title": "a"},'
        '              {"severity": "Major", "title": "b"},'
        '              {"severity": "nit", "title": "c"},'
        '              {"severity": "spicy", "title": "d"}]}'
    )
    _, findings = reports.parse_findings(text)

    assert [f.severity for f in findings] == ["critical", "high", "low", "info"]
    # Including the one nobody planned for: "spicy" lands in info rather than vanishing.
    assert findings[-1].title == "d"


def test_a_malformed_block_loses_the_findings_but_never_the_report():
    """The whole point: a truncated answer still produces a readable report."""
    text = "The auth middleware is missing a check.\n\n```json\n{\"findings\": [{\"sev"
    summary, findings = reports.parse_findings(text)

    assert (summary, findings) == ("", [])
    assert "auth middleware" in reports.strip_findings_block(text)


def test_no_block_at_all_is_not_an_error():
    summary, findings = reports.parse_findings("Nothing to report this week.")
    assert (summary, findings) == ("", [])


def test_the_last_block_wins():
    """An agent that quotes the format before using it writes two blocks. The answer is
    the one it ends on — reading the first would report the example as real findings."""
    text = (
        _block('{"summary": "example", "findings": [{"severity": "high", "title": "EXAMPLE"}]}')
        + "\n\nNow the real answer.\n\n"
        + _block('{"summary": "real", "findings": [{"severity": "low", "title": "REAL"}]}')
    )
    summary, findings = reports.parse_findings(text)

    assert summary == "real"
    assert [f.title for f in findings] == ["REAL"]


def test_counts_keep_every_severity_even_at_zero():
    """A column that disappears at zero reads as "not checked", which is the opposite
    of "nothing found"."""
    _, findings = reports.parse_findings(
        _block('{"findings": [{"severity": "high", "title": "x"}]}')
    )
    counts = reports.severity_counts(findings)

    assert set(counts) == set(reports.SEVERITIES)
    assert counts["high"] == 1 and counts["critical"] == 0


def test_an_empty_report_reports_info_as_its_worst():
    assert reports.Report(loop="x").worst == "info"
    assert reports.Report(loop="x").counts["critical"] == 0


def test_the_prompt_carries_the_agents_the_repo_and_the_contract():
    prompt = reports.audit_prompt(
        "Review yesterday's commits.",
        ["agent-pr-reviewer", "agent-security-reviewer"],
        repo="/srv/repo",
    )

    assert "Review yesterday's commits." in prompt
    assert "`agent-pr-reviewer`" in prompt and "`agent-security-reviewer`" in prompt
    assert "/srv/repo" in prompt
    assert "```json" in prompt
    # The read-only contract is stated as well as enforced: an agent that plans around
    # editing files wastes the whole run discovering it cannot.
    assert "READ-ONLY" in prompt


def test_a_loop_with_no_agents_gets_no_empty_delegation_sentence():
    prompt = reports.audit_prompt("Audit this.", [])
    assert "Delegate the work" not in prompt
    assert "```json" in prompt


def test_rendered_html_is_self_contained_and_shows_the_findings():
    _, findings = reports.parse_findings(
        _block('{"summary": "one issue", "findings": [{"severity": "critical",'
               ' "title": "SQL injection", "file": "db.py", "line": 42,'
               ' "detail": "user input concatenated", "agent": "agent-security-reviewer"}]}')
    )
    html = reports.render_html(reports.Report(
        loop="security-audit-weekly", summary="one issue", body_md="Body.",
        findings=findings, agents=["agent-security-reviewer"],
    ))

    assert "SQL injection" in html and "db.py:42" in html
    assert "security-audit-weekly" in html
    # Self-contained: this file's job is to be forwarded, and anything fetched from
    # elsewhere renders as unstyled text on the machine it is opened on.
    for scheme in ("http://", "https://", "//cdn"):
        assert scheme not in html


def test_render_escapes_what_the_agent_wrote():
    """Findings are model output pasted into a page — treat them as data, not markup."""
    _, findings = reports.parse_findings(
        _block('{"findings": [{"severity": "low", "title": "<script>alert(1)</script>"}]}')
    )
    html = reports.render_html(reports.Report(loop="x", findings=findings))

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_the_body_shown_to_a_reader_has_the_machine_half_removed():
    text = _block('{"summary": "s", "findings": []}')
    body = reports.strip_findings_block(text)

    assert "Here is what I found." in body
    assert "```json" not in body
