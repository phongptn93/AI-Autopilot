"""Tests for the retrospective learning-loop store."""

from __future__ import annotations

from datetime import datetime

from ai_autopilot import lessons


def test_record_and_read_round_trip(tmp_path):
    ws = str(tmp_path)
    now = datetime(2026, 7, 29)
    lessons.record_lessons(ws, "Backend", ["[High] null check missing"], now=now)
    lessons.record_lessons(ws, "Backend", ["[Medium] add unit test"], now=now)
    got = lessons.read_lessons(ws, "Backend")
    assert got == ["[High] null check missing", "[Medium] add unit test"]  # newest last
    # A stored file with the date prefix exists and is human-readable.
    text = (tmp_path / ".autopilot" / "lessons" / "Backend.md").read_text(encoding="utf-8")
    assert "2026-07-29" in text and "null check missing" in text


def test_record_dedupes(tmp_path):
    ws = str(tmp_path)
    now = datetime(2026, 7, 29)
    lessons.record_lessons(ws, "Repo", ["same lesson"], now=now)
    lessons.record_lessons(ws, "Repo", ["same lesson", "new one"], now=now)
    assert lessons.read_lessons(ws, "Repo") == ["same lesson", "new one"]


def test_record_caps_history(tmp_path):
    ws = str(tmp_path)
    now = datetime(2026, 7, 29)
    lessons.record_lessons(ws, "Repo", [f"lesson {i}" for i in range(80)], now=now)
    kept = lessons.read_lessons(ws, "Repo", limit=100)
    assert len(kept) == 50 and kept[-1] == "lesson 79"  # oldest dropped, newest kept


def test_lessons_brief_empty_when_none(tmp_path):
    assert lessons.lessons_brief(str(tmp_path), ["Nope"]) == ""


def test_lessons_brief_lists_across_repos(tmp_path):
    ws = str(tmp_path)
    now = datetime(2026, 7, 29)
    lessons.record_lessons(ws, "BE", ["BE lesson"], now=now)
    lessons.record_lessons(ws, "FE", ["FE lesson"], now=now)
    brief = lessons.lessons_brief(ws, ["BE", "FE"])
    assert "Lessons from past runs" in brief
    assert "BE lesson" in brief and "FE lesson" in brief


def test_read_missing_is_empty(tmp_path):
    assert lessons.read_lessons(str(tmp_path), "ghost") == []
    assert lessons.read_lessons("", "x") == []  # no workspace → no-op


def test_brief_and_injected_count_agree(tmp_path):
    """The badge counts the SAME list the brief carries — no over-claiming."""
    ws = str(tmp_path)
    now = datetime(2026, 7, 29)
    lessons.record_lessons(ws, "BE", [f"lesson {i}" for i in range(10)], now=now)
    picked = lessons.recent(ws, ["BE"], limit=3)
    brief = lessons.lessons_brief(ws, ["BE"], limit=3)
    assert picked == ["lesson 7", "lesson 8", "lesson 9"]        # newest 3
    assert all(p in brief for p in picked) and "lesson 6" not in brief


def test_zero_limit_records_but_injects_nothing(tmp_path):
    ws = str(tmp_path)
    lessons.record_lessons(ws, "BE", ["a lesson"], now=datetime(2026, 7, 29))
    assert lessons.recent(ws, ["BE"], limit=0) == []
    assert lessons.lessons_brief(ws, ["BE"], limit=0) == ""
    assert lessons.read_lessons(ws, "BE") == ["a lesson"]        # still remembered


def test_entries_expose_date_and_repo_newest_first(tmp_path):
    ws = str(tmp_path)
    lessons.record_lessons(ws, "BE", ["older"], now=datetime(2026, 7, 28))
    lessons.record_lessons(ws, "BE", ["newer"], now=datetime(2026, 7, 29))
    got = lessons.entries(ws, "BE")
    assert [e.text for e in got] == ["newer", "older"]
    assert got[0].date == "2026-07-29" and got[0].repo == "BE"
    assert lessons.list_repos(ws) == ["BE"]


def test_delete_removes_one_and_clear_removes_the_file(tmp_path):
    ws = str(tmp_path)
    now = datetime(2026, 7, 29)
    lessons.record_lessons(ws, "BE", ["keep me", "wrong lesson"], now=now)
    assert lessons.delete(ws, "BE", "wrong lesson") is True
    assert lessons.read_lessons(ws, "BE") == ["keep me"]
    assert lessons.delete(ws, "BE", "never stored") is False
    assert lessons.clear(ws, "BE") is True
    assert lessons.list_repos(ws) == []


