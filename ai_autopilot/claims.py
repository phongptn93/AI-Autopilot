"""Claims between machines that share nothing but the tracker.

Several autopilot machines can see the same piece of work — a conflicted PR, an item
carrying the shared run-now tag — and each keeps its "already handled" memory in its
own database, so none of them can tell the others "mine". The one state they DO share
is the work item (or PR) itself, so that is where a claim lives: a machine posts a
comment carrying a marker, waits for the others' markers to land, and the EARLIEST
claim wins. Losers walk away silently.

Pure functions only: parsing a marker, ordering claims, picking the winner. The
posting and waiting belong to the caller, because only the caller knows which API the
comment goes through and what "earliest" can be measured by there (a PR thread has a
publish date; a work-item comment only has an ever-increasing id).
"""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass
from datetime import UTC, datetime

CLAIM_TAG = "autopilot-claim"
# key: letters, digits, ':', '_', '-'. machine: anything up to whitespace or a tag, so
# the marker survives being wrapped in <sub> by the comment renderer.
CLAIM_RE = re.compile(re.escape(CLAIM_TAG) + r":([A-Za-z0-9:_\-]+)\s*·\s*([^<\s]+)")


@dataclass(frozen=True)
class Claim:
    """One marker found on the shared object.

    ``order`` is whatever the source can rank by — a comment id, or an ISO timestamp.
    It only has to sort the same way on every machine, which both of those do.
    """

    order: object
    machine: str
    key: str


def machine_name(cfg: object) -> str:
    """This machine's name in a claim: the fleet name, else its trigger tag, else the host.

    The same precedence the PR-conflict claim uses, so a machine is called the same
    thing in every claim it writes and an operator can match them up.
    """
    return (
        (getattr(cfg, "fleet_worker_name", "") or "").strip()
        or (getattr(cfg, "trigger_tag", "") or "").strip()
        or socket.gethostname()
    )


def marker(key: str, machine: str) -> str:
    """The machine-readable part of a claim comment."""
    return f"{CLAIM_TAG}:{key} · {machine}"


def parse(text: str) -> tuple[str, str] | None:
    """``(key, machine)`` from a comment's text, or None when it carries no claim."""
    hit = CLAIM_RE.search(str(text or ""))
    return (hit.group(1), hit.group(2)) if hit else None


def claims_in_comments(comments: list[dict], key: str) -> list[Claim]:
    """Every claim for ``key`` among work-item comments, earliest first.

    Work-item comment ids increase monotonically per item, which makes the id a better
    "who was first" than any timestamp: it is assigned by the server in arrival order,
    so two machines' clocks never enter into it.
    """
    out: list[Claim] = []
    for cm in comments or []:
        found = parse(str(cm.get("text") or cm.get("content") or ""))
        if not found or found[0] != key:
            continue
        try:
            order = int(cm.get("id") or 0)
        except (TypeError, ValueError):
            continue
        out.append(Claim(order=order, machine=found[1], key=key))
    return sorted(out, key=lambda c: c.order)


def current_episode(comments: list[dict]) -> list[dict]:
    """The comments since the last BOT comment that is not itself a claim.

    A work item is claimed again every time someone re-applies the run-now tag, and the
    old claims stay in its history forever. A key cannot tell the episodes apart without
    a timestamp the comment list does not carry — the revision number moves the moment
    a claim is posted (so two machines would compute different keys), and a calendar
    date splits a race that straddles midnight. The work itself draws the line instead:
    the winner's run posts its own comments, so any claim older than the last non-claim
    bot comment belongs to a run that already happened. Every machine applies the same
    rule to the same list, so they agree on the boundary as well as on the winner.
    """
    rows = list(comments or [])
    cut = 0
    for idx, cm in enumerate(rows):
        text = str(cm.get("text") or cm.get("content") or "")
        if cm.get("is_bot") and parse(text) is None:
            cut = idx + 1
    return rows[cut:]


def winner(claims: list[Claim]) -> str | None:
    """The machine holding the earliest claim, or None when there is none."""
    return claims[0].machine if claims else None


def age_hours(published: str, now: datetime | None = None) -> float:
    """Hours since an ISO timestamp; 0 when it cannot be read (treated as fresh).

    Fresh rather than ancient on purpose: an unreadable date must not make a live claim
    look dead and let a second machine start the same work.
    """
    try:
        at = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - at).total_seconds() / 3600


def safe_key(raw: str) -> str:
    """Squash anything into the marker's key alphabet, so a key never breaks parsing."""
    return re.sub(r"[^A-Za-z0-9:_\-]", "-", str(raw or "")).strip("-") or "x"
