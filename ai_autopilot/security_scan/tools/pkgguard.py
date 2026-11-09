"""Package guard — does the dependency a change adds actually exist, and is it old enough
to trust?

Two supply-chain failures that SCA cannot see, because SCA asks "is this *known* to be
vulnerable" and both of these are about packages nobody has reported yet:

- **Hallucinated names.** A model suggests ``requests-oauth-helper`` because it sounds
  right. Today that fails the install. The day an attacker registers the name (slopsquatting)
  it installs, and runs their code in CI and in production.
- **Brand-new look-alikes.** A package first published three days ago, with one release,
  that this very change introduces.

The check is deterministic and costs no tokens: parse the manifests, work out which
dependencies the change *added or re-versioned* (against the merge-base with the base
branch), and ask the public registry about each — exists? first published when? does the
pinned version exist? No registry answer is ever guessed: a lookup that fails is reported
as "could not check", never as "fine" and never as "missing".

Scope rules, so the report is not noise:

- Only added/re-versioned dependencies are judged when a base is resolvable. Otherwise
  (a full scan on the default branch) it checks existence only, capped, and says so.
- ``pkgguard_internal_prefixes`` names packages that live on a private feed; they are
  never looked up (a 404 there is expected, and the name must not leak to a public host).
- Ecosystems: PyPI (requirements*.txt, pyproject.toml), npm (package.json), NuGet
  (*.csproj / Directory.Packages.props).
"""

from __future__ import annotations

import re
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.reports import Finding
from ai_autopilot.security_scan.tools.base import ToolRun, ToolStatus, rel, run_process

_log = get_logger("security_scan.pkgguard")

_SKIP_DIRS = {"node_modules", ".git", "bin", "obj", "dist", ".venv", "venv", "packages",
              "__pycache__", ".tox", "site-packages"}
_TIMEOUT = 10.0
_FULL_SCAN_CAP = 150          # existence-only lookups when there is no diff to narrow to
_PARALLEL = 8

PYPI, NPM, NUGET = "pypi", "npm", "nuget"
_DEFAULT_URLS = {
    PYPI: "https://pypi.org/pypi",
    NPM: "https://registry.npmjs.org",
    NUGET: "https://api.nuget.org/v3-flatcontainer",
}

_PEP508 = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(.*)$")
_PIN = re.compile(r"^(?:===|==)\s*([A-Za-z0-9_.!+*-]+)\s*(?:[;,#].*)?$")
_CSPROJ_REF = re.compile(
    r"<(?:PackageReference|PackageVersion)\b[^>]*?\bInclude\s*=\s*\"([^\"]+)\""
    r"(?:[^>]*?\bVersion\s*=\s*\"([^\"]*)\")?", re.I)


@dataclass(frozen=True)
class Dep:
    eco: str
    name: str            # as written
    version: str = ""    # exact pin when the manifest pins one, else ""
    manifest: str = ""   # repo-relative path
    line: int | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.eco, canonical(self.eco, self.name)

    @property
    def spec_key(self) -> tuple[str, str, str]:
        return self.eco, canonical(self.eco, self.name), self.version


def canonical(eco: str, name: str) -> str:
    """Registry-equivalent name: PyPI folds case and ``._-``; npm/NuGet fold case."""
    name = name.strip().lower()
    return re.sub(r"[-_.]+", "-", name) if eco == PYPI else name


# ── manifest parsing (pure) ─────────────────────────────────────────────────


def _py_requirement(text: str) -> tuple[str, str] | None:
    text = text.split("#", 1)[0].strip()
    if not text or text.startswith(("-", "git+", "http://", "https://", ".", "/")):
        return None
    if "@" in text.split(";", 1)[0] and "://" in text:   # PEP 508 direct reference
        return None
    m = _PEP508.match(text)
    if not m:
        return None
    pin = _PIN.match(m.group(2).strip())
    return m.group(1), (pin.group(1) if pin else "")


def parse_requirements(text: str, manifest: str) -> list[Dep]:
    out = []
    for no, raw in enumerate(text.splitlines(), 1):
        got = _py_requirement(raw)
        if got:
            out.append(Dep(PYPI, got[0], got[1], manifest, no))
    return out


