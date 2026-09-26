"""Security scan — the deterministic core: fingerprints, baseline diff, suppressions,
the builtin rule set, and the scanner output parsers (fed recorded tool output).

None of these tests spawn a scanner binary. The adapters' parsers are pure functions
over the JSON each tool prints, so a fixture recorded once stands in for the tool —
which is also the only way this suite runs the same on a runner with none installed.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ai_autopilot.reports import Finding, parse_findings
from ai_autopilot.security_scan import fingerprint as fp
from ai_autopilot.security_scan import sarif
from ai_autopilot.security_scan import suppressions as sup
from ai_autopilot.security_scan.tools import builtin, gitleaks, sca, semgrep
from ai_autopilot.security_scan.tools.base import strip_dot_slash

FIXTURES = Path(__file__).parent / "fixtures" / "security_scan"


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ── fingerprint ─────────────────────────────────────────────────────────────


def test_fingerprint_ignores_line_number_and_slashes():
    a = Finding(severity="high", title="x", file="src/a.cs", line=10, tool="builtin",
                rule_id="cs-sql-concat", snippet='cmd.CommandText = "SELECT " + q;')
    b = Finding(severity="high", title="x", file="src\\a.cs", line=57, tool="builtin",
                rule_id="cs-sql-concat", snippet='cmd.CommandText  =  "SELECT "  + q;')
    assert fp.fingerprint(a) == fp.fingerprint(b)


def test_fingerprint_differs_by_rule_and_file():
    base = dict(severity="high", title="x", snippet="s", tool="builtin")
    a = Finding(file="a.cs", rule_id="r1", **base)
    b = Finding(file="a.cs", rule_id="r2", **base)
    c = Finding(file="b.cs", rule_id="r1", **base)
    assert len({fp.fingerprint(a), fp.fingerprint(b), fp.fingerprint(c)}) == 3


def test_fingerprint_is_digit_blind_in_snippet():
    # A rotated key's trailing digits or a moved line reference must not be a "new" finding.
    a = Finding(title="k", file="x", tool="builtin", rule_id="s", snippet="Take(rows, 100)")
    b = Finding(title="k", file="x", tool="builtin", rule_id="s", snippet="Take(rows, 250)")
    assert fp.fingerprint(a) == fp.fingerprint(b)
    # …but digits INSIDE an identifier still distinguish it (sha1 is not sha2).
    c = Finding(title="k", file="x", tool="builtin", rule_id="s", snippet="use sha1()")
    d = Finding(title="k", file="x", tool="builtin", rule_id="s", snippet="use sha2()")
    assert fp.fingerprint(c) != fp.fingerprint(d)


def test_dedupe_keeps_worst_severity_and_sorts():
    low = Finding(severity="low", title="t", file="a", tool="x", rule_id="r", snippet="s")
    high = Finding(severity="high", title="t", file="a", tool="x", rule_id="r", snippet="s")
    other = Finding(severity="critical", title="u", file="b", tool="x", rule_id="r", snippet="s")
    out = fp.dedupe([low, other, high])
    assert [f.severity for f in out] == ["critical", "high"]


def test_diff_against_baseline():
    a = Finding(title="a", file="a", tool="x", rule_id="r")
    b = Finding(title="b", file="b", tool="x", rule_id="r")
    fp.stamp([a, b])
    d = fp.diff([a, b], baseline={a.fingerprint, "gone-fp"})
    assert d.new == [b] and d.persisting == [a] and d.fixed == ["gone-fp"]


def test_diff_with_no_baseline_is_all_new():
    a = Finding(title="a", file="a", tool="x", rule_id="r")
    d = fp.diff([a], None)
    assert d.new == [a] and d.fixed == [] and d.persisting == []


def test_strip_dot_slash_keeps_dot_directories():
    assert strip_dot_slash("./.claude/hooks/x.json") == ".claude/hooks/x.json"
    assert strip_dot_slash("src/a.py") == "src/a.py"


# ── suppressions ────────────────────────────────────────────────────────────


def test_suppressions_round_trip_and_expiry(tmp_path):
    s = sup.Suppressions(path=sup.file_for(tmp_path))
    s.add("fp-live", "test fixture key", by="phong", expires=date(2099, 1, 1))
    s.add("fp-old", "accepted for Q1", by="phong", expires=date(2020, 1, 1))
    s.add("fp-forever", "vendor code")
    path = sup.save(s)
    assert path.is_file()

    loaded = sup.load(tmp_path)
    assert loaded.active_fingerprints() == {"fp-live", "fp-forever"}
    assert [e.fingerprint for e in loaded.expired()] == ["fp-old"]
    assert loaded.reason_for("fp-live") == "test fixture key"


def test_suppression_requires_reason(tmp_path):
    s = sup.Suppressions()
    with pytest.raises(ValueError):
        s.add("fp", "   ")


def test_suppressions_malformed_file_is_empty(tmp_path):
    p = sup.file_for(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("suppressions: [ {fingerprint: x, reason: ok", encoding="utf-8")
    assert sup.load(tmp_path).entries == []


# ── builtin rules ───────────────────────────────────────────────────────────


# Built at runtime so this file holds no literal that secret scanners (ours, gitleaks,
# ADO/GitHub push protection) would flag.
_FAKE_AWS = "AKIA" + "IOSFODNN7QZXWVTR"


def _rules(text: str, path: str) -> list[str]:
    return [f.rule_id for f in builtin.scan_text(text, path)]


def test_builtin_private_key_is_critical_even_in_tests():
    pem = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIE...\n"
    out = builtin.scan_text(pem, "tests/fixtures/key.pem")
    assert out and out[0].severity == "critical" and out[0].cwe == "CWE-321"


def test_builtin_password_literal_and_placeholder():
    # autopilot:ignore[secret-password-literal] deliberate scanner test input
    hit = _rules('var pw = "S3cr3tPassw0rd!";\nvar password = "S3cr3tPassw0rd!";', "a.cs")
    assert "secret-password-literal" in hit
    assert _rules('password = "${DB_PASSWORD}"', "a.yaml") == []
    assert _rules('password: "changeme"', "a.yaml") == []


def test_builtin_example_file_downgrades_secret():
    out = builtin.scan_text(f'API_KEY="{_FAKE_AWS}"', ".env.example")
    # AWS id is high-confidence → stays critical; the generic literal rule is demoted.
    sev = {f.rule_id: f.severity for f in out}
    assert sev["secret-aws-access-key"] == "critical"


def test_builtin_csharp_sql_concat_but_not_interpolated_api():
    concat = 'var q = db.Orders.FromSqlRaw("SELECT * FROM T WHERE X=" + x);'
    interp = 'db.Database.ExecuteSqlRaw($"DELETE FROM T WHERE Id={id}");'
    assert "cs-sql-concat" in _rules(concat, "S.cs")
    assert "cs-sql-concat" in _rules(interp, "S.cs")
    assert _rules('db.Orders.FromSqlInterpolated($"SELECT * FROM T WHERE X={x}");', "S.cs") == []


def test_builtin_sast_rules_skip_test_paths_but_secrets_do_not():
    sql = 'db.Database.ExecuteSqlRaw("DELETE FROM T WHERE Id=" + id);'
    assert _rules(sql, "Tests/OrderTests.cs") == []
    assert "secret-aws-access-key" in _rules(_FAKE_AWS, "Tests/x.cs")


def test_builtin_python_rules():
    assert "py-shell-true" in _rules("subprocess.run(cmd, shell=True)", "a.py")
    assert "py-eval-exec" in _rules("value = eval(expr)", "a.py")
    assert _rules("def eval(self, sheet, v):", "a.py") == []          # a method named eval
    assert "py-yaml-load" in _rules("cfg = yaml.load(f)", "a.py")
    assert _rules("cfg = yaml.load(f, Loader=yaml.SafeLoader)", "a.py") == []
    assert "py-requests-verify-false" in _rules("requests.get(u, verify=False)", "a.py")


def test_builtin_typescript_rules():
    assert "ts-bypass-sanitizer" in _rules("this.s.bypassSecurityTrustHtml(x)", "a.ts")
    assert "ts-innerhtml-dynamic" in _rules("el.innerHTML = userHtml;", "a.ts")
    assert _rules("el.innerHTML = '';", "a.ts") == []
    assert "ts-eval" in _rules("setTimeout('doIt()', 10)", "a.js")
    assert "ts-target-blank" in _rules('<a href="x" target="_blank">', "a.html")
    assert _rules('<a href="x" target="_blank" rel="noopener">', "a.html") == []


def test_builtin_config_rules_ignore_private_hosts():
    assert _rules('"push_url": "http://192.168.1.5:3100/x"', "a.json") == []
    assert _rules('"push_url": "http://localhost:3100/x"', "a.json") == []
    assert "cfg-http-not-https" in _rules('"api_url": "http://api.example.com/x"', "a.json")


def test_builtin_secret_snippet_is_masked():
    out = builtin.scan_text(f"token = {_FAKE_AWS}", "a.py")
    assert out and _FAKE_AWS not in out[0].snippet


def test_builtin_scanner_walks_repo_and_skips_node_modules(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = eval(data)\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "b.py").write_text("x = eval(data)\n", encoding="utf-8")
    import asyncio

    run = asyncio.run(builtin.BuiltinScanner().run(str(tmp_path)))
    assert run.status.ran and [f.file for f in run.findings] == ["src/a.py"]


# ── parsers ─────────────────────────────────────────────────────────────────


def test_semgrep_parser():
    out = semgrep.parse_results(_fixture("semgrep.json"))
    assert len(out) == 2
    sqli, xss = out
    assert sqli.tool == "semgrep" and sqli.severity == "high"
    assert sqli.cwe == "CWE-89" and sqli.owasp == "A03:2021"
    assert sqli.rule_id == "entity-framework-fromsqlraw" and sqli.line == 88
    assert sqli.confidence == "high"
    assert xss.cwe == "CWE-79" and xss.severity == "medium"


def test_gitleaks_parser_masks_secret():
    out = gitleaks.parse_results(_fixture("gitleaks.json"))
    assert len(out) == 1
    f = out[0]
    assert f.severity == "critical" and f.cwe == "CWE-798" and f.tool == "gitleaks"
    assert "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ" not in f.snippet
    assert "3fa9c1e2d4" in f.detail
    assert f.file.endswith("appsettings.Production.json") and f.line == 12


def test_npm_audit_parser():
    out = sca.parse_npm(_fixture("npm_audit.json"), "Micro-Frontend")
    by = {f.title.split("@")[0]: f for f in out}
    assert by["lodash"].severity == "high" and by["lodash"].rule_id == "GHSA-p6mc-m468-83gw"
    assert "4.17.21" in by["lodash"].detail and "direct" in by["lodash"].detail
    assert by["minimist"].severity == "critical" and by["minimist"].owasp == "A06:2021"


def test_dotnet_parser():
    out = sca.parse_dotnet(_fixture("dotnet_vulnerable.json"), "Nois.Api.csproj")
    assert {f.severity for f in out} == {"high", "medium"}
    transitive = [f for f in out if "Newtonsoft" in f.title][0]
    assert "transitive" in transitive.detail and transitive.rule_id == "GHSA-5crp-9r3c-p9vr"


def test_pip_audit_parser():
    out = sca.parse_pip_audit(_fixture("pip_audit.json"), "requirements.txt")
    assert len(out) == 1 and out[0].severity == "high" and "41.0.6" in out[0].detail


def test_adapters_report_not_installed_when_binary_missing(monkeypatch):
    import asyncio

    monkeypatch.setattr("ai_autopilot.security_scan.tools.semgrep.which", lambda _b: "")
    monkeypatch.setattr("ai_autopilot.security_scan.tools.gitleaks.which", lambda _b: "")
    for adapter in (semgrep.SemgrepScanner(), gitleaks.GitleaksScanner()):
        assert not adapter.available()
        run = asyncio.run(adapter.run("."))
        assert run.findings == [] and not run.status.ran
        assert "not installed" in run.status.label


# ── report contract & sarif ─────────────────────────────────────────────────


def test_parse_findings_reads_security_keys():
    row = {"severity": "High", "title": "BOLA on GET /orders/{id}", "file": "OrdersController.cs",
           "line": 40, "cwe": 639, "owasp": "API1:2023", "rule_id": "bola", "confidence": "high"}
    text = "done\n```json\n" + json.dumps({"summary": "s", "findings": [row]}) + "\n```"
    summary, out = parse_findings(text)
    assert summary == "s" and out[0].cwe == "CWE-639" and out[0].owasp == "API1:2023"
    assert out[0].rule_id == "bola" and out[0].confidence == "high"
    assert "cwe" in out[0].as_dict() and "tool" not in out[0].as_dict()


def test_sarif_shape():
    f = Finding(severity="high", title="SQLi", file="a.cs", line=3, tool="builtin",
                rule_id="cs-sql-concat", cwe="CWE-89", fingerprint="abc", snippet="x")
    doc = sarif.to_sarif([f], repo="C:/repo")
    assert doc["version"] == "2.1.0"
    run = doc["runs"][0]
    assert run["tool"]["driver"]["name"] == "ai-autopilot/builtin"
    assert run["tool"]["driver"]["rules"][0]["id"] == "cs-sql-concat"
    res = run["results"][0]
    assert res["level"] == "error" and res["partialFingerprints"]["aiAutopilot/v1"] == "abc"
    assert res["locations"][0]["physicalLocation"]["region"]["startLine"] == 3
    json.dumps(doc)  # serialisable


# ── inline ignore · documented examples · multi-line tags ────────────────────

def test_inline_ignore_scoped_to_rule():
    line = "subprocess.run(cmd, shell=True)  # autopilot:ignore[py-shell-true] operator cmd"
    stats: dict = {}
    assert builtin.scan_text(line, "a.py", stats) == []
    assert stats["inline_ignored"] == 1
    # Scoped to another rule → still reported.
    other = "subprocess.run(cmd, shell=True)  # autopilot:ignore[py-eval-exec]"
    assert "py-shell-true" in _rules(other, "a.py")


def test_inline_ignore_on_comment_line_above_but_not_code_line_above():
    above = "# autopilot:ignore[py-eval-exec] trusted formula\nvalue = eval(expr)"
    assert _rules(above, "a.py") == []
    code_above = "x = 1  # autopilot:ignore\nvalue = eval(expr)"
    assert "py-eval-exec" in _rules(code_above, "a.py")
    js = "// autopilot:ignore[ts-innerhtml-dynamic] server partial\nel.innerHTML = html;"
    assert _rules(js, "a.js") == []


def test_inline_ignore_honours_other_scanners_markers():
    assert _rules("subprocess.run(cmd, shell=True)  # nosec", "a.py") == []
    assert _rules(f"k = '{_FAKE_AWS}'  # gitleaks:allow", "a.py") == []


def test_documented_example_credentials_are_allowlisted():
    stats: dict = {}
    doc_key = "AKIAIOSFODNN7" + "EXAMPLE"          # the key printed in the AWS docs
    assert builtin.scan_text(f"k = '{doc_key}'", "a.py", stats) == []
    assert stats["allowlisted"] == 1
    assert "secret-aws-access-key" in _rules(f"k = '{_FAKE_AWS}'", "a.py")


def test_target_blank_reads_the_whole_tag_across_lines():
    wrapped = '<a href="{{ u }}" target="_blank"\n   rel="noopener" class="x">go</a>'
    assert _rules(wrapped, "a.html") == []
    before = '<a rel="noreferrer" href="x" target="_blank">go</a>'
    assert _rules(before, "a.html") == []
    missing = '<a href="x" target="_blank"\n   class="x">go</a>'
    assert "ts-target-blank" in _rules(missing, "a.html")


def test_tool_label_reports_what_was_excused():
    from ai_autopilot.security_scan.tools.base import ToolStatus

    st = ToolStatus("builtin", ran=True, findings=2, extra={"inline_ignored": 3, "allowlisted": 1})
    assert st.label.endswith("(3 inline ignored, 1 allowlisted)")
    assert ToolStatus("builtin", ran=True, findings=0).label == "0 finding(s) in 0.0s"


def test_column_migration_identifiers_are_validated():
    from ai_autopilot.data import database

    assert database._IDENT.fullmatch("security_scans")
    assert database._DDL.fullmatch("VARCHAR(200)") and database._DDL.fullmatch("INTEGER")
    assert not database._IDENT.fullmatch("t; DROP TABLE x")
    assert not database._DDL.fullmatch("TEXT); DROP TABLE x; --")
    for table, column, ddl in database._COLUMN_MIGRATIONS:
        assert database._IDENT.fullmatch(table) and database._IDENT.fullmatch(column), column
        assert database._DDL.fullmatch(ddl), ddl


def test_code_rules_skip_comment_lines_but_secrets_do_not():
    assert _rules("# never call eval(expr) on a formula", "a.py") == []
    assert _rules("// el.innerHTML = userHtml;", "a.ts") == []
    assert "secret-aws-access-key" in _rules(f"# old key {_FAKE_AWS}", "a.py")
    # A code line that merely STARTS with a string literal is not a comment.
    for code in ("'SELECT * FROM T WHERE X=' + x", '"a" + b', "; x = 1", "--i;"):
        assert not builtin._COMMENT_LINE.match(code), code
    for comment in ("# x", "  // x", "/* x", " * x", "<!-- x", "{# x", "-- x"):
        assert builtin._COMMENT_LINE.match(comment), comment
