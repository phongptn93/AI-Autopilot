"""Policy engine — deterministic guardrails on what a run may change.

Pure functions (files in → violations out), enforced by the control plane at the
same point as the auto-review / test gates: AFTER the agent edits, BEFORE a PR
opens. This is a hard rail, not a prompt instruction — the agent cannot talk its
way past it.

Two rules, both opt-in (empty/0 = off):

* ``policy_protected_paths`` — glob patterns of files the autopilot must never
  modify (deploy manifests, CI pipelines, secrets templates…). Any changed file
  matching a pattern blocks the PR.
* ``policy_max_files_changed`` — blast-radius cap: a run touching more files than
  this is blocked (a "small fix" that rewrites half the repo needs a human).
"""

from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath


def _matches(path: str, pattern: str) -> bool:
    """Case-insensitive glob match on the full (posix-normalised) path, or on the
    bare filename so ``*.env`` also catches ``config/prod.env``. Note ``*`` in
    fnmatch crosses ``/`` — ``k8s/*`` protects the whole subtree."""
    p = path.replace("\\", "/").strip("/").lower()
    pat = pattern.replace("\\", "/").strip("/").lower()
    return fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(PurePosixPath(p).name, pat)


def check_changes(
    changed_files: list[str], *, protected_paths: list[str], max_files: int = 0
) -> list[str]:
    """Violations for this change-set — empty list = policy passes.

    Each violation is a human-readable line (used in the block reason and the
    escalation comment)."""
    violations: list[str] = []
    for pattern in protected_paths or ():
        pattern = (pattern or "").strip()
        if not pattern:
            continue
        hits = [f for f in changed_files if _matches(f, pattern)]
        if hits:
            shown = ", ".join(hits[:3]) + ("…" if len(hits) > 3 else "")
            violations.append(f"protected path `{pattern}` touched: {shown}")
    if max_files and len(changed_files) > max_files:
        violations.append(
            f"blast radius: {len(changed_files)} files changed (cap {max_files})"
        )
    return violations
