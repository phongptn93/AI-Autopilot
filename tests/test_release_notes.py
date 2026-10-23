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
    assert "20 lên production" in heading and "| " not in text                # no markdown table
    assert "[#1](https://dev.azure.com/org/_workitems/edit/1) TLCL.1 — item 1" in text
    assert f"và {20 - rn.TEAMS_LINES_PER_GROUP} hạng mục khác" in text
    assert "có thể chưa đủ" in text                                     # partial window said
    empty_heading, empty_text = rn.render_teams(
        rn.ReleaseNote(start=START, end=NOW, days=7))
    assert "0 lên production" in empty_heading and "Không có" in empty_text


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

    async def latest_entries(self, ids):
        latest = {}
        for ch in sorted(self.changes, key=lambda c: c.entered_at):
            if ch.work_item_id in ids:
                latest[ch.work_item_id] = (ch.state, ch.entered_at)
        return latest


class _Ado:
    def __init__(self, items=(), fail=False, waiting=(), waiting_fail=False):
        self.items, self.fail, self.asked = list(items), fail, None
        self.waiting, self.waiting_fail, self.asked_states = list(waiting), waiting_fail, None

    async def get_work_items_by_ids(self, ids):
        self.asked = list(ids)
        if self.fail:
            raise RuntimeError("ADO down")
        return [i for i in self.items if i.id in ids]

    async def get_work_items_in_states(self, states):
        self.asked_states = list(states)
        if self.waiting_fail:
            raise RuntimeError("WIQL down")
        return list(self.waiting)


class _Notifier:
    def __init__(self):
        self.sent = []

    async def notify(self, message):
        self.sent.append(message)


def _container(changes, items=(), fail=False, waiting=(), waiting_fail=False, **cfg):
    base = {"release_note_tenant_tags": ["TLCL", "VPM"], "ado_organization": "https://dev.azure.com/org",
            "ado_project": "TLCL-DxFac", "release_note_ai_enabled": False}
    base.update(cfg)
    return SimpleNamespace(config=Settings(**base), state_history=_History(changes),
                           ado=_Ado(items, fail, waiting, waiting_fail), notifier=_Notifier())


def _ready(wid, title, tags=(), project="TLCL-DxFac", changed_days_ago=None):
    item = _wi(wid, title, tags, project)
    item.state = "Ready for UAT"
    if changed_days_ago is not None:
        item.changed_date = NOW - timedelta(days=changed_days_ago)
    return item


# ── links: the base is ".../_workitems/edit" with NO trailing slash ─────────────


def test_links_put_a_slash_between_the_base_and_the_id():
    """The real base (work_item_link_base) has no trailing slash; gluing the id on bare
    produced .../_workitems/edit8357 — a link to nothing, in every card ever sent."""
    base = "https://dev.azure.com/newoceanis/_workitems/edit"
    assert rn.item_url(base, 8357) == f"{base}/8357"
    assert rn.item_url(base + "/", 8357) == f"{base}/8357"
    assert rn.item_url("", 8357) == ""
    note = rn.build([_wi(8357, "VPM_UAT báo cáo", ["TLCL"])], {8357: NOW}, tenant_tags=["TLCL"],
                    start=START, end=NOW, days=7)
    _, text = rn.render_teams(note, link_base=base)
    assert f"({base}/8357)" in text and "edit8357" not in text
    assert "edit8357" not in str(rn.render_card(note, link_base=base))


# ── waiting queue ────────────────────────────────────────────────────────────


