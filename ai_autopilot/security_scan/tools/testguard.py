"""Test guard — tests that cannot fail.

"Green" only means something if the test could have been red. The commonest way an
AI-written suite is green for no reason is the test that runs the code and then asserts
nothing, or asserts something that is always true. Coverage tools count both as covered.

This looks at the test files in scope and reports, deterministically and without a model:

- ``test-no-assertion`` — a test function whose body contains no assertion at all (and
  calls no helper in the same file that asserts);
- ``test-tautology`` — an assertion that is true by construction (``assert True``,
  ``Assert.True(true)``, ``expect(true).toBe(true)``).

It cannot tell a test that asserts the *wrong thing* — that needs mutation testing or a
reviewer; this narrows what the reviewer has to look at, it does not replace them. Both
rules are ``medium``: they are a prompt to look, not a proof, so they never fail a
default gate by themselves.

Python is parsed with ``ast``; C# and TypeScript/JavaScript are read with brace matching
(good enough for "is there any assertion token in this body", which is all that is asked).
"""

from __future__ import annotations

import ast
import re
import time
import warnings
from pathlib import Path

from ai_autopilot.reports import Finding
from ai_autopilot.security_scan.tools.base import ToolRun, ToolStatus, rel

_SKIP_DIRS = {"node_modules", ".git", "bin", "obj", "dist", ".venv", "venv", "__pycache__"}
_MAX_FILES = 400
_MAX_BYTES = 400_000

_PY_ASSERT_CALL = re.compile(r"^(assert|expect|verify|check|ensure)", re.I)
_CS_METHOD = re.compile(
    r"\[(?:Fact|Theory|Test|TestMethod|TestCase)[^\]]*\][\s\S]*?"
    r"(?:public|internal)\s+(?:async\s+)?[\w<>\[\]?]+\s+(\w+)\s*\([^)]*\)\s*\{")
_CS_ASSERT = re.compile(r"\bAssert\.|\.Should\(|\.Should[A-Z]\w*\(|\bVerify(?:All|NoOtherCalls)?\(|"
                        r"\bThrows(?:Async)?\b|\bExpect\(|\bShould\.")
_CS_TAUTOLOGY = re.compile(r"Assert\.(?:True\(\s*true\s*\)|False\(\s*false\s*\)|"
                           r"Equal\(\s*(\w+)\s*,\s*\1\s*\))")
_JS_TEST = re.compile(r"\b(?:it|test)(?:\.each\([^)]*\))?\s*\(\s*(['\"`])(.+?)\1\s*,\s*"
                      r"(?:async\s*)?(?:\([^)]*\)|\w+)?\s*(?:=>)?\s*\{")
_JS_ASSERT = re.compile(r"\bexpect\s*\(|\bassert\b|\.should\b|\bexpectAsync\b|toHaveBeenCalled|"
                        r"\.rejects\b|\.resolves\b")
_JS_TAUTOLOGY = re.compile(
    r"expect\(\s*(true|1)\s*\)\.(?:toBe|toEqual|toBeTruthy)\(\s*(?:true|1)?\s*\)")


def is_test_file(path: str) -> bool:
    p = path.replace("\\", "/").lower()
    name = p.rsplit("/", 1)[-1]
    if name.endswith(".py"):
        return name.startswith("test_") or name.endswith("_test.py")
    if name.endswith(".cs"):
        return name.endswith(("tests.cs", "test.cs"))
    return name.endswith((".spec.ts", ".test.ts", ".spec.js", ".test.js", ".spec.tsx", ".test.tsx"))


