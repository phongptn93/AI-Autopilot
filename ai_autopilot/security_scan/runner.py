"""One scan, end to end: scanners → model → merge → fingerprint → suppress → baseline
diff → persist → render. Every entry point (CLI, loop, gate, page) calls :func:`run_scan`
so they cannot disagree about what a finding is or when the gate fails.

The pieces are independent on purpose. Storage is optional (``--no-store``, or no
repository handed in) so the CLI works on a laptop with no database; the model is
optional (``ai_mode: off``) so a CI runner with no API key still gets the deterministic
half; each scanner is optional because its binary might not be there. What is never
optional is the answer: a :class:`ScanResult` comes back even when everything failed,
saying so per tool.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_autopilot import activity, reports
from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.reports import SEVERITIES, Finding
from ai_autopilot.security_scan import ai_sast, progress
from ai_autopilot.security_scan import fingerprint as fp_mod
from ai_autopilot.security_scan import suppressions as sup_mod
from ai_autopilot.security_scan.tools import ALL_TOOLS, registry
from ai_autopilot.security_scan.tools.base import ToolStatus, run_process

_log = get_logger("security_scan.runner")


@dataclass
class ScanRequest:
    repo: str                                   # absolute path of the repo to scan
    project: str = ""                           # ADO project (workspace scoping)
    workspace: str = ""                         # where suppressions + reports live
    tools: list[str] = field(default_factory=lambda: list(ALL_TOOLS))
    ai_mode: str = "off"                        # off | fast | deep
    ai_agents: list[str] = field(default_factory=list)
    scope: str = "full"                         # full | diff
    base_branch: str = "main"                   # for scope=diff
    fail_on: str = "high"
    trigger: str = "cli"                        # cli | loop | pr-gate | dashboard
    loop_name: str = ""
    store: bool = True
    write_html: bool = True
    max_findings_per_tool: int = 500
    semgrep_config: list[str] = field(default_factory=list)
    gitleaks_log_opts: str = ""                 # e.g. "main..HEAD" for a branch gate
    files: list[str] | None = None              # explicit file list (overrides scope)
    disabled_rules: list[str] = field(default_factory=list)
    ignore_paths: list[str] = field(default_factory=list)
    # Progress: streamed to the workspace activity feed (``activity.security_key``) so
    # the Security page and /dashboard/now can watch; ``on_progress`` for callers that
    # want it in-process too.
    on_progress: Any = None


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)       # reported (not suppressed)
    suppressed: list[Finding] = field(default_factory=list)
    diff: fp_mod.Diff = field(default_factory=fp_mod.Diff)
    tools: dict[str, ToolStatus] = field(default_factory=dict)
    ai_summary: str = ""
    ai_body: str = ""
    ai_demoted: int = 0
    scan_id: int = 0
    report_id: int = 0
    html_path: str = ""
    duration_seconds: float = 0.0
    fail_on: str = "high"
    scope: str = "full"
    baseline_known: bool = False
    expired_suppressions: list[str] = field(default_factory=list)
    error: str = ""
    cost_tokens: int = 0
    verify: object = None   # VerifyOutcome when a PoC pass ran (Phase 3)
    filtered: int = 0       # dropped by disabled_rules / ignore_paths

    @property
    def counts(self) -> dict[str, int]:
        return reports.severity_counts(self.findings)

    @property
    def gate_findings(self) -> list[Finding]:
        """New, unsuppressed findings at/above ``fail_on`` — what fails the gate."""
        floor = _sev_index(self.fail_on)
        return [f for f in self.diff.new if SEVERITIES.index(f.severity) <= floor]

    @property
    def passed(self) -> bool:
        return not self.gate_findings and not self.error

    def tool_labels(self) -> dict[str, str]:
        return {name: st.label for name, st in self.tools.items()}


def apply_ignores(
    findings: list[Finding], disabled_rules: list[str], ignore_paths: list[str],
) -> tuple[list[Finding], int]:
    """Drop findings whose rule is switched off or whose path is ignored. Returns
    ``(kept, dropped)``. Globs are ``fnmatch`` over the posix repo-relative path, with
    ``**`` allowed as a prefix/suffix wildcard the way people write it."""
    if not disabled_rules and not ignore_paths:
        return findings, 0
    rules = {r.strip().lower() for r in disabled_rules if r.strip()}
    globs = [g.strip() for g in ignore_paths if g.strip()]
    kept: list[Finding] = []
    dropped = 0
    for f in findings:
        if (f.rule_id or "").lower() in rules or _path_ignored(f.file, globs):
            dropped += 1
            continue
        kept.append(f)
    return kept, dropped


def _path_ignored(path: str, globs: list[str]) -> bool:
    if not path or not globs:
        return False
    p = path.replace("\\", "/")
    for g in globs:
        g = g.replace("\\", "/")
        # "**/x/**" → any segment "x"; "**/*.ext" → suffix; plain → fnmatch.
        core = g
        if core.startswith("**/"):
            core = core[3:]
        if core.endswith("/**"):
            core = core[:-3]
            if f"/{core}/" in f"/{p}/" or p.startswith(core + "/"):
                return True
            continue
        if fnmatch.fnmatch(p, g) or fnmatch.fnmatch(p, core) or fnmatch.fnmatch(
            p.rsplit("/", 1)[-1], core):
            return True
    return False


def _sev_index(name: str) -> int:
    name = (name or "high").strip().lower()
    return SEVERITIES.index(name) if name in SEVERITIES else SEVERITIES.index("high")


async def changed_files(repo: str, base_branch: str) -> list[str]:
    """Files changed on this branch vs ``origin/<base>`` (falls back to local base)."""
    for ref in (f"origin/{base_branch}", base_branch):
        res = await run_process(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{ref}...HEAD"],
            repo, timeout_seconds=60,
        )
        if res.ok and res.returncode == 0:
            files = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
            return [f for f in files if (Path(repo) / f).is_file()]
    return []


async def run_scan(
    req: ScanRequest, *, executor: Any = None, security_repo: Any = None,
    loop_report_repo: Any = None, config: Any = None,
) -> ScanResult:
    """Run the whole pipeline. Never raises: failures land in ``result.error`` /
    per-tool status and the result is still stored, because "the scan did not run" is
    the one fact nobody can see otherwise."""
    started = time.monotonic()
    started_at = datetime.now(UTC)
    result = ScanResult(fail_on=req.fail_on, scope=req.scope)
    repo = str(Path(req.repo).resolve())  # noqa: ASYNC240 — local path math
    workspace = req.workspace or str(Path(repo).parent)
    prog = progress.start(repo, req.trigger)
    feed_key = activity.security_key(Path(repo).name)
    activity.clear(workspace, feed_key)

    def say(line: str, stage: str | None = None) -> None:
        if stage:
            prog.stage = stage
        activity.append(workspace, feed_key, line)
        if req.on_progress:
            with contextlib.suppress(Exception):
                req.on_progress(line)

    say(f"🔐 security scan started — {Path(repo).name} · scope {req.scope} · AI {req.ai_mode}"
        f" · tools {', '.join(t for t in req.tools if t != 'ai') or '-'}")

    # The baseline is read BEFORE this scan is recorded, or the first scan of a repo
    # would see itself in the history and report every finding as "known".
    baseline: set[str] | None = None
    if security_repo is not None:
        try:
            if await security_repo.has_history(repo):
                baseline = await security_repo.open_fingerprints(repo)
                result.baseline_known = True
        except Exception as exc:  # noqa: BLE001
            _log.warning("security scan: baseline unavailable", error=describe_exc(exc))

    scan_id = 0
    if req.store and security_repo is not None:
        try:
            scan_id = await security_repo.start_scan(
                repo=repo, project=req.project, trigger=req.trigger, loop_name=req.loop_name,
                scope=req.scope, ai_mode=req.ai_mode, fail_on=req.fail_on,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("security scan: could not record start", error=describe_exc(exc))
    result.scan_id = scan_id
    prog.scan_id = scan_id

    # ── which files ───────────────────────────────────────────────────────────
    files = req.files
    if files is None and req.scope == "diff":
        files = await changed_files(repo, req.base_branch)
        say(f"📂 diff vs {req.base_branch}: {len(files)} changed file(s)")
        if not files:
            _log.info("security scan: diff scope has no changed files", repo=repo)
    say("▶ scanners", "scanners")

    # ── deterministic scanners, in parallel ───────────────────────────────────
    adapters = registry(config)
    if req.semgrep_config:
        adapters["semgrep"] = type(adapters["semgrep"])(rulesets=req.semgrep_config)
    if req.gitleaks_log_opts:
        adapters["gitleaks"] = type(adapters["gitleaks"])(log_opts=req.gitleaks_log_opts)
    wanted = [t for t in req.tools if t in adapters and t != "ai"]
    scanner_findings: list[Finding] = []

    async def _one(name: str):
        adapter = adapters[name]
        try:
            if files is not None and not files:
                return name, [], ToolStatus(name, skipped_reason="nothing to scan")
            run = await adapter.run(repo, files)
            return name, run.findings, run.status
        except Exception as exc:  # noqa: BLE001
            return name, [], ToolStatus(name, error=describe_exc(exc))

    for name, found, status in await asyncio.gather(*(_one(n) for n in wanted)):
        if len(found) > req.max_findings_per_tool:
            status.extra["truncated"] = len(found) - req.max_findings_per_tool
            found = found[: req.max_findings_per_tool]
        result.tools[name] = status
        prog.tools[name] = status.label
        say(f"  {name}: {status.label}")
        scanner_findings.extend(found)
    scanner_findings, result.filtered = apply_ignores(
        scanner_findings, req.disabled_rules, req.ignore_paths,
    )
    if result.filtered:
        say(f"  ignored {result.filtered} by disabled_rules / ignore_paths")
    scanner_findings = fp_mod.dedupe(scanner_findings)

    # ── the model ─────────────────────────────────────────────────────────────
    ai_findings: list[Finding] = []
    ai_mode = (req.ai_mode or "off").lower()
    if ai_mode in ("fast", "deep"):
        st = ToolStatus("ai")
        if executor is None:
            st.skipped_reason = "no executor"
        else:
            t0 = time.monotonic()
            ai_feed = activity.loop_key(req.loop_name or f"security-scan-{ai_mode}")
            say(f"🤖 AI review ({ai_mode}) — live: /dashboard/activity/{ai_feed}", "ai")
            scope_note = ""
            if files is not None:
                shown = files[:80]
                scope_note = ("Scope: ONLY these changed files (already computed):\n"
                              + "\n".join(f"- {f}" for f in shown)
                              + (f"\n… and {len(files) - 80} more." if len(files) > 80 else ""))
            prompt = ai_sast.build_prompt(
                repo=repo, mode=ai_mode, agents=req.ai_agents, seeded=scanner_findings,
                scope_note=scope_note,
            )
            try:
                exec_result = await executor.run_audit(
                    req.loop_name or f"security-scan-{ai_mode}", prompt, repo,
                    req.base_branch, req.project,
                )
                st.duration_seconds = time.monotonic() - t0
                result.cost_tokens = int(getattr(exec_result, "cost_tokens", 0) or 0)
                text = getattr(exec_result, "output", "") or ""
                if getattr(exec_result, "success", False):
                    summary, parsed = reports.parse_findings(text)
                    own, verdicts = ai_sast.split_triage(parsed)
                    for f in own:
                        f.tool = "ai"
                        f.agent = f.agent or (req.ai_agents[0] if req.ai_agents else "")
                    result.ai_demoted = ai_sast.apply_triage(scanner_findings, verdicts)
                    ai_findings = own
                    result.ai_summary, result.ai_body = summary, text
                    st.ran, st.findings = True, len(own)
                    st.extra["triaged"] = len(verdicts)
                else:
                    st.error = (getattr(exec_result, "error", "") or "model run failed")[:200]
            except Exception as exc:  # noqa: BLE001
                st.error = describe_exc(exc)[:200]
            ai_findings, _ = (apply_ignores(ai_findings, req.disabled_rules, req.ignore_paths)
                              if ai_findings else (ai_findings, 0))
        result.tools["ai"] = st
        prog.tools["ai"] = st.label
        say(f"  ai: {st.label}")

    # ── merge · suppress · baseline ───────────────────────────────────────────
    merged = fp_mod.dedupe(scanner_findings + ai_findings)
    sup = sup_mod.load(workspace)
    active = sup.active_fingerprints()
    result.expired_suppressions = [s.fingerprint for s in sup.expired()]
    result.suppressed = [f for f in merged if f.fingerprint in active]
    for f in result.suppressed:
        prefix = (f.detail + " ") if f.detail else ""
        f.detail = prefix + f"Suppressed: {sup.reason_for(f.fingerprint)}"
    reported = [f for f in merged if f.fingerprint not in active]
    result.findings = reported
    if baseline is not None:
        # A finding that is still detected but suppressed is neither new nor fixed: it
        # is not in ``reported`` (so the plain diff would call it fixed), so take it
        # out of the baseline before comparing.
        baseline = baseline - {f.fingerprint for f in result.suppressed}

    if baseline is not None and files is not None and security_repo is not None:
        # A diff scan only read ``files``: it can say a finding there is fixed, but
        # knows nothing about the rest of the repo, so "fixed" is judged against the
        # part of the baseline it actually looked at.
        try:
            scoped = await security_repo.open_fingerprints(repo, files=set(files))
            seen_known = {f.fingerprint for f in reported if f.fingerprint in baseline}
            baseline = (baseline & scoped) | seen_known
        except Exception as exc:  # noqa: BLE001
            _log.warning("security scan: scoped baseline unavailable", error=describe_exc(exc))
    result.diff = fp_mod.diff(reported, baseline)

    d = result.diff.counts
    say(f"📊 {len(result.findings)} finding(s) · {d['new']} new · {d['fixed']} fixed · "
        f"{len(result.suppressed)} suppressed", "storing")

    # ── persist (before verify, so PoC results land on stored rows) ────────────
    if req.store and security_repo is not None:
        try:
            await security_repo.upsert_scan(
                repo, req.project, scan_id, merged, scope=req.scope, suppressed=active,
                scanned_files=set(files) if files else None,
            )
            result._stored = True  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            _log.warning("security scan: findings not stored", error=describe_exc(exc))
            result.error = result.error or f"store failed: {describe_exc(exc)}"

    # ── Phase 3: PoC verification of the new, high-severity findings ───────────
    if executor is not None and config is not None and getattr(config, "verify_enabled", False):
        try:
            from ai_autopilot.security_scan import verify as verify_mod

            say(f"🧪 PoC verification of up to {config.verify_max_per_scan} new finding(s)",
                "verify")
            result.verify = await verify_mod.verify_findings(
                result.diff.new, executor=executor, security_repo=security_repo,
                sec=config, repo=repo, project=req.project,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("security scan: verify pass failed", error=describe_exc(exc))

    # ── render ────────────────────────────────────────────────────────────────
    result.duration_seconds = time.monotonic() - started
    report = _as_report(req, result, started_at)
    if req.write_html and req.workspace:
        result.html_path = _write_html(req, report)
    if req.store and loop_report_repo is not None:
        try:
            result.report_id = await loop_report_repo.save(report, html_path=result.html_path)
        except Exception as exc:  # noqa: BLE001
            _log.warning("security scan: report not saved", error=describe_exc(exc))
    if req.store and security_repo is not None and scan_id:
        counts = result.counts
        try:
            await security_repo.finish_scan(
                scan_id, status="failed" if result.error else "success",
                tools_json=json.dumps(result.tool_labels()),
                new_count=len(result.diff.new), fixed_count=len(result.diff.fixed),
                suppressed_count=len(result.suppressed), gate_passed=result.passed,
                report_id=result.report_id or None, html_path=result.html_path or None,
                error=(result.error or None), duration_seconds=result.duration_seconds,
                cost_tokens=result.cost_tokens, filtered_count=result.filtered,
                new_json=json.dumps([f.fingerprint for f in result.diff.new]),
                fixed_json=json.dumps(list(result.diff.fixed)),
                **{f"{sev}_count": counts[sev] for sev in SEVERITIES},
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("security scan: could not record finish", error=describe_exc(exc))
    gate = "✅ gate passed" if result.passed else "❌ gate FAILED"
    say(f"{gate} · {result.duration_seconds:.0f}s"
        + (f" · report /dashboard/reports/{result.report_id}" if result.report_id else ""), "done")
    progress.finish(repo)

    _log.info(
        "security scan finished", repo=Path(repo).name, scope=req.scope, ai=ai_mode,
        findings=len(result.findings), new=len(result.diff.new), fixed=len(result.diff.fixed),
        suppressed=len(result.suppressed), passed=result.passed,
        tools={k: v for k, v in result.tool_labels().items()},
    )
    return result


def _as_report(req: ScanRequest, result: ScanResult, started_at: datetime) -> reports.Report:
    """The scan as a :class:`reports.Report`, so the Reports page and HTML renderer show
    it like any other audit — with the tool table and baseline diff in the body."""
    verdict = "PASS" if result.passed else "FAIL"
    lines = [f"# Security scan — {Path(req.repo).name}", ""]
    lines.append(f"Scope: **{req.scope}** · AI: **{req.ai_mode}** · gate: fail on "
                 f"**{req.fail_on}** → **{verdict}**")
    lines += ["", "## Tools", "", "| Tool | Result |", "|---|---|"]
    lines += [f"| {name} | {label} |" for name, label in result.tool_labels().items()]
    d = result.diff.counts
    lines += ["", "## Against baseline", ""]
    if result.baseline_known:
        lines.append(f"- **{d['new']} new**, {d['persisting']} already known, "
                     f"{d['fixed']} fixed since last scan")
    else:
        lines.append(f"- no earlier scan of this repo: all {d['new']} findings count as new")
    if result.suppressed:
        lines.append(f"- {len(result.suppressed)} suppressed "
                     f"(see `.autopilot/{sup_mod.FILE_NAME}`)")
    if result.expired_suppressions:
        lines.append(f"- ⚠ {len(result.expired_suppressions)} suppression(s) EXPIRED "
                     "and are reported again")
    if result.ai_demoted:
        lines.append(f"- AI triage demoted {result.ai_demoted} scanner finding(s) "
                     "as likely false positives")
    v = result.verify
    if v is not None and getattr(v, "attempted", 0):
        lines.append(f"- PoC verify: {v.confirmed} confirmed, {v.refuted} could not be "
                     f"reproduced, of {v.attempted} attempted")
    if result.gate_findings:
        lines += ["", "## Gate", ""]
        lines += [f"- [{f.severity}] {f.file}:{f.line or ''} — {f.title}"
                  for f in result.gate_findings[:30]]
    if result.ai_body:
        lines += ["", "## AI review", "", reports.strip_findings_block(result.ai_body)]
    return reports.Report(
        loop=req.loop_name or f"security-scan ({req.trigger})",
        summary=result.ai_summary or _summary_line(result),
        body_md="\n".join(lines),
        findings=list(result.findings),
        status="failed" if result.error else "success",
        project=req.project, repo=req.repo,
        started_at=started_at, finished_at=datetime.now(UTC),
        duration_seconds=result.duration_seconds,
        agents=list(req.ai_agents) if req.ai_mode != "off" else [],
    )


def _summary_line(result: ScanResult) -> str:
    c = result.counts
    parts = [f"{c[s]} {s}" for s in SEVERITIES if c[s]]
    head = ", ".join(parts) or "no findings"
    gate = "passed" if result.passed else "FAILED"
    return f"{head}; {len(result.diff.new)} new since baseline; gate {gate}."


def _write_html(req: ScanRequest, report: reports.Report) -> str:
    try:
        out_dir = Path(req.workspace) / "reports" / "security" / Path(req.repo).name
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{datetime.now(UTC).strftime('%Y%m%d-%H%M')}.html"
        path.write_text(reports.render_html(report), encoding="utf-8")
        return str(path)
    except OSError as exc:
        _log.warning("security scan: html not written", error=describe_exc(exc))
        return ""
