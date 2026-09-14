"""Audit reports: the prompt contract a report loop runs under, and what comes back.

A build loop's output is a diff — git already stores it, and a PR already renders it.
A report loop's output is prose, and prose had nowhere to go: ``executions.output`` is
``String(5000)``, which truncates the middle of a real audit, and no page read it back.
So a report is a first-class thing here — parsed into findings, counted by severity,
stored whole, and rendered.

The findings arrive as a fenced ``json`` block at the end of the agent's answer. That is
a contract with a model, so it is treated as advisory: :func:`parse_findings` never
raises and never discards the answer. A run whose JSON is missing, truncated or
malformed still produces a report — one with the full text and no structured findings —
because a report you can read beats an error that threw away what the agent found.

The HTML file is written HERE rather than by the agent. A report loop runs with the
file-mutating tools denied (that is what makes "it changes nothing" true rather than
merely asked for), so the agent cannot write its own file — and should not: two
renderers would drift, and the page would stop matching the attachment.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

# Ordered worst-first: this is the sort order, the display order, and the order the
# counts are reported in. "info" is last and is also where anything unrecognised lands.
SEVERITIES = ("critical", "high", "medium", "low", "info")

_SEVERITY_ALIASES = {
    "crit": "critical", "blocker": "critical", "sev1": "critical",
    "major": "high", "sev2": "high", "error": "high",
    "moderate": "medium", "medium": "medium", "warn": "medium", "warning": "medium",
    "minor": "low", "nit": "low", "suggestion": "low",
    "note": "info", "informational": "info", "ok": "info",
}

_JSON_BLOCK = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def normalise_severity(value: object) -> str:
    """Map whatever the agent wrote to one of :data:`SEVERITIES`.

    Models are consistent about *meaning* and inconsistent about *spelling* — "Critical",
    "BLOCKER" and "sev1" are the same finding. Unknown values become ``info`` rather than
    being dropped: a finding filed under the wrong heading is still readable; a finding
    that vanished because its label was unexpected is not.
    """
    text = str(value or "").strip().lower()
    if text in SEVERITIES:
        return text
    return _SEVERITY_ALIASES.get(text, "info")


@dataclass
class Finding:
    """One thing the audit found."""

    severity: str = "info"
    title: str = ""
    file: str = ""
    line: int | None = None
    detail: str = ""
    agent: str = ""          # which sub-agent reported it, when the agent says

    def as_dict(self) -> dict:
        return {
            "severity": self.severity, "title": self.title, "file": self.file,
            "line": self.line, "detail": self.detail, "agent": self.agent,
        }


@dataclass
class Report:
    """A single run of a report loop."""

    loop: str = ""
    summary: str = ""
    body_md: str = ""                                  # the agent's answer, whole
    findings: list[Finding] = field(default_factory=list)
    status: str = "success"
    project: str = ""
    repo: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float = 0.0
    agents: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return severity_counts(self.findings)

    @property
    def worst(self) -> str:
        """Highest severity present, or "info" when nothing was found."""
        for sev in SEVERITIES:
            if any(f.severity == sev for f in self.findings):
                return sev
        return "info"


def severity_counts(findings: list[Finding]) -> dict[str, int]:
    """Count per severity, always with every key present.

    Every key, including the zeros: a dashboard column that disappears when the count is
    zero reads as "not checked" rather than "nothing found", and those are opposite
    answers for an audit.
    """
    counts = dict.fromkeys(SEVERITIES, 0)
    for finding in findings:
        counts[normalise_severity(finding.severity)] += 1
    return counts


def _coerce_line(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_findings(text: str) -> tuple[str, list[Finding]]:
    """``(summary, findings)`` from the agent's answer. Never raises.

    The LAST fenced json block wins: an agent explaining the format it is about to use,
    or quoting an example finding, writes one block before the real one. The real answer
    is the one it ends on.
    """
    blocks = _JSON_BLOCK.findall(text or "")
    for raw in reversed(blocks):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        rows = data.get("findings")
        if not isinstance(rows, list):
            continue
        findings = [
            Finding(
                severity=normalise_severity(row.get("severity")),
                title=str(row.get("title") or "").strip(),
                file=str(row.get("file") or "").strip(),
                line=_coerce_line(row.get("line")),
                detail=str(row.get("detail") or "").strip(),
                agent=str(row.get("agent") or "").strip(),
            )
            for row in rows if isinstance(row, dict)
        ]
        findings.sort(key=lambda f: SEVERITIES.index(f.severity))
        return str(data.get("summary") or "").strip(), findings
    return "", []


def strip_findings_block(text: str) -> str:
    """The answer with its trailing machine-readable block removed.

    The page renders the findings as a table; leaving the raw JSON underneath shows the
    reader the same content twice, once in a form meant for the parser.
    """
    blocks = list(_JSON_BLOCK.finditer(text or ""))
    if not blocks:
        return (text or "").strip()
    last = blocks[-1]
    return ((text[: last.start()] + text[last.end():]).strip()) if text else ""


# ── the prompt side of the contract ─────────────────────────────────────────

_CONTRACT = """
Finish your answer with a single fenced ```json block, and nothing after it:

```json
{"summary": "one sentence a reader can act on",
 "findings": [{"severity": "critical|high|medium|low|info",
               "title": "short, specific",
               "file": "repo/relative/path.ext", "line": 42,
               "detail": "what is wrong and what it causes",
               "agent": "which sub-agent found it"}]}
