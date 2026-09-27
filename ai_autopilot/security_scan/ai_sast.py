"""The model-driven pass: what the regexes cannot see, and triage of what they did.

Pattern scanners find the shape of a bug — a concatenated SQL string, a missing
``rel=noopener``. They cannot tell whether an endpoint checks that the caller OWNS the
row it asked for (BOLA), whether a role check was skipped on one of twelve actions, or
whether a "secret" is a fixture. That is the model's half of the job, and it is handed
the scanners' output up front for two reasons: so it does not re-find what is already
found, and so it can say which of those are false positives — with a reason.

It runs through the executor's read-only path (``run_audit``): file-mutating tools
denied at the tool surface, fresh session every time, streamed to the activity feed.
The answer comes back under the same JSON contract every report loop uses, so
``reports.parse_findings`` reads it unchanged.
"""

from __future__ import annotations

from ai_autopilot import reports
from ai_autopilot.reports import SEVERITIES, Finding

# How many scanner findings to show the model. Above this it is a list, not a brief.
_MAX_SEEDED = 60

_FAST = (
    "Scan mode: FAST. Use the `security-review` skill in Fast mode: prioritise CRITICAL "
    "and HIGH — authentication and authorisation gaps (missing [Authorize], BOLA/IDOR: "
    "an id from the request used without checking the caller owns it, role checks "
    "skipped on some actions), injection (SQL, command, template), secrets, unsafe "
    "deserialisation, and mass assignment. Skip style and low-value findings."
)
_DEEP = (
    "Scan mode: DEEP. Use the `security-review` skill in Deep mode: the full OWASP API "
    "Security Top 10 (2023) and OWASP Top 10 (2021) checklists for this stack, all four "
    "severities, with a short STRIDE view of the trust boundaries you inspected. Read "
    "ownership and flow, not just grep hits: for every controller action that takes an "
    "id, say whether the caller's tenant/ownership is verified before the row is "
    "touched."
)

# Deep mode runs one focused pass per category instead of one prompt asking for
# everything — a reviewer told to hunt exactly one class of flaw finds instances a
# do-everything sweep skims past. Three passes ≈ 3x tokens, which is why only deep
# (the nightly/audit mode) pays it; fast stays a single pass.
DEEP_PASSES: tuple[tuple[str, str], ...] = (
    ("access-control",
     "THIS PASS HUNTS ACCESS CONTROL AND AUTHENTICATION ONLY — ignore other categories. "
     "BOLA/IDOR (CWE-639: an id from the request reaching a query without an ownership "
     "check), missing or wrong authorisation (CWE-285/862/863), function-level authz "
     "(admin actions reachable by ordinary roles), privilege management (CWE-269), "
     "authentication and session flaws (CWE-287, session fixation CWE-384, sessions "
     "that never expire CWE-613, weak password recovery CWE-640, missing brute-force "
     "protection CWE-307)."),
    ("injection-config",
     "THIS PASS HUNTS INJECTION AND CONFIGURATION ONLY — ignore other categories. "
     "SQL/command/template injection, SSRF (API7), unsafe deserialisation, secrets in "
     "config or code, security headers, CORS, mass assignment and excessive data "
     "exposure (API3), unsafe consumption of third-party APIs (API10)."),
    ("business-logic",
     "THIS PASS HUNTS BUSINESS LOGIC FLAWS ONLY — ignore other categories. These live "
     "in the gap between intended and actual behaviour, where pattern scanners see "
     "nothing: workflow steps that can be skipped or reordered (CWE-840/841 — can step "
     "N be reached without step N-1? can an approval status be set directly through an "
     "update endpoint or mass assignment?), actions that must be idempotent or limited "
     "but are repeatable (double-submit → double spend or duplicate approval, CWE-799), "
     "missing rate limits or resource caps on expensive or sensitive flows (CWE-770, "
     "API4/API6), client-supplied price/quantity/total arithmetic (negative amounts, "
     "totals computed on the client), and race conditions on balances, stock or "
     "counters (CWE-362)."),
)


