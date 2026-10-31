"""Acceptance-criteria traceability: which criteria are tested, which fail, which cases float.

A spec lists acceptance criteria; QC writes free-form test cases. Nothing ties the two
together, so "is the spec actually verified?" is answered by feel at UAT. This module
answers it from data: every AC with at least one linked case is *covered*, one whose
linked case failed (or is blocked) is *failing*, one with nothing is *uncovered* — and a
case that names no known AC is reported as *unlinked*, because a test nobody can trace
back to the spec proves nothing about the spec.

Linking is deliberately forgiving about spelling (``ac-1``, ``AC 01``, ``AC_001`` are the
same criterion) and strict about boundaries (``AC-1`` must never match inside ``AC-12``),
since a false link hides a real gap and a missed link cries wolf.

Pure functions, no I/O, like :mod:`ai_autopilot.risk`: the caller supplies criteria and
cases, this module only classifies and renders. The result feeds the spec-conformance
gate score and the BA dashboard.
"""

from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass, field

from ai_autopilot import spec_readiness

COVERED, UNCOVERED, FAILING = "covered", "uncovered", "failing"

# Outcomes that make a criterion "failing": a case failed, or could not even run.
_BAD_OUTCOMES = frozenset({"fail", "blocked"})

STATUS_LABELS: dict[str, str] = {
    COVERED: "✅ Đã có test",
    UNCOVERED: "⬜ Chưa có test",
    FAILING: "❌ Test lỗi",
}

# An AC id as a whole token: not glued to a preceding word char, digits fully consumed.
_AC_TOKEN = re.compile(r"(?<!\w)AC[-_ ]?(\d+)(?!\w)", re.IGNORECASE)
_AC_FULL = re.compile(r"^\s*AC[-_ ]?(\d+)\s*$", re.IGNORECASE)
_FIELD_SPLIT = re.compile(r"[,;/\n]+")


@dataclass
class AcRow:
    ac_id: str
    text: str
    cases: list[str] = field(default_factory=list)
    status: str = UNCOVERED


@dataclass
class Coverage:
    rows: list[AcRow] = field(default_factory=list)
    unlinked_cases: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def covered(self) -> int:
        """ACs that have at least one case (covered OR failing)."""
        return sum(1 for r in self.rows if r.status != UNCOVERED)

    @property
    def failing(self) -> int:
        return sum(1 for r in self.rows if r.status == FAILING)

    @property
    def uncovered(self) -> int:
        return sum(1 for r in self.rows if r.status == UNCOVERED)

    @property
    def pct(self) -> float | None:
        """Share of ACs with at least one case, 0..100; None when there are no ACs."""
        if not self.rows:
            return None
        return 100.0 * self.covered / self.total


def _fmt(num: str) -> str:
    return f"AC-{int(num):02d}"


def normalise_id(raw: str) -> str:
    """One spelling for an AC id: ``ac-1`` / ``AC 01`` / ``AC_001`` → ``AC-01``.

    Anything that is not an AC id is returned stripped and upper-cased, so other id
    schemes still compare consistently.
    """
    s = str(raw or "").strip()
    m = _AC_FULL.match(s)
    if m:
        return _fmt(m.group(1))
    return s.upper()


def link_ids(case: dict[str, str]) -> list[str]:
    """AC ids a case refers to: its ``ac`` field plus AC tokens in its ``title``.

    Normalised, de-duplicated, order preserved. A bare number in the ``ac`` field
    (``"3"``) is read as ``AC-03``.
    """
    out: list[str] = []

    def add(i: str) -> None:
        if i not in out:
            out.append(i)

    field_val = str(case.get("ac") or "")
    for part in _FIELD_SPLIT.split(field_val):
        found = _AC_TOKEN.findall(part)
        if found:
            for num in found:
                add(_fmt(num))
        elif part.strip().isdigit():
            add(_fmt(part.strip()))
    for num in _AC_TOKEN.findall(str(case.get("title") or "")):
        add(_fmt(num))
    return out