def _body(text: str, open_idx: int) -> str:
    """Text between the brace at ``open_idx`` and its match (strings/comments not parsed —
    a stray brace inside a string only makes the body longer, which errs toward 'asserts')."""
    depth = 0
    for i in range(open_idx, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
    return text[open_idx + 1:]


def _line(text: str, idx: int) -> int:
    return text.count("\n", 0, idx) + 1


# ── per language (pure: text in, findings out) ──────────────────────────────


def _finding(path: str, line: int, rule: str, title: str, detail: str, snippet: str) -> Finding:
    return Finding(severity="medium", tool="testguard", rule_id=rule, file=path, line=line,
                   title=title, detail=detail, confidence="medium", snippet=snippet,
                   cwe="CWE-1164", owasp="")


def _py_asserting_helpers(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("test"):
            continue
        if any(isinstance(n, ast.Assert) for n in ast.walk(node)) or _py_has_assert_call(node):
            names.add(node.name)
    return names


def _call_name(call: ast.Call) -> str:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")


def _py_has_assert_call(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            name = _call_name(n)
            if _PY_ASSERT_CALL.match(name) or name in ("raises", "warns", "fail", "approx"):
                return True
        if isinstance(n, ast.With):
            for item in n.items:
                if isinstance(item.context_expr, ast.Call) and _call_name(item.context_expr) in (
                        "raises", "warns", "assertRaises", "assertLogs"):
                    return True
    return False


def scan_python(path: str, text: str) -> list[Finding]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")      # the file's own invalid escapes are not ours
            tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    helpers = _py_asserting_helpers(tree)
    out: list[Finding] = []
    for node in ast.walk(tree):
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test")):
            continue
        asserts = [n for n in ast.walk(node) if isinstance(n, ast.Assert)]
        called = {_call_name(n) for n in ast.walk(node) if isinstance(n, ast.Call)}
        real = [a for a in asserts if not (isinstance(a.test, ast.Constant) and a.test.value)]
        if asserts and not real and not _py_has_assert_call(node) and not (called & helpers):
            out.append(_finding(path, node.lineno, "test-tautology",
                                f"Test only asserts a constant: {node.name}",
                                "Every assertion in this test is true by construction, so it "
                                "cannot fail whatever the code does.", f"py:{node.name}"))
        elif not asserts and not _py_has_assert_call(node) and not (called & helpers):
            out.append(_finding(path, node.lineno, "test-no-assertion",
                                f"Test has no assertion: {node.name}",
                                "It runs code and checks nothing; it passes unless the code "
                                "raises. Add an assertion on the behaviour it is named for.",
                                f"py:{node.name}"))
    return out


def scan_csharp(path: str, text: str) -> list[Finding]:
    out: list[Finding] = []
    for m in _CS_METHOD.finditer(text):
        body = _body(text, m.end() - 1)
        name = m.group(1)
        only_constants = len(_CS_ASSERT.findall(body)) == len(_CS_TAUTOLOGY.findall(body))
        if _CS_TAUTOLOGY.search(body) and only_constants:
            out.append(_finding(path, _line(text, m.start(1)), "test-tautology",
                                f"Test only asserts a constant: {name}",
                                "Its only assertions are true by construction.", f"cs:{name}"))
        elif not _CS_ASSERT.search(body):
            out.append(_finding(path, _line(text, m.start(1)), "test-no-assertion",
                                f"Test has no assertion: {name}",
                                "It runs code and checks nothing.", f"cs:{name}"))
    return out


def scan_js(path: str, text: str) -> list[Finding]:
    out: list[Finding] = []
    for m in _JS_TEST.finditer(text):
        body = _body(text, m.end() - 1)
        title = m.group(2)
        only_constants = len(_JS_ASSERT.findall(body)) == len(_JS_TAUTOLOGY.findall(body))
        if _JS_TAUTOLOGY.search(body) and only_constants:
            out.append(_finding(path, _line(text, m.start()), "test-tautology",
                                f"Test only asserts a constant: {title}",
                                "Its only expectations are true by construction.", f"js:{title}"))
        elif not _JS_ASSERT.search(body):
            out.append(_finding(path, _line(text, m.start()), "test-no-assertion",
                                f"Test has no assertion: {title}",
                                "It runs code and expects nothing.", f"js:{title}"))
    return out


def scan_file(path: str, text: str) -> list[Finding]:
    if path.endswith(".py"):
        return scan_python(path, text)
    if path.endswith(".cs"):
        return scan_csharp(path, text)
    return scan_js(path, text)


# ── adapter ─────────────────────────────────────────────────────────────────


def _find_tests(repo: str) -> list[str]:
    root = Path(repo)
    out: list[str] = []
    stack = [root]
    while stack and len(out) < _MAX_FILES:
        d = stack.pop()
        try:
            for e in sorted(d.iterdir(), key=lambda p: p.name):
                if e.is_dir():
                    if e.name not in _SKIP_DIRS and not e.name.startswith("."):
                        stack.append(e)
                elif is_test_file(e.name):
                    out.append(rel(repo, str(e)))
        except OSError:
            continue
    return out


class TestGuardScanner:
    __test__ = False          # not a pytest class, whatever the name suggests
    name = "testguard"

    def available(self) -> bool:
        return True

    async def run(self, repo: str, files: list[str] | None = None) -> ToolRun:
        started = time.monotonic()
        st = ToolStatus(self.name)
        paths = [f.replace("\\", "/") for f in files if is_test_file(f)] if files is not None \
            else _find_tests(repo)
        if not paths:
            st.skipped_reason = "no test file in scope"
            return ToolRun([], st)
        findings: list[Finding] = []
        for p in paths[:_MAX_FILES]:
            try:
                full = Path(repo) / p
                if full.stat().st_size > _MAX_BYTES:
                    continue
                findings += scan_file(p, full.read_text(encoding="utf-8-sig", errors="replace"))
            except OSError:
                continue
        st.ran = True
        st.findings = len(findings)
        st.extra["scope"] = f"{len(paths)} test file(s)"
        st.duration_seconds = time.monotonic() - started
        return ToolRun(findings, st)
