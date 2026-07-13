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

from ai_autopilot.config import SdlcStage
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


def _profile_map(cfg: Settings) -> dict[str, list[str]]:
    """Built-in profiles with any config-defined profiles merged over them."""
    merged = dict(PROFILES)
    merged.update(cfg.sdlc_profiles or {})
    return merged


def _catalog(cfg: Settings) -> dict[str, SdlcStage]:
    """Built-in catalog plus any custom stages the machine defines in sdlc_stages
    (so a profile may reference a machine-specific stage by name)."""
    cat = dict(CATALOG)
    for stage in cfg.sdlc_stages or []:
        cat[stage.name] = stage
    return cat


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


def resolve_profile_name(tags: list[str], work_item_type: str, cfg: Settings) -> str:
    """The profile NAME chosen for an item (used for handoff-state lookup).

    Precedence: per-item ``sdlc:<name>`` tag override > per-machine explicit
    ``sdlc_stages`` (→ ``"custom"``) > per-machine ``sdlc_profile`` > work-item-type
    map > ``sdlc_default_profile``.
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
    mapped = (cfg.sdlc_type_profiles or {}).get(work_item_type)
    if mapped:
        return mapped
    return cfg.sdlc_default_profile or "full"


def resolve_stages(tags: list[str], work_item_type: str, cfg: Settings) -> list[SdlcStage]:
    """Concrete ordered stage list for an item (see ``resolve_profile_name`` for the
    precedence). An explicit per-machine ``sdlc_stages`` wins directly; otherwise the
    resolved profile is expanded. Unknown profile → default profile (logged)."""
    name = resolve_profile_name(tags, work_item_type, cfg)
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
    """ADO state to set when ``profile_name`` completes — what the next machine's
    ``trigger_states`` picks up. Falls back to ``resolved_state`` then blank."""
    return (cfg.sdlc_profile_states or {}).get(profile_name) or cfg.resolved_state or ""


def handoff_collides(cfg: Settings) -> bool:
    """True if THIS machine's own handoff state is also one of its ``trigger_states``
    — which would make it re-pick items it just finished (an infinite loop). The
    container asserts this is False at startup."""
    name = cfg.sdlc_profile or cfg.sdlc_default_profile or "full"
    hs = handoff_state(name, cfg)
    if not hs:
        return False
    return hs in set(cfg.trigger_states or [])


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
