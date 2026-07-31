"""Tests for per-work-item-type state flows (``ai_autopilot.flows``).

The states used here are the real ones from a live project, because the whole point of
this module is agreeing with what ADO actually defines: ``Ready to Deploy`` exists on
Bug and Task only, ``Implement Done`` on Requirement only, and ``Feature`` has just the
four generic states. A fictional fixture would let the validator pass on cases that
break in production.
"""

from __future__ import annotations

from ai_autopilot.config import Settings
from ai_autopilot.flows import (
    STAGE_KEYS,
    flow_for_type,
    parse_flow_form,
    parse_rollup_entry,
    resolve_state,
    resolve_tag,
    should_comment,
    uncovered_types,
    validate_flows,
)

STATES_BY_TYPE = {
    "Bug": ["Proposed", "Review Fails", "Active", "Ready to Deploy", "Ready to Review",
            "Ready to Testing", "Closed"],
    "Task": ["Proposed", "Review Fails", "Active", "Ready to Review", "Ready to Deploy",
             "Ready to Testing", "Closed"],
    "Requirement": ["Proposed", "Pending", "To Do", "Active", "Resolved",
                    "Writing Specification", "Specification Done", "QC Fails",
                    "Implement Done", "Testing", "Closed"],
    "Feature": ["Proposed", "Active", "Resolved", "Closed"],
}

DEV = {
    "name": "Dev items", "types": ["Bug", "Task"],
    "states": {"in_progress": "Active", "review": "Ready to Review", "done": "Closed",
               "report": "", "needs_human": "Review Fails", "failed": "Review Fails",
               "on_merge": "Ready to Deploy", "on_deploy": "Ready to Testing"},
}
REQ = {
    "name": "Requirement", "types": ["Requirement"],
    "states": {"on_merge": "Implement Done", "on_deploy": "Testing",
               "needs_human": "QC Fails"},
    "rollup": ["Ready to Testing = Implement Done", "Active = Active"],
}


# ── Resolution ────────────────────────────────────────────────────────────────

def test_no_flows_configured_keeps_the_legacy_flat_behaviour():
    """The upgrade must be a no-op until someone defines a flow — an installation that
    never opens the Flow page has to keep transitioning exactly as it did."""
    cfg = Settings(on_merge_state="Ready to Deploy", state_in_review="Active")
    assert resolve_state(cfg, "on_merge", "Requirement") == "Ready to Deploy"
    assert resolve_state(cfg, "review", "Bug") == "Active"
    assert flow_for_type(cfg, "Bug") is None


def test_flow_state_wins_per_type():
    cfg = Settings(work_item_flows=[DEV, REQ], on_merge_state="Ready to Deploy")
    assert resolve_state(cfg, "on_merge", "Bug") == "Ready to Deploy"
    assert resolve_state(cfg, "on_merge", "Requirement") == "Implement Done"
    assert resolve_state(cfg, "on_deploy", "Requirement") == "Testing"
    # Type matching ignores case — ADO returns the display name, config may differ.
    assert resolve_state(cfg, "on_merge", "bug") == "Ready to Deploy"


def test_type_outside_every_flow_falls_back_to_the_flat_setting():
    """A type nobody grouped is the dangerous case: it must be visibly on the old
    behaviour, not silently on some other group's states."""
    cfg = Settings(work_item_flows=[DEV, REQ], on_merge_state="Resolved")
    assert resolve_state(cfg, "on_merge", "Feature") == "Resolved"
    assert uncovered_types([DEV, REQ], STATES_BY_TYPE) == ["Feature"]


def test_absent_stage_inherits_but_explicit_blank_skips():
    """A flow overrides only what it names, so an absent key inherits the flat value;
    a key present as "" means "set no state here" and must NOT fall back."""
    cfg = Settings(work_item_flows=[DEV, REQ], state_report="Resolved")
    assert resolve_state(cfg, "report", "Requirement") == "Resolved"   # absent → inherit
    assert resolve_state(cfg, "report", "Bug") == ""                   # explicit "" → skip


def test_blank_type_resolves_to_the_flat_setting():
    """Callers that genuinely don't know the type (no item in hand) must still work."""
    cfg = Settings(work_item_flows=[DEV], on_merge_state="Resolved")
    assert resolve_state(cfg, "on_merge", "") == "Resolved"


