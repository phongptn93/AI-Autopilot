"""Workspace introspection — discover the git repos inside the workspace.

In AI-native mode the agent runs from the workspace root and chooses which repo
(subfolder) to edit; it is not pinned to a single configured repo. This lists the
candidate repos so the brief can name them and the dashboard can show them.
"""

from __future__ import annotations

from pathlib import Path


def discover_repos(workspace: str) -> list[str]:
    """Names of immediate subfolders of the workspace that are git repos."""
    if not workspace:
        return []
    root = Path(workspace)
    if not root.is_dir():
        return []
    repos = [
        sub.name
        for sub in root.iterdir()
        # Skip dotfolders (e.g. .claude is itself a git repo but is config, not a
        # source repo the agent should edit).
        if sub.is_dir() and not sub.name.startswith(".") and (sub / ".git").exists()
    ]
    return sorted(repos)


def parse_repo_descriptions(entries: list[str]) -> dict[str, str]:
    """Parse ``["RepoName = what it is", ...]`` into ``{name_lower: description}``
    so the agent's brief can explain each repo and pick the right one."""
    out: dict[str, str] = {}
    for entry in entries:
        sep = "=" if "=" in entry else (":" if ":" in entry else "")
        if not sep:
            continue
        name, desc = entry.split(sep, 1)
        name, desc = name.strip(), desc.strip()
        if name and desc:
            out[name.lower()] = desc
    return out
