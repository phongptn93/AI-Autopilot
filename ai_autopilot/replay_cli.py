"""``ai-autopilot evals harvest`` / ``ai-autopilot evals replay`` — the command line for
:mod:`ai_autopilot.replay_eval`.

Kept out of ``__main__`` so the entry point stays a dispatcher and so these commands can
be exercised in tests with an injected agent, without a console script in the way.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ai_autopilot import replay_eval as rp
from ai_autopilot.logging_config import describe_exc

DEFAULT_REPLAY_DIR = "evals/replay"


def _workspace(config: Any, override: str = "") -> str:
    return (override or getattr(config, "workspace_directory", "")
            or getattr(config, "repo_working_directory", "") or ".")


def _test_command_resolver(config: Any):
    """The same choice the test gate makes: the operator's per-repo command first,
    then whatever the repo's own files announce."""
    from ai_autopilot.execution.test_gate import detect_test_command

    def resolve(repo: str, worktree: str) -> str:
        configured = ""
        getter = getattr(config, "test_command_for", None)
        if callable(getter):
            try:
                configured = getter(repo) or ""
            except Exception:  # noqa: BLE001 — a config quirk must not stop the replay
                configured = ""
        return configured or detect_test_command(worktree) or ""

    return resolve


def _ado_fetcher(config: Any) -> rp.TaskFetcher | None:
    """Work-item titles and descriptions from ADO — only when credentials exist.

    Optional by design: harvest is git-first and must work on a laptop with no PAT, in
    CI, or against a repo whose tracker is somewhere else. Any failure falls back to
    the commit messages (see :func:`ai_autopilot.replay_eval.harvest`).
    """
    if not (getattr(config, "ado_organization", "") and getattr(config, "ado_pat", "")):
        return None

    async def fetch(ids: list[int]) -> Mapping[int, tuple[str, str]]:
        import httpx

        from ai_autopilot.ado.auth import AdoAuthService
        from ai_autopilot.ado.client import AdoClient

        async with httpx.AsyncClient(timeout=30) as http:
            client = AdoClient(http, AdoAuthService(config), config)
            items = await client.get_work_items_by_ids(ids)
        out: dict[int, tuple[str, str]] = {}
        for item in items or []:
            parts = [item.description or ""]
            if item.acceptance_criteria:
                parts.append("Acceptance criteria:\n" + item.acceptance_criteria)
            out[item.id] = (item.title or "", "\n\n".join(p for p in parts if p))
        return out

    return fetch


def _load_config() -> Any:
    from ai_autopilot.config import load_settings

    return load_settings()


def harvest_main(argv: list[str], config: Any | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ai-autopilot evals harvest",
        description="Build replay cases from a repo's merged work (git history).")
    ap.add_argument("--repo", required=True,
                    help="repo folder name under the workspace, or a path")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--out", default=DEFAULT_REPLAY_DIR)
    ap.add_argument("--workspace", default="", help="override the configured workspace")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace case files that already exist")
    ap.add_argument("--no-tracker", action="store_true",
                    help="use commit messages only, even when ADO is configured")
    args = ap.parse_args(argv)

    config = config if config is not None else _load_config()
    repo_dir = rp.resolve_repo_dir(args.repo, _workspace(config, args.workspace))
    if repo_dir is None:
        print(f"Repo {args.repo!r} not found under {_workspace(config, args.workspace)!r}.",
              file=sys.stderr)
        return 2
    fetcher = None if args.no_tracker else _ado_fetcher(config)
    try:
        cases = rp.harvest(repo_dir, repo_name=Path(args.repo).name, limit=args.limit,
                           fetch_tasks=fetcher)
    except Exception as exc:  # noqa: BLE001 — say what broke instead of a traceback
        print(f"Harvest failed: {describe_exc(exc)}", file=sys.stderr)
        return 1
    if not cases:
        print("No commit referencing a work item (#123 / AB#123) found - nothing harvested.",
              file=sys.stderr)
        return 1
    written, skipped = rp.write_cases(cases, args.out, overwrite=args.overwrite)
    for p in written:
        print(f"  + {p}")
    for p in skipped:
        print(f"  = {p} (exists - kept; --overwrite to replace)")
    print(f"\n  {len(written)} written, {len(skipped)} kept. Review expected_files and the "
          "task text before trusting the score.")
    return 0


def replay_main(argv: list[str], config: Any | None = None,
                agent: rp.ReplayAgent | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ai-autopilot evals replay",
        description="Replay real past tasks in throwaway worktrees and score the agent.")
    ap.add_argument("directory", nargs="?", default=DEFAULT_REPLAY_DIR)
    ap.add_argument("--case", default="", help="run only the case with this name")
    ap.add_argument("--min-pass-rate", type=float, default=1.0)
    ap.add_argument("--json", default="", help="also write the report as JSON here")
    ap.add_argument("--workspace", default="", help="override the configured workspace")
    ap.add_argument("--include-examples", action="store_true",
                    help="also run cases marked example: true")
    args = ap.parse_args(argv)

    cases = rp.load_replay_cases(args.directory, include_examples=args.include_examples,
                                 name=args.case)
    if not cases:
        what = f"named {args.case!r} " if args.case else ""
        print(f"No replay cases {what}under {args.directory!r} - nothing to prove.",
              file=sys.stderr)
        return 1
    config = config if config is not None else _load_config()
    scores = asyncio.run(rp.run_replay_suite(
        cases,
        workspace=_workspace(config, args.workspace),
        agent=agent or rp.claude_agent(config),
        test_command_for=_test_command_resolver(config),
    ))
    print(rp.format_replay_report(scores, args.min_pass_rate))
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rp.report_json(scores, args.min_pass_rate), indent=2,
                                  ensure_ascii=False), encoding="utf-8")
        print(f"  JSON report: {out}")
    return 0 if rp.pass_rate(scores) >= args.min_pass_rate else 1


def run(command: str, argv: list[str]) -> int:
    if command == "harvest":
        return harvest_main(argv)
    return replay_main(argv)
