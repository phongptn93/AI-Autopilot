"""Test guard — tests that cannot fail."""

from __future__ import annotations

import pytest

from ai_autopilot.security_scan.tools import testguard as tg


def _ids(findings):
    return sorted((f.rule_id, f.snippet) for f in findings)


def test_python_no_assertion_and_tautology():
    src = (
        "def test_runs_only():\n    x = 1 + 1\n\n"
        "def test_constant():\n    assert True\n\n"
        "def test_real():\n    assert 1 + 1 == 2\n\n"
        "def test_raises():\n    import pytest\n    with pytest.raises(ValueError):\n        int('x')\n\n"
        "def test_unittest_style(self):\n    self.assertEqual(1, 1)\n"
    )
    assert _ids(tg.scan_python("t.py", src)) == [
        ("test-no-assertion", "py:test_runs_only"), ("test-tautology", "py:test_constant")]


def test_python_helper_that_asserts_counts():
    src = ("def _check(x):\n    assert x > 0\n\n"
           "def test_uses_helper():\n    _check(3)\n\n"
           "def helper_not_a_test():\n    pass\n")
    assert tg.scan_python("t.py", src) == []


def test_python_syntax_error_is_not_a_crash():
    assert tg.scan_python("t.py", "def (:") == []


def test_csharp():
    src = ("public class T {\n"
           "  [Fact]\n  public void Empty() {\n    var x = Sut.Run();\n  }\n"
           "  [Fact]\n  public void Real() {\n    Assert.Equal(3, Sut.Run());\n  }\n"
           "  [Fact]\n  public async Task Const() {\n    Assert.True(true);\n  }\n"
           "  [Fact]\n  public void Fluent() {\n    Sut.Run().Should().Be(3);\n  }\n}\n")
    assert _ids(tg.scan_csharp("T.cs", src)) == [
        ("test-no-assertion", "cs:Empty"), ("test-tautology", "cs:Const")]


def test_typescript():
    src = ("describe('x', () => {\n"
           "  it('does nothing', () => {\n    const a = svc.run();\n  });\n"
           "  it('is real', async () => {\n    expect(svc.run()).toBe(3);\n  });\n"
           "  it('is constant', () => {\n    expect(true).toBe(true);\n  });\n"
           "});\n")
    assert _ids(tg.scan_js("a.spec.ts", src)) == [
        ("test-no-assertion", "js:does nothing"), ("test-tautology", "js:is constant")]


@pytest.mark.parametrize("path,expected", [
    ("tests/test_a.py", True), ("a_test.py", True), ("conftest.py", False),
    ("Foo/BarTests.cs", True), ("Foo/Bar.cs", False),
    ("x/a.spec.ts", True), ("x/a.test.js", True), ("x/a.ts", False)])
def test_is_test_file(path, expected):
    assert tg.is_test_file(path) is expected


@pytest.mark.asyncio
async def test_adapter_respects_the_changed_file_scope(tmp_path):
    (tmp_path / "test_a.py").write_text("def test_x():\n    pass\n", "utf-8")
    (tmp_path / "test_b.py").write_text("def test_y():\n    pass\n", "utf-8")
    run = await tg.TestGuardScanner().run(str(tmp_path), ["test_a.py", "app.py"])
    assert [f.file for f in run.findings] == ["test_a.py"] and run.status.ran
    none = await tg.TestGuardScanner().run(str(tmp_path), ["app.py"])
    assert not none.status.ran and "no test file" in none.status.skipped_reason
