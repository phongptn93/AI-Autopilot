"""`/ai` on a PR, as an interactive session — the same shape as a work item's run.

Under ``execution_mode: interactive`` an ACTION command on a PR (``/ai fix …``) opens a
Remote-Control Claude Code session on the PR branch, which a person can attach to and
steer, instead of one unattended SDK run. Advisory commands (``/review``) stay as they
are: they read and comment, and change nothing.

The session edits (and may commit) in an isolated scratch worktree, detached at the PR
head. It never pushes. When it writes its result, ``finalize`` does what the headless
revise does — and does it in a stricter order:

1. the branch on origin must still be the commit the session started from (the session
   is not held under the branch lock, so a teammate may have pushed meanwhile);
2. anything left uncommitted is committed;
3. no new commit → the same failure the headless path reports;
4. the test gate and the auto-review run BEFORE the push (headless pushes first and
   reviews after) — a red result means nothing is pushed;
5. push to the PR branch, never ``--force``.

The scratch is always released, pushed or not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ai_autopilot.execution.result_contract import clear_result, find_result
from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.models import ExecutionResult

_log = get_logger("execution.revise_session")

_RUNS = Path(".autopilot") / "runs"
SKILL = "pr-command (interactive)"


@dataclass
class ReviseSession:
    run_dir: str
    key: str
    session: str
    base_sha: str


class ReviseSessions:
    def __init__(self, executor, config) -> None:
        self._ex = executor
        self._config = config

    async def prepare(
        self, *, item_id: int, repo_name: str, branch: str, prompt: str, key: str,
    ) -> ReviseSession | str:
        """Open a session on ``branch``. Returns the session, or why it could not open.

        Refuses rather than falls back to the real checkout: without an isolated
        worktree a person-steered session would be editing the shared repo.
        """
        ex = self._ex
        scratch = await ex._acquire_agent_scratch(key, [repo_name], stable=True)
        if not scratch:
            return ("không tạo được worktree tách biệt cho phiên interactive "
                    "(cần use_worktrees + workspace có repo này)")
        wd = str(Path(scratch) / repo_name)
        keep = False
        try:
            await ex._git(["fetch", "origin", branch], wd, check=False)
            # Detached at the PR head: a local branch of that name may be checked out in
            # the main workspace, and the push names the remote ref explicitly anyway.
            await ex._git(["checkout", "--detach", f"origin/{branch}"], wd)
            base_sha = (await ex._git(["rev-parse", "HEAD"], wd)).strip()
            runs = Path(scratch) / _RUNS
            runs.mkdir(parents=True, exist_ok=True)
            (runs / f"{key}.state.json").write_text(json.dumps({
                "repo_name": repo_name, "branch": branch, "base_sha": base_sha,
                "item_id": item_id,
            }), encoding="utf-8")
            clear_result(scratch, key)
            brief_rel = (_RUNS / f"{key}.brief.md").as_posix()
            (Path(scratch) / brief_rel).write_text(
                _brief(prompt, repo_name, branch, key), encoding="utf-8")
            session = f"autopilot-{key}"
            pid = ex._launch_console(
                scratch, session,
                f"Read and follow the instructions in {brief_rel}, then write the result "
                "JSON as instructed there.",
            )
            if pid is None:
                return "không mở được console Claude Code"
            ex._write_session_handle(scratch, key, pid, session)
            keep = True
            _log.info("revise session opened", session=session, branch=branch)
            return ReviseSession(run_dir=scratch, key=key, session=session, base_sha=base_sha)
        except Exception as exc:  # noqa: BLE001
            _log.warning("revise session prepare failed", key=key, error=describe_exc(exc))
            return describe_exc(exc)[:300]
        finally:
            if not keep:
                await ex.release_scratch(scratch)

    async def finalize(self, run_dir: str, key: str) -> ExecutionResult | None:
        """``None`` while the session works; otherwise verify, push, clean up."""
        agent = find_result(run_dir, key)
        if agent is None:
            return None
        ex = self._ex
        try:
            state = json.loads((Path(run_dir) / _RUNS / f"{key}.state.json")
                               .read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            await self.cancel(run_dir, key)
            return ExecutionResult.fail(0, SKILL, f"mất trạng thái phiên ({describe_exc(exc)})")
        item_id = int(state.get("item_id") or 0)
        wd = str(Path(run_dir) / state["repo_name"])
        branch, base = state["branch"], state["base_sha"]
        try:
            status = (agent.status or "").lower()
            if agent.needs_human or status in ("needs_human", "failed"):
                return ExecutionResult.fail(
                    item_id, SKILL, agent.reason or agent.summary or "phiên dừng, không nêu lý do")
            await ex._git(["fetch", "origin", branch], wd, check=False)
            head = (await ex._git(["rev-parse", f"origin/{branch}"], wd, check=False)).strip()
            if head and head != base:
                return ExecutionResult.fail(
                    item_id, SKILL, "branch có commit mới trong lúc phiên chạy — không push đè; "
                                    "hãy gửi lại lệnh trên bản mới nhất")
            if (await ex._git(["status", "--porcelain"], wd, check=False)).strip():
                await ex._git(["add", "-A"], wd)
                await ex._git(["commit", "-m",
                               f"fix(autopilot): address PR feedback (#{item_id})"], wd)
            count = (await ex._git(["rev-list", "--count", f"{base}..HEAD"], wd,
                                   check=False)).strip()
            if not count or count == "0":
                return ExecutionResult.fail(item_id, SKILL, "No file changes produced")
            files = [ln.strip() for ln in (await ex._git(
                ["diff", "--name-only", f"{base}..HEAD"], wd, check=False)).splitlines()
                if ln.strip()]

            tests = await ex._test_gate.run(wd)
            if not tests.passed:
                res = ExecutionResult.fail(
                    item_id, SKILL,
                    ("Tests failed: " if tests.ran else "Tests could not run: ") + tests.summary,
                )
                res.files_changed = files
                return res
            reviewer = getattr(ex, "_reviewer", None)
            if reviewer is not None:
                review = await reviewer.review(wd, self._config.base_branch)
                if not review.passed:
                    res = ExecutionResult.fail(
                        item_id, SKILL,
                        "Auto-review blocked: " + "; ".join(review.critical_issues[:3]))
                    res.files_changed = files
                    return res

            # Never --force: a push that is rejected because the branch moved in the last
            # seconds is reported, not overwritten.
            await ex._git(["push", "origin", f"HEAD:{branch}"], wd)
            res = ExecutionResult.ok(item_id, SKILL, agent.summary or "branch updated")
            res.branch_name = branch
            res.files_changed = files
            _log.info("revise session pushed", key=key, branch=branch, files=len(files))
            return res
        except Exception as exc:  # noqa: BLE001
            _log.warning("revise session finalize failed", key=key, error=describe_exc(exc))
            return ExecutionResult.fail(item_id, SKILL, describe_exc(exc)[:300])
        finally:
            await self.cancel(run_dir, key)

    async def cancel(self, run_dir: str, key: str) -> None:
        """Close the console and drop the scratch. Never raises."""
        ex = self._ex
        try:
            await ex.close_interactive(run_dir, key)
        except Exception as exc:  # noqa: BLE001
            _log.warning("revise session close failed", key=key, error=describe_exc(exc))
        await ex.release_scratch(run_dir)


def _brief(prompt: str, repo_name: str, branch: str, key: str) -> str:
    result_rel = (_RUNS / f"{key}.json").as_posix()
    return (
        f"# Address a pull-request comment on `{branch}`\n\n"
        f"The repository is the `{repo_name}/` folder in this directory, checked out at the "
        f"head of `{branch}` (detached). Run every git command inside `{repo_name}/`.\n\n"
        + prompt
        + f"""

## How this session ends

A person may be attached to this session and may refine what they want — follow them.
You MAY commit your changes locally with a clear message. Do NOT push, and do NOT
create or switch branches: AI Autopilot runs the tests and the auto-review on your
commits and only then pushes them to `{branch}`.

When you are done, write `{result_rel}` (in THIS directory, not inside `{repo_name}/`):

```json
{{"status": "completed", "summary": "one sentence on what you changed"}}
```

or, if it cannot be done here:

```json
{{"status": "needs_human", "reason": "what is blocking and what decision it needs"}}
```
"""
    )
