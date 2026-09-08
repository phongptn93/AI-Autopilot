"""Process lenses: one board per role (BA / Dev / QC / …).

The pipeline columns in :mod:`ai_autopilot.board` are the machine's own shape.
A person arrives with a role instead: a developer asks "what is mine to build",
QC asks "what can I test", a BA asks "what needs a decision". A lens answers one
of those questions with two knobs:

    tags    which work items belong to this process at all — the ADO tags a
            person puts on an item to hand it to that role. Usually left blank:
            in a relay (BA → Dev → QC) the same item passes through every role,
            so a permanent per-role tag would be the wrong shape. Use tags only
            to carve out a stream one process owns end to end.
    stages  the ordered lanes that process reads, each folding one or more
            pipeline columns, so a lens never invents a state the engine does not
            have and no card is silently dropped. A stage marked ``mine`` is where
            THAT role has to act — which is how the hand-off works: an item moves
            column by column, and each move passes the ball to the next role
            without anyone re-tagging anything.

Lenses live in config (``board_lenses``) and are edited at
``/dashboard/board-views``. :data:`DEFAULT_LENSES` is what an unconfigured
install gets — a starting point, not a rule.

Pure functions (no I/O), so the whole model is unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ai_autopilot.board import (
    COL_READY_DEPLOY,
    COL_READY_REVIEW,
    COL_READY_TESTING,
    BoardCard,
    board_columns,
    canon_column,
    handoff_states,
)
from ai_autopilot.config import Settings

TONES: tuple[str, ...] = (
    "slate", "blue", "violet", "purple", "cyan", "teal", "amber", "green", "red",
)

PIPELINE_KEY = "pipeline"


@dataclass(frozen=True)
class BoardLane:
    """One rendered lane: a label over one or more pipeline columns."""

    name: str
    columns: tuple[str, ...]
    # ADO states this lane also claims. The pipeline columns describe what the
    # AUTOPILOT is doing (queued / running / reviewing); a relay's parking spots are
    # states the autopilot does not manage at all — "Ready for Dev" is not "queued",
    # it is "BA finished, Dev has not looked yet". Without this a parked item falls
    # back to Queued and the board says it is the previous role's turn, which is the
    # opposite of the truth. A state match wins over a column match.
    states: tuple[str, ...] = ()
    # ADO tags this lane claims — the other way to park an item, and the one that
    # needs no ADO process change at all: the poller already ignores anything
    # carrying autopilot-done / -review / -hold / -live, so a finished stage brakes
    # itself. Pressing Run on such a lane RELEASES these tags (see the dashboard's
    # /board/run) — leaving the lane and removing what put it there are one act.
    # Only ever claim a tag that exists WHILE PARKED: a long-lived tag (a profile or
    # a team label) would win over the column for the item's whole life and freeze
    # it in this lane.
    tags: tuple[str, ...] = ()
    tone: str = "slate"
    hint: str = ""
    drop: str = ""  # pipeline column applied when a card is dropped here
    # Is the ball in THIS role's court while a card sits here? Work is a relay —
    # BA hands to Dev hands to QC — so ownership is a property of where the item is
    # RIGHT NOW, not a label stuck on it once. A lane that is not "mine" is still
    # shown: a role needs to see what is coming and what it has handed on.
    mine: bool = False


@dataclass(frozen=True)
class BoardView:
    key: str
    label: str
    hint: str = ""
    lanes: tuple[BoardLane, ...] = ()
    tags: tuple[str, ...] = ()  # ADO tags that put an item in this process ( () = all )
    icon: str = ""
    # SDLC profile this process runs when a person triggers an item from its board.
    # This is what makes the relay a REVIEWED hand-off rather than a conveyor belt:
    # the previous role's work stops in a state the poller ignores, a human reads it,
    # and pressing Run here is the act that starts the next role's stages. Blank =
    # the board has no Run button (the machine picks the profile itself, or the
    # process is watch-only).
    profile: str = ""


# Colour + one-line meaning per pipeline column, for the 1:1 pipeline view.
_TONES: dict[str, str] = {
    "Queued": "slate",
    "In progress": "blue",
    "In review": "violet",
    COL_READY_REVIEW: "purple",
    COL_READY_DEPLOY: "teal",
    COL_READY_TESTING: "cyan",
    "Needs human": "amber",
    "Done": "green",
    "Failed": "red",
}

_HINTS: dict[str, str] = {
    "Queued": "picked up next",
    "In progress": "agent working",
    "In review": "self-review / PR checks",
    COL_READY_REVIEW: "waiting on a reviewer",
    COL_READY_DEPLOY: "waiting for the test deploy",
    COL_READY_TESTING: "on test — QC verifying",
    "Needs human": "escalated",
    "Done": "closed",
    "Failed": "run errored",
}

# Shipped defaults. The tags are suggestions a team renames to its own vocabulary;
# the stages only ever reference real pipeline columns.
DEFAULT_LENSES: list[dict] = [
    {
        "key": "ba",
        "profile": "ba",
        "label": "BA",
        "icon": "\U0001f4cb",
        "hint": "Where the scope stands and what needs a decision",
        "tags": [],
        "stages": [
            {"name": "Intake", "columns": ["Queued"], "tone": "slate", "mine": True,
             "hint": "spec not started — your turn", "drop": "Queued"},
            {"name": "In flight",
             "columns": ["In progress", "In review", COL_READY_REVIEW, COL_READY_DEPLOY],
             "tone": "blue", "hint": "being built and deployed", "drop": "In progress"},
            {"name": "Decision needed", "columns": ["Needs human", "Failed"], "tone": "amber",
             "mine": True, "hint": "escalated — needs a call", "drop": "Needs human"},
            {"name": "In testing", "columns": [COL_READY_TESTING], "tone": "cyan",
             "hint": "on the test environment — being verified", "drop": COL_READY_TESTING},
            {"name": "Delivered", "columns": ["Done"], "tone": "green",
             "hint": "closed", "drop": "Done"},
        ],
    },
    {
        "key": "dev",
        "profile": "dev",
        "label": "Dev",
        "icon": "\U0001f6e0",
        "hint": "What is mine to build, review or unblock",
        "tags": [],
        "stages": [
            {"name": "Backlog", "columns": ["Queued"], "tone": "slate",
             "hint": "waiting to start", "drop": "Queued"},
            {"name": "Building", "columns": ["In progress"], "tone": "blue", "mine": True,
             "hint": "agent working — your turn", "drop": "In progress"},
            {"name": "In review", "columns": ["In review", COL_READY_REVIEW],
             "tone": "violet", "mine": True, "hint": "PR open — review it",
             "drop": "In review", "review_owner": True},
            {"name": "Blocked", "columns": ["Needs human", "Failed"], "tone": "red", "mine": True,
             "hint": "escalated or errored", "drop": "Needs human"},
            {"name": "Deploy to test", "columns": [COL_READY_DEPLOY], "tone": "teal",
             "mine": True, "hint": "approved — put it on the test env", "drop": COL_READY_DEPLOY},
            {"name": "With QC", "columns": [COL_READY_TESTING], "tone": "purple",
             "hint": "QC verifying — not yours", "drop": COL_READY_TESTING,
             "qc_handoff": True, "review_context": True},
            {"name": "Shipped", "columns": ["Done"], "tone": "green",
             "hint": "merged / closed", "drop": "Done"},
        ],
    },
    {
        "key": "qc",
        "profile": "qc",
        "label": "QC",
        "icon": "\U0001f9ea",
        "hint": "What can I test, and what came back broken",
        "tags": [],
        "stages": [
            {"name": "Not testable yet", "columns": ["Queued", "In progress"], "tone": "slate",
             "hint": "still being built", "drop": "Queued"},
            {"name": "In dev review", "columns": ["In review", COL_READY_REVIEW],
             "tone": "violet", "hint": "dev's own checks — not yours yet",
             "drop": "In review"},
            {"name": "Waiting on deploy", "columns": [COL_READY_DEPLOY], "tone": "teal",
             "hint": "not on the test env yet — not yours", "drop": COL_READY_DEPLOY},
            {"name": "Ready for testing", "columns": [COL_READY_TESTING], "tone": "cyan",
             "mine": True, "hint": "deployed — your turn to verify",
             "drop": COL_READY_TESTING, "qc_handoff": True, "review_fallback": True},
            {"name": "Needs attention", "columns": ["Failed", "Needs human"], "tone": "red",
             "hint": "failed run or escalation", "drop": "Needs human"},
            {"name": "Passed", "columns": ["Done"], "tone": "green",
             "hint": "accepted and closed", "drop": "Done"},
        ],
    },
]


def role_tag_prefixes(cfg: Settings) -> list[str]:
    """The tag families this instance already speaks, e.g. ``["autopilot-"]``.

    A role tag has to look like the tags the team already uses or nobody will type
    it. Two sources, in order: the family the autopilot's own lifecycle tags belong
    to (``autopilot-review`` / ``-done`` / ``-hold`` → ``autopilot-``), and each
    trigger tag (``vm-autopilot`` → ``vm-autopilot-``). Both are offered, so an
    item tagged either way lands on the right board.
    """
    out: list[str] = []

    def _add(prefix: str) -> None:
        prefix = prefix.strip().strip("-")
        if prefix and f"{prefix}-" not in out:
            out.append(f"{prefix}-")

    for tag in (cfg.review_tag, cfg.processed_tag, cfg.escalation_tag):
        head, sep, _tail = (tag or "").rpartition("-")
        if sep and head:
            _add(head)
    for tag in cfg.effective_trigger_tags:
        _add(tag)
    return out or ["autopilot-"]


def suggested_role_tags(cfg: Settings, role: str) -> list[str]:
    """Tags a team COULD use to hand a whole stream to one role, in this
    instance's vocabulary (``vm-autopilot`` → ``vm-autopilot-qc``). Offered as a
    placeholder in the editor; the shipped processes use none, because a relay
    item belongs to every role in turn."""
    tags = [f"{prefix}{role}" for prefix in role_tag_prefixes(cfg)]
    if role not in tags:
        tags.append(role)
    return tags


def default_lenses(cfg: Settings) -> list[dict]:
    """The shipped BA → Dev → QC relay, wired to THIS machine's hand-off signals.

    No per-role tags on the lens itself: the same work item passes through all
    three, and ownership is read from where it sits right now (the stages marked
    ``mine``), not from a label someone has to remember to move.

    The lanes, though, are bound to the tags this machine actually produces. The
    review tag is the one hand-off that exists on every install with no ADO change
    at all — the autopilot sets it when a PR is waiting on a person — so QC's queue
    is real out of the box instead of empty until someone configures a state.
    """
    review = (cfg.review_tag or "").strip()
    # "Ready for review" means waiting on a REVIEWER, and a reviewer is a developer,
    # so Dev owns it — but only once QC has a queue of its own. Without a testing
    # state configured that column is all QC would ever have, and a role with no turn
    # is a board that can only be watched. So the fallback hands it back to QC and
    # takes it off Dev, because two roles claiming one hand-off means the item is
    # done twice or by nobody.
    has_testing = bool(handoff_states(getattr(cfg, "board_testing_state", None)))
    out: list[dict] = []
    for lens in DEFAULT_LENSES:
        stages = []
        for st in lens["stages"]:
            st = dict(st)
            if review and st.pop("qc_handoff", False):
                st["tags"] = [review]
            else:
                st.pop("qc_handoff", None)
            if st.pop("review_owner", False) and not has_testing:
                st["columns"] = [c for c in st["columns"] if c != COL_READY_REVIEW]
            if st.pop("review_fallback", False) and not has_testing:
                st["columns"] = [COL_READY_REVIEW, *st["columns"]]
                st["drop"] = COL_READY_REVIEW
            # Dev stops owning the column, so Dev's context lane has to pick it up —
            # a column no lane shows is a card that vanishes from that board.
            if st.pop("review_context", False) and not has_testing:
                st["columns"] = [COL_READY_REVIEW, *st["columns"]]
                st["drop"] = COL_READY_REVIEW
            stages.append(st)
        out.append({**lens, "stages": stages})
    return out


def slug(raw: str) -> str:
    out = "".join(ch if ch.isalnum() else "-" for ch in (raw or "").strip().lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")


def pipeline_view(cfg: Settings) -> BoardView:
    """The raw pipeline: one lane per active column, in order."""
    # Keywords, not positions: this dataclass gained a field once already, and the
    # positional call silently shifted tone into states and lost the drop target.
    lanes = tuple(
        BoardLane(
            name=col,
            columns=(col,),
            tone=_TONES.get(col, "slate"),
            hint=_HINTS.get(col, ""),
            drop=col,
        )
        for col in board_columns(cfg)
    )
    return BoardView(
        key=PIPELINE_KEY,
        label="Pipeline",
        hint="Every column the autopilot manages",
        lanes=lanes,
        icon="🛰",
    )


def _view_from_dict(raw: dict, active: set[str]) -> BoardView | None:
    """One configured lens → a view, with columns this config lacks pruned out.

    Returns None when nothing usable survives, so one bad entry costs its own lens
    and not the whole page.
    """
    key = slug(str(raw.get("key") or raw.get("label") or ""))
    if not key or key == PIPELINE_KEY:
        return None
    label = str(raw.get("label") or key).strip() or key
    lanes: list[BoardLane] = []
    for stage in raw.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        name = str(stage.get("name") or "").strip()
        cols = tuple(
            col for col in (canon_column(str(c)) for c in (stage.get("columns") or []))
            if col in active
        )
        states = tuple(str(x).strip() for x in (stage.get("states") or []) if str(x).strip())
        lane_tags = tuple(str(x).strip() for x in (stage.get("tags") or []) if str(x).strip())
        if not name or (not cols and not states and not lane_tags):
            continue
        tone = str(stage.get("tone") or "slate").strip().lower()
        drop = canon_column(str(stage.get("drop") or ""))
        lanes.append(
            BoardLane(
                name=name,
                columns=cols,
                states=states,
                tags=lane_tags,
                tone=tone if tone in TONES else "slate",
                hint=str(stage.get("hint") or "").strip(),
                drop=drop if drop in cols else (cols[0] if cols else ""),
                mine=bool(stage.get("mine")),
            )
        )
    if not lanes:
        return None
    tags = tuple(str(t).strip() for t in (raw.get("tags") or []) if str(t).strip())
    return BoardView(
        key=key,
        label=label,
        hint=str(raw.get("hint") or "").strip(),
        lanes=tuple(lanes),
        tags=tags,
        icon=str(raw.get("icon") or "").strip(),
        profile=str(raw.get("profile") or "").strip(),
    )


def view_of(raw: dict, active_columns) -> BoardView | None:
    """A single lens dict → a view against these columns (None if unusable).

    The editor needs this to score a lens it has not saved yet.
    """
    return _view_from_dict(raw, set(active_columns))


def lens_dicts(cfg: Settings) -> list[dict]:
    """The configured lenses as plain dicts — config first, defaults when unset.

    Column names are canonicalised on the way out, so a config saved before a column
    was renamed still edits (and renders) as the column it means. Rewriting here and
    not on disk keeps the migration a read concern: the file is only rewritten when
    the operator next saves, and an older build reading it still finds what it wrote.
    """
    raw = [e for e in (getattr(cfg, "board_lenses", None) or []) if isinstance(e, dict)]
    if not raw:
        return default_lenses(cfg)
    out: list[dict] = []
    for lens in raw:
        stages = []
        for stage in lens.get("stages") or []:
            if not isinstance(stage, dict):
                continue
            stage = dict(stage)
            stage["columns"] = [canon_column(str(c)) for c in (stage.get("columns") or [])]
            if stage.get("drop"):
                stage["drop"] = canon_column(str(stage["drop"]))
            stages.append(stage)
        out.append({**lens, "stages": stages})
    return out


def board_views(cfg: Settings) -> list[BoardView]:
    """Every selectable view for this config, pipeline first."""
    active = set(board_columns(cfg))
    views = [pipeline_view(cfg)]
    seen = {PIPELINE_KEY}
    for raw in lens_dicts(cfg):
        view = _view_from_dict(raw, active)
        if view is None or view.key in seen:
            continue
        seen.add(view.key)
        views.append(view)
    return views


def board_view(cfg: Settings, key: str | None) -> BoardView:
    """The requested view, falling back to the pipeline for an unknown key."""
    wanted = slug(key or "")
    views = board_views(cfg)
    for view in views:
        if view.key == wanted:
            return view
    return views[0]


def in_lens(card: BoardCard, view: BoardView) -> bool:
    """Does this card belong to the lens's process? (no tags configured = all do)."""
    if not view.tags:
        return True
    wanted = {t.lower() for t in view.tags}
    return any((t or "").strip().lower() in wanted for t in card.tags)


