"""Enforce the two things a finished run owes the people who come after it.

**1. The PR must name its work item.** Not as an instruction in the brief — an
instruction is advice a model can drop on a long run, and when it does, nothing
notices. The link is checked against ADO after the fact and attached if it is
missing, so "which ticket shipped this change" is answerable for every PR the
autopilot opens rather than for most of them.

**2. Decisions the agent made on the team's behalf must reach a human.** The agent is
told to decide rather than ask; each of those decisions is a place where the written
item and the merged code quietly stopped agreeing. Left alone, that surfaces months
later as a defect nobody can explain against a specification nobody trusts. So the run
reports them, and this files them: a prefixed comment on the item, a prefixed comment
on the PR (the reviewer is the last person who can catch a wrong call before merge), a
tag the board filters on, and a row a BA can tick off.

Everything here is best-effort: neither obligation is worth failing a delivered run
over. A failure is logged and the run stands.
"""

from __future__ import annotations

from ai_autopilot import spec_drift
from ai_autopilot.container import Container
from ai_autopilot.execution.result_contract import Deviation
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from ai_autopilot.services.pr_feedback import parse_pr_url, parse_work_item_id


class SpecGuard:
    """Post-run obligations: link the PR, report the drift, record it for the BA."""

    def __init__(self, c: Container) -> None:
        self._c = c
        self._log = get_logger("services.spec_guard")
        # repo name (lower) → (repo guid, project guid); ADO's repo list is stable and
        # this runs once per finished PR, so one fetch per process is plenty.
        self._repos: dict[str, tuple[str, str]] | None = None

    @property
    def _config(self):
        return self._c.config

    # ── 1. the PR must name its work item ────────────────────────────────────

    async def _repo_ids(self, project: str) -> dict[str, tuple[str, str]]:
        if self._repos is None:
            self._repos = {
                (r.get("name") or "").strip().lower(): (
                    str(r.get("id") or ""), str((r.get("project") or {}).get("id") or "")
                )
                for r in await self._c.ado.get_repositories()
                if r.get("id")
            }
        return self._repos

    async def ensure_pr_links(self, item: WorkItemInfo, result: ExecutionResult) -> list[str]:
        """Make sure every PR from this run names ``item``. Returns the PRs it fixed."""
        cfg = self._config
        if not cfg.pr_require_work_item_link or cfg.dry_run:
            return []
        urls = [u for u in ([result.pr_url] + list(result.pr_urls or [])) if u]
        fixed: list[str] = []
        for url in dict.fromkeys(urls):          # de-dup, keep order
            parsed = parse_pr_url(url)
            if parsed is None:
                continue
            repo_name, pr_id = parsed
            # The WHOLE lookup is guarded, not just the link call: resolving the repo
            # list and reading the PR are ADO calls too, and this runs at the end of a
            # run that already delivered — a traceability check must never be what
            # turns a finished task into a failed one.
            try:
                # A PR whose branch names a DIFFERENT item (a batched run opens one per
                # item) must be linked to that item, not to the one being finalised.
                target = parse_work_item_id(await self._source_ref(repo_name, pr_id)) or item.id
                if await self._link_one(repo_name, pr_id, target):
                    fixed.append(url)
            except Exception as exc:  # noqa: BLE001 — never fail a delivered run on this
                self._log.warning("pr link check failed", id=item.id, pr=url, error=str(exc))
        return fixed

    async def _source_ref(self, repo_name: str, pr_id: int) -> str:
        ids = await self._repo_ids("")
        entry = ids.get(repo_name.strip().lower())
        if entry is None:
            return ""
        pr = await self._c.ado.get_pull_request(entry[0], pr_id)
        return (pr or {}).get("sourceRefName", "")

    async def _link_one(self, repo_name: str, pr_id: int, work_item_id: int) -> bool:
        ids = await self._repo_ids("")
        entry = ids.get(repo_name.strip().lower())
        if entry is None:
            self._log.warning("pr link: unknown repo", repo=repo_name, pr=pr_id)
            return False
        repo_guid, project_guid = entry
        linked = await self._c.ado.get_pull_request_work_items(repo_guid, pr_id)
        if work_item_id in linked:
            return False  # already correct — nothing to report
        ok = await self._c.ado.link_work_item_to_pr(
            work_item_id, project_guid, repo_guid, pr_id
        )
        if ok:
            self._log.info("pr was missing its work item — linked", id=work_item_id, pr=pr_id)
        return ok

    # ── 2. decisions the agent made must reach a human ───────────────────────

    async def report_drift(
        self, item: WorkItemInfo, result: ExecutionResult, deviations: list[Deviation]
    ) -> int:
        """File the run's deviations. Returns how many were filed (0 = nothing to do)."""
        cfg = self._config
        found = [d for d in deviations if not d.is_empty]
        if not (cfg.spec_drift_enabled and found) or cfg.dry_run:
            return 0

        # A rework re-runs the agent, which re-reports the decisions it already
        # reported; without this the item collects one identical notice per revision
        # and the BA stops reading them.
        try:
            existing = await self._c.ado.get_work_item_comments(item.id)
            if spec_drift.already_reported([str(c.get("text", "")) for c in existing]):
                self._log.info("spec drift already reported — not repeating", id=item.id)
                return 0
        except Exception as exc:  # noqa: BLE001 — a failed check must not suppress the notice
            self._log.warning("drift dedup check failed", id=item.id, error=str(exc))

        notice = spec_drift.render_comment(
            found, pr_url=result.pr_url or "", tag=cfg.spec_drift_tag,
            dashboard_url=self._dashboard_url(),
        )
        try:
            await self._c.ado.add_comment(item.id, notice.html)
            if cfg.spec_drift_tag:
                await self._c.ado.add_tag(item.id, cfg.spec_drift_tag)
        except Exception as exc:  # noqa: BLE001 — the run is already delivered
            self._log.warning("drift notice failed", id=item.id, error=str(exc))
            return 0
        await self._comment_on_pr(result, found)
        await self._record(item, result, found)
        self._log.info(
            "spec drift reported", id=item.id, count=notice.count, kinds=list(notice.kinds),
        )
        return notice.count

    def _dashboard_url(self) -> str:
        base = (self._config.dashboard_public_url or "").rstrip("/")
        return f"{base}/dashboard/specs" if base else ""

    async def _comment_on_pr(self, result: ExecutionResult, found: list[Deviation]) -> None:
        """Put the notice where the reviewer will see it, not only on the work item."""
        parsed = parse_pr_url(result.pr_url or "")
        if parsed is None:
            return
        repo_name, pr_id = parsed
        try:
            entry = (await self._repo_ids("")).get(repo_name.strip().lower())
            if entry is None:
                return
            await self._c.ado.add_pull_request_comment(
                entry[0], pr_id, spec_drift.render_pr_comment(found)
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("drift PR comment failed", pr=pr_id, error=str(exc))

    async def _record(
        self, item: WorkItemInfo, result: ExecutionResult, found: list[Deviation]
    ) -> None:
        repo = self._c.spec_drift_repo
        if repo is None:
            return
        try:
            await repo.add(item, result.pr_url or "", found)
        except Exception as exc:  # noqa: BLE001 — the ADO comment is the durable record
            self._log.warning("drift not recorded", id=item.id, error=str(exc))

    # ── 3. a human says the specification is back in line ────────────────────

    async def mark_resolved(self, work_item_id: int, by: str = "") -> int:
        """Close out an item's drifts: clear the tag and say so on the item.

        The tag has to go, or the board keeps showing work that is finished; the comment
        has to go on, or nobody reading the item later can tell whether the earlier
        notice was ever acted on.
        """
        cfg = self._config
        repo = self._c.spec_drift_repo
        count = await repo.resolve(work_item_id, by) if repo is not None else 0
        if cfg.dry_run:
            return count
        if cfg.spec_drift_tag:
            await self._c.ado.remove_tag(work_item_id, cfg.spec_drift_tag)
        await self._c.ado.add_comment(
            work_item_id, spec_drift.render_resolved_comment(count, by)
        )
        self._log.info("spec drift resolved", id=work_item_id, count=count, by=by)
        return count
