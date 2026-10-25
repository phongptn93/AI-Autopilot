"""Filing what a QC run found onto the work item: Test Cases, Bugs and a results comment.

A mixin of :class:`~ai_autopilot.services.poller.AdoPollerService`, split out so the
QC bookkeeping reads on its own rather than as 160 lines in the middle of the poller.
The methods are the poller's own (tests call them on the service), and they need only
the attributes declared below.
"""

from __future__ import annotations

import contextlib
from html import escape as html_escape
from typing import TYPE_CHECKING, Any

from ai_autopilot import test_report
from ai_autopilot.logging_config import describe_exc
from ai_autopilot.models import ExecutionResult, WorkItemInfo

if TYPE_CHECKING:
    from ai_autopilot.config import Settings


class QcReporting:
    """Test Cases, Bugs and the results comment for a finished QC run."""

    _c: Any
    _config: Settings
    _log: Any
    _deferred: Any

    def _provider(self, project: str = "") -> Any:  # pragma: no cover — the poller's
        raise NotImplementedError

    async def _file_test_cases(self, item: WorkItemInfo, result: ExecutionResult) -> int:
        """File each test case the run wrote as an ADO Test Case item; returns how many.

        A QC run's test cases lived in a repo, reachable only through the pull request
        that carried them — so on the work item itself, where QC looks, there was
        nothing. Filed here rather than by the agent because it is a fact about the
        item, and because "did QC produce cases" should not depend on which MCP tools
        that session happened to have.

        Best-effort per case: a template that refuses the Test Case type or the Tests
        link must cost the run nothing — the work is done either way.
        """
        cfg = self._config
        cases = list(result.test_cases or [])
        if not cases or not cfg.qc_create_test_case_items or cfg.dry_run:
            return 0
        filed = 0
        for case in cases:
            try:
                new_id = await self._c.ado.create_test_case(
                    title=getattr(case, "title", "") or "",
                    steps=list(getattr(case, "steps", []) or []),
                    expected=getattr(case, "expected", "") or "",
                    preconditions=getattr(case, "preconditions", "") or "",
                    tests_item_id=item.id,
                    tag=cfg.processed_tag or "",
                    project=item.project or "",
                )
            except Exception as exc:  # noqa: BLE001 — bookkeeping must not sink the run
                self._log.warning(
                    "test case not filed", id=item.id, error=describe_exc(exc)
                )
                continue
            filed += 1 if new_id else 0
        self._log.info("test cases filed", id=item.id, filed=filed, offered=len(cases))
        return filed

    @staticmethod
    def _bug_title(case) -> str:
        """The Bug's title, and therefore its identity for the duplicate check.

        Prefixed so a human scanning the board can see at a glance which Bugs the
        autopilot filed, and so the duplicate check cannot collide with a Bug somebody
        wrote by hand that happens to share a case name.
        """
        return f"[QC] {(getattr(case, 'title', '') or '').strip()}"[:255]

    async def _bugs_already_filed(self, item: WorkItemInfo) -> set[str]:
        """Titles of Bugs already linked to this item, lowercased.

        Read off the ITEM rather than kept in our own ledger, on purpose. A ledger says
        "we filed it" and keeps saying so after somebody deletes the Bug, which is the
        one case where refiling is the correct answer. Reading the links asks the
        question that actually matters — is there a Bug on this item for this case —
        and answers it the same way the person looking at the board would.

        Both link directions are read: the child link is the one we prefer, but where a
        process template refuses it the Bug lands as Related and would otherwise be
        invisible here, so a re-run would file a second one.
        """
        ado = self._provider(item.project)
        linked: list = []
        with contextlib.suppress(Exception):
            linked = list(await ado.get_children(item.id))
        with contextlib.suppress(Exception):
            _preds, related = await ado.get_work_item_links([item.id])
            ids = sorted(related.get(item.id, set()))
            if ids:
                linked += list(await ado.get_work_items_by_ids(ids))
        return {
            (w.title or "").strip().lower() for w in linked
            if str(getattr(w, "work_item_type", "")).strip().lower() == "bug"
        }

    async def _file_bugs(self, item: WorkItemInfo, result: ExecutionResult) -> int:
        """File one Bug per FAILED case; returns how many were created.

        One per case rather than one listing them all, for the reason the audit-filing
        screen already gives: they are fixed by different people at different times, and
        a single Bug holding nine defects is closed when the easiest one is done.

        Only failures. A blocked case means QC could not reach a verdict — filing a Bug
        for it would assert a defect nobody has established.

        Best-effort per Bug, like ``_file_test_cases``: the run's work is done either
        way, and a template that refuses the type must not turn a good QC run into a
        failed one.
        """
        cfg = self._config
        failures = [
            r for r in (result.test_results or [])
            if getattr(r, "outcome", "") == "fail" and (getattr(r, "title", "") or "").strip()
        ]
        if not failures or not cfg.qc_create_bug_items or cfg.dry_run:
            return 0
        existing = await self._bugs_already_filed(item)
        filed = 0
        for case in failures:
            title = self._bug_title(case)
            if title.strip().lower() in existing:
                continue                      # already on the board — re-run, same case
            note = (getattr(case, "note", "") or "").strip()
            try:
                bug_id, link = await self._provider(item.project).create_bug(
                    title=title,
                    description=(
                        f"<div>Tìm thấy khi chạy QC trên "
                        f"<b>#{item.id}</b> — {html_escape(item.title or '')}.</div>"
                        + (f"<div>{html_escape(note)}</div>" if note else "")
                    ),
                    repro_steps=html_escape(note) if note else "",
                    parent_id=item.id,
                    tag=cfg.processed_tag or "",
                    project=item.project or "",
                )
            except Exception as exc:  # noqa: BLE001 — bookkeeping must not sink the run
                self._log.warning("bug not filed", id=item.id, error=describe_exc(exc))
                continue
            if bug_id:
                filed += 1
                existing.add(title.strip().lower())   # guard within this run too
                self._log.info("bug filed", id=item.id, bug=bug_id, link=link)
        self._log.info("bugs filed", id=item.id, filed=filed, failures=len(failures))
        return filed

    async def _report_test_results(self, item: WorkItemInfo, result: ExecutionResult) -> int:
        """Comment the outcomes of the cases this run executed; returns how many.

        Its OWN comment, not a line appended to the completion badge: twenty rows do not
        belong inside "PR created (draft)", and more to the point the badge is tied to
        whether a PR exists — which is exactly the condition a QC verdict must not
        depend on. A run that opens no PR is the normal shape of QC.

        Best-effort: a comment that fails to post must not sink a run whose work is done.
        """
        results = list(result.test_results or [])
        if not results or self._config.dry_run:
            return 0
        # Before the comment, and regardless of whether it posts: the hand-off later
        # reads these rows, not the comment.
        await self._deferred.record(item, results)
        report = test_report.render_comment(
            results, dashboard_url=self._config.dashboard_public_url or ""
        )
        if report.is_empty:
            return 0
        try:
            await self._provider(item.project).add_comment(item.id, report.html)
        except Exception as exc:  # noqa: BLE001 — reporting must not sink the run
            self._log.warning("test results not posted", id=item.id, error=describe_exc(exc))
            return 0
        self._log.info(
            "test results posted", id=item.id, total=report.total,
            passed=report.passed, failed=report.failed, blocked=report.blocked,
            pending_deploy=report.pending,
        )
        return report.total
