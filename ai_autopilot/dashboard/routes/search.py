"""Quick-find data for Ctrl+K — work items, review sessions, specs and reports.

The palette used to know only the menu pages, so "where is that item about the
inventory report?" meant opening the Board and scrolling. This answers from data the
machine already holds locally (no tracker round-trip on every keystroke), accent-
insensitively, because nobody types Vietnamese marks into a search box.
"""

from __future__ import annotations

import unicodedata
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ai_autopilot import spec_library
from ai_autopilot.container import Container

_LIMIT = 6


def norm(text: object) -> str:
    s = unicodedata.normalize("NFD", str(text or "").lower())
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn").replace("đ", "d")


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/search")
    async def quick_search(request: Request, q: str = ""):
        c: Container = request.app.state.container
        needle = norm(q).strip()
        if len(needle) < 2:
            return JSONResponse({"results": []})
        out: list[dict] = []

        def hit(*fields: object) -> bool:
            return any(needle in norm(f) for f in fields if f)

        # Work items this machine has seen (pipeline state is local and complete).
        items = []
        try:
            for st in await c.state_repo.all():
                if hit(st.title, str(st.work_item_id)):
                    items.append(st)
        except Exception:  # noqa: BLE001 — search must never break the page
            items = []
        items.sort(key=lambda s: s.updated_at, reverse=True)
        for st in items[:_LIMIT]:
            out.append({"href": f"/dashboard/task/{st.work_item_id}", "icon": "🧩",
                        "label": f"#{st.work_item_id} {st.title or ''}".strip(),
                        "sub": f"Work item · {st.state.value}", "group": "Work item"})

        try:
            reviews = [r for r in await c.review_repo.recent(300) if hit(r.title, r.target)]
        except Exception:  # noqa: BLE001
            reviews = []
        for r in reviews[:_LIMIT]:
            out.append({"href": f"/dashboard/review/{r.id}", "icon": "🧭",
                        "label": r.title[:90], "sub": f"Phiên rà soát #{r.id}",
                        "group": "Rà soát"})

        try:
            specs = [s for s in spec_library.discover(c.config.workspace_directory)
                     if hit(s.title, s.rel, s.item_id)]
        except Exception:  # noqa: BLE001
            specs = []
        for s in specs[:_LIMIT]:
            out.append({"href": f"/dashboard/requirements/spec?path={quote(s.rel)}",
                        "icon": "📄", "label": s.title[:90], "sub": s.rel, "group": "Spec"})

        try:
            reports = [r for r in await c.loop_report_repo.recent(100)
                       if hit(r.loop_name, r.summary)]
        except Exception:  # noqa: BLE001
            reports = []
        for r in reports[:4]:
            out.append({"href": f"/dashboard/reports/{r.id}", "icon": "🛡️",
                        "label": f"Báo cáo #{r.id} · {r.loop_name}", "sub": (r.summary or "")[:90],
                        "group": "Báo cáo"})
        return JSONResponse({"results": out})

    return router