def test_deleting_the_last_lesson_drops_the_repo(tmp_path):
    ws = str(tmp_path)
    lessons.record_lessons(ws, "BE", ["only one"], now=datetime(2026, 7, 29))
    assert lessons.delete(ws, "BE", "only one") is True
    assert lessons.list_repos(ws) == []          # no ghost repo with an empty file


def test_per_day_counts_new_lessons(tmp_path):
    ws = str(tmp_path)
    lessons.record_lessons(ws, "BE", ["a", "b"], now=datetime(2026, 7, 28))
    lessons.record_lessons(ws, "FE", ["c"], now=datetime(2026, 7, 29))
    assert lessons.per_day(ws, today="2026-07-29") == [("2026-07-28", 2), ("2026-07-29", 1)]


def test_per_day_keeps_the_quiet_days(tmp_path):
    """"A falling tail means the loop is working" is only readable if the quiet days
    are drawn. Skipping them puts two busy days side by side and calls it a trend."""
    ws = str(tmp_path)
    lessons.record_lessons(ws, "BE", ["a"], now=datetime(2026, 8, 12))
    lessons.record_lessons(ws, "BE", ["b"], now=datetime(2026, 8, 15))
    series = lessons.per_day(ws, today="2026-08-17")
    assert [n for _, n in series] == [1, 0, 0, 1, 0, 0]
    assert series[0][0] == "2026-08-12" and series[-1][0] == "2026-08-17"


def test_per_day_is_empty_without_lessons(tmp_path):
    assert lessons.per_day(str(tmp_path)) == []


def test_repo_name_cannot_escape_the_lessons_dir(tmp_path):
    """A repo name is used to build a path — traversal must be neutralised."""
    ws = str(tmp_path)
    lessons.record_lessons(ws, "../../etc", ["nope"], now=datetime(2026, 7, 29))
    lessons_dir = tmp_path / ".autopilot" / "lessons"
    written = list(lessons_dir.glob("*.md"))
    assert len(written) == 1
    # Separators are stripped, so the name can never climb out of the lessons dir.
    assert written[0].parent == lessons_dir
    assert "/" not in written[0].name and "\\" not in written[0].name


# ── the workspace's own Claude memory ────────────────────────────────────────

_NOW_MEM = datetime(2026, 9, 23)


def test_lessons_are_written_where_claude_already_looks(tmp_path):
    """The agent runs with setting_sources=["user","project","local"], so it loads the
    workspace's CLAUDE.md and .claude/ rules by itself. Prepending the newest eight lines
    to every brief was the weaker channel: it ignored whether a lesson had anything to do
    with the task, capped the store at eight slots a burst of machine noise could take,
    and lived in a dotfolder nobody reviews.
    """
    ws = str(tmp_path)
    lessons.add(ws, lessons.SHARED_BUCKET, "Validate every DTO before mapping")
    lessons.record_lessons(ws, "Backend", ["[High] null check missing"], now=_NOW_MEM)

    rule = tmp_path / ".claude" / "rules" / "autopilot-lessons.md"
    assert rule.is_file(), "the agent reads .claude/rules — that is where instructions belong"
    body = rule.read_text(encoding="utf-8")
    # The human's rule is IN the always-loaded file: they typed it so it would always hold.
    assert "Validate every DTO before mapping" in body
    assert "Quy tắc do người viết" in body
    # The machine's guess is NOT — it is named, and its body lives in a skill.
    assert "[High] null check missing" not in body
    assert "autopilot-lessons-backend" in body


def test_deleting_a_lesson_takes_it_out_of_what_the_agent_reads(tmp_path):
    ws = str(tmp_path)
    lessons.add(ws, lessons.SHARED_BUCKET, "a rule that turns out to be wrong")
    rule = tmp_path / ".claude" / "rules" / "autopilot-lessons.md"
    assert "turns out to be wrong" in rule.read_text(encoding="utf-8")

    lessons.delete(ws, lessons.SHARED_BUCKET, "a rule that turns out to be wrong")
    # An emptied store removes the file rather than leaving an empty rule behind.
    assert not rule.exists()