def lens_tag_matches(tags: list[str], views: list[BoardView]) -> list[BoardView]:
    """Which lenses claim an item with these tags (for the card's role chips)."""
    have = {(t or "").strip().lower() for t in (tags or [])}
    return [v for v in views if v.tags and have & {t.lower() for t in v.tags}]


def parked_tags(views: list[BoardView]) -> set[str]:
    """Every ADO tag some process claims as a parking spot, lower-cased.

    Same rule as :func:`parked_states`: once a process says "items with this tag
    live in my lane", no other process may pick them up through the column
    fallback — a card parked for QC is not BA's intake.
    """
    return {t.strip().lower() for v in views for lane in v.lanes for t in lane.tags}


def parked_states(views: list[BoardView]) -> set[str]:
    """Every ADO state some process claims as a parking spot, lower-cased.

    A parked item is ``Queued`` to the engine — it is not running, and nothing about
    the column says which role is holding it. So once ANY process has named a state,
    no other process may quietly absorb it through the column fallback: an item
    waiting for QC must not surface as BA's intake just because BA's first lane
    happens to cover ``Queued``.
    """
    return {s.strip().lower() for v in views for lane in v.lanes for s in lane.states}


def _context_lane(card: BoardCard, view: BoardView) -> BoardLane | None:
    """Where a card parked for ANOTHER process may still be shown here.

    Only a lane this process does not call its turn: seeing what you handed on is
    useful, being told it is your job again is not.
    """
    for lane in view.lanes:
        if not lane.mine and card.column in lane.columns:
            return lane
    return None