```

Report only what you can point at in the code — file and line. An empty `findings` list
is a real answer and a good one; do not pad it. Everything above the block is written
for a person and will be shown as the body of the report.
""".strip()


def agents_block(agents: list[str]) -> str:
    """The instruction naming the sub-agents this loop is built on, or "" for none."""
    names = [a.strip() for a in (agents or []) if a and a.strip()]
    if not names:
        return ""
    listed = ", ".join(f"`{n}`" for n in names)
    return (
        f"Delegate the work to these sub-agents, one per area, and run them in "
        f"parallel where they do not depend on each other: {listed}. Attribute every "
        f"finding to the sub-agent that produced it. Investigate with them rather than "
        f"reviewing everything yourself — that is what they are for."
    )


def audit_prompt(prompt: str, agents: list[str], repo: str = "") -> str:
    """Assemble a report loop's prompt: the operator's ask, its agents, the contract.

    Assembled here, not stored, so a loop written before the contract existed (or edited
    on the page by someone who does not know it) still returns parseable findings.
    """
    parts = [(prompt or "").strip()]
    if repo:
        parts.append(f"Repository under audit: {repo}")
    block = agents_block(agents)
    if block:
        parts.append(block)
    parts.append(
        "This is a READ-ONLY audit: report what you find, change no files. The "
        "file-editing tools are disabled, so do not plan around using them."
    )
    parts.append(_CONTRACT)
    return "\n\n".join(p for p in parts if p)


# ── rendering ───────────────────────────────────────────────────────────────

_SEVERITY_COLOR = {
    "critical": ("#7f1d1d", "#fecaca"), "high": ("#7c2d12", "#fed7aa"),
    "medium": ("#78350f", "#fde68a"), "low": ("#1e3a5f", "#bfdbfe"),
    "info": ("#334155", "#e2e8f0"),
}


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def render_html(report: Report) -> str:
    """A self-contained HTML report — no CDN, no external font, opens offline.

    Self-contained because this file's whole job is to be forwarded: attached to a mail,
    dropped in a chat, opened on a machine that has never heard of this dashboard. A
    stylesheet fetched from anywhere would make it render as unstyled text there.
    """
    counts = report.counts
    when = (report.finished_at or report.started_at or datetime.now()).strftime(
        "%Y-%m-%d %H:%M"
    )
    chips = "".join(
        f'<span class="chip {sev}">{_esc(sev)} {counts[sev]}</span>'
        for sev in SEVERITIES if counts[sev]
    ) or '<span class="chip none">no findings</span>'

    rows = "".join(
        f'<tr class="sev-{_esc(f.severity)}">'
        f'<td><span class="chip {_esc(f.severity)}">{_esc(f.severity)}</span></td>'
        f"<td><b>{_esc(f.title)}</b>"
        + (f'<div class="detail">{_esc(f.detail)}</div>' if f.detail else "")
        + "</td>"
        f'<td class="mono">{_esc(f.file)}'
        + (f":{f.line}" if f.line else "")
        + "</td>"
        f"<td>{_esc(f.agent)}</td></tr>"
        for f in report.findings
    )
    table = (
        "<table><thead><tr><th>Severity</th><th>Finding</th><th>Where</th>"
        f"<th>Agent</th></tr></thead><tbody>{rows}</tbody></table>"
        if report.findings else
        '<p class="empty">No findings were reported for this run.</p>'
    )

    sev_css = "".join(
        f".chip.{sev}{{background:{bg};color:{fg}}}"
        for sev, (fg, bg) in _SEVERITY_COLOR.items()
    )
    body = _esc(strip_findings_block(report.body_md)) or "—"
    agents = ", ".join(report.agents) or "—"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(report.loop)} — audit report {_esc(when)}</title>
<style>
:root{{color-scheme:light dark;--bg:#f8fafc;--card:#fff;--text:#0f172a;
--muted:#64748b;--line:#e2e8f0}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f172a;--card:#1e293b;
--text:#e2e8f0;--muted:#94a3b8;--line:#334155}}}}
*{{box-sizing:border-box}}
body{{margin:0;padding:24px;background:var(--bg);color:var(--text);
font:14px/1.6 ui-sans-serif,system-ui,'Segoe UI',sans-serif}}
.wrap{{max-width:1040px;margin:0 auto}}
h1{{font-size:20px;margin:0 0 4px}}
.meta{{color:var(--muted);font-size:13px;margin-bottom:16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px;margin-bottom:16px}}
.chip{{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;
font-weight:600;margin-right:6px}}
.chip.none{{background:#dcfce7;color:#166534}}{sev_css}
table{{width:100%;border-collapse:collapse}}
th,td{{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}}
.mono{{font-family:ui-monospace,'Cascadia Code',Consolas,monospace;font-size:12.5px;
word-break:break-all}}
.detail{{color:var(--muted);font-size:13px;margin-top:3px}}
.empty{{color:var(--muted);margin:0}}
pre{{white-space:pre-wrap;word-wrap:break-word;margin:0;font-family:inherit}}
.scroll{{overflow-x:auto}}
</style></head><body><div class="wrap">
<h1>{_esc(report.loop)}</h1>
<div class="meta">{_esc(when)} · {report.duration_seconds:.0f}s · status
{_esc(report.status)}{f" · {_esc(report.project)}" if report.project else ""}
 · agents: {_esc(agents)}</div>
<div class="card">{chips}
{f"<p>{_esc(report.summary)}</p>" if report.summary else ""}</div>
<div class="card scroll">{table}</div>
<div class="card"><pre>{body}</pre></div>
</div></body></html>"""
