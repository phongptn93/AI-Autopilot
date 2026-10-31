"""Group near-duplicate production tickets so a BA sees one requirement gap, not N rows.

Support/Ops tickets repeat the same complaint in different words ("Xuất báo cáo bị
timeout", "xuat bao cao timeout", "Export báo cáo quá lâu"). Read one by one they look
like noise; grouped, "12 tickets in 30 days about the export timing out" is a signal that
the product has a gap the original spec never covered — the production feedback loop a BA
needs to close. This module does only the grouping and the draft wording; deciding
whether the cluster is a real requirement stays with the BA.

Deliberately **pure**: no I/O, no network, and the current time is always passed in
(``now``) so recency logic is testable from literals. Matching is on accent-stripped
token sets (Jaccard), so "đăng nhập" and "dang nhap" are the same words — support staff
type both ways, and a clustering that splits them under-counts exactly the loudest issues.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html import escape

# Words that carry no topic. Stored accent-stripped, because tokens are stripped before
# the lookup. "error/bug/issue/loi" are in the list on purpose: nearly every ticket says
# them, so they would glue unrelated tickets together.
_STOP_WORDS = frozenset(
    {
        # Vietnamese (accent-stripped)
        "loi", "khong", "bi", "khi", "cho", "cua", "va", "voi", "cac", "nhung", "mot",
        "the", "nay", "do", "da", "dang", "duoc", "se", "thi", "la", "co", "trong",
        "tren", "tai", "den", "tu", "vao", "ra", "hay", "nhu", "nen", "can", "xin",
        "vui", "long", "anh", "chi", "em", "sao", "gap", "van", "de", "ban", "hien",
        # English
        "and", "for", "are", "was", "with", "from", "that", "this", "not",
        "has", "have", "cannot", "when", "while", "after", "before", "into",
        "error", "bug", "issue", "problem", "fail", "failed", "please",
    }
)  # fmt: skip
_SPLIT_RE = re.compile(r"[^0-9a-z]+")
_MIN_TOKEN_LEN = 3
_MAX_LISTED = 10
_TITLE_PREFIX = "[Phản hồi production] "
_TITLE_MAX = 120


@dataclass(frozen=True)
class Ticket:
    """One support/production ticket, reduced to what clustering reads."""

    id: int
    title: str
    state: str = ""
    tags: tuple[str, ...] = ()
    created: datetime | None = None
    area_path: str = ""


def _utc(dt: datetime) -> datetime:
    """Naive datetimes are treated as UTC so aware/naive values compare safely."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _sort_key(t: Ticket) -> tuple[int, datetime, int]:
    """Oldest first, undated last, ties by id."""
    if t.created is None:
        return (1, datetime.min.replace(tzinfo=UTC), t.id)
    return (0, _utc(t.created), t.id)


def _strip_accents(text: str) -> str:
    text = text.replace("đ", "d").replace("Đ", "D")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def tokens(title: str) -> frozenset[str]:
    """Topic words of a title: lower-case, accent-free, no stop-words/short/numeric."""
    plain = _strip_accents(title or "").lower()
    out: set[str] = set()
    for raw in _SPLIT_RE.split(plain):
        if len(raw) < _MIN_TOKEN_LEN or raw in _STOP_WORDS:
            continue
        if any(ch.isdigit() for ch in raw) and raw.isdigit():
            continue
        out.add(raw)
    return frozenset(out)


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity; 0.0 when either set is empty."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class Cluster:
    """A group of near-duplicate tickets."""

    key: str
    label: str
    tickets: list[Ticket] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.tickets)

    def _dates(self) -> list[datetime]:
        return [_utc(t.created) for t in self.tickets if t.created is not None]

    @property
    def first_seen(self) -> datetime | None:
        dates = self._dates()
        return min(dates) if dates else None

    @property
    def last_seen(self) -> datetime | None:
        dates = self._dates()
        return max(dates) if dates else None

    def open_count(self, closed_states: Iterable[str]) -> int:
        """Tickets whose state (case-insensitive) is not one of ``closed_states``."""
        closed = {s.strip().lower() for s in closed_states}
        return sum(1 for t in self.tickets if t.state.strip().lower() not in closed)


