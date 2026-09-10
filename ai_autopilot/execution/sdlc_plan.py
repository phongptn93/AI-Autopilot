"""Closed-loop SDLC engine — the PURE planning core (Phase 1).

Everything here is a deterministic function of its inputs — no I/O — so the whole
stage machine is trivially unit-testable, exactly like ``pr_scorer.score_run``.
The impure orchestration (running skills, git, ADO) lives in ``sdlc_loop.py``.

Responsibilities:

* **Catalog + profiles** — the fixed set of SDLC stages and named ordered subsets
  of them; a machine (or an item tag) selects which subset runs.
* **resolve_stages** — per-item stage selection with a clear precedence ladder,
  primary knob being the PER-MACHINE profile.
* **decide** — the sole transition authority: advance / revise / escalate, from a
  stage's gate and the shared iteration budget.
* **handoff_state / handoff_collides** — the ADO state a completed profile sets so
  the NEXT machine's role picks the item up (the cross-machine loop).
* **StageSignals / stage_score_input** — accumulate objective signals across
  stages and turn them into a ``pr_scorer.ScoreInput``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from ai_autopilot.config import SdlcRole, SdlcStage, stage_wiring_value
from ai_autopilot.execution.pr_scorer import ScoreInput
from ai_autopilot.logging_config import get_logger

if TYPE_CHECKING:
    from ai_autopilot.config import Settings

_log = get_logger("execution.sdlc_plan")

# Transition decisions.
ADVANCE = "advance"
REVISE = "revise"
ESCALATE = "escalate"

# ── Built-in stage CATALOG (name → stage). Each states a GOAL and lets Claude pick
# the skill; `skill` is left blank on purpose (override per machine to pin one). ──
CATALOG: dict[str, SdlcStage] = {
    "analyze": SdlcStage(
        name="analyze", role="ba",
        goal="Analyze the requirement — clarify scope, write user stories and acceptance criteria.",
        artifact_skill="technical-note", gate="soft",
    ),
    "design": SdlcStage(
        name="design", role="design",
        goal="Design the technical approach / API contract for the requirement.",
        artifact_skill="architecture-report", gate="soft",
    ),
    "implement": SdlcStage(
        name="implement", role="dev",
        goal="Implement the work item end-to-end (backend/frontend/DB as appropriate).",
        gate="hard",
    ),
    "test": SdlcStage(
        name="test", role="qc",
        goal="Create and/or execute test cases for the item (QC).", gate="hard",
    ),
    "review": SdlcStage(
        name="review", role="review",
        goal="Review the change for correctness and security.",
        artifact_skill="bugfix-report", gate="hard",
    ),
    "pr": SdlcStage(
        name="pr", role="pr",
        goal="Open a pull request for the branch with a clear description.",
        gate="hard", produces_pr=True,
    ),
}

# ── Built-in PROFILES (name → ordered stage names). Overridable via config. ──
PROFILES: dict[str, list[str]] = {
    "full": ["analyze", "design", "implement", "test", "review", "pr"],
    "dev": ["implement", "review", "pr"],
    "ba": ["analyze"],
    "qc": ["test"],
    "review": ["review"],
    "design": ["design"],
}


def role_doors(role: object) -> list[str]:
    """Every ADO state this role waits in — a role may have more than one door.

    One door was too few for a real board. A developer takes new work from
    "Ready for Development" AND takes rejected work back from "Rework Required": same
    person, same stages, two queues. With a single ``waits_in`` the second queue had no
    owner at all, and the only way to start it was to tag the item by hand.

    Stored as ONE comma-separated string rather than a list so every config written
    before this reads back unchanged and no migration is needed; blank entries and
    duplicates are dropped, order is kept.
    """
    raw = getattr(role, "waits_in", "") if not isinstance(role, str) else role
    out: list[str] = []
    for part in str(raw or "").replace(chr(10), ",").split(","):
        door = part.strip()
        if door and door.lower() not in {d.lower() for d in out}:
            out.append(door)
    return out


def first_door(role: object) -> str:
    """The role's primary door — what a one-line summary should name."""
    doors = role_doors(role)
    return doors[0] if doors else ""