def lane_for(
    card: BoardCard,
    view: BoardView,
    parked: set[str] | None = None,
    parked_tag_set: set[str] | None = None,
) -> BoardLane | None:
    """Which lane a card belongs to: tag, then ADO state, then pipeline column.

    Most specific claim wins. A parking TAG is the most specific of all — it is put
    there by whoever stopped the item — then the state it sits in, and only then the
    column, which is merely the autopilot's coarse view ("queued") of an item that
    is in fact waiting for a named person. ``parked``/``parked_tag_set`` close the
    column fallback for spots some OTHER process claims.
    """
    # Running beats every claim: while the agent is working, "In progress" is the
    # most specific truth there is, and a tag left over from the previous stage must
    # not park the card on someone's queue as if it were waiting for them.
    if card.column == "In progress":
        for lane in view.lanes:
            if "In progress" in lane.columns:
                return lane

    tags = {(t or "").strip().lower() for t in card.tags}
    if tags:
        for lane in view.lanes:
            if tags & {t.strip().lower() for t in lane.tags}:
                return lane
        if parked_tag_set and tags & parked_tag_set:
            return _context_lane(card, view)  # someone else's queue — context only
    state = (card.ado_state or "").strip().lower()
    if state:
        for lane in view.lanes:
            if any(state == s.strip().lower() for s in lane.states):
                return lane
        if parked and state in parked:
            return _context_lane(card, view)
    for lane in view.lanes:
        if card.column in lane.columns:
            return lane
    return None


