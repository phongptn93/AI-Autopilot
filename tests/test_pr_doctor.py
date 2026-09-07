"""Tests for `ai-autopilot pr-doctor` — the answer to "I @mentioned it and nothing
happened".

Six gates decide whether a comment becomes work, and five of them fail by returning
early, so the log says nothing. These pin what the report says for each one: a wrong
answer here sends someone to change the wrong setting.
"""

from __future__ import annotations

from ai_autopilot import pr_doctor
from ai_autopilot.config import BotIdentity, Settings

PR_URL = "https://dev.azure.com/newoceanis/DxFactory/_git/Micro-Frontend/pullrequest/3861"


def _levels(checks) -> list[str]:
    return [c.level for c in checks]


def _titles(checks) -> str:
    return " | ".join(c.title + " " + c.detail + " " + c.fix for c in checks)


def test_parses_a_pull_request_url():
    assert pr_doctor.parse_target(PR_URL) == ("Micro-Frontend", 3861)
    assert pr_doctor.parse_target(PR_URL + "?path=%2Fsrc%2Fx.ts") == ("Micro-Frontend", 3861)
    assert pr_doctor.parse_target("https://example.com/nope") is None


def test_a_hand_made_pr_is_the_reviewer_tracker_not_the_babysitter():
    """The babysitter only owns branches it created. Saying "feedback loop off" for a
    hand-made PR would send someone to switch on the loop that ignores it anyway."""
    cfg = Settings()  # both loops off, as shipped
    owned, checks = pr_doctor.check_branch("refs/heads/dxmpm/material-usage", cfg)
    assert owned is False
    assert "not autopilot-shaped" in checks[0].title

    report = checks + pr_doctor.check_switches(cfg, owned)
    assert "pr_reviewer_tracking_enabled" in _titles(report)
    assert "feedback_loop_enabled" not in _titles(report)


def test_an_autopilot_pr_points_at_the_feedback_loop():
    cfg = Settings()
    owned, checks = pr_doctor.check_branch("refs/heads/feature/be/8953-thing", cfg)
    assert owned is True and "8953" in checks[0].title
    report = pr_doctor.check_switches(cfg, owned)
    assert "feedback_loop_enabled" in _titles(report)

    # ...and says nothing about it once it is on.
    on = pr_doctor.check_switches(Settings(feedback_loop_enabled=True), owned)
    assert _levels(on) == ["ok"]


def test_a_prefixed_branch_without_a_work_item_id_says_which_half_failed():
    cfg = Settings()
    owned, checks = pr_doctor.check_branch("refs/heads/feature/no-id-here", cfg)
    assert owned is False
    assert "work item id" in checks[0].detail


def test_scope_gates_name_the_list_that_excluded_the_pr():
    cfg = Settings(pr_reviewer_target_branches=["main"], allowed_repos=["Backend-Fresh"])
    checks = pr_doctor.check_scope("refs/heads/dxfac/development", "Micro-Frontend", cfg)
    text = _titles(checks)
    assert "out of scope" in text and "main" in text
    assert "allowed_repos" in text
    # In scope on both counts → nothing but an ok line.
    fine = pr_doctor.check_scope("refs/heads/main", "Backend-Fresh", cfg)
    assert _levels(fine) == ["ok"]


def _thread(author: str, content: str) -> dict:
    return {"comments": [{"author": {"displayName": author}, "content": content}]}


def test_no_comment_addresses_the_bot():
    """The commonest cause, and the least visible: a mention of a DIFFERENT account
    looks identical in the PR to a mention of this one."""
    cfg = Settings()
    bot = BotIdentity(identity_id="guid-1", display_name="AI Autopilot", claimed="")
    checks = pr_doctor.check_comments([_thread("Phong Pham", "look at this please")], cfg, bot)
    assert _levels(checks) == ["bad"]
    assert "GUID" in checks[0].detail


def test_a_mention_of_the_bot_is_found_and_attributed():
    cfg = Settings()
    bot = BotIdentity(identity_id="guid-1", display_name="AI Autopilot", claimed="")
    mention = '<a href="#" data-vss-mention="version:2.0,guid-1">@AI Autopilot</a> review this'
    checks = pr_doctor.check_comments([_thread("Phong Pham", mention)], cfg, bot)
    assert _levels(checks) == ["ok", "ok"]
    assert "@mention by Phong Pham" in checks[1].title


def test_a_command_from_someone_not_allowed_is_named_as_such():
    """"Nobody said anything" and "someone did, but is not allowed" look the same from
    outside the bot — and lead to opposite fixes."""
    cfg = Settings(assignee_trigger_user="Phong Pham")
    checks = pr_doctor.check_comments([_thread("Dat Pham", "/review this")], cfg, None)
    assert _levels(checks) == ["ok", "bad"]
    assert "only obeys" in checks[1].detail
    # The owner's own command passes.
    ok = pr_doctor.check_comments([_thread("Phong Pham", "/review this")], cfg, None)
    assert _levels(ok) == ["ok", "ok"]


def test_identity_note_says_how_mentions_are_matched():
    cfg = Settings()
    named = pr_doctor.identity_note(
        BotIdentity(identity_id="guid-1", display_name="AI Autopilot", claimed=""), cfg
    )
    assert named.level == "ok" and "GUID" in named.detail

    # No GUID: matching falls back to the display name, which a rename breaks.
    fallback = pr_doctor.identity_note(
        BotIdentity(identity_id="", display_name="", claimed=""), cfg
    )
    assert fallback.level == "warn" and "pr_bot_identity" in fallback.fix

    off = pr_doctor.identity_note(None, Settings(comment_mention_enabled=False))
    assert off.level == "bad"


def test_render_counts_the_blockers_and_says_when_there_are_none():
    blocked = pr_doctor.render([pr_doctor.Check("bad", "x"), pr_doctor.Check("ok", "y")])
    assert "1 blocker(s)" in blocked
    clean = pr_doctor.render([pr_doctor.Check("ok", "y"), pr_doctor.Check("warn", "z")])
    assert "No blocker found" in clean


def test_cli_refuses_without_a_url():
    assert pr_doctor.run("") == 2