def test_tag_and_comment_actions_default_then_override():
    cfg = Settings(work_item_flows=[
        {"name": "Dev", "types": ["Bug"], "tags": {"done": "shipped"},
         "comment": {"on_merge": False}},
    ])
    assert resolve_tag(cfg, "done", "Bug", "autopilot-done") == "shipped"
    assert resolve_tag(cfg, "review", "Bug", "autopilot-review") == "autopilot-review"
    assert resolve_tag(cfg, "done", "Feature", "autopilot-done") == "autopilot-done"
    assert should_comment(cfg, "on_merge", "Bug") is False
    assert should_comment(cfg, "on_deploy", "Bug") is True     # unnamed → default
    assert should_comment(cfg, "on_merge", "Feature") is True  # no flow → default


def test_malformed_flow_entries_are_ignored_not_crashed_on():
    """Hand-edited YAML must not be able to take the poller down mid-transition."""
    cfg = Settings(work_item_flows=[
        "nonsense", {"types": "Bug"}, {"types": ["Bug"], "states": "x"},
    ])
    assert resolve_state(cfg, "on_merge", "Bug") == ""
    assert should_comment(cfg, "on_merge", "Bug") is True


def test_parse_rollup_entry():
    assert parse_rollup_entry("Ready to Testing = Implement Done") == (
        "Ready to Testing", "Implement Done")
    assert parse_rollup_entry("Active : Active") == ("Active", "Active")
    assert parse_rollup_entry("Closed") == ("Closed", "Closed")


# ── Validation ────────────────────────────────────────────────────────────────

def test_a_complete_valid_configuration_passes():
    assert validate_flows([DEV, REQ], STATES_BY_TYPE) == []
    assert set(DEV["states"]) == set(STAGE_KEYS)   # the editor writes all eight


def test_rejects_the_live_bug_state_not_on_this_type():
    """The defect this whole feature exists for: on_merge_state='Ready to Deploy' with
    Requirement in scope. ADO rejects the transition, so the config must be refused."""
    errors = validate_flows(
        [{"name": "Req", "types": ["Requirement"], "states": {"on_merge": "Ready to Deploy"}}],
        STATES_BY_TYPE,
    )
    assert len(errors) == 1
    assert "Ready to Deploy" in errors[0] and "Requirement" in errors[0]
    # It must point at the fix: the state lives on Bug/Task, it isn't a typo.
    assert "Bug" in errors[0] and "Task" in errors[0]
    assert "Implement Done" in errors[0]          # and list what IS valid here


def test_rejects_a_state_only_some_types_in_the_group_have():
    """Grouping Bug with Feature makes 'Ready to Deploy' work for half the group — the
    kind of half-broken rule that is hardest to notice from a log."""
    errors = validate_flows(
        [{"name": "Mixed", "types": ["Bug", "Feature"], "states": {"on_merge": "Ready to Deploy"}}],
        STATES_BY_TYPE,
    )
    assert len(errors) == 1
    assert "exists on Bug but not the rest" in errors[0]


def test_rejects_the_live_rollup_typo_with_a_suggestion():
    """'Ready for Testing' (configured) vs 'Ready to Testing' (real) silently disabled
    parent roll-up entirely — one word, months of nothing happening."""
    errors = validate_flows(
        [{"name": "Req", "types": ["Requirement"],
          "rollup": ["Ready for Testing = Implement Done"]}],
        STATES_BY_TYPE,
    )
    assert len(errors) == 1
    assert "exists on no work-item type" in errors[0]
    assert 'did you mean "Ready to Testing"?' in errors[0]


def test_rejects_a_rollup_target_the_parent_type_lacks():
    errors = validate_flows(
        [{"name": "Req", "types": ["Requirement"], "rollup": ["Active = Ready to Deploy"]}],
        STATES_BY_TYPE,
    )
    assert len(errors) == 1 and "target state" in errors[0]


def test_rejects_a_type_claimed_by_two_flows():
    errors = validate_flows(
        [{"name": "A", "types": ["Bug", "Task"]}, {"name": "B", "types": ["Task"]}],
        STATES_BY_TYPE,
    )
    assert len(errors) == 1
    assert "in two flows" in errors[0] and "Task" in errors[0]


def test_rejects_unknown_types_and_unknown_stages():
    errors = validate_flows(
        [{"name": "X", "types": ["Requirment"], "states": {"on_merged": "Active"}}],
        STATES_BY_TYPE,
    )
    assert any('did you mean "Requirement"?' in e for e in errors)
    assert any("unknown stage" in e for e in errors)


