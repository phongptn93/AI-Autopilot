"""SBOM — build a CycloneDX document, audit a stored one against OSV later."""

from __future__ import annotations

import json

import pytest

from ai_autopilot.security_scan import cli, sbom


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p = status, payload

    def json(self):
        return self._p


class _Osv:
    def __init__(self, hits: dict[str, list[str]], fail=False):
        self.hits, self.fail, self.posts = hits, fail, []

    async def post(self, url, json=None, **_):
        self.posts.append(json)
        if self.fail:
            raise OSError("offline")
        out = [{"vulns": [{"id": i} for i in self.hits.get(q["package"]["purl"], [])]}
               for q in json["queries"]]
        return _Resp(200, {"results": out})

    async def get(self, url, **_):
        vid = url.rsplit("/", 1)[1]
        return _Resp(200, {"id": vid, "aliases": ["CVE-2024-0001"], "summary": "bad",
                           "database_specific": {"severity": "HIGH"}})

    async def aclose(self):
        pass


def test_purl_forms():
    assert sbom.purl("pypi", "Foo_Bar", "1.0") == "pkg:pypi/foo-bar@1.0"
    assert sbom.purl("npm", "@scope/pkg", "2.0.0") == "pkg:npm/%40scope/pkg@2.0.0"
    assert sbom.purl("nuget", "Serilog") == "pkg:nuget/serilog"


def test_lockfile_versions_beat_manifest_ranges(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies":{"left-pad":"^1.0.0"}}', "utf-8")
    (tmp_path / "package-lock.json").write_text(json.dumps({"packages": {
        "": {}, "node_modules/left-pad": {"version": "1.3.0"},
        "node_modules/a/node_modules/dep": {"version": "0.2.0"}}}), "utf-8")
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\nflask\n", "utf-8")
    doc = sbom.build_sbom(str(tmp_path), name="demo", version="1.2.0")
    by = {c["name"]: c.get("version") for c in doc["components"]}
    assert by == {"left-pad": "1.3.0", "dep": "0.2.0", "requests": "2.31.0", "flask": None}
    assert doc["bomFormat"] == "CycloneDX" and doc["metadata"]["component"]["version"] == "1.2.0"


@pytest.mark.asyncio
async def test_audit_reports_vulnerable_components_and_counts_unversioned():
    doc = {"bomFormat": "CycloneDX", "components": [
        {"name": "requests", "version": "2.0.0", "purl": "pkg:pypi/requests@2.0.0"},
        {"name": "safe", "version": "1.0", "purl": "pkg:pypi/safe@1.0"},
        {"name": "flask", "purl": "pkg:pypi/flask"}]}
    findings, stats = await sbom.audit_sbom(
        doc, http=_Osv({"pkg:pypi/requests@2.0.0": ["GHSA-aaaa-bbbb-cccc"]}))
    assert stats == {"audited": 2, "unversioned": 1, "vulnerable": 1, "error": ""}
    assert [(f.rule_id, f.cve, f.severity, f.tool) for f in findings] == [
        ("GHSA-aaaa-bbbb-cccc", "CVE-2024-0001", "high", "sca")]


@pytest.mark.asyncio
async def test_offline_audit_says_not_audited_instead_of_clean():
    doc = {"bomFormat": "CycloneDX", "components": [
        {"name": "x", "version": "1", "purl": "pkg:pypi/x@1"}]}
    findings, stats = await sbom.audit_sbom(doc, http=_Osv({}, fail=True))
    assert findings == [] and stats["error"] and stats["audited"] == 0


def test_load_rejects_non_cyclonedx(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"hello": 1}', "utf-8")
    with pytest.raises(ValueError):
        sbom.load_sbom(str(p))


def test_cli_writes_sbom(tmp_path, capsys):
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n", "utf-8")
    out = tmp_path / "out" / "bom.json"
    args = cli.build_parser().parse_args(["--sbom", str(out)])
    assert cli._run_sbom_build(args, tmp_path, str(tmp_path)) == 0
    assert json.loads(out.read_text("utf-8"))["components"][0]["name"] == "requests"
    assert "1 component" in capsys.readouterr().out
