"""Tests for the PURE link-graph grouping used by the Planning workbench."""

from __future__ import annotations

from ai_autopilot.models import TaskCategory, WorkItemInfo
from ai_autopilot.routing.planning_groups import group_by_links


def _wi(i: int) -> WorkItemInfo:
    return WorkItemInfo(id=i, title=f"#{i}", category=TaskCategory.BACKEND_TASK)


def test_serial_chain_ordered():
    g = group_by_links([_wi(1), _wi(2), _wi(3)], predecessors={2: {1}, 3: {2}})
    assert g["serial"] == [[1, 2, 3]]
    assert g["parallel"] == []


def test_related_conflict_cluster():
    g = group_by_links([_wi(1), _wi(2), _wi(3)], related={1: {2}, 2: {1}})
    assert g["conflicts"] == [[1, 2]]
    assert g["parallel"] == [3]


def test_parallel_when_no_links():
    g = group_by_links([_wi(1), _wi(2)])
    assert g["parallel"] == [1, 2] and g["serial"] == [] and g["conflicts"] == []


def test_edges_to_items_outside_selection_are_ignored():
    g = group_by_links([_wi(1)], predecessors={1: {99}}, related={1: {88}})
    assert g["parallel"] == [1] and g["serial"] == [] and g["conflicts"] == []


def test_cycle_falls_back_to_sorted():
    g = group_by_links([_wi(1), _wi(2)], predecessors={1: {2}, 2: {1}})
    assert g["serial"] == [[1, 2]]  # cycle → sorted, still one serial group
