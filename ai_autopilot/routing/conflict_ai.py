"""AI conflict-analysis helpers for the Planning workbench (PURE parts).

The workbench's Analyze action can ask Claude whether two selected work items are
likely to touch the same code (a *hidden* conflict the BA never linked). To keep
that cheap, a heuristic pre-filter (:func:`suspicious_pairs`) selects only pairs
that share salient keywords, and only those pairs get an AI judge. The prompt
builder and the tolerant JSON parser live here (pure/testable); the impure Claude
call lives in ``services/planning_analyzer.py``.
"""

from __future__ import annotations

import json
import re
from itertools import combinations

from ai_autopilot.models import WorkItemInfo

# Common words that carry no signal about which code an item touches.
_STOP = {
    "this", "that", "with", "from", "have", "will", "should", "when", "then",
    "into", "your", "form", "page", "list", "data", "item", "items", "task",
    "support", "update", "create", "delete", "change", "value", "field", "table",
    "cho", "khi", "tren", "duoc", "theo", "phai", "trong", "khong", "them", "sua",
}
_MARKERS = ("[be]", "[fe]", "[db]", "[qc]", "[test]")


def _keywords(item: WorkItemInfo, min_len: int, stop: frozenset[str]) -> set[str]:
    text = f"{item.title} {item.description or ''}".lower()
    for m in _MARKERS:
        text = text.replace(m, " ")
    token = re.compile(rf"[a-z0-9]{{{max(1, min_len)},}}")
    return {t for t in token.findall(text) if t not in stop}


def suspicious_pairs(
    items: list[WorkItemInfo],
    *,
    max_pairs: int | None = 6,
    min_token_len: int = 4,
    extra_stopwords: list[str] | None = None,
) -> list[tuple[int, int, list[str]]]:
    """Pairs of items sharing ≥1 salient keyword — candidates for an AI judge.

    Returns ``(id_a, id_b, [shared keywords])`` ranked by overlap, capped at
    ``max_pairs`` (``None`` = uncapped). Zero shared keywords → no AI call → 0 tokens.

    ``min_token_len`` sets the shortest keyword considered; ``extra_stopwords`` adds
    project-specific noise words on top of the built-in list.
    """
    stop = _STOP | {w.strip().lower() for w in (extra_stopwords or []) if w.strip()}
    kw = {i.id: _keywords(i, min_token_len, frozenset(stop)) for i in items}
    pairs: list[tuple[int, int, list[str]]] = []
    for a, b in combinations(items, 2):
        shared = kw[a.id] & kw[b.id]
        if shared:
            pairs.append((a.id, b.id, sorted(shared)))
    pairs.sort(key=lambda p: len(p[2]), reverse=True)
    return pairs[:max_pairs] if max_pairs else pairs


def build_judge_prompt(a: WorkItemInfo, b: WorkItemInfo, shared: list[str]) -> str:
    """One-shot prompt asking Claude to rate whether two items conflict in code."""
    return (
        "Two work items may touch the same code and would then conflict if worked on "
        "at the same time on separate branches.\n"
        f"A #{a.id}: {a.title}\n"
        f"B #{b.id}: {b.title}\n"
        f"Shared keywords: {', '.join(shared)}\n\n"
        "Rate the likelihood they touch the same files/modules (0-100) and name the "
        'likely shared modules. Reply with ONLY JSON: '
        '{"score": <0-100>, "modules": ["..."], "reason": "<one short sentence>"}'
    )


def parse_verdict(text: str) -> dict | None:
    """Parse the judge's JSON reply tolerantly. Returns None if unusable."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    score = data.get("score")
    if not isinstance(score, (int, float)):
        return None
    return {
        "score": max(0, min(100, int(score))),
        "modules": [str(m) for m in (data.get("modules") or [])][:5],
        "reason": str(data.get("reason") or "")[:300],
    }
