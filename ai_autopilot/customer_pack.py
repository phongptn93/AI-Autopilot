"""Customer pack: the two pages a customer can actually read and sign.

A customer does not read a technical spec — FR tables, edge cases and data definitions are
written for the people building the product. To confirm a requirement quickly they need ONE
printable page in plain language: what the screen will do, the scenarios as "when… then…",
the assumptions we need them to confirm, and a signature line. At the end of delivery they
need the mirror image: an ACCEPTANCE RECORD listing every criterion with its result and
evidence, the deviations that were decided, and the exceptions accepted.

Why a module of its own: a signed page is only worth something if it says what the spec
says. Rendering both documents from the same data (never retyped by hand) keeps the paper
the customer signs identical to the document QC tests against.

Pure and dependency-free like :mod:`ai_autopilot.risk`: text/data in, document out. No I/O,
no network, no clock (the caller passes ``generated``). Storing the customer's confirmation
is somebody else's job — this module only RENDERS.

Security: the HTML is opened in a browser and printed, and spec text is attacker-influenced
(anyone who can edit a work item). Every piece of spec or data text goes through
:func:`html.escape`; the document carries no script, no event handler and no external
resource. The only URL is the optional mockup link, accepted solely for ``http://``,
``https://`` and site-relative ``/`` forms — anything else (``javascript:``) is shown as
inert, escaped text.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ai_autopilot import spec_readiness

DEFAULT_TITLE = "Yêu cầu"

RESULT_LABELS: dict[str, str] = {
    "pass": "Đạt",
    "fail": "Không đạt",
    "blocked": "Bị chặn",
    "": "Chưa kiểm",
}
DECISION_LABELS: dict[str, str] = {
    "update_spec": "Cập nhật spec",
    "fix_code": "Sửa code",
    "accept": "Chấp nhận",
    "": "Chưa quyết",
}

VERDICT_READY = "ĐỦ ĐIỀU KIỆN NGHIỆM THU"
VERDICT_NOT_READY = "CHƯA ĐỦ ĐIỀU KIỆN"
NO_AC_WARNING = "Chưa có tiêu chí chấp nhận — không thể xác nhận"

_H_INTENT = re.compile(
    r"ý định|mục tiêu|tổng quan|mô tả|overview|summary|business|bối cảnh|goal|context", re.I)
_H_ASSUME = re.compile(r"câu hỏi|giả định|assumption|open question|chưa chốt", re.I)
_H_OUT = re.compile(r"ngoài phạm vi|out of scope|không làm", re.I)
_H_SEC = re.compile(r"bảo mật|phân quyền|security|permission", re.I)
_H_AC = re.compile(r"acceptance|tiêu chí|(?<!\w)ac(?!\w)", re.I)

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_BOLD_HEADING = re.compile(r"^\s*\*\*([^*]{3,80})\*\*\s*:?\s*$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.*\S)\s*$")
_UNCHECKED = re.compile(r"^\s*[-*+]\s+\[ \]\s+(.*\S)")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_AC_PREFIX = re.compile(r"^\W{0,4}AC[-_ ]?\d+(?!\w)[\s:.)\-–—]*", re.I)

_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


@dataclass
class PackContent:
    """What the customer page says, already in plain text (no markdown, not yet escaped)."""

    title: str
    intent: list[str] = field(default_factory=list)
    scenarios: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    security_notes: list[str] = field(default_factory=list)


# ── spec -> plain content ────────────────────────────────────────────────────────────


def _clean(text: str) -> str:
    """Markdown emphasis and link syntax out; whitespace collapsed. Keeps the words."""
    t = _IMAGE.sub(r"\1", text or "")
    t = _LINK.sub(r"\1", t)
    t = t.replace("**", "").replace("__", "").replace("`", "")
    return re.sub(r"\s+", " ", t).strip()


def _sections(markdown: str) -> tuple[str, list[tuple[str, list[str]]]]:
    """Split into (first H1, [(heading, body lines)]); lines before any heading get ''.
    Fenced code is dropped — it is never customer-facing prose."""
    h1 = ""
    sections: list[tuple[str, list[str]]] = [("", [])]
    in_code = False
    for raw in (markdown or "").splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = _HEADING.match(line)
        if m:
            heading = _clean(m.group(2))
            if len(m.group(1)) == 1 and not h1:
                h1 = heading
            sections.append((heading, []))
            continue
        mb = _BOLD_HEADING.match(line)
        if mb:
            sections.append((_clean(mb.group(1)), []))
            continue
        sections[-1][1].append(line)
    return h1, sections


def _paragraphs(lines: Sequence[str]) -> list[str]:
    """Blank-line separated paragraphs; each list item is its own paragraph; tables skipped."""
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            text = _clean(" ".join(buf))
            if text:
                out.append(text)
            buf.clear()

    for line in lines:
        if not line.strip() or _TABLE_ROW.match(line):
            flush()
        elif (li := _LIST_ITEM.match(line)):
            flush()
            text = _clean(li.group(1))
            if text:
                out.append(text)
        else:
            buf.append(line.strip())
    flush()
    return out


def _list_items(lines: Sequence[str]) -> list[str]:
    items: list[str] = []
    for line in lines:
        li = _LIST_ITEM.match(line)
        if li:
            text = _clean(li.group(1))
            if text:
                items.append(text)
    return items


def _dedupe(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _scenario_lines(markdown: str) -> list[str]:
    out: list[str] = []
    for ac in spec_readiness.extract_acs(markdown or ""):
        body = _clean(_AC_PREFIX.sub("", ac.text or ""))
        if not body:
            continue
        out.append(f"{ac.id}: {body}" if ac.id else body)
    return out


def summarise_spec(markdown: str, *, title: str = "") -> PackContent:
    """Boil a technical spec down to what a customer needs to confirm. Never raises: odd or
    empty input yields an (almost) empty :class:`PackContent` — the renderer then shows the
    "no acceptance criteria" warning instead of a blank page."""
    try:
        h1, sections = _sections(markdown or "")
    except Exception:  # pragma: no cover - defensive; the helpers are total
        return PackContent(title=_clean(title) or DEFAULT_TITLE)

    intent: list[str] = []
    for heading, lines in sections:
        if heading and _H_INTENT.search(heading):
            intent = _paragraphs(lines)
            if intent:
                break
    if not intent:
        for heading, lines in sections:
            if heading:
                continue
            first = _paragraphs(lines)
            if first:
                intent = first[:1]
                break
        if not intent:
            for _heading, lines in sections:
                first = _paragraphs(lines)
                if first:
                    intent = first[:1]
                    break

    assumptions: list[str] = []
    out_of_scope: list[str] = []
    security: list[str] = []
    for heading, lines in sections:
        if _H_ASSUME.search(heading):
            assumptions.extend(_list_items(lines))
        if _H_OUT.search(heading):
            out_of_scope.extend(_list_items(lines))
        if _H_SEC.search(heading):
            security.extend(_list_items(lines))
        if _H_AC.search(heading):
            continue  # an unchecked box under "Acceptance" is a criterion, not a question
        for line in lines:
            um = _UNCHECKED.match(line)
            if um:
                text = _clean(um.group(1))
                if text:
                    assumptions.append(text)

    return PackContent(
        title=_clean(title) or h1 or DEFAULT_TITLE,
        intent=intent,
        scenarios=_scenario_lines(markdown or ""),
        assumptions=_dedupe(assumptions),
        out_of_scope=_dedupe(out_of_scope),
        security_notes=_dedupe(security),
    )


# ── acceptance readiness ─────────────────────────────────────────────────────────────


def _val(row: Mapping[str, str], key: str) -> str:
    v = row.get(key)
    return str(v).strip() if v is not None else ""


def acceptance_ready(
    acs: Sequence[Mapping[str, str]], drifts: Sequence[Mapping[str, str]] = ()
) -> tuple[bool, list[str]]:
    """(ready, reasons). Ready only when there is at least one criterion, every one passed,
    and no deviation is still undecided. The reasons are customer-readable sentences, shared
    by both renderers so the HTML and the Markdown can never disagree on the verdict."""
    reasons: list[str] = []
    if not acs:
        reasons.append("Không có tiêu chí nào")
    else:
        not_pass = [
            _val(a, "id") or f"#{i}" for i, a in enumerate(acs, 1) if _val(a, "result") != "pass"
        ]
        if not_pass:
            reasons.append(f"{len(not_pass)} tiêu chí chưa đạt: " + ", ".join(not_pass))
    undecided = sum(1 for d in drifts if not _val(d, "decision"))
    if undecided:
        reasons.append(f"{undecided} điểm lệch spec chưa quyết")
    return (not reasons, reasons)


def _result_label(row: Mapping[str, str]) -> str:
    r = _val(row, "result")
    return RESULT_LABELS.get(r, r)


def _decision_label(row: Mapping[str, str]) -> str:
    d = _val(row, "decision")
    return DECISION_LABELS.get(d, d)


# ── HTML ─────────────────────────────────────────────────────────────────────────────

_CSS = """
@page { size: A4; margin: 18mm 16mm; }
* { box-sizing: border-box; }
body { font: 14px/1.55 "Segoe UI", Arial, sans-serif; color: #1a1a1a; background: #fff;
  max-width: 190mm; margin: 0 auto; padding: 16px; }
