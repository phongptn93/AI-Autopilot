"""Tests for the PR babysitter's pure decision logic."""

from __future__ import annotations

import pytest

from ai_autopilot.config import BotIdentity
from ai_autopilot.services.pr_feedback import (
    actionable_comments,
    command_threads,
    is_bot_branch,
    newest_actionable_thread_id,
    parse_work_item_id,
)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("refs/heads/feature/be/123-add-login", 123),
        ("refs/heads/fix/42-crash", 42),
        ("feature/fe/7-page", 7),
        ("refs/heads/autopilot/loop/deps-20260629", None),
        ("refs/heads/develop", None),
    ],
)
def test_parse_work_item_id(ref, expected):
    assert parse_work_item_id(ref) == expected


def test_is_bot_branch():
    pfx = ("feature/", "fix/", "autopilot/")
    assert is_bot_branch("refs/heads/feature/be/1-x", pfx) is True
    assert is_bot_branch("refs/heads/fix/2-y", pfx) is True
    assert is_bot_branch("refs/heads/main", pfx) is False


def test_actionable_comments_filters_resolved_and_system():
    def comment(text, author="Human", ctype="text"):
        return {"commentType": ctype, "author": {"displayName": author}, "content": text}

    threads = [
        {"status": "active", "comments": [comment("Please rename")]},
        {"status": "fixed", "comments": [comment("old issue")]},
        {"status": "active", "comments": [comment("PR updated", ctype="system")]},
    ]
    assert actionable_comments(threads) == ["Please rename"]


def test_actionable_comments_excludes_bot_author():
    def comment(text, author):
        return {"commentType": "text", "author": {"displayName": author}, "content": text}

    threads = [
        {
            "status": "active",
            "comments": [comment("done", "Autopilot"), comment("fix this", "Carol")],
        }
    ]
    assert actionable_comments(threads, bot_name="Autopilot") == ["fix this"]


def test_actionable_comments_excludes_bot_signature_same_account():
    from ai_autopilot.config import BOT_COMMENT_PREFIX

    def comment(text, author="Phong"):  # SAME author for bot & human (same-account setup)
        return {"commentType": "text", "author": {"displayName": author}, "content": text}

    threads = [
        {
            "status": "active",
            "comments": [
                comment(BOT_COMMENT_PREFIX + "🔧 đang xử lý…"),      # the bot's own ack
                comment("please also handle the null case"),          # a real human comment
            ],
        }
    ]
    # Both share the same author, but the signed one is skipped — so the bot's own
    # acks / status comments never re-trigger another revision.
    assert actionable_comments(threads) == ["please also handle the null case"]


def test_actionable_comments_excludes_html_encoded_signature():
    # ADO stores the 🤖 emoji HTML-encoded; detection must still recognise the bot.
    def comment(text):
        return {"commentType": "text", "author": {"displayName": "Phong"}, "content": text}

    threads = [
        {"status": "active", "comments": [
            comment("🔧 đang xử lý… <sub>&#129302; AI-Autopilot</sub>"),  # bot, encoded
            comment("real human note"),
        ]}
    ]
    assert actionable_comments(threads) == ["real human note"]


def test_newest_actionable_thread_id_picks_newest_human_unresolved():
    def c(content, when):
        return {"commentType": "text", "content": content, "publishedDate": when}

    threads = [
        {"id": 10, "status": "active", "comments": [c("old", "2026-07-20T10:00:00Z")]},
        {"id": 20, "status": "active", "comments": [c("newer", "2026-07-20T11:00:00Z")]},
        {"id": 30, "status": "closed", "comments": [c("resolved", "2026-07-20T12:00:00Z")]},
        {"id": 40, "status": "active",
         "comments": [c("bot <sub>&#129302; AI-Autopilot</sub>", "2026-07-20T13:00:00Z")]},
    ]
    # 30 is resolved, 40 is the bot's own → newest actionable human thread is 20.
    assert newest_actionable_thread_id(threads) == 20
    assert newest_actionable_thread_id([]) is None


def test_command_threads_picks_unresolved_command_threads():
    from ai_autopilot.config import BOT_COMMENT_PREFIX

    def comment(cid, content, email="phong@nois.vn"):
        return {
            "id": cid, "commentType": "text",
            "author": {"displayName": "Phong", "uniqueName": email}, "content": content,
        }

    threads = [
        {"id": 10, "status": "active", "comments": [comment(1, "/ai đổi sang 2048")]},
        {"id": 20, "status": "active", "comments": [comment(2, "just a note")]},   # not a command
        {"id": 30, "status": "fixed", "comments": [comment(3, "/ai done")]},       # resolved → skip
        {"id": 40, "status": "active", "comments": [comment(4, BOT_COMMENT_PREFIX + "wip")]},  # bot
        # a command then a plain follow-up → still picked up (newest command wins)
        {"id": 50, "status": "active",
         "comments": [comment(5, "/ai do it"), comment(6, "hurry up")]},
    ]
    got = command_threads(threads, ["/ai", "/review"])
    assert [t["thread_id"] for t in got] == [10, 50]
    assert got[0]["instruction"] == "/ai đổi sang 2048"
    assert got[0]["comment_id"] == 1
    assert got[0]["author_email"] == "phong@nois.vn"
    assert got[1]["instruction"] == "/ai do it"       # not the "hurry up" follow-up