def effective_roles(cfg: Settings) -> dict[str, SdlcRole]:
    """Every role this machine knows: what it runs, and its way in and out.

    ``sdlc_roles`` is the source of truth. When it is empty the same shape is DERIVED
    from the four deprecated keys, so an install that predates the Roles page keeps
    behaving identically until somebody saves that page once.

    Derivation reproduces the old rules exactly, including the one the new shape makes
    impossible: a stage-keyed door with ``runs_profile`` set belonged to THAT profile
    alone, so every other profile sharing the entry stage got no door.
    """
    if cfg.sdlc_roles:
        return dict(cfg.sdlc_roles)

    stage_sets = dict(PROFILES)
    stage_sets.update(cfg.sdlc_profiles or {})
    wiring_by_stage = cfg.sdlc_stage_wiring or {}
    roles: dict[str, SdlcRole] = {}
    for name, stage_names in stage_sets.items():
        door = SdlcRole(stages=list(stage_names))
        entry = stage_names[0] if stage_names else ""
        wiring = wiring_by_stage.get(entry)
        if wiring is not None:
            owner = str(stage_wiring_value(wiring, "runs_profile", "") or "").strip()
            # An owned door belongs to one profile; the others sharing the entry stage
            # had none, which is precisely the tie `runs_profile` existed to break.
            if not owner or owner == name:
                door.waits_in = str(stage_wiring_value(wiring, "queue_state", "") or "")
                door.shows = str(stage_wiring_value(wiring, "working_state", "") or "")
                door.entry_tag = str(stage_wiring_value(wiring, "entry_tag", "") or "")
                door.auto = bool(stage_wiring_value(wiring, "auto", False))
        door.done = (cfg.sdlc_profile_states or {}).get(name, "")
        door.done_tag = (cfg.sdlc_profile_tags or {}).get(name, "")
        roles[name] = door
    return roles


def _profile_map(cfg: Settings) -> dict[str, list[str]]:
    """``role -> ordered stage names``, from the role definitions."""
    return {name: list(role.stages) for name, role in effective_roles(cfg).items()}


def _catalog(cfg: Settings) -> dict[str, SdlcStage]:
    """Built-in catalog, custom stages, then the board wiring overlaid on both.

    The overlay exists so that saying "test waits in Ready for Testing" does not
    require restating the stage's goal and gate just to reach the one field you
    wanted. Wiring is the operator's half of a stage; the rest ships with the tool.
    """
    cat = dict(CATALOG)
    for stage in cfg.sdlc_stages or []:
        cat[stage.name] = stage
    for name, wiring in (cfg.sdlc_stage_wiring or {}).items():
        base = cat.get(name)
        if base is None:
            _log.warning("sdlc: wiring for an unknown stage — ignored", stage=name)
            continue
        cat[name] = base.model_copy(update={
            f: stage_wiring_value(wiring, f, False if f == "auto" else "")
            for f in ("queue_state", "working_state", "entry_tag", "auto", "runs_profile")
        })
    return cat


def stage_catalog(cfg: Settings) -> dict[str, SdlcStage]:
    """Every stage this machine knows, wiring applied — what the Relay page edits."""
    return _catalog(cfg)


def profile_map(cfg: Settings) -> dict[str, list[str]]:
    """``profile -> ordered stage names``, built-ins plus config overrides."""
    return _profile_map(cfg)


def profile_names(cfg: Settings) -> list[str]:
    """Every profile this machine can run, built-ins plus config overrides."""
    return sorted(_profile_map(cfg))


def profile_stages(name: str, cfg: Settings) -> list[SdlcStage]:
    """Concrete ordered stages for a profile name. Unknown stage names are skipped
    with a warning (never crash); an unknown profile yields ``[]`` (caller falls back)."""
    names = _profile_map(cfg).get(name)
    if names is None:
        return []
    cat = _catalog(cfg)
    stages: list[SdlcStage] = []
    for stage_name in names:
        stage = cat.get(stage_name)
        if stage is None:
            _log.warning("sdlc: unknown stage in profile — skipped", profile=name, stage=stage_name)
            continue
        stages.append(stage)
    return stages


def entry_stage(profile_name: str, cfg: Settings) -> SdlcStage | None:
    """The first stage a role runs. Kept for callers that want the stage object; the
    role's door no longer lives on it."""
    stages = profile_stages(profile_name, cfg)
    return stages[0] if stages else None


