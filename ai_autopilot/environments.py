"""What is running where, and how production incidents are going.

Two questions DevSecOps and the PM ask several times a week and the board cannot
answer: *which build is on UAT / Production right now, and what is in it?* and *how
many production incidents are open, and how fast do we restore?*

Environments are declared in ``deploy_environments`` ("UAT = 45 | release"); each is
the newest successful build of that pipeline on that branch. An incident is a defect
found in production (``Found In Environment``) or an item carrying a ``hotfix_tags``
tag. Pure functions; the route gathers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ai_autopilot.defect_metrics import DONE_CATEGORIES, REMOVED_CATEGORY, is_defect
from ai_autopilot.process_health import HealthItem

PROD_ENVIRONMENTS = ("production", "prod")


@dataclass(frozen=True)
class EnvSpec:
    name: str
    pipeline: int
    branch: str


def parse_environments(cfg) -> list[EnvSpec]:
    """``deploy_environments`` lines → specs; malformed lines are skipped. Blank config
    falls back to the single deploy scope the state-sync already watches."""
    out: list[EnvSpec] = []
    for line in getattr(cfg, "deploy_environments", None) or []:
        name, sep, rest = str(line).partition("=")
        if not sep or not name.strip():
            continue
        pipe, _, branch = rest.partition("|")
        try:
            pipeline = int(pipe.strip() or 0)
        except ValueError:
            continue
        branch = branch.strip()
        if branch:
            out.append(EnvSpec(name.strip(), pipeline, branch))
    if not out:
        branch = (getattr(cfg, "deploy_branch", "") or getattr(cfg, "base_branch", "")
                  or "").strip()
        if branch:
            out.append(EnvSpec("Deploy", int(getattr(cfg, "deploy_pipeline_id", 0) or 0),
                               branch))
    return out


def _dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class Deployment:
    build_id: int
    number: str
    commit: str
    finished: datetime | None
    requested_for: str
    url: str = ""


def deployment(build: dict) -> Deployment:
    return Deployment(
        build_id=int(build.get("id") or 0),
        number=str(build.get("buildNumber") or ""),
        commit=str(build.get("sourceVersion") or ""),
        finished=_dt(build.get("finishTime")),
        requested_for=str((build.get("requestedFor") or {}).get("displayName") or ""),
        url=str(((build.get("_links") or {}).get("web") or {}).get("href") or ""),
    )


@dataclass
class EnvView:
    spec: EnvSpec
    status: str = "ok"            # "ok" | "empty" (never deployed) | "unknown"
    current: Deployment | None = None
    history: list[Deployment] = field(default_factory=list)
    items: list[tuple[int, str]] = field(default_factory=list)   # (id, title) in current

    @property
    def age_days(self) -> float | None:
        if self.current is None or self.current.finished is None:
            return None
        return round((datetime.now(UTC) - self.current.finished).total_seconds() / 86400, 1)


def env_view(spec: EnvSpec, builds: list[dict] | None) -> EnvView:
    if builds is None:
        return EnvView(spec, status="unknown")
    deploys = sorted((deployment(b) for b in builds if isinstance(b.get("id"), int)),
                     key=lambda d: d.finished or datetime.min.replace(tzinfo=UTC), reverse=True)
    if not deploys:
        return EnvView(spec, status="empty")
    return EnvView(spec, current=deploys[0], history=deploys[:5])


def is_incident(item: HealthItem, hotfix_tags) -> bool:
    env = (item.found_in_environment or "").strip().lower()
    if is_defect(item) and any(env.startswith(p) for p in PROD_ENVIRONMENTS):
        return True
    return any(item.has_tag(t) for t in hotfix_tags or () if t)


@dataclass
class Incident:
    id: int
    title: str
    state: str
    assigned_to: str
    age_days: float
    severity: str


@dataclass
class IncidentReport:
    days: int
    opened: int = 0
    open: list[Incident] = field(default_factory=list)
    restore_days: list[float] = field(default_factory=list)

    @property
    def restore_p50_days(self) -> float | None:
        if not self.restore_days:
            return None
        s = sorted(self.restore_days)
        mid = len(s) // 2
        return round(s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2, 1)


def incidents(items: list[HealthItem], *, categories: dict[str, str], hotfix_tags,
              days: int, now: datetime | None = None) -> IncidentReport:
    now = now or datetime.now(UTC)
    start = now - timedelta(days=days)
    rep = IncidentReport(days=days)
    for it in items:
        if not is_incident(it, hotfix_tags):
            continue
        cat = categories.get(it.state, "")
        if cat == REMOVED_CATEGORY:
            continue
        created = _utc(it.created)
        if created and created >= start:
            rep.opened += 1
        if cat in DONE_CATEGORIES:
            closed = _utc(it.closed) or _utc(it.changed)
            if closed and closed >= start and created:
                rep.restore_days.append(round((closed - created).total_seconds() / 86400, 2))
        else:
            age = (now - created).total_seconds() / 86400 if created else 0.0
            rep.open.append(Incident(it.id, it.title, it.state, it.assigned_to, round(age, 1),
                                     it.severity))
    rep.open.sort(key=lambda i: (-i.age_days, i.id))
    return rep


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)
