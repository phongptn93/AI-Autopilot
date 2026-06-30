"""Discover the skills the agent can use, from the workspace ``.claude/skills``.

The dashboard reads this so operators see exactly the capabilities the agent has
— sourced live from ``<workspace>/.claude/skills/*/SKILL.md`` frontmatter, never
a hardcoded list (which would drift from reality).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class SkillInfo:
    name: str
    description: str


def discover_skills(workspace: str) -> list[SkillInfo]:
    """Return skills found under ``<workspace>/.claude/skills``, sorted by name.

    Empty list if no workspace is configured or the directory is absent.
    """
    if not workspace:
        return []
    skills_dir = Path(workspace) / ".claude" / "skills"
    if not skills_dir.is_dir():
        return []

    skills: list[SkillInfo] = []
    for sub in skills_dir.iterdir():
        if not sub.is_dir():
            continue
        md = sub / "SKILL.md"
        if not md.is_file():
            continue
        meta = _frontmatter(md)
        skills.append(
            SkillInfo(
                name=str(meta.get("name") or sub.name),
                description=str(meta.get("description") or "").strip(),
            )
        )
    return sorted(skills, key=lambda s: s.name.lower())


def _frontmatter(path: Path) -> dict:
    """Parse the leading ``--- ... ---`` YAML frontmatter block of a markdown file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.lstrip().startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}
