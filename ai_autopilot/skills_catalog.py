"""Discover what the agent can use, from the workspace's ``.claude`` folder.

The dashboard reads this so operators see exactly the capabilities the agent has —
skills (``skills/*/SKILL.md``), subagents (``agents/*.md``), slash commands
(``commands/*.md``) and always-loaded rules (``rules/*.md``) — sourced live from the
files, never a hardcoded list (which would drift from reality).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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


@dataclass
class AgentInfo:
    """One subagent: what it is for, which model it runs on, what it may touch."""

    name: str
    description: str
    model: str = ""
    tools: list[str] = field(default_factory=list)
    #: Skills the agent's own instructions name — the work it actually delegates to.
    skills: list[str] = field(default_factory=list)


@dataclass
class DocInfo:
    """A slash command or a rule file: a name and the one line that says what it is."""

    name: str
    description: str


def _body(path: Path) -> str:
    """The markdown after the frontmatter (or the whole file when there is none)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if text.lstrip().startswith("---"):
        parts = text.split("---", 2)
        return parts[2] if len(parts) == 3 else ""
    return text


def _first_line(body: str) -> str:
    """The first sentence a reader would see: the H1 if any, else the first prose line."""
    heading = ""
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith(("```", "|", "---", ">")):
            continue
        if line.startswith("#"):
            heading = heading or line.lstrip("#").strip()
            continue
        return re.sub(r"[*`]", "", line)[:240]
    return heading


def discover_agents(workspace: str, skill_names: list[str] | None = None) -> list[AgentInfo]:
    """Subagents under ``<workspace>/.claude/agents``, sorted by name.

    ``skill_names`` (the discovered skills) lets each agent list the skills its own
    instructions name, matched as whole words — that is how an operator sees which
    agent does which work, without anyone maintaining a second mapping.
    """
    if not workspace:
        return []
    folder = Path(workspace) / ".claude" / "agents"
    if not folder.is_dir():
        return []
    known = sorted({n for n in (skill_names or []) if n}, key=len, reverse=True)
    agents: list[AgentInfo] = []
    for md in folder.glob("*.md"):
        if md.stem.lower() == "readme":
            continue
        meta = _frontmatter(md)
        body = _body(md)
        tools = meta.get("tools") or []
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(",") if t.strip()]
        used = [n for n in known
                if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", body)]
        agents.append(AgentInfo(
            name=str(meta.get("name") or md.stem),
            description=str(meta.get("description") or _first_line(body)).strip(),
            model=str(meta.get("model") or "").strip(),
            tools=[str(t) for t in tools],
            skills=sorted(used),
        ))
    return sorted(agents, key=lambda a: a.name.lower())


def _discover_docs(workspace: str, sub: str) -> list[DocInfo]:
    if not workspace:
        return []
    folder = Path(workspace) / ".claude" / sub
    if not folder.is_dir():
        return []
    out = []
    for md in folder.glob("*.md"):
        if md.stem.lower() == "readme":
            continue
        meta = _frontmatter(md)
        out.append(DocInfo(
            name=md.stem,
            description=str(meta.get("description") or _first_line(_body(md))).strip(),
        ))
    return sorted(out, key=lambda d: d.name.lower())


def discover_commands(workspace: str) -> list[DocInfo]:
    """Slash commands under ``<workspace>/.claude/commands``."""
    return _discover_docs(workspace, "commands")


def discover_rules(workspace: str) -> list[DocInfo]:
    """Always-loaded rules under ``<workspace>/.claude/rules``."""
    return _discover_docs(workspace, "rules")
