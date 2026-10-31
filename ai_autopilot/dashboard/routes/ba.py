"""The BA workbench: which specs are ready, what is still open, what the customer said.

The Requirements page is where a BA writes and reads one spec. This is the view ACROSS
specs, for the four questions a BA asks before handing work on:

* **Specs** — which of them is precise enough for an agent to build from, and which has
  the customer actually confirmed (and has it changed since)?
* **Open questions** — every question and assumption the specs still carry, to answer
  here and have the answer land on the work item.
* **Production feedback** — support tickets that keep saying the same thing, grouped
  into one candidate gap instead of a dozen rows.
* **Metrics** — spec quality, how fast drift gets decided, how often a confirmation
  goes stale: the numbers that say whether to loosen or tighten the process.

The customer's word is RECORDED here, never collected: the BA writes down that the
customer confirmed (who, through what channel, when) after the customer said so. There
is deliberately no public, unauthenticated link for a customer to click — that would be
a new door into the dashboard to save a BA one sentence.
"""

from __future__ import annotations

import html as html_mod
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from ai_autopilot import (
    assumptions,
    ba_metrics,
    customer_pack,
    spec_impact,
    spec_library,
    spec_readiness,
    ticket_clusters,
)
from ai_autopilot.container import Container
from ai_autopilot.dashboard import ba_view
from ai_autopilot.dashboard.common import (
    _FLASH_COOKIE,
    _TEMPLATES,
    _flash,
    _log,
    _take_flash,
)
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.dashboard.routes.requirements import DRAFT_TAG
from ai_autopilot.services.process_health_service import to_health_item

TABS = ("specs", "questions", "production", "metrics")
_MAX_SPECS = 150
_CLOSED = frozenset({"closed", "done", "resolved", "removed", "completed"})
#: A customer document is opened by someone outside the dashboard (printed, saved as PDF),
#: so it gets the strictest policy a self-contained page can have: no script, no network.
_DOC_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                               "frame-ancestors 'self'",
    "X-Content-Type-Options": "nosniff",
}


