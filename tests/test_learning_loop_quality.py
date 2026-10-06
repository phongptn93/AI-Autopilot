"""Closing the learning loop: recurring guesses become rules, stale one-offs fade out."""

from __future__ import annotations

from datetime import date, datetime

from starlette.testclient import TestClient

from ai_autopilot import lessons
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings

TODAY = date(2026, 10, 6)


def _seed(ws: str) -> None:
    for _ in range(4):                                                   # keeps biting
        lessons.record_lessons(ws, "Api", ["Validate DTOs before mapping"],
                               now=datetime(2026, 9, 1))
    lessons.record_lessons(ws, "Api", ["Old one-off about a typo"], now=datetime(2026, 3, 1))
    lessons.record_lessons(ws, "Api", ["Fresh one-off"], now=datetime(2026, 10, 1))
    lessons.add(ws, "Api", "RULE: never commit secrets")


def test_recurring_and_stale_are_told_apart(tmp_path):
    ws = str(tmp_path)
    _seed(ws)
    by_text = {le.text: le for le in lessons.entries(ws, "Api")}
    assert by_text["Validate DTOs before mapping"].recurring
    assert by_text["Old one-off about a typo"].stale(TODAY)
    assert not by_text["Fresh one-off"].stale(TODAY)
    assert not by_text["RULE: never commit secrets"].stale(date(2030, 1, 1))   # rules never fade
    assert lessons.health(ws, today=TODAY) == {
        "total": 4, "rules": 1, "learned": 3, "recurring": 1, "stale": 1,
    }


def test_the_skill_leads_with_what_recurs_and_drops_what_is_stale(tmp_path):
    ws = str(tmp_path)
    _seed(ws)
    skill = lessons.render_skill(ws, "Api")
    assert "Old one-off about a typo" not in skill
    assert skill.index("Validate DTOs") < skill.index("Fresh one-off")


def test_a_forced_brief_keeps_the_recurring_line_over_newer_noise(tmp_path):
    ws = str(tmp_path)
    lessons.record_lessons(ws, "Api", ["Recurring mistake"], now=datetime(2026, 9, 1))
    lessons.record_lessons(ws, "Api", ["Recurring mistake"], now=datetime(2026, 9, 2))
    for i in range(5):
        lessons.record_lessons(ws, "Api", [f"noise {i}"], now=datetime(2026, 10, 1, 12, i))
    picked = lessons.recent(ws, ["Api"], limit=3)
    assert "Recurring mistake" in picked and len(picked) == 3


def test_promote_turns_a_guess_into_a_rule_keeping_its_history(tmp_path):
    ws = str(tmp_path)
    _seed(ws)
    assert lessons.promote(ws, "Api", "Validate DTOs before mapping")
    row = next(le for le in lessons.entries(ws, "Api") if le.text.startswith("Validate"))
    assert row.pinned and row.count == 4 and row.date == "2026-09-01"
    assert "Validate DTOs before mapping" in lessons.render_rules(ws)
    assert lessons.promote(ws, "Api", "Validate DTOs before mapping") is False   # once


def test_prune_removes_only_stale_lines(tmp_path):
    ws = str(tmp_path)
    _seed(ws)
    assert lessons.prune_stale(ws, today=TODAY) == 1
    texts = {le.text for le in lessons.entries(ws, "Api")}
    assert "Old one-off about a typo" not in texts and len(texts) == 3


def test_the_page_leads_with_health_and_offers_promotion(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed(str(ws))
    cfg = Settings(dry_run=True, workspace_directory=str(ws), learning_loop_enabled=True,
                   database_url=f"sqlite+aiosqlite:///{tmp_path / 'l.db'}")
    with TestClient(create_app(cfg)) as client:
        html = client.get("/dashboard/learning").text
        assert "Nâng thành quy tắc" in html and "lặp lại ≥3 lần" in html
        client.post("/dashboard/learning/promote",
                    data={"repo": "Api", "text": "Validate DTOs before mapping"},
                    follow_redirects=False)
    rows = lessons.entries(str(ws), "Api")
    assert any(le.pinned and le.text.startswith("Validate") for le in rows)
