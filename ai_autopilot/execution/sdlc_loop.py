"""Closed-loop SDLC engine (Phase 1) — the impure orchestrator.

Drives ONE work item through a profile-selected, ordered list of SDLC stages. For
each stage it runs a skill in the item's single scratch worktree, commits what the
stage produced, gathers objective signals, gates the run with ``score_run``, and
lets the PURE ``decide`` advance / revise (under one shared budget) / escalate.
Progress is persisted after every step so a crash resumes mid-loop. On success the
caller applies the profile's handoff ADO state so the next machine's role picks the
item up. All the deciding lives in ``sdlc_plan``; this module only does the I/O.
"""

from __future__ import annotations

import contextlib
import re
import time
from pathlib import Path

from ai_autopilot.config import SdlcStage, Settings
from ai_autopilot.execution.claude_client import ClaudeRun, Usage
from ai_autopilot.execution.claude_executor import ClaudeExecutor, _branch_name
from ai_autopilot.execution.pr_scorer import score_badge_html, score_run
from ai_autopilot.execution.result_contract import clear_result, find_result
from ai_autopilot.execution.sdlc_plan import (
    ADVANCE,
    REVISE,
    StageSignals,
    decide,
    is_blocking,
    profile_stages,
    resolve_profile_name,
    resolve_stages,
    stage_score_input,
)
from ai_autopilot import activity
from ai_autopilot.data import QualityKind
from ai_autopilot.execution.test_gate import TestGate
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import ExecutionResult, WorkItemInfo

_PR_URL_RE = re.compile(r"https://[^\s)\"']*?/pullrequest/\d+", re.IGNORECASE)