def test_command_threads_reply_reactivates_resolved_thread():
    # The /review conversation: bot handles the command and RESOLVES the thread. A human
    # reply "/ai …" after that must still be picked up — resolution is not the handled
    # mark any more, a bot-signed reply after the command is.
    from ai_autopilot.config import BOT_COMMENT_PREFIX

    def comment(cid, content):
        return {
            "id": cid, "commentType": "text",
            "author": {"displayName": "Phong", "uniqueName": "phong@nois.vn"},
            "content": content,
        }

    threads = [
        {"id": 10, "status": "fixed", "comments": [
            comment(1, "/review check the null handling"),
            comment(2, BOT_COMMENT_PREFIX + "🔍 đang review…"),
            comment(3, BOT_COMMENT_PREFIX + "🔍 Đã review — nhận xét ở trên."),
            comment(4, "/ai fix issue 2 as suggested"),      # follow-up after resolution
        ]},
    ]
    got = command_threads(threads, ["/ai", "/review"])
    assert [t["thread_id"] for t in got] == [10]
    assert got[0]["instruction"] == "/ai fix issue 2 as suggested"
    assert got[0]["comment_id"] == 4                          # the reply, not the /review


def test_command_threads_skips_command_the_bot_already_answered():
    # Durable handled mark: once a bot-signed reply follows the newest command, the
    # thread is done — even if it's still ACTIVE (e.g. the run failed and the bot
    # posted the error). No silent re-runs after a restart.
    from ai_autopilot.config import BOT_COMMENT_PREFIX

    def comment(cid, content):
        return {
            "id": cid, "commentType": "text",
            "author": {"displayName": "Phong", "uniqueName": "phong@nois.vn"},
            "content": content,
        }

    threads = [
        {"id": 10, "status": "active", "comments": [
            comment(1, "/ai rename the field"),
            comment(2, BOT_COMMENT_PREFIX + "⚠️ Chưa xử lý được: timeout"),
        ]},
    ]
    assert command_threads(threads, ["/ai", "/review"]) == []


# ── @mention: how a human actually addresses a bot ───────────────────────────
#
# Before this, ONLY a leading /command counted, so "@AI Autopilot review this" on a pull
# request was silently ignored — and putting the bot's email in comment_command did not
# help, because ADO renders a mention as the display NAME.

_BOT_GUID = "11111111-2222-3333-4444-555555555555"


def _mention(guid: str = _BOT_GUID, name: str = "Phong Pham") -> str:
    """An @mention exactly as Azure DevOps stores it in comment HTML."""
    return f'<a href="#" data-vss-mention="version:2.0,{guid}">@{name}</a>'


def _bot() -> BotIdentity:
    return BotIdentity(
        identity_id=_BOT_GUID, display_name="Phong Pham", claimed="phong@nois.vn"
    )


def _mention_thread(tid: int, cid: int, content: str):
    return {
        "id": tid, "status": "active",
        "comments": [{
            "id": cid, "commentType": "text",
            "author": {"displayName": "Phong", "uniqueName": "phong@nois.vn"},
            "content": content,
        }],
    }


def test_mention_is_detected_and_flagged_for_inference():
    threads = [_mention_thread(10, 1, _mention() + " sao chỗ này chậm vậy?")]
    got = command_threads(threads, ["/ai", "/review"], bot=_bot())
    assert [t["thread_id"] for t in got] == [10]
    assert got[0]["instruction"] == "sao chỗ này chậm vậy?"  # mention stripped
    assert got[0]["via_mention"] is True                     # caller must infer + advisory


def test_mention_of_someone_else_is_ignored():
    """The bot must not answer a mention of a colleague — GUID is decisive."""
    other = _mention(guid="99999999-2222-3333-4444-555555555555", name="Ai Khac")
    assert command_threads(
        [_mention_thread(10, 1, other + " xem hộ cái này")], ["/ai"], bot=_bot()
    ) == []


def test_mention_with_explicit_command_is_not_inferred():
    """"@bot /security check this" named a command — honour it, don't second-guess it."""
    threads = [_mention_thread(10, 1, _mention() + " /security rà đoạn này")]
    got = command_threads(threads, ["/ai", "/security"], bot=_bot())
    assert got[0]["instruction"] == "/security rà đoạn này"
    assert got[0]["via_mention"] is False


def test_mentions_off_when_no_identity():
    """bot=None (comment_mention_enabled off, or identity unresolved) → old behaviour."""
    threads = [_mention_thread(10, 1, _mention() + " review giúp")]
    assert command_threads(threads, ["/ai", "/review"]) == []


def test_slash_commands_still_report_no_mention():
    threads = [_mention_thread(10, 1, "/ai đổi field")]
    got = command_threads(threads, ["/ai"], bot=_bot())
    assert got[0]["via_mention"] is False and got[0]["instruction"] == "/ai đổi field"