def test_waiting_queue_is_oldest_first_aged_from_history_else_marked_approximate():
    items = [_ready(1, "TLCL.1 Mới", ["TLCL"]), _ready(2, "TLCL.2 Cũ", ["TLCL"]),
             _ready(3, "Không lịch sử", ["VPM"], changed_days_ago=4), _ready(4, "Không gì cả")]
    entered = {1: NOW - timedelta(days=1), 2: NOW - timedelta(days=12)}
    queue = rn.build_waiting(items, entered, tenant_tags=["TLCL", "VPM"])
    assert [w.id for w in queue] == [2, 3, 1, 4]                  # unknown age last
    assert queue[0].ticket == "TLCL.2" and queue[0].age_days(NOW) == 12 and not queue[0].approx
    assert queue[1].approx and queue[1].tenants == ("VPM",)       # from ChangedDate: "≥ 4"
    assert queue[3].age_days(NOW) is None and queue[3].tenants == (rn.COMMON,)
    note = rn.ReleaseNote(start=START, end=NOW, days=7, waiting=queue,
                          waiting_states=["Ready for UAT"], stale_days=7)
    assert [w.id for w in note.stale()] == [2] and note.oldest_wait() == 12
    assert note.waiting_by_tenant() == [("TLCL", 2), ("VPM", 1), (rn.COMMON, 1)]
    _, text = rn.render_teams(note)
    assert "Chờ deploy (4)" in text and "12 ngày" in text and "≥ 4 ngày" in text


async def test_service_reads_the_queue_scopes_it_and_dates_it_only_from_a_matching_state():
    changes = [_ch(1, "Ready for UAT", "Resolved", 9), _ch(2, "Ready for UAT", "Resolved", 20),
               _ch(2, "Rework Required", "InProgress", 3)]       # history lags ADO for #2
    waiting = [_ready(1, "TLCL.1 a", ["TLCL"]),
               _ready(2, "TLCL.2 b", ["TLCL"], changed_days_ago=1),
               _ready(3, "c", project="Other")]
    c = _container(changes, waiting=waiting, release_note_waiting_states=["Ready for UAT"],
                   release_note_stale_days=5)
    note = await ReleaseNoteService(c).build(now=NOW, projects=["TLCL-DxFac"])
    assert c.ado.asked_states == ["Ready for UAT"]
    assert [w.id for w in note.waiting] == [1, 2]                  # #3 out of scope
    first, second = note.waiting
    assert first.age_days(NOW) == 9 and not first.approx
    assert second.approx and second.age_days(NOW) == 1             # not the stale 20-day row
    assert note.stale_days == 5 and [w.id for w in note.stale()] == [1]
    off = _container(changes, waiting=waiting)                     # no waiting states → no query
    assert (await ReleaseNoteService(off).build(now=NOW)).waiting == [] and off.ado.asked_states is None
    broken = _container([], waiting_fail=True, release_note_waiting_states=["Ready for UAT"])
    assert "chờ deploy" in (await ReleaseNoteService(broken).build(now=NOW)).error


async def test_a_quiet_morning_still_posts_when_the_queue_is_not_empty():
    c = _container([], waiting=[_ready(9, "TLCL.9 Chờ", ["TLCL"], changed_days_ago=10)],
                   release_note_waiting_states=["Ready for UAT"], release_note_daily_enabled=True)
    sch = next(s for s in svc_mod.schedules(c.config) if s.daily)
    tuesday = datetime(2026, 10, 27, 1, 0, tzinfo=UTC)
    assert await ReleaseNoteService(c).run_once(tuesday, sch) == "sent"
    (msg,) = c.notifier.sent
    assert "0 lên production · 1 chờ deploy" in msg.heading


async def test_the_teams_post_is_a_native_card_with_kpis_and_both_sections():
    from ai_autopilot.notifications.teams import TeamsNotifier

    c = _container([_ch(1, "In UAT", "Resolved", 1)], [_wi(1, "TLCL.5 Done", ["TLCL"])],
                   waiting=[_ready(9, "TLCL.9 Chờ", ["TLCL"], changed_days_ago=10)],
                   release_note_states=["In UAT"], release_note_waiting_states=["Ready for UAT"])
    service = ReleaseNoteService(c)
    assert await service.post(await service.build(now=NOW))
    (msg,) = c.notifier.sent
    body = TeamsNotifier._payload(msg)["attachments"][0]["content"]["body"]
    flat = str(body)
    assert body[0]["text"] == msg.heading and body[1:] == msg.card
    assert "ColumnSet" in flat and "ĐÃ LÊN PRODUCTION" in flat and "CHỜ DEPLOY (Ready for UAT)" in flat
    assert "https://dev.azure.com/org/TLCL-DxFac/_workitems/edit/9" in flat
    assert "⚠️ ≥ 10 ngày" in flat                                  # past stale_days → flagged
    assert msg.text and "TLCL.5" in msg.text                       # fallback for email/Zalo


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
    assert msg.heading.startswith("📣 Release 7 ngày") and "TLCL.5" in msg.text
    assert msg.actions == [("📣 Mở release note đầy đủ",
                            "https://autopilot.example.com/dashboard/release-notes?days=7")]
    dry = _container([_ch(1, "Resolved", "Resolved", 2)], [_wi(1, "x")], dry_run=True)
    assert not await ReleaseNoteService(dry).post(await ReleaseNoteService(dry).build(now=NOW))
    assert dry.notifier.sent == []


