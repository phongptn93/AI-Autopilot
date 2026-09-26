"""``ai-autopilot scan`` — the pipeline-facing entry point.

Exit code is the contract: 0 when no NEW, unsuppressed finding is at or above
``--fail-on``; 1 when there is one; 2 for a usage error; 3 when the scan itself could
not run. "New" is measured against the stored baseline when there is one, so a
long-lived repo with known debt still gates on regressions rather than on history.

Works without the dashboard running: it needs the config (for the workspace and the
database URL) and nothing else. ``--no-store`` needs not even that.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path

from ai_autopilot.reports import SEVERITIES
from ai_autopilot.security_scan import sarif
from ai_autopilot.security_scan.runner import ScanRequest, ScanResult, run_scan
from ai_autopilot.security_scan.tools import ALL_TOOLS

_FORMATS = ("table", "json", "sarif", "html", "md")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ai-autopilot scan",
        description="Security scan: SAST + SCA + secrets, with baseline and suppressions.",
    )
    p.add_argument("--repo", default="", help="repo path (default: repo_working_directory / cwd)")
    p.add_argument("--project", default="", help="ADO project whose workspace to use")
    p.add_argument("--mode", choices=("off", "fast", "deep"), default=None,
                   help="AI review depth (default: security_scan.ai_mode)")
    p.add_argument("--no-ai", action="store_true", help="same as --mode off")
    p.add_argument("--scope", choices=("full", "diff"), default="full")
    p.add_argument("--base", default="", help="base branch for --scope diff (default: config)")
    p.add_argument("--tools", default="",
                   help=f"comma list of {', '.join(ALL_TOOLS)} (default: config)")
    p.add_argument("--fail-on", choices=SEVERITIES, default=None)
    p.add_argument("--format", choices=_FORMATS, default="table")
    p.add_argument("--out", default="", help="write the formatted output to this file")
    p.add_argument("--no-store", action="store_true", help="do not touch the database / baseline")
    p.add_argument("--no-html", action="store_true", help="do not write the HTML report")
    p.add_argument("--all", action="store_true", help="table: include suppressed findings")
    p.add_argument("--verify", action="store_true",
                   help="Phase 3: build a PoC for each new high+ finding in an isolated "
                        "worktree (needs worktrees + an API key)")
    p.add_argument("--dast", default="", metavar="TARGET",
                   help="Phase 4: dynamic probe of a configured, owner-confirmed DAST target "
                        "instead of a code scan")
    p.add_argument("--quiet", "-q", action="store_true")
    return p


def run(argv: list[str]) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) else 2
    # A Windows console defaults to cp1252 and dies on the first "✅"; a pipeline log is
    # UTF-8 anyway. Replace rather than raise — an exit code must never depend on a glyph.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")
    _quiet_logging(args.quiet)
    try:
        return asyncio.run(_main(args))
    except KeyboardInterrupt:
        return 130


def _quiet_logging(quiet: bool) -> None:
    """Route library logs to stderr so stdout is ONLY the formatted result.

    Unconfigured structlog prints to stdout, which put a log line in front of the JSON
    and broke every ``| jq`` — the one thing a ``--format json`` must not do.
    """
    import logging

    import structlog

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.ERROR if quiet else logging.INFO
        ),
        cache_logger_on_first_use=False,
    )
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)


async def _main(args: argparse.Namespace) -> int:
    from ai_autopilot.config import load_settings

    config = load_settings()
    sec = config.security_scan
    scoped = config.scoped_for_project(args.project) if args.project else config

    repo = args.repo or scoped.repo_working_directory or "."
    repo_path = Path(repo).resolve()  # noqa: ASYNC240 — local path math
    if not repo_path.is_dir():  # noqa: ASYNC240
        print(f"not a directory: {repo_path}", file=sys.stderr)
        return 2
    workspace = scoped.workspace_directory or str(repo_path.parent)

    if args.dast:
        return await _run_dast(args, config, scoped)

    ai_mode = "off" if args.no_ai else (args.mode or sec.ai_mode or "off")
    if args.verify:
        sec.verify_enabled = True
        if ai_mode == "off":
            ai_mode = "fast"   # verify needs the model; keep it on
    tools = list(sec.tools)
    if args.tools:
        tools = [t.strip() for t in args.tools.split(",") if t.strip()]
    unknown = [t for t in tools if t not in ALL_TOOLS and t != "ai"]
    if unknown:
        print(f"unknown tool(s): {', '.join(unknown)} — choose from {', '.join(ALL_TOOLS)}",
              file=sys.stderr)
        return 2

    req = ScanRequest(
        repo=str(repo_path), project=args.project, workspace=workspace, tools=tools,
        ai_mode=ai_mode, ai_agents=list(sec.ai_agents), scope=args.scope,
        base_branch=args.base or scoped.base_branch or "main",
        fail_on=args.fail_on or sec.fail_on, trigger="cli", store=not args.no_store,
        write_html=not args.no_html, max_findings_per_tool=sec.max_findings_per_tool,
        semgrep_config=list(sec.semgrep_config),
        disabled_rules=list(sec.disabled_rules), ignore_paths=list(sec.ignore_paths),
        on_progress=(None if args.quiet else lambda line: print(f"  {line}", file=sys.stderr)),
    )

    executor = security_repo = loop_report_repo = None
    container = None
    if req.store or ai_mode != "off":
        # The container owns the database and the executor. Built lazily: a --no-store
        # --no-ai run must work with no database file and no API key.
        from ai_autopilot.container import Container

        container = Container(scoped)
        if req.store:
            await container.database.create_all()
            security_repo, loop_report_repo = container.security_repo, container.loop_report_repo
        if ai_mode != "off":
            executor = container.executor
    try:
        result = await run_scan(
            req, executor=executor, security_repo=security_repo,
            loop_report_repo=loop_report_repo, config=sec,
        )
    finally:
        if container is not None:
            await container.database.dispose()

    text = render(result, args.format, repo=str(repo_path), include_suppressed=args.all)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")  # noqa: ASYNC240 — one small file
        if not args.quiet:
            print(f"wrote {args.out}")
            if args.format != "table":
                print(render(result, "table", repo=str(repo_path), include_suppressed=args.all))
    elif not args.quiet or args.format != "table":
        print(text)
    if result.error and not result.findings:
        return 3
    return 0 if result.passed else 1


async def _run_dast(args: argparse.Namespace, config, scoped) -> int:
    """``ai-autopilot scan --dast <target>`` — a dynamic probe of a running app.

    Refuses (exit 2) when the target is unknown or a gate fails; the gate messages are
    safe to print. The probe itself needs the model, so it always builds the container.
    """
    from ai_autopilot.container import Container
    from ai_autopilot.security_scan import dast

    target = dast.find_target(config, args.dast)
    if target is None:
        names = ", ".join(t.name for t in config.security_scan.dast_targets) or "(none configured)"
        print(f"no DAST target named {args.dast!r}. Configured: {names}", file=sys.stderr)
        return 2
    gate = dast.check_target(target)
    if not gate.ok:
        print(f"DAST refused: {gate.reason}", file=sys.stderr)
        return 2
    if not config.security_scan.dast_enabled:
        print("DAST is disabled — set security_scan.dast_enabled: true", file=sys.stderr)
        return 2

    container = Container(scoped)
    try:
        await container.database.create_all()
        out = await dast.run_dast(
            config=scoped, target_name=args.dast, executor=container.executor,
        )
    finally:
        await container.database.dispose()
    if out.refused:
        print(out.refused, file=sys.stderr)
        return 3
    print(f"DAST {target.name}: {len(out.findings)} finding(s) — {out.status.label}")
    for f in out.findings:
        print(f"  {f.severity:<9}{(f.cwe or ''):<9}{f.title}")
    blocking = [f for f in out.findings
                if SEVERITIES.index(f.severity) <= SEVERITIES.index(config.security_scan.fail_on)]
    return 1 if blocking else 0


# ── output formats ──────────────────────────────────────────────────────────

def render(
    result: ScanResult, fmt: str, *, repo: str = "", include_suppressed: bool = False,
) -> str:
    if fmt == "json":
        return json.dumps(_as_json(result), indent=2, ensure_ascii=False)
    if fmt == "sarif":
        return sarif.dumps(result.findings, repo=repo)
    if fmt == "html":
        from datetime import UTC, datetime

        from ai_autopilot import reports
        from ai_autopilot.security_scan.runner import _as_report
        req = ScanRequest(repo=repo, fail_on=result.fail_on, scope=result.scope)
        return reports.render_html(_as_report(req, result, datetime.now(UTC)))
    if fmt == "md":
        return _markdown(result, include_suppressed)
    return _table(result, include_suppressed)


def _as_json(result: ScanResult) -> dict:
    return {
        "passed": result.passed, "fail_on": result.fail_on, "scope": result.scope,
        "counts": result.counts, "diff": result.diff.counts,
        "baseline_known": result.baseline_known,
        "tools": result.tool_labels(),
        "scan_id": result.scan_id, "report_id": result.report_id, "html": result.html_path,
        "duration_seconds": round(result.duration_seconds, 1),
        "ai_summary": result.ai_summary, "error": result.error,
        "findings": [dict(f.as_dict(), new=(f in result.diff.new)) for f in result.findings],
        "suppressed": [f.as_dict() for f in result.suppressed],
        "fixed": list(result.diff.fixed),
    }


def _table(result: ScanResult, include_suppressed: bool) -> str:
    c = result.counts
    new_fps = {f.fingerprint for f in result.diff.new}
    lines = []
    verdict = "PASS ✅" if result.passed else "FAIL ❌"
    lines.append(
        f"Security scan — {verdict}  (gate: new ≥ {result.fail_on}; scope: {result.scope})"
    )
    lines.append("  " + "  ".join(f"{s}: {c[s]}" for s in SEVERITIES))
    d = result.diff.counts
    base = "vs baseline" if result.baseline_known else "no baseline (first scan)"
    lines.append(f"  new: {d['new']}  known: {d['persisting']}  fixed: {d['fixed']}  "
                 f"suppressed: {len(result.suppressed)}  — {base}")
    lines.append("  tools: " + "; ".join(f"{k} → {v}" for k, v in result.tool_labels().items()))
    if result.expired_suppressions:
        lines.append(f"  ⚠ {len(result.expired_suppressions)} suppression(s) expired")
    if result.error:
        lines.append(f"  ⚠ {result.error}")
    if result.ai_summary:
        lines.append(f"  AI: {result.ai_summary}")
    rows = list(result.findings) + (list(result.suppressed) if include_suppressed else [])
    if not rows:
        lines.append("\n  No findings.")
        return "\n".join(lines)
    lines.append("")
    lines.append(f"  {'SEV':<9}{'NEW':<5}{'TOOL':<9}{'CWE':<9}{'WHERE':<52}TITLE")
    for f in rows:
        where = f"{f.file}:{f.line}" if f.line else f.file
        if len(where) > 50:
            where = "…" + where[-49:]
        flag = "new" if f.fingerprint in new_fps else ("sup" if f in result.suppressed else "")
        lines.append(
            f"  {f.severity:<9}{flag:<5}{(f.tool or 'ai'):<9}{(f.cwe or ''):<9}"
            f"{where:<52}{f.title[:70]}"
        )
    lines.append("")
    lines.append("  fingerprints: run with --format json for the ids to put in "
                 ".autopilot/security-suppressions.yaml")
    if result.html_path:
        lines.append(f"  report: {result.html_path}")
    return "\n".join(lines)


def _markdown(result: ScanResult, include_suppressed: bool) -> str:
    c = result.counts
    d = result.diff.counts
    out = [f"## Security scan — {'PASS' if result.passed else 'FAIL'}", ""]
    out.append("| " + " | ".join(SEVERITIES) + " | new | fixed | suppressed |")
    out.append("|" + "---|" * (len(SEVERITIES) + 3))
    out.append("| " + " | ".join(str(c[s]) for s in SEVERITIES)
               + f" | {d['new']} | {d['fixed']} | {len(result.suppressed)} |")
    out += ["", "| Tool | Result |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in result.tool_labels().items()]
    rows = list(result.findings) + (list(result.suppressed) if include_suppressed else [])
    if rows:
        new_fps = {f.fingerprint for f in result.diff.new}
        out += ["", "| Sev | New | Tool | CWE | Where | Finding | Fingerprint |",
                "|---|---|---|---|---|---|---|"]
        for f in rows:
            where = f"{f.file}:{f.line}" if f.line else f.file
            new = "✦" if f.fingerprint in new_fps else ""
            out.append(f"| {f.severity} | {new} | {f.tool or 'ai'} | {f.cwe} | `{where}` | "
                       f"{f.title.replace('|', '/')} | `{f.fingerprint}` |")
    return "\n".join(out)
