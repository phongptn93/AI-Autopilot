"""The Capabilities page shows the whole .claude inventory: subagents, skills, commands, rules."""

from __future__ import annotations

from starlette.testclient import TestClient

from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.skills_catalog import (
    discover_agents,
    discover_commands,
    discover_rules,
    discover_skills,
)


def _ws(tmp_path):
    claude = tmp_path / ".claude"
    for name, desc in (("api-design", "Design REST contracts"),
                       ("api-controller", "Scaffold a controller"),
                       ("bugfix-workflow", "Fix bugs")):
        (claude / "skills" / name).mkdir(parents=True)
        (claude / "skills" / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\nbody", encoding="utf-8")
    agents = claude / "agents"
    agents.mkdir()
    (agents / "agent-backend.md").write_text(
        "---\nname: agent-backend\nmodel: opus\ntools: Read, Edit\n"
        "description: Implement the backend\n---\n"
        "Use skill api-design first, then api-controller. Not my-api-design-v2.\n",
        encoding="utf-8")
    (agents / "agent-plain.md").write_text(
        "# Plain agent\n\nDoes plain things.\n", encoding="utf-8")
    (agents / "README.md").write_text("# not an agent", encoding="utf-8")
    (claude / "commands").mkdir()
    (claude / "commands" / "find-key.md").write_text(
        "# Find a key\n\nLook up **a key** in code.\n", encoding="utf-8")
    (claude / "rules").mkdir()
    (claude / "rules" / "comms.md").write_text("---\ndescription: How to talk\n---\n# x",
                                               encoding="utf-8")
    return str(tmp_path)


def test_agents_are_read_with_model_tools_and_the_skills_they_name(tmp_path):
    ws = _ws(tmp_path)
    skills = [s.name for s in discover_skills(ws)]
    agents = {a.name: a for a in discover_agents(ws, skills)}
    assert set(agents) == {"agent-backend", "agent-plain"}          # README is not an agent
    be = agents["agent-backend"]
    assert be.model == "opus" and be.tools == ["Read", "Edit"]
    # Whole-word match: "my-api-design-v2" does not count as api-design a second time,
    # and bugfix-workflow is not mentioned at all.
    assert be.skills == ["api-controller", "api-design"]
    assert agents["agent-plain"].description == "Does plain things."


def test_commands_and_rules_get_a_one_line_description(tmp_path):
    ws = _ws(tmp_path)
    assert discover_commands(ws)[0].description == "Look up a key in code."
    assert discover_rules(ws)[0].description == "How to talk"


def test_the_page_shows_every_kind_and_links_both_ways(tmp_path):
    cfg = Settings(dry_run=True, workspace_directory=_ws(tmp_path),
                   database_url=f"sqlite+aiosqlite:///{tmp_path / 'c.db'}")
    with TestClient(create_app(cfg)) as client:
        html = client.get("/dashboard/capabilities").text
    for needle in ("Subagent", "agent-backend", 'href="#skill-api-design"',
                   'href="#agent-agent-backend"', "find-key", "comms", "opus"):
        assert needle in html, needle


def test_an_empty_workspace_says_how_to_add_agents(tmp_path):
    cfg = Settings(dry_run=True, workspace_directory=str(tmp_path),
                   database_url=f"sqlite+aiosqlite:///{tmp_path / 'e.db'}")
    with TestClient(create_app(cfg)) as client:
        html = client.get("/dashboard/capabilities").text
    assert "Chưa có subagent nào" in html