def group_lanes(
    board: dict[str, list[BoardCard]],
    view: BoardView,
    parked: set[str] | None = None,
    parked_tag_set: set[str] | None = None,
) -> dict[str, list[BoardCard]]:
    """Assign every card to exactly one lane of this view, in pipeline order.

    A lens with tags only shows the items its process owns; the pipeline view and
    any untagged lens show everything. A card whose column and state the view claims
    nowhere is left out — which the editor reports as a coverage gap.
    """
    out: dict[str, list[BoardCard]] = {lane.name: [] for lane in view.lanes}
    for cards in board.values():
        for card in cards:
            if not in_lens(card, view):
                continue
            lane = lane_for(card, view, parked, parked_tag_set)
            if lane is not None:
                out[lane.name].append(card)
    return out


# Escalations belong to everyone: an item that stopped needs whoever can restart it,
# which is usually more than one role. Two processes claiming these is a decision, not
# a mistake, so the editor does not flag it — unlike two claiming a FLOW column, which
# means the hand-off is ambiguous and work will be dropped or done twice.
SHARED_COLUMNS: frozenset[str] = frozenset({"Needs human", "Failed"})


def my_turn_columns(view: BoardView) -> set[str]:
    """Pipeline columns where this process has to act."""
    return {col for lane in view.lanes if lane.mine for col in lane.columns}


