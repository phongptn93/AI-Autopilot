"""Knowledge: what a human puts in front of the agent, and what the machine works out.

The page could delete and forget but never add, so the only thing that ever reached a
brief was the machine's post-mortem of its own mistakes — and dedup was an exact string
match, so one event that happened five times became five lines that read identically and
ate five of the eight injection slots. Both are about the same question: of everything
known about this codebase, which eight lines does the agent get told?
"""

from __future__ import annotations

from datetime import datetime

from starlette.testclient import TestClient

from ai_autopilot import lessons
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings

TOKEN = "s3cret-fleet"
REOPENED = "A work item that looked finished was reopened by a human."


def _ws(tmp_path) -> str:
    return str(tmp_path)


def _day(n: int) -> datetime:
    return datetime(2026, 9, n)


# ── the same lesson, five times ──────────────────────────────────────────────


def test_one_event_five_times_is_one_line_with_a_count(tmp_path):
    """Five reopens differ only in their Context tail. As five lines they read as five
    problems and they consumed five of the eight slots the brief has."""
    ws = _ws(tmp_path)
    for i, state in enumerate(["To Do", "Ready for Testing", "Proposed", "Done", "New"]):
        lessons.record_lessons(
            ws, "repo", [f"{REOPENED} Context: reopened from state '{state}'"],
            now=_day(8 + i),
        )
    items = lessons.entries(ws, "repo")
    assert len(items) == 1
    assert items[0].count == 5
    assert items[0].date == "2026-09-12"          # the most recent occurrence
    assert lessons.recent(ws, ["repo"], limit=8) == [items[0].text]


def test_two_genuinely_different_lessons_stay_apart(tmp_path):
    ws = _ws(tmp_path)
    lessons.record_lessons(ws, "repo", ["Tests failed before the PR"], now=_day(8))
    lessons.record_lessons(ws, "repo", ["A reviewer blocked the PR"], now=_day(9))
    assert len(lessons.entries(ws, "repo")) == 2


# ── knowledge a human typed ──────────────────────────────────────────────────


def test_a_human_can_put_knowledge_in_and_it_reaches_the_brief(tmp_path):
    ws = _ws(tmp_path)
    assert lessons.add(ws, lessons.SHARED_BUCKET, "Validate every DTO before mapping")
    assert "Validate every DTO before mapping" in lessons.recent(ws, ["repo"], limit=8)


def test_authored_knowledge_is_injected_before_anything_the_machine_learned(tmp_path):
    """Sorting purely by recency meant a burst of machine noise on a Tuesday could push
    every rule the team wrote out of the brief, silently."""
    ws = _ws(tmp_path)
    lessons.add(ws, "repo", "RULE: never call SaveChanges in a loop")
    for i in range(10):
        lessons.record_lessons(ws, "repo", [f"machine lesson {i}"], now=_day(10 + i))

    got = lessons.recent(ws, ["repo"], limit=3)

    assert got[0] == "RULE: never call SaveChanges in a loop"
    assert len(got) == 3                       # the rest of the budget goes to the newest
    assert got[1:] == ["machine lesson 8", "machine lesson 9"]


def test_a_pasted_block_becomes_one_line_each(tmp_path):
    """People paste from a conventions document. Demanding they reformat it first is
    how an import feature goes unused."""
    ws = _ws(tmp_path)
    added = lessons.add_many(ws, "repo", """
        - Validate DTOs at the edge
        * No SaveChanges in a loop
        1. i18n keys shared by apps live in the backend dict

    """)
    assert added == 3
    texts = [le.text for le in lessons.entries(ws, "repo")]
    assert "Validate DTOs at the edge" in texts
    assert "No SaveChanges in a loop" in texts
    assert "i18n keys shared by apps live in the backend dict" in texts


def test_typing_a_rule_the_machine_guessed_promotes_it(tmp_path):
    """The human and the machine reached the same conclusion — that makes it a rule,
    not a guess, and it must stop being droppable."""
    ws = _ws(tmp_path)
    lessons.record_lessons(ws, "repo", ["Validate DTOs at the edge"], now=_day(8))
    assert lessons.entries(ws, "repo")[0].authored is False
    lessons.add(ws, "repo", "Validate DTOs at the edge")
    item = lessons.entries(ws, "repo")[0]
    assert item.authored is True and item.count == 2


