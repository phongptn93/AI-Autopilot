"""Resolve a PR's merge conflict: merge the target in, settle the hunks, prove it, push.

See ``ai_autopilot.pr_conflicts`` for why this is a merge and not a rebase. This module
is the mechanics, and its one rule is that the model's word is never the evidence:

1. ``git merge --no-ff --no-commit origin/<target>`` in an isolated checkout. A clean
   merge (ADO's view was stale) is committed and pushed with no model involved.
2. Lock files, binaries, or more conflicted files than ``pr_conflict_max_files`` →
   abort before spending a token; those need a person.
3. The model edits the conflicted files only.
4. Checked, objectively, before anything is committed:
   - no conflict marker is left in any conflicted file
   - nothing OUTSIDE the conflicted files was touched (the scope guard)
   - the index has no unmerged path after staging
   - the repo's tests pass (when the test gate is enabled)
   - the resolution introduced no security finding that NEITHER side had
5. Commit the merge and push — never ``--force``. A push rejected because the branch
   moved meanwhile is a failure to retry, not something to overwrite.

Any failure aborts the merge and releases the checkout, so the PR branch on origin is
exactly as it was.
"""

from __future__ import annotations

import time
from pathlib import Path

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


class ConflictResolver:
    """Drives one resolution attempt through the executor's checkout machinery."""

    def __init__(self, executor, config) -> None:
        self._ex = executor
        self._config = config

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
            await ex._git(["fetch", "origin", target_branch], wd, check=False)
            await ex._git(
                ["merge", "--no-ff", "--no-commit", f"origin/{target_branch}"], wd, check=False,
            )
            conflicted = _lines(await ex._git(
                ["diff", "--name-only", "--diff-filter=U"], wd, check=False))
            res.files = conflicted

            if not conflicted:
                if not await _merge_in_progress(ex, wd):
                    # "Already up to date": the target is already in the branch. ADO's
                    # mergeStatus is recomputed lazily and was stale.
                    res.success, res.how = True, BY_CLEAN_MERGE
                    res.checks["merge"] = "already up to date"
                    return res
                return await self._commit_and_push(res, wd, branch, target_branch, ctx,
                                                   BY_CLEAN_MERGE)

            max_files = int(getattr(cfg, "pr_conflict_max_files", 15) or 15)
            if len(conflicted) > max_files:
                res.error = (f"{len(conflicted)} file bị conflict — vượt ngưỡng "
                             f"pr_conflict_max_files={max_files}, cần người xử lý")
                return res
            blocked = unresolvable(conflicted)
            if blocked:
                res.error = ("file sinh tự động / nhị phân không nên sửa tay: "
                             + ", ".join(blocked[:5]) + " — cần tạo lại (vd `npm install`)")
                return res

            ours = (await ex._git(["log", "--oneline", "-n", "20",
                                   f"origin/{target_branch}..HEAD"], wd, check=False)).strip()
            theirs = (await ex._git(["log", "--oneline", "-n", "20",
                                     f"HEAD..origin/{target_branch}"], wd, check=False)).strip()
            before = await _touched(ex, wd)
            prompt = (f"Working tree: {wd}\n\n"
                      + resolution_prompt(ctx, conflicted, ours, theirs))
            claude_cwd = ws.claude_cwd or wd
            run = await ex._run_claude(prompt, claude_cwd, repo=wd)
            res.tokens = int(getattr(run, "input_tokens", 0) or 0) + int(
                getattr(run, "output_tokens", 0) or 0)
            verdict, reason = parse_verdict(getattr(run, "text", "") or "")

            if verdict == "needs-human":
                res.error = f"agent cần người quyết định: {reason or 'không nêu lý do'}"
                return res
            left = files_with_markers(wd, conflicted)
            res.checks["markers"] = "ok" if not left else f"còn marker: {', '.join(left)}"
            if left:
                res.error = "chưa gỡ hết conflict marker trong " + ", ".join(left[:5])
                return res
            outside = sorted((await _touched(ex, wd)) - before - set(conflicted))
            res.checks["scope"] = "ok" if not outside else "sửa ngoài phạm vi: " + ", ".join(
                outside[:5])
            if outside:
                res.error = ("agent sửa cả file không bị conflict ("
                             + ", ".join(outside[:5]) + ") — huỷ để an toàn")
                return res

            await ex._git(["add", "--", *conflicted], wd)
            still = _lines(await ex._git(
                ["diff", "--name-only", "--diff-filter=U"], wd, check=False))
            if still:
                res.error = "git vẫn báo unmerged: " + ", ".join(still[:5])
                return res

            new_issues = await self._new_security_findings(wd, conflicted, target_branch)
            res.checks["security"] = "ok" if not new_issues else "; ".join(new_issues[:3])
            if new_issues:
                res.error = "bản giải conflict tạo ra lỗi bảo mật mới: " + "; ".join(
                    new_issues[:3])
                return res

            tests = await ex._test_gate.run(wd)
            if tests.ran:
                res.checks["tests"] = "ok" if tests.passed else tests.summary
                if not tests.passed:
                    res.error = "test fail sau khi giải conflict: " + tests.summary
                    return res
            else:
                res.checks["tests"] = f"skipped ({tests.summary})"

            return await self._commit_and_push(res, wd, branch, target_branch, ctx, BY_AGENT,
                                               note=reason)
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
