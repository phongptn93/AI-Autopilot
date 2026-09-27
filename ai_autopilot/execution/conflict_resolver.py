"""Resolve a PR's merge conflict: merge the target in, settle the hunks, prove it, push.

See ``ai_autopilot.pr_conflicts`` for why this is a merge and not a rebase. This module
is the mechanics, and its one rule is that the model's word is never the evidence:

1. ``git merge --no-ff --no-commit origin/<target>`` in an isolated checkout. A clean
   merge (ADO's view was stale) is committed and pushed with no model involved.
2. Lock files, binaries, or more conflicted files than ``pr_conflict_max_files`` →
   abort before spending a token; those need a person.
3. The agent edits the conflicted files only — headless (one SDK run, ``resolve``) or
   in a visible Remote-Control session a person can attach to and steer
   (``prepare_session`` → the session writes its verdict → ``finalize_session``),
   following ``execution_mode`` exactly as work items do.
4. Checked, objectively, before anything is committed — the SAME checks in both modes,
   run by this code and never by the session:
   - no conflict marker is left in any conflicted file
   - nothing OUTSIDE the conflicted files was touched (the scope guard)
   - the index has no unmerged path after staging
   - the resolution introduced no security finding that NEITHER side had
   - the repo's tests pass (when the test gate is enabled)
5. Commit the merge and push — never ``--force``. An interactive session is not held
   under the branch lock (it may last hours), so before pushing it also confirms the
   branch on origin is still the commit the merge started from.

Any failure aborts the merge and releases the checkout, so the PR branch on origin is
exactly as it was.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from ai_autopilot.execution.result_contract import clear_result, find_result
from ai_autopilot.execution.test_gate import TestResult
from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.pr_conflicts import (
    BY_AGENT,
    BY_CLEAN_MERGE,
    ConflictResolution,
    ResolveContext,
    files_with_markers,
    parse_verdict,
    resolution_prompt,
    unresolvable,
)

_log = get_logger("execution.conflict_resolver")

_RUNS = Path(".autopilot") / "runs"


@dataclass
class PreparedSession:
    """An interactive resolution waiting on its session."""

    run_dir: str        # the scratch the console runs in (holds the worktree)
    key: str            # result / brief / state file stem, e.g. "conflict-12"
    session: str        # Remote-Control session name to attach to


class ConflictResolver:
    """Drives one resolution attempt through the executor's checkout machinery."""

    def __init__(self, executor, config) -> None:
        self._ex = executor
        self._config = config
        # Target test runs, by target commit: a red target fails EVERY PR merged into
        # it, and re-testing the same commit for each of them costs minutes apiece.
        self._baselines: dict[str, TestResult] = {}

    # ── headless ─────────────────────────────────────────────────────────────

    async def resolve(
        self, *, repo_path: str, branch: str, target_branch: str, ctx: ResolveContext,
    ) -> ConflictResolution:
        ex, cfg = self._ex, self._config
        started = time.monotonic()
        res = ConflictResolution()
        # Same rule as every other write to a shared checkout: workspace mode edits the
        # repo in place, so serialise per repo. Worktree mode isolates by itself.
        lock = ex._repo_lock(repo_path) if cfg.workspace_directory else None
        if lock is not None:
            await lock.acquire()
        ws = None
        wd = ""
        try:
            ws = await ex._acquire_workspace(
                repo_path, branch, target_branch, ctx.work_item_id or ctx.pr_id,
                existing_branch=True,
            )
            wd = ws.path
            conflicted, early = await self._start_merge(wd, branch, target_branch, ctx, res)
            if early is not None:
                return early

            before = await _touched(ex, wd)
            prompt = (f"Working tree: {wd}\n\n"
                      + resolution_prompt(ctx, conflicted, *await self._logs(wd, target_branch)))
            run = await ex._run_claude(prompt, ws.claude_cwd or wd, repo=wd)
            res.tokens = int(getattr(run, "input_tokens", 0) or 0) + int(
                getattr(run, "output_tokens", 0) or 0)
            verdict, reason = parse_verdict(getattr(run, "text", "") or "")
            if verdict == "needs-human":
                res.error = f"agent cần người quyết định: {reason or 'không nêu lý do'}"
                return res
            return await self._verify_commit_push(
                res, wd, conflicted, before, branch, target_branch, ctx, note=reason,
            )
        except Exception as exc:  # noqa: BLE001 — every failure must end in a clean abort
            _log.warning("conflict resolution failed", pr=ctx.pr_id, error=describe_exc(exc))
            res.success = False
            res.error = res.error or describe_exc(exc)[:300]
            return res
        finally:
            if wd and await _merge_in_progress(ex, wd):
                await ex._git(["merge", "--abort"], wd, check=False)
            if ws is not None:
                await ex._release_workspace(ws)
            if lock is not None:
                lock.release()
            res.duration_seconds = time.monotonic() - started

    # ── interactive ──────────────────────────────────────────────────────────

    async def prepare_session(
        self, *, repo_name: str, branch: str, target_branch: str, ctx: ResolveContext,
        key: str,
    ) -> PreparedSession | ConflictResolution:
        """Stage the merge in an isolated scratch and open a session on it.

        Returns a ``ConflictResolution`` instead when no session is needed or allowed:
        a clean merge (already committed and pushed), an early escalation, or no way
        to isolate the work (worktrees off) — never a session in the real checkout.
        """
        ex = self._ex
        res = ConflictResolution()
        scratch = await ex._acquire_agent_scratch(key, [repo_name], stable=True)
        if not scratch:
            res.error = ("không tạo được worktree tách biệt cho phiên interactive "
                         "(cần use_worktrees + workspace) — không mở phiên trên checkout thật")
            return res
        wd = str(Path(scratch) / repo_name)
        keep = False
        try:
            await ex._git(["fetch", "origin", branch, target_branch], wd, check=False)
            # Detached at the PR head: a local branch of that name may be checked out in
            # the main workspace, and the push names the remote ref explicitly anyway.
            await ex._git(["checkout", "--detach", f"origin/{branch}"], wd)
            base_sha = (await ex._git(["rev-parse", "HEAD"], wd)).strip()
            conflicted, early = await self._start_merge(wd, branch, target_branch, ctx, res)
            if early is not None:
                return early
            before = sorted(await _touched(ex, wd))
            ours, theirs = await self._logs(wd, target_branch)
            runs = Path(scratch) / _RUNS
            runs.mkdir(parents=True, exist_ok=True)
            # Everything finalize needs, on disk — a restart in between must not lose
            # the scope baseline or the commit the merge started from.
            (runs / f"{key}.state.json").write_text(json.dumps({
                "repo_name": repo_name, "branch": branch, "target": target_branch,
                "base_sha": base_sha, "conflicted": conflicted, "before": before,
                "ctx": ctx.__dict__,
            }), encoding="utf-8")
            clear_result(scratch, key)
            brief_rel = (_RUNS / f"{key}.brief.md").as_posix()
            (Path(scratch) / brief_rel).write_text(
                _session_brief(ctx, conflicted, ours, theirs, repo_name, key),
                encoding="utf-8",
            )
            session = f"autopilot-{key}"
            pid = ex._launch_console(
                scratch, session,
                f"Read and follow the instructions in {brief_rel}, then write the result "
                "JSON as instructed there.",
            )
            if pid is None:
                res.error = "không mở được console Claude Code cho phiên interactive"
                return res
            ex._write_session_handle(scratch, key, pid, session)
            keep = True
            _log.info("conflict session opened", pr=ctx.pr_id, session=session,
                      files=len(conflicted))
            return PreparedSession(run_dir=scratch, key=key, session=session)
        except Exception as exc:  # noqa: BLE001
            _log.warning("conflict session prepare failed", pr=ctx.pr_id,
                         error=describe_exc(exc))
            res.error = res.error or describe_exc(exc)[:300]
            return res
        finally:
            if not keep:
                if await _merge_in_progress(ex, wd):
                    await ex._git(["merge", "--abort"], wd, check=False)
                await ex.release_scratch(scratch)

    async def finalize_session(self, run_dir: str, key: str) -> ConflictResolution | None:
        """``None`` while the session is still working; otherwise verify, push, clean up.

        The session's verdict is only an input: ``completed`` still has to pass every
        check below, and nothing the session did outside the conflicted files survives.
        """
        agent = find_result(run_dir, key)
        if agent is None:
            return None
        ex = self._ex
        started = time.monotonic()
        res = ConflictResolution()
        try:
            state = json.loads((Path(run_dir) / _RUNS / f"{key}.state.json")
                               .read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            await self.cancel_session(run_dir, key)
            res.error = f"mất trạng thái phiên ({describe_exc(exc)}) — cần giải lại"
            return res
        wd = str(Path(run_dir) / state["repo_name"])
        ctx = ResolveContext(**{k: v for k, v in (state.get("ctx") or {}).items()
                                if k in ResolveContext.__dataclass_fields__})
        res.files = list(state.get("conflicted") or [])
        try:
            status = (agent.status or "").lower()
            if agent.needs_human or status in ("needs_human", "failed"):
                res.error = ("phiên cần người quyết định: "
                             + (agent.reason or agent.summary or "không nêu lý do"))
                return res
            # Nothing may land on a branch that moved while the session ran: the merge
            # was staged on base_sha, and pushing on top of a newer head would silently
            # re-conflict or drop the newcomer's intent. Stop and let it be redone.
            await ex._git(["fetch", "origin", state["branch"]], wd, check=False)
            head = (await ex._git(["rev-parse", f"origin/{state['branch']}"], wd,
                                  check=False)).strip()
            if head and head != state["base_sha"]:
                res.error = ("branch có commit mới trong lúc phiên chạy — giải lại trên bản "
                             "mới nhất để không đè lên thay đổi đó")
                return res
            return await self._verify_commit_push(
                res, wd, res.files, set(state.get("before") or []), state["branch"],
                state["target"], ctx, note=agent.summary or "",
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("conflict session finalize failed", key=key, error=describe_exc(exc))
            res.success = False
            res.error = res.error or describe_exc(exc)[:300]
            return res
        finally:
            await self.cancel_session(run_dir, key)
            res.duration_seconds = time.monotonic() - started

    async def cancel_session(self, run_dir: str, key: str) -> None:
        """Close the console, abort any merge, drop the scratch. Never raises."""
        ex = self._ex
        try:
            await ex.close_interactive(run_dir, key)
            for sub in Path(run_dir).iterdir() if Path(run_dir).is_dir() else []:  # noqa: ASYNC240 — local listing
                if sub.is_dir() and not sub.name.startswith(".") and await _merge_in_progress(
                        ex, str(sub)):
                    await ex._git(["merge", "--abort"], str(sub), check=False)
        except Exception as exc:  # noqa: BLE001
            _log.warning("conflict session cleanup failed", key=key, error=describe_exc(exc))
        await ex.release_scratch(run_dir)

    # ── shared ───────────────────────────────────────────────────────────────

    async def _start_merge(
        self, wd: str, branch: str, target: str, ctx: ResolveContext,
        res: ConflictResolution,
    ) -> tuple[list[str], ConflictResolution | None]:
        """Run the merge. Returns ``(conflicted, early)`` — ``early`` set when the
        attempt already ended (clean merge pushed, nothing to do, or an escalation)."""
        ex, cfg = self._ex, self._config
        await ex._git(["fetch", "origin", target], wd, check=False)
        await ex._git(["merge", "--no-ff", "--no-commit", f"origin/{target}"], wd, check=False)
        conflicted = _lines(await ex._git(
            ["diff", "--name-only", "--diff-filter=U"], wd, check=False))
        res.files = conflicted
        if not conflicted:
            if not await _merge_in_progress(ex, wd):
                # "Already up to date": the target is already in the branch. ADO's
                # mergeStatus is recomputed lazily and was stale.
                res.success, res.how = True, BY_CLEAN_MERGE
                res.checks["merge"] = "already up to date"
                return conflicted, res
            return conflicted, await self._commit_and_push(
                res, wd, branch, target, ctx, BY_CLEAN_MERGE)
        max_files = int(getattr(cfg, "pr_conflict_max_files", 15) or 15)
        if len(conflicted) > max_files:
            res.error = (f"{len(conflicted)} file bị conflict — vượt ngưỡng "
                         f"pr_conflict_max_files={max_files}, cần người xử lý")
            return conflicted, res
        blocked = unresolvable(conflicted)
        if blocked:
            res.error = ("file sinh tự động / nhị phân không nên sửa tay: "
                         + ", ".join(blocked[:5]) + " — cần tạo lại (vd `npm install`)")
            return conflicted, res
        return conflicted, None

    async def _logs(self, wd: str, target: str) -> tuple[str, str]:
        ex = self._ex
        ours = (await ex._git(["log", "--oneline", "-n", "20", f"origin/{target}..HEAD"], wd,
                              check=False)).strip()
        theirs = (await ex._git(["log", "--oneline", "-n", "20", f"HEAD..origin/{target}"], wd,
                                check=False)).strip()
        return ours, theirs

    async def _verify_commit_push(
        self, res: ConflictResolution, wd: str, conflicted: list[str], before: set[str],
        branch: str, target: str, ctx: ResolveContext, note: str = "",
    ) -> ConflictResolution:
        """The objective gate, identical for headless and interactive runs."""
        ex = self._ex
        left = files_with_markers(wd, conflicted)
        res.checks["markers"] = "ok" if not left else f"còn marker: {', '.join(left)}"
        if left:
            res.error = "chưa gỡ hết conflict marker trong " + ", ".join(left[:5])
            return res
        outside = sorted((await _touched(ex, wd)) - set(before) - set(conflicted))
        res.checks["scope"] = "ok" if not outside else "sửa ngoài phạm vi: " + ", ".join(
            outside[:5])
        if outside:
            res.error = ("agent sửa cả file không bị conflict ("
                         + ", ".join(outside[:5]) + ") — huỷ để an toàn")
            return res
        await ex._git(["add", "--", *conflicted], wd)
        still = _lines(await ex._git(["diff", "--name-only", "--diff-filter=U"], wd,
                                     check=False))
        if still:
            res.error = "git vẫn báo unmerged: " + ", ".join(still[:5])
            return res
        new_issues = await self._new_security_findings(wd, conflicted, target)
        res.checks["security"] = "ok" if not new_issues else "; ".join(new_issues[:3])
        if new_issues:
            res.error = "bản giải conflict tạo ra lỗi bảo mật mới: " + "; ".join(new_issues[:3])
            return res
        tests = await ex._test_gate.run(wd)
        if not tests.ran:
            res.checks["tests"] = f"skipped ({tests.summary})"
        elif tests.passed:
            res.checks["tests"] = "ok"
        else:
            blocked, note = await self._judge_test_failure(res, tests, wd, target, note)
            if blocked:
                return res
        return await self._commit_and_push(res, wd, branch, target, ctx, BY_AGENT, note=note)

    async def _judge_test_failure(
        self, res: ConflictResolution, tests: TestResult, wd: str, target: str, note: str,
    ) -> tuple[bool, str]:
        """Red after resolving: was it the resolution, or was the target red already?

        Tests the target commit on its own (a clean worktree, cached by commit) and
        compares normalised failure lists. Only failures the resolution ADDED are its
        fault. Returns ``(blocked, note)``; fills ``res.checks["tests"]``, ``res.error``
        and ``res.test_failures`` with what a reader needs to act.
        """
        base = await self._baseline(wd, target)
        mine = tests.failures
        known = set(base.failures) if (base.ran and not base.passed) else set()
        added = [f for f in mine if f not in known]
        target_red = base.ran and not base.passed

        if target_red and mine and not added:
            # Every failure is already on the target: the resolution made nothing worse.
            res.test_failures = mine
            res.checks["tests"] = (f"target {target} đỏ sẵn — {len(base.failures)} lỗi có sẵn, "
                                   "resolution không thêm lỗi mới")
            if self._config.pr_conflict_allow_preexisting_failures:
                return False, (note + "\n\n" if note else "") + (
                    f"Tests: target {target} is already red ({len(base.failures)} failure(s)); "
                    "this merge adds none.")
            res.error = (f"target `{target}` đang đỏ sẵn — không do bản giải conflict "
                         f"({len(base.failures)} lỗi có sẵn trên target). Sửa target rồi "
                         "/resolve lại.")
            return True, note

        # The resolution's own failures (or, when output could not be parsed, the run).
        res.test_failures = added or mine
        if added:
            res.checks["tests"] = f"{len(added)} lỗi mới do resolution" + (
                f" (+{len(mine) - len(added)} có sẵn trên target)" if len(mine) > len(added)
                else "")
        else:
            res.checks["tests"] = tests.summary
        if target_red and not mine:
            # Both red but nothing parseable to compare — say so rather than guess.
            res.error = (f"test fail sau khi giải conflict ({tests.summary}); target "
                         f"`{target}` cũng đang fail — chưa đối chiếu được lỗi, cần người xem")
        else:
            res.error = "test fail sau khi giải conflict: " + res.checks["tests"]
        return True, note

    async def _baseline(self, wd: str, target: str) -> TestResult:
        """The target commit's own test result, from a throwaway worktree beside ``wd``.

        Sibling rather than under a temp dir: Windows' 260-character path limit fails a
        checkout deep in a large repo, and ``wd``'s parent is known to be short enough.
        Never raises — an unavailable baseline is "unknown", which never unblocks.
        """
        ex = self._ex
        sha = (await ex._git(["rev-parse", f"origin/{target}"], wd, check=False)).strip()
        if not sha:
            return TestResult(ran=False, summary="target commit unknown")
        if sha in self._baselines:
            return self._baselines[sha]
        base_dir = str(Path(wd).parent / f"{Path(wd).name}-base")
        result = TestResult(ran=False, summary="baseline not run")
        try:
            await ex._git(["worktree", "add", "--detach", "--force", base_dir, sha], wd)
            result = await ex._test_gate.run(base_dir)   # signatures: repo-relative
            _log.info("target baseline tested", target=target, commit=sha[:10],
                      passed=result.passed, failures=len(result.failures))
        except Exception as exc:  # noqa: BLE001
            _log.warning("target baseline unavailable", target=target, error=describe_exc(exc))
        finally:
            await ex._git(["worktree", "remove", "--force", base_dir], wd, check=False)
        if result.ran:
            self._baselines[sha] = result
        return result

    async def _commit_and_push(
        self, res: ConflictResolution, wd: str, branch: str, target: str,
        ctx: ResolveContext, how: str, note: str = "",
    ) -> ConflictResolution:
        ex = self._ex
        files = ", ".join(res.files[:10]) if res.files else "none"
        body = (f"Resolved by AI Autopilot for PR !{ctx.pr_id}. Conflicted files: {files}."
                + (f"\n\n{note}" if note else ""))
        await ex._git(["commit", "--no-edit", "-m", f"Merge {target} into {branch}",
                       "-m", body], wd)
        # Never --force: someone may have pushed to the branch since we fetched it, and
        # their commit is worth more than our merge. A rejection is reported and retried.
        await ex._git(["push", "origin", f"HEAD:{branch}"], wd)
        res.merge_commit = (await ex._git(["rev-parse", "HEAD"], wd, check=False)).strip()
        res.success, res.how = True, how
        _log.info("conflict resolved", pr=ctx.pr_id, how=how, files=len(res.files),
                  commit=res.merge_commit[:10])
        return res

    async def _new_security_findings(
        self, wd: str, files: list[str], target: str,
    ) -> list[str]:
        """Findings in the resolved files that exist on NEITHER side of the merge.

        A resolved file legitimately inherits whatever either parent had; what a
        resolution must not do is invent something new (a secret pasted from one hunk
        into another context, a check dropped). Compared on (rule, trimmed line), so a
        finding that merely moved lines is not "new".
        """
        from ai_autopilot.security_scan.tools import builtin

        block = {s.strip().lower() for s in
                 (getattr(self._config, "block_on_severity", "") or "critical,high").split(",")
                 if s.strip()}
        out: list[str] = []
        for rel in files:
            try:
                merged = (Path(wd) / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            known: set[tuple[str, str]] = set()
            for ref in ("HEAD", f"origin/{target}"):
                side = await self._ex._git(["show", f"{ref}:{rel}"], wd, check=False)
                known |= {(f.rule_id, _norm(f.snippet)) for f in builtin.scan_text(side, rel)}
            for f in builtin.scan_text(merged, rel):
                if f.severity in block and (f.rule_id, _norm(f.snippet)) not in known:
                    out.append(f"{rel}:{f.line} {f.rule_id}")
        return out


def _session_brief(
    ctx: ResolveContext, files: list[str], ours: str, theirs: str, repo_name: str, key: str,
) -> str:
    """The session's instructions: the same resolution prompt, plus how an interactive
    run hands back — a result file, never a commit or a push."""
    result_rel = (_RUNS / f"{key}.json").as_posix()
    return (
        f"# Resolve the merge conflict on PR !{ctx.pr_id}\n\n"
        f"The repository is the `{repo_name}/` folder in this directory. Run every git "
        f"command inside `{repo_name}/`.\n\n"
        + resolution_prompt(ctx, files, ours, theirs)
        + f"""
## How this session ends

A person may be attached to this session and may tell you how a hunk should be
resolved — follow them. When the conflicted files are settled (or you and they decide
it needs a product decision), write this file and stop:

`{result_rel}` (in THIS directory, not inside `{repo_name}/`):

```json
{{"status": "completed", "summary": "one sentence on what you reconciled"}}
```

or, when it cannot be settled here:

```json
{{"status": "needs_human", "reason": "which file/hunk and the decision it needs"}}
```

Do NOT `git add`, `git commit`, `git merge --abort` or `git push` — AI Autopilot checks
the result (no marker left, no other file touched, security gate, tests) and only then
commits the merge and pushes it.
"""
    )


async def _merge_in_progress(ex, wd: str) -> bool:
    out = await ex._git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], wd, check=False)
    return bool(out.strip())


async def _touched(ex, wd: str) -> set[str]:
    """Working-tree edits + untracked files — what an agent edit shows up as. Files the
    merge itself staged are in the index, not here, so they never count against it."""
    changed = _lines(await ex._git(["diff", "--name-only"], wd, check=False))
    untracked = _lines(await ex._git(
        ["ls-files", "--others", "--exclude-standard"], wd, check=False))
    return set(changed) | set(untracked)


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _norm(s: str) -> str:
    return " ".join((s or "").split())
