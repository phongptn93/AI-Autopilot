"""Stable identity for a finding, and the diff between two scans.

A finding's line number is the least stable thing about it — every edit above it moves
it — so the fingerprint deliberately excludes it. What it keeps is the tool, the rule,
the file, and a normalised version of the offending code (or the title, for findings
that carry no snippet). Two runs a week apart that report the same secret on the same
file agree on the fingerprint even after the file grew by fifty lines.

That identity is what a baseline is built on: ``diff`` answers "which of these are
new", which is the only question a nightly scan is really asked.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from ai_autopilot.reports import SEVERITIES, Finding

_WS = re.compile(r"\s+")
_NUMS = re.compile(r"\b\d+\b")


def _normalise(text: str) -> str:
    """Whitespace-collapsed, digit-blind, case-insensitive — the shape of a line, not
    its exact bytes. Digits are blanked so a moved line number inside a snippet (or a
    rotated key's last characters) does not become a "new" finding."""
    text = _WS.sub(" ", (text or "").strip().lower())
    return _NUMS.sub("#", text)[:400]


def fingerprint(f: Finding) -> str:
    """The stable id for ``f``. Idempotent: returns an existing one unchanged.

    ``file`` is normalised to forward slashes because the same finding is reported with
    backslashes on a Windows runner and slashes on Linux, and those must not diverge.
    """
    if f.fingerprint:
        return f.fingerprint
    path = (f.file or "").replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    tool = (f.tool or "ai").lower()
    rule = (f.rule_id or "").lower().strip()
    body = _normalise(f.snippet) if f.snippet else _normalise(f.title)
    raw = "|".join((tool, rule, path, body))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def stamp(findings: list[Finding]) -> list[Finding]:
    """Fill ``fingerprint`` on every finding; returns the same list for chaining."""
    for f in findings:
        f.fingerprint = fingerprint(f)
    return findings


def dedupe(findings: list[Finding]) -> list[Finding]:
    """One finding per fingerprint, keeping the WORST severity seen for it.

    Two scanners raising the same thing (semgrep and the builtin rules both flag a raw
    SQL concat) is one problem, not two. Kept in first-seen order after the severity
    sort so the output is stable across runs.
    """
    best: dict[str, Finding] = {}
    for f in stamp(findings):
        current = best.get(f.fingerprint)
        if current is None or SEVERITIES.index(f.severity) < SEVERITIES.index(current.severity):
            best[f.fingerprint] = f
    out = list(best.values())
    out.sort(key=lambda f: (SEVERITIES.index(f.severity), f.file, f.line or 0))
    return out


@dataclass
class Diff:
    """What changed between this scan and the baseline it was compared with."""

    new: list[Finding] = field(default_factory=list)
    persisting: list[Finding] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)   # fingerprints no longer reported

    @property
    def counts(self) -> dict[str, int]:
        return {"new": len(self.new), "persisting": len(self.persisting), "fixed": len(self.fixed)}


def diff(current: list[Finding], baseline: set[str] | list[str] | None) -> Diff:
    """Split ``current`` by whether its fingerprint was in ``baseline``.

    ``baseline`` None means "no earlier scan": everything is new, nothing is fixed —
    which is the honest answer, not a zero.
    """
    stamp(current)
    if baseline is None:
        return Diff(new=list(current))
    known = set(baseline)
    seen = {f.fingerprint for f in current}
    return Diff(
        new=[f for f in current if f.fingerprint not in known],
        persisting=[f for f in current if f.fingerprint in known],
        fixed=sorted(known - seen),
    )
