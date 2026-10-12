"""Runs Review Studio sessions: one read-only agent per review, resumable for follow-ups.

Deliberately NOT the executor's work-item path. A review has no branch, no PR and no
work-item state to move; it is a read of the checkout as it is. So it runs in the main
workspace like the Teams agent, with the same hard read-only tool allowlist — Read,
Grep, Glob and the ADO MCP read tools — which is the safety, not the filesystem.

Runs are detached tasks held in a set (asyncio keeps only weak references to bare
tasks). A restart cannot resume them, so startup fails whatever was left running —
a review stuck at "running" forever is worse than one marked interrupted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from ai_autopilot import review_docs, review_snapshot, spec_library
from ai_autopilot import review_studio as rs
from ai_autopilot.logging_config import describe_exc, get_logger

_log = get_logger(__name__)
_TASKS: set[asyncio.Task] = set()
# Review worktrees live outside the workspace: nothing the user greps, builds or
# commits may ever stumble on a second copy of their repos.
TREES_ROOT = Path(tempfile.gettempdir()) / "ai-autopilot-review-trees"
_ATTR = "_review_studio"


def studio_for(container) -> ReviewStudio:
    """The one studio per container — built on first use, so tests and pages share it."""
    studio = getattr(container, _ATTR, None)
    if studio is None:
        studio = ReviewStudio(container)
        setattr(container, _ATTR, studio)
    return studio


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


class ReviewStudio:
    def __init__(self, container) -> None:
        self._c = container
        cfg = container.config
        self._gate = asyncio.Semaphore(max(1, int(getattr(cfg, "review_max_concurrent", 2) or 2)))

    # ── workspaces ──────────────────────────────────────────────────────────
    def workspaces(self) -> list:
        """Every workspace with a folder, default first (workspaces.resolve)."""
        from ai_autopilot import workspaces as workspaces_mod

        out, seen = [], set()
        for ws in workspaces_mod.resolve(self._c.config):
            d = (ws.directory or "").strip()
            if ws.enabled and d and d.lower() not in seen:
                seen.add(d.lower())
                out.append(ws)
        return out

    def all_workspaces(self) -> list:
        """Every enabled workspace — what the sidebar lists — folder or not.

        :meth:`workspaces` is the RUNNABLE set (has a folder, one per folder) and stays
        that for sweeps; the hub shows this one, because a workspace that silently
        vanished from the page read as "the studio does not support it".
        """
        from ai_autopilot import workspaces as workspaces_mod

        return [ws for ws in workspaces_mod.resolve(self._c.config) if ws.enabled]

    def workspace_dir(self, ws_id: str | None) -> str:
        """The folder a review of workspace ``ws_id`` reads; the default one when blank
        or unknown (a renamed workspace must not make an old review unrunnable).

        A KNOWN workspace without a folder raises: falling back to the default folder
        would review some other team's code and present it as this one's.
        """
        for ws in self.all_workspaces():
            if ws_id and ws.id == ws_id:
                if not (ws.directory or "").strip():
                    raise ValueError(f"Workspace «{ws.label}» chưa đặt thư mục code — "
                                     "đặt ở trang Workspaces trước khi rà soát.")
                return ws.directory
        return self._c.config.workspace_directory

    # ── starting ────────────────────────────────────────────────────────────
    async def start_spec(self, rel: str, *, focus: str = "", by: str = "",
                         doc_type: str = "spec", workspace: str = "") -> int:
        ws = self.workspace_dir(workspace)
        path = spec_library.resolve(ws, rel)
        if path is None:
            raise ValueError(f"Không tìm thấy spec '{rel}' trong workspace")
        text = spec_library.read(path)
        title = spec_library.title_of(text, path.suffix.lstrip(".").lower(), path.stem)
        item_id = spec_library.item_ref(text[:6000])
        rid = await self._c.review_repo.create(
            kind=rs.KIND_SPEC, target=rel, title=title, work_item_id=item_id,
            focus=focus, requested_by=by, doc_type=doc_type, workspace=workspace)
        prompt = rs.spec_prompt(rel, text[: self._cap()], item_id=item_id, focus=focus,
                                doc_type=doc_type)
        _spawn(self._run(rid, rs.KIND_SPEC, prompt))
        return rid

    async def start_document(self, filename: str, data: bytes | None, *, doc_type: str = "brd",
                             focus: str = "", item_id: int = 0, by: str = "",
                             text: str | None = None, workspace: str = "") -> int:
        """Review an UPLOADED document (PRD, BRD, SRS…). Raises DocumentError on bad files.

        The text is extracted and stored; the file itself is not kept and never placed in
        the workspace, so nothing a BA uploads becomes readable to other runs.
        """
        self.workspace_dir(workspace)               # refuse a workspace with no code folder
        body = text if text is not None else review_docs.extract(filename, data or b"")
        title = review_docs.title_of(body, filename)
        item_id = item_id or spec_library.item_ref(body[:6000])
        doc_type = doc_type if doc_type in rs.DOC_TYPES else "other"
        rid = await self._c.review_repo.create(
            kind=rs.KIND_SPEC, target=f"upload:{filename}"[:1000], title=title,
            work_item_id=item_id, focus=focus, requested_by=by, doc_type=doc_type,
            source_text=body[: self._cap()], workspace=workspace)
        prompt = rs.spec_prompt(f"(tải lên) {filename}", body[: self._cap()], item_id=item_id,
                                focus=focus, doc_type=doc_type)
        _spawn(self._run(rid, rs.KIND_SPEC, prompt))
        return rid

    async def start_report(self, report_id: int, *, focus: str = "", by: str = "",
                           workspace: str = "") -> int:
        self.workspace_dir(workspace)
        row = await self._c.loop_report_repo.get(report_id)
        if row is None:
            raise ValueError(f"Không có báo cáo #{report_id}")
        try:
            findings = json.loads(row.findings_json or "[]")
        except ValueError:
            findings = []
        title = f"Báo cáo #{report_id} · {row.loop_name or 'audit'}"
        rid = await self._c.review_repo.create(
            kind=rs.KIND_REPORT, target=f"report:{report_id}", title=title,
            focus=focus, requested_by=by, workspace=workspace)
        _spawn(self._run(rid, rs.KIND_REPORT, rs.report_prompt(title, findings, focus=focus)))
        return rid

    async def start_question(self, question: str, *, by: str = "", workspace: str = "") -> int:
        q = question.strip()
        if not q:
            raise ValueError("Câu hỏi trống")
        self.workspace_dir(workspace)
        rid = await self._c.review_repo.create(
            kind=rs.KIND_QUESTION, target=q[:1000], title=q[:400], requested_by=by,
            workspace=workspace)
        _spawn(self._run(rid, rs.KIND_QUESTION, rs.question_prompt(q)))
        return rid

    async def rerun(self, review_id: int, *, by: str = "") -> int:
        row = await self._c.review_repo.get(review_id)
        if row is None:
            raise ValueError("Không có phiên rà soát này")
        if row.kind == rs.KIND_SPEC and row.target.startswith("upload:"):
            return await self.start_document(
                row.target.split(":", 1)[1], None, text=row.source_text, doc_type=row.doc_type,
                focus=row.focus, item_id=row.work_item_id, by=by, workspace=row.workspace or "")
        if row.kind == rs.KIND_SPEC:
            return await self.start_spec(row.target, focus=row.focus, by=by,
                                         doc_type=row.doc_type, workspace=row.workspace or "")
        if row.kind == rs.KIND_REPORT:
            return await self.start_report(int(row.target.split(":", 1)[1]), focus=row.focus,
                                           by=by, workspace=row.workspace or "")
        return await self.start_question(row.target, by=by, workspace=row.workspace or "")

    async def ask(self, review_id: int, question: str, *, by: str = "") -> int:
        """A follow-up in the review's own agent session."""
        row = await self._c.review_repo.get(review_id)
        if row is None or not question.strip():
            raise ValueError("Không có phiên rà soát hoặc câu hỏi trống")
        await self._c.review_repo.add_message(review_id, "user", question.strip(), author=by)
        mid = await self._c.review_repo.add_message(review_id, "assistant", "", status="running")
        _spawn(self._followup(row, mid, question))
        return mid

    async def review_all_specs(self, *, by: str = "lịch định kỳ") -> list[int]:
        """Queue a review of every spec in every workspace — the scheduled sweep."""
        ids = []
        for ws in self.workspaces() or [None]:
            folder = ((ws.directory if ws else self._c.config.workspace_directory) or "").strip()
            if not folder:      # Path("") would sweep the autopilot's own source tree
                continue
            for spec in spec_library.discover(folder):
                with contextlib.suppress(Exception):
                    ids.append(await self.start_spec(spec.rel, by=by,
                                                     workspace=ws.id if ws else ""))
        return ids

    # ── running ─────────────────────────────────────────────────────────────
    def _safe(self) -> bool:
        return bool(getattr(self._c.config, "review_safe_mode", True))

    def _cap(self) -> int:
        return int(getattr(self._c.config, "review_spec_max_chars", 60000) or 60000)

    async def challenge(self, review_id: int, index: int, note: str, *, by: str = "") -> int:
        """A person disputes one matrix row; the agent re-checks it in the same session."""
        row = await self._c.review_repo.get(review_id)
        result = rs.load(row.result_json) if row else None
        if row is None or result is None or not (0 <= index < len(result.traces)) \
                or not note.strip():
            raise ValueError("Không có mục này hoặc phản biện trống")
        t = result.traces[index]
        await self._c.review_repo.add_message(
            review_id, "user", f"⚖️ Phản biện {t.ref}: {note.strip()}", author=by)
        mid = await self._c.review_repo.add_message(review_id, "assistant", "", status="running")
        _spawn(self._challenge(review_id, index, note, mid, by))
        return mid

    def _cwd(self, row) -> str | None:
        """Where follow-ups run: the review's own tree while it still exists, else the
        review's workspace folder."""
        path = getattr(row, "tree_path", None)
        if path and Path(path).is_dir():
            return path
        try:
            return self.workspace_dir(getattr(row, "workspace", None)) or None
        except ValueError:
            return None

    async def _agent(self, prompt: str, *, resume: str | None = None, turns: int | None = None,
                     cwd: str | None = None):
        from ai_autopilot.execution.claude_client import run_claude
        from ai_autopilot.execution.claude_executor import _load_mcp_servers
        from ai_autopilot.teams_agent import _agent_allowed_tools

        cfg = self._c.config
        base = cwd or cfg.workspace_directory
        mcp = _load_mcp_servers(base) if base else None   # a review tree carries a copy
        return await run_claude(
            prompt, base or tempfile.gettempdir(),
            timeout_seconds=float(getattr(cfg, "review_timeout_seconds", 900) or 900),
            model=cfg.claude_model or None,
            max_turns=turns or int(getattr(cfg, "review_max_turns", 80) or 80),
            allowed_tools=_agent_allowed_tools(mcp),   # read-only, hard limit
            # Belt and braces: nothing that writes, executes, or reaches the network.
            # A review reads code and documents a BA uploaded; with web tools it could
            # be talked into sending either somewhere.
            disallowed_tools=["Bash", "Write", "Edit", "MultiEdit", "NotebookEdit",
                              "WebFetch", "WebSearch", "Task"],
            mcp_servers=mcp, resume=resume,
            setting_sources=["project"],               # the workspace's CLAUDE.md/rules
            effort=(getattr(cfg, "review_effort", "") or None),
        )

    async def _run(self, rid: int, kind: str, prompt: str) -> None:
        repo = self._c.review_repo
        async with self._gate:
            t0 = time.monotonic()
            cwd = None
            row = await repo.get(rid)
            try:
                ws = self.workspace_dir(getattr(row, "workspace", None))
            except ValueError as exc:   # its folder was cleared after it was queued
                await repo.update(rid, status="failed", error=str(exc)[:2000])
                return
            cwd = ws
            if ws:
                await repo.update(rid, status="preparing")
                latest = (getattr(self._c.config, "review_code_source", "latest") or "latest") \
                    == "latest"
                try:
                    cwd, states = await asyncio.to_thread(
                        review_snapshot.prepare, ws, TREES_ROOT, rid, latest=latest)
                    await repo.update(rid, snapshot_json=review_snapshot.to_json(states),
                                      tree_path=cwd if cwd != ws else None)
                except Exception as exc:  # noqa: BLE001 — a review still runs on the checkout
                    _log.warning("review snapshot failed", id=rid, error=describe_exc(exc))
                    cwd = ws
            await repo.update(rid, status="running")
            try:
                run = await self._agent(prompt, cwd=cwd)
            except Exception as exc:  # noqa: BLE001 — a failed review is a row, not a crash
                _log.warning("review failed", id=rid, error=describe_exc(exc))
                await repo.update(rid, status="failed", error=describe_exc(exc)[:2000],
                                  finished_at=datetime.now(UTC),
                                  duration_seconds=time.monotonic() - t0)
                return
            result, prose = rs.parse(run.text or "", kind, safe=self._safe())
            await repo.update(
                rid, status="done" if not run.is_error else "failed",
                error=(run.text or "")[:2000] if run.is_error else None,
                verdict=(result.verdict or result.summary)[:1000],
                score=result.score(kind),
                counts_json=json.dumps(result.counts(kind)),
                result_json=result.to_json(), analysis_md=prose,
                session_id=run.session_id, cost_usd=run.cost_usd,
                duration_seconds=time.monotonic() - t0, finished_at=datetime.now(UTC),
            )
            _log.info("review finished", id=rid, kind=kind, score=result.score(kind),
                      rows=len(result.traces), parsed=result.parsed)
            await self._warn_on_drop(rid, kind)

    async def _followup(self, row, mid: int, question: str) -> None:
        repo = self._c.review_repo
        async with self._gate:
            try:
                run = await self._agent(rs.followup_prompt(question, row.kind),
                                        resume=row.session_id, turns=30, cwd=self._cwd(row))
            except Exception as exc:  # noqa: BLE001
                await repo.update_message(mid, status="failed",
                                          content=f"⚠️ Không trả lời được: {describe_exc(exc)}")
                return
            text, _ = rs.split_body(run.text or "")
            if self._safe():
                text = rs.redact(text)
            await repo.update_message(mid, status="done", content=text or "(không có nội dung)",
                                      cost_usd=run.cost_usd)
            if run.session_id and run.session_id != row.session_id:
                await repo.update(row.id, session_id=run.session_id)

    async def _challenge(self, rid: int, index: int, note: str, mid: int, by: str) -> None:
        repo = self._c.review_repo
        async with self._gate:
            row = await repo.get(rid)
            result = rs.load(row.result_json)
            try:
                run = await self._agent(rs.challenge_prompt(result.traces[index], note, row.kind),
                                        resume=row.session_id, turns=40, cwd=self._cwd(row))
            except Exception as exc:  # noqa: BLE001
                await repo.update_message(mid, status="failed",
                                          content=f"⚠️ Không kiểm lại được: {describe_exc(exc)}")
                return
            # Re-read: another challenge may have finished while this one ran.
            row = await repo.get(rid)
            result = rs.load(row.result_json)
            prose, entry = rs.apply_challenge(
                result, index, run.text or "", row.kind, note=note, by=by,
                at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M"), safe=self._safe())
            await repo.update(rid, result_json=result.to_json(), score=result.score(row.kind),
                              counts_json=json.dumps(result.counts(row.kind)))
            t = result.traces[index]
            label = rs.statuses_for(row.kind)
            head = {"accept": "✅ Chấp nhận phản biện", "reject": "↩️ Giữ kết luận",
                    "partial": "🟡 Chấp nhận một phần"}.get(entry["verdict"], "ℹ️ Đã kiểm lại")
            moved = (f" — {t.ref}: {label.get(entry['before'], ('', entry['before']))[1]} → "
                     f"{label.get(entry['after'], ('', entry['after']))[1]}"
                     if entry["before"] != entry["after"] else f" — {t.ref} giữ nguyên")
            body = f"**{head}{moved}.**" + (f" {entry['reason']}" if entry["reason"] else "")
            await repo.update_message(mid, status="done",
                                      content=body + ("\n\n" + prose if prose else ""),
                                      cost_usd=run.cost_usd)
            if run.session_id and run.session_id != row.session_id:
                await repo.update(rid, session_id=run.session_id)

    async def _warn_on_drop(self, rid: int, kind: str) -> None:
        """Coverage FELL since the last review of the same spec → say so, once."""
        if kind != rs.KIND_SPEC:
            return
        repo = self._c.review_repo
        row = await repo.get(rid)
        prev = await repo.previous(kind, row.target, rid) if row else None
        if not row or not prev or row.score is None or prev.score is None:
            return
        drop = float(getattr(self._c.config, "review_alert_drop_pct", 10) or 0)
        if drop and prev.score - row.score >= drop:
            _log.warning("spec coverage dropped", spec=row.target, before=prev.score,
                         after=row.score, review=rid)
            with contextlib.suppress(Exception):
                await self._c.notifier.broadcast_digest(
                    "🧭 Độ bao phủ spec giảm",
                    f"«{row.title}»: {prev.score:g}% → {row.score:g}% (phiên rà #{rid}). "
                    f"Xem /dashboard/review/{rid}")
