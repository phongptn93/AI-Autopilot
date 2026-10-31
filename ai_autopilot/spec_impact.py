"""Spec impact: which test cases go stale when the specification changes?

Spec drift reports code that left the spec. This is the other direction: the BA edits an
acceptance criterion AFTER the customer confirmed it, and every QC test case written
against the OLD wording silently becomes wrong. QC then fails a build against an AC that
no longer exists, or passes one against a rule nobody asked for. The only moment anyone
can cheaply fix that is when the edit happens.

So: take two snapshots of a spec's acceptance criteria, say what was added, removed and
changed, and name the test cases that need a second look. Cases are linked to criteria by
the AC id they mention (``AC-02``); when no case mentions any id at all there is no
linkage to follow, and the honest answer is "review everything" rather than "nothing".

Pure and dependency-free, like :mod:`ai_autopilot.risk`: text in, verdict out, no I/O and
no clock. Whitespace and letter-case are never a change — a re-flowed paragraph must not
wake QC up.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from ai_autopilot import spec_readiness

#: Prefix of the work-item comment, stable like ``spec_readiness.COMMENT_PREFIX`` so a
#: board query or an "already posted" check can match it across releases.
COMMENT_PREFIX = "🔄 SPEC-CHANGED"

#: Similarity above which two UNNAMED criteria are treated as the same criterion edited.
SIMILARITY = 0.85

#: Entries listed per section in a comment before the rest collapses into "… và N mục nữa".
MAX_LIST = 15

NO_LINKAGE_REASON = "chưa liên kết AC — rà lại toàn bộ"
UNNAMED_REASON = "AC đổi nhưng không có mã — rà lại toàn bộ"

_WS = re.compile(r"\s+")
_AC_TOKEN = re.compile(r"(?<![A-Za-z0-9])AC[-_ ]?0*(\d+)(?![A-Za-z0-9])", re.IGNORECASE)


def _norm(text: str) -> str:
    """Whitespace-collapsed, case-folded form used for every comparison."""
    return _WS.sub(" ", text or "").strip().casefold()


def _norm_id(ac_id: str) -> str:
    """``AC-01`` / ``ac_1`` / ``AC 001`` -> ``AC-1``; anything else -> ``""``."""
    m = _AC_TOKEN.fullmatch((ac_id or "").strip())
    return f"AC-{m.group(1)}" if m else ""


def hash_text(text: str) -> str:
    """SHA-256 of the whitespace-normalised, case-PRESERVED text. Cheap "did the spec
    change at all" check to store beside a snapshot; stable under re-flowing."""
    return hashlib.sha256(_WS.sub(" ", text or "").strip().encode("utf-8")).hexdigest()


def snapshot(text: str) -> list[dict[str, str]]:
    """The acceptance criteria of a spec as plain dicts (JSON-friendly), document order."""
    return [{"id": a.id, "text": a.text} for a in spec_readiness.extract_acs(text)]


@dataclass
class Impact:
    added: list[dict[str, str]] = field(default_factory=list)
    removed: list[dict[str, str]] = field(default_factory=list)
    changed: list[dict[str, str]] = field(default_factory=list)   # {"id","before","after"}
    unchanged: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.changed)

    @property
    def changed_ids(self) -> list[str]:
        """Ids of changed + removed criteria (non-empty only): the ones whose test cases
        were written against wording that no longer holds."""
        ids = [c.get("id", "") for c in self.changed] + [r.get("id", "") for r in self.removed]
        return [i for i in ids if i]


def _split_named(
    items: list[dict[str, str]],
) -> tuple[dict[str, dict[str, str]], list[dict[str, str]]]:
    """Named criteria keyed by normalised id (first occurrence wins); duplicates of an id
    and criteria without one fall into the unnamed list."""
    named: dict[str, dict[str, str]] = {}
    unnamed: list[dict[str, str]] = []
    for it in items:
        key = _norm_id(it.get("id", ""))
        if key and key not in named:
            named[key] = it
        else:
            unnamed.append(it)
    return named, unnamed