def seeded_block(findings: list[Finding], *, triage: bool = True) -> str:
    """The scanner findings, formatted for the model. ``triage=False`` lists them only
    as already-found (a multi-pass deep scan asks ONE pass to triage — three passes all
    triaging the same seed would stack three verdict notes on every finding)."""
    if not findings:
        return ""
    rows = []
    for f in findings[:_MAX_SEEDED]:
        where = f"{f.file}:{f.line}" if f.line else f.file
        rows.append(
            f"- [{f.severity}] ({f.tool}/{f.rule_id}) {where} — {f.title}"
            + (f" — `{f.snippet[:120]}`" if f.snippet else "")
        )
    more = len(findings) - _MAX_SEEDED
    head = (
        "External scanner findings — ALREADY FOUND, do not report these again. "
        "Triage them instead: for each one you inspect, add a finding with `rule_id` "
        "\"triage\", the same `file` and `line`, `severity` \"info\", and a `title` "
        "starting with either \"CONFIRMED:\" or \"FALSE POSITIVE:\" followed by one "
        "sentence of reason. Then look for what they cannot see.\n"
    ) if triage else (
        "External scanner findings — ALREADY FOUND, do not report or triage these; "
        "another pass owns them. Look for what they cannot see.\n"
    )
    return head + "\n".join(rows) + (f"\n… and {more} more not shown." if more > 0 else "")


def build_prompt(
    *, repo: str, mode: str, agents: list[str], seeded: list[Finding],
    digest: str = "", scope_note: str = "", focus: str = "", triage: bool = True,
) -> str:
    """The full prompt: operator's ask, mode, scanner seed, the report contract.

    ``focus`` narrows a deep pass to one category (see :data:`DEEP_PASSES`); ``triage``
    hands the scanner-seed verdict work to exactly one pass of a multi-pass run.
    """
    ask = _DEEP if (mode or "fast").lower() == "deep" else _FAST
    parts = [
        "Security review of this repository. Report only what you can point at — "
        "file and line — and say what an attacker gets.",
        ask,
    ]
    if focus:
        parts.append(focus)
    if scope_note:
        parts.append(scope_note)
    seed = seeded_block(seeded, triage=triage)
    if seed:
        parts.append(seed)
    parts.append(
        "For every finding fill `cwe` (e.g. CWE-639 for BOLA, CWE-89 for SQLi), `owasp` "
        "(API1:2023 … API10:2023 for backend, A01:2021 … A10:2021 for web), a short "
        "`rule_id` slug, and `confidence` (high only when you read the code path end to "
        "end)."
    )
    return reports.audit_prompt("\n\n".join(parts), agents, repo, digest=digest)


Verdicts = dict[tuple[str, int | None], str]


def split_triage(findings: list[Finding]) -> tuple[list[Finding], Verdicts]:
    """Separate the model's triage notes from its own findings.

    Returns ``(own_findings, verdicts)`` where verdicts map ``(file, line)`` →
    ``"confirmed"`` / ``"false_positive"``. The scan applies them to the scanner
    findings: a false positive drops to ``low``/``info`` with the reason attached —
    demoted, not deleted, because the model can be wrong too and a reader should be
    able to see what was demoted and why.
    """
    own: list[Finding] = []
    verdicts: Verdicts = {}
    for f in findings:
        if (f.rule_id or "").lower() == "triage":
            title = (f.title or "").strip()
            upper = title.upper()
            key = (f.file, f.line)
            if upper.startswith("FALSE POSITIVE"):
                verdicts[key] = "false_positive:" + title.split(":", 1)[-1].strip()
            elif upper.startswith("CONFIRMED"):
                verdicts[key] = "confirmed:" + title.split(":", 1)[-1].strip()
            continue
        f.tool = f.tool or "ai"
        own.append(f)
    return own, verdicts


def apply_triage(scanner_findings: list[Finding], verdicts: dict) -> int:
    """Demote scanner findings the model called false positives; returns how many."""
    if not verdicts:
        return 0
    demoted = 0
    for f in scanner_findings:
        verdict = verdicts.get((f.file, f.line)) or verdicts.get((f.file, None))
        if not verdict:
            continue
        kind, _, reason = verdict.partition(":")
        prefix = (f.detail + " ") if f.detail else ""
        if kind == "false_positive":
            if f.kev:
                # CISA says this is being exploited in the wild; the model's opinion is
                # recorded but does not lower severity — the gate keys on ``kev``, and a
                # demoted-yet-failing finding would read as a contradiction.
                f.detail = prefix + f"AI triage disagreed ({reason}) — kept: in CISA KEV."
                continue
            if SEVERITIES.index(f.severity) < SEVERITIES.index("low"):
                f.severity = "low"
            f.confidence = "low"
            f.detail = prefix + f"AI triage: likely false positive — {reason}"
            demoted += 1
        elif kind == "confirmed":
            f.confidence = "high"
            f.detail = prefix + f"AI triage: confirmed — {reason}"
    return demoted