def my_turn_states(view: BoardView) -> set[str]:
    """ADO states where this process has to act (the relay's parking spots)."""
    return {s for lane in view.lanes if lane.mine for s in lane.states}


def my_turn_tags(view: BoardView) -> set[str]:
    """Parking tags where this process has to act."""
    return {t for lane in view.lanes if lane.mine for t in lane.tags}


def my_turn_claims(view: BoardView) -> set[str]:
    """Everything this process calls its turn — column, state or tag alike.

    One set because the diagnostics ask one question ("does anything ever stop
    here, and does anyone else claim the same spot?"), and a process whose only
    queue is a tag is no less real than one holding a column.
    """
    return my_turn_columns(view) | my_turn_states(view) | my_turn_tags(view)


def my_turn_count(cards_by_lane: dict[str, list[BoardCard]], view: BoardView) -> int:
    """How many of this view's cards are waiting on THIS role right now."""
    return sum(len(cards_by_lane.get(lane.name, [])) for lane in view.lanes if lane.mine)


@dataclass
class LaneRender:
    """A lane ready to draw: its cards, split by source column when folded."""

    lane: BoardLane
    total: int
    hidden: int
    # (sub-heading, cards) — "" for a lane over a single column, else the column's
    # own name, so folding never costs the reader "failed" vs "escalated".
    groups: list[tuple[str, list[BoardCard]]] = field(default_factory=list)