async def test_scheduled_firing_respects_day_quiet_hours_and_empty(monkeypatch):
    monday = datetime(2026, 10, 26, 8, 30, tzinfo=UTC)          # a Monday
    c = _container([], release_note_days="Mon")
    service = ReleaseNoteService(c)
    assert await service.run_once(monday + timedelta(days=1)) == "not-today"
    assert await service.run_once(monday) == "empty"                  # nothing → no post
    monkeypatch.setattr(svc_mod.QuietHours, "is_quiet", lambda self, now=None: True)
    assert await service.run_once(monday) == "quiet"
    monkeypatch.setattr(svc_mod.QuietHours, "is_quiet", lambda self, now=None: False)
    c2 = _container([_ch(1, "Resolved", "Resolved", 1)], [_wi(1, "TLCL.1 x", ["TLCL"])],
                    release_note_days="")
    assert await ReleaseNoteService(c2).run_once(NOW) == "sent"     # cut at the firing time
    assert len(c2.notifier.sent) == 1


@pytest.mark.parametrize(("weekday", "days", "back"), [
    (0, "Mon,Tue,Wed,Thu,Fri", 3),      # Monday covers the weekend
    (1, "Mon,Tue,Wed,Thu,Fri", 1),
    (4, "Mon,Tue,Wed,Thu,Fri", 1),
    (2, "Mon,Wed", 2),
    (3, "", 1),                         # every day → exactly one day
])
def test_the_morning_brief_covers_the_time_since_the_previous_send_day(weekday, days, back):
    when = datetime(2026, 10, 26, 8, 0) + timedelta(days=weekday)   # 26/10/2026 is a Monday
    assert when.weekday() == weekday
    assert svc_mod.since_previous_send_day(days, when) == back


async def test_monday_morning_brief_reports_the_weekend_under_its_own_heading():
    monday = datetime(2026, 10, 26, 1, 0, tzinfo=UTC)              # 08:00 in Hanoi
    changes = [
        _ch(1, "Resolved", "Resolved", -1.5),    # Saturday 14:00 UTC → in (weekend)
        _ch(2, "Resolved", "Resolved", 1),       # Thursday → before Friday 08:00, out
    ]
    c = _container(changes, [_wi(1, "TLCL.1 Cuối tuần", ["TLCL"]), _wi(2, "TLCL.2 Cũ", ["TLCL"])],
                   release_note_daily_enabled=True)
    sch = next(s for s in svc_mod.schedules(c.config) if s.daily)
    assert await ReleaseNoteService(c).run_once(monday, sch) == "sent"
    (msg,) = c.notifier.sent
    assert msg.heading.startswith("☀️ Bản tin sáng 26/10 — 1 lên production (gồm 3 ngày")
    assert "TLCL.1" in msg.text and "TLCL.2" not in msg.text
    tuesday = monday + timedelta(days=1)
    assert await ReleaseNoteService(c).run_once(tuesday, sch) == "empty"   # nothing new


async def test_weekly_and_daily_run_as_separate_schedules():
    c = _container([], release_note_enabled=True, release_note_daily_enabled=True)
    assert [s.name for s in svc_mod.schedules(c.config)] == ["weekly", "daily"]
    service = ReleaseNoteService(c)
    service.start()
    try:
        assert sorted(t.get_name() for t in service._tasks) == [
            "release-note-daily", "release-note-weekly"]
    finally:
        await service.stop()
    assert service._tasks == []