def test_authored_lines_are_not_the_ones_dropped_when_the_file_fills_up(tmp_path):
    """A plain tail-slice deleted whatever was oldest, which on a busy repo quietly
    removed the only lines nothing will ever regenerate."""
    ws = _ws(tmp_path)
    lessons.add(ws, "repo", "RULE: the one thing we must not forget")
    for i in range(lessons._MAX_LESSONS + 20):
        lessons.record_lessons(ws, "repo", [f"noise {i}"], now=_day(9))
    texts = [le.text for le in lessons.entries(ws, "repo")]
    assert "RULE: the one thing we must not forget" in texts
    assert len(texts) <= lessons._MAX_LESSONS


# ── correcting a line without losing what it knows ───────────────────────────


def test_editing_keeps_the_date_the_source_and_the_count(tmp_path):
    """Delete-and-retype was the only correction available, and it threw away exactly
    the two facts that make a line worth reading."""
    ws = _ws(tmp_path)
    lessons.record_lessons(ws, "repo", [REOPENED], now=_day(8))
    lessons.record_lessons(ws, "repo", [REOPENED], now=_day(9))

    assert lessons.edit(ws, "repo", REOPENED, "Reopened twice — check the AC first")

    item = lessons.entries(ws, "repo")[0]
    assert item.text == "Reopened twice — check the AC first"
    assert item.count == 2 and item.date == "2026-09-09"


def test_editing_something_that_is_not_there_changes_nothing(tmp_path):
    ws = _ws(tmp_path)
    lessons.record_lessons(ws, "repo", ["a lesson"], now=_day(8))
    assert lessons.edit(ws, "repo", "not stored", "new text") is False
    assert [le.text for le in lessons.entries(ws, "repo")] == ["a lesson"]


# ── the file stays something a human can read and hand-edit ──────────────────


def test_a_file_written_by_an_older_build_still_reads(tmp_path):
    """The format gained metadata. Files already on disk must not need a migration."""
    ws = _ws(tmp_path)
    path = tmp_path / ".autopilot" / "lessons" / "repo.md"
    path.parent.mkdir(parents=True)
    path.write_text("- [2026-08-01] an old lesson\n", encoding="utf-8")
    item = lessons.entries(ws, "repo")[0]
    assert item.text == "an old lesson" and item.count == 1 and item.authored is False


def test_a_lesson_whose_text_starts_with_a_bracket_is_not_eaten(tmp_path):
    ws = _ws(tmp_path)
    lessons.record_lessons(ws, "repo", ["[urgent] do not skip the migration"], now=_day(8))
    assert lessons.entries(ws, "repo")[0].text == "[urgent] do not skip the migration"


# ── the page ─────────────────────────────────────────────────────────────────


def _client(tmp_path):
    cfg = Settings(
        dry_run=True, learning_loop_enabled=True,
        workspace_directory=str(tmp_path),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'k.db'}",
    )
    cfg.dashboard_auth_password_hash = ""
    return TestClient(create_app(cfg))


def test_the_page_offers_a_way_in_and_shows_what_the_brief_will_carry(tmp_path):
    with _client(tmp_path) as client:
        page = client.get("/dashboard/learning").text
        assert "Nạp tri thức của bạn" in page          # the door that did not exist

        posted = client.post("/dashboard/learning/add", data={
            "repo": lessons.SHARED_BUCKET,
            "text": "- Validate every DTO\n- No SaveChanges in a loop",
        }, follow_redirects=True)
        assert posted.status_code == 200

        page = client.get("/dashboard/learning").text
        assert "Validate every DTO" in page
        assert "Brief kế tiếp sẽ mang" in page          # the output, not just inventory
        assert "bạn nhập" in page                        # provenance is on the row
    stored = lessons.entries(str(tmp_path), lessons.SHARED_BUCKET)
    assert len(stored) == 2 and all(le.authored for le in stored)


def test_the_page_can_reword_a_line_in_place(tmp_path):
    lessons.add(str(tmp_path), lessons.SHARED_BUCKET, "validate dtos")
    with _client(tmp_path) as client:
        client.post("/dashboard/learning/edit", data={
            "repo": lessons.SHARED_BUCKET, "old": "validate dtos",
            "text": "Validate DTOs at the edge",
        }, follow_redirects=True)
    texts = [le.text for le in lessons.entries(str(tmp_path), lessons.SHARED_BUCKET)]
    assert texts == ["Validate DTOs at the edge"]