def render_lanes(
    view: BoardView, cards_by_lane: dict[str, list[BoardCard]], limit: int = 0
) -> list[LaneRender]:
    """Apply the per-lane display cap and split each lane by source column."""
    out: list[LaneRender] = []
    for lane in view.lanes:
        cards = cards_by_lane.get(lane.name, [])
        shown = cards[:limit] if limit else cards
        if len(lane.columns) > 1:
            groups = [(col, [c for c in shown if c.column == col]) for col in lane.columns]
            groups = [(name, cs) for name, cs in groups if cs]
        else:
            groups = [("", shown)]
        out.append(
            LaneRender(lane=lane, total=len(cards), hidden=len(cards) - len(shown), groups=groups)
        )
    return out


# ── Lens editor: form parsing + validation ───────────────────────────────────


def _split_list(raw: str) -> list[str]:
    """Comma- or newline-separated free text → a clean list (the editor's list inputs)."""
    parts = (raw or "").replace(chr(10), ",").split(",")
    return [part.strip() for part in parts if part.strip()]


def _field_list(form, name: str) -> list[str]:
    """Every value posted under ``name``, flattened and de-duplicated.

    The editor offers the same field twice — tick-boxes for the states/tags already
    on the board, and a free-text box for one that isn't there yet — so a browser
    posts the name several times. Reading only the first (``form.get``) silently
    dropped every choice but one, which is exactly how a multi-claim lane came back
    from a save holding a single value.
    """
    raw = form.getlist(name) if hasattr(form, "getlist") else [form.get(name)]
    out: list[str] = []
    for chunk in raw:
        for part in _split_list(str(chunk or "")):
            if part not in out:
                out.append(part)
    return out