h1 { font-size: 22px; margin: 0 0 6px; }
h2 { font-size: 16px; margin: 22px 0 8px; padding-bottom: 4px; border-bottom: 2px solid #333; }
.meta { color: #555; font-size: 12px; margin-bottom: 6px; }
ol, ul { margin: 6px 0; padding-left: 22px; }
li { margin: 4px 0; }
.box { border: 1px solid #999; border-radius: 4px; padding: 10px 12px; margin: 10px 0; }
.warn { border-color: #b00020; background: #fdecee; color: #7a0014; font-weight: 600; }
.ok { border-color: #1b6e2d; background: #e8f5ea; color: #14501f; font-weight: 600; }
.assume li { list-style: none; margin-left: -22px; }
table { border-collapse: collapse; width: 100%; margin: 8px 0; }
th, td { border: 1px solid #888; padding: 5px 8px; text-align: left; vertical-align: top; }
th { background: #eee; }
.pass { color: #14501f; font-weight: 600; }
.fail, .blocked { color: #7a0014; font-weight: 600; }
.sign { margin-top: 26px; page-break-inside: avoid; }
.sign p { margin: 14px 0; }
.sign .line { display: inline-block; min-width: 150px; border-bottom: 1px solid #333; }
@media print { body { padding: 0; } a { color: inherit; text-decoration: none; } }
"""


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _safe_url(url: str) -> bool:
    u = (url or "").strip()
    return u.startswith(("http://", "https://")) or (u.startswith("/") and not u.startswith("//"))


def _doc(title: str, body: str) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="vi">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(title)}</title>\n<style>{_CSS}</style>\n</head>\n"
        f"<body>\n{body}\n</body>\n</html>\n"
    )


def _meta_line(parts: Sequence[str]) -> str:
    shown = [p for p in parts if p]
    return f'<div class="meta">{" · ".join(shown)}</div>' if shown else ""


def _list(tag: str, items: Sequence[str], cls: str = "", prefix: str = "") -> str:
    attr = f' class="{cls}"' if cls else ""
    rows = "".join(f"<li>{prefix}{_e(i)}</li>" for i in items)
    return f"<{tag}{attr}>{rows}</{tag}>"


def _signature(roles: Sequence[str]) -> str:
    blocks = []
    for role in roles:
        blocks.append(
            '<div class="sign">'
            f"<strong>{_e(role)}</strong>"
            '<p>Người xác nhận: <span class="line"></span> '
            'Chức danh: <span class="line"></span> '
            'Ngày: <span class="line"></span> '
            'Chữ ký: <span class="line"></span></p></div>'
        )
    return "".join(blocks)


def render_confirmation_html(
    content: PackContent,
    *,
    item_id: int = 0,
    spec_path: str = "",
    mockup_url: str = "",
    generated: str = "",
    version: str = "",
) -> str:
    """The one-page, printable "please confirm this" document for the customer."""
    meta = _meta_line([
        f"Work item #{_e(item_id)}" if item_id else "",
        _e(spec_path),
        f"Phiên bản {_e(version)}" if version else "",
        f"Lập ngày {_e(generated)}" if generated else "",
    ])
    out = [f"<h1>{_e(content.title)}</h1>", meta]

    out.append("<h2>Chúng tôi sẽ làm gì</h2>")
    if content.intent:
        out.extend(f"<p>{_e(p)}</p>" for p in content.intent)
    else:
        out.append("<p>(Chưa có mô tả ý định.)</p>")

    if mockup_url:
        out.append("<h2>Bạn sẽ thấy gì</h2>")
        if _safe_url(mockup_url):
            u = _e(mockup_url.strip())
            out.append(f'<p>Bản minh hoạ giao diện: <a href="{u}">{u}</a></p>')
        else:
            out.append(f"<p>Bản minh hoạ giao diện: {_e(mockup_url)}</p>")

    out.append("<h2>Kịch bản kiểm tra</h2>")
    if content.scenarios:
        out.append(_list("ol", content.scenarios))
    else:
        out.append(f'<div class="box warn">{_e(NO_AC_WARNING)}</div>')

    out.append("<h2>Cần bạn xác nhận</h2>")
    if content.assumptions:
        out.append(_list("ul", content.assumptions, "assume", "☐ "))
    else:
        out.append("<p>Không có điểm nào cần xác nhận thêm.</p>")

    if content.out_of_scope:
        out.append("<h2>Ngoài phạm vi</h2>")
        out.append(_list("ul", content.out_of_scope))
    if content.security_notes:
        out.append("<h2>Bảo mật &amp; phân quyền</h2>")
        out.append(_list("ul", content.security_notes))

    out.append("<h2>Xác nhận</h2>")
    out.append(
        "<p>Bằng việc ký tên dưới đây, tôi xác nhận các kịch bản nêu trên mô tả đúng "
        "những gì tôi cần.</p>"
    )
    out.append(_signature(["Khách hàng"]))
    return _doc(content.title, "\n".join(o for o in out if o))


def _acceptance_header(
    title: str, item_id: int, build: str, generated: str
) -> list[str]:
    meta = _meta_line([
        f"Work item #{_e(item_id)}" if item_id else "",
        f"Bản build {_e(build)}" if build else "",
        f"Lập ngày {_e(generated)}" if generated else "",
    ])
    return [f"<h1>Biên bản nghiệm thu — {_e(title)}</h1>", meta]


def render_acceptance_html(
    title: str,
    acs: Sequence[Mapping[str, str]],
    *,
    drifts: Sequence[Mapping[str, str]] = (),
    exceptions: Sequence[str] = (),
    item_id: int = 0,
    build: str = "",
    generated: str = "",
) -> str:
    """The end-of-delivery acceptance record, with a verdict computed by
    :func:`acceptance_ready` — never typed by hand."""
    ready, reasons = acceptance_ready(acs, drifts)
    passed = sum(1 for a in acs if _val(a, "result") == "pass")
    out = _acceptance_header(title, item_id, build, generated)
    out.append(f"<p><strong>{passed}/{len(acs)} tiêu chí đạt</strong></p>")

    out.append("<h2>Tiêu chí chấp nhận</h2>")
    if acs:
        rows = "".join(
            "<tr>"
            f"<td>{_e(_val(a, 'id'))} {_e(_val(a, 'text'))}</td>"
            f'<td class="{_e(_val(a, "result") or "none")}">{_e(_result_label(a))}</td>'
            f"<td>{_e(_val(a, 'evidence'))}</td></tr>"
            for a in acs
        )
        out.append(f"<table><tr><th>AC</th><th>Kết quả</th><th>Bằng chứng</th></tr>{rows}</table>")
    else:
        out.append("<p>Không có tiêu chí nào.</p>")

    out.append("<h2>Điểm lệch so với spec</h2>")
    if drifts:
        rows = "".join(
            f"<tr><td>{_e(_val(d, 'summary'))}</td><td>{_e(_decision_label(d))}</td></tr>"
            for d in drifts
        )
        out.append(f"<table><tr><th>Điểm lệch</th><th>Quyết định</th></tr>{rows}</table>")
    else:
        out.append("<p>Không có điểm lệch.</p>")

    out.append("<h2>Ngoại lệ được chấp nhận</h2>")
    out.append(_list("ul", list(exceptions)) if exceptions else "<p>Không có.</p>")

    if ready:
        out.append(f'<div class="box ok">{_e(VERDICT_READY)}</div>')
    else:
        out.append(
            f'<div class="box warn">{_e(VERDICT_NOT_READY)}'
            f"{_list('ul', reasons)}</div>"
        )

    out.append("<h2>Chữ ký</h2>")
    out.append(_signature(["BA", "Khách hàng / Chủ sở hữu"]))
    return _doc(f"Biên bản nghiệm thu — {title}", "\n".join(o for o in out if o))


# ── Markdown ─────────────────────────────────────────────────────────────────────────


def _md(value: object) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().replace("|", "\\|")


def render_acceptance_markdown(
    title: str,
    acs: Sequence[Mapping[str, str]],
    *,
    drifts: Sequence[Mapping[str, str]] = (),
    exceptions: Sequence[str] = (),
    item_id: int = 0,
    build: str = "",
    generated: str = "",
) -> str:
    """Same record as :func:`render_acceptance_html`, for a work-item comment or a wiki."""
    ready, reasons = acceptance_ready(acs, drifts)
    passed = sum(1 for a in acs if _val(a, "result") == "pass")
    lines = [f"# Biên bản nghiệm thu — {_md(title)}", ""]
    meta = [
        f"Work item #{item_id}" if item_id else "",
        f"Bản build {_md(build)}" if build else "",
        f"Lập ngày {_md(generated)}" if generated else "",
    ]
    meta_text = " · ".join(m for m in meta if m)
    if meta_text:
        lines += [meta_text, ""]
    lines += [f"**{passed}/{len(acs)} tiêu chí đạt**", "", "## Tiêu chí chấp nhận", ""]
    if acs:
        lines += ["| AC | Kết quả | Bằng chứng |", "|---|---|---|"]
        for a in acs:
            lines.append(
                f"| {_md(_val(a, 'id') + ' ' + _val(a, 'text'))} | {_md(_result_label(a))} "
                f"| {_md(_val(a, 'evidence'))} |"
            )
    else:
        lines.append("Không có tiêu chí nào.")
    lines += ["", "## Điểm lệch so với spec", ""]
    if drifts:
        lines += ["| Điểm lệch | Quyết định |", "|---|---|"]
        lines += [f"| {_md(_val(d, 'summary'))} | {_md(_decision_label(d))} |" for d in drifts]
    else:
        lines.append("Không có điểm lệch.")
    lines += ["", "## Ngoại lệ được chấp nhận", ""]
    lines += [f"- {_md(x)}" for x in exceptions] if exceptions else ["Không có."]
    lines.append("")
    if ready:
        lines.append(f"**{VERDICT_READY}**")
    else:
        lines.append(f"**{VERDICT_NOT_READY}**: " + "; ".join(reasons))
    return "\n".join(lines) + "\n"
