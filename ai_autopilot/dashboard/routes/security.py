"""Security scanner pages: findings, scans, verify, suppress, export."""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from ai_autopilot import activity, markdown_lite
from ai_autopilot import reports as reports_mod
from ai_autopilot import workspaces as workspaces_mod
from ai_autopilot.config import config_file_path
from ai_autopilot.container import Container
from ai_autopilot.dashboard import settings_form
from ai_autopilot.dashboard.common import (
    _BACKGROUND_RUNS,
    _FLASH_COOKIE,
    _TEMPLATES,
    _flash,
    _int_or_zero,
    _log,
    _take_flash,
    work_item_link_base,
)
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.logging_config import describe_exc

_CWE_URL = "https://cwe.mitre.org/data/definitions/{n}.html"


_OWASP_URL = {
    "API": "https://owasp.org/API-Security/editions/2023/en/0x11-t10/",
    "A": "https://owasp.org/Top10/",
}


def _tool_summary(tools_json: str) -> str:
    try:
        data = json.loads(tools_json or "{}")
    except (TypeError, ValueError):
        return ""
    return ", ".join(
        f"{k}✓" if not str(v).startswith(("skipped", "error")) else f"{k}✗"
        for k, v in data.items()
    )


def _tools_dict(tools_json: str) -> dict:
    try:
        data = json.loads(tools_json or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _cwe_link(cwe: str) -> str:
    digits = "".join(ch for ch in (cwe or "") if ch.isdigit())
    return _CWE_URL.format(n=digits) if digits else ""


def _owasp_link(owasp: str) -> str:
    key = "API" if (owasp or "").upper().startswith("API") else "A"
    return _OWASP_URL[key] if owasp else ""


def _readiness(cfg) -> list[dict]:
    """What this machine can actually run — shown ON the page, not only in doctor,
    because the person pressing ▶ Scan now is the one who needs to know that
    semgrep is missing and the AI pass has no key."""
    import os
    import shutil

    from ai_autopilot.security_scan.tools import registry

    sec = cfg.security_scan
    adapters = registry(sec)
    rows = []
    for name in ("builtin", "gitleaks", "semgrep", "sca"):
        ok = adapters[name].available()
        rows.append({
            "name": name, "ok": ok, "on": name in sec.tools,
            "note": ("always available" if name == "builtin" else
                     ("installed" if ok else "not installed — skipped")),
        })
    key_ok = bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
    rows.append({"name": "ai", "ok": key_ok or sec.ai_mode == "off", "on": sec.ai_mode != "off",
                 "note": (f"mode {sec.ai_mode}" + ("" if key_ok else " — no ANTHROPIC_API_KEY"))
                 if sec.ai_mode != "off" else "off"})
    rows.append({"name": "verify", "ok": cfg.use_worktrees and key_ok, "on": sec.verify_enabled,
                 "note": ("worktrees + key ok" if (cfg.use_worktrees and key_ok)
                          else "needs use_worktrees and an API key")})
    rows.append({"name": "ado bugs", "ok": bool(cfg.ado_pat) and cfg.autonomy_level != "report",
                 "on": sec.file_bugs,
                 "note": ("ready" if cfg.ado_pat and cfg.autonomy_level != "report"
                          else "needs ADO PAT and autonomy ≠ report")})
    rows.append({"name": "git", "ok": bool(shutil.which("git")), "on": True,
                 "note": "for --scope diff and gitleaks history"})
    return rows


def _scan_progress(repo: str):
    from ai_autopilot.security_scan import progress

    return progress.get(repo) if repo else None


def _security_back(row) -> str:
    return f"/dashboard/security?repo={quote(row.repo or '')}"


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    # ── Security findings (lifecycle of what the scanners found) ────────────────
    _SCANS_RUNNING: set[str] = set()

    @router.get("/security", response_class=HTMLResponse)
    async def security_page(
        request: Request, repo: str = "", status: str = "open", severity: str = "",
        tool: str = "", q: str = "", new: str = "",
    ):
        c: Container = request.app.state.container
        cfg = c.config
        sec = cfg.security_scan
        repos = await c.security_repo.repos()
        # A scannable repo the page can offer even before the first scan.
        default_repo = cfg.repo_working_directory
        if not repo and len(repos) == 1:
            repo = repos[0]
        elif not repo and not repos and default_repo:
            repo = str(Path(default_repo).resolve())  # noqa: ASYNC240 — local path math
        new_since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=7)
        rows = await c.security_repo.list_findings(
            repo=repo, status=status or "open", severity=severity, tool=tool, limit=500,
            q=q, new_since=(new_since if new else None),
        )
        counts = await c.security_repo.counts(repo=repo)
        scans = await c.security_repo.recent_scans(limit=30, repo=repo)
        open_counts = counts.get("open", {})
        trend = [
            {
                "id": s.id, "when": s.started_at.strftime("%m-%d %H:%M") if s.started_at else "",
                "trigger": s.trigger, "critical": s.critical_count, "high": s.high_count,
                "medium": s.medium_count, "low": s.low_count,
                "total": s.critical_count + s.high_count + s.medium_count + s.low_count,
                "passed": bool(s.gate_passed), "new": s.new_count, "fixed": s.fixed_count,
            }
            for s in reversed(scans) if s.status != "running"
        ]
        all_rows = await c.security_repo.list_findings(repo=repo, status="all", limit=2000)
        tools = sorted({r.tool for r in all_rows if r.tool})
        sev_counts = {s: 0 for s in reports_mod.SEVERITIES}
        for r in all_rows:
            if r.status == (status or "open") or status == "all":
                sev_counts[r.severity] = sev_counts.get(r.severity, 0) + 1
        prog = _scan_progress(repo)
        running_row = next((s for s in scans if s.status == "running"), None)
        # Repos the form can offer: every one already scanned, plus the git repos found
        # in each configured workspace — so the first scan is a pick, not a path to type.
        suggestions = list(repos)
        with contextlib.suppress(Exception):
            from ai_autopilot.workspace import discover_repos
            for ws in workspaces_mod.resolve(cfg):
                root = getattr(ws, "directory", "") or ""
                for name in discover_repos(root):
                    full = str(Path(root) / name)
                    if full not in suggestions:
                        suggestions.append(full)
        flash = _take_flash(request)
        response = _TEMPLATES.TemplateResponse(
            request, "security.html",
            _ctx(request, "security", flash=flash,
                 rows=rows, repos=repos, repo=repo, status=status or "open",
                 severity=severity, tool=tool, tools=tools, q=q, new_only=bool(new),
                 repo_label=(Path(repo).name if repo else "all repos"),
                 open_counts=open_counts, sev_counts=sev_counts,
                 status_counts={k: sum(v.values()) for k, v in counts.items()},
                 severities=reports_mod.SEVERITIES, scans=scans, trend=trend,
                 new_since=new_since, sec=sec, sec_enabled=sec.enabled, ai_mode=sec.ai_mode,
                 # Not tied to a known repo: the path is typed or picked in the form
                 # and validated on submit. Tying it to the query string disabled the
                 # button on every fresh install, and typing a path never re-enabled it.
                 can_scan=sec.enabled and prog is None, suggestions=suggestions,
                 can_file=bool(cfg.ado_pat) and cfg.autonomy_level != "report",
                 can_verify=bool(cfg.use_worktrees),
                 item_link=work_item_link_base(cfg), tool_summary=_tool_summary,
                 readiness=_readiness(cfg), progress=prog, running_row=running_row,
                 feed_url=(f"/dashboard/activity/{activity.security_key(Path(repo).name)}"
                           if repo else ""),
                 all_tools=("builtin", "gitleaks", "semgrep", "sca"),
                 config_file=str(config_file_path())),
        )
        if flash:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        return response

    def _start_scan(c: Container, key: str, *, tools: list[str], ai_mode: str, scope: str,
                    verify: bool) -> bool:
        """Kick a scan of ``key`` in the background; False when one is already running."""
        from ai_autopilot.security_scan import progress

        cfg = c.config
        sec = cfg.security_scan
        if key in _SCANS_RUNNING or progress.get(key) is not None:
            return False
        # Registered BEFORE the redirect, so the page the browser lands on already shows
        # the progress bar — the task itself may not have started running yet.
        progress.start(key, "dashboard")
        _SCANS_RUNNING.add(key)

        async def _go() -> None:
            from ai_autopilot.security_scan.runner import ScanRequest, run_scan

            sec_view = sec.model_copy(update={"verify_enabled": verify or sec.verify_enabled})
            try:
                await run_scan(
                    ScanRequest(
                        repo=key, workspace=cfg.workspace_directory or str(Path(key).parent),
                        tools=tools or list(sec.tools), ai_mode=ai_mode,
                        ai_agents=list(sec.ai_agents), scope=scope or "full",
                        base_branch=cfg.base_branch or "main", fail_on=sec.fail_on,
                        trigger="dashboard", max_findings_per_tool=sec.max_findings_per_tool,
                        semgrep_config=list(sec.semgrep_config),
                        disabled_rules=list(sec.disabled_rules),
                        ignore_paths=list(sec.ignore_paths),
                    ),
                    executor=c.executor, security_repo=c.security_repo,
                    loop_report_repo=c.loop_report_repo, config=sec_view,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("dashboard scan failed", repo=key, error=describe_exc(exc))
            finally:
                _SCANS_RUNNING.discard(key)
                progress.finish(key)

        task = asyncio.create_task(_go())
        _BACKGROUND_RUNS.add(task)
        task.add_done_callback(_BACKGROUND_RUNS.discard)
        return True

    @router.post("/security/rescan")
    async def security_rescan(
        request: Request, repo: str = Form(""), scope: str = Form("full"),
        ai: str = Form(""), verify: str = Form(""),
    ):
        """The page's ▶ Scan now — with the choices that matter made on the page."""
        c: Container = request.app.state.container
        cfg = c.config
        form = await request.form()
        target = (repo or "").strip().strip('"') or cfg.repo_working_directory
        if not target:
            return _flash("/dashboard/security", "err_sec_repo_required")
        key = str(Path(target).resolve())  # noqa: ASYNC240 — local path math
        if not Path(key).is_dir():  # noqa: ASYNC240 — one local stat
            return _flash(f"/dashboard/security?repo={quote(key)}", "err_sec_repo_invalid")
        tools = [t for t in form.getlist("tools") if t]
        ai_mode = ai if ai in ("off", "fast", "deep") else cfg.security_scan.ai_mode
        if not _start_scan(c, key, tools=tools, ai_mode=ai_mode, scope=scope, verify=bool(verify)):
            return _flash(f"/dashboard/security?repo={quote(key)}", "sec_scan_busy")
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="security.scan",
            detail=f"{key} tools={','.join(tools) or 'config'} ai={ai_mode} scope={scope}",
        )
        return _flash(f"/dashboard/security?repo={quote(key)}", "sec_scan_started")

    @router.get("/security/progress", response_class=PlainTextResponse)
    async def security_progress(request: Request, repo: str = ""):
        """One line the page polls while a scan runs — stage · elapsed · tools so far."""
        prog = _scan_progress(str(Path(repo).resolve()) if repo else "")  # noqa: ASYNC240
        return prog.label() if prog else "idle"

    @router.post("/security/settings")
    async def security_settings(request: Request):
        """The handful of knobs that decide what a scan is — editable where the scans
        are looked at, saved to config.yaml and applied live."""
        c: Container = request.app.state.container
        form = await request.form()
        current = c.config.security_scan.model_dump()
        current.update({
            "enabled": bool(form.get("enabled")),
            "ai_mode": str(form.get("ai_mode") or "fast"),
            "fail_on": str(form.get("fail_on") or "high"),
            "tools": [t for t in form.getlist("tools") if t] or ["builtin"],
            "pr_gate_tools": [t for t in form.getlist("pr_gate_tools") if t],
            "file_bugs": bool(form.get("file_bugs")),
            "file_bugs_from": str(form.get("file_bugs_from") or "high"),
            "verify_enabled": bool(form.get("verify_enabled")),
            "verify_max_per_scan": _int_or_zero(form.get("verify_max_per_scan")) or 5,
            "disabled_rules": [r.strip() for r in str(form.get("disabled_rules") or "")
                               .replace("\n", ",").split(",") if r.strip()],
            "ignore_paths": [p.strip() for p in str(form.get("ignore_paths") or "")
                             .splitlines() if p.strip()],
        })
        settings_form.save_to_yaml(config_file_path(), {"security_scan": current})
        settings_form.apply_to_config(c.config, {"security_scan": current})
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="config.security_scan_updated",
            detail=f"ai={current['ai_mode']} fail_on={current['fail_on']} "
                   f"tools={','.join(current['tools'])}",
        )
        return _flash("/dashboard/security", "sec_settings_saved")

    @router.get("/security/export")
    async def security_export(
        request: Request, repo: str = "", fmt: str = "sarif", status: str = "open",
    ):
        """Download the current findings as SARIF (for ADO Advanced Security / VS Code)
        or JSON — the page's data, not a re-scan."""
        from starlette.responses import Response

        from ai_autopilot.security_scan import sarif as sarif_mod

        c: Container = request.app.state.container
        rows = await c.security_repo.list_findings(repo=repo, status=status, limit=5000)
        findings = [reports_mod.Finding(
            severity=r.severity, title=r.title, file=r.file, line=r.line, detail=r.detail or "",
            agent=r.agent or "", tool=r.tool or "", rule_id=r.rule_id or "", cwe=r.cwe or "",
            owasp=r.owasp or "", confidence=r.confidence or "", fingerprint=r.fingerprint,
            snippet=r.snippet or "",
            cve=r.cve or "", kev=bool(r.kev), kev_due=r.kev_due or "",
            kev_ransomware=bool(r.kev_ransomware), epss=r.epss,
            epss_percentile=r.epss_percentile,
        ) for r in rows]
        name = (Path(repo).name if repo else "all") + f"-{status}"
        if fmt == "json":
            body = json.dumps([f.as_dict() for f in findings], indent=2, ensure_ascii=False)
            media, ext = "application/json", "json"
        else:
            body = sarif_mod.dumps(findings, repo=repo)
            media, ext = "application/sarif+json", "sarif"
        return Response(body, media_type=media, headers={
            "Content-Disposition": f'attachment; filename="security-{name}.{ext}"'})

    @router.get("/security/f/{finding_id}", response_class=HTMLResponse)
    async def security_finding_page(request: Request, finding_id: int):
        """One finding, whole: enough to decide fix / suppress / file without opening
        the repo — snippet, classification links, history, PoC, and every action."""
        c: Container = request.app.state.container
        cfg = c.config
        row = await c.security_repo.get(finding_id)
        if row is None:
            return RedirectResponse("/dashboard/security", status_code=303)
        scans = [s for s in await c.security_repo.recent_scans(limit=50, repo=row.repo)
                 if s.status != "running"]
        seen_in = [s for s in scans if s.id and (
            row.fingerprint in json.loads(s.new_json or "[]")
            or row.fingerprint in json.loads(s.fixed_json or "[]")
            or (row.first_seen and s.started_at and s.started_at >= row.first_seen)
        )][:12]
        flash = _take_flash(request)
        response = _TEMPLATES.TemplateResponse(
            request, "security_finding.html",
            _ctx(request, "security", f=row, flash=flash, seen_in=seen_in,
                 cwe_link=_cwe_link(row.cwe), owasp_link=_owasp_link(row.owasp),
                 poc_html=markdown_lite.render(row.poc_md or "") if row.poc_md else "",
                 item_link=work_item_link_base(cfg),
                 can_file=bool(cfg.ado_pat) and cfg.autonomy_level != "report",
                 can_verify=bool(cfg.use_worktrees) and row.status == "open",
                 repo_name=Path(row.repo).name if row.repo else "",
                 verifying=(row.fingerprint in _VERIFYING)),
        )
        if flash:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        return response

    _VERIFYING: set[str] = set()

    @router.post("/security/{finding_id}/verify")
    async def security_verify(request: Request, finding_id: int):
        """▶ Verify on one finding: PoC in an isolated worktree, in the background."""
        c: Container = request.app.state.container
        row = await c.security_repo.get(finding_id)
        if row is None:
            return _flash("/dashboard/security", "err_sec_missing")
        back = f"/dashboard/security/f/{finding_id}"
        if not c.config.use_worktrees:
            return _flash(back, "err_sec_no_worktrees")
        if row.fingerprint in _VERIFYING:
            return _flash(back, "sec_verify_busy")

        async def _go() -> None:
            from ai_autopilot.security_scan import verify as verify_mod

            _VERIFYING.add(row.fingerprint)
            try:
                await verify_mod.verify_single(
                    row, executor=c.executor, security_repo=c.security_repo, repo=row.repo,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("verify failed", fp=row.fingerprint, error=describe_exc(exc))
            finally:
                _VERIFYING.discard(row.fingerprint)

        task = asyncio.create_task(_go())
        _BACKGROUND_RUNS.add(task)
        task.add_done_callback(_BACKGROUND_RUNS.discard)
        await c.audit_repo.record(actor="dashboard", source="dashboard",
                                  action="security.verify", target=row.fingerprint)
        return _flash(back, "sec_verify_started")

    @router.get("/security/scans/{scan_id}", response_class=HTMLResponse)
    async def security_scan_page(request: Request, scan_id: int):
        """One run: what ran, what it found new, what it closed, where the report is."""
        c: Container = request.app.state.container
        s = await c.security_repo.get_scan(scan_id)
        if s is None:
            return RedirectResponse("/dashboard/security", status_code=303)
        new_fps = json.loads(s.new_json or "[]")
        fixed_fps = json.loads(s.fixed_json or "[]")
        new_rows = await c.security_repo.by_fingerprints(s.repo, new_fps)
        fixed_rows = await c.security_repo.by_fingerprints(s.repo, fixed_fps)
        return _TEMPLATES.TemplateResponse(
            request, "security_scan.html",
            _ctx(request, "security", s=s, tools=_tools_dict(s.tools_json),
                 new_rows=new_rows, fixed_rows=fixed_rows,
                 repo_name=Path(s.repo).name if s.repo else "",
                 severities=reports_mod.SEVERITIES),
        )

    @router.post("/security/{finding_id}/suppress")
    async def security_suppress(
        request: Request, finding_id: int, reason: str = Form(""), until: str = Form(""),
        kind: str = Form("suppressed"),
    ):
        """Suppress (or mark false positive) — in the DB and in the workspace file, so
        the CLI in CI sees the same decision this page made."""
        from ai_autopilot.security_scan import suppressions as sup_mod

        c: Container = request.app.state.container
        row = await c.security_repo.get(finding_id)
        if row is None:
            return _flash("/dashboard/security", "err_sec_missing")
        if not reason.strip():
            return _flash(_security_back(row), "err_sec_reason")
        status = "false_positive" if kind == "false_positive" else "suppressed"
        expires = None
        with contextlib.suppress(ValueError):
            expires = datetime.strptime(until.strip(), "%Y-%m-%d").date() if until.strip() else None
        who = "dashboard"
        await c.security_repo.set_status(
            finding_id, status, reason=reason.strip(), by=who,
            until=datetime.combine(expires, datetime.min.time()) if expires else None,
        )
        workspace = c.config.workspace_directory or str(Path(row.repo).parent)
        with contextlib.suppress(Exception):
            s = sup_mod.load(workspace)
            prefix = "FP: " if status == "false_positive" else ""
            s.add(row.fingerprint, prefix + reason.strip(), by=who, expires=expires)
            sup_mod.save(s, workspace)
        await c.audit_repo.record(
            actor=who, source="dashboard", action="security.suppress",
            target=row.fingerprint, detail=f"{status}: {reason.strip()}",
        )
        return _flash(_security_back(row), "sec_suppressed")

    @router.post("/security/{finding_id}/reopen")
    async def security_reopen(request: Request, finding_id: int):
        from ai_autopilot.security_scan import suppressions as sup_mod

        c: Container = request.app.state.container
        row = await c.security_repo.get(finding_id)
        if row is None:
            return _flash("/dashboard/security", "err_sec_missing")
        await c.security_repo.set_status(finding_id, "open")
        workspace = c.config.workspace_directory or str(Path(row.repo).parent)
        with contextlib.suppress(Exception):
            s = sup_mod.load(workspace)
            if s.remove(row.fingerprint):
                sup_mod.save(s, workspace)
        await c.audit_repo.record(actor="dashboard", source="dashboard", action="security.reopen",
                                  target=row.fingerprint)
        return _flash(_security_back(row), "sec_reopened")

    @router.post("/security/{finding_id}/file")
    async def security_file_bug(request: Request, finding_id: int):
        from ai_autopilot.security_scan import ado_sync

        c: Container = request.app.state.container
        row = await c.security_repo.get(finding_id)
        if row is None:
            return _flash("/dashboard/security", "err_sec_missing")
        try:
            bug_id = await c.ado_for(row.project).create_work_item(
                title=ado_sync.bug_title(row), item_type="Bug", parent_id=None,
                tag="security, autopilot-security",
                description=ado_sync.bug_body(row, Path(row.repo).name, row.last_scan_id or 0),
                project=row.project,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("filing security bug failed", error=describe_exc(exc))
            bug_id = 0
        if not bug_id:
            return _flash(_security_back(row), "sec_file_failed")
        await c.security_repo.set_bug(finding_id, int(bug_id))
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="security.file_bug",
            target=row.fingerprint, detail=f"Bug #{bug_id}",
        )
        return _flash(_security_back(row), "sec_filed")

    return router