def test_schedule_starts_only_when_enabled_and_history_is_recorded():
    off = ReleaseNoteService(_container([], release_note_enabled=False))
    off.start()
    assert off._tasks == []
    no_history = ReleaseNoteService(_container([], release_note_enabled=True,
                                               delivery_history_enabled=False))
    no_history.start()
    assert no_history._tasks == []


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
    daily = doctor.check_release_note(Settings(
        release_note_daily_enabled=True, release_note_daily_at="25:00",
        timezone="Asia/Ho_Chi_Minh"))
    assert [f.title for f in daily] == ["Morning brief schedule has no valid time"]


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
    assert 'href="https://dev.azure.com/org/P/_workitems/edit/42"' in html
    customer = html.split("Bàn giao khách hàng", 1)[1]
    assert "#42" not in customer and "| 1 | TLCL.18 | Thêm cột |" in customer


def test_page_shows_the_waiting_queue_and_one_handover_per_tenant(tmp_path, monkeypatch):
    async def fake_build(self, **kw):
        note = rn.build([_wi(1, "TLCL.1 Một", ["TLCL"]), _wi(2, "VPM.2 Hai", ["VPM"])],
                        {1: NOW, 2: NOW}, tenant_tags=["TLCL", "VPM"], start=START, end=NOW,
                        days=7, history_since=NOW - timedelta(days=30))
        note.waiting_states = ["Ready for UAT"]
        note.waiting = rn.build_waiting([_ready(9, "TLCL.9 Chờ lâu", ["TLCL"])],
                                        {9: NOW - timedelta(days=15)}, tenant_tags=["TLCL"])
        return note

    monkeypatch.setattr(ReleaseNoteService, "build", fake_build)
    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'w.db'}")
    with TestClient(create_app(cfg)) as client:
        html = client.get("/dashboard/release-notes").text
    waiting = html.split("Chờ deploy production", 1)[1].split("Bàn giao khách hàng", 1)[0]
    assert "TLCL.9" in waiting and "15 ngày" in waiting and 'class="stale"' in waiting
    handover = html.split("Bàn giao khách hàng", 1)[1]
    tlcl, vpm = handover.split('id="ho-1"', 1)[1], handover.split('id="ho-2"', 1)[1]
    assert "TLCL.1" in tlcl.split("</textarea>")[0] and "VPM.2" not in tlcl.split("</textarea>")[0]
    assert "VPM.2" in vpm.split("</textarea>")[0] and "TLCL.1" not in vpm.split("</textarea>")[0]


def test_send_now_posts_and_reports_back(tmp_path, monkeypatch):
    async def fake_build(self, **kw):
        return _note()

    posted = []

    async def fake_post(self, note):
        posted.append(note.total)
        return True

    async def fake_analyze(self, note, **kw):
        return False

    monkeypatch.setattr(ReleaseNoteService, "build", fake_build)
    monkeypatch.setattr(ReleaseNoteService, "post", fake_post)
    monkeypatch.setattr(ReleaseNoteService, "analyze", fake_analyze)
    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    with TestClient(create_app(cfg)) as client:
        r = client.post("/dashboard/release-notes/send", data={"days": "7"},
                        follow_redirects=False)
        assert r.status_code == 303 and "sent=sent" in r.headers["location"]
        assert "Đã gửi release note" in client.get(r.headers["location"]).text
    assert posted == [1]


# ── roles, WIP, Claude's assessment ──────────────────────────────────────────


def _state(item, state, assignee=""):
    item.state, item.assigned_to = state, assignee
    return item


def test_roles_split_one_snapshot_into_a_block_per_role_with_zero_counts_kept():
    roles = rn.parse_roles({"🛠️ Dev": "Rework Required", "🧪 QC": "Ready for Testing, In Testing",
                            "Nobody": " "})
    assert roles == [("🛠️ Dev", ["Rework Required"]),
                     ("🧪 QC", ["Ready for Testing", "In Testing"])]
    queue = rn.build_waiting(
        [_state(_wi(1, "a"), "Rework Required", "An"), _state(_wi(2, "b"), "Ready for Testing")],
        {1: NOW - timedelta(days=9), 2: NOW - timedelta(days=2)}, tenant_tags=[])
    dev, qc = rn.build_lanes(roles, queue)
    assert [w.id for w in dev.items] == [1] and dev.items[0].assignee == "An"
    assert qc.by_state(NOW) == [("Ready for Testing", 1, 2), ("In Testing", 0, None)]


