"""Tests for live skill discovery from the workspace .claude/skills."""

from __future__ import annotations

from pathlib import Path

from ai_autopilot.skills_catalog import discover_skills


def _skill(ws: Path, name: str, desc: str) -> None:
    d = ws / ".claude" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n", encoding="utf-8"
    )


def test_discover_sorted_with_metadata(tmp_path: Path):
    _skill(tmp_path, "fe-module", "build FE module")
    _skill(tmp_path, "api-controller", "scaffold API")
    skills = discover_skills(str(tmp_path))
    assert [s.name for s in skills] == ["api-controller", "fe-module"]
    assert skills[0].description == "scaffold API"


def test_no_workspace_returns_empty():
    assert discover_skills("") == []


def test_missing_dir_returns_empty(tmp_path: Path):
    assert discover_skills(str(tmp_path)) == []


def test_falls_back_to_dirname_without_frontmatter(tmp_path: Path):
    d = tmp_path / ".claude" / "skills" / "weird"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")
    skills = discover_skills(str(tmp_path))
    assert skills[0].name == "weird"
    assert skills[0].description == ""
