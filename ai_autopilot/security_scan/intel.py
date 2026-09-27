"""Threat intel for SCA findings: CISA KEV and FIRST EPSS.

A dependency CVE is not one fact but three: that the flaw exists (the scanner's job),
that someone is exploiting it in the wild (CISA's KEV catalog — a record of events, not
a prediction), and how likely exploitation is in the next 30 days (EPSS — a prediction,
not a record). This module joins the last two onto what the scanners found:

- **KEV hit** → severity becomes ``critical``, the finding carries ``kev``/``kev_due``/
  ``kev_ransomware``, and the runner's gate fails on it even when the finding has been
  sitting in the baseline for months. "We already knew about it" is exactly the state
  KEV exists to escalate out of.
- **EPSS score** → recorded and displayed, never a severity change. A probability going
  up is a reason to look, not to page.

Most SCA backends report GHSA/PYSEC ids, not CVEs (and ``rule_id`` must never change —
fingerprints are built on it), so ids are resolved to CVEs through OSV's alias data and
kept on the separate ``cve`` field.

Everything network-facing is fail-soft: any fetch that cannot happen leaves the scan
untouched and says why in the :class:`ToolStatus`, because "not enriched" and "nothing
exploited" are opposite answers. The on-disk cache lives in the workspace
(``.autopilot/intel/``) and is only written when the scan itself stores — a
``--no-store`` run must leave no files behind (see runner).
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.reports import Finding
from ai_autopilot.security_scan.tools.base import ToolStatus

_log = get_logger("security_scan.intel")

_TIMEOUT = 20.0          # per request, matching updates.py's external-fetch pattern
_EPSS_CHUNK = 100        # CVEs per EPSS API call (its page size)
_MAX_OSV_LOOKUPS = 50    # per scan — one GET each; more than this is a flooded report
_CVE_RE = re.compile(r"^CVE-\d{4}-\d{4,}$")
_ALIAS_RE = re.compile(r"CVE-\d{4}-\d{4,}")


@dataclass
class KevEntry:
    """One catalog row — what turns "vulnerable" into "being exploited"."""

    cve: str
    due: str = ""             # CISA remediation due date, YYYY-MM-DD
    ransomware: bool = False  # knownRansomwareCampaignUse == "Known"
    date_added: str = ""


# ── pure parsers (fixture-tested; no I/O) ────────────────────────────────────


def parse_kev(data: object) -> dict[str, KevEntry]:
    """The KEV catalog JSON → ``{cve: entry}``. Tolerant of missing optionals."""
    out: dict[str, KevEntry] = {}
    vulns = data.get("vulnerabilities") if isinstance(data, dict) else None
    for v in vulns or []:
        if not isinstance(v, dict):
            continue
        cve = str(v.get("cveID") or "").strip().upper()
        if not _CVE_RE.match(cve):
            continue
        out[cve] = KevEntry(
            cve=cve,
            due=str(v.get("dueDate") or ""),
            ransomware=str(v.get("knownRansomwareCampaignUse") or "").lower() == "known",
            date_added=str(v.get("dateAdded") or ""),
        )
    return out


def parse_epss(data: object) -> dict[str, tuple[float, float]]:
    """The EPSS API response → ``{cve: (epss, percentile)}``.

    The API serialises both numbers as STRINGS ("0.999990000") — parse, don't trust.
    """
    out: dict[str, tuple[float, float]] = {}
    rows = data.get("data") if isinstance(data, dict) else None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        cve = str(row.get("cve") or "").strip().upper()
        try:
            score = float(row.get("epss"))
            pct = float(row.get("percentile"))
        except (TypeError, ValueError):
            continue
        if _CVE_RE.match(cve):
            out[cve] = (score, pct)
    return out


def parse_osv(data: object) -> str:
    """One OSV vulnerability record → its first CVE alias, or ""."""
    if not isinstance(data, dict):
        return ""
    ids = [str(data.get("id") or "")] + [str(a) for a in data.get("aliases") or []]
    for candidate in ids:
        m = _ALIAS_RE.search(candidate.upper())
        if m:
            return m.group(0)
    return ""


def cve_of(f: Finding) -> str:
    """The finding's CVE when it already carries one (field, or a CVE-shaped rule_id)."""
    if f.cve:
        return f.cve
    rid = (f.rule_id or "").strip().upper()
    return rid if _CVE_RE.match(rid) else ""


