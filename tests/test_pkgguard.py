"""Package guard — hallucinated / brand-new dependencies.

Parsers and the judge are pure; the registry layer runs through a duck-typed fake client;
the adapter tests use a real throwaway git repo to prove the one behaviour it exists for:
only what THIS change adds is judged.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_autopilot.security_scan.tools import pkgguard as pg
from ai_autopilot.security_scan.tools import registry

NOW = datetime(2026, 11, 9, tzinfo=UTC)


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p = status, payload

    def json(self):
        return self._p


class _Http:
    """Routes by URL substring → _Resp (or an exception to raise); records every URL."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def get(self, url, **_):
        self.calls.append(url)
        for key, resp in self.routes.items():
            if key in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return _Resp(404)

    async def aclose(self):
        pass


def _pypi(first_days_ago: int, versions=("1.0",)):
    t = (NOW - timedelta(days=first_days_ago)).isoformat()
    return _Resp(200, {"releases": {v: [{"upload_time_iso_8601": t}] for v in versions}})


# ── parsing ─────────────────────────────────────────────────────────────────


def test_requirements_skips_urls_flags_and_reads_pins():
    deps = pg.parse_requirements(
        "# c\n-r base.txt\nrequests==2.31.0\nFlask[async]>=2\ngit+https://x/y.git\n"
        "numpy ; python_version>'3'\n", "requirements.txt")
    assert [(d.name, d.version) for d in deps] == [
        ("requests", "2.31.0"), ("Flask", ""), ("numpy", "")]


def test_pyproject_reads_main_and_optional_groups():
    text = ('[project]\nname="x"\ndependencies=["httpx>=0.27","pydantic==2.7.1"]\n'
            '[project.optional-dependencies]\ndev=["pytest"]\n')
    assert {d.name for d in pg.parse_pyproject(text, "pyproject.toml")} == {
        "httpx", "pydantic", "pytest"}


def test_package_json_ignores_local_and_git_specs():
    text = ('{"dependencies":{"left-pad":"1.3.0","ours":"file:../ours","g":"github:a/b",'
            '"x":"^1.2.0"},"devDependencies":{"jest":"29.0.0"}}')
    deps = {d.name: d.version for d in pg.parse_package_json(text, "package.json")}
    assert deps == {"left-pad": "1.3.0", "x": "", "jest": "29.0.0"}


def test_csproj_package_references():
    text = ('<Project>\n<ItemGroup>\n<PackageReference Include="Serilog" Version="3.1.1" />\n'
            '<PackageReference Include="Dapper" Version="$(DapperV)" />\n</ItemGroup></Project>')
    deps = pg.parse_csproj(text, "a.csproj")
    assert [(d.name, d.version, d.line) for d in deps] == [
        ("Serilog", "3.1.1", 3), ("Dapper", "", 4)]


def test_pypi_names_are_canonicalised():
    assert pg.canonical(pg.PYPI, "Foo_Bar.baz") == "foo-bar-baz"


# ── judging ─────────────────────────────────────────────────────────────────


def _dep(**kw):
    return pg.Dep(**{"eco": pg.PYPI, "name": "somepkg", "manifest": "requirements.txt",
                     "line": 3, **kw})


def test_missing_package_is_high_and_names_the_risk():
    out = pg.judge(_dep(), pg.RegistryInfo(False), is_new=True, min_age_days=14, now=NOW)
    assert [f.rule_id for f in out] == ["pkg-not-found"]
    assert out[0].severity == "high" and "hallucinated" in out[0].detail
    assert out[0].file == "requirements.txt" and out[0].line == 3


def test_unknown_registry_answer_yields_nothing():
    assert pg.judge(_dep(), pg.RegistryInfo(None, error="boom"), is_new=True,
                    min_age_days=14, now=NOW) == []


def test_too_new_only_when_the_change_introduced_it():
    info = pg.parse_pypi(_pypi(3).json())
    assert [f.rule_id for f in pg.judge(_dep(), info, is_new=True, min_age_days=14, now=NOW)] \
        == ["pkg-too-new"]
    assert pg.judge(_dep(), info, is_new=False, min_age_days=14, now=NOW) == []


def test_established_package_is_clean():
    info = pg.parse_pypi(_pypi(900, versions=("1.0", "1.1", "2.0")).json())
    assert pg.judge(_dep(version="1.1"), info, is_new=True, min_age_days=14, now=NOW) == []


def test_low_history_is_medium_not_blocking():
    info = pg.parse_pypi(_pypi(60).json())
    out = pg.judge(_dep(), info, is_new=True, min_age_days=14, now=NOW)
    assert [(f.rule_id, f.severity) for f in out] == [("pkg-low-history", "medium")]


