"""Tests for Settings-derived helpers."""

from __future__ import annotations

from ai_autopilot.config import Settings


def test_effective_trigger_tags_dedupes_and_drops_blanks():
    cfg = Settings(trigger_tag="autopilot", trigger_tags=["squad-a", "autopilot", "", "  ", "squad-b"])
    # primary first, duplicates and blank/whitespace-only entries removed, order kept
    assert cfg.effective_trigger_tags == ["autopilot", "squad-a", "squad-b"]


def test_effective_trigger_tags_primary_only():
    assert Settings(trigger_tag="autopilot").effective_trigger_tags == ["autopilot"]


def test_effective_trigger_tags_blank_primary():
    cfg = Settings(trigger_tag="", trigger_tags=["only-extra"])
    assert cfg.effective_trigger_tags == ["only-extra"]
