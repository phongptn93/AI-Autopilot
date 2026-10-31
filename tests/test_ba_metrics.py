"""Tests for ai_autopilot.ba_metrics — the BA/PM numbers must be honest about missing data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from ai_autopilot.ba_metrics import (
    ConfirmationMetrics,
    DriftMetrics,
    SpecQuality,
    confirmation_metrics,
    drift_metrics,
    kpis,
    spec_quality,
)

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def _spec(rel="a.md", score=50, verdict="needs_work", ac=0, ac_verifiable=0, title="T"):
    return {"rel": rel, "title": title, "score": score, "verdict": verdict,
            "ac": ac, "ac_verifiable": ac_verifiable}


def _drift(days_ago=1, resolved_after=None, decision="", kind="code_diverged"):
    created = NOW - timedelta(days=days_ago)
    return {
        "created_at": created,
        "resolved_at": created + timedelta(days=resolved_after) if resolved_after is not None else None,
        "decision": decision,
        "kind": kind,
    }


# ── spec_quality ──────────────────────────────────────────────────────────────


def test_spec_quality_empty_is_none_not_zero():
    """An empty repo must show "—", not a 0 % that reads as 'all specs are bad'."""
    q = spec_quality([])
    assert q.count == 0
    assert q.avg_score is None and q.ready_pct is None and q.verifiable_pct is None
    assert q.worst == []


def test_spec_quality_average_and_ready_pct():
    """avg is the plain mean of scores; ready_pct is share with verdict == ready."""
    q = spec_quality([_spec(score=90, verdict="ready"), _spec(score=50), _spec(score=70)])
    assert q.count == 3
    assert q.avg_score == 70.0
    assert q.ready_pct == 33.3


def test_verifiable_pct_uses_sums_not_mean_of_ratios():
    """1/1 and 1/9 must give 2/10 = 20 %, not the mean of 100 % and 11 % (55 %)."""
    q = spec_quality([_spec(ac=1, ac_verifiable=1), _spec(ac=9, ac_verifiable=1)])
    assert q.verifiable_pct == 20.0


def test_verifiable_pct_none_when_no_ac_at_all():
    """No acceptance criteria anywhere is 'unknown', not 0 % verifiable."""
    assert spec_quality([_spec(), _spec()]).verifiable_pct is None


def test_verifiable_never_exceeds_ac():
    """Corrupt data with ac_verifiable > ac must not push the percentage over 100."""
    assert spec_quality([_spec(ac=2, ac_verifiable=5)]).verifiable_pct == 100.0


def test_worst_is_five_lowest_ascending():
    """The worst list is capped at 5 and ordered lowest score first."""
    q = spec_quality([_spec(rel=f"s{i}", score=s) for i, s in enumerate([9, 3, 7, 1, 5, 2, 8])])
    assert [w["score"] for w in q.worst] == [1, 2, 3, 5, 7]
    assert set(q.worst[0]) == {"rel", "title", "score", "verdict"}


def test_worst_ties_keep_input_order():
    """Equal scores must not shuffle between page loads."""
    q = spec_quality([_spec(rel="x", score=10), _spec(rel="y", score=10), _spec(rel="z", score=10)])
    assert [w["rel"] for w in q.worst] == ["x", "y", "z"]


def test_spec_quality_tolerates_missing_and_none_keys():
    """A row with missing/None/garbage cells counts as 0/'' and never raises."""
    q = spec_quality([{}, {"score": None, "ac": "x", "rel": None}, {"score": "80", "ac": "3"}])
    assert q.count == 3
    assert q.avg_score == round((0 + 0 + 80) / 3, 1)
    assert q.worst[0]["rel"] == ""


# ── drift_metrics ─────────────────────────────────────────────────────────────


def test_drift_empty_gives_none_rates():
    """No drift points: rates and median are None (a 0 % spec-fault rate would be a claim)."""
    d = drift_metrics([], sla_days=3, now=NOW)
    assert (d.total, d.open, d.overdue, d.decided) == (0, 0, 0, 0)
    assert d.median_days_to_decide is None
    assert d.spec_fault_rate is None and d.update_spec_rate is None
    assert d.by_kind == {}


def test_overdue_boundary_is_inclusive_of_sla_days():
    """Exactly sla_days old is overdue; one day less is not."""
    d = drift_metrics([_drift(days_ago=3), _drift(days_ago=2)], sla_days=3, now=NOW)
    assert d.open == 2 and d.overdue == 1


def test_sla_zero_or_negative_disables_overdue():
    """sla_days <= 0 means 'no SLA configured' — nothing is overdue."""
    rows = [_drift(days_ago=100)]
    assert drift_metrics(rows, sla_days=0, now=NOW).overdue == 0
    assert drift_metrics(rows, sla_days=-1, now=NOW).overdue == 0


def test_resolved_rows_are_never_overdue():
    """A decided point stays out of overdue however old it is."""
    d = drift_metrics([_drift(days_ago=50, resolved_after=1)], sla_days=3, now=NOW)
    assert d.overdue == 0 and d.open == 0 and d.decided == 1


def test_naive_and_aware_datetimes_mix_without_error():
    """Naive datetimes are UTC; mixing them with aware ones must not raise TypeError."""
    naive = (NOW - timedelta(days=4)).replace(tzinfo=None)
    plus7 = (NOW - timedelta(days=4)).astimezone(timezone(timedelta(hours=7)))
    rows = [
        {"created_at": naive, "resolved_at": None, "decision": "", "kind": "x"},
        {"created_at": plus7, "resolved_at": None, "decision": "", "kind": "x"},
    ]
    assert drift_metrics(rows, sla_days=3, now=NOW.replace(tzinfo=None)).overdue == 2


def test_median_days_odd_and_even():
    """Median over decided rows only: odd picks the middle, even averages the two (1 decimal)."""
    odd = [_drift(resolved_after=x) for x in (1, 5, 2)] + [_drift()]
    assert drift_metrics(odd, sla_days=3, now=NOW).median_days_to_decide == 2.0
    even = [_drift(days_ago=10, resolved_after=x) for x in (1, 2, 4, 5)]
    assert drift_metrics(even, sla_days=3, now=NOW).median_days_to_decide == 3.0
    frac = [_drift(days_ago=10, resolved_after=0.25)]
    assert drift_metrics(frac, sla_days=3, now=NOW).median_days_to_decide == 0.2


def test_by_kind_sorted_by_count_then_name():
    """Ties break alphabetically so the list is stable; counts include open rows."""
    rows = [_drift(kind=k) for k in ("b", "a", "c", "c", "b", "d")]
    d = drift_metrics(rows, sla_days=3, now=NOW)
    assert list(d.by_kind.items()) == [("b", 2), ("c", 2), ("a", 1), ("d", 1)]


def test_spec_fault_rate_counts_only_spec_kinds():
    """spec_unclear / spec_gap / assumption are the spec's fault; code drift is not."""
    rows = [_drift(kind="spec_unclear"), _drift(kind="spec_gap"), _drift(kind="assumption"),
            _drift(kind="code_diverged")]
    assert drift_metrics(rows, sla_days=3, now=NOW).spec_fault_rate == 0.75


