"""What the BA pages need to know about one spec: ready? confirmed? changed since?

The pure modules (``spec_readiness``, ``spec_impact``, ``assumptions``, ``customer_pack``)
each answer one question about a document's TEXT. A page has to ask all of them about
one spec at once, and add what only the database knows: did the customer confirm it, and
is that confirmation still about the text on disk. That glue lives here so the BA
workbench and the spec page cannot drift apart in how they answer.

Kept out of the route modules because the spec page (``requirements``) and the workbench
(``ba``) both need it, and routes importing each other is how circular imports start.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_autopilot import assumptions, customer_pack, spec_impact, spec_library, spec_readiness

NONE, CONFIRMED, REJECTED, STALE = "none", "confirmed", "rejected", "stale"

#: Results the BA can record per criterion, and the label shown for each.
RESULTS = {"pass": "Đạt", "fail": "Không đạt", "blocked": "Bị chặn", "": "Chưa kiểm"}


def item_url(cfg: Any, item_id: int) -> str:
    org = (cfg.ado_organization or "").rstrip("/")
    return f"{org}/_workitems/edit/{item_id}" if org and item_id else ""


def load_spec(cfg: Any, rel: str) -> tuple[Path | None, str, str]:
    """``(path, plain text, kind)`` of a spec, or ``(None, "", "")`` when ``rel`` is not a
    readable spec inside the workspace's spec folders (the library enforces containment)."""
    target = spec_library.resolve(cfg.workspace_directory, rel)
    if target is None:
        return None, "", ""
    raw = spec_library.read(target)
    kind = target.suffix.lower().lstrip(".")
    text = spec_readiness.plain_text_from_html(raw) if kind == "html" else raw
    return target, text, kind


def numbered(acs: list[dict[str, str]]) -> list[dict[str, str]]:
    """Give every criterion an id: the spec's own when it names one, its position if not."""
    return [{"id": a.get("id") or f"AC-{n:02d}", "text": a.get("text", "")}
            for n, a in enumerate(acs, start=1)]


def parse_acs(raw_json: str) -> list[dict[str, str]]:
    try:
        data = json.loads(raw_json or "[]")
    except ValueError:
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


async def confirmation_state(c: Any, rel: str, snap: list[dict[str, str]],
                             text_hash: str) -> dict[str, Any]:
    """Where the customer's confirmation of this spec stands against the text on disk.

    ``stale`` means the customer confirmed an earlier version AND an acceptance criterion
    has since changed, been added or been dropped — the case where a "confirmed" badge
    would be a lie. Prose edits outside the criteria leave it ``confirmed`` (the row still
    notes ``text_changed``): the customer approved scenarios, and they are unchanged.
    """
    row = await c.customer_confirm_repo.latest(rel, "spec_confirm")
    if row is None:
        return {"state": NONE, "row": None, "impact": None, "text_changed": False}
    old = parse_acs(row.ac_json)
    impact = spec_impact.diff(old, snap)
    state = STALE if impact.has_changes else CONFIRMED
    if row.outcome == "rejected":
        state = REJECTED
    return {"state": state, "row": row, "impact": impact,
            "text_changed": bool(row.spec_hash) and row.spec_hash != text_hash}


def drift_rows(drifts: list[Any]) -> list[dict[str, str]]:
    """Spec-drift rows as the acceptance record wants them. A point closed by the old
    item-wide button has no decision but is settled, so it reads as accepted."""
    out = []
    for d in drifts:
        decision = d.decision or ("accept" if d.resolved_at else "")
        out.append({"summary": d.summary or d.kind, "decision": decision})
    return out


async def spec_panel(c: Any, cfg: Any, rel: str, text: str, item_id: int) -> dict[str, Any]:
    """Everything the spec page's BA cards show, for one spec."""
    readiness = spec_readiness.check(text, ready_min=cfg.spec_readiness_min)
    snap = spec_impact.snapshot(text)
    h = spec_impact.hash_text(text)
    confirmation = await confirmation_state(c, rel, snap, h)
    qs = assumptions.extract(text)
    answers = await c.assumption_repo.answers_for(rel)
    ledger = assumptions.ledger(qs, answers)

    last = await c.customer_confirm_repo.latest(rel, "acceptance")
    prior = {a.get("id", ""): a for a in parse_acs(last.ac_json)} if last else {}
    acs = []
    for a in numbered(snap):
        old = prior.get(a["id"], {})
        acs.append({**a, "result": old.get("result", ""), "evidence": old.get("evidence", "")})

    drifts = await c.spec_drift_repo.for_item(item_id) if item_id else []
    drows = drift_rows(drifts)
    ready, reasons = customer_pack.acceptance_ready(acs, drows)
    return {
        "readiness": readiness, "ready_min": cfg.spec_readiness_min,
        "confirmation": confirmation, "ledger": ledger, "acs": acs,
        "acceptance": last, "accept_ready": ready, "accept_reasons": reasons,
        "drifts_open": sum(1 for d in drows if not d["decision"]),
        "results": RESULTS,
    }