def test_adding_without_a_workspace_says_so_rather_than_failing_quietly(tmp_path):
    cfg = Settings(dry_run=True, workspace_directory="",
                   database_url=f"sqlite+aiosqlite:///{tmp_path / 'k.db'}")
    cfg.dashboard_auth_password_hash = ""
    with TestClient(create_app(cfg)) as client:
        page = client.post("/dashboard/learning/add", data={"text": "a rule"},
                           follow_redirects=True).text
        assert "workspace_directory" in page


# ── learning from a real PR review ───────────────────────────────────────────


def test_the_addressing_is_stripped_but_the_ask_survives():
    """A comment is "@bot /ai <the actual ask>". The addressing is plumbing; the ask
    is the only part worth carrying to the next run."""
    from ai_autopilot.learning import teachable_ask

    assert teachable_ask("@ai-bot /ai dùng ILogger thay cho Console.WriteLine ở service") \
        == "dùng ILogger thay cho Console.WriteLine ở service"
    assert teachable_ask("<div>validate DTO trước khi map sang entity nhé</div>") \
        == "validate DTO trước khi map sang entity nhé"
    assert teachable_ask("/ai fix it") == ""            # about this PR, not the next
    assert teachable_ask("") == ""


async def test_a_revision_round_files_the_reviewers_words(tmp_path):
    """End to end: the babysitter counts a revision round, and the sentence that
    caused it lands where the next brief will read it."""
    from types import SimpleNamespace

    from ai_autopilot.data import Database, QualityRepository
    from ai_autopilot.learning import QualityLog
    from ai_autopilot.services.pr_monitor import PrMonitorService

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'q.db'}")
    await db.create_all()
    cfg = Settings(learning_loop_enabled=True, workspace_directory=str(tmp_path / "ws"))
    quality = QualityLog(QualityRepository(db), cfg)

    svc = PrMonitorService.__new__(PrMonitorService)
    svc._config = cfg
    svc._revision_counts = {}
    # `_repo` is a property reading off the container — no pr_command_repo means the
    # durable revision store is simply absent, which is the case this test wants.
    svc._c = SimpleNamespace(quality_repo=quality, pr_command_repo=None)

    await svc._set_revisions(
        42, 1, asked="gom SaveChanges ra ngoài vòng lặp, đang gọi mỗi dòng một lần",
        actor="Thach Pham",
    )

    carried = lessons.recent(str(tmp_path / "ws"), [], limit=8)
    assert any("SaveChanges" in line for line in carried)
    assert any("Thach Pham" in line for line in carried)
    await db.dispose()


# ── pooling knowledge across the fleet ───────────────────────────────────────


def _central(tmp_path, **over):
    cfg = Settings(
        dry_run=True, fleet_role="central", fleet_token=TOKEN,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'central.db'}", **over,
    )
    cfg.dashboard_auth_password_hash = ""
    return cfg


def _push(client, worker, items):
    from ai_autopilot import fleet

    payload = fleet.KnowledgeExchange(
        worker=worker, items=[fleet.KnowledgeLine(**i) for i in items],
    )
    return client.post("/api/fleet/knowledge", json=payload.model_dump(mode="json"),
                       headers={fleet.TOKEN_HEADER: TOKEN})


def _line(text, **over):
    return {"key": lessons.normalize(text), "text": text, "repo": "repo",
            "source": "learned", "count": 1, **over}


def test_knowledge_needs_a_token_like_everything_else_on_this_endpoint(tmp_path):
    with TestClient(create_app(_central(tmp_path))) as client:
        from ai_autopilot import fleet

        answer = client.post("/api/fleet/knowledge", json={"worker": "w", "items": []},
                             headers={fleet.TOKEN_HEADER: "wrong"})
        assert answer.status_code == 401


def test_one_machines_lesson_is_held_back_until_a_human_approves(tmp_path):
    """The blast radius of auto-merge here is the whole fleet, and a wrong lesson is
    re-taught on every run of every machine."""
    with TestClient(create_app(_central(tmp_path, fleet_knowledge_auto_promote=2))) as client:
        answer = _push(client, "tram-01", [_line("always delete the migration folder")])
        assert answer.status_code == 200
        body = answer.json()
        assert body["accepted"] == 1
        assert body["items"] == []          # nothing comes back down yet
        assert body["pending"] == 1


