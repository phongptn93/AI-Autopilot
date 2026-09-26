"""Phase 3 — prove a finding is real by building a PoC for it, in throwaway isolation.

A scanner says "SQL built by concatenation here"; that is a hypothesis, not a fact. This
pass hands the model the finding and one instruction: write a test or a small script that
DEMONSTRATES the vulnerability — an xUnit test that reaches the endpoint without a token,
a request that returns another tenant's row — run it, and report whether it actually
reproduced. A confirmed finding gets ``confidence = high`` and keeps its PoC; one that
could not be reproduced stays open but is flagged "unconfirmed" so a human looks rather
than trusting either the scanner or the model alone.

Isolation is the whole point and is not negotiable: the PoC is written and run in a git
worktree created for this one finding (the executor's ``_acquire_agent_scratch``), and
that worktree is ALWAYS torn down afterwards — nothing is committed, pushed, or merged.
Verification requires worktrees to be enabled; without them it is skipped, because
"changes nothing" cannot be promised when the agent is editing the real checkout.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.reports import SEVERITIES, Finding

_log = get_logger("security_scan.verify")

_PROMPT = """\
You are verifying ONE security finding by building a minimal proof-of-concept in an
ISOLATED throwaway worktree. Nothing you write here is kept — do not try to "fix" the
code, only to PROVE or DISPROVE the finding.

Finding to verify:
- Severity: {severity}
- Title: {title}
- Where: {where}
- Detail: {detail}
- Rule: {rule_id} ({cwe} {owasp})

Steps:
1. Read the code at that location and the paths it depends on.
2. Write the SMALLEST possible proof: a unit/integration test, a script, or a curl-style
   call that demonstrates the vulnerable behaviour actually happens (e.g. the endpoint
   returns data without an auth check; the query is injectable with a crafted input).
   Prefer the repo's own test framework so it runs with the existing toolchain.
3. Run it. Capture the exact command and its output.
4. Decide honestly: did it reproduce? A finding you could not trigger is "verified": false
   — say why (guarded upstream, input not reachable, already parameterised).

Keep every command's output small (`| head`). Do NOT modify application code, open a PR,
or push anything.

Finish with a single fenced ```json block and nothing after it:
```json
{{"verified": true, "confidence": "high",
  "evidence": "one paragraph: what you ran and what proved it",
  "poc_files": ["relative/path/to/poc_test.cs"],
  "command": "the command you ran",
  "notes": "caveats, or why it did not reproduce"}}