def test_the_claude_md_pointer_keeps_the_file_s_own_line_endings(tmp_path):
    """Measured before this was handled: reading CLAUDE.md with the default newline mode
    and writing it back turned 338 LF endings into 342 CRLF — every line of a tracked
    20KB file showing as changed, from a tool that only meant to add one pointer.
    """
    claude_md = tmp_path / "CLAUDE.md"
    original = b"# Project\r\n\r\nSome convention.\r\n"
    claude_md.write_bytes(original)
    lessons.add(str(tmp_path), lessons.SHARED_BUCKET, "a lesson worth keeping")

    after = claude_md.read_bytes()
    assert after.startswith(b"# Project\r\n\r\nSome convention.")
    assert after.count(b"\n") == after.count(b"\r\n"), "a bare LF crept into a CRLF file"
    assert b"autopilot-lessons.md" in after          # the pointer is there

    # …and it is removed again when there is nothing to point at.
    lessons.delete(str(tmp_path), lessons.SHARED_BUCKET, "a lesson worth keeping")
    assert claude_md.read_bytes() == original


def test_writing_the_memory_twice_changes_nothing(tmp_path):
    """It runs on every mutation, so a non-idempotent write would churn a tracked file."""
    (tmp_path / "CLAUDE.md").write_text("# Project\n", encoding="utf-8")
    lessons.add(str(tmp_path), lessons.SHARED_BUCKET, "one lesson")
    first = (tmp_path / "CLAUDE.md").read_bytes()
    lessons.sync_memory(str(tmp_path))
    lessons.sync_memory(str(tmp_path))
    assert (tmp_path / "CLAUDE.md").read_bytes() == first


# ── rules vs skills: instructions are pushed, guesses are pulled ─────────────

def test_a_humans_rule_is_always_loaded_and_a_machine_guess_is_not(tmp_path):
    """The split that makes this cheap AND reliable.

    A rule somebody typed is an instruction — short, and typed precisely so that it
    holds on every run, which is what `.claude/rules` does. A line the machine inferred
    from one bad run is a guess: it accumulates, it is situational, and most of it has
    nothing to do with any given task. That goes to a skill, whose body costs nothing
    until something opens it.
    """
    ws = str(tmp_path)
    lessons.add(ws, "Backend", "Validate every DTO before mapping")
    lessons.record_lessons(ws, "Backend", ["[High] null check on CustomerId"], now=_NOW_MEM)

    rule = (tmp_path / ".claude" / "rules" / "autopilot-lessons.md").read_text(encoding="utf-8")
    skill = (tmp_path / ".claude" / "skills" / "autopilot-lessons-backend"
             / "SKILL.md").read_text(encoding="utf-8")

    assert "Validate every DTO before mapping" in rule     # instruction: always loaded
    assert "null check on CustomerId" not in rule          # guess: not in the always-loaded file
    assert "null check on CustomerId" in skill             # …it is here
    assert "autopilot-lessons-backend" in rule             # and the rule names where


def test_the_skill_frontmatter_can_stand_on_its_own(tmp_path):
    """Belt and braces: the brief names this skill outright, but somebody running the
    agent by hand only has the description to go on."""
    ws = str(tmp_path)
    lessons.record_lessons(ws, "Backend", ["Không gọi SaveChanges trong vòng lặp"], now=_NOW_MEM)
    skill = (tmp_path / ".claude" / "skills" / "autopilot-lessons-backend"
             / "SKILL.md").read_text(encoding="utf-8")
    head = skill.split("---")[1]
    assert "name: autopilot-lessons-backend" in head
    assert "Backend" in head and "PR" in head              # names the repo and when to read it
    assert "SaveChanges" in head                           # and what is actually in it


def test_the_brief_names_only_the_skills_for_this_work_items_repos(tmp_path):
    """The selection nobody has to guess at: the autopilot knows which repos a work item
    touches, so it names those skills and no others."""
    ws = str(tmp_path)
    lessons.record_lessons(ws, "Backend", ["backend lesson"], now=_NOW_MEM)
    lessons.record_lessons(ws, "Frontend", ["frontend lesson"], now=_NOW_MEM)

    pointer = lessons.lessons_pointer(ws, ["Backend"])
    assert "autopilot-lessons-backend" in pointer
    assert "autopilot-lessons-frontend" not in pointer, "named a skill for an unrelated repo"
    # …and it points, it does not paste.
    assert "backend lesson" not in pointer


def test_the_shared_bucket_rides_along_with_every_repo(tmp_path):
    """A reopen or a rejection is attached to a work item, not to one repo — it has to
    reach a run whatever repo that run touches."""
    ws = str(tmp_path)
    lessons.record_lessons(ws, lessons.SHARED_BUCKET, ["an unattributable lesson"], now=_NOW_MEM)
    assert "autopilot-lessons-workspace" in lessons.lessons_pointer(ws, ["AnyRepo"])