def test_pinned_version_that_was_never_published():
    info = pg.parse_pypi(_pypi(900, versions=("1.0",)).json())
    out = pg.judge(_dep(version="9.9.9"), info, is_new=False, min_age_days=14, now=NOW)
    assert [f.rule_id for f in out] == ["pkg-version-not-found"]


def test_fingerprint_inputs_are_stable_across_line_moves():
    a = pg.judge(_dep(line=3), pg.RegistryInfo(False), is_new=True, min_age_days=14, now=NOW)
    b = pg.judge(_dep(line=40), pg.RegistryInfo(False), is_new=True, min_age_days=14, now=NOW)
    assert a[0].snippet == b[0].snippet == "pypi:somepkg"


# ── registry layer ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lookup_404_is_missing_and_network_error_is_unknown():
    http = _Http({"pypi.org/pypi/ghost/": _Resp(404), "pypi.org/pypi/net/": OSError("down")})
    assert (await pg.lookup(http, pg.PYPI, "ghost", {})).exists is False
    unknown = await pg.lookup(http, pg.PYPI, "net", {})
    assert unknown.exists is None and unknown.error


@pytest.mark.asyncio
async def test_scoped_npm_name_is_encoded_and_nuget_lowercased():
    http = _Http({"registry.npmjs.org": _Resp(
        200, {"versions": {"1.0.0": {}}, "time": {"created": "2020-01-01T00:00:00Z"}}),
        "flatcontainer": _Resp(200, {"versions": ["1.0.0"]})})
    npm = await pg.lookup(http, pg.NPM, "@scope/pkg", {})
    await pg.lookup(http, pg.NUGET, "Serilog", {})
    assert npm.exists and npm.first_published.year == 2020
    assert http.calls[0].endswith("/@scope%2Fpkg")
    assert http.calls[1].endswith("/serilog/index.json")


# ── adapter, against a real git repo ────────────────────────────────────────


def _git(repo: Path, *args: str):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    return tmp_path


@pytest.mark.asyncio
async def test_only_what_the_branch_adds_is_judged(repo):
    (repo / "requirements.txt").write_text(
        "requests==2.31.0\nfastjson-utils-pro\nfresh-lib\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "add deps")
    http = _Http({"/fresh-lib/": _pypi(2), "/fastjson-utils-pro/": _Resp(404)})
    run = await pg.PkgGuardScanner(http=http).run(str(repo))
    assert {f.rule_id for f in run.findings} == {"pkg-not-found", "pkg-too-new"}
    assert all("requests" not in c for c in http.calls)        # pre-existing: not asked
    assert run.status.ran and "added/changed" in run.status.extra["scope"]


@pytest.mark.asyncio
async def test_internal_prefix_never_leaves_the_machine(repo):
    (repo / "requirements.txt").write_text("requests==2.31.0\nacme-core\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "private")
    http = _Http({})
    run = await pg.PkgGuardScanner(internal_prefixes=["acme-"], http=http).run(str(repo))
    assert run.findings == [] and http.calls == []


@pytest.mark.asyncio
async def test_offline_is_reported_as_unchecked_not_clean(repo):
    (repo / "requirements.txt").write_text("requests==2.31.0\nnewthing\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "x")
    run = await pg.PkgGuardScanner(http=_Http({"": OSError("offline")})).run(str(repo))
    assert run.findings == [] and run.status.extra["unchecked"] == 1
    assert "unchecked" in run.status.label


@pytest.mark.asyncio
async def test_no_manifest_in_scope_is_skipped(tmp_path):
    run = await pg.PkgGuardScanner(http=_Http({})).run(str(tmp_path))
    assert not run.status.ran and "no dependency manifest" in run.status.skipped_reason


@pytest.mark.asyncio
async def test_on_the_base_branch_it_checks_existence_only(repo):
    _git(repo, "checkout", "-q", "main")
    http = _Http({"/requests/": _pypi(4000, versions=("2.31.0",))})
    run = await pg.PkgGuardScanner(http=http).run(str(repo))
    assert run.findings == [] and "existence only" in run.status.extra["scope"]


def test_registered_and_default_on():
    from ai_autopilot.config import SecurityScanSettings

    sec = SecurityScanSettings()
    assert "pkgguard" in sec.tools and "pkgguard" in sec.pr_gate_tools
    assert "testguard" in sec.tools
    assert registry(sec)["pkgguard"].min_age_days == 14