def enrich(
    findings: list[Finding],
    kev: dict[str, KevEntry],
    epss: dict[str, tuple[float, float]],
    cves: dict[str, str],
) -> dict[str, int]:
    """Apply intel to ``findings`` in place; returns ``{"kev": n, "epss": n}``.

    Pure: only severity/detail and the intel fields change — never tool, rule_id, file
    or snippet, so fingerprints (and with them the baseline and every suppression)
    are untouched.
    """
    stats = {"kev": 0, "epss": 0}
    for f in findings:
        cve = cves.get(f.fingerprint) or cve_of(f)
        if not cve:
            continue
        f.cve = cve
        notes: list[str] = []
        entry = kev.get(cve)
        if entry is not None:
            stats["kev"] += 1
            f.kev = True
            f.kev_due = entry.due
            f.kev_ransomware = entry.ransomware
            f.severity = "critical"
            note = f"⚠ In CISA KEV since {entry.date_added or '?'}"
            if entry.due:
                note += f" (remediation due {entry.due}"
                note += ", used in ransomware campaigns)" if entry.ransomware else ")"
            elif entry.ransomware:
                note += " (used in ransomware campaigns)"
            notes.append(note + ".")
        scored = epss.get(cve)
        if scored is not None:
            stats["epss"] += 1
            f.epss, f.epss_percentile = scored
            notes.append(f"EPSS {scored[0]:.2f} (top {max(0.0, (1 - scored[1])) * 100:.0f}%).")
        if notes:
            prefix = (f.detail + " ") if f.detail else ""
            f.detail = prefix + " ".join(notes)
    return stats


# ── cache (workspace/.autopilot/intel; disk writes only when the scan stores) ─


class Cache:
    """Feeds on disk with a fetched-at stamp. ``persist=False`` reads but never writes,
    so a ``--no-store`` scan can still benefit from an earlier stored scan's cache."""

    def __init__(self, workspace: str, *, ttl_hours: int, persist: bool) -> None:
        self.dir = Path(workspace) / ".autopilot" / "intel"
        self.ttl_seconds = max(1, int(ttl_hours)) * 3600
        self.persist = persist

    def load(self, name: str) -> tuple[object | None, float]:
        """``(payload, age_seconds)`` — payload None when absent/corrupt."""
        try:
            raw = json.loads((self.dir / name).read_text(encoding="utf-8"))
            fetched = float(raw.get("fetched_at") or 0)
            return raw.get("payload"), max(0.0, time.time() - fetched)
        except (OSError, ValueError, TypeError, AttributeError):
            return None, 0.0

    def save(self, name: str, payload: object) -> None:
        if not self.persist:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / name).write_text(
                json.dumps({"fetched_at": time.time(), "payload": payload}),
                encoding="utf-8",
            )
        except OSError as exc:
            _log.warning("intel cache not written", file=name, error=describe_exc(exc))

    def fresh(self, age_seconds: float) -> bool:
        return age_seconds <= self.ttl_seconds


def _age_label(age_seconds: float) -> str:
    hours = age_seconds / 3600
    return f"{hours:.0f}h" if hours >= 1 else f"{age_seconds / 60:.0f}m"


# ── network (fail-soft; every miss becomes a status note, never an exception) ─


async def fetch_kev(http, cfg, cache: Cache) -> tuple[dict[str, KevEntry], str]:
    """The KEV catalog, from cache when fresh, network otherwise, stale cache as the
    fallback. Returns ``(catalog, note)`` — empty catalog + note when nothing worked."""
    payload, age = cache.load("kev.json")
    if payload is not None and cache.fresh(age):
        return parse_kev(payload), f"cache {_age_label(age)}"
    try:
        resp = await http.get(cfg.intel_kev_url, timeout=_TIMEOUT,
                              follow_redirects=True)
        if resp.status_code == 200:
            data = resp.json()
            catalog = parse_kev(data)
            if catalog:
                cache.save("kev.json", data)
                return catalog, "fetched"
    except Exception as exc:  # noqa: BLE001 — offline is not a problem
        _log.info("KEV feed unavailable", error=describe_exc(exc))
        if payload is not None:
            return parse_kev(payload), f"stale cache ({_age_label(age)})"
        return {}, f"offline: {describe_exc(exc)}"
    if payload is not None:
        return parse_kev(payload), f"stale cache ({_age_label(age)})"
    return {}, (f"feed returned HTTP {resp.status_code}" if resp.status_code != 200
                else "feed returned no vulnerabilities")


