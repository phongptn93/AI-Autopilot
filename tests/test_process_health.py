"""Tests for the process-health metrics (pure — no ADO connection needed)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ai_autopilot.process_health import (
    HealthItem,
    ad_hoc_ratio,
    build_report,
    escaped_defects,
    missing_area_path,
    render_text,
    routing_tags_left,
    stale_blocked,
    tag_case_drift,
    untagged,
)
from ai_autopilot.services.process_health_service import _as_bool, to_health_item

NOW = datetime(2026, 8, 20, tzinfo=UTC)


def _item(ident=1, **kw):
    return HealthItem(id=ident, **kw)


def test_ad_hoc_ratio_counts_only_classified_tickets():
    """An untagged item says nothing about planning. Counting it as planned would let
    the ratio improve simply because people stopped tagging."""
    items = [
        _item(1, tags=("Support",)),
        _item(2, tags=("Ops",)),
        _item(3, tags=("New Request",)),
        _item(4, tags=("Change Request",)),
        _item(5, tags=("Go-Live",)),      # routing/context tag only → not counted
        _item(6),                          # no tags at all → not counted
    ]
    ratio = ad_hoc_ratio(items)
    assert (ratio.ad_hoc, ratio.planned, ratio.total) == (2, 2, 4)
    assert ratio.percent == 50.0
    assert ratio.by_tag == {"Support": 1, "Ops": 1}


def test_ad_hoc_threshold_ignores_tiny_samples():
    """One ad-hoc ticket in a quiet week is 100% and means nothing — alerting on it
    trains people to ignore the alert."""
    tiny = ad_hoc_ratio([_item(1, tags=("Support",))])
    assert tiny.percent == 100.0 and tiny.over(30.0) is False
    busy = ad_hoc_ratio([_item(i, tags=("Support",)) for i in range(4)]
                        + [_item(9, tags=("New Request",))])
    assert busy.over(30.0) is True


def test_escaped_defects_counts_both_shapes_per_module():
    """A defect found after acceptance is raised as a Requirement tagged BUG, not as a
    Bug — so counting only Bug rows undercounts exactly the escapes that matter."""
    items = [
        _item(1, work_item_type="Bug", area_path="DxFactory\\DxMPM",
              found_in_environment="Production"),
        _item(2, work_item_type="Bug", area_path="DxFactory\\DxMPM",
              found_in_environment="UAT"),
        _item(3, work_item_type="Bug", area_path="DxFactory\\Core",
              found_in_environment="Dev"),          # caught in-house → not escaped
        _item(4, work_item_type="Requirement", area_path="DxFactory\\Core", tags=("BUG",)),
        _item(5, work_item_type="Requirement", area_path="DxFactory\\Core",
              tags=("New Request",)),
    ]
    assert escaped_defects(items) == {"DxMPM": 2, "Core": 1}


def test_stale_blocked_uses_last_change():
    items = [
        _item(1, blocked=True, changed=NOW - timedelta(days=5)),
        _item(2, blocked=True, changed=NOW - timedelta(hours=6)),   # still being chased
        _item(3, blocked=False, changed=NOW - timedelta(days=9)),
        _item(4, blocked=None, changed=NOW - timedelta(days=9)),    # no Blocked field
    ]
    assert [i.id for i in stale_blocked(items, days=3, now=NOW)] == [1]


def test_routing_tags_left_on_finished_items():
    done = ["Closed", "In UAT"]
    items = [
        _item(1, state="Closed", tags=("BUG", "PM - Need Review")),
        _item(2, state="Closed", tags=("BUG",)),
        _item(3, state="Active", tags=("Product - Review",)),  # still open → legitimate
    ]
    left = routing_tags_left(items, done)
    assert [(i.id, tags) for i, tags in left] == [(1, ["PM - Need Review"])]


def test_tag_case_drift_groups_variants():
    items = [_item(1, tags=("BUG",)), _item(2, tags=("Bug",)), _item(3, tags=("bug", "Ops"))]
    assert tag_case_drift(items) == {"bug": ["BUG", "Bug", "bug"]}


def test_missing_area_path_flags_project_root():
    items = [
        _item(1, area_path="DxFactory"),            # never moved off the root
        _item(2, area_path="DxFactory\\DxAI"),
        _item(3, area_path=""),
    ]
    assert [i.id for i in missing_area_path(items, "DxFactory")] == [1, 3]


def test_untagged_covers_tickets_only():
    """The classification tag decides handling and billing — a property of the ticket,
    not of the Tasks it was split into. Flagging children produced hundreds of
    "violations" the process never asked for."""
    items = [
        _item(1, work_item_type="Requirement", tags=("Go-Live",)),   # routing tag only
        _item(2, work_item_type="Requirement", tags=("Support",)),
        _item(3, work_item_type="Task"),                              # child → not a ticket
        _item(4, work_item_type="Bug"),
    ]
    assert [i.id for i in untagged(items)] == [1]


def test_report_with_nothing_wrong_reports_no_findings():
    """A clean report still gets computed — it is the evidence the process holds — but
    must not notify, or the digest becomes noise people mute."""
    items = [
        _item(i, work_item_type="Requirement", state="Active", tags=("New Request",),
              area_path="DxFactory\\Core", changed=NOW)
        for i in range(1, 6)
    ]
    report = build_report(items, project="DxFactory", done_states=["Closed"], now=NOW)
    assert report.has_findings is False
    assert "Không có gì bất thường" in render_text(report)


def test_report_surfaces_each_finding_in_the_text():
    items = [
        _item(1, work_item_type="Bug", state="Closed", area_path="DxFactory\\DxMPM",
              tags=("BUG", "PM - Need Review"), found_in_environment="Production",
              changed=NOW),
        _item(2, work_item_type="Requirement", state="Active", area_path="DxFactory",
              tags=("Support",), blocked=True, changed=NOW - timedelta(days=8)),
        _item(3, work_item_type="Task", state="Active", area_path="DxFactory\\Core",
              tags=("bug",), changed=NOW),
        _item(4, work_item_type="Requirement", state="Active", area_path="DxFactory\\Core",
              changed=NOW),                      # a ticket with no classification tag
    ]
    report = build_report(
        items, project="DxFactory", done_states=["Closed"], blocked_days=3, now=NOW
    )
    text = render_text(report)
    assert report.has_findings
    assert report.escaped == {"DxMPM": 1}
    assert [i.id for i in report.blocked] == [2]
    assert [i.id for i, _ in report.routing_left] == [1]
    assert "bug" in report.tag_drift            # BUG vs bug
    assert [i.id for i in report.no_module] == [2]
    assert [i.id for i in report.untagged] == [4]
    for expect in ("Lỗi lọt qua QC", "Bị chặn quá lâu", "tag định tuyến", "hoa/thường"):
        assert expect in text


def test_blocked_field_absent_is_not_the_same_as_not_blocked():
    """ADO omits a field the process never defined. Reading that as False would report
    a confident zero for a metric the project cannot actually measure."""
    assert _as_bool(None) is None
    assert _as_bool("") is None
    assert _as_bool("Yes") is True
    assert _as_bool("No") is False
    assert _as_bool(True) is True


def test_to_health_item_maps_ado_fields():
    item = to_health_item({
        "System.Id": 42,
        "System.WorkItemType": "Bug",
        "System.State": "Closed",
        "System.Title": "Sai tồn kho",
        "System.AreaPath": "DxFactory\\DxMPM",
        "System.Tags": "BUG; WA ; Go-Live",
        "System.ChangedDate": "2026-08-19T10:00:00Z",
        "Microsoft.VSTS.CMMI.Blocked": "Yes",
        "Microsoft.VSTS.CMMI.FoundInEnvironment": "Production",
        "System.AssignedTo": {"displayName": "Phong"},
    })
    assert item is not None
    assert item.id == 42 and item.module == "DxMPM" and item.blocked is True
    assert item.tags == ("BUG", "WA", "Go-Live")
    assert item.has_tag("wa") and item.assigned_to == "Phong"
    assert item.changed == datetime(2026, 8, 19, 10, tzinfo=UTC)
    assert to_health_item({"System.Title": "no id"}) is None


def test_project_not_using_area_path_is_not_700_violations():
    """When everything sits at the project root, the project simply does not split by
    module. Listing every item then buries the findings that ARE actionable."""
    items = [_item(i, work_item_type="Requirement", area_path="TLCL-DxFac", tags=("BUG",))
             for i in range(1, 11)]
    report = build_report(items, project="TLCL-DxFac", done_states=["Closed"], now=NOW)
    assert report.no_module == [] and report.area_path_unused is True
    assert "chưa dùng Area Path" in render_text(report)

    # A project that DOES use modules still gets the stragglers named.
    mixed = [_item(i, work_item_type="Requirement", area_path="DxFactory\Core",
                   tags=("BUG",)) for i in range(1, 10)]
    mixed.append(_item(99, work_item_type="Requirement", area_path="DxFactory", tags=("BUG",)))
    report = build_report(mixed, project="DxFactory", done_states=["Closed"], now=NOW)
    assert [i.id for i in report.no_module] == [99] and report.area_path_unused is False