def _rich_note():
    note = rn.build([_wi(1, "TLCL.1 Một", ["TLCL"])], {1: NOW}, tenant_tags=["TLCL"],
                    start=START, end=NOW, days=7, history_since=NOW - timedelta(days=30))
    note.previous_total = 3
    note.waiting_states = ["Ready for UAT"]
    note.waiting = rn.build_waiting([_ready(9, "TLCL.9 Chờ", ["TLCL"])],
                                    {9: NOW - timedelta(days=15)}, tenant_tags=["TLCL"])
    note.lanes = rn.build_lanes([("🛠️ Dev", ["Rework Required"])], rn.build_waiting(
        [_state(_wi(5, "Sửa lỗi"), "Rework Required", "An")], {5: NOW - timedelta(days=6)},
        tenant_tags=[]))
    note.wip = [rn.WipColumn("P", "Rework Required", 11, 5), rn.WipColumn("P", "In UAT", 1, 5)]
    return note


def test_facts_for_claude_carry_every_number_it_may_cite():
    facts = rn.analysis_facts(_rich_note())
    for needle in ("Lên production trong kỳ: 1", "Kỳ trước cùng độ dài: 3", "#9 chờ 15 ngày",
                   "Vai trò 🛠️ Dev: Rework Required: 1 (lâu nhất 6 ngày)", "An",
                   "WIP P/Rework Required: 11/5 — VƯỢT", "WIP P/In UAT: 1/5"):
        assert needle in facts, needle


def test_claude_answer_is_parsed_strictly_and_invented_ids_are_dropped():
    ids = {1, 9}
    text = ('Đây là nhận định:\n{"headline": "Hàng chờ UAT kéo dài, #9 chờ 15 ngày", '
            '"health": "risk", "insights": ["#9 quá hạn", "#4242 bị bịa", "Rework vượt WIP"], '
            '"actions": [{"role": "BA/PO", "text": "Chốt lịch deploy #9"}, '
            '{"role": "Dev", "text": "Xử lý #777"}, "rác"]}')
    a = rn.parse_analysis(text, ids)
    assert a.health == "risk" and a.headline.startswith("Hàng chờ")
    assert a.insights == ("#9 quá hạn", "Rework vượt WIP")
    assert a.actions == (("BA/PO", "Chốt lịch deploy #9"),)
    assert rn.parse_analysis('{"headline": "x", "health": "great"}', ids).health == "watch"
    assert rn.parse_analysis("không phải JSON", ids) is None
    assert rn.parse_analysis('{"headline": "#5 bịa"}', ids) is None     # no grounded headline


def test_card_puts_assessment_wip_and_role_blocks_above_the_lists():
    note = _rich_note()
    note.analysis = rn.Analysis("Ổn", "good", ("i1",), (("QC", "test #9"),))
    body = rn.render_card(note, link_base="https://x/_workitems/edit")
    flat = [str(b) for b in body]
    assert "NHẬN ĐỊNH (Claude)" in flat[1] and "'style': 'good'" in flat[1]
    assert "VƯỢT GIỚI HẠN WIP" in flat[2] and "Rework Required **11**/5" in flat[2]
    assert "In UAT" not in flat[2]                                  # under limit → not flagged
    assert "🛠️ DEV — 1 hạng mục" in flat[3] and "_An_" in flat[3]
    assert "-2 so với kỳ trước" in flat[0]
    assert "ĐÃ LÊN PRODUCTION" in flat[4]
    _, text = rn.render_teams(note)
    assert "🧠 🟢 Ổn định — Ổn" in text and "**QC**: test #9" in text and "Vượt WIP" in text


