"""Customer confirmations and assumption answers: kept as evidence, replaced when answered."""

from __future__ import annotations

import json

import pytest

from ai_autopilot.data import AssumptionRepository, CustomerConfirmationRepository, Database


@pytest.fixture
async def repos(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'ba.db'}")
    await db.create_all()
    yield CustomerConfirmationRepository(db), AssumptionRepository(db)
    await db.dispose()


# ── customer confirmations ─────────────────────────────────────────────────────


async def test_a_confirmation_round_trips_with_its_frozen_criteria(repos):
    conf, _ = repos
    acs = [{"id": "AC-01", "text": "Khi bấm Xuất thì tải về xlsx"}]
    new_id = await conf.add(
        kind="spec_confirm", spec_path="specs/a.md", work_item_id=8530,
        spec_hash="h1", ac_json=json.dumps(acs), confirmed_by="Chị Lan",
        channel="email", note="OK", recorded_by="ba")
    assert new_id > 0
    row = await conf.latest("specs/a.md")
    assert row is not None
    assert (row.confirmed_by, row.channel, row.spec_hash) == ("Chị Lan", "email", "h1")
    assert json.loads(row.ac_json) == acs
    assert row.outcome == "confirmed" and row.created_at is not None


async def test_confirmations_are_append_only_and_latest_is_the_newest(repos):
    """A second confirmation must not rewrite the first: the history of what the customer
    agreed to, and when, is the thing worth keeping."""
    conf, _ = repos
    await conf.add(kind="spec_confirm", spec_path="s.md", spec_hash="v1", confirmed_by="A")
    await conf.add(kind="spec_confirm", spec_path="s.md", spec_hash="v2", confirmed_by="B")
    assert (await conf.latest("s.md")).spec_hash == "v2"
    history = await conf.for_spec("s.md")
    assert [r.spec_hash for r in history] == ["v2", "v1"]


async def test_latest_is_per_spec_and_per_kind(repos):
    conf, _ = repos
    await conf.add(kind="spec_confirm", spec_path="a.md", spec_hash="x")
    await conf.add(kind="acceptance", spec_path="a.md", spec_hash="y")
    await conf.add(kind="spec_confirm", spec_path="b.md", spec_hash="z")
    assert (await conf.latest("a.md", "spec_confirm")).spec_hash == "x"
    assert (await conf.latest("a.md", "acceptance")).spec_hash == "y"
    assert (await conf.latest("b.md", "acceptance")) is None
    assert (await conf.latest("nope.md")) is None


async def test_a_rejection_is_kept_like_an_approval(repos):
    conf, _ = repos
    await conf.add(kind="acceptance", spec_path="a.md", outcome="rejected", note="thiếu AC-03")
    assert (await conf.latest("a.md", "acceptance")).outcome == "rejected"


async def test_recent_lists_newest_first_and_respects_the_limit(repos):
    conf, _ = repos
    for i in range(5):
        await conf.add(kind="spec_confirm", spec_path=f"s{i}.md")
    rows = await conf.recent(limit=3)
    assert [r.spec_path for r in rows] == ["s4.md", "s3.md", "s2.md"]


async def test_overlong_names_are_truncated_not_rejected(repos):
    conf, _ = repos
    await conf.add(kind="spec_confirm", spec_path="a.md", confirmed_by="x" * 500,
                   channel="y" * 500, recorded_by="z" * 500)
    row = await conf.latest("a.md")
    assert len(row.confirmed_by) == 200 and len(row.channel) == 100 and len(row.recorded_by) == 200


# ── assumption answers ─────────────────────────────────────────────────────────


async def test_an_answer_is_stored_and_read_back_by_question_key(repos):
    _, asm = repos
    await asm.answer(spec_path="a.md", question_key="k1", question="Kỳ tối đa?",
                     answer="12 tháng", answered_by="Lan", work_item_id=8530)
    got = await asm.answers_for("a.md")
    assert got["k1"]["answer"] == "12 tháng" and got["k1"]["answered_by"] == "Lan"
    assert got["k1"]["answered_at"]


async def test_answering_again_replaces_the_answer_instead_of_stacking(repos):
    """One row per (spec, question): a second answer must replace the first, never leave
    two contradicting answers for the same question."""
    _, asm = repos
    await asm.answer(spec_path="a.md", question_key="k1", question="Q", answer="6 tháng", answered_by="A")
    await asm.answer(spec_path="a.md", question_key="k1", question="Q", answer="12 tháng", answered_by="B")
    got = await asm.answers_for("a.md")
    assert len(got) == 1
    assert got["k1"]["answer"] == "12 tháng" and got["k1"]["answered_by"] == "B"
    assert len(await asm.recent()) == 1


async def test_answers_are_scoped_to_their_spec(repos):
    _, asm = repos
    await asm.answer(spec_path="a.md", question_key="k", question="Q", answer="x")
    await asm.answer(spec_path="b.md", question_key="k", question="Q", answer="y")
    assert (await asm.answers_for("a.md"))["k"]["answer"] == "x"
    assert (await asm.answers_for("b.md"))["k"]["answer"] == "y"
    assert await asm.answers_for("c.md") == {}


async def test_work_item_id_is_kept_when_a_later_answer_omits_it(repos):
    _, asm = repos
    await asm.answer(spec_path="a.md", question_key="k", question="Q", answer="x", work_item_id=77)
    await asm.answer(spec_path="a.md", question_key="k", question="Q", answer="y")
    row = (await asm.recent())[0]
    assert row.work_item_id == 77 and row.answer == "y"
