"""Release note: what was delivered in a window, per tenant, posted on a schedule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from ai_autopilot import doctor
from ai_autopilot import release_notes as rn
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.data.repository import StateChange
from ai_autopilot.models import WorkItemInfo
from ai_autopilot.services import release_note_service as svc_mod
from ai_autopilot.services.release_note_service import ReleaseNoteService

NOW = datetime(2026, 10, 23, 2, 0, tzinfo=UTC)
START = NOW - timedelta(days=7)


def _ch(wid, state, category, days_ago, project="TLCL-DxFac", title=""):
    return StateChange(work_item_id=wid, project=project, state=state, category=category,
                       assigned_to="", title=title or f"t{wid}",
                       entered_at=(NOW - timedelta(days=days_ago)).replace(tzinfo=None))


def _wi(wid, title, tags=(), project="TLCL-DxFac", kind="Task"):
    return WorkItemInfo(id=wid, title=title, tags=list(tags), project=project,
                        work_item_type=kind)


# ── pure rules ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("title", "expected"), [
    ("TLCL.18 - Thêm cột ngày", ("TLCL.18", "Thêm cột ngày")),
    ("VPM.204: Sửa báo cáo", ("VPM.204", "Sửa báo cáo")),
    ("TLLA.7 Xuất Excel", ("TLLA.7", "Xuất Excel")),
    ("Sửa lỗi đăng nhập", ("", "Sửa lỗi đăng nhập")),
    ("tlcl.18 lowercase is not a code", ("", "tlcl.18 lowercase is not a code")),
])
def test_ticket_code_is_taken_from_the_title_and_never_invented(title, expected):
    assert rn.split_ticket(title) == expected


def test_released_counts_the_first_done_entry_inside_the_window_only():
    changes = [
        _ch(1, "Active", "InProgress", 9), _ch(1, "Resolved", "Resolved", 3),    # in
        _ch(2, "Resolved", "Resolved", 10), _ch(2, "Active", "InProgress", 5),
        _ch(2, "Resolved", "Resolved", 2),                                       # repeat: out
        _ch(3, "Active", "InProgress", 1),                                       # not done
    ]
    assert set(rn.released_between(changes, START, NOW)) == {1}


def test_named_release_states_replace_the_category_rule():
    changes = [
        _ch(1, "Pre-Release", "InProgress", 2),       # the team's release state
        _ch(2, "Resolved", "Resolved", 2),            # done, but not "released" for them
        _ch(3, "Pre-Release", "InProgress", 12), _ch(3, "Pre-Release", "InProgress", 1),
    ]
    assert set(rn.released_between(changes, START, NOW, ["pre-release"])) == {1}


def test_groups_follow_the_tenant_order_with_common_last_and_shared_items_in_both():
    items = [
        _wi(10, "VPM.2 B", ["VPM"]), _wi(11, "TLCL.9 A", ["TLCL", "VPM"]),
        _wi(12, "Không tag", ["Feature"]), _wi(13, "TLCL.3 C", ["tlcl"]),
    ]
    released = {i.id: NOW for i in items}
    note = rn.build(items, released, tenant_tags=["TLCL", "TLLA", "VPM"],
                    start=START, end=NOW, days=7, history_since=START - timedelta(days=30))
    assert [g.tenant for g in note.groups] == ["TLCL", "VPM", rn.COMMON]
    assert [e.ticket for e in note.groups[0].entries] == ["TLCL.3", "TLCL.9"]   # by code
    assert {e.id for e in note.groups[1].entries} == {10, 11}                  # shared item
    assert note.total == 4 and not note.partial
    rows = note.customer_rows()
    # The customer version is (ticket, title) only — there is no slot an id could leak into.
    assert all(len(r) == 2 for r in rows) and len(rows) == 4                 # no repeats
    assert ("TLCL.9", "A") in rows and ("", "Không tag") in rows


def test_without_tenant_tags_items_are_grouped_by_project():
    items = [_wi(1, "a", project="Beta"), _wi(2, "b", project="Alpha")]
    note = rn.build(items, {1: NOW, 2: NOW}, tenant_tags=[], start=START, end=NOW, days=7)
    assert [g.tenant for g in note.groups] == ["Alpha", "Beta"]


def test_teams_text_is_bullets_with_links_capped_per_group_and_flags_partial_history():
    items = [_wi(i, f"TLCL.{i} item {i}", ["TLCL"]) for i in range(1, 21)]
    note = rn.build(items, {i.id: NOW for i in items}, tenant_tags=["TLCL"],
                    start=START, end=NOW, days=7, history_since=NOW - timedelta(days=2))
    heading, text = rn.render_teams(note, link_base="https://dev.azure.com/org/_workitems/edit/")
    assert "20 hạng mục" in heading and "| " not in text                # no markdown table
    assert "[#1](https://dev.azure.com/org/_workitems/edit/1) TLCL.1 — item 1" in text
    assert f"và {20 - rn.TEAMS_LINES_PER_GROUP} hạng mục khác" in text
    assert "có thể chưa đủ" in text                                     # partial window said
    empty_heading, empty_text = rn.render_teams(
        rn.ReleaseNote(start=START, end=NOW, days=7))
    assert "0 hạng mục" in empty_heading and "Không có" in empty_text


# ── service ──────────────────────────────────────────────────────────────────


class _History:
    def __init__(self, changes, first=None):
        self.changes, self.first = changes, first or (NOW - timedelta(days=60))
        self.since = None

    async def changes_since(self, since):
        self.since = since
        return list(self.changes)

    async def first_seen(self):
        return self.first.replace(tzinfo=None)


class _Ado:
    def __init__(self, items=(), fail=False):
        self.items, self.fail, self.asked = list(items), fail, None

    async def get_work_items_by_ids(self, ids):
        self.asked = list(ids)
        if self.fail:
            raise RuntimeError("ADO down")
        return [i for i in self.items if i.id in ids]


class _Notifier:
    def __init__(self):
        self.sent = []

    async def notify(self, message):
        self.sent.append(message)


def _container(changes, items=(), fail=False, **cfg):
    base = {"release_note_tenant_tags": ["TLCL", "VPM"], "ado_organization": "https://dev.azure.com/org",
            "ado_project": "TLCL-DxFac"}
    base.update(cfg)
    return SimpleNamespace(config=Settings(**base), state_history=_History(changes),
                           ado=_Ado(items, fail), notifier=_Notifier())


async def test_service_builds_from_history_and_asks_ado_only_for_released_items():
    changes = [_ch(1, "Resolved", "Resolved", 2), _ch(2, "Active", "InProgress", 1),
               _ch(3, "Closed", "Completed", 4, project="Other")]
    c = _container(changes, [_wi(1, "TLCL.5 Done", ["TLCL"]), _wi(3, "Other one", project="Other")])
    note = await ReleaseNoteService(c).build(now=NOW)
    assert sorted(c.ado.asked) == [1, 3]
    assert c.state_history.since == START - timedelta(days=7)      # sees just before window
    assert note.total == 2 and [g.tenant for g in note.groups] == ["TLCL", rn.COMMON]


async def test_service_scopes_by_project_and_an_empty_scope_shows_nothing():
    changes = [_ch(1, "Resolved", "Resolved", 2), _ch(3, "Closed", "Completed", 4, project="Other")]
    c = _container(changes, [_wi(1, "a", ["TLCL"]), _wi(3, "b", project="Other")])
    only = await ReleaseNoteService(c).build(now=NOW, projects=["other"])
    assert only.total == 1 and only.groups[0].entries[0].id == 3
    assert (await ReleaseNoteService(c).build(now=NOW, projects=[])).total == 0


async def test_an_item_ado_cannot_return_still_appears_with_its_recorded_title():
    c = _container([_ch(7, "Resolved", "Resolved", 1, title="TLCL.7 Gone from ADO")], fail=True)
    note = await ReleaseNoteService(c).build(now=NOW)
    assert note.error and note.total == 1
    assert note.groups[0].entries[0].ticket == "TLCL.7"


async def test_send_posts_one_card_with_a_link_to_the_page_when_there_is_a_public_url():
    c = _container([_ch(1, "Resolved", "Resolved", 2)], [_wi(1, "TLCL.5 Done", ["TLCL"])],
                   dashboard_public_url="https://autopilot.example.com/")
    service = ReleaseNoteService(c)
    assert await service.post(await service.build(now=NOW))
    (msg,) = c.notifier.sent
    assert msg.heading.startswith("📣 Release note 7 ngày") and "TLCL.5" in msg.text
    assert msg.actions == [("📣 Mở release note đầy đủ",
                            "https://autopilot.example.com/dashboard/release-notes?days=7")]
    dry = _container([_ch(1, "Resolved", "Resolved", 2)], [_wi(1, "x")], dry_run=True)
    assert not await ReleaseNoteService(dry).post(await ReleaseNoteService(dry).build(now=NOW))
    assert dry.notifier.sent == []


async def test_scheduled_firing_respects_day_quiet_hours_and_empty(monkeypatch):
    monday = datetime(2026, 10, 26, 8, 30)          # a Monday
    c = _container([], release_note_days="Mon")
    service = ReleaseNoteService(c)
    assert await service.run_once(monday + timedelta(days=1)) == "not-today"
    assert await service.run_once(monday) == "empty"                  # nothing → no post
    monkeypatch.setattr(svc_mod.QuietHours, "is_quiet", lambda self, now=None: True)
    assert await service.run_once(monday) == "quiet"
    monkeypatch.setattr(svc_mod.QuietHours, "is_quiet", lambda self, now=None: False)
    c2 = _container([_ch(1, "Resolved", "Resolved", 1)], [_wi(1, "TLCL.1 x", ["TLCL"])],
                    release_note_days="")
    assert await ReleaseNoteService(c2).run_once(monday + timedelta(days=3)) == "sent"
    assert len(c2.notifier.sent) == 1


def test_schedule_starts_only_when_enabled_and_history_is_recorded():
    off = ReleaseNoteService(_container([], release_note_enabled=False))
    off.start()
    assert off._task is None
    no_history = ReleaseNoteService(_container([], release_note_enabled=True,
                                               delivery_history_enabled=False))
    no_history.start()
    assert no_history._task is None


# ── doctor ───────────────────────────────────────────────────────────────────


def test_doctor_names_a_schedule_that_can_never_post():
    assert doctor.check_release_note(Settings(release_note_enabled=False)) == []
    bad = doctor.check_release_note(Settings(
        release_note_enabled=True, release_note_at="8h", release_note_days="Funday",
        delivery_history_enabled=False, timezone=""))
    titles = " | ".join(f.title for f in bad)
    assert "no valid time" in titles and "not recognised" in titles
    assert "state history is off" in titles and "host clock" in titles
    good = doctor.check_release_note(Settings(
        release_note_enabled=True, timezone="Asia/Ho_Chi_Minh"))
    assert [f.level for f in good] == [doctor.OK]


# ── dashboard ────────────────────────────────────────────────────────────────


def _note():
    items = [_wi(42, "TLCL.18 Thêm cột", ["TLCL"])]
    return rn.build(items, {42: NOW}, tenant_tags=["TLCL"], start=START, end=NOW, days=7,
                    history_since=NOW - timedelta(days=30))


def test_page_shows_internal_tables_and_a_customer_version_without_ids(tmp_path, monkeypatch):
    async def fake_build(self, **kw):
        return _note()

    monkeypatch.setattr(ReleaseNoteService, "build", fake_build)
    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'r.db'}",
                   ado_organization="https://dev.azure.com/org", ado_project="P")
    with TestClient(create_app(cfg)) as client:
        html = client.get("/dashboard/release-notes").text
    assert "#42" in html and "TLCL.18" in html and "Thêm cột" in html
    customer = html.split("Bản gửi khách hàng", 1)[1]
    assert "#42" not in customer and "| 1 | TLCL.18 | Thêm cột |" in customer


def test_send_now_posts_and_reports_back(tmp_path, monkeypatch):
    async def fake_build(self, **kw):
        return _note()

    posted = []

    async def fake_post(self, note):
        posted.append(note.total)
        return True

    monkeypatch.setattr(ReleaseNoteService, "build", fake_build)
    monkeypatch.setattr(ReleaseNoteService, "post", fake_post)
    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    with TestClient(create_app(cfg)) as client:
        r = client.post("/dashboard/release-notes/send", data={"days": "7"},
                        follow_redirects=False)
        assert r.status_code == 303 and "sent=sent" in r.headers["location"]
        assert "Đã gửi release note" in client.get(r.headers["location"]).text
    assert posted == [1]