def diff(old: list[dict[str, str]], new: list[dict[str, str]]) -> Impact:
    """Compare two snapshots. Named criteria match by id; unnamed ones match by text
    similarity (>= :data:`SIMILARITY`), best pair first."""
    impact = Impact()
    old_named, old_unnamed = _split_named(old)
    new_named, new_unnamed = _split_named(new)

    for key, o in old_named.items():
        n = new_named.get(key)
        if n is None:
            impact.removed.append({"id": o.get("id", ""), "text": o.get("text", "")})
        elif _norm(o.get("text", "")) == _norm(n.get("text", "")):
            impact.unchanged += 1
        else:
            impact.changed.append({"id": n.get("id", "") or o.get("id", ""),
                                   "before": o.get("text", ""), "after": n.get("text", "")})
    for key, n in new_named.items():
        if key not in old_named:
            impact.added.append({"id": n.get("id", ""), "text": n.get("text", "")})

    pairs: list[tuple[float, int, int]] = []
    for i, o in enumerate(old_unnamed):
        for j, n in enumerate(new_unnamed):
            a, b = _norm(o.get("text", "")), _norm(n.get("text", ""))
            ratio = SequenceMatcher(None, a, b).ratio()
            if ratio >= SIMILARITY:
                pairs.append((ratio, i, j))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))
    used_old: set[int] = set()
    used_new: set[int] = set()
    for _ratio, i, j in pairs:
        if i in used_old or j in used_new:
            continue
        used_old.add(i)
        used_new.add(j)
        o, n = old_unnamed[i], new_unnamed[j]
        if _norm(o.get("text", "")) == _norm(n.get("text", "")):
            impact.unchanged += 1
        else:
            impact.changed.append({"id": "", "before": o.get("text", ""),
                                   "after": n.get("text", "")})
    for i, o in enumerate(old_unnamed):
        if i not in used_old:
            impact.removed.append({"id": "", "text": o.get("text", "")})
    for j, n in enumerate(new_unnamed):
        if j not in used_new:
            impact.added.append({"id": "", "text": n.get("text", "")})
    return impact


def _refs(*texts: str) -> set[str]:
    return {f"AC-{m.group(1)}" for t in texts for m in _AC_TOKEN.finditer(t or "")}


def affected_cases(impact: Impact, cases: list[dict[str, str]]) -> list[dict[str, str]]:
    """Test cases that need a re-review: the ones that cite a changed or removed AC."""
    if not impact.has_changes:
        return []
    case_refs = [_refs(c.get("ac", ""), c.get("title", "")) for c in cases]
    if not any(case_refs):
        return [{"title": c.get("title", ""), "reason": NO_LINKAGE_REASON} for c in cases]

    stale = {_norm_id(i): i for i in impact.changed_ids}
    stale.pop("", None)
    if not stale:
        unnamed_edit = any(not c.get("id") for c in impact.changed) or any(
            not r.get("id") for r in impact.removed)
        if unnamed_edit:
            return [{"title": c.get("title", ""), "reason": UNNAMED_REASON} for c in cases]
        return []

    removed = {_norm_id(r.get("id", "")) for r in impact.removed}
    out: list[dict[str, str]] = []
    for c, refs in zip(cases, case_refs, strict=True):
        hit = sorted((r for r in refs if r in stale), key=lambda r: int(r[3:]))
        if not hit:
            continue
        parts = [f"{r} {'đã bị xoá' if r in removed else 'đã đổi'}" for r in hit]
        out.append({"title": c.get("title", ""), "reason": "; ".join(parts)})
    return out


def _label(ac_id: str) -> str:
    return ac_id or "(không mã)"


def _more(total: int) -> str:
    return f"… và {total - MAX_LIST} mục nữa"


