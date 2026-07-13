"""Tests for the PURE conflict-AI helpers (heuristic pre-filter + verdict parse)."""

from __future__ import annotations

from ai_autopilot.models import TaskCategory, WorkItemInfo
from ai_autopilot.routing.conflict_ai import (
    build_judge_prompt,
    parse_verdict,
    suspicious_pairs,
)


def _wi(i: int, title: str) -> WorkItemInfo:
    return WorkItemInfo(id=i, title=title, category=TaskCategory.BACKEND_TASK)


def test_suspicious_only_when_keyword_shared():
    a = _wi(1, "SparePart create API")
    b = _wi(2, "SparePart inventory")
    c = _wi(3, "Invoice export")
    pairs = suspicious_pairs([a, b, c])
    keys = [(p[0], p[1]) for p in pairs]
    assert (1, 2) in keys                       # share 'sparepart'
    assert all(3 not in k for k in keys)        # invoice shares nothing


def test_suspicious_respects_cap():
    items = [_wi(i, "SparePart module") for i in range(1, 6)]
    assert len(suspicious_pairs(items, max_pairs=3)) == 3


def test_stopwords_and_short_tokens_dropped():
    a = _wi(1, "update the form page")
    b = _wi(2, "update the form page")
    assert suspicious_pairs([a, b]) == []       # no salient shared keyword


def test_extra_stopwords_suppress_a_pair():
    a = _wi(1, "SparePart module")
    b = _wi(2, "SparePart module")
    assert (1, 2) in [(p[0], p[1]) for p in suspicious_pairs([a, b])]
    # 'sparepart' + 'module' declared noise → nothing salient left → no pair.
    assert suspicious_pairs([a, b], extra_stopwords=["sparepart", "module"]) == []


def test_min_token_len_controls_matching():
    a = _wi(1, "ERP job")
    b = _wi(2, "ERP job")
    assert suspicious_pairs([a, b]) == []                    # 'erp'/'job' are 3 chars, dropped at min_len=4
    assert (1, 2) in [(p[0], p[1]) for p in suspicious_pairs([a, b], min_token_len=3)]


def test_parse_verdict_good_bad_and_clamp():
    assert parse_verdict('noise {"score": 80, "modules": ["X"], "reason": "r"} tail')["score"] == 80
    assert parse_verdict("no json here") is None
    assert parse_verdict('{"score": 150}')["score"] == 100      # clamped
    assert parse_verdict('{"score": "high"}') is None           # non-numeric


def test_build_prompt_has_ids_and_json_ask():
    p = build_judge_prompt(_wi(1, "A"), _wi(2, "B"), ["x"])
    assert "#1" in p and "#2" in p and "JSON" in p