def test_the_same_lesson_from_enough_machines_promotes_itself(tmp_path):
    """One machine tripping over something is an anecdote. Several machines
    independently reporting it is corroboration — that is the signal, not a guess."""
    text = "gom SaveChanges ra ngoài vòng lặp"
    with TestClient(create_app(_central(tmp_path, fleet_knowledge_auto_promote=2))) as client:
        assert _push(client, "tram-01", [_line(text)]).json()["items"] == []
        second = _push(client, "tram-02", [_line(text)]).json()
        assert [i["text"] for i in second["items"]] == [text]
        assert second["items"][0]["source"] == "fleet"
        # …and the third machine gets it without having made the mistake at all.
        third = _push(client, "tram-03", [_line("something else entirely")]).json()
        assert text in [i["text"] for i in third["items"]]


def test_a_rule_a_human_typed_does_not_need_corroboration(tmp_path):
    with TestClient(create_app(_central(tmp_path, fleet_knowledge_auto_promote=5))) as client:
        body = _push(client, "tram-01",
                     [_line("Validate every DTO", source="authored")]).json()
        assert [i["text"] for i in body["items"]] == ["Validate every DTO"]


def test_a_rejected_lesson_does_not_come_back_every_beat(tmp_path):
    """The machines that still hold it locally re-contribute it forever, so deleting
    the row would put the same line in front of the reviewer again and again."""
    cfg = _central(tmp_path, fleet_knowledge_auto_promote=2)
    with TestClient(create_app(cfg)) as client:
        _push(client, "tram-01", [_line("a bad lesson")])
        key = lessons.normalize("a bad lesson")
        client.post("/dashboard/learning/pool/rejected", data={"key": key},
                    follow_redirects=True)
        # Two more machines report it — enough to auto-promote, if it were still a draft.
        _push(client, "tram-02", [_line("a bad lesson")])
        body = _push(client, "tram-03", [_line("a bad lesson")]).json()
        assert body["items"] == []
        assert body["pending"] == 0


def test_the_central_page_lists_what_the_fleet_sent_and_can_approve_it(tmp_path):
    cfg = _central(tmp_path, fleet_knowledge_auto_promote=0, learning_loop_enabled=True,
                   workspace_directory=str(tmp_path / "ws"))
    with TestClient(create_app(cfg)) as client:
        _push(client, "tram-01", [_line("đừng gọi API trong vòng lặp render")])
        page = client.get("/dashboard/learning").text
        assert "Tri thức đội gửi về" in page
        assert "đừng gọi API trong vòng lặp render" in page
        assert "tram-01" in page and "chờ duyệt" in page

        client.post("/dashboard/learning/pool/approved",
                    data={"key": lessons.normalize("đừng gọi API trong vòng lặp render")},
                    follow_redirects=True)
        assert _push(client, "tram-02", []).json()["items"][0]["repo"] == "repo"


def test_a_worker_has_no_review_queue(tmp_path):
    """Approving for the whole fleet from whichever machine you happen to be sitting at
    is how a wrong lesson spreads before anyone with context sees it."""
    cfg = Settings(dry_run=True, fleet_role="worker", learning_loop_enabled=True,
                   workspace_directory=str(tmp_path / "ws"),
                   database_url=f"sqlite+aiosqlite:///{tmp_path / 'w.db'}")
    cfg.dashboard_auth_password_hash = ""
    with TestClient(create_app(cfg)) as client:
        assert "Tri thức đội gửi về" not in client.get("/dashboard/learning").text
        assert client.post("/dashboard/learning/pool/approved",
                           data={"key": "x"}).status_code == 404


def test_what_the_centre_approves_lands_pinned_on_the_worker(tmp_path):
    """It has been through a human at the centre — more review than a locally learned
    line ever gets — so it outranks this machine's own guesses in the brief."""
    ws = str(tmp_path)
    for i in range(10):
        lessons.record_lessons(ws, lessons.SHARED_BUCKET, [f"local guess {i}"], now=_day(9))

    added = lessons.apply_fleet(ws, [(lessons.SHARED_BUCKET, "TEAM RULE: validate at the edge")])

    assert added == 1
    got = lessons.recent(ws, [], limit=3)
    assert got[0] == "TEAM RULE: validate at the edge"
    item = next(le for le in lessons.entries(ws, lessons.SHARED_BUCKET) if le.pinned)
    assert item.source == lessons.SOURCE_FLEET and item.authored is False