def profile_for_state(state: str, cfg: Settings) -> str:
    """The role an item in this ADO state should run, or "" if none claims it.

    This is the piece a central machine cannot work without. Role is not a property
    of the machine (one box runs them all) nor of the work-item type (a Bug is a Bug
    at every step); it is where the item stands right now, which only the ADO state
    records.

    Two roles claiming one state is a genuine contradiction — the state cannot say
    which role is due — so it is refused rather than broken by iteration order. It is
    also now visible: both rows show the same door on one page.
    """
    wanted = (state or "").strip().lower()
    if not wanted:
        return ""
    matches = [
        name for name, role in sorted(effective_roles(cfg).items())
        if any(d.lower() == wanted for d in role_doors(role))
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        _log.warning(
            "sdlc: two roles wait in the same state — give each its own",
            state=state, roles=matches,
        )
    return ""


def auto_states(cfg: Settings) -> list[str]:
    """Door states marked ``auto`` — the hand-offs that self-start."""
    out: list[str] = []
    for role in effective_roles(cfg).values():
        for door in role_doors(role) if role.auto else ():
            if door not in out:
                out.append(door)
    return out


def waiting_states(cfg: Settings) -> list[str]:
    """Door states explicitly NOT auto — they wait for a person to press Run."""
    out: list[str] = []
    for role in effective_roles(cfg).values():
        for door in () if role.auto else role_doors(role):
            if door not in out:
                out.append(door)
    return out


def entry_tag_for(profile_name: str, cfg: Settings) -> str:
    """The one-shot tag that starts this role regardless of the item's state."""
    role = effective_roles(cfg).get(profile_name)
    own = (role.entry_tag or "").strip() if role else ""
    return own or (cfg.stage_entry_tag or "").strip()


def entry_tags(cfg: Settings) -> dict[str, str]:
    """``lower(tag) -> role`` for every role that names its own run-now tag.

    The machine-wide tag maps to whichever role the item's CURRENT state names, so it
    is resolved at pickup rather than listed here.
    """
    out: dict[str, str] = {}
    for name, role in sorted(effective_roles(cfg).items()):
        own = (role.entry_tag or "").strip()
        if own:
            out[own.lower()] = name
    return out


def working_state_for(profile_name: str, cfg: Settings) -> str:
    """ADO state to show while this role runs. Blank → the global one.

    Two roles running at once on one machine both reading "Active" is a board that
    cannot say who is holding the work — which is the whole reason a QC run should
    read "In Testing".
    """
    role = effective_roles(cfg).get(profile_name)
    return (role.shows or "").strip() if role else ""


def resolve_profile_name(tags: list[str], work_item_type: str, cfg: Settings,
                         state: str = "") -> str:
    """The profile NAME chosen for an item (used for handoff-state lookup).

    Precedence: per-item ``sdlc:<name>`` tag override > per-machine explicit
    ``sdlc_stages`` (→ ``"custom"``) > per-machine ``sdlc_profile`` > the item's ADO
    ``state`` > work-item-type map > ``sdlc_default_profile``.

    State sits above the type map because it is the more specific fact: a Bug is a
    Bug at every step of the relay, but the state says which step it is ON. It sits
    below the machine pin and the explicit tag because those are somebody stating an
    intention, and an intention outranks an inference.
    """
    prefix = (cfg.sdlc_profile_tag_prefix or "sdlc:").lower()
    profiles = _profile_map(cfg)
    for tag in tags or []:
        t = (tag or "").strip().lower()
        if t.startswith(prefix):
            name = t[len(prefix):]
            if name in profiles:
                return name
    if cfg.sdlc_stages:
        return "custom"
    if cfg.sdlc_profile:
        return cfg.sdlc_profile
    by_state = profile_for_state(state, cfg)
    if by_state:
        return by_state
    mapped = (cfg.sdlc_type_profiles or {}).get(work_item_type)
    if mapped:
        return mapped
    return cfg.sdlc_default_profile or "full"


def resolve_stages(tags: list[str], work_item_type: str, cfg: Settings,
                   state: str = "") -> list[SdlcStage]:
    """Concrete ordered stage list for an item (see ``resolve_profile_name`` for the
    precedence). An explicit per-machine ``sdlc_stages`` wins directly; otherwise the
    resolved profile is expanded. Unknown profile → default profile (logged)."""
    name = resolve_profile_name(tags, work_item_type, cfg, state=state)
    if name == "custom":
        return list(cfg.sdlc_stages)
    stages = profile_stages(name, cfg)
    if stages:
        return stages
    # Unknown/empty profile → fall back to the default, never crash.
    _log.warning(
        "sdlc: unknown profile — using default", profile=name, default=cfg.sdlc_default_profile
    )
    return profile_stages(cfg.sdlc_default_profile or "full", cfg)


def is_blocking(stage: SdlcStage, signals: StageSignals) -> bool:
    """Whether a stage's outcome must be revised before advancing.

    ``score_run`` produces a headline 0–100 badge for humans, but its PR-shaped
    rubric can't fairly gate an intermediate stage (an ``implement`` run has no PR
    yet, so it could never reach the auto threshold). So the hard gate keys off
    *explicit negative signals* instead: the skill errored, a review found blocking
    issues, or CI is red. A ``soft`` stage never blocks (advisory only)."""
    if stage.gate != "hard":
        return False
    if signals.had_error:
        return True
    if stage.role == "review" and signals.review_passed is False:
        return True
    return signals.ci_passed is False


def decide(stage: SdlcStage, blocking: bool, iterations: int, budget: int) -> str:
    """The sole transition authority.

    - not blocking (or soft stage) → ADVANCE.
    - blocking hard stage → REVISE until the SHARED budget is spent, then ESCALATE.
    """
    if not blocking:
        return ADVANCE
    if iterations + 1 > budget:
        return ESCALATE
    return REVISE


def handoff_state(profile_name: str, cfg: Settings) -> str:
    """ADO state to set when ``profile_name`` completes — the next role's front door.

    A role's ``done`` is the next role's ``waits_in``; that is the whole chain, and
    keeping both halves on one object is what lets a reader see it. Falls back to
    ``resolved_state`` then blank.
    """
    role = effective_roles(cfg).get(profile_name)
    own = (role.done or "").strip() if role else ""
    return own or cfg.resolved_state or ""


def handoff_tag(profile_name: str, cfg: Settings) -> str:
    """Tag to add when ``profile_name`` completes — the no-ADO-change hand-off.

    The poller skips anything carrying the processed / review / hold / live tags, so
    a completed profile already stops there; this tag is what says WHO it stopped
    for, which a board lane then claims. Blank = no tag hand-off for that profile.
    """
    role = effective_roles(cfg).get(profile_name)
    return (role.done_tag or "").strip() if role else ""


def handoff_collisions(cfg: Settings) -> list[tuple[str, str]]:
    """``(profile, state)`` pairs whose hand-off lands back in a trigger state.

    Every profile the machine could run, not just one. The old check read only the
    pinned/default profile, which was right when a machine ran a single role and
    handed off to the NEXT machine. A central machine runs them all, so checking one
    profile leaves the other hand-offs unguarded — the same blind spot that let a
    hand-off state be used as a trigger and rework finished work.
    """
    if cfg.sdlc_profile:
        names = [cfg.sdlc_profile]
    else:
        names = sorted(set(_profile_map(cfg)) | {cfg.sdlc_default_profile or "full"})
    triggers = {s.strip().lower() for s in (cfg.trigger_states or []) if s.strip()}
    out: list[tuple[str, str]] = []
    for name in names:
        hs = handoff_state(name, cfg).strip()
        if hs and hs.lower() in triggers:
            out.append((name, hs))
    return out


def handoff_collides(cfg: Settings) -> bool:
    """True if any profile's hand-off state is also one of this machine's
    ``trigger_states`` — it would re-pick items it just finished (an infinite loop).
    The container asserts this is False at startup."""
    return bool(handoff_collisions(cfg))


@dataclass
class StageSignals:
    """Objective signals accumulated across an item's stages, persisted between
    stage runs so the score after ``review``/``pr`` reflects everything gathered."""

    completed: bool = False
    has_pr: bool = False
    files_changed: int = 0
    needs_human: bool = False
    had_error: bool = False
    review_passed: bool | None = None
    review_critical: int = 0
    review_warnings: int = 0
    ci_passed: bool | None = None
    unresolved_threads: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> StageSignals:
        try:
            data = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            data = {}
        known = {k: data[k] for k in data if k in cls.__dataclass_fields__}
        return cls(**known)


def stage_score_input(signals: StageSignals) -> ScoreInput:
    """Map accumulated signals into a ``pr_scorer.ScoreInput`` for gating."""
    return ScoreInput(
        completed=signals.completed,
        has_pr=signals.has_pr,
        files_changed=signals.files_changed,
        needs_human=signals.needs_human,
        had_error=signals.had_error,
        review_passed=signals.review_passed,
        review_critical=signals.review_critical,
        review_warnings=signals.review_warnings,
        ci_passed=signals.ci_passed,
        unresolved_threads=signals.unresolved_threads,
    )
