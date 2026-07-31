"""Per-work-item-type state flows: which ADO state each lifecycle stage sets, for
which work-item types.

Why this exists
---------------
ADO states belong to a work-item TYPE. On a stock process template ``Ready to Deploy``
exists on Bug and Task and on nothing else; ``Implement Done`` exists only on
Requirement. Every state setting in this app used to be a single project-wide string,
so one value had to fit every type — which fails two ways at once:

* **It breaks.** ``on_merge_state='Ready to Deploy'`` makes ADO reject the transition
  for a Requirement, and the caller had no way to know the state was the problem.
* **It flattens the flow.** The only states safe to put in a shared field are the ones
  every type happens to share (``Active``), so the richer states a process defines —
  ``Ready to Review`` / ``Review Fails`` on Bug and Task, ``Writing Specification`` /
  ``Implement Done`` / ``QC Fails`` on Requirement — stay unusable.

A *flow* fixes both: it names a group of types and the state each stage sets for them.

Shape (``Settings.work_item_flows`` — plain dicts so YAML round-trips and a live
``setattr`` applies them without a restart)::

    - name: Dev items
      types: [Bug, Task]
      states:   {on_merge: Ready to Deploy, review: Ready to Review, ...}
      tags:     {done: autopilot-done}            # optional per-stage tag override
      comment:  {on_merge: true}                  # optional per-stage comment toggle
      rollup:   ["Ready to Testing = Implement Done"]   # lives on the PARENT's flow

Empty ``work_item_flows`` → every resolver returns the legacy flat setting, so an
existing installation behaves exactly as before until someone defines a flow.

This is a leaf module (no package imports) for the same reason as ``outcomes.py``: the
poller, the PR babysitter and the state-sync service all need it without a cycle.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from typing import Any

# Lifecycle stages, in the order the flow diagram draws them. ``legacy`` is the flat
# ``Settings`` field a stage falls back to, which keeps the fallback in ONE place
# instead of scattered ``getattr`` calls across the services.
STAGES: tuple[tuple[str, str, str], ...] = (
    # (stage key, label, legacy Settings field)
    ("in_progress", "⏳ In progress", "state_in_progress"),
    ("review", "🔍 Review", "state_in_review"),
    ("done", "✅ Done", "resolved_state"),
    ("report", "📝 Report", "state_report"),
    ("needs_human", "🙋 Needs human", "state_needs_human"),
    ("failed", "⛔ Failed", "state_failed"),
    ("on_merge", "🔀 PR merged", "on_merge_state"),
    ("on_deploy", "🚀 Deployed", "on_deploy_state"),
)

STAGE_KEYS: tuple[str, ...] = tuple(key for key, _, _ in STAGES)
_LEGACY_FIELD: dict[str, str] = {key: legacy for key, _, legacy in STAGES}
STAGE_LABELS: dict[str, str] = {key: label for key, label, _ in STAGES}


def _flows(cfg: object) -> list[dict]:
    raw = getattr(cfg, "work_item_flows", None) or []
    return [f for f in raw if isinstance(f, dict)]


def flow_for_type(cfg: object, work_item_type: str) -> dict | None:
    """The first flow whose ``types`` contains ``work_item_type`` (case-insensitive).

    ``None`` when the type is blank or belongs to no flow — the caller then uses the
    legacy flat setting. Validation forbids a type appearing in two flows, so "first
    match" is only a tie-break for hand-edited config that skipped the UI.
    """
    wanted = (work_item_type or "").strip().lower()
    if not wanted:
        return None
    for flow in _flows(cfg):
        types = flow.get("types") or []
        if any(str(t).strip().lower() == wanted for t in types):
            return flow
    return None


def resolve_state(cfg: object, stage: str, work_item_type: str = "") -> str:
    """The ADO state ``stage`` should set for ``work_item_type`` (``""`` = set none).

    Precedence: the matching flow's ``states[stage]``, else the legacy flat field.

    An **absent** key inherits the legacy value (a flow overrides only what it names);
    a key present as ``""`` means "skip this stage" and does NOT fall back. The Flow
    editor always writes all eight keys, so what the page shows is what runs — the
    distinction only matters for hand-edited YAML.
    """
    flow = flow_for_type(cfg, work_item_type)
    if flow is not None:
        states = flow.get("states") or {}
        if isinstance(states, dict) and stage in states:
            return str(states.get(stage) or "").strip()
    return str(getattr(cfg, _LEGACY_FIELD.get(stage, ""), "") or "").strip()


def resolve_tag(cfg: object, stage: str, work_item_type: str, default: str) -> str:
    """The tag ``stage`` should add, letting a flow override the global outcome tag.

    ``default`` is the project-wide tag the caller would otherwise use, so tag policy
    stays owned by ``outcomes.py`` and a flow only deviates where it says so.
    """
    flow = flow_for_type(cfg, work_item_type)
    if flow is not None:
        tags = flow.get("tags") or {}
        if isinstance(tags, dict) and stage in tags:
            return str(tags.get(stage) or "").strip()
    return default


def should_comment(cfg: object, stage: str, work_item_type: str, default: bool = True) -> bool:
    """Whether ``stage`` posts its work-item comment for this type."""
    flow = flow_for_type(cfg, work_item_type)
    if flow is not None:
        comment = flow.get("comment") or {}
        if isinstance(comment, dict) and stage in comment:
            return bool(comment.get(stage))
    return default


def stage_configured(cfg: object, stage: str) -> bool:
    """Whether ``stage`` sets a state anywhere — the flat field or any flow.

    Service loops gate on this. Reading only the flat field would keep a feature switched
    off for an installation that configured it exclusively per type.
    """
    if str(getattr(cfg, _LEGACY_FIELD.get(stage, ""), "") or "").strip():
        return True
    for flow in _flows(cfg):
        states = flow.get("states") or {}
        if isinstance(states, dict) and str(states.get(stage) or "").strip():
            return True
    return False


def rollup_entries(cfg: object) -> bool:
    """Whether any flow defines parent roll-up lines (the flat ``parent_rollup_map``
    is checked by the caller)."""
    return any(flow.get("rollup") for flow in _flows(cfg))


def resolve_rollup(cfg: object, work_item_type: str = "") -> list[str]:
    """The ``"Child = Parent"`` lines governing roll-up for a parent of this type.

    Roll-up lines live on the PARENT's flow, because the state they target is the
    parent's — the child side may come from any type. Falls back to the flat
    ``parent_rollup_map`` when this type's flow doesn't define its own.
    """
    flow = flow_for_type(cfg, work_item_type)
    if flow is not None:
        lines = flow.get("rollup")
        if isinstance(lines, list) and lines:
            return [str(x) for x in lines]
    return [str(x) for x in (getattr(cfg, "parent_rollup_map", None) or [])]


def parse_rollup_entry(entry: str) -> tuple[str, str]:
    """``"Child = Parent"`` → ``("Child", "Parent")``; no separator maps to itself.

    Mirrors ``state_sync.parse_rollup_map`` for a single entry so validation and the
    editor agree with the runtime on what a line means.
    """
    text = str(entry)
    sep = "=" if "=" in text else (":" if ":" in text else "")
    child, parent = (text.split(sep, 1) if sep else (text, text))
    return child.strip(), parent.strip()


def _suggest(name: str, known: list[str]) -> str:
    """`` (did you mean "X"?)`` when a near-miss exists, else ``""``.

    Aimed squarely at the real failure: ``Ready for Testing`` was configured where the
    process defines ``Ready to Testing``, and because nothing compared the value against
    the project it silently disabled parent roll-up entirely.
    """
    others = [k for k in known if k != name]
    close = difflib.get_close_matches(name, others, n=1, cutoff=0.75)
    return f' (did you mean "{close[0]}"?)' if close else ""


def _shortlist(names: list[str], limit: int = 8) -> str:
    head = names[:limit]
    return " · ".join(head) + (f" … (+{len(names) - limit})" if len(names) > limit else "")


def validate_flows(
    flows: list[Any], states_by_type: dict[str, list[str]]
) -> list[str]:
    """Reasons ``flows`` cannot be saved — empty list means valid.

    Checked against the project's real types and states (``get_states_by_type``), so a
    state that ADO would reject is refused at configuration time instead of failing
    per work item, months later, in a log line nobody reads.

    With no type information available (ADO unreachable) only structure is checked —
    refusing to save because the network is down would be worse than saving.
    """
    errors: list[str] = []
    known_types = {t.lower(): t for t in states_by_type}
    all_states = sorted({s for v in states_by_type.values() for s in v})
    claimed: dict[str, str] = {}  # lower type → flow name that already claimed it

    for index, flow in enumerate(flows, start=1):
        if not isinstance(flow, dict):
            errors.append(f"Flow #{index}: not a mapping (expected name/types/states).")
            continue
        name = str(flow.get("name") or "").strip() or f"#{index}"
        types = [str(t).strip() for t in (flow.get("types") or []) if str(t).strip()]
        if not types:
            errors.append(f'Flow "{name}": pick at least one work-item type.')
            continue

        # ── types must be real, and owned by exactly one flow ──
        for type_name in types:
            key = type_name.lower()
            if known_types and key not in known_types:
                errors.append(
                    f'Flow "{name}": work-item type "{type_name}" does not exist in this '
                    f"project{_suggest(type_name, sorted(states_by_type))}."
                )
            elif key in claimed:
                errors.append(
                    f'Work-item type "{type_name}" is in two flows ("{claimed[key]}" and '
                    f'"{name}") — one flow per type, so which one wins is never ambiguous.'
                )
            else:
                claimed[key] = name

        # A state is only settable if EVERY type in the group defines it — otherwise the
        # flow works for some of its own types and 400s for the rest.
        recognised = [t for t in types if t.lower() in known_types]
        common: set[str] | None = None
        if known_types and recognised:
            for type_name in recognised:
                states = set(states_by_type[known_types[type_name.lower()]])
                common = states if common is None else (common & states)

        # ── stage states ──
        states_cfg = flow.get("states") or {}
        if not isinstance(states_cfg, dict):
            errors.append(f'Flow "{name}": "states" must be a mapping of stage → state.')
            states_cfg = {}
        for stage, value in states_cfg.items():
            state = str(value or "").strip()
            if stage not in STAGE_KEYS:
                errors.append(
                    f'Flow "{name}": unknown stage "{stage}". '
                    f"Valid: {' · '.join(STAGE_KEYS)}."
                )
                continue
            if not state or common is None:
                continue  # blank = skip this stage; no type info = nothing to check against
            if state not in common:
                # Say WHERE the state does live. "It exists on Bug but not Feature" and
                # "it exists on no type at all" are different mistakes with different
                # fixes (regroup the types vs. correct a typo), so don't collapse them.
                on = [t for t in recognised if state in states_by_type[known_types[t.lower()]]]
                elsewhere = [t for t in sorted(states_by_type) if state in states_by_type[t]]
                if on:
                    detail = f" It exists on {' · '.join(on)} but not the rest of this group."
                elif elsewhere:
                    detail = (f" It exists on {_shortlist(elsewhere, 4)} — either move those "
                              f"types into this flow or give this group its own state.")
                else:
                    detail = _suggest(state, all_states)
                errors.append(
                    f'Flow "{name}" · {STAGE_LABELS[stage]}: "{state}" is not a state of '
                    f"{' + '.join(recognised)}.{detail} "
                    f"Valid here: {_shortlist(sorted(common))}."
                )

        # ── parent roll-up: child states come from the CHILDREN's types (any type),
        #    the target state must exist on this flow's own types ──
        rollup = flow.get("rollup") or []
        if not isinstance(rollup, list):
            errors.append(f'Flow "{name}": "rollup" must be a list of "Child = Parent" lines.')
            rollup = []
        for entry in rollup:
            child, parent = parse_rollup_entry(entry)
            if not child or not parent:
                errors.append(
                    f'Flow "{name}" · roll-up: "{entry}" is not "Child state = Parent state".'
                )
                continue
            if all_states and child not in all_states:
                errors.append(
                    f'Flow "{name}" · roll-up: child state "{child}" exists on no work-item '
                    f"type in this project{_suggest(child, all_states)} — this line can "
                    f"never match."
                )
            if common is not None and parent not in common:
                errors.append(
                    f'Flow "{name}" · roll-up: target state "{parent}" is not a state of '
                    f"{' + '.join(recognised)}{_suggest(parent, sorted(common))}. "
                    f"Valid here: {_shortlist(sorted(common))}."
                )
    return errors


def parse_flow_form(form: Mapping[str, Any], all_types: list[str]) -> list[dict]:
    """Build the ``work_item_flows`` value from the Flow editor's POST.

    Field names are ``flow{i}_name``, ``flow{i}_type__<Type>``, ``flow{i}_state__<stage>``,
    ``flow{i}_tag__<stage>``, ``flow{i}_comment__<stage>``, ``flow{i}_rollup`` and
    ``flow{i}_delete``, with ``flow_count`` bounding ``i``.

    A slot with no types ticked (or with delete ticked) is dropped rather than reported:
    the page always renders one blank slot at the end so a group can be added without a
    round trip, and an untouched blank slot must not become a validation error.

    All eight stage keys are written explicitly — including empty ones — so the page is
    the whole truth about what runs, with no invisible inheritance from the flat fields.
    """
    try:
        count = int(str(form.get("flow_count", "0")))
    except ValueError:
        count = 0
    flows: list[dict] = []
    for index in range(max(0, count)):
        prefix = f"flow{index}_"
        if form.get(f"{prefix}delete"):
            continue
        types = [t for t in all_types if form.get(f"{prefix}type__{t}")]
        if not types:
            continue
        name = str(form.get(f"{prefix}name", "") or "").strip() or " + ".join(types)
        flow: dict[str, Any] = {
            "name": name,
            "types": types,
            "states": {
                stage: str(form.get(f"{prefix}state__{stage}", "") or "").strip()
                for stage in STAGE_KEYS
            },
        }
        tags = {
            stage: value
            for stage in STAGE_KEYS
            if (value := str(form.get(f"{prefix}tag__{stage}", "") or "").strip())
        }
        if tags:
            flow["tags"] = tags
        # Comments default to on, so only the OFF switches need recording — keeping the
        # saved YAML about what was changed rather than restating every default.
        comment = {
            stage: False
            for stage in (*STAGE_KEYS, "rollup")
            if f"{prefix}comment__{stage}" in form and not form.get(f"{prefix}comment__{stage}")
        }
        if comment:
            flow["comment"] = comment
        rollup = [
            line.strip()
            for line in str(form.get(f"{prefix}rollup", "") or "").splitlines()
            if line.strip()
        ]
        if rollup:
            flow["rollup"] = rollup
        flows.append(flow)
    return flows


def uncovered_types(flows: list[Any], states_by_type: dict[str, list[str]]) -> list[str]:
    """Project types no flow claims — they still use the flat legacy settings.

    Surfaced in the editor so "configured per type" never hides a type that isn't.
    """
    claimed = {
        str(t).strip().lower()
        for flow in flows if isinstance(flow, dict)
        for t in (flow.get("types") or [])
    }
    return [t for t in sorted(states_by_type) if t.lower() not in claimed]