def _actor(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return (getattr(user, "label", "") or "dashboard") if user else "dashboard"


def _back(path: str, anchor: str = "") -> str:
    return f"/dashboard/requirements/spec?path={quote(path)}{anchor}"


def _int(raw: object) -> int:
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        return 0


async def _comment(c: Container, item_id: int, markup: str) -> bool:
    """Post to the work item. False when nothing was posted (no item, or ``dry_run``)."""
    if item_id <= 0 or c.config.dry_run:
        return False
    await c.ado.add_comment(item_id, markup)
    return True


async def _spec_rows(c: Container) -> list[dict[str, Any]]:
    """One row per spec in the workspace: readiness and the customer's confirmation."""
    cfg = c.config
    rows: list[dict[str, Any]] = []
    for s in spec_library.discover(cfg.workspace_directory)[:_MAX_SPECS]:
        target, text, _kind = ba_view.load_spec(cfg, s.rel)
        if target is None:
            continue
        r = spec_readiness.check(text, ready_min=cfg.spec_readiness_min)
        snap = spec_impact.snapshot(text)
        conf = await ba_view.confirmation_state(c, s.rel, snap, spec_impact.hash_text(text))
        ledger = assumptions.ledger(
            assumptions.extract(text), await c.assumption_repo.answers_for(s.rel))
        rows.append({
            "rel": s.rel, "q": quote(s.rel), "title": s.title, "item_id": s.item_id,
            "item_url": ba_view.item_url(cfg, s.item_id), "readiness": r,
            "confirmation": conf, "open_questions": ledger.open_count,
            "modified": datetime.fromtimestamp(s.modified).strftime("%Y-%m-%d"),
        })
    order = {spec_readiness.NOT_READY: 0, spec_readiness.NEEDS_WORK: 1, spec_readiness.READY: 2}
    rows.sort(key=lambda x: (order[x["readiness"].verdict], x["readiness"].score))
    return rows


async def _questions(c: Container) -> list[dict[str, Any]]:
    """Open questions grouped by spec, worst-documented first."""
    cfg = c.config
    groups: list[dict[str, Any]] = []
    for s in spec_library.discover(cfg.workspace_directory)[:_MAX_SPECS]:
        _t, text, _k = ba_view.load_spec(cfg, s.rel)
        qs = assumptions.extract(text)
        if not qs:
            continue
        led = assumptions.ledger(qs, await c.assumption_repo.answers_for(s.rel))
        if led.open or led.answered:
            groups.append({"rel": s.rel, "q": quote(s.rel), "title": s.title,
                           "item_id": s.item_id, "ledger": led})
    groups.sort(key=lambda g: -g["ledger"].open_count)
    return groups


async def _production(c: Container) -> dict[str, Any]:
    """Production tickets grouped by what they complain about, per polled project."""
    cfg = c.config
    tags = {t.strip().lower() for t in cfg.production_feedback_tags if t.strip()}
    if not tags:
        return {"clusters": [], "error": "", "tags": []}
    now = datetime.now(UTC)
    clusters: list[dict[str, Any]] = []
    error = ""
    for project in cfg.effective_ado_projects:
        try:
            raws = await c.ado.get_raw_work_items(project, cfg.production_feedback_window_days)
        except Exception as exc:  # the tab must open even when the tracker is unreachable
            error = f"Không đọc được ticket của {project}: {exc}"
            continue
        tickets = []
        for fields in raws or []:
            hi = to_health_item(fields)
            if hi and any(t.lower() in tags for t in hi.tags):
                tickets.append(ticket_clusters.Ticket(
                    id=hi.id, title=hi.title, state=hi.state, tags=tuple(hi.tags),
                    created=hi.created, area_path=hi.area_path))
        for cl in ticket_clusters.cluster(
                tickets, min_size=max(2, cfg.production_feedback_min_cluster)):
            clusters.append({
                "project": project, "cluster": cl,
                "rising": ticket_clusters.rising(cl, now=now),
                "open": cl.open_count(_CLOSED),
                "tickets": [{"id": t.id, "title": t.title, "state": t.state,
                             "url": ba_view.item_url(cfg, t.id)} for t in cl.tickets[:10]],
                "more": max(0, cl.size - 10),
            })
    clusters.sort(key=lambda x: (not x["rising"], -x["cluster"].size))
    return {"clusters": clusters, "error": error, "tags": sorted(tags)}


async def _metrics(c: Container, spec_rows: list[dict[str, Any]]) -> dict[str, Any]:
    cfg = c.config
    now = datetime.now(UTC)
    quality = ba_metrics.spec_quality([{
        "rel": r["rel"], "title": r["title"], "score": r["readiness"].score,
        "verdict": r["readiness"].verdict, "ac": r["readiness"].stats.get("ac", 0),
        "ac_verifiable": r["readiness"].stats.get("ac_verifiable", 0),
    } for r in spec_rows])
    drifts = (await c.spec_drift_repo.open_drifts(limit=1000)
              + await c.spec_drift_repo.recent_resolved(limit=500))
    drift = ba_metrics.drift_metrics([{
        "created_at": d.created_at, "resolved_at": d.resolved_at,
        "decision": d.decision, "kind": d.kind,
    } for d in drifts], sla_days=cfg.spec_drift_sla_days, now=now)
    confirmations = [{"kind": "spec_confirm", "stale": r["confirmation"]["state"] == ba_view.STALE}
                     for r in spec_rows if r["confirmation"]["state"] != ba_view.NONE]
    confirmations += [{"kind": "acceptance", "stale": False}
                      for row in await c.customer_confirm_repo.recent(500)
                      if row.kind == "acceptance"]
    conf = ba_metrics.confirmation_metrics(confirmations)
    return {"kpis": ba_metrics.kpis(quality, drift, conf), "quality": quality, "drift": drift}


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    @router.get("/ba", response_class=HTMLResponse)
    async def ba_page(request: Request, tab: str = "specs"):
        c: Container = request.app.state.container
        tab = tab if tab in TABS else "specs"
        flash = _take_flash(request)
        data: dict[str, Any] = {"tab": tab, "tabs": TABS, "flash": flash,
                                "dry_run": c.config.dry_run}
        if tab in ("specs", "metrics"):
            data["specs"] = await _spec_rows(c)
        if tab == "questions":
            data["groups"] = await _questions(c)
        if tab == "production":
            data["production"] = await _production(c)
        if tab == "metrics":
            data["metrics"] = await _metrics(c, data["specs"])
        response = _TEMPLATES.TemplateResponse(request, "ba.html", _ctx(request, "ba", **data))
        if flash is not None:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        return response

    # ── the customer's word ────────────────────────────────────────────────────────

    @router.get("/ba/pack", response_class=HTMLResponse)
    async def customer_pack_page(request: Request, path: str):
        """The one-page, plain-language confirmation to hand the customer."""
        c: Container = request.app.state.container
        target, text, _kind = ba_view.load_spec(c.config, path)
        if target is None:
            return PlainTextResponse("Không mở được spec này.", status_code=404)
        spec = next((s for s in spec_library.discover(c.config.workspace_directory)
                     if s.rel == path), None)
        item_id = spec.item_id if spec else 0
        content = customer_pack.summarise_spec(
            text, title=spec_library.title_of(text[:6000], "md", target.stem))
        # No spec_path: the customer reads this, and a file path inside our workspace is
        # internal detail that means nothing to them and says how our repo is laid out.
        page = customer_pack.render_confirmation_html(
            content, item_id=item_id,
            generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
            version="v" + spec_impact.hash_text(text)[:8])
        return HTMLResponse(page, headers=_DOC_HEADERS)

    @router.post("/ba/confirm")
    async def record_confirmation(request: Request):
        """Write down that the customer confirmed (or refused) THIS version of a spec."""
        c: Container = request.app.state.container
        form = await request.form()
        path = str(form.get("path", ""))
        back = _back(path, "#ba-customer")
        target, text, _k = ba_view.load_spec(c.config, path)
        if target is None:
            return _flash(back, "ba_no_spec")
        who = str(form.get("confirmed_by", "")).strip()[:200]
        if not who:
            return _flash(back, "ba_no_name")
        outcome = "rejected" if str(form.get("outcome", "")) == "rejected" else "confirmed"
        channel = str(form.get("channel", "")).strip()[:100]
        note = str(form.get("note", "")).strip()[:4000]
        item_id = _int(form.get("item_id"))
        snap = ba_view.numbered(spec_impact.snapshot(text))
        await c.customer_confirm_repo.add(
            kind="spec_confirm", outcome=outcome, spec_path=path, work_item_id=item_id,
            spec_hash=spec_impact.hash_text(text), ac_json=json.dumps(snap, ensure_ascii=False),
            confirmed_by=who, channel=channel, note=note, recorded_by=_actor(request))
        esc = html_mod.escape
        icon, label = ("⛔", "CUSTOMER-REJECTED") if outcome == "rejected" else (
            "✅", "CUSTOMER-CONFIRMED")
        posted = await _comment(c, item_id, (
            f"<div><b>{icon} {label}</b> — {esc(who)}" + (f" ({esc(channel)})" if channel else "")
            + f"<br/>Spec: <code>{esc(path)}</code> · {len(snap)} tiêu chí"
            + (f"<p>{esc(note)}</p>" if note else "") + "</div>"))
        await c.audit_repo.record(
            actor=_actor(request), source="dashboard", action=f"ba.customer_{outcome}",
            target=f"#{item_id}" if item_id else path, detail=f"{who} · {channel}"[:300])
        if item_id and not posted:
            return _flash(back, "ba_saved_dry")
        return _flash(back, "ba_rejected" if outcome == "rejected" else "ba_confirmed")

    @router.post("/ba/accept")
    async def record_acceptance(request: Request):
        """Record the customer's acceptance of the DELIVERY, criterion by criterion."""
        c: Container = request.app.state.container
        form = await request.form()
        path = str(form.get("path", ""))
        back = _back(path, "#ba-accept")
        target, text, _k = ba_view.load_spec(c.config, path)
        if target is None:
            return _flash(back, "ba_no_spec")
        who = str(form.get("confirmed_by", "")).strip()[:200]
        if not who:
            return _flash(back, "ba_no_name")
        outcome = "rejected" if str(form.get("outcome", "")) == "rejected" else "confirmed"
        item_id = _int(form.get("item_id"))
        # The criteria come from the file, never from the form: a posted AC text could
        # claim the customer accepted something the spec does not say.
        acs = []
        for i, a in enumerate(ba_view.numbered(spec_impact.snapshot(text))):
            res = str(form.get(f"res_{i}", "")).strip()
            acs.append({**a, "result": res if res in ("pass", "fail", "blocked") else "",
                        "evidence": str(form.get(f"ev_{i}", "")).strip()[:500]})
        drifts = await c.spec_drift_repo.for_item(item_id) if item_id else []
        ready, _reasons = customer_pack.acceptance_ready(acs, ba_view.drift_rows(drifts))
        if outcome == "confirmed" and not ready:
            return _flash(back, "ba_accept_blocked")
        channel = str(form.get("channel", "")).strip()[:100]
        note = str(form.get("note", "")).strip()[:4000]
        await c.customer_confirm_repo.add(
            kind="acceptance", outcome=outcome, spec_path=path, work_item_id=item_id,
            spec_hash=spec_impact.hash_text(text), ac_json=json.dumps(acs, ensure_ascii=False),
            confirmed_by=who, channel=channel, note=note, recorded_by=_actor(request))
        esc = html_mod.escape
        passed = sum(1 for a in acs if a["result"] == "pass")
        icon, label = ("⛔", "ACCEPTANCE-REJECTED") if outcome == "rejected" else ("✅", "ACCEPTED")
        posted = await _comment(c, item_id, (
            f"<div><b>{icon} {label}</b> — {esc(who)}" + (f" ({esc(channel)})" if channel else "")
            + f"<br/>Spec: <code>{esc(path)}</code> · {passed}/{len(acs)} tiêu chí đạt"
            + (f"<p>{esc(note)}</p>" if note else "") + "</div>"))
        await c.audit_repo.record(
            actor=_actor(request), source="dashboard", action=f"ba.acceptance_{outcome}",
            target=f"#{item_id}" if item_id else path, detail=f"{who} · {passed}/{len(acs)}"[:300])
        if item_id and not posted:
            return _flash(back, "ba_saved_dry")
        return _flash(back, "ba_accept_rejected" if outcome == "rejected" else "ba_accepted")

    @router.get("/ba/acceptance", response_class=HTMLResponse)
    async def acceptance_record(request: Request, path: str, fmt: str = "html"):
        """The latest acceptance record of a spec, printable (or as Markdown)."""
        c: Container = request.app.state.container
        row = await c.customer_confirm_repo.latest(path, "acceptance")
        if row is None:
            return PlainTextResponse("Spec này chưa có biên bản nghiệm thu.", status_code=404)
        acs = ba_view.parse_acs(row.ac_json)
        drifts = await c.spec_drift_repo.for_item(row.work_item_id) if row.work_item_id else []
        title = path.rsplit("/", 1)[-1]
        kw: dict[str, Any] = {
            "drifts": ba_view.drift_rows(drifts), "item_id": row.work_item_id,
            "generated": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
        }
        if fmt == "md":
            return PlainTextResponse(
                customer_pack.render_acceptance_markdown(title, acs, **kw),
                media_type="text/markdown; charset=utf-8")
        return HTMLResponse(customer_pack.render_acceptance_html(title, acs, **kw),
                            headers=_DOC_HEADERS)

    # ── readiness, impact, questions ───────────────────────────────────────────────

    @router.post("/ba/readiness/comment")
    async def post_readiness(request: Request):
        """Put the readiness findings on the work item, where the author will see them."""
        c: Container = request.app.state.container
        form = await request.form()
        path = str(form.get("path", ""))
        back = _back(path, "#ba-ready")
        item_id = _int(form.get("item_id"))
        if item_id <= 0:
            return _flash(back, "ba_no_item")
        if c.config.dry_run:
            return _flash(back, "req_dry_run")
        target, text, _k = ba_view.load_spec(c.config, path)
        if target is None:
            return _flash(back, "ba_no_spec")
        r = spec_readiness.check(text, ready_min=c.config.spec_readiness_min)
        await c.ado.add_comment(item_id, spec_readiness.render_comment(r, path))
        await c.audit_repo.record(actor=_actor(request), source="dashboard",
                                  action="ba.readiness_posted", target=f"#{item_id}",
                                  detail=f"{r.verdict} {r.score}")
        return _flash(back, "ba_readiness_posted")

    @router.post("/ba/impact/notify")
    async def notify_impact(request: Request):
        """Tell QC which criteria changed since the customer confirmed the spec."""
        c: Container = request.app.state.container
        form = await request.form()
        path = str(form.get("path", ""))
        back = _back(path, "#ba-customer")
        item_id = _int(form.get("item_id"))
        if item_id <= 0:
            return _flash(back, "ba_no_item")
        if c.config.dry_run:
            return _flash(back, "req_dry_run")
        target, text, _k = ba_view.load_spec(c.config, path)
        if target is None:
            return _flash(back, "ba_no_spec")
        state = await ba_view.confirmation_state(
            c, path, spec_impact.snapshot(text), spec_impact.hash_text(text))
        impact = state["impact"]
        if impact is None or not impact.has_changes:
            return _flash(back, "ba_no_change")
        await c.ado.add_comment(item_id, spec_impact.render_comment(path, impact, []))
        await c.audit_repo.record(actor=_actor(request), source="dashboard",
                                  action="ba.impact_posted", target=f"#{item_id}",
                                  detail=f"{len(impact.changed)} đổi, {len(impact.removed)} bỏ, "
                                         f"{len(impact.added)} thêm")
        return _flash(back, "ba_impact_posted")

    @router.post("/ba/questions/answer")
    async def answer_question(request: Request):
        """Answer one open question; the answer is kept and posted to the work item."""
        c: Container = request.app.state.container
        form = await request.form()
        path = str(form.get("path", ""))
        back = "/dashboard/ba?tab=questions"
        key = str(form.get("key", "")).strip()
        answer = str(form.get("answer", "")).strip()[:4000]
        if not answer:
            return _flash(back, "ba_answer_empty")
        target, text, _k = ba_view.load_spec(c.config, path)
        if target is None:
            return _flash(back, "ba_no_spec")
        question = next((q for q in assumptions.extract(text) if q.key == key), None)
        if question is None:
            return _flash(back, "ba_question_gone")
        item_id = _int(form.get("item_id"))
        await c.assumption_repo.answer(
            spec_path=path, question_key=key, question=question.text, answer=answer,
            answered_by=_actor(request), work_item_id=item_id)
        posted = await _comment(c, item_id, assumptions.render_answer_comment(
            path, question, answer, _actor(request)))
        await c.audit_repo.record(actor=_actor(request), source="dashboard",
                                  action="ba.question_answered",
                                  target=f"#{item_id}" if item_id else path,
                                  detail=question.text[:300])
        return _flash(back, "ba_saved_dry" if item_id and not posted else "ba_answered")

    # ── production feedback ────────────────────────────────────────────────────────

    @router.post("/ba/production/create")
    async def create_from_cluster(request: Request):
        """File one group of production tickets as a DRAFT requirement for a BA to shape."""
        c: Container = request.app.state.container
        cfg = c.config
        form = await request.form()
        back = "/dashboard/ba?tab=production"
        if cfg.dry_run:
            return _flash(back, "req_dry_run")
        key, project = str(form.get("key", "")), str(form.get("project", ""))
        found = next((x for x in (await _production(c))["clusters"]
                      if x["cluster"].key == key and x["project"] == project), None)
        if found is None:
            return _flash(back, "ba_cluster_gone")
        cl = found["cluster"]
        item_id = await c.ado.create_work_item(
            ticket_clusters.render_candidate_title(cl), "User Story", None, DRAFT_TAG,
            description=ticket_clusters.render_candidate_html(
                cl, now=datetime.now(UTC), closed_states=_CLOSED),
            project=project)
        if not item_id:
            return _flash(back, "ba_prod_failed")
        await c.audit_repo.record(actor=_actor(request), source="dashboard",
                                  action="ba.production_requirement", target=f"#{item_id}",
                                  detail=f"{cl.size} ticket: {cl.label}"[:300])
        _log.info("requirement from production feedback", id=item_id, tickets=cl.size)
        return _flash(back, "ba_prod_created")

    return router