def acs_from_spec(text: str) -> list[dict[str, str]]:
    """Convenience: the AC list of a spec document, in the shape :func:`coverage` takes."""
    return [{"id": a.id, "text": a.text} for a in spec_readiness.extract_acs(text)]


def coverage(acs: list[dict[str, str]], cases: list[dict[str, str]]) -> Coverage:
    """Link cases to criteria and classify each criterion.

    failing if any linked case failed/blocked; else covered if any case is linked; else
    uncovered. A case is unlinked when it names no AC, or only ids absent from ``acs``.
    """
    known = {normalise_id(a.get("id", "")) for a in acs if (a.get("id") or "").strip()}
    by_ac: dict[str, list[dict[str, str]]] = {}
    unlinked: list[str] = []
    for c in cases:
        ids = [i for i in link_ids(c) if i in known]
        if not ids:
            unlinked.append(str(c.get("title") or ""))
            continue
        for i in ids:
            by_ac.setdefault(i, []).append(c)

    rows: list[AcRow] = []
    for a in acs:
        raw_id = (a.get("id") or "").strip()
        ac_id = normalise_id(raw_id) if raw_id else ""
        linked = by_ac.get(ac_id, []) if ac_id else []
        if any(str(c.get("outcome") or "").strip().lower() in _BAD_OUTCOMES for c in linked):
            status = FAILING
        elif linked:
            status = COVERED
        else:
            status = UNCOVERED
        rows.append(AcRow(ac_id, str(a.get("text") or ""),
                          [str(c.get("title") or "") for c in linked], status))
    return Coverage(rows, unlinked)


def conformance(cov: Coverage) -> float | None:
    """0..1 spec conformance for gating: covered, non-failing ACs over all ACs."""
    if cov.total == 0:
        return None
    return (cov.covered - cov.failing) / cov.total


def _summary(cov: Coverage) -> str:
    return f"{cov.covered}/{cov.total} AC có test case, {cov.failing} lỗi"


def _md_cell(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_markdown(cov: Coverage, title: str = "") -> str:
    """Heading, summary line, AC table and the list of unlinked cases."""
    lines = [f"### {_md_cell(title) if title else 'Độ phủ tiêu chí chấp nhận'}", "",
             f"**{_summary(cov)}**", "",
             "| AC | Trạng thái | Test case |", "|---|---|---|"]
    for r in cov.rows:
        label = _md_cell(r.ac_id or "(không có mã)")
        if r.text:
            label += " — " + _md_cell(r.text)
        cases = "; ".join(_md_cell(c) for c in r.cases) or "—"
        lines.append(f"| {label} | {STATUS_LABELS[r.status]} | {cases} |")
    if cov.unlinked_cases:
        lines += ["", "**Test case chưa gắn AC nào:**", ""]
        lines += [f"- {_md_cell(t)}" for t in cov.unlinked_cases]
    return "\n".join(lines) + "\n"


def render_html(cov: Coverage, title: str = "") -> str:
    """Small ``<table>`` fragment; every user-supplied string is HTML-escaped."""
    e = html_mod.escape
    parts = []
    if title:
        parts.append(f"<h3>{e(title)}</h3>")
    parts.append(f"<p><strong>{e(_summary(cov))}</strong></p>")
    parts.append("<table><thead><tr><th>AC</th><th>Trạng thái</th><th>Test case</th></tr>"
                 "</thead><tbody>")
    for r in cov.rows:
        label = e(r.ac_id or "(không có mã)")
        if r.text:
            label += " — " + e(r.text)
        cases = "; ".join(e(c) for c in r.cases) or "—"
        parts.append(f'<tr class="{r.status}"><td>{label}</td>'
                     f"<td>{e(STATUS_LABELS[r.status])}</td><td>{cases}</td></tr>")
    parts.append("</tbody></table>")
    if cov.unlinked_cases:
        parts.append("<p>Test case chưa gắn AC nào:</p><ul>"
                     + "".join(f"<li>{e(t)}</li>" for t in cov.unlinked_cases) + "</ul>")
    return "".join(parts)
