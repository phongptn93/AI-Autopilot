"""Change requests: the register, requirement volatility, and silent scope changes.

A signed spec is a contract. When the customer asks for something different, the change
has to be raised, decided and traced — otherwise QC tests against an AC nobody agreed
to and the acceptance meeting argues about what was promised. Three things the BA desk
needs and did not have:

* **The register** — every change request (``change_request_tags`` or the *Change
  Request* type), open or decided, with its age and p50 time to decide.
* **Volatility** — requirements that attracted more than one CR, the ones whose scope is
  not settled.
* **Silent changes** — specs the customer confirmed that have changed since
  (``ba_view.STALE``) with no change request against their work item. This is exactly
  where the code and the signed document drift apart without anyone deciding it.

Pure; the BA route gathers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

DONE = {"Resolved", "Completed"}
CR_TYPE = "change request"


def _dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _tags(fields: dict) -> set[str]:
    return {t.strip().lower() for t in str(fields.get("System.Tags") or "").split(";")
            if t.strip()}


def is_change_request(fields: dict, cr_tags) -> bool:
    if str(fields.get("System.WorkItemType") or "").strip().lower() == CR_TYPE:
        return True
    wanted = {t.strip().lower() for t in cr_tags or () if t and t.strip()}
    return bool(wanted & _tags(fields))


_REF = re.compile(r"#(\d{2,})")


@dataclass
class ChangeRequest:
    id: int
    title: str
    state: str
    category: str
    parent: int
    refs: set[int]               # every work item it names: parent + "#123" in the title
    created: datetime | None
    closed: datetime | None
    assigned_to: str

    @property
    def decided(self) -> bool:
        return self.category in DONE or self.category == "Removed"

    def age_days(self, now: datetime) -> float | None:
        return round((now - self.created).total_seconds() / 86400, 1) if self.created else None


@dataclass
class SilentChange:
    item_id: int
    title: str
    rel: str            # spec path in the workspace


@dataclass
class CrReport:
    days: int
    crs: list[ChangeRequest] = field(default_factory=list)
    opened: int = 0
    decide_days: list[float] = field(default_factory=list)
    volatile: list[tuple[int, int]] = field(default_factory=list)   # (requirement, CR count)
    silent: list[SilentChange] = field(default_factory=list)

    @property
    def open(self) -> list[ChangeRequest]:
        return [c for c in self.crs if not c.decided]

    @property
    def decide_p50_days(self) -> float | None:
        if not self.decide_days:
            return None
        s = sorted(self.decide_days)
        mid = len(s) // 2
        return round(s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2, 1)


def build(raw: list[dict], *, categories: dict[str, str], cr_tags, stale_specs: list[dict],
          days: int, now: datetime | None = None) -> CrReport:
    """``stale_specs``: spec rows whose customer confirmation is older than the spec,
    each ``{"item_id", "title", "rel"}``."""
    now = now or datetime.now(UTC)
    start = now - timedelta(days=days)
    rep = CrReport(days=days)
    for f in raw:
        if not is_change_request(f, cr_tags):
            continue
        state = str(f.get("System.State") or "")
        parent = int(f.get("System.Parent") or 0)
        title = str(f.get("System.Title") or "")
        who = f.get("System.AssignedTo")
        cr = ChangeRequest(
            id=int(f.get("System.Id") or 0), title=title, state=state,
            category=categories.get(state, ""), parent=parent,
            refs={parent, *(int(m) for m in _REF.findall(title))} - {0},
            created=_dt(f.get("System.CreatedDate")),
            closed=_dt(f.get("Microsoft.VSTS.Common.ClosedDate")),
            assigned_to=str((who.get("displayName") if isinstance(who, dict) else who) or ""),
        )
        rep.crs.append(cr)
        if cr.created and cr.created >= start:
            rep.opened += 1
        if cr.decided and cr.created and cr.closed and cr.closed >= start:
            rep.decide_days.append(round((cr.closed - cr.created).total_seconds() / 86400, 2))
    counts: dict[int, int] = {}
    for cr in rep.crs:
        for ref in cr.refs:
            counts[ref] = counts.get(ref, 0) + 1
    rep.volatile = sorted(((k, n) for k, n in counts.items() if n >= 2),
                          key=lambda kv: (-kv[1], kv[0]))
    covered = set(counts)
    rep.silent = [SilentChange(int(s.get("item_id") or 0), str(s.get("title") or ""),
                               str(s.get("rel") or ""))
                  for s in stale_specs if int(s.get("item_id") or 0) not in covered]
    rep.crs.sort(key=lambda c: (c.decided, -(c.age_days(now) or 0), c.id))
    return rep