def test_knowledge_arriving_twice_by_two_routes_is_not_stored_twice(tmp_path):
    ws = str(tmp_path)
    lessons.record_lessons(ws, "repo", ["validate at the edge"], now=_day(8))
    assert lessons.apply_fleet(ws, [("repo", "Validate at the edge")]) == 0
    items = lessons.entries(ws, "repo")
    assert len(items) == 1 and items[0].pinned          # merged, and promoted


# ── files an older build left behind ─────────────────────────────────────────


def test_compaction_folds_what_an_older_build_accumulated(tmp_path):
    """Dedup-by-meaning and the dead-pointer rule both run on WRITE, so a file already
    on disk kept everything it had: five identical reopens and two useless pointers
    went on eating seven of the eight slots after the upgrade that fixed them — which
    reads, fairly, as the fix having done nothing."""
    path = tmp_path / ".autopilot" / "lessons" / "_workspace.md"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join([
        f"- [2026-09-0{i}] {REOPENED} Context: reopened from state 'S{i}'" for i in range(1, 6)
    ] + [
        "- [2026-09-11] A human reviewer (Thach Pham) blocked a previous PR [Rejected]. "
        "Re-read the review comments on that PR before repeating the same approach.",
        "- [2026-09-12] A human reviewer (Phong Huynh) blocked a previous PR [Waiting]. "
        "Re-read the review comments on that PR before repeating the same approach.",
        "- [2026-09-13] Tests failed on a previous run — check this before opening a PR",
    ]) + "\n", encoding="utf-8")
    ws = str(tmp_path)
    assert len(lessons.entries(ws, lessons.SHARED_BUCKET)) == 8

    merged, dropped = lessons.compact_all(ws)

    assert (merged, dropped) == (4, 2)
    items = lessons.entries(ws, lessons.SHARED_BUCKET)
    assert len(items) == 2
    reopened = next(le for le in items if "reopened" in le.text)
    assert reopened.count == 5 and reopened.date == "2026-09-05"
    assert not any("Re-read the review comments" in le.text for le in items)


def test_compacting_an_already_clean_file_rewrites_nothing(tmp_path):
    ws = str(tmp_path)
    lessons.add(ws, "repo", "a rule")
    before = (tmp_path / ".autopilot" / "lessons" / "repo.md").read_text(encoding="utf-8")
    assert lessons.compact_all(ws) == (0, 0)
    assert (tmp_path / ".autopilot" / "lessons" / "repo.md").read_text(encoding="utf-8") == before


def test_startup_compacts_what_is_already_on_disk(tmp_path):
    """The upgrade has to fix the file you ALREADY have, or it looks like nothing
    happened — which is exactly how it was reported."""
    path = tmp_path / ".autopilot" / "lessons" / "repo.md"
    path.parent.mkdir(parents=True)
    path.write_text("- [2026-09-01] same thing\n- [2026-09-02] Same Thing\n",
                    encoding="utf-8")
    with _client(tmp_path):
        pass                                # starting the app IS the action under test
    items = lessons.entries(str(tmp_path), "repo")
    assert len(items) == 1 and items[0].count == 2


def test_the_page_can_compact_on_demand(tmp_path):
    """The button is for after a hand-edit, with the process already running."""
    with _client(tmp_path) as client:
        path = tmp_path / ".autopilot" / "lessons" / "repo.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("- [2026-09-01] same thing\n- [2026-09-02] Same Thing\n",
                        encoding="utf-8")
        assert "Đã dọn" in client.post(
            "/dashboard/learning/compact", follow_redirects=True
        ).text
    items = lessons.entries(str(tmp_path), "repo")
    assert len(items) == 1 and items[0].count == 2


# ── per-repo base branches (optional; shared branch stays the default) ───────


def test_every_repo_shares_the_base_branch_until_one_is_named():
    cfg = Settings(base_branch="development")
    assert cfg.branch_for_repo("Backend-Fresh") == "development"
    assert cfg.branch_for_repo("Micro-Frontend") == "development"
    assert cfg.branch_for_repo("") == "development"


def test_a_named_repo_cuts_from_its_own_branch():
    """A split BE/FE is the ordinary case: the API on `development`, the UI on `main`."""
    cfg = Settings(base_branch="development", repo_branches=["Micro-Frontend = main"])
    assert cfg.branch_for_repo("Micro-Frontend") == "main"
    assert cfg.branch_for_repo("micro-frontend") == "main"      # folder vs typed case
    assert cfg.branch_for_repo("Backend-Fresh") == "development"