```
"""


@dataclass
class VerifyOutcome:
    attempted: int = 0
    confirmed: int = 0
    refuted: int = 0
    errored: int = 0
    skipped: str = ""
    results: list[dict] = field(default_factory=list)   # per finding, for logging


def _floor(name: str) -> int:
    name = (name or "high").lower()
    return SEVERITIES.index(name) if name in SEVERITIES else SEVERITIES.index("high")


def candidates(findings: list[Finding], *, floor: str, limit: int) -> list[Finding]:
    """The findings worth the cost of a PoC: severe enough, not already high-confidence."""
    bar = _floor(floor)
    picked = [
        f for f in findings
        if SEVERITIES.index(f.severity) <= bar and (f.confidence or "").lower() != "high"
    ]
    picked.sort(key=lambda f: SEVERITIES.index(f.severity))
    return picked[:limit]


def parse_verdict(text: str) -> dict | None:
    """The trailing ```json verdict, or None. Tolerant like ``reports.parse_findings``."""
    import re

    blocks = re.findall(r"```(?:json)?\s*\n(.*?)```", text or "", re.DOTALL | re.IGNORECASE)
    for raw in reversed(blocks):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and "verified" in data:
            return data
    return None


async def verify_findings(
    findings: list[Finding], *, executor, security_repo, sec, repo: str, project: str = "",
) -> VerifyOutcome:
    """Attempt a PoC for each candidate finding. Never raises; always tears down.

    ``sec`` is the ``SecurityScanSettings`` (``verify_enabled`` / ``verify_from`` /
    ``verify_max_per_scan``). ``executor`` must expose ``_acquire_agent_scratch``,
    ``release_scratch`` and ``_run_claude`` (the real ``ClaudeExecutor``); a test passes
    a fake with the same surface. Worktree isolation is enforced by
    ``_acquire_agent_scratch`` returning None when worktrees are off — a finding whose
    PoC has nowhere isolated to run is skipped, never run in the real checkout.
    """
    out = VerifyOutcome()
    if not sec.verify_enabled:
        out.skipped = "verify disabled"
        return out
    if executor is None:
        out.skipped = "no executor"
        return out

    picked = candidates(findings, floor=sec.verify_from, limit=sec.verify_max_per_scan)
    if not picked:
        out.skipped = "no candidates"
        return out

    repo_name = Path(repo).name
    repo_abs = str(Path(repo).resolve())  # noqa: ASYNC240 — local path math
    for f in picked:
        out.attempted += 1
        verdict, evidence = await _verify_one(executor, repo_name, f)
        if verdict is None:
            out.errored += 1
            out.results.append({"fingerprint": f.fingerprint, "result": "error"})
            continue
        confirmed = bool(verdict.get("verified"))
        poc_md = _poc_markdown(verdict) if confirmed else ""
        if security_repo is not None and f.fingerprint:
            row = await security_repo.by_fingerprint(repo_abs, f.fingerprint)
            if row is not None:
                await security_repo.set_verification(row.id, confirmed, poc_md or evidence)
        if confirmed:
            out.confirmed += 1
            f.confidence = "high"
        else:
            out.refuted += 1
        out.results.append({
            "fingerprint": f.fingerprint, "result": "confirmed" if confirmed else "refuted",
        })
    _log.info(
        "verify pass done", repo=repo_name, attempted=out.attempted,
        confirmed=out.confirmed, refuted=out.refuted, errored=out.errored,
    )
    return out


async def _verify_one(executor, repo_name: str, f: Finding):
    """Run one PoC in an isolated worktree. Returns (verdict dict | None, evidence str)."""
    where = f"{f.file}:{f.line}" if f.line else f.file
    prompt = _PROMPT.format(
        severity=f.severity, title=f.title, where=where, detail=f.detail or "(none)",
        rule_id=f.rule_id or "?", cwe=f.cwe or "", owasp=f.owasp or "",
    )
    scratch = None
    try:
        scratch = await executor._acquire_agent_scratch(0, [repo_name])
        if not scratch:
            # No isolation available → refuse rather than run in the real checkout.
            _log.warning("verify: no scratch worktree — skipping finding", fp=f.fingerprint)
            return None, ""
        work_dir = str(Path(scratch) / repo_name)  # noqa: ASYNC240 — local path math
        run = await executor._run_claude(prompt, work_dir, repo=work_dir)
        verdict = parse_verdict(run.text or "")
        if verdict is None:
            return {"verified": False, "evidence": "no verdict returned"}, (run.text or "")[:500]
        return verdict, str(verdict.get("evidence") or "")[:1000]
    except Exception as exc:  # noqa: BLE001 — a PoC failure must not crash the scan
        _log.warning("verify: PoC run failed", fp=f.fingerprint, error=describe_exc(exc))
        return None, ""
    finally:
        if scratch:
            await executor.release_scratch(scratch)


async def verify_single(row, *, executor, security_repo, repo: str) -> dict | None:
    """The Security page's per-finding ▶ Verify: one stored row, one PoC, one verdict.

    Same isolation as the batch pass. Returns the verdict dict (or None on error) and
    records it on the row so the page shows verified / unconfirmed next time it loads.
    """
    f = Finding(
        severity=row.severity, title=row.title, file=row.file, line=row.line,
        detail=row.detail or "", rule_id=row.rule_id or "", cwe=row.cwe or "",
        owasp=row.owasp or "", fingerprint=row.fingerprint,
    )
    verdict, evidence = await _verify_one(executor, Path(repo).name, f)
    if verdict is None:
        return None
    confirmed = bool(verdict.get("verified"))
    poc_md = _poc_markdown(verdict) if confirmed else (
        "**Could not reproduce.** " + str(verdict.get("notes") or evidence or "")
    )
    if security_repo is not None:
        await security_repo.set_verification(row.id, confirmed, poc_md)
    return verdict


def _poc_markdown(verdict: dict) -> str:
    lines = ["**PoC verified.**", ""]
    if verdict.get("evidence"):
        lines += [str(verdict["evidence"]), ""]
    if verdict.get("command"):
        lines += ["```", str(verdict["command"]), "```"]
    files = verdict.get("poc_files") or []
    if files:
        lines.append("Files: " + ", ".join(f"`{p}`" for p in files))
    if verdict.get("notes"):
        lines += ["", "> " + str(verdict["notes"])]
    return "\n".join(lines)
