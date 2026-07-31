"""Tests for Settings-derived helpers."""

from __future__ import annotations

from ai_autopilot.config import Settings, is_ambiguous_user, matches_user


def test_effective_trigger_tags_dedupes_and_drops_blanks():
    cfg = Settings(trigger_tag="autopilot", trigger_tags=["squad-a", "autopilot", "", "  ", "squad-b"])
    # primary first, duplicates and blank/whitespace-only entries removed, order kept
    assert cfg.effective_trigger_tags == ["autopilot", "squad-a", "squad-b"]


def test_effective_trigger_tags_primary_only():
    assert Settings(trigger_tag="autopilot").effective_trigger_tags == ["autopilot"]


def test_effective_trigger_tags_blank_primary():
    cfg = Settings(trigger_tag="", trigger_tags=["only-extra"])
    assert cfg.effective_trigger_tags == ["only-extra"]


# ── Assignee scoping: who this machine acts for ───────────────────────────────

def test_a_full_email_or_name_still_matches_the_decorated_ado_display_name():
    """ADO returns display names with a title attached, so the configured value has to
    match as a substring — this is what makes real identities work at all."""
    email, name = "phong.pham@nois.vn", "Phong Pham (Industrial - Head of P&T)"
    assert matches_user(email, name, "phong.pham@nois.vn") is True
    assert matches_user(email, name, "Phong Pham") is True
    assert matches_user(email, name, "phong pham") is True      # case-insensitive


def test_a_lone_first_name_no_longer_claims_a_colleagues_items():
    """The defect: 'Phong' as a substring matched BOTH "Phong Pham" and "Phong Nguyen", so
    this machine picked up a colleague's work items and ran an agent on them. No string rule
    can tell those two apart, so a bare word must IDENTIFY the person, not appear inside
    one — and matching nobody is the safe direction."""
    assert matches_user("phong.pham@nois.vn", "Phong Pham", "Phong") is False
    assert matches_user("phong.nguyen@nois.vn", "Phong Nguyen", "Phong") is False


def test_a_lone_word_that_is_the_whole_identity_still_matches():
    """Strictness must not break someone whose account really is one word."""
    assert matches_user("phong@nois.vn", "Phong", "Phong") is True
    assert matches_user("phong@nois.vn", "Someone Else", "phong") is True   # email local part


def test_a_full_name_still_excludes_a_different_person():
    assert matches_user("phong.nguyen@nois.vn", "Phong Nguyen", "Phong Pham") is False


def test_blank_means_unrestricted():
    """Single-machine setups leave it empty and must keep matching everyone."""
    assert matches_user("anyone@x.vn", "Anyone", "") is True
    assert matches_user(None, None, "") is True


def test_missing_identity_fields_do_not_crash():
    assert matches_user(None, None, "Phong Pham") is False
    assert matches_user(None, "Phong Pham", "Phong Pham") is True


def test_is_ambiguous_user_flags_only_the_unusable_shape():
    assert is_ambiguous_user("Phong") is True
    assert is_ambiguous_user("  Phong  ") is True
    assert is_ambiguous_user("Phong Pham") is False       # two words identify a person
    assert is_ambiguous_user("phong@nois.vn") is False    # an email is unique
    assert is_ambiguous_user("") is False                 # blank = unrestricted, not ambiguous