def test_a_repo_whose_lessons_are_all_deleted_loses_its_skill(tmp_path):
    """Otherwise the skill sits on disk advertising knowledge that no longer exists, and
    the brief goes on naming it."""
    ws = str(tmp_path)
    lessons.record_lessons(ws, "Backend", ["a wrong lesson"], now=_NOW_MEM)
    skill = tmp_path / ".claude" / "skills" / "autopilot-lessons-backend"
    assert skill.is_dir()

    lessons.delete(ws, "Backend", "a wrong lesson")
    assert not skill.exists()
    assert "autopilot-lessons-backend" not in lessons.lessons_pointer(ws, ["Backend"])


def test_a_hand_written_skill_is_never_touched(tmp_path):
    """Pruning only ever removes directories this module owns."""
    ws = str(tmp_path)
    mine = tmp_path / ".claude" / "skills" / "deploy-to-k8s"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\nname: deploy-to-k8s\n---\n", encoding="utf-8")

    lessons.record_lessons(ws, "Backend", ["x"], now=_NOW_MEM)
    lessons.delete(ws, "Backend", "x")

    assert (mine / "SKILL.md").is_file(), "pruned a skill it did not write"


# ── the centre proposes, this machine decides ───────────────────────────────

def test_deleting_a_line_the_centre_sent_is_a_refusal_not_a_delay(tmp_path):
    """Measured before this existed: delete it, wait one beat, and it is back on disk.

    The centre already had a human gate (approve / reject). The machine that has to live
    with the line had none — so "delete" on this page taught operators that the button
    does not work.
    """
    ws = str(tmp_path)
    text = "Luôn dùng ILogger thay cho Console.WriteLine"
    lessons.apply_fleet(ws, [("Backend", text)])
    assert len(lessons.entries(ws, "Backend")) == 1

    lessons.delete(ws, "Backend", text)
    lessons.apply_fleet(ws, [("Backend", text)])      # the next beat
    lessons.apply_fleet(ws, [("Backend", text)])      # and the one after

    assert lessons.entries(ws, "Backend") == []
    assert lessons.normalize(text) in lessons.declined_keys(ws)


def test_a_locally_learned_line_is_deleted_without_being_refused(tmp_path):
    """Only the centre's lines become refusals — deleting one of this machine's own
    guesses must not blacklist the sentence for good."""
    ws = str(tmp_path)
    lessons.record_lessons(ws, "Backend", ["a local guess"], now=_NOW_MEM)
    lessons.delete(ws, "Backend", "a local guess")
    assert lessons.declined_keys(ws) == set()


def test_manual_mode_queues_instead_of_applying(tmp_path):
    ws = str(tmp_path)
    text = "Không gọi SaveChanges trong vòng lặp"
    lessons.apply_fleet(ws, [("Backend", text)], mode=lessons.ACCEPT_MANUAL)

    assert lessons.entries(ws, "Backend") == []        # nothing applied behind our back
    assert lessons.offers(ws) == [("Backend", text)]

    assert lessons.accept_offer(ws, text) is True
    assert [le.text for le in lessons.entries(ws, "Backend")] == [text]
    assert lessons.offers(ws) == []                    # and the queue is emptied


def test_a_queued_line_is_not_offered_twice(tmp_path):
    ws = str(tmp_path)
    for _ in range(3):
        lessons.apply_fleet(ws, [("Backend", "x")], mode=lessons.ACCEPT_MANUAL)
    assert len(lessons.offers(ws)) == 1


def test_declining_a_queued_line_takes_it_out_and_keeps_it_out(tmp_path):
    ws = str(tmp_path)
    text = "một dòng máy này không muốn"
    lessons.apply_fleet(ws, [("Backend", text)], mode=lessons.ACCEPT_MANUAL)
    assert lessons.decline_fleet(ws, text) is True
    assert lessons.offers(ws) == []

    lessons.apply_fleet(ws, [("Backend", text)], mode=lessons.ACCEPT_MANUAL)
    assert lessons.offers(ws) == [], "a refused line was offered again"


def test_the_state_files_are_never_mistaken_for_a_repo(tmp_path):
    """They live beside the per-repo markdown, so a glob that caught them would invent
    a repo called "declined" with one nonsense lesson in it."""
    ws = str(tmp_path)
    lessons.decline_fleet(ws, "nope")
    lessons.apply_fleet(ws, [("Backend", "queued")], mode=lessons.ACCEPT_MANUAL)
    assert lessons.list_repos(ws) == []


def test_accept_mode_is_the_machines_own_call():
    from ai_autopilot.dashboard import settings_form as sf

    assert "fleet_knowledge_accept" in sf.MACHINE_LOCAL