def test_update_spec_rate_only_over_decided_rows():
    """Undecided rows must not dilute the share of decisions that updated the spec."""
    rows = [_drift(resolved_after=1, decision="update_spec"),
            _drift(resolved_after=1, decision="fix_code"),
            _drift(), _drift(), _drift()]
    d = drift_metrics(rows, sla_days=3, now=NOW)
    assert d.decided == 2 and d.update_spec_rate == 0.5


def test_update_spec_rate_none_when_nothing_decided():
    """Zero decided rows -> None, not 0 %."""
    assert drift_metrics([_drift()], sla_days=3, now=NOW).update_spec_rate is None


def test_bad_drift_rows_are_skipped():
    """Rows without a usable created_at are dropped from every count instead of raising."""
    rows = [{}, {"created_at": None}, {"created_at": "2026-01-01"}, _drift()]
    d = drift_metrics(rows, sla_days=3, now=NOW)
    assert d.total == 1 and d.open == 1


# ── confirmation_metrics ──────────────────────────────────────────────────────


def test_confirmations_empty_stale_rate_none():
    """No spec confirmations -> stale_rate None."""
    c = confirmation_metrics([])
    assert c.total == 0 and c.stale_rate is None


def test_stale_rate_is_over_spec_confirmations_only():
    """Acceptances are not spec confirmations and must not dilute the stale rate."""
    rows = [{"kind": "spec_confirm", "stale": True}, {"kind": "spec_confirm", "stale": False},
            {"kind": "spec_confirm"}, {"kind": "spec_confirm", "stale": True},
            {"kind": "acceptance", "stale": True}, {"kind": "acceptance"}]
    c = confirmation_metrics(rows)
    assert (c.total, c.spec_confirmations, c.acceptances, c.stale) == (6, 4, 2, 2)
    assert c.stale_rate == 0.5