def test_rejects_structural_nonsense():
    errors = validate_flows(["not a dict", {"name": "empty", "types": []}], STATES_BY_TYPE)
    assert len(errors) == 2
    assert "not a mapping" in errors[0]
    assert "at least one work-item type" in errors[1]


def test_without_type_information_only_structure_is_checked():
    """When ADO is unreachable the editor must still be usable — refusing to save
    because the network is down would be worse than saving an unverified value."""
    assert validate_flows([{"name": "Z", "types": ["Anything"],
                            "states": {"on_merge": "Whatever"}}], {}) == []
    assert validate_flows([{"name": "Z", "types": []}], {}) != []


def test_blank_stage_states_are_always_allowed():
    """"Skip this stage" is a legitimate choice for every group."""
    flow = {"name": "Dev", "types": ["Bug"], "states": {key: "" for key in STAGE_KEYS}}
    assert validate_flows([flow], STATES_BY_TYPE) == []


# ── Form parsing (the Flow editor's POST) ─────────────────────────────────────

ALL_TYPES = ["Bug", "Feature", "Requirement", "Task"]


def test_parse_form_builds_a_group_with_all_eight_stages():
    """Every stage key is written, including the blank ones, so the page is the whole
    truth about what runs — no invisible inheritance from the flat fields."""
    parsed = parse_flow_form({
        "flow_count": "1",
        "flow0_name": "Dev items",
        "flow0_type__Bug": "on", "flow0_type__Task": "on",
        "flow0_state__on_merge": "Ready to Deploy",
        "flow0_state__review": "Ready to Review",
        "flow0_rollup": "Ready to Testing = Implement Done\n\nActive = Active\n",
    }, ALL_TYPES)
    assert len(parsed) == 1
    flow = parsed[0]
    assert flow["name"] == "Dev items"
    assert flow["types"] == ["Bug", "Task"]                    # in ALL_TYPES order
    assert set(flow["states"]) == set(STAGE_KEYS)
    assert flow["states"]["on_merge"] == "Ready to Deploy"
    assert flow["states"]["done"] == ""                        # untouched → explicit skip
    assert flow["rollup"] == ["Ready to Testing = Implement Done", "Active = Active"]
    assert "tags" not in flow and "comment" not in flow        # nothing overridden


def test_parse_form_drops_the_blank_slot_and_deleted_groups():
    """The page always renders one blank slot so a group can be added without a round
    trip; an untouched slot must vanish rather than become a validation error."""
    parsed = parse_flow_form({
        "flow_count": "3",
        "flow0_type__Bug": "on",
        "flow1_name": "Gone", "flow1_type__Feature": "on", "flow1_delete": "on",
        "flow2_name": "",                                      # the blank slot
    }, ALL_TYPES)
    assert [f["types"] for f in parsed] == [["Bug"]]


def test_parse_form_names_an_unnamed_group_after_its_types():
    parsed = parse_flow_form(
        {"flow_count": "1", "flow0_type__Bug": "on", "flow0_type__Task": "on"}, ALL_TYPES)
    assert parsed[0]["name"] == "Bug + Task"


def test_parse_form_records_only_the_actions_that_deviate():
    """Comments default to on, so only the switches turned OFF are stored — keeping the
    saved YAML about what was changed rather than restating every default."""
    parsed = parse_flow_form({
        "flow_count": "1", "flow0_type__Bug": "on",
        "flow0_tag__done": " shipped ",
        "flow0_comment__on_merge": "on",          # left on → not recorded
        "flow0_comment__on_deploy": "",           # unticked → recorded as False
    }, ALL_TYPES)
    assert parsed[0]["tags"] == {"done": "shipped"}
    assert parsed[0]["comment"] == {"on_deploy": False}


def test_parse_form_survives_a_missing_or_bogus_count():
    assert parse_flow_form({}, ALL_TYPES) == []
    assert parse_flow_form({"flow_count": "not a number"}, ALL_TYPES) == []
    assert parse_flow_form({"flow_count": "-3"}, ALL_TYPES) == []


def test_parse_form_output_is_directly_valid():
    """What the form produces must be what the validator accepts — otherwise a legitimate
    save is rejected by a shape mismatch rather than by a real problem."""
    parsed = parse_flow_form({
        "flow_count": "1", "flow0_name": "Dev", "flow0_type__Bug": "on",
        "flow0_state__on_merge": "Ready to Deploy",
    }, ALL_TYPES)
    assert validate_flows(parsed, STATES_BY_TYPE) == []