def parse_lens_form(form, active_columns: list[str]) -> list[dict]:
    """Parse the /dashboard/board-views editor form into ``board_lenses`` entries.

    Fields are ``lens{i}_key``, ``lens{i}_label``, ``lens{i}_icon``, ``lens{i}_hint``,
    ``lens{i}_tags`` (comma/newline separated) and, per stage,
    ``lens{i}_stage{j}_name`` / ``_columns`` (multi-select) / ``_tone`` / ``_hint`` /
    ``_drop``. A blank name drops that lens or stage — that is how a row is deleted.
    """
    active = list(active_columns)
    out: list[dict] = []
    lens_ids = sorted(
        {
            int(head[4:])
            for head in (k.split("_", 1)[0] for k in form if k.startswith("lens"))
            if head[4:].isdigit()
        }
    )
    for i in lens_ids:
        label = str(form.get(f"lens{i}_label") or "").strip()
        key = slug(str(form.get(f"lens{i}_key") or "") or label)
        if not key or not label:
            continue
        raw_tags = str(form.get(f"lens{i}_tags") or "")
        tags = [t.strip() for t in raw_tags.replace("\n", ",").split(",") if t.strip()]
        prefix = f"lens{i}_stage"
        stage_ids = sorted(
            {
                int(num)
                for num in (
                    k[len(prefix):].rsplit("_", 1)[0] for k in form if k.startswith(prefix)
                )
                if num.isdigit()
            }
        )
        stages: list[dict] = []
        for j in stage_ids:
            base = f"{prefix}{j}"
            name = str(form.get(f"{base}_name") or "").strip()
            cols = [str(c).strip() for c in form.getlist(f"{base}_columns") if str(c).strip()]
            cols = [c for c in active if c in cols]  # keep pipeline order
            states = _field_list(form, f"{base}_states")
            lane_tags = _field_list(form, f"{base}_tags")
            if not name or (not cols and not states and not lane_tags):
                continue
            drop = str(form.get(f"{base}_drop") or "").strip()
            tone = str(form.get(f"{base}_tone") or "slate").strip().lower()
            stages.append({
                "name": name,
                "columns": cols,
                "states": states,
                "tags": lane_tags,
                "tone": tone if tone in TONES else "slate",
                "hint": str(form.get(f"{base}_hint") or "").strip(),
                "drop": drop if drop in cols else (cols[0] if cols else ""),
                "mine": bool(form.get(f"{base}_mine")),
            })
        if not stages:
            continue
        out.append({
            "key": key,
            "label": label,
            "icon": str(form.get(f"lens{i}_icon") or "").strip(),
            "hint": str(form.get(f"lens{i}_hint") or "").strip(),
            "profile": str(form.get(f"lens{i}_profile") or "").strip(),
            "tags": tags,
            "stages": stages,
        })
    return out


# Every column name that MEANS something, whether or not this config has it on.
# Validation uses this, not the active set: the shipped processes name the two
# optional hand-off columns, and a machine that has not configured those states is
# not misconfigured — the column is simply pruned at render time. Erroring on it
# would flag a healthy default install.
KNOWN_COLUMNS: frozenset[str] = frozenset(
    ["Queued", "In progress", "In review", COL_READY_REVIEW, COL_READY_DEPLOY,
     COL_READY_TESTING, "Needs human", "Done", "Failed"]
)


def validate_lenses(lenses: list[dict], active_columns: list[str]) -> list[str]:
    """Reasons a lens set would not work, in the reader's words. Empty = save it."""
    errors: list[str] = []
    active = set(active_columns) | KNOWN_COLUMNS
    seen: set[str] = set()
    for lens in lenses:
        key = str(lens.get("key") or "")
        label = str(lens.get("label") or key)
        if key in seen:
            errors.append(f"'{label}': duplicate key '{key}' — each process needs its own key.")
        seen.add(key)
        if key == PIPELINE_KEY:
            errors.append(f"'{label}': 'pipeline' is reserved for the built-in view.")
        for stage in lens.get("stages") or []:
            unknown = [c for c in (stage.get("columns") or []) if c not in active]
            if unknown:
                errors.append(
                    f"'{label}' → '{stage.get('name')}': no such board column: "
                    + ", ".join(unknown)
                )
    return errors


def coverage_gaps(lens: dict, active_columns: list[str]) -> list[str]:
    """Pipeline columns no stage of this lens shows.

    Not an error — a process may deliberately ignore a column — but an item sitting
    in one would be invisible in that lens, which the editor says out loud.
    """
    covered = {c for stage in (lens.get("stages") or []) for c in (stage.get("columns") or [])}
    return [c for c in active_columns if c not in covered]
