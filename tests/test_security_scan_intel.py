"""Security scan — threat intel (CISA KEV + FIRST EPSS).

The parsers are pure functions over recorded feed shapes (fixtures), the network layer
is exercised through a duck-typed fake client (the ``test_updates.py`` pattern), and the
runner integration proves the one behaviour the feature exists for: a finding that has
been sitting in the baseline fails the gate the day CISA lists its CVE as exploited —
worded as a KEV escalation, not as new code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_autopilot.data import Database, SecurityRepository
from ai_autopilot.reports import Finding
from ai_autopilot.security_scan import ai_sast, intel
from ai_autopilot.security_scan import fingerprint as fp
from ai_autopilot.security_scan import suppressions as sup
from ai_autopilot.security_scan.runner import ScanRequest, run_scan
from ai_autopilot.security_scan.tools import sca
from ai_autopilot.security_scan.tools.base import ToolRun, ToolStatus

FIXTURES = Path(__file__).parent / "fixtures" / "security_scan"


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ── fakes (duck-typed httpx, the test_updates.py pattern) ───────────────────


class _Resp:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


class _Http:
    """Routes GETs by substring of the URL; a route may raise to simulate offline."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    async def get(self, url, params=None, **kw):
        self.calls.append(url)
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        return _Resp(404)

    async def aclose(self):
        pass