def render_comment(title: str, impact: Impact, cases: list[dict[str, str]]) -> str:
    """HTML work-item comment. Every piece of user text goes through ``html.escape``."""
    e = html.escape
    out = [f"<p><b>{COMMENT_PREFIX}</b> — {e(title)}</p>"]
    if not impact.has_changes:
        out.append("<p>Không có thay đổi tiêu chí chấp nhận.</p>")
        return "\n".join(out)
    out.append(f"<p>Tiêu chí chấp nhận đã thay đổi so với bản trước "
               f"({len(impact.changed)} đổi, {len(impact.removed)} xoá, "
               f"{len(impact.added)} thêm, {impact.unchanged} giữ nguyên).</p>")

    def section(heading: str, rows: list[str]) -> None:
        if not rows:
            return
        out.append(f"<p><b>{heading} ({len(rows)})</b></p><ul>")
        out.extend(f"<li>{r}</li>" for r in rows[:MAX_LIST])
        if len(rows) > MAX_LIST:
            out.append(f"<li>{_more(len(rows))}</li>")
        out.append("</ul>")

    section("✏️ Đã đổi", [
        f"<b>{e(_label(c.get('id', '')))}</b>: {e(c.get('before', ''))} → {e(c.get('after', ''))}"
        for c in impact.changed])
    section("🗑️ Đã xoá", [
        f"<b>{e(_label(r.get('id', '')))}</b>: {e(r.get('text', ''))}" for r in impact.removed])
    section("➕ Mới thêm", [
        f"<b>{e(_label(a.get('id', '')))}</b>: {e(a.get('text', ''))}" for a in impact.added])
    section("🧪 Test case cần rà lại", [
        f"{e(c.get('title', ''))} — <i>{e(c.get('reason', ''))}</i>" for c in cases])
    return "\n".join(out)


def _cell(text: str) -> str:
    """Make text safe inside a Markdown table cell."""
    return _WS.sub(" ", text or "").replace("|", "\\|").strip()


def render_markdown(title: str, impact: Impact, cases: list[dict[str, str]]) -> str:
    """Same content as :func:`render_comment`, as Markdown with a table."""
    out = [f"**{COMMENT_PREFIX}** — {_cell(title)}", ""]
    if not impact.has_changes:
        out.append("Không có thay đổi tiêu chí chấp nhận.")
        return "\n".join(out)
    out.append(f"Tiêu chí chấp nhận đã thay đổi ({len(impact.changed)} đổi, "
               f"{len(impact.removed)} xoá, {len(impact.added)} thêm, "
               f"{impact.unchanged} giữ nguyên).")
    out.append("")

    rows: list[tuple[str, str, str, str]] = []
    rows += [("Đổi", _label(c.get("id", "")), c.get("before", ""), c.get("after", ""))
             for c in impact.changed]
    rows += [("Xoá", _label(r.get("id", "")), r.get("text", ""), "") for r in impact.removed]
    rows += [("Thêm", _label(a.get("id", "")), "", a.get("text", "")) for a in impact.added]
    out += ["| Loại | Mục | Hiện tại | Điều chỉnh |", "|---|---|---|---|"]
    for kind, ac_id, before, after in rows[:MAX_LIST]:
        out.append(f"| {kind} | {_cell(ac_id)} | {_cell(before)} | {_cell(after)} |")
    if len(rows) > MAX_LIST:
        out.append(f"| | | {_more(len(rows))} | |")

    if cases:
        out += ["", f"**🧪 Test case cần rà lại ({len(cases)})**", "",
                "| Test case | Lý do |", "|---|---|"]
        for c in cases[:MAX_LIST]:
            out.append(f"| {_cell(c.get('title', ''))} | {_cell(c.get('reason', ''))} |")
        if len(cases) > MAX_LIST:
            out.append(f"| {_more(len(cases))} | |")
    return "\n".join(out)
