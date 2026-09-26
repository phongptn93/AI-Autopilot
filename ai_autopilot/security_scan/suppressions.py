"""Suppressions: findings a person has looked at and decided not to act on — yet.

Kept in the WORKSPACE as ``.autopilot/security-suppressions.yaml`` rather than only in
the database, because the CI runner that gates a pipeline on ``ai-autopilot scan`` has
the repo and not the dashboard's SQLite file. The dashboard writes the same file when
someone suppresses from the page, so both readers agree.

Every entry needs a reason and every entry can expire. An accepted risk without a
reason is indistinguishable from a finding someone silenced to get a green build; one
without an expiry is a decision nobody revisits. ``expires`` past → the finding is
back, and the run says so.

Shape::

    - fingerprint: 3f2a9c…
      reason: "test fixture key, never deployed"
      by: phong.pham
      expires: 2026-12-31        # optional; ISO date
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

from ai_autopilot.logging_config import get_logger

_log = get_logger("security_scan.suppressions")

FILE_NAME = "security-suppressions.yaml"


@dataclass
class Suppression:
    fingerprint: str
    reason: str = ""
    by: str = ""
    expires: date | None = None

    def active(self, today: date | None = None) -> bool:
        if not self.expires:
            return True
        return (today or datetime.now(UTC).date()) <= self.expires

    def as_dict(self) -> dict:
        data: dict = {"fingerprint": self.fingerprint, "reason": self.reason}
        if self.by:
            data["by"] = self.by
        if self.expires:
            data["expires"] = self.expires.isoformat()
        return data


@dataclass
class Suppressions:
    entries: list[Suppression] = field(default_factory=list)
    path: Path | None = None

    def active_fingerprints(self, today: date | None = None) -> set[str]:
        return {s.fingerprint for s in self.entries if s.active(today)}

    def expired(self, today: date | None = None) -> list[Suppression]:
        return [s for s in self.entries if not s.active(today)]

    def reason_for(self, fp: str) -> str:
        for s in self.entries:
            if s.fingerprint == fp:
                return s.reason
        return ""

    def add(self, fp: str, reason: str, by: str = "", expires: date | None = None) -> None:
        """Add or replace the entry for ``fp``. Reason is required — see module doc."""
        reason = (reason or "").strip()
        if not fp or not reason:
            raise ValueError("a suppression needs a fingerprint and a reason")
        self.entries = [s for s in self.entries if s.fingerprint != fp]
        self.entries.append(Suppression(fp, reason, by or "", expires))

    def remove(self, fp: str) -> bool:
        before = len(self.entries)
        self.entries = [s for s in self.entries if s.fingerprint != fp]
        return len(self.entries) != before


def file_for(workspace: str | Path) -> Path:
    return Path(workspace) / ".autopilot" / FILE_NAME


def _coerce_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def load(workspace: str | Path) -> Suppressions:
    """Read the file; a missing or malformed file is an empty list, logged, never fatal —
    a scan that refuses to run because a yaml comma is wrong helps nobody."""
    path = file_for(workspace)
    result = Suppressions(path=path)
    if not path.is_file():
        return result
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError) as exc:
        _log.warning("suppressions file unreadable", path=str(path), error=str(exc))
        return result
    rows = data.get("suppressions") if isinstance(data, dict) else data
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("fingerprint"):
            continue
        result.entries.append(Suppression(
            fingerprint=str(row["fingerprint"]).strip(),
            reason=str(row.get("reason") or "").strip(),
            by=str(row.get("by") or "").strip(),
            expires=_coerce_date(row.get("expires")),
        ))
    return result


def save(sup: Suppressions, workspace: str | Path | None = None) -> Path:
    path = sup.path or file_for(workspace or ".")
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"suppressions": [s.as_dict() for s in sorted(sup.entries, key=lambda s: s.fingerprint)]}
    path.write_text(
        "# Security findings accepted or deferred — every entry needs a reason.\n"
        "# Managed by ai-autopilot (dashboard → Security → Suppress) and by hand.\n"
        + yaml.safe_dump(body, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    sup.path = path
    return path
