"""SCA — known-vulnerable dependencies, per ecosystem the repo actually uses.

One adapter, several backends, chosen by what is on disk: ``*.csproj``/``*.sln`` →
``dotnet list package --vulnerable``; ``package-lock.json`` → ``npm audit``;
``requirements*.txt`` / ``pyproject.toml`` → ``pip-audit``; and ``trivy fs`` when it is
installed, which covers all of those and Dockerfiles too. Each backend that cannot run
says why in its own :class:`ToolStatus` so the report distinguishes "no vulnerable
packages" from "could not check".
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from ai_autopilot.reports import Finding, normalise_severity
from ai_autopilot.security_scan.tools.base import (
    ToolRun,
    ToolStatus,
    clip,
    parse_json,
    run_process,
    which,
)

_TIMEOUT = 15 * 60
_SKIP = {"node_modules", ".git", "bin", "obj", "dist", ".venv", "venv", "packages"}


def _find(repo: str, patterns: tuple[str, ...], limit: int = 50) -> list[Path]:
    root = Path(repo)
    out: list[Path] = []
    stack = [root]
    while stack and len(out) < limit:
        d = stack.pop()
        try:
            for e in sorted(d.iterdir(), key=lambda p: p.name):
                if e.is_dir():
                    if e.name not in _SKIP and not e.name.startswith("."):
                        stack.append(e)
                elif any(e.match(p) for p in patterns):
                    out.append(e)
        except OSError:
            continue
    return out


class ScaScanner:
    name = "sca"

    def __init__(self, timeout: float = _TIMEOUT) -> None:
        self._timeout = timeout

    def available(self) -> bool:
        return any(which(b) for b in ("trivy", "dotnet", "npm", "pip-audit"))

    async def run(self, repo: str, files: list[str] | None = None) -> ToolRun:
        started = time.monotonic()
        findings: list[Finding] = []
        parts: dict[str, str] = {}
        ran_any = False

        if which("trivy"):
            res = await run_process(
                ["trivy", "fs", "--scanners", "vuln", "--format", "json", "--quiet", "."],
                repo, timeout_seconds=self._timeout,
            )
            if res.ok and (data := parse_json(res.stdout)) is not None:
                got = parse_trivy(data)
                findings += got
                parts["trivy"] = f"{len(got)}"
                ran_any = True
            else:
                parts["trivy"] = "error: " + clip(res.error or res.stderr, 120)
        else:
            csproj = _find(repo, ("*.csproj", "*.sln"))
            if csproj:
                if which("dotnet"):
                    for proj in csproj[:10]:
                        res = await run_process(
                            ["dotnet", "list", str(proj), "package", "--vulnerable",
                             "--include-transitive", "--format", "json"],
                            repo, timeout_seconds=self._timeout,
                        )
                        if res.ok and (data := parse_json(res.stdout)) is not None:
                            got = parse_dotnet(data, proj.name)
                            findings += got
                            parts[f"dotnet:{proj.name}"] = f"{len(got)}"
                            ran_any = True
                        else:
                            parts[f"dotnet:{proj.name}"] = "error: " + clip(
                                res.error or res.stderr, 120)
                else:
                    parts["dotnet"] = "not installed"

            locks = _find(repo, ("package-lock.json",))
            if locks:
                if which("npm"):
                    for lock in locks[:10]:
                        res = await run_process(
                            ["npm", "audit", "--json", "--audit-level=low"],
                            str(lock.parent), timeout_seconds=self._timeout,
                        )
                        data = parse_json(res.stdout) if res.ok else None
                        if isinstance(data, dict) and "vulnerabilities" in data:
                            got = parse_npm(data, lock.parent.name)
                            findings += got
                            parts[f"npm:{lock.parent.name}"] = f"{len(got)}"
                            ran_any = True
                        else:
                            parts[f"npm:{lock.parent.name}"] = "error: " + clip(
                                res.error or res.stderr or "no JSON", 120)
                else:
                    parts["npm"] = "not installed"

            reqs = _find(repo, ("requirements*.txt", "pyproject.toml"))
            if reqs:
                if which("pip-audit"):
                    for req in reqs[:5]:
                        argv = ["pip-audit", "-f", "json", "--progress-spinner", "off"]
                        argv += ["-r", str(req)] if req.suffix == ".txt" else []
                        res = await run_process(
                            argv, str(req.parent), timeout_seconds=self._timeout)
                        data = parse_json(res.stdout) if res.ok else None
                        if data is not None:
                            got = parse_pip_audit(data, req.name)
                            findings += got
                            parts[f"pip-audit:{req.name}"] = f"{len(got)}"
                            ran_any = True
                        else:
                            parts[f"pip-audit:{req.name}"] = "error: " + clip(
                                res.error or res.stderr or "no JSON", 120)
                else:
                    parts["pip-audit"] = "not installed"

        status = ToolStatus(self.name, duration_seconds=time.monotonic() - started, extra=parts)
        if not parts:
            status.skipped_reason = "no dependency manifests found"
        elif not ran_any:
            status.skipped_reason = "no backend available: " + ", ".join(
                f"{k} {v}" for k, v in parts.items())
        else:
            status.ran, status.findings = True, len(findings)
        return ToolRun(findings, status)


# ── parsers (pure, fixture-tested) ──────────────────────────────────────────

def _vuln(title: str, pkg: str, version: str, severity: str, ident: str, manifest: str,
          fix: str = "", detail: str = "") -> Finding:
    return Finding.from_dict({
        "severity": normalise_severity(severity),
        "title": f"{pkg}@{version}: {title}" if title else f"{pkg}@{version} is vulnerable",
        "file": manifest,
        "detail": (detail + " " if detail else "") + (f"Fixed in {fix}." if fix else ""),
        "tool": "sca",
        "rule_id": ident or f"{pkg}@{version}",
        "cwe": "",
        "owasp": "A06:2021",
        "confidence": "high",
        "snippet": f"{pkg} {version}",
    })


def parse_trivy(data: object) -> list[Finding]:
    out: list[Finding] = []
    results = data.get("Results") if isinstance(data, dict) else None
    for res in results or []:
        target = str(res.get("Target") or "")
        for v in res.get("Vulnerabilities") or []:
            out.append(_vuln(
                clip(str(v.get("Title") or ""), 120), str(v.get("PkgName") or "?"),
                str(v.get("InstalledVersion") or "?"), str(v.get("Severity") or "medium"),
                str(v.get("VulnerabilityID") or ""), target, str(v.get("FixedVersion") or ""),
            ))
    return out


def parse_dotnet(data: object, manifest: str) -> list[Finding]:
    out: list[Finding] = []
    for proj in (data.get("projects") if isinstance(data, dict) else None) or []:
        for fw in proj.get("frameworks") or []:
            for kind in ("topLevelPackages", "transitivePackages"):
                for pkg in fw.get(kind) or []:
                    for v in pkg.get("vulnerabilities") or []:
                        url = str(v.get("advisoryurl") or "")
                        ident = url.rsplit("/", 1)[-1] if url else ""
                        out.append(_vuln(
                            "", str(pkg.get("id") or "?"),
                            str(pkg.get("resolvedVersion") or "?"),
                            str(v.get("severity") or "medium"), ident, manifest,
                            detail=(("transitive. " if kind == "transitivePackages" else "") + url),
                        ))
    return out


def parse_npm(data: dict, manifest_dir: str) -> list[Finding]:
    out: list[Finding] = []
    vulns = data.get("vulnerabilities") or {}
    for name, v in vulns.items():
        if not isinstance(v, dict):
            continue
        via = v.get("via") or []
        advisories = [x for x in via if isinstance(x, dict)]
        title = clip(str(advisories[0].get("title") if advisories else ""), 120)
        ident = ""
        if advisories:
            url = str(advisories[0].get("url") or "")
            ident = url.rsplit("/", 1)[-1] if url else ""
        fix = v.get("fixAvailable")
        fix_text = ""
        if isinstance(fix, dict):
            fix_text = f"{fix.get('name')}@{fix.get('version')}"
        elif fix is True:
            fix_text = "available via npm audit fix"
        out.append(_vuln(
            title, str(name), str(v.get("range") or "?"), str(v.get("severity") or "medium"),
            ident, f"{manifest_dir}/package-lock.json", fix_text,
            detail="direct" if v.get("isDirect") else "transitive",
        ))
    return out


def parse_pip_audit(data: object, manifest: str) -> list[Finding]:
    out: list[Finding] = []
    deps = data.get("dependencies") if isinstance(data, dict) else data
    for dep in deps or []:
        if not isinstance(dep, dict):
            continue
        for v in dep.get("vulns") or []:
            fixes = v.get("fix_versions") or []
            out.append(_vuln(
                clip(str(v.get("description") or ""), 120), str(dep.get("name") or "?"),
                str(dep.get("version") or "?"), _pip_severity(v), str(v.get("id") or ""),
                manifest, ", ".join(map(str, fixes)),
            ))
    return out


def _pip_severity(v: dict) -> str:
    """pip-audit carries no severity; infer from aliases (GHSA critical marker absent) →
    high for anything with a CVE, medium otherwise. Conservative on purpose."""
    aliases = " ".join(map(str, v.get("aliases") or []))
    return "high" if re.search(r"CVE-\d{4}-\d+", aliases + str(v.get("id") or "")) else "medium"
