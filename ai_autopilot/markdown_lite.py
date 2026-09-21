"""A small, escape-first Markdown renderer.

Agent answers are written in Markdown and were shown as literal text: a security audit
arrived on screen as ``### 🔴 Critical`` and ``**Hardcoded AES fallback key**`` with the
backticks and asterisks still in it. That is not a cosmetic problem — the thing being
read is a list of findings ranked by severity, and the ranking was invisible.

No dependency for this. The alternative is pulling in a full Markdown library to render
text this repo generates itself, and the subset below (headings, emphasis, code, lists,
tables, quotes, links) is the entire vocabulary those answers use.

**Escape first, structure second.** Every byte is HTML-escaped before a single tag is
produced, so nothing in the source can introduce markup — reports quote real code, real
config and real customer data, and a report that renders a ``<script>`` from a file it
was auditing would be a vulnerability in the tool that reports vulnerabilities. Links
are the only place a source value reaches an attribute, and their scheme is allow-listed.
"""

from __future__ import annotations

import html
import re

__all__ = ["render"]

#: Only schemes that cannot execute. `javascript:` and `data:` are the two that turn a
#: link into script execution, so an href that is not clearly safe becomes plain text.
_SAFE_LINK = re.compile(r"^(?:https?://|mailto:|/|\#)[^\s<>\"']*$", re.IGNORECASE)

_FENCE = re.compile(r"^\s*(?:```|~~~)\s*([A-Za-z0-9_+-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s*(\d{1,3})[.)]\s+(.*)$")
# Escaping runs BEFORE structure is read, so a blockquote marker reaches this pattern
# as `&gt;`. Matching a bare `>` here silently turned every quoted line into an
# ordinary paragraph that begins with a stray entity.
_QUOTE = re.compile(r"^\s*&gt;\s?(.*)$")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")

# Inline, applied to ALREADY-ESCAPED text. Code first: what is inside a code span must
# not then be read as emphasis, which is exactly what `**kwargs` inside backticks would
# otherwise become.
_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")
_STRIKE = re.compile(r"~~([^~]+)~~")


def _inline(text: str) -> str:
    """Inline markup on one already-escaped line, code spans protected first."""
    spans: list[str] = []

    def stash(match: re.Match[str]) -> str:
        spans.append(f"<code>{match.group(1)}</code>")
        return f"\x00{len(spans) - 1}\x00"

    out = _CODE.sub(stash, text)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _ITALIC.sub(r"<em>\1</em>", out)
    out = _STRIKE.sub(r"<s>\1</s>", out)

    def link(match: re.Match[str]) -> str:
        label, href = match.group(1), html.unescape(match.group(2))
        if not _SAFE_LINK.match(href):
            return match.group(0)      # not obviously safe → leave it as visible text
        safe = html.escape(href, quote=True)
        rel = ' target="_blank" rel="noopener noreferrer"' if href.startswith("http") else ""
        return f'<a href="{safe}"{rel}>{label}</a>'

    out = _LINK.sub(link, out)
    for i, span in enumerate(spans):
        out = out.replace(f"\x00{i}\x00", span)
    return out


def _cells(line: str) -> list[str]:
    """One table row split on pipes, tolerating the optional outer pair."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [c.strip() for c in stripped.split("|")]


def render(text: str) -> str:
    """``text`` as HTML. Safe to mark ``|safe`` in a template — see the module note."""
    if not (text or "").strip():
        return ""
    lines = html.escape(text).replace("\r\n", "\n").split("\n")
    out: list[str] = []
    #: open list stack, so a bullet list inside the document closes at the right place
    list_tag = ""
    quoting = False
    quoted: list[str] = []      # lines of the blockquote currently open

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = ""

    def close_quote() -> None:
        nonlocal quoting
        if quoting:
            if quoted:
                out.append("<p>" + " ".join(_inline(q) for q in quoted) + "</p>")
                quoted.clear()
            out.append("</blockquote>")
            quoting = False

    i = 0
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            out.append("<p>" + "<br>".join(_inline(p) for p in paragraph) + "</p>")
            paragraph.clear()

    while i < len(lines):
        raw = lines[i]
        fence = _FENCE.match(raw)
        if fence:
            flush_paragraph(), close_list(), close_quote()
            lang = fence.group(1)
            body: list[str] = []
            i += 1
            while i < len(lines) and not _FENCE.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1                                   # consume the closing fence
            cls = f' class="lang-{lang}"' if lang else ""
            out.append(f"<pre><code{cls}>" + "\n".join(body) + "</code></pre>")
            continue

        if not raw.strip():
            flush_paragraph(), close_list(), close_quote()
            i += 1
            continue

        if _RULE.match(raw):
            flush_paragraph(), close_list(), close_quote()
            out.append("<hr>")
            i += 1
            continue

        heading = _HEADING.match(raw)
        if heading:
            flush_paragraph(), close_list(), close_quote()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            i += 1
            continue

        # A table needs its separator row to exist, or a line that merely contains a
        # pipe (a shell command, a regex alternation) would start one.
        if "|" in raw and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush_paragraph(), close_list(), close_quote()
            head = _cells(raw)
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            head_html = "".join(f"<th>{_inline(c)}</th>" for c in head)
            body_html = "".join(
                "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>"
                for r in rows
            )
            out.append(
                f"<table><thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table>"
            )
            continue

        quote = _QUOTE.match(raw)
        if quote:
            flush_paragraph(), close_list()
            if not quoting:
                out.append("<blockquote>")
                quoting = True
                quoted.clear()
            # Consecutive quoted lines are ONE paragraph. A `<p>` per line turned a
            # three-line "bottom line" into three widely spaced sentences that no
            # longer read as a single thought.
            quoted.append(quote.group(1))
            i += 1
            continue
        close_quote()

        bullet = _BULLET.match(raw)
        ordered = _ORDERED.match(raw)
        if bullet or ordered:
            flush_paragraph()
            want = "ul" if bullet else "ol"
            if list_tag != want:
                close_list()
                out.append(f"<{want}>")
                list_tag = want
            item = bullet.group(1) if bullet else ordered.group(2)
            out.append(f"<li>{_inline(item.strip())}</li>")
            i += 1
            continue

        # A wrapped continuation line belongs to the item above it. Treating it as a
        # new paragraph closed the list, so the next numbered item opened a FRESH
        # `<ol>` and restarted at 1 — an audit's "1., 2., 3." came out "1., 1., 1.".
        if list_tag and raw.startswith((" ", "\t")) and out and out[-1].endswith("</li>"):
            out[-1] = out[-1][: -len("</li>")] + " " + _inline(raw.strip()) + "</li>"
            i += 1
            continue
        close_list()

        paragraph.append(raw.strip())
        i += 1

    flush_paragraph(), close_list(), close_quote()
    return "\n".join(out)