async def test_service_builds_role_lanes_from_one_query_and_counts_the_previous_window():
    changes = [_ch(1, "In UAT", "Resolved", 2), _ch(2, "In UAT", "Resolved", 10),
               _ch(3, "In UAT", "Resolved", 12)]
    waiting = [_ready(9, "TLCL.9 w", ["TLCL"], changed_days_ago=3),
               _state(_wi(5, "rework"), "Rework Required", "An"),
               _state(_wi(6, "testing"), "In Testing")]
    c = _container(changes, [_wi(1, "a", ["TLCL"])], waiting=waiting,
                   release_note_states=["In UAT"], release_note_waiting_states=["Ready for UAT"],
                   release_note_roles={"Dev": "Rework Required",
                                       "QC": "Ready for Testing, In Testing"})
    note = await ReleaseNoteService(c).build(now=NOW)
    assert c.ado.asked_states == ["Ready for UAT", "Rework Required", "Ready for Testing",
                                  "In Testing"]
    assert [w.id for w in note.waiting] == [9]
    assert [[w.id for w in lane.items] for lane in note.lanes] == [[5], [6]]
    assert note.total == 1 and note.previous_total == 2
    assert note.has_news


async def test_analysis_is_added_by_claude_and_its_failure_never_blocks_the_post(monkeypatch):
    from ai_autopilot.execution import claude_client

    prompts = []

    async def fake_run(prompt, cwd, **kw):
        prompts.append((prompt, kw))
        return SimpleNamespace(text='{"headline": "Tuần ổn, #1 đã lên prod", "health": "good",'
                                    ' "insights": [], "actions": []}')

    monkeypatch.setattr(claude_client, "run_claude", fake_run)
    c = _container([_ch(1, "Resolved", "Resolved", 1)], [_wi(1, "TLCL.1 x", ["TLCL"])],
                   release_note_ai_enabled=True, release_note_days="")
    assert await ReleaseNoteService(c).run_once(NOW) == "sent"
    (msg,) = c.notifier.sent
    assert "Tuần ổn" in str(msg.card) and "Tuần ổn" in msg.text
    ((prompt, kw),) = prompts
    assert kw["allowed_tools"] == [] and kw["max_turns"] == 1 and "bản tin tuần" in prompt
    assert "#1" in prompt and "CẢ TEAM" in prompt

    async def boom(prompt, cwd, **kw):
        raise TimeoutError

    monkeypatch.setattr(claude_client, "run_claude", boom)
    c2 = _container([_ch(1, "Resolved", "Resolved", 1)], [_wi(1, "TLCL.1 x", ["TLCL"])],
                    release_note_ai_enabled=True, release_note_days="")
    assert await ReleaseNoteService(c2).run_once(NOW) == "sent"
    assert "NHẬN ĐỊNH" not in str(c2.notifier.sent[0].card)


async def test_board_wip_reads_the_limited_columns_of_the_lowest_board_in_the_team_area():
    import json

    import httpx

    from ai_autopilot.ado.client import AdoClient

    queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/work/boards"):
            return httpx.Response(200, json={"value": [{"name": "Epics"},
                                                       {"name": "Requirements"}]})
        if path.endswith("/Requirements/columns"):
            return httpx.Response(200, json={"value": [
                {"name": "New", "itemLimit": 0, "stateMappings": {"Requirement": "New"}},
                {"name": "Rework Required", "itemLimit": 5,
                 "stateMappings": {"Requirement": "Rework Required", "Bug": "Rework"}},
            ]})
        if path.endswith("/teamfieldvalues"):
            return httpx.Response(200, json={
                "field": {"referenceName": "System.AreaPath"},
                "values": [{"value": "P\\Team", "includeChildren": True}]})
        if path.endswith("/wiql"):
            queries.append(json.loads(request.content)["query"])
            return httpx.Response(200, json={"workItems": [{"id": i} for i in range(11)]})
        return httpx.Response(404, text="nope")

    client = AdoClient(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), auth=None,
                       config=Settings(ado_organization="https://dev.azure.com/org",
                                       ado_project="P"))

    async def _auth_header():
        return {}

    client._auth = type("A", (), {"get_auth_header": staticmethod(_auth_header)})()
    assert await client.get_board_wip("P") == [("Rework Required", 11, 5)]
    (q,) = queries
    assert "[System.WorkItemType] = 'Bug' AND [System.State] = 'Rework'" in q
    assert "[System.AreaPath] UNDER 'P\\Team'" in q and "[System.TeamProject] = 'P'" in q