class SdlcLoopEngine:
    """Runs the SDLC stage machine for one item. Reuses ``ClaudeExecutor`` for all
    scratch/git/Claude I/O and ``AutoReviewer`` for the review gate."""

    def __init__(
        self, executor: ClaudeExecutor, reviewer, ado, router, config: Settings, repo,
        quality=None,
    ) -> None:
        self._exec = executor
        self._reviewer = reviewer
        self._test_gate = TestGate(config)  # auto-test-gate signal for the review stage
        self._ado = ado
        self._router = router
        self._config = config
        self._repo = repo
        # Append-only quality log. Optional so the engine stays constructible without a
        # database (tests, dry runs); every write is guarded.
        self._quality = quality
        self._log = get_logger("execution.sdlc_loop")

    async def _record_quality(self, item_id: int, kind: str, **kw) -> None:
        """Append one quality event, if a log is wired. Never raises into the loop."""
        if self._quality is None:
            return
        with contextlib.suppress(Exception):
            await self._quality.record(work_item_id=item_id, kind=kind, **kw)

    async def run(self, item: WorkItemInfo) -> ExecutionResult:  # noqa: C901 - a bounded state loop
        cfg = self._config
        started = time.monotonic()
        workspace = cfg.workspace_directory
        repos = self._exec._allowed_repos(workspace)

        profile = resolve_profile_name(item.tags, item.work_item_type, cfg)
        stages = resolve_stages(item.tags, item.work_item_type, cfg)
        if not stages:
            return ExecutionResult.fail(item.id, "sdlc", "no SDLC stages resolved")

        # Resume from persisted progress (config-drift safe: re-use the stored profile).
        st = await self._repo.load(item.id)
        if st is not None and st.branch:
            profile = st.profile or profile
            stages = self._stages_for(profile) or stages
            stage_index, iterations, branch = st.stage_index, st.iterations, st.branch
            signals = StageSignals.from_json(st.signals_json)
            self._log.info(
                "sdlc resume", id=item.id, profile=profile, stage=stage_index, iter=iterations
            )
        else:
            stage_index, iterations, branch = 0, 0, _branch_name(item)
            signals = StageSignals()

        scratch = await self._exec._acquire_agent_scratch(item.id, repos)
        run_dir = scratch or workspace
        # An SDLC item is many Claude calls; History reports per ITEM, so the run
        # detail is accumulated rather than reduced to a token count on the way past.
        usage = Usage()
        touched: set[str] = set()
        pr_url: str | None = None
        try:
            clear_result(run_dir, item.id)
            # Stream what each stage does to the dashboard's Live activity feed
            # (kept in the real workspace, where the dashboard reads it).
            activity.clear(workspace, item.id)
            activity.append(
                workspace, item.id,
                f"🔁 SDLC start · profile '{profile}' · {len(stages)} stage(s)",
            )
            if scratch:
                await self._exec.prepare_stage_branch(scratch, repos, branch)

            while stage_index < len(stages):
                stage = stages[stage_index]
                run_text, is_error, stage_run = await self._run_stage(
                    item, stage, run_dir, scratch, repos, branch, touched
                )
                usage.add(stage_run)

                self._absorb(signals, stage, run_text, is_error)
                agent = find_result(run_dir, item.id)
                if agent is not None:
                    signals.needs_human = signals.needs_human or agent.needs_human
                    if agent.pr_url:
                        pr_url, signals.has_pr = agent.pr_url, True
                if stage.produces_pr:
                    found = _PR_URL_RE.search(run_text or "")
                    if found:
                        pr_url, signals.has_pr = found.group(0), True

                if signals.needs_human:
                    return await self._escalate(
                        item, profile, stage_index, iterations, branch, signals,
                        usage, pr_url, started,
                        reason=f"agent needs human at '{stage.name}'",
                    )

                signals.completed = not signals.had_error  # for a sensible headline badge
                score = score_run(
                    stage_score_input(signals),
                    auto_min=cfg.pr_score_auto_min, review_min=cfg.pr_score_review_min,
                )
                await self._comment_badge(item, stage, score)
                blocking = is_blocking(stage, signals)
                decision = decide(stage, blocking, iterations, cfg.sdlc_max_iterations)

                if decision == ADVANCE:
                    stage_index += 1
                elif decision == REVISE:
                    iterations += 1
                    self._log.info(
                        "sdlc revise", id=item.id, stage=stage.name, iteration=iterations
                    )
                    # The loop's own row is DELETED on success (`_repo.clear` below), so
                    # a completed item leaves no record of how many rounds it took.
                    await self._record_quality(
                        item.id, QualityKind.SDLC_ITERATION, value=iterations,
                        stage=stage.name, actor="autopilot",
                        detail=f"score {score.score} below bar at stage '{stage.name}'",
                    )
                else:  # ESCALATE — shared budget spent
                    return await self._escalate(
                        item, profile, stage_index, iterations, branch, signals,
                        usage, pr_url, started,
                        reason=f"budget exhausted at '{stage.name}' (score {score.score})",
                    )

                await self._repo.save(
                    item.id, profile=profile, stage_index=stage_index, iterations=iterations,
                    branch=branch, signals_json=signals.to_json(),
                )

            # All stages passed.
            signals.completed = True
            await self._repo.clear(item.id)
            activity.append(workspace, item.id, f"✅ SDLC completed · profile '{profile}'")
            self._log.info(
                "sdlc completed", id=item.id, profile=profile,
                tokens=usage.tokens, model=usage.model_label,
            )
            res = ExecutionResult.ok(
                item.id, f"sdlc:{profile}", f"SDLC profile '{profile}' completed"
            )
            res.branch_name = branch
            res.pr_url = pr_url
            res.pr_urls = [pr_url] if pr_url else []
            usage.apply(res)
            res.duration_seconds = time.monotonic() - started
            return res

        except TimeoutError:
            mins = cfg.task_timeout_minutes
            self._log.error("sdlc timed out", id=item.id, minutes=mins)
            return self._fail(
                item, branch, usage, started, f"Timed out after {mins} minutes"
            )
        except Exception as exc:  # noqa: BLE001 — never leave the item stuck IN_PROGRESS
            self._log.error("sdlc crashed", id=item.id, error=str(exc))
            return self._fail(item, branch, usage, started, str(exc))
        finally:
            await self._exec.release_scratch(scratch)

    # ── stage execution ──────────────────────────────────────────────────────

    async def _run_stage(
        self, item: WorkItemInfo, stage: SdlcStage, run_dir: str,
        scratch: str | None, repos: list[str], branch: str, touched: set[str],
    ) -> tuple[str, bool, ClaudeRun | None]:
        """Run one stage; return (output text, is_error, the ClaudeRun). Commits changes,
        runs AutoReviewer for the review gate, and pushes + parses the PR for the
        pr stage."""
        ws = self._config.workspace_directory
        activity.append(ws, item.id, f"▶ stage: {stage.name} ({stage.role})")
        # Review stage: AutoReviewer is the structured gate signal (no separate skill run).
        if stage.role == "review":
            primary = self._primary_dir(run_dir, scratch, touched, repos)
            review = await self._reviewer.review(primary)
            self._pending_review = review  # consumed in _absorb
            activity.append(ws, item.id, "🔍 auto-review " + ("passed" if review.passed else "found issues"))
            # Same gate stage runs the test suite; its result feeds signals.ci_passed
            # (sdlc_plan hard-fails on ci_passed is False).
            if review.critical_issues or review.warnings:
                findings = review.critical_issues + review.warnings
                await self._record_quality(
                    item.id, QualityKind.REVIEW_FINDING, value=len(findings),
                    stage=stage.name, actor="auto-review",
                    detail=" | ".join(findings[:10]),
                )
            tests = await self._test_gate.run(primary)
            self._pending_tests = tests  # consumed in _absorb
            if tests.ran:
                activity.append(ws, item.id, "🧪 tests " + ("passed" if tests.passed else "FAILED"))
                if not tests.passed:
                    await self._record_quality(
                        item.id, QualityKind.TEST_FAILED, stage=stage.name,
                        actor="test-gate", detail=tests.summary,
                    )
            # The review gate makes its own Claude call; hand it back so its tokens are
            # billed to the item. Returning 0 here is what made an SDLC run report less
            # than it actually spent. None when auto-review is off or the call failed.
            return review.raw_output, False, review.run

        # For the pr stage, make sure the branch is on origin before /pr-create.
        if stage.produces_pr and scratch and touched:
            await self._exec.push_stage_branch(scratch, sorted(touched), branch)

        prompt = self._stage_prompt(item, stage, branch)
        run = await self._exec._run_claude(
            prompt, run_dir, on_event=lambda line: activity.append(ws, item.id, line)
        )
        if scratch and not stage.produces_pr:
            msg = f"sdlc({stage.name}): #{item.id}"
            committed = await self._exec.stage_commit(scratch, repos, msg)
            touched.update(committed.keys())
            self._pending_files = sum(len(v) for v in committed.values())
        else:
            self._pending_files = 0
        return run.text, run.is_error, run

    def _absorb(
        self, signals: StageSignals, stage: SdlcStage, run_text: str, is_error: bool
    ) -> None:
        signals.had_error = is_error  # current-stage state, not cumulative
        signals.files_changed += getattr(self, "_pending_files", 0)
        self._pending_files = 0
        review = getattr(self, "_pending_review", None)
        if review is not None and stage.role == "review":
            signals.review_passed = review.passed
            signals.review_critical = len(review.critical_issues)
            signals.review_warnings = len(review.warnings)
            self._pending_review = None
        tests = getattr(self, "_pending_tests", None)
        if tests is not None and stage.role == "review":
            signals.ci_passed = tests.passed if tests.ran else None
            self._pending_tests = None

    def _stage_prompt(self, item: WorkItemInfo, stage: SdlcStage, branch: str) -> str:
        """Brief for one stage. By default it states the goal and lets Claude choose
        the skill(s); a non-blank ``stage.skill`` pins a specific command."""
        skill = stage.skill
        if skill == "route":
            skill = self._router.route(item) or ""
        skill = skill.replace("{id}", str(item.id)) if skill else ""
        draft = " Open it as a DRAFT PR." if stage.produces_pr and self._config.pr_is_draft else ""
        head = (
            f"You are the '{stage.name}' ({stage.role}) stage of an autonomous SDLC loop "
            f"for work item #{item.id}, on branch '{branch}'. Do ONLY this stage's work."
        )
        if skill:
            body = f" Run this skill: {skill}.{draft}"
        else:
            goal = stage.goal or f"complete the {stage.role} work for this item"
            body = (
                f" Goal: {goal} Choose and run the most appropriate skill(s) in this "
                f"workspace for this stage — do not assume one.{draft}"
            )
        # A human steered this item via a comment (poller's comment-reaction loop) —
        # carry that guidance into EVERY stage as the top-priority instruction, so a
        # resumed loop acts on the latest direction rather than its original plan.
        guidance = ""
        if item.pending_comment:
            guidance = (
                "\n\n## ⚠️ Latest human guidance (highest priority — respond to THIS)\n"
                "A human left this comment as the most recent direction. Treat it as the "
                "top-priority instruction for this stage, overriding earlier assumptions "
                f"where they conflict:\n\n{item.pending_comment}"
            )
        return (head + body).strip() + guidance

    # ── helpers ──────────────────────────────────────────────────────────────

    def _stages_for(self, profile: str) -> list[SdlcStage]:
        if profile == "custom":
            return list(self._config.sdlc_stages)
        return profile_stages(profile, self._config)

    def _primary_dir(
        self, run_dir: str, scratch: str | None, touched: set[str], repos: list[str]
    ) -> str:
        if scratch:
            name = next(iter(sorted(touched)), None) or (repos[0] if repos else None)
            if name:
                return str(Path(scratch) / name)
        return run_dir

    async def _comment_badge(self, item: WorkItemInfo, stage: SdlcStage, score) -> None:
        if self._config.dry_run:
            return
        badge = score_badge_html(score)
        html = f"<div><b>🔁 SDLC · {stage.name} ({stage.role})</b></div>{badge}"
        try:
            await self._ado.add_comment(item.id, html)
        except Exception as exc:  # noqa: BLE001 — a comment must never break the loop
            self._log.warning("sdlc badge comment failed", id=item.id, error=str(exc))

    async def _escalate(
        self, item, profile, stage_index, iterations, branch, signals,
        usage, pr_url, started, *, reason: str,
    ) -> ExecutionResult:
        await self._repo.save(
            item.id, profile=profile, stage_index=stage_index, iterations=iterations,
            branch=branch, signals_json=signals.to_json(),
        )
        self._log.info("sdlc escalate", id=item.id, reason=reason)
        res = ExecutionResult(
            work_item_id=item.id, success=False, skill_used=f"sdlc:{profile}",
            error=reason, needs_human=True, branch_name=branch,
        )
        res.pr_url = pr_url
        usage.apply(res)
        res.duration_seconds = time.monotonic() - started
        return res

    def _fail(self, item, branch, usage, started, error: str) -> ExecutionResult:
        res = ExecutionResult.fail(item.id, "sdlc", error)
        res.branch_name = branch
        usage.apply(res)
        res.duration_seconds = time.monotonic() - started
        return res
