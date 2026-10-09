"""Why does (or doesn't) this machine pick up a work item? — the trigger rules, explained.

The poller decides with a WIQL query plus a few in-process filters spread over three
files. Each rule is sensible on its own; together they are hard to predict, and the
only symptom of a wrong guess is silence — the item just never runs. This module
replays the same rules against ONE item and says, step by step, which passed.

Pure: it reads the config and an item and touches nothing, so the Settings page can
explain a live item and tests can pin every branch. When a rule changes in the poller,
change it here too — ``tests/test_trigger_check.py`` mirrors the poller's own cases.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ai_autopilot.config import Settings, matches_user
from ai_autopilot.execution import sdlc_plan
from ai_autopilot.models.work_item import WorkItemInfo


@dataclass
class Check:
    """One rule and how the item fared. ``ok=None`` = informational, not a gate."""

    label: str
    ok: bool | None
    detail: str = ""


@dataclass
class TriggerReport:
    item_id: int
    picked: bool
    headline: str
    checks: list[Check] = field(default_factory=list)
    role: str = ""
    run_now: str = ""


@dataclass
class StateSource:
    """A state the trigger list mentions, and WHY it is (or is not) polled."""

    state: str
    polled: bool
    source: str   # "manual" | "role-auto" | "role-manual"
    roles: list[str] = field(default_factory=list)


def owner(cfg: Settings) -> str:
    """The assignee the shared tag is scoped to — same fallback as the WIQL."""
    return (cfg.assignee_trigger_user or cfg.auto_transition_assignee or "").strip()


def summary(cfg: Settings) -> str:
    """One sentence a person can check against the board: what this machine takes."""
    mine = [t for t in cfg.effective_trigger_tags if t]
    parts = []
    if mine:
        parts.append("tag " + ", ".join(f"`{t}`" for t in mine))
    atag = (cfg.assignee_trigger_tag or "").strip()
    if atag:
        who = owner(cfg)
        parts.append(f"tag chung `{atag}` " + (f"được giao cho **{who}**" if who
                                              else "(chưa đặt assignee → nhận MỌI item)"))
    states = cfg.effective_trigger_states
    what = " hoặc ".join(parts) if parts else "(không có tag nào — máy không nhận việc)"
    where = ", ".join(states) if states else "(không state nào)"
    return f"Máy này nhận item có {what} — khi state thuộc: {where}."


def state_sources(cfg: Settings) -> list[StateSource]:
    """Every state either list mentions, with the rule that decided it.

    ``effective_trigger_states`` is ``trigger_states`` amended by the role doors: an
    auto role adds its door, a manual one removes it. That amendment is invisible on
    the state checklist, which is how a ticked state could silently not poll.
    """
    doors: dict[str, dict[str, list[str]]] = {}
    for name, role in sdlc_plan.effective_roles(cfg).items():
        for part in (role.waits_in or "").replace("\n", ",").split(","):
            door = part.strip()
            if door:
                key = "auto" if role.auto else "manual"
                doors.setdefault(door.lower(), {"auto": [], "manual": [], "name": door})
                doors[door.lower()][key].append(name)
    polled = {s.strip().lower() for s in cfg.effective_trigger_states}
    out: list[StateSource] = []
    seen: set[str] = set()
    for s in [*cfg.trigger_states, *cfg.effective_trigger_states]:
        k = (s or "").strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        d = doors.get(k)
        if d and d["manual"] and k not in polled:
            out.append(StateSource(s, False, "role-manual", sorted(d["manual"])))
        elif d and d["auto"]:
            out.append(StateSource(s, k in polled, "role-auto", sorted(d["auto"])))
        else:
            out.append(StateSource(s, k in polled, "manual"))
    return out


def explain(item: WorkItemInfo, cfg: Settings, *, already_processed: bool = False
            ) -> TriggerReport:
    """Replay the poller's pickup rules for ``item``."""
    tags = [t for t in (item.tags or []) if t]
    low = {t.lower() for t in tags}
    checks: list[Check] = []

    # 1 — project scope (``_project_clause``)
    projects = [p.lower() for p in cfg.effective_ado_projects]
    in_project = not item.project or not projects or item.project.lower() in projects
    checks.append(Check(
        "Project được quét", in_project,
        f"`{item.project}`" + ("" if in_project
                               else " — không nằm trong danh sách project máy này quét")))

    # 2 — ownership (``_candidate_clause`` / ``_owns_item``)
    mine = [t for t in cfg.effective_trigger_tags if t and t.lower() in low]
    atag = (cfg.assignee_trigger_tag or "").strip()
    who = owner(cfg)
    via_shared = False
    if atag and atag.lower() in low:
        via_shared = matches_user(item.assigned_to_email, item.assigned_to, who)
    owned = bool(mine) or via_shared
    if mine:
        detail = f"Có tag riêng của máy: `{mine[0]}`"
    elif via_shared:
        detail = (f"Có tag chung `{atag}` và được giao cho {item.assigned_to or '—'}"
                  + (f" (khớp `{who}`)" if who else " — chưa đặt assignee nên nhận mọi item"))
    elif atag and atag.lower() in low:
        detail = (f"Có tag chung `{atag}` nhưng item giao cho "
                  f"**{item.assigned_to or 'không ai'}**, không khớp `{who}`")
    else:
        wanted = [f"`{t}`" for t in cfg.effective_trigger_tags if t]
        if atag:
            wanted.append(f"`{atag}` (+ assign cho {who or 'bất kỳ ai'})")
        detail = "Không có tag nào của máy này. Cần một trong: " + (", ".join(wanted) or "—")
    checks.append(Check("Thuộc về máy này", owned, detail))

    # 3 — state (``effective_trigger_states``), annotated with its source
    sources = {s.state.strip().lower(): s for s in state_sources(cfg)}
    st = (item.state or "").strip()
    src = sources.get(st.lower())
    state_ok = st.lower() in {s.strip().lower() for s in cfg.effective_trigger_states}
    if state_ok and src and src.source == "role-auto":
        detail = f"`{st}` là cửa vào của vai **{', '.join(src.roles)}** (tự động)"
    elif state_ok:
        detail = f"`{st}` được tick trong State kích hoạt"
    elif src and src.source == "role-manual":
        detail = (f"`{st}` là cửa của vai **{', '.join(src.roles)}** nhưng vai đó KHÔNG tự "
                  "chạy — cần người bấm Run hoặc gắn tag chạy ngay")
    else:
        detail = (f"`{st or '—'}` không có trong State kích hoạt: "
                  + (", ".join(cfg.effective_trigger_states) or "—"))
    checks.append(Check("State được poll", state_ok, detail))

    # 4 — held / finished (the poller's ``skip_tags`` + processed memory)
    held_map = {
        (cfg.escalation_tag or "").lower(): "đang chờ người (escalation)",
        (cfg.processed_tag or "").lower(): "đã xử lý xong",
        (cfg.review_tag or "").lower(): "đang chờ review",
        (cfg.live_tag or "").lower(): "đang có phiên interactive",
    }
    held_map.pop("", None)
    held = [f"`{t}` — {held_map[t.lower()]}" for t in tags if t.lower() in held_map]
    if already_processed:
        held.append("đã xử lý trong phiên poller hiện tại")
    checks.append(Check("Không bị giữ lại", not held,
                        "; ".join(held) if held else "Không có tag giữ lại"))

    # Informational: the plan / risk gates hold an item AFTER pickup, not before.
    gates = [t for t in tags if t.lower() in {
        (cfg.plan_pending_tag or "").lower(), (cfg.risk_gate_tag or "").lower()} - {""}]
    if gates:
        checks.append(Check("Cổng chờ duyệt", None,
                            "Đang chờ duyệt: " + ", ".join(f"`{t}`" for t in gates)))

    # 5 — run-now tags: a separate door (``_reconcile_stage_entries``). It queries by the
    # run-now tag itself, so it needs neither ownership nor a polled state — putting this
    # machine's run-now tag on an item IS naming this machine. A role tag beats the
    # shared one when both are present, as in the sweep.
    run_map = sdlc_plan.run_now_tags(cfg)
    hits = [t for t in tags if t.lower() in run_map]
    run_now = next((t for t in hits if run_map[t.lower()]), hits[0] if hits else "")
    forced = run_map.get(run_now.lower()) if run_now else None
    live = (cfg.live_tag or "").lower()
    if run_now:
        checks.append(Check("Tag chạy ngay", True,
                            f"`{run_now}` → " + (f"chạy vai **{forced}**" if forced
                                                 else "vai theo state hiện tại")
                            + " · bỏ qua điều kiện tag/state, tag bị gỡ khi nhận"))

    role = forced or sdlc_plan.resolve_profile_name(tags, item.work_item_type, cfg,
                                                    state=item.state)
    checks.append(Check("Vai sẽ chạy", None, f"**{role or '—'}**"))

    picked_poll = in_project and owned and state_ok and not held
    picked_now = (bool(run_now) and in_project and not already_processed
                  and not (live and live in low))
    picked = picked_poll or picked_now
    if picked_now and not picked_poll:
        # Those rules did not block anything — the run-now door does not ask them.
        for c in checks:
            if c.ok is False and c.label in ("Thuộc về máy này", "State được poll"):
                c.ok, c.detail = None, c.detail + " — không cần, tag chạy ngay bỏ qua"
        headline = f"✅ Sẽ chạy ngay nhờ tag `{run_now}` (bỏ qua State kích hoạt)."
    elif picked:
        headline = f"✅ Máy này SẼ nhận #{item.id} ở lượt quét tới — vai **{role}**."
    else:
        first = next(c for c in checks if c.ok is False)
        headline = f"⛔ Máy này KHÔNG nhận #{item.id}: {first.label.lower()} — {first.detail}"
    return TriggerReport(item.id, picked, headline, checks, role=role, run_now=run_now)
