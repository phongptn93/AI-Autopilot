"""File security findings on Azure DevOps as Bugs — idempotently, per fingerprint.

A finding becomes at most ONE Bug: the row remembers ``ado_bug_id`` and a second scan
that sees the same fingerprint comments on that Bug rather than opening another. When
a full scan stops reporting it, the Bug gets a "no longer detected" comment; closing
it is a person's call (the scanner cannot know the fix is right).

Gated on the autopilot's autonomy level the same way a PR is: ``report`` never writes
to the tracker. Comments use Markdown (Pascal case ``format`` — the MCP/REST enum).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape

from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.reports import SEVERITIES

_log = get_logger("security_scan.ado_sync")

_SEV_ADO = {
    "critical": "1 - Critical", "high": "2 - High", "medium": "3 - Medium", "low": "4 - Low",
}


@dataclass
class SyncOutcome:
    filed: list[tuple[int, str]] = field(default_factory=list)   # (bug id, fingerprint)
    commented: int = 0
    skipped: str = ""
    failed: int = 0


def _floor(name: str) -> int:
    name = (name or "high").lower()
    return SEVERITIES.index(name) if name in SEVERITIES else SEVERITIES.index("high")


def bug_title(row) -> str:
    cwe = f"[{row.cwe}]" if row.cwe else ""
    return f"[Security][{row.severity}]{cwe} {row.title}"[:250]


def bug_body(row, repo_name: str, scan_id: int = 0) -> str:
    where = f"{row.file}:{row.line}" if row.line else row.file
    parts = [
        f"<b>Severity:</b> {escape(row.severity)} &nbsp; <b>Confidence:</b> "
        f"{escape(row.confidence or '?')} &nbsp; <b>Tool:</b> {escape(row.tool or 'ai')}",
        f"<b>Where:</b> <code>{escape(repo_name)}/{escape(where)}</code>",
    ]
    if row.cwe or row.owasp:
        parts.append(f"<b>Classification:</b> {escape(row.cwe)} {escape(row.owasp)}")
    if row.detail:
        parts.append(escape(row.detail))
    if row.snippet:
        parts.append(f"<pre>{escape(row.snippet)}</pre>")
    parts.append(f"<b>Fingerprint:</b> <code>{escape(row.fingerprint)}</code>"
                 + (f" · scan #{scan_id}" if scan_id else ""))
    parts.append("Filed by AI Autopilot security scan. Suppress with a reason in "
                 "<code>.autopilot/security-suppressions.yaml</code> if accepted.")
    return "<br/>".join(parts)


async def file_new_findings(
    *, ado, security_repo, config, repo: str, project: str, scan_id: int,
    fingerprints: list[str], repo_name: str = "",
) -> SyncOutcome:
    """Create a Bug for each NEW finding at/above ``security_scan.file_bugs_from``."""
    out = SyncOutcome()
    sec = config.security_scan
    if not sec.file_bugs:
        out.skipped = "file_bugs off"
        return out
    if getattr(config, "autonomy_level", "assisted") == "report":
        out.skipped = "autonomy_level is report"
        return out
    floor = _floor(sec.file_bugs_from)
    tag = ", ".join(t for t in ("security", "autopilot-security") if t)
    for fp in fingerprints:
        row = await security_repo.by_fingerprint(repo, fp)
        if row is None or row.ado_bug_id or row.status != "open":
            continue
        if SEVERITIES.index(row.severity) > floor:
            continue
        try:
            bug_id = await ado.create_work_item(
                title=bug_title(row), item_type="Bug", parent_id=None, tag=tag,
                description=bug_body(row, repo_name or repo, scan_id), project=project,
            )
        except Exception as exc:  # noqa: BLE001 — one failure must not stop the rest
            _log.warning("security bug not filed", fp=fp, error=describe_exc(exc))
            bug_id = 0
        if bug_id:
            await security_repo.set_bug(row.id, int(bug_id))
            out.filed.append((int(bug_id), fp))
        else:
            out.failed += 1
    return out


async def comment_fixed(*, ado, security_repo, repo: str, fingerprints: list[str]) -> int:
    """Tell each filed Bug its finding is no longer detected. Returns how many."""
    done = 0
    for fp in fingerprints:
        row = await security_repo.by_fingerprint(repo, fp)
        if row is None or not row.ado_bug_id:
            continue
        try:
            ok = await ado.add_comment(
                int(row.ado_bug_id),
                "🔐 AI Autopilot: this finding is **no longer detected** by the latest "
                f"full scan (`{fp}`). Verify the fix before closing.",
            )
            done += int(bool(ok))
        except Exception as exc:  # noqa: BLE001
            _log.warning("fixed-comment failed", bug=row.ado_bug_id, error=describe_exc(exc))
    return done


def ado_severity(severity: str) -> str:
    return _SEV_ADO.get((severity or "").lower(), "3 - Medium")