def parse_pyproject(text: str, manifest: str) -> list[Dep]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    specs: list[str] = list((data.get("project") or {}).get("dependencies") or [])
    for group in ((data.get("project") or {}).get("optional-dependencies") or {}).values():
        specs += list(group or [])
    for group in (data.get("dependency-groups") or {}).values():
        specs += [s for s in (group or []) if isinstance(s, str)]
    out = []
    for spec in specs:
        got = _py_requirement(spec)
        if got:
            out.append(Dep(PYPI, got[0], got[1], manifest, _line_of(text, got[0])))
    return out


def parse_package_json(text: str, manifest: str) -> list[Dep]:
    import json

    try:
        data = json.loads(text)
    except ValueError:
        return []
    out = []
    for block in ("dependencies", "devDependencies", "optionalDependencies"):
        for name, spec in ((data or {}).get(block) or {}).items():
            spec = str(spec).strip()
            if re.match(r"^(file:|link:|git|github:|http|workspace:|npm:|\.|/|~/)", spec):
                continue
            pinned = spec if re.fullmatch(r"\d+(\.\d+){0,2}([-+][\w.]+)?", spec) else ""
            out.append(Dep(NPM, name, pinned, manifest, _line_of(text, f'"{name}"')))
    return out


def parse_csproj(text: str, manifest: str) -> list[Dep]:
    out = []
    for m in _CSPROJ_REF.finditer(text):
        version = (m.group(2) or "").strip()
        if version.startswith(("$", "[", "(")) or "*" in version:
            version = ""
        out.append(Dep(NUGET, m.group(1), version, manifest,
                       text.count("\n", 0, m.start()) + 1))
    return out


def _line_of(text: str, needle: str) -> int | None:
    idx = text.find(needle)
    return text.count("\n", 0, idx) + 1 if idx >= 0 else None


def parse_manifest(path: str, text: str) -> list[Dep]:
    """Dependencies declared in one manifest file, by file name."""
    name = Path(path).name.lower()
    if name == "pyproject.toml":
        return parse_pyproject(text, path)
    if name.startswith("requirements") and name.endswith((".txt", ".in")):
        return parse_requirements(text, path)
    if name == "package.json":
        return parse_package_json(text, path)
    if name.endswith((".csproj", ".props")):
        return parse_csproj(text, path)
    return []


def is_manifest(name: str) -> bool:
    name = name.lower()
    return (name in ("pyproject.toml", "package.json")
            or (name.startswith("requirements") and name.endswith((".txt", ".in")))
            or name.endswith(".csproj") or name == "directory.packages.props")


def find_manifests(repo: str, limit: int = 80) -> list[str]:
    root = Path(repo)
    out: list[str] = []
    stack = [root]
    while stack and len(out) < limit:
        d = stack.pop()
        try:
            for e in sorted(d.iterdir(), key=lambda p: p.name):
                if e.is_dir():
                    if e.name not in _SKIP_DIRS and not e.name.startswith("."):
                        stack.append(e)
                elif is_manifest(e.name):
                    out.append(rel(repo, str(e)))
        except OSError:
            continue
    return sorted(out)


# ── registry lookup ─────────────────────────────────────────────────────────


@dataclass
class RegistryInfo:
    exists: bool | None = None          # None = could not tell (network / odd status)
    first_published: datetime | None = None
    release_count: int = 0
    versions: frozenset[str] = frozenset()
    error: str = ""


def _ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_pypi(data: object) -> RegistryInfo:
    releases = (data.get("releases") or {}) if isinstance(data, dict) else {}
    times = [t for files in releases.values() for f in (files or [])
             if (t := _ts((f or {}).get("upload_time_iso_8601") or (f or {}).get("upload_time")))]
    return RegistryInfo(True, min(times) if times else None,
                        sum(1 for v in releases.values() if v), frozenset(releases))


def parse_npm(data: object) -> RegistryInfo:
    if not isinstance(data, dict):
        return RegistryInfo(None, error="unexpected response")
    versions = frozenset((data.get("versions") or {}).keys())
    return RegistryInfo(True, _ts((data.get("time") or {}).get("created")),
                        len(versions), versions)


