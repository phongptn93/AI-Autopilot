"""Tests for the PURE SDLC planning core (catalog/profiles/resolve/decide/handoff)."""

from __future__ import annotations

from ai_autopilot.config import SdlcStage, Settings
from ai_autopilot.execution.sdlc_plan import (
    ADVANCE,
    CATALOG,
    ESCALATE,
    PROFILES,
    REVISE,
    StageSignals,
    decide,
    handoff_collides,
    handoff_state,
    is_blocking,
    resolve_profile_name,
    resolve_stages,
    stage_score_input,
)


def _names(stages):
    return [s.name for s in stages]


# ── resolve_stages / resolve_profile_name precedence ──

def test_default_profile_is_full():
    assert _names(resolve_stages([], "Task", Settings())) == PROFILES["full"]


def test_machine_profile_beats_default():
    s = Settings(sdlc_profile="dev")
    assert _names(resolve_stages([], "Task", s)) == ["implement", "review", "pr"]


def test_tag_override_beats_machine_profile():
    s = Settings(sdlc_profile="dev")
    assert _names(resolve_stages(["sdlc:ba"], "Task", s)) == ["analyze"]


def test_tag_override_is_case_insensitive():
    assert _names(resolve_stages(["SDLC:BA"], "Task", Settings(sdlc_profile="dev"))) == ["analyze"]


def test_explicit_machine_stages_beat_profile():
    custom = [SdlcStage(name="implement", role="dev", skill="route")]
    s = Settings(sdlc_profile="full", sdlc_stages=custom)
    assert _names(resolve_stages([], "Task", s)) == ["implement"]
    assert resolve_profile_name([], "Task", s) == "custom"


def test_type_map_used_when_no_machine_profile():
    s = Settings(sdlc_type_profiles={"Bug": "dev"})
    assert _names(resolve_stages([], "Bug", s)) == ["implement", "review", "pr"]
    assert _names(resolve_stages([], "User Story", s)) == PROFILES["full"]  # unmapped → default


def test_unknown_profile_falls_back_to_default():
    s = Settings(sdlc_profile="nope", sdlc_default_profile="dev")
    assert _names(resolve_stages([], "Task", s)) == ["implement", "review", "pr"]


def test_custom_profiles_merge_over_builtins():
    s = Settings(sdlc_profile="mini", sdlc_profiles={"mini": ["implement", "pr"]})
    assert _names(resolve_stages([], "Task", s)) == ["implement", "pr"]


def test_unknown_stage_in_profile_is_skipped():
    s = Settings(sdlc_profile="x", sdlc_profiles={"x": ["implement", "ghost", "pr"]})
    assert _names(resolve_stages([], "Task", s)) == ["implement", "pr"]


def test_catalog_defaults_let_ai_choose_the_skill():
    # Every built-in stage states a goal and pins NO skill → Claude picks it itself.
    for name, stage in CATALOG.items():
        assert stage.goal, f"{name} should have a goal"
        assert stage.skill == "", f"{name} should not hardcode a skill by default"


# ── decide / is_blocking ──

_HARD = CATALOG["implement"]
_SOFT = CATALOG["analyze"]
_REVIEW = CATALOG["review"]


def test_soft_stage_never_blocks():
    assert is_blocking(_SOFT, StageSignals(had_error=True)) is False
    assert decide(_SOFT, False, 0, 3) == ADVANCE


def test_hard_stage_error_blocks_and_revises():
    assert is_blocking(_HARD, StageSignals(had_error=True)) is True
    assert decide(_HARD, True, 0, 3) == REVISE


def test_review_fail_blocks_pass_does_not():
    assert is_blocking(_REVIEW, StageSignals(review_passed=False)) is True
    assert is_blocking(_REVIEW, StageSignals(review_passed=True)) is False
    assert is_blocking(_REVIEW, StageSignals(review_passed=None)) is False


def test_ci_red_blocks_hard_stage():
    assert is_blocking(_HARD, StageSignals(ci_passed=False)) is True


def test_clean_hard_stage_advances():
    assert is_blocking(_HARD, StageSignals(files_changed=3)) is False
    assert decide(_HARD, False, 0, 3) == ADVANCE


def test_budget_boundary_escalates():
    assert decide(_HARD, True, 0, 2) == REVISE
    assert decide(_HARD, True, 1, 2) == REVISE
    assert decide(_HARD, True, 2, 2) == ESCALATE  # 2+1 > 2


# ── handoff ──

def test_handoff_state_from_map():
    s = Settings(sdlc_profile_states={"ba": "Ready for Dev"})
    assert handoff_state("ba", s) == "Ready for Dev"


def test_handoff_state_falls_back_to_resolved():
    assert handoff_state("dev", Settings(resolved_state="Resolved")) == "Resolved"


def test_handoff_state_blank_when_nothing_set():
    assert handoff_state("dev", Settings(resolved_state="")) == ""


def test_handoff_collides_true_and_false():
    collide = Settings(
        sdlc_profile="ba", sdlc_profile_states={"ba": "Active"}, trigger_states=["Active"]
    )
    ok = Settings(
        sdlc_profile="ba", sdlc_profile_states={"ba": "Ready for Dev"}, trigger_states=["Active"]
    )
    assert handoff_collides(collide) is True
    assert handoff_collides(ok) is False


def test_two_machine_handoff_chain():
    """BA machine's output state is exactly what the Dev machine triggers on."""
    ba = Settings(sdlc_profile="ba", sdlc_profile_states={"ba": "Ready for Dev"})
    dev = Settings(
        sdlc_profile="dev", trigger_states=["Ready for Dev"],
        sdlc_profile_states={"dev": "Ready to Test"},
    )
    assert handoff_state("ba", ba) in dev.trigger_states  # BA → Dev picks up
    assert not handoff_collides(dev)                       # Dev doesn't re-pick its own output


# ── signals ──

def test_signals_json_roundtrip():
    sig = StageSignals(files_changed=3, review_critical=2, ci_passed=True)
    assert StageSignals.from_json(sig.to_json()) == sig


def test_signals_from_bad_json_is_default():
    assert StageSignals.from_json("not json") == StageSignals()
    assert StageSignals.from_json("") == StageSignals()


def test_stage_score_input_maps_fields():
    sig = StageSignals(
        completed=True, has_pr=True, files_changed=4, review_passed=True, ci_passed=True
    )
    si = stage_score_input(sig)
    assert si.completed and si.has_pr and si.files_changed == 4
    assert si.review_passed and si.ci_passed
