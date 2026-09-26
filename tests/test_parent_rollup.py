"""Parent roll-up: the parent follows its least-advanced child.

Two questions exposed this, and neither had a defensible answer before:

  * "one child is Active — is the parent Active too?"
  * "what happens when every child is Closed?"

The rule itself was sound. What ranked the children was not: it was the ORDER THE MAP
LINES HAPPENED TO BE WRITTEN IN, and the editor wrote them alphabetically. So the
alphabet was the workflow. On a real board that made "Deferred" — last letter-wise —
the most advanced state a child could reach, so a child parked there never held its
parent back, and "Awaiting Clarification" outranked "Approved". Nobody decided that;
`sorted()` did.

The ordering now comes from the ADO state CATEGORY, which is the process template's own
answer to "how far along is this".
"""

from __future__ import annotations

from ai_autopilot.models import WorkItemInfo
from ai_autopilot.services.state_sync import (
    parent_rollup_target,
    parse_rollup_map,
    unmapped_child_states,
)

# A real project's states, in the order the editor used to render them: alphabetical.
# Keeping that order here is the point — the ranking must no longer come from it.
MAP = parse_rollup_map([
    "Active = Active",
    "Approved = Active",
    "Awaiting Clarification = Active",
    "Closed = Closed",
    "Deferred = Active",
])

CATEGORIES = {
    "Active": "InProgress",
    "Approved": "Proposed",
    "Awaiting Clarification": "Proposed",
    "Closed": "Completed",
    "Deferred": "Removed",
}


def kids(*states: str) -> list[WorkItemInfo]:
    return [
        WorkItemInfo(id=i, title=f"child {i}", work_item_type="Task", state=s, tags=[])
        for i, s in enumerate(states, start=1)
    ]


# ── The two questions ────────────────────────────────────────────────────────

def test_one_child_still_working_keeps_the_parent_there():
    """"1 children active thì thằng cha cũng active sao" — yes, and that is the rule:
    the parent reflects the slowest child, not a state they all share."""
    target = parent_rollup_target(kids("Active", "Closed"), MAP, CATEGORIES)
    assert target == "Active"


def test_every_child_closed_closes_the_parent():
    """"tất cả close thì sao" — the parent reaches the last state only when every child
    has."""
    target = parent_rollup_target(kids("Closed", "Closed", "Closed"), MAP, CATEGORIES)
    assert target == "Closed"


# ── The alphabet is no longer the workflow ───────────────────────────────────

def test_a_deferred_child_does_not_hold_the_parent_open():
    """Deferred is Removed-category: cancelled work is not slow work. Ranking it low
    would let one abandoned child pin the parent open forever."""
    assert parent_rollup_target(kids("Deferred", "Closed"), MAP, CATEGORIES) == "Closed"


def test_a_deferred_child_is_not_counted_as_finished_either():
    """The other half of the same mistake. Under the old alphabetical ranking Deferred
    sorted LAST, so it counted as the most advanced state there was."""
    assert parent_rollup_target(kids("Deferred", "Active"), MAP, CATEGORIES) == "Active"


def test_proposed_outranks_in_progress_regardless_of_spelling():
    """"Awaiting Clarification" is Proposed and "Active" is InProgress, so Awaiting is
    the slower of the two — the opposite of what the alphabet said."""
    assert parent_rollup_target(
        kids("Awaiting Clarification", "Active"), MAP, CATEGORIES) == "Active"
    # Both Proposed: the author's own line order breaks the tie, Approved first.
    assert parent_rollup_target(
        kids("Approved", "Awaiting Clarification"), MAP, CATEGORIES) == "Active"


def test_all_children_cancelled_leaves_the_parent_alone():
    """Not the same as "all done". Closing a parent because its only children were
    cancelled reports work finished that nobody did."""
    assert parent_rollup_target(kids("Deferred", "Deferred"), MAP, CATEGORIES) is None


# ── The holds that were already right, kept right ────────────────────────────

def test_an_unmapped_child_state_still_stops_everything():
    """A child whose stage is unknown could be the slowest one; advancing past it would
    be a guess."""
    assert parent_rollup_target(kids("Active", "Resolved"), MAP, CATEGORIES) is None


def test_the_held_roll_up_names_the_state_that_is_missing():
    assert unmapped_child_states(kids("Active", "Resolved"), MAP, CATEGORIES) == ["Resolved"]


def test_a_skipped_child_is_not_reported_as_an_unmapped_state():
    """Removed-category children are deliberately ignored, so naming them would send
    somebody off to map a state the engine will never read."""
    no_deferred = parse_rollup_map(["Active = Active", "Closed = Closed"])
    assert unmapped_child_states(kids("Active", "Deferred"), no_deferred, CATEGORIES) == []
    assert parent_rollup_target(kids("Active", "Deferred"), no_deferred, CATEGORIES) == "Active"


def test_no_children_and_no_map_do_nothing():
    assert parent_rollup_target([], MAP, CATEGORIES) is None
    assert parent_rollup_target(kids("Active"), [], CATEGORIES) is None


# ── Degrading, not changing, when ADO cannot be reached ──────────────────────

def test_without_categories_it_falls_back_to_the_written_order():
    """An unreachable ADO must degrade to the previous behaviour rather than invent a
    third answer. Line order then ranks, exactly as it used to."""
    assert parent_rollup_target(kids("Active", "Closed"), MAP) == "Active"
    assert parent_rollup_target(kids("Closed", "Closed"), MAP) == "Closed"
    # …including the old wrong answer: with no categories, Deferred is still last.
    assert parent_rollup_target(kids("Deferred", "Closed"), MAP) == "Closed"


def test_an_unknown_category_is_treated_as_early_not_finished():
    """Guessing "finished" is the guess that closes a parent too soon."""
    partial = {"Active": "InProgress", "Closed": "Completed"}   # Approved unclassified
    assert parent_rollup_target(kids("Approved", "Closed"), MAP, partial) == "Active"