async def fetch_epss(http, cfg, cves: set[str], cache: Cache) -> dict[str, tuple[float, float]]:
    """EPSS scores for exactly ``cves`` — cached per entry (scores update daily),
    missing/stale ones re-queried in chunks. Partial results are fine."""
    payload, age = cache.load("epss.json")
    known: dict[str, list] = payload if isinstance(payload, dict) else {}
    out: dict[str, tuple[float, float]] = {}
    missing: list[str] = []
    for cve in sorted(cves):
        row = known.get(cve)
        if isinstance(row, list) and len(row) == 3 and time.time() - row[2] <= cache.ttl_seconds:
            out[cve] = (float(row[0]), float(row[1]))
        else:
            missing.append(cve)
    for i in range(0, len(missing), _EPSS_CHUNK):
        chunk = missing[i : i + _EPSS_CHUNK]
        try:
            resp = await http.get(cfg.intel_epss_url,
                                  params={"cve": ",".join(chunk)}, timeout=_TIMEOUT)
            if resp.status_code != 200:
                continue
            for cve, (score, pct) in parse_epss(resp.json()).items():
                out[cve] = (score, pct)
                known[cve] = [score, pct, time.time()]
        except Exception as exc:  # noqa: BLE001
            _log.info("EPSS unavailable", error=describe_exc(exc))
            break
    if missing:
        cache.save("epss.json", known)
    return out


async def resolve_cves(http, cfg, findings: list[Finding], cache: Cache) -> dict[str, str]:
    """``{fingerprint: cve}`` for findings whose id is GHSA/PYSEC-shaped. Aliases are
    immutable, so the OSV answer (including "no CVE") is cached without a TTL."""
    payload, _age = cache.load("osv.json")
    known: dict[str, str] = payload if isinstance(payload, dict) else {}
    out: dict[str, str] = {}
    dirty = False
    lookups = 0
    for f in findings:
        if cve_of(f):
            continue
        rid = (f.rule_id or "").strip()
        # GHSA-xxxx-…, PYSEC-…, RUSTSEC-… — OSV's home databases. "pkg@ver" is not an id.
        if not re.match(r"^(GHSA|PYSEC|RUSTSEC|OSV)-", rid, re.IGNORECASE):
            continue
        if rid in known:
            if known[rid]:
                out[f.fingerprint] = known[rid]
            continue
        if lookups >= _MAX_OSV_LOOKUPS:
            continue
        lookups += 1
        try:
            resp = await http.get(f"{cfg.intel_osv_url.rstrip('/')}/{rid}", timeout=_TIMEOUT)
            cve = parse_osv(resp.json()) if resp.status_code == 200 else ""
            # A 404 is an answer (no such id → no CVE); a network error is not.
            if resp.status_code in (200, 404):
                known[rid] = cve
                dirty = True
            if cve:
                out[f.fingerprint] = cve
        except Exception as exc:  # noqa: BLE001
            _log.info("OSV unavailable", id=rid, error=describe_exc(exc))
            break
    if dirty:
        cache.save("osv.json", known)
    return out


async def run_intel(
    findings: list[Finding], *, workspace: str, cfg, store: bool, http=None,
) -> ToolStatus:
    """Enrich the SCA findings in ``findings`` in place. Never raises.

    ``st.findings`` counts KEV hits — the thing this step *finds*; EPSS coverage and
    cache state ride along in ``extra`` (rendered by ``ToolStatus.label``).
    """
    started = time.monotonic()
    st = ToolStatus("intel")
    sca = [f for f in findings if f.tool == "sca"]
    if not sca:
        st.skipped_reason = "no SCA findings"
        return st
    cache = Cache(workspace, ttl_hours=cfg.intel_cache_hours, persist=store)
    own_client = http is None
    if own_client:
        http = httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        kev, kev_note = await fetch_kev(http, cfg, cache)
        if not kev:
            st.skipped_reason = kev_note or "KEV feed unavailable"
            return st
        cves = await resolve_cves(http, cfg, sca, cache)
        wanted = {c for c in (cves.get(f.fingerprint) or cve_of(f) for f in sca) if c}
        epss = await fetch_epss(http, cfg, wanted, cache)
        stats = enrich(sca, kev, epss, cves)
        st.ran = True
        st.findings = stats["kev"]
        st.extra["epss_scored"] = stats["epss"]
        st.extra["cache"] = kev_note
    except Exception as exc:  # noqa: BLE001 — enrichment must never sink a scan
        st.error = describe_exc(exc)[:200]
    finally:
        if own_client:
            with contextlib.suppress(Exception):
                await http.aclose()
        st.duration_seconds = time.monotonic() - started
    return st