def _label(members: list[Ticket], toks: dict[int, frozenset[str]]) -> str:
    """Shortest title among those sharing the most tokens with the rest."""

    def shared(t: Ticket) -> int:
        return sum(len(toks[t.id] & toks[o.id]) for o in members if o is not t)

    best = max(shared(t) for t in members)
    candidates = [t for t in members if shared(t) == best]
    return min(candidates, key=lambda t: (len(t.title), t.id)).title


def cluster(
    tickets: Sequence[Ticket], *, threshold: float = 0.5, min_size: int = 2
) -> list[Cluster]:
    """Greedy single-link grouping of near-duplicate tickets.

    A ticket joins the cluster holding its best match at or above ``threshold`` (ties go
    to the older cluster). Input is sorted first so the result never depends on order.
    """
    ordered = sorted(tickets, key=_sort_key)
    toks = {t.id: tokens(t.title) for t in ordered}
    groups: list[list[Ticket]] = []
    for t in ordered:
        best_idx = -1
        best_score = 0.0
        for idx, members in enumerate(groups):
            score = max(similarity(toks[t.id], toks[m.id]) for m in members)
            if score >= threshold and score > best_score:
                best_idx, best_score = idx, score
        if best_idx >= 0:
            groups[best_idx].append(t)
        else:
            groups.append([t])

    result = [
        Cluster(key=str(g[0].id), label=_label(g, toks), tickets=sorted(g, key=_sort_key))
        for g in groups
        if len(g) >= min_size
    ]
    result.sort(key=lambda c: c.key)
    result.sort(
        key=lambda c: c.last_seen.timestamp() if c.last_seen else float("-inf"), reverse=True
    )
    result.sort(key=lambda c: c.size, reverse=True)
    return result


def rising(c: Cluster, *, now: datetime, window_days: int = 14) -> bool:
    """True when most of the cluster's dated tickets are recent (and there are >= 3)."""
    dates = c._dates()
    if len(dates) < 3:
        return False
    cutoff = _utc(now) - timedelta(days=window_days)
    recent = sum(1 for d in dates if d >= cutoff)
    return recent * 2 > len(dates)


def _fmt(dt: datetime | None) -> str:
    return _utc(dt).strftime("%d/%m/%Y") if dt else "không rõ"


def render_candidate_html(
    c: Cluster, *, now: datetime | None = None, closed_states: Iterable[str] = ()
) -> str:
    """HTML description for a DRAFT requirement created from the cluster."""
    closed = list(closed_states)
    parts = [
        f"<p><b>Nhóm phản hồi production:</b> {escape(c.label)}</p>",
        f"<p>Số ticket: <b>{c.size}</b>",
    ]
    if closed:
        parts[-1] += f" (còn mở: {c.open_count(closed)})"
    parts[-1] += "</p>"
    parts.append(
        f"<p>Xuất hiện lần đầu: {_fmt(c.first_seen)} — gần nhất: {_fmt(c.last_seen)}</p>"
    )
    if now is not None and rising(c, now=now):
        parts.append("<p><b>Xu hướng tăng</b> trong 14 ngày gần đây.</p>")
    items = [
        f"<li>#{t.id} — {escape(t.title)} ({escape(t.state or 'không rõ')})</li>"
        for t in c.tickets[:_MAX_LISTED]
    ]
    parts.append("<ul>" + "".join(items) + "</ul>")
    extra = c.size - _MAX_LISTED
    if extra > 0:
        parts.append(f"<p>… và {extra} ticket nữa</p>")
    parts.append(
        "<p>BA vui lòng xác nhận nhu cầu thực sự đằng sau các phản hồi này "
        "trước khi chuyển thành yêu cầu chính thức.</p>"
    )
    return "\n".join(parts)


def render_candidate_title(c: Cluster) -> str:
    """Work-item title for the draft requirement, capped at 120 characters."""
    return (_TITLE_PREFIX + c.label)[:_TITLE_MAX]