def parse_nuget(data: object) -> RegistryInfo:
    versions = frozenset(str(v).lower() for v in (data or {}).get("versions") or []) \
        if isinstance(data, dict) else frozenset()
    return RegistryInfo(True, None, len(versions), versions)


_PARSERS = {PYPI: parse_pypi, NPM: parse_npm, NUGET: parse_nuget}


async def lookup(http, eco: str, name: str, urls: dict[str, str]) -> RegistryInfo:
    """One registry question. 404 → exists False; anything unclear → exists None."""
    base = urls.get(eco) or _DEFAULT_URLS[eco]
    if eco == PYPI:
        url = f"{base}/{quote(name, safe='')}/json"
    elif eco == NPM:
        url = f"{base}/{quote(name, safe='@').replace('/', '%2F')}"
    else:
        url = f"{base}/{quote(name.lower(), safe='')}/index.json"
    try:
        resp = await http.get(url, timeout=_TIMEOUT, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001 — offline is "unknown", not "missing"
        return RegistryInfo(None, error=describe_exc(exc)[:120])
    if resp.status_code == 404:
        return RegistryInfo(False)
    if resp.status_code != 200:
        return RegistryInfo(None, error=f"HTTP {resp.status_code}")
    try:
        return _PARSERS[eco](resp.json())
    except Exception as exc:  # noqa: BLE001
        return RegistryInfo(None, error=describe_exc(exc)[:120])


# ── judging (pure) ──────────────────────────────────────────────────────────


def judge(dep: Dep, info: RegistryInfo, *, is_new: bool, min_age_days: int,
          now: datetime | None = None) -> list[Finding]:
    """Findings for one dependency given what the registry said. No I/O."""
    if info.exists is None:
        return []
    now = now or datetime.now(UTC)
    where: dict[str, Any] = dict(tool="pkgguard", file=dep.manifest, line=dep.line,
                 snippet=f"{dep.eco}:{canonical(dep.eco, dep.name)}",
                 cwe="CWE-1357", owasp="A03:2025")
    label = f"{dep.name}{'==' + dep.version if dep.version else ''} ({dep.eco})"
    out: list[Finding] = []
    if info.exists is False:
        out.append(Finding(
            severity="high", rule_id="pkg-not-found", confidence="high",
            title=f"Dependency does not exist on the public registry: {label}",
            detail=("The registry has no package with this name. If a model suggested it, "
                    "the name is hallucinated — and whoever registers it next controls "
                    "code that runs in your build. Remove it, or add its prefix to "
                    "`pkgguard_internal_prefixes` if it lives on a private feed."),
            **where))
        return out
    if dep.version and info.versions and dep.version.lower() not in {
            v.lower() for v in info.versions}:
        out.append(_version_missing(label, where))
    if is_new and info.first_published is not None:
        age = (now - info.first_published).days
        if age < min_age_days:
            out.append(Finding(
                severity="high", rule_id="pkg-too-new", confidence="medium",
                title=f"New dependency first published {age} day(s) ago: {label}",
                detail=(f"Younger than the {min_age_days}-day minimum. Fresh packages "
                        "have no track record and are the usual shape of a look-alike "
                        "or takeover. Confirm the maintainer and source, or pin a "
                        "reviewed alternative."), **where))
        elif age < 90 and info.release_count <= 2:
            out.append(Finding(
                severity="medium", rule_id="pkg-low-history", confidence="low",
                title=f"New dependency with little history: {label}",
                detail=(f"First published {age} days ago with {info.release_count} "
                        "release(s). Not proof of anything — a prompt to check who "
                        "maintains it."), **where))
    return out


def _version_missing(label: str, where: dict[str, Any]) -> Finding:
    return Finding(
        severity="high", rule_id="pkg-version-not-found", confidence="high",
        title=f"Pinned version does not exist: {label}",
        detail=("The package exists but this exact version was never published. "
                "Models reproduce version numbers from memory; this one is not real."),
        **where)


# ── base comparison ─────────────────────────────────────────────────────────


async def _git(repo: str, *args: str) -> str | None:
    res = await run_process(["git", *args], repo, timeout_seconds=30)
    return res.stdout if res.ok and res.returncode == 0 else None


async def resolve_base(repo: str, base: str) -> str:
    """Merge-base commit of HEAD with ``base`` (or ``origin/<base>``), "" when HEAD *is*
    that commit (nothing to diff) or git cannot say."""
    head = (await _git(repo, "rev-parse", "HEAD") or "").strip()
    for ref in (f"origin/{base}", base):
        mb = (await _git(repo, "merge-base", "HEAD", ref) or "").strip()
        if mb:
            return "" if mb == head else mb
    return ""


# ── the adapter ─────────────────────────────────────────────────────────────


class PkgGuardScanner:
    name = "pkgguard"

    def __init__(self, *, min_age_days: int = 14, internal_prefixes: list[str] | None = None,
                 registry_urls: dict[str, str] | None = None, base_ref: str = "main",
                 http=None) -> None:
        self.min_age_days = min_age_days
        self.internal_prefixes = [p.lower() for p in internal_prefixes or [] if p]
        self.registry_urls = registry_urls or {}
        self.base_ref = base_ref
        self._http = http

    def available(self) -> bool:
        return True

    def _internal(self, dep: Dep) -> bool:
        n = dep.name.lower()
        return any(n.startswith(p) for p in self.internal_prefixes)

    async def _declared(self, repo: str, manifests: list[str]) -> list[Dep]:
        deps: list[Dep] = []
        for m in manifests:
            try:
                text = (Path(repo) / m).read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            deps += parse_manifest(m, text)
        return deps

    async def _before(self, repo: str, base: str, manifests: list[str]) -> set[tuple]:
        before: set[tuple] = set()
        for m in manifests:
            text = await _git(repo, "show", f"{base}:{m}")
            if text:
                before |= {d.spec_key for d in parse_manifest(m, text)}
        return before

    async def run(self, repo: str, files: list[str] | None = None) -> ToolRun:
        started = time.monotonic()
        st = ToolStatus(self.name)
        manifests = find_manifests(repo)
        if files is not None:
            changed = {f.replace("\\", "/") for f in files}
            manifests = [m for m in manifests if m in changed]
        if not manifests:
            st.skipped_reason = "no dependency manifest in scope"
            return ToolRun([], st)

        deps = [d for d in await self._declared(repo, manifests) if not self._internal(d)]
        base = await resolve_base(repo, self.base_ref)
        new_keys: set[tuple[str, str]] = set()
        if base:
            before = await self._before(repo, base, manifests)
            targets = [d for d in deps if d.spec_key not in before]
            before_names = {(e, n) for e, n, _ in before}
            new_keys = {d.key for d in targets if d.key not in before_names}
            st.extra["scope"] = f"{len(targets)} added/changed vs {self.base_ref}"
        else:
            targets = deps[:_FULL_SCAN_CAP]
            st.extra["scope"] = f"existence only, {len(targets)} of {len(deps)}"
        if not targets:
            st.ran = True
            st.duration_seconds = time.monotonic() - started
            return ToolRun([], st)

        own = self._http is None
        http = self._http or httpx.AsyncClient(timeout=_TIMEOUT)
        try:
            import asyncio

            sem = asyncio.Semaphore(_PARALLEL)
            unique = {d.key: d for d in targets}

            async def one(d: Dep):
                async with sem:
                    return d.key, await lookup(http, d.eco, d.name, self.registry_urls)

            infos = dict(await asyncio.gather(*(one(d) for d in unique.values())))
        finally:
            if own:
                await http.aclose()

        findings: list[Finding] = []
        unchecked = 0
        for d in targets:
            info = infos[d.key]
            if info.exists is None:
                unchecked += 1
                continue
            findings += judge(d, info, is_new=d.key in new_keys,
                              min_age_days=self.min_age_days)
        st.ran = True
        st.findings = len(findings)
        if unchecked:
            # "could not check" must not read as "clean".
            st.extra["unchecked"] = unchecked
        st.duration_seconds = time.monotonic() - started
        return ToolRun(findings, st)