def _cfg(**kw) -> SimpleNamespace:
    base = dict(
        intel_enabled=True, intel_cache_hours=24,
        intel_kev_url="https://kev.example/feed.json",
        intel_epss_url="https://epss.example/data",
        intel_osv_url="https://osv.example/v1/vulns",
        verify_enabled=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _routes(**overrides) -> dict:
    routes = {
        "kev.example": _Resp(200, _fixture("kev.json")),
        "epss.example": _Resp(200, _fixture("epss.json")),
        "osv.example": _Resp(200, _fixture("osv_ghsa.json")),
    }
    routes.update(overrides)
    return routes


def _sca_finding(rule_id: str, cve: str = "", severity: str = "high") -> Finding:
    f = Finding(severity=severity, title=f"pkg vulnerable ({rule_id})", file="requirements.txt",
                tool="sca", rule_id=rule_id, snippet="pkg 1.0.0", cve=cve)
    f.fingerprint = fp.fingerprint(f)
    return f


# ── pure parsers ─────────────────────────────────────────────────────────────


def test_parse_kev_reads_catalog_and_optionals():
    catalog = intel.parse_kev(_fixture("kev.json"))
    assert set(catalog) == {"CVE-2023-49083", "CVE-2021-44228", "CVE-2020-7598"}
    log4shell = catalog["CVE-2021-44228"]
    assert log4shell.ransomware and log4shell.due == "2021-12-24"
    # knownRansomwareCampaignUse is optional — absent must read as False, not crash.
    assert catalog["CVE-2020-7598"].ransomware is False
    assert catalog["CVE-2023-49083"].date_added == "2026-09-20"


def test_parse_kev_tolerates_garbage():
    assert intel.parse_kev(None) == {}
    assert intel.parse_kev({"vulnerabilities": [{"cveID": "not-a-cve"}, "junk", None]}) == {}


def test_parse_epss_parses_string_numbers():
    scores = intel.parse_epss(_fixture("epss.json"))
    assert scores["CVE-2023-49083"] == (0.81234, 0.9812)
    assert scores["CVE-2020-7598"][0] == pytest.approx(0.00421)


def test_parse_osv_finds_cve_alias():
    assert intel.parse_osv(_fixture("osv_ghsa.json")) == "CVE-2020-7598"
    assert intel.parse_osv({"id": "GHSA-x", "aliases": ["SNYK-1"]}) == ""
    assert intel.parse_osv(None) == ""


def test_cve_of_reads_field_then_rule_id():
    assert intel.cve_of(_sca_finding("CVE-2024-1234")) == "CVE-2024-1234"
    assert intel.cve_of(_sca_finding("GHSA-vh95-rmgr-6w4m")) == ""
    assert intel.cve_of(_sca_finding("GHSA-x", cve="CVE-2020-7598")) == "CVE-2020-7598"


# ── enrich (pure) ────────────────────────────────────────────────────────────


def test_enrich_kev_escalates_and_keeps_fingerprint():
    f = _sca_finding("CVE-2023-49083")
    before = f.fingerprint
    stats = intel.enrich([f], intel.parse_kev(_fixture("kev.json")),
                         intel.parse_epss(_fixture("epss.json")), {})
    assert stats == {"kev": 1, "epss": 1}
    assert f.severity == "critical" and f.kev and f.kev_due == "2026-10-11"
    assert f.epss == pytest.approx(0.81234)
    assert "CISA KEV" in f.detail and "EPSS 0.81" in f.detail
    assert fp.fingerprint(f) == before  # baseline & suppressions still match


def test_enrich_epss_only_never_touches_severity():
    f = _sca_finding("CVE-2024-9999", cve="CVE-2024-9999", severity="medium")
    intel.enrich([f], {}, {"CVE-2024-9999": (0.93, 0.99)}, {})
    assert f.severity == "medium" and not f.kev
    assert f.epss == 0.93 and "EPSS 0.93" in f.detail


def test_enrich_uses_resolved_cve_map():
    f = _sca_finding("GHSA-vh95-rmgr-6w4m")   # npm-shaped id, no CVE of its own
    intel.enrich([f], intel.parse_kev(_fixture("kev.json")), {},
                 {f.fingerprint: "CVE-2020-7598"})
    assert f.cve == "CVE-2020-7598" and f.kev and f.severity == "critical"


def test_ai_triage_cannot_demote_a_kev_finding():
    f = _sca_finding("CVE-2023-49083")
    f.kev, f.severity = True, "critical"
    demoted = ai_sast.apply_triage(
        [f], {(f.file, None): "false_positive:not reachable"})
    assert demoted == 0
    assert f.severity == "critical" and f.confidence != "low"
    assert "kept: in CISA KEV" in f.detail


def test_from_dict_accepts_cve_but_never_kev():
    row = {"severity": "high", "title": "x", "file": "a", "tool": "sca",
           "cve": "CVE-2024-1234", "kev": True, "epss": 0.99}
    f = Finding.from_dict(row)
    assert f.cve == "CVE-2024-1234"
    assert f.kev is False and f.epss is None      # model output cannot fail the gate
    assert Finding.from_dict({"cve": "evil'); drop--"}).cve == ""


def test_as_dict_keeps_a_zero_epss():
    f = _sca_finding("CVE-2024-1", cve="CVE-2024-1")
    f.epss, f.epss_percentile = 0.0, 0.0
    data = f.as_dict()
    assert data["epss"] == 0.0 and data["epss_percentile"] == 0.0


def test_pip_audit_parser_surfaces_cve_alias():
    out = sca.parse_pip_audit(_fixture("pip_audit.json"), "requirements.txt")
    assert out[0].rule_id == "GHSA-jfhm-5ghh-2f97"   # fingerprint material, unchanged
    assert out[0].cve == "CVE-2023-49083"


# ── run_intel (network via fakes; cache on tmp workspace) ───────────────────


async def test_run_intel_enriches_and_caches(tmp_path: Path):
    findings = [_sca_finding("CVE-2023-49083"), _sca_finding("GHSA-vh95-rmgr-6w4m")]
    http = _Http(_routes())
    st = await intel.run_intel(findings, workspace=str(tmp_path), cfg=_cfg(),
                               store=True, http=http)
    assert st.ran and st.findings == 2            # both CVEs are in the KEV fixture
    assert st.extra["epss_scored"] >= 1
    assert findings[1].cve == "CVE-2020-7598"     # GHSA resolved through OSV
    for name in ("kev.json", "epss.json", "osv.json"):
        assert (tmp_path / ".autopilot" / "intel" / name).is_file()

    # Second run: the KEV feed comes from cache — no network call for it.
    http2 = _Http(_routes())
    st2 = await intel.run_intel([_sca_finding("CVE-2023-49083")], workspace=str(tmp_path),
                                cfg=_cfg(), store=True, http=http2)
    assert st2.ran and "cache" in st2.extra["cache"]
    assert not any("kev.example" in u for u in http2.calls)


async def test_run_intel_no_store_leaves_no_files(tmp_path: Path):
    f = _sca_finding("CVE-2023-49083")
    st = await intel.run_intel([f], workspace=str(tmp_path), cfg=_cfg(),
                               store=False, http=_Http(_routes()))
    assert st.ran and f.kev
    assert not (tmp_path / ".autopilot" / "intel").exists()


async def test_run_intel_offline_is_a_skip_not_an_error(tmp_path: Path):
    f = _sca_finding("CVE-2023-49083")
    http = _Http({"kev.example": ConnectionError("no route to host")})
    st = await intel.run_intel([f], workspace=str(tmp_path), cfg=_cfg(),
                               store=True, http=http)
    assert not st.ran and st.skipped_reason.startswith("offline")
    assert not f.kev and f.severity == "high"     # untouched


async def test_run_intel_stale_cache_survives_an_outage(tmp_path: Path):
    # Prime the cache, then age it past the TTL and cut the network.
    await intel.run_intel([_sca_finding("CVE-2023-49083")], workspace=str(tmp_path),
                          cfg=_cfg(), store=True, http=_Http(_routes()))
    kev_file = tmp_path / ".autopilot" / "intel" / "kev.json"
    raw = json.loads(kev_file.read_text(encoding="utf-8"))
    raw["fetched_at"] = 1.0   # 1970 — as stale as it gets
    kev_file.write_text(json.dumps(raw), encoding="utf-8")

    f = _sca_finding("CVE-2023-49083")
    st = await intel.run_intel([f], workspace=str(tmp_path), cfg=_cfg(),
                               store=True,
                               http=_Http({"kev.example": ConnectionError("down")}))
    assert st.ran and f.kev                        # stale beats nothing
    assert "stale" in st.extra["cache"]


async def test_run_intel_skips_without_sca_findings(tmp_path: Path):
    other = Finding(severity="high", title="x", file="a.py", tool="builtin", rule_id="r")
    st = await intel.run_intel([other], workspace=str(tmp_path), cfg=_cfg(),
                               store=True, http=_Http(_routes()))
    assert not st.ran and st.skipped_reason == "no SCA findings"


# ── runner integration: the baseline-escalation gate ────────────────────────


@dataclass
class _FakeSca:
    """A registry stand-in that reports one vulnerable dependency."""

    name: str = "sca"
    findings: list = field(default_factory=list)

    def available(self) -> bool:
        return True

    async def run(self, repo: str, files=None) -> ToolRun:
        st = ToolStatus(self.name)
        st.ran, st.findings = True, len(self.findings)
        return ToolRun(list(self.findings), st)


@pytest.fixture
async def security_repo(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'sec.db'}")
    await db.create_all()
    try:
        yield SecurityRepository(db)
    finally:
        await db.dispose()


def _wire(monkeypatch, tmp_path: Path, routes: dict):
    """Point the runner's registry at the fake SCA adapter and intel at fake HTTP."""
    finding = Finding(severity="high", title="cryptography@41.0.0 vulnerable",
                      file="requirements.txt", tool="sca",
                      rule_id="GHSA-jfhm-5ghh-2f97", cve="CVE-2023-49083",
                      snippet="cryptography 41.0.0")
    adapter = _FakeSca(findings=[finding])
    monkeypatch.setattr("ai_autopilot.security_scan.runner.registry",
                        lambda config=None: {"sca": adapter})
    monkeypatch.setattr(intel.httpx, "AsyncClient",
                        lambda **kw: _Http(routes))
    repo_dir = tmp_path / "ws" / "app"
    repo_dir.mkdir(parents=True, exist_ok=True)
    return ScanRequest(repo=str(repo_dir), workspace=str(repo_dir.parent), tools=["sca"],
                       ai_mode="off", fail_on="critical", write_html=False)


async def test_kev_escalation_fails_gate_on_a_baseline_finding(
    monkeypatch, tmp_path: Path, security_repo,
):
    req = _wire(monkeypatch, tmp_path, {"kev.example": ConnectionError("not yet listed")})

    # Scan 1: intel offline → the finding lands in the baseline as plain "high".
    first = await run_scan(req, security_repo=security_repo, config=_cfg())
    assert first.passed and not first.tools["intel"].ran
    assert len(first.diff.new) == 1

    # Scan 2: CISA has listed the CVE. Nothing new in the code — the gate fails anyway,
    # and every summary names KEV as the cause so nobody reads it as a flake.
    monkeypatch.setattr(intel.httpx, "AsyncClient", lambda **kw: _Http(_routes()))
    second = await run_scan(req, security_repo=security_repo, config=_cfg())
    assert second.tools["intel"].ran
    assert not second.passed and second.kev_only_failure
    assert second.diff.new == [] and len(second.gate_findings) == 1
    assert second.gate_findings[0].kev and second.gate_findings[0].severity == "critical"


async def test_suppressed_kev_respects_the_human_but_warns(
    monkeypatch, tmp_path: Path, security_repo,
):
    req = _wire(monkeypatch, tmp_path, _routes())
    first = await run_scan(req, security_repo=security_repo, config=_cfg())
    assert not first.passed and first.kev_findings

    key = first.kev_findings[0].fingerprint
    s = sup.Suppressions(path=sup.file_for(req.workspace))
    s.add(key, "vendored copy, not reachable", by="qa", expires=date(2099, 1, 1))
    sup.save(s)

    second = await run_scan(req, security_repo=security_repo, config=_cfg())
    assert second.passed                       # an explicit suppression still wins
    assert not second.kev_findings             # kev_findings reads unsuppressed rows
    assert [f.fingerprint for f in second.suppressed] == [key]


async def test_intel_off_when_config_absent(monkeypatch, tmp_path: Path):
    req = _wire(monkeypatch, tmp_path, _routes())
    result = await run_scan(req)               # config=None — the pre-PR gate's shape
    assert "intel" not in result.tools
    assert all(not f.kev for f in result.findings)
