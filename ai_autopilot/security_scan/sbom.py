"""SBOM — "what are we actually running?", answered once and kept.

A scan answers that question at the moment it runs. A CVE published next Tuesday does not
wait for the next scan, and nobody remembers which build had which version. So a release
records its components as a CycloneDX document, and that stored document can be audited
again later — against today's OSV data and today's CISA KEV list — without touching the
code, the build or a checkout of that tag.

Two halves, kept apart so the second needs no repo:

- :func:`build_sbom` reads manifests and lockfiles and returns a CycloneDX 1.5 dict. Exact
  versions come from lockfiles (``package-lock.json``, ``packages.lock.json``) and
  ``==`` pins; a component with no resolvable version is still listed, with no version,
  because "we depend on it but cannot say which" is itself the finding an auditor needs.
- :func:`audit_sbom` takes any such document, asks OSV which components are affected, and
  returns :class:`Finding` s shaped like SCA findings (``tool="sca"``) so
  :func:`ai_autopilot.security_scan.intel.run_intel` enriches them with KEV and EPSS
  exactly as it does for a scan. Offline means "not audited" — said so, never "clean".
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.reports import Finding, normalise_severity
from ai_autopilot.security_scan.tools import pkgguard as pg
from ai_autopilot.security_scan.tools.base import rel

_log = get_logger("security_scan.sbom")

_TIMEOUT = 20.0
_BATCH = 500
_MAX_DETAIL_LOOKUPS = 120
_OSV_BASE = "https://api.osv.dev/v1"

_PURL_TYPE = {pg.PYPI: "pypi", pg.NPM: "npm", pg.NUGET: "nuget"}


def purl(eco: str, name: str, version: str = "") -> str:
    """Package URL. PyPI names are normalised, npm scopes keep ``@`` encoded per spec."""
    n = pg.canonical(eco, name) if eco == pg.PYPI else name.lower()
    n = "%40" + quote(n[1:], safe="/") if n.startswith("@") else quote(n, safe="/")
    return f"pkg:{_PURL_TYPE[eco]}/{n}" + (f"@{quote(version, safe='')}" if version else "")


# ── lockfiles (pure) ────────────────────────────────────────────────────────


def parse_package_lock(text: str, manifest: str) -> list[pg.Dep]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out: list[pg.Dep] = []
    for path, meta in (data.get("packages") or {}).items():
        if not path or not isinstance(meta, dict) or "node_modules/" not in path:
            continue
        name = path.rsplit("node_modules/", 1)[1]
        if meta.get("version") and not meta.get("link"):
            out.append(pg.Dep(pg.NPM, name, str(meta["version"]), manifest))
    if not out:   # lockfileVersion 1
        for name, meta in (data.get("dependencies") or {}).items():
            if isinstance(meta, dict) and meta.get("version"):
                out.append(pg.Dep(pg.NPM, name, str(meta["version"]), manifest))
    return out


def parse_nuget_lock(text: str, manifest: str) -> list[pg.Dep]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out: list[pg.Dep] = []
    for deps in (data.get("dependencies") or {}).values():
        for name, meta in (deps or {}).items():
            if isinstance(meta, dict) and meta.get("resolved"):
                out.append(pg.Dep(pg.NUGET, name, str(meta["resolved"]), manifest))
    return out


def _lockfiles(repo: str, limit: int = 60) -> list[str]:
    root = Path(repo)
    out: list[str] = []
    stack = [root]
    while stack and len(out) < limit:
        d = stack.pop()
        try:
            for e in sorted(d.iterdir(), key=lambda p: p.name):
                if e.is_dir():
                    if e.name not in pg._SKIP_DIRS and not e.name.startswith("."):
                        stack.append(e)
                elif e.name in ("package-lock.json", "packages.lock.json"):
                    out.append(rel(repo, str(e)))
        except OSError:
            continue
    return sorted(out)


# ── build ───────────────────────────────────────────────────────────────────


def collect(repo: str) -> list[pg.Dep]:
    """Every dependency the repo declares, versions resolved from lockfiles where they
    exist. A lockfile entry wins over a manifest range for the same package."""
    resolved: dict[tuple[str, str], pg.Dep] = {}
    declared: dict[tuple[str, str], pg.Dep] = {}
    for m in pg.find_manifests(repo):
        try:
            text = (Path(repo) / m).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for d in pg.parse_manifest(m, text):
            (resolved if d.version else declared).setdefault(d.key, d)
    for m in _lockfiles(repo):
        try:
            text = (Path(repo) / m).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        parser = parse_package_lock if m.endswith("package-lock.json") else parse_nuget_lock
        for d in parser(text, m):
            resolved[d.key] = d
    merged = {**declared, **resolved}
    return sorted(merged.values(), key=lambda d: (d.eco, d.key[1]))


def build_sbom(repo: str, *, name: str = "", version: str = "",
               now: datetime | None = None) -> dict:
    """CycloneDX 1.5 document for ``repo``."""
    deps = collect(repo)
    components = [{
        "type": "library", "bom-ref": purl(d.eco, d.name, d.version),
        "name": d.name, **({"version": d.version} if d.version else {}),
        "purl": purl(d.eco, d.name, d.version),
        "properties": [{"name": "autopilot:manifest", "value": d.manifest}],
    } for d in deps]
    return {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "metadata": {
            "timestamp": (now or datetime.now(UTC)).isoformat(),
            "tools": {"components": [{"type": "application", "name": "ai-autopilot"}]},
            "component": {"type": "application", "name": name or Path(repo).resolve().name,
                          **({"version": version} if version else {})},
        },
        "components": components,
    }


# ── audit ───────────────────────────────────────────────────────────────────

_OSV_SEVERITY = {"CRITICAL": "critical", "HIGH": "high", "MODERATE": "medium",
                 "MEDIUM": "medium", "LOW": "low"}


def severity_of(vuln: dict) -> str:
    raw = str((vuln.get("database_specific") or {}).get("severity") or "").upper()
    return _OSV_SEVERITY.get(raw) or normalise_severity("medium")


def _components(sbom: dict) -> list[dict]:
    return [c for c in (sbom or {}).get("components") or []
            if isinstance(c, dict) and c.get("purl")]


async def audit_sbom(
    sbom: dict, *, http=None, osv_url: str = _OSV_BASE,
) -> tuple[list[Finding], dict[str, Any]]:
    """Findings for every component OSV says is affected, plus a status dict
    ``{audited, unversioned, vulnerable, error}``. Never raises."""
    comps = _components(sbom)
    versioned = [c for c in comps if c.get("version")]
    stats: dict[str, Any] = {
        "audited": 0, "unversioned": len(comps) - len(versioned), "vulnerable": 0, "error": ""}
    if not versioned:
        return [], stats
    own = http is None
    http = http or httpx.AsyncClient(timeout=_TIMEOUT)
    findings: list[Finding] = []
    base = osv_url.rstrip("/")
    try:
        hits: list[tuple[dict, list[str]]] = []
        for i in range(0, len(versioned), _BATCH):
            chunk = versioned[i:i + _BATCH]
            resp = await http.post(f"{base}/querybatch", timeout=_TIMEOUT, json={
                "queries": [{"package": {"purl": c["purl"]}} for c in chunk]})
            if resp.status_code != 200:
                stats["error"] = f"OSV HTTP {resp.status_code}"
                break
            results = (resp.json() or {}).get("results") or []
            for comp, res in zip(chunk, results, strict=False):
                ids = [v.get("id") for v in (res or {}).get("vulns") or [] if v.get("id")]
                stats["audited"] += 1
                if ids:
                    hits.append((comp, ids))
        details: dict[str, dict] = {}
        for _comp, ids in hits:
            for vid in ids:
                if vid in details or len(details) >= _MAX_DETAIL_LOOKUPS:
                    continue
                r = await http.get(f"{base}/vulns/{vid}", timeout=_TIMEOUT)
                details[vid] = r.json() if r.status_code == 200 else {}
        for comp, ids in hits:
            stats["vulnerable"] += 1
            manifest = next((p.get("value") for p in comp.get("properties") or []
                             if p.get("name") == "autopilot:manifest"), "") or "sbom"
            for vid in ids:
                v = details.get(vid) or {}
                cve = next((a for a in v.get("aliases") or [] if a.startswith("CVE-")), "")
                findings.append(Finding(
                    severity=severity_of(v), tool="sca", rule_id=vid, cve=cve,
                    file=manifest, confidence="high", cwe="CWE-1395", owasp="A03:2025",
                    title=f"{comp['name']}@{comp['version']} is affected by {vid}",
                    detail=(v.get("summary") or "Listed in OSV; see the advisory.")[:400],
                    snippet=f"{comp['purl']} {vid}"))
    except Exception as exc:  # noqa: BLE001 — "not audited", never "clean"
        stats["error"] = describe_exc(exc)[:160]
        _log.info("SBOM audit failed", error=stats["error"])
    finally:
        if own:
            await http.aclose()
    return findings, stats


def load_sbom(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or data.get("bomFormat") != "CycloneDX":
        raise ValueError(f"{path} is not a CycloneDX document")
    return data


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def default_path(workspace: str, name: str, version: str = "") -> Path:
    """Where a release's SBOM is kept: ``<workspace>/.autopilot/sbom/<name>-<version>.cdx.json``."""
    stem = _SAFE.sub("-", f"{name}-{version}".strip("-")) or "sbom"
    return Path(workspace) / ".autopilot" / "sbom" / f"{stem}.cdx.json"