def test_unknown_confirmation_kind_ignored():
    """An unknown kind is not counted anywhere."""
    assert confirmation_metrics([{"kind": "other", "stale": True}]).total == 0


# ── kpis ──────────────────────────────────────────────────────────────────────


def test_kpis_without_data_show_dash_and_no_tone():
    """No data: value is the dash and the tile has no colour (nothing to be green about)."""
    tiles = kpis(SpecQuality(), DriftMetrics(), ConfirmationMetrics())
    assert len(tiles) == 7
    for t in tiles:
        assert t.value == "—" and t.tone == "" and not t.has_data


def test_every_kpi_has_label_and_hint():
    """The hint is the only place the method is stated; no tile may ship without it."""
    for tiles in (kpis(SpecQuality(), DriftMetrics(), ConfirmationMetrics()),
                  kpis(spec_quality([_spec(score=90, verdict="ready", ac=2, ac_verifiable=2)]),
                       drift_metrics([_drift(resolved_after=1, decision="update_spec")],
                                     sla_days=3, now=NOW),
                       confirmation_metrics([{"kind": "spec_confirm", "stale": False}]))):
        for t in tiles:
            assert t.label and t.hint and t.icon and t.key
        assert len({t.key for t in tiles}) == len(tiles)


def test_kpi_tones_good_with_healthy_data():
    """Healthy numbers go green."""
    q = spec_quality([_spec(score=90, verdict="ready", ac=10, ac_verifiable=10)])
    d = drift_metrics([_drift(resolved_after=1, decision="fix_code")], sla_days=3, now=NOW)
    c = confirmation_metrics([{"kind": "spec_confirm", "stale": False}])
    tones = {t.key: t.tone for t in kpis(q, d, c)}
    assert set(tones.values()) == {"good"}


def test_kpi_tones_bad_with_poor_data():
    """Low score, overdue drift and many stale confirmations go red."""
    q = spec_quality([_spec(score=20, ac=4, ac_verifiable=1)])
    d = drift_metrics([_drift(days_ago=9, kind="spec_gap"), _drift(days_ago=9, kind="spec_gap")],
                      sla_days=3, now=NOW)
    c = confirmation_metrics([{"kind": "spec_confirm", "stale": True},
                              {"kind": "spec_confirm", "stale": False}])
    by = {t.key: t for t in kpis(q, d, c)}
    assert by["spec_score"].tone == "bad"
    assert by["drift_open"].tone == "bad" and by["drift_open"].value == "2"
    assert by["spec_fault"].tone == "bad" and by["spec_fault"].value == "100%"
    assert by["stale_confirm"].tone == "bad" and by["stale_confirm"].value == "50%"
    assert by["drift_days"].value == "—"


def test_score_thresholds():
    """avg >= 80 good, >= 60 warn, else bad."""
    def tone(score):
        return {t.key: t.tone for t in kpis(spec_quality([_spec(score=score)]), DriftMetrics(),
                                            ConfirmationMetrics())}["spec_score"]
    assert (tone(80), tone(79), tone(60), tone(59)) == ("good", "warn", "warn", "bad")


def test_stale_rate_exactly_quarter_is_not_bad():
    """The bad threshold is strictly greater than 25 %."""
    c = confirmation_metrics([{"kind": "spec_confirm", "stale": True}]
                             + [{"kind": "spec_confirm", "stale": False}] * 3)
    t = {k.key: k for k in kpis(SpecQuality(), DriftMetrics(), c)}["stale_confirm"]
    assert t.tone == "warn" and t.value == "25%"
