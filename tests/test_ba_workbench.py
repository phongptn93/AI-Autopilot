"""The BA workbench: readiness, the customer's word, open questions, production feedback."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from starlette.testclient import TestClient

from ai_autopilot.app import create_app
from ai_autopilot.config import Settings

SPEC_REL = "specs/active/export/export.md"

GOOD_SPEC = """# Xuất công nợ

ADO: #8530

## Mục tiêu
Kế toán xuất được công nợ theo khách hàng.

## Câu hỏi mở
- Kỳ báo cáo tối đa bao lâu?

## Acceptance Criteria
- AC-01: Khi chọn khách hàng rồi bấm Xuất, thì hệ thống tải về tệp xlsx
- AC-02: Khi kỳ không có dữ liệu thì hệ thống báo lỗi "Không có dữ liệu"

## Edge cases
- Kỳ rỗng
"""

WEAK_SPEC = """# Báo cáo nhanh

ADO: #8531

## Acceptance Criteria
- AC-01: Màn hình phải nhanh và thân thiện
"""


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / "specs" / "active" / "export").mkdir(parents=True)
    (ws / SPEC_REL).write_text(GOOD_SPEC, encoding="utf-8")
    (ws / "specs" / "active" / "weak").mkdir(parents=True)
    (ws / "specs" / "active" / "weak" / "weak.md").write_text(WEAK_SPEC, encoding="utf-8")
    return ws


def _fields(i, title, tags, days_ago, state="Active"):
    created = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    return {"System.Id": i, "System.Title": title, "System.State": state,
            "System.Tags": tags, "System.CreatedDate": created, "System.ChangedDate": created,
            "System.WorkItemType": "Bug", "System.AreaPath": "P"}


@pytest.fixture
def client(tmp_path, workspace):
    cfg = Settings(dry_run=False, database_url=f"sqlite+aiosqlite:///{tmp_path / 'ba.db'}",
                   workspace_directory=str(workspace), ado_organization="https://dev.azure.com/o",
                   ado_projects=["P"])
    with TestClient(create_app(cfg)) as c:
        calls: list = []

        async def rec(name, *a, **k):
            calls.append((name, *a))
            return True

        async def create(title, item_type, parent, tag, description="", project=""):
            calls.append(("create", title, item_type, tag, description, project))
            return 9001

        async def raw(project, days, top=1000):
            return [
                _fields(1, "Xuất báo cáo bị timeout", "Support", 5),
                _fields(2, "Xuat bao cao timeout lau", "Support", 3),
                _fields(3, "Xuất báo cáo timeout khi nhiều dòng", "Support", 1),
                _fields(4, "In phiếu bị lệch lề", "Support", 4),
                _fields(5, "Xuất báo cáo timeout QAONLY", "Dev", 2),
            ]

        c.app.state.container.ado = SimpleNamespace(
            create_work_item=create, get_raw_work_items=raw,
            add_tag=lambda i, t: rec("add_tag", i, t),
            remove_tag=lambda i, t: rec("remove_tag", i, t),
            add_comment=lambda i, t: rec("comment", i, t),
            update_state=lambda i, s: rec("state", i, s),
            get_work_item=lambda i: rec("get", i), refresh=lambda: None,
        )
        c.calls = calls
        yield c


def _post(client, url, **data):
    return client.post(url, data=data, follow_redirects=False)


def _comments(client):
    return [c for c in client.calls if c[0] == "comment"]


# ── the specs tab ──────────────────────────────────────────────────────────────


def test_specs_tab_ranks_the_weakest_spec_first(client):
    html = client.get("/dashboard/ba").text
    assert "Báo cáo nhanh" in html and "Xuất công nợ" in html
    assert html.index("Báo cáo nhanh") < html.index("Xuất công nợ")


def test_specs_tab_shows_each_specs_unchecked_customer_state(client):
    assert "Chưa xác nhận" in client.get("/dashboard/ba").text


def test_unknown_tab_falls_back_to_specs(client):
    assert client.get("/dashboard/ba?tab=nonsense").status_code == 200


# ── readiness on the spec page ─────────────────────────────────────────────────


def test_spec_page_shows_the_readiness_of_that_spec(client):
    html = client.get(f"/dashboard/requirements/spec?path={quote(SPEC_REL)}").text
    assert "Độ sẵn sàng giao agent" in html
    assert "Khách xác nhận" in html and "Nghiệm thu" in html


def test_posting_readiness_comments_on_the_work_item(client):
    r = _post(client, "/dashboard/ba/readiness/comment", path=SPEC_REL, item_id="8530")
    assert r.status_code == 303
    [(_, item, body)] = _comments(client)
    assert item == 8530 and "SPEC-READINESS" in body


def test_posting_readiness_needs_a_work_item(client):
    _post(client, "/dashboard/ba/readiness/comment", path=SPEC_REL, item_id="")
    assert not _comments(client)


def test_a_weak_spec_cannot_be_approved_when_the_gate_is_on(client):
    client.app.state.container.config.spec_readiness_blocks_approve = True
    weak = "specs/active/weak/weak.md"
    r = _post(client, "/dashboard/requirements/review", path=weak, item_id="8531",
              decision="approve")
    assert r.status_code == 303
    assert not _comments(client) and not [c for c in client.calls if c[0] == "add_tag"]


def test_a_weak_spec_can_still_be_approved_when_the_gate_is_off(client):
    weak = "specs/active/weak/weak.md"
    _post(client, "/dashboard/requirements/review", path=weak, item_id="8531",
          decision="approve")
    assert any("SPEC APPROVED" in c[2] for c in _comments(client))


def test_a_ready_spec_passes_the_gate(client):
    client.app.state.container.config.spec_readiness_blocks_approve = True
    client.app.state.container.config.spec_readiness_min = 60
    _post(client, "/dashboard/requirements/review", path=SPEC_REL, item_id="8530",
          decision="approve")
    assert any("SPEC APPROVED" in c[2] for c in _comments(client))


# ── the customer's confirmation ────────────────────────────────────────────────


def _confirm(client, **over):
    data = {"path": SPEC_REL, "item_id": "8530", "confirmed_by": "Chị Lan (KT trưởng)",
            "channel": "họp", "outcome": "confirmed", "note": "OK"}
    data.update(over)
    return _post(client, "/dashboard/ba/confirm", **data)


def test_a_confirmation_is_recorded_and_commented(client):
    assert _confirm(client).status_code == 303
    [(_, item, body)] = _comments(client)
    assert item == 8530 and "CUSTOMER-CONFIRMED" in body and "Chị Lan" in body
    html = client.get(f"/dashboard/requirements/spec?path={quote(SPEC_REL)}").text
    assert "Đã xác nhận" in html


def test_a_confirmation_without_a_name_is_refused(client):
    _confirm(client, confirmed_by="  ")
    assert not _comments(client)
    assert "Chưa xác nhận" in client.get("/dashboard/ba").text


def test_a_refusal_is_kept_as_a_refusal(client):
    _confirm(client, outcome="rejected")
    assert "CUSTOMER-REJECTED" in _comments(client)[0][2]
    assert "Khách từ chối" in client.get(
        f"/dashboard/requirements/spec?path={quote(SPEC_REL)}").text


def test_editing_a_criterion_after_confirmation_makes_it_stale(client, workspace):
    """The badge must not keep saying "confirmed" about text the customer never saw."""
    _confirm(client)
    path = workspace / SPEC_REL
    path.write_text(GOOD_SPEC.replace("tải về tệp xlsx", "tải về tệp pdf"), encoding="utf-8")
    html = client.get(f"/dashboard/requirements/spec?path={quote(SPEC_REL)}").text
    assert "Đã đổi sau xác nhận" in html
    assert "xlsx" in html and "pdf" in html           # before → after is shown


def test_editing_prose_outside_the_criteria_does_not_make_it_stale(client, workspace):
    _confirm(client)
    path = workspace / SPEC_REL
    path.write_text(GOOD_SPEC.replace("Kế toán xuất được", "Kế toán viên xuất được"),
                    encoding="utf-8")
    html = client.get(f"/dashboard/requirements/spec?path={quote(SPEC_REL)}").text
    assert "Đã xác nhận" in html and "Đã đổi sau xác nhận" not in html


def test_qc_is_told_which_criteria_changed(client, workspace):
    _confirm(client)
    client.calls.clear()
    (workspace / SPEC_REL).write_text(GOOD_SPEC.replace("xlsx", "pdf"), encoding="utf-8")
    _post(client, "/dashboard/ba/impact/notify", path=SPEC_REL, item_id="8530")
    [(_, item, body)] = _comments(client)
    assert item == 8530 and "SPEC-CHANGED" in body and "AC-01" in body


def test_nothing_is_posted_when_no_criterion_changed(client):
    _confirm(client)
    client.calls.clear()
    _post(client, "/dashboard/ba/impact/notify", path=SPEC_REL, item_id="8530")
    assert not _comments(client)


def test_dry_run_records_the_confirmation_but_posts_nothing(client):
    client.app.state.container.config.dry_run = True
    _confirm(client)
    assert not _comments(client)
    assert "Đã xác nhận" in client.get(
        f"/dashboard/requirements/spec?path={quote(SPEC_REL)}").text


def test_a_path_outside_the_spec_folders_is_refused(client):
    _confirm(client, path="../../etc/passwd")
    assert not _comments(client)


# ── the customer pack ──────────────────────────────────────────────────────────


def test_the_customer_pack_is_a_locked_down_page(client):
    r = client.get(f"/dashboard/ba/pack?path={quote(SPEC_REL)}")
    assert r.status_code == 200
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert "<script" not in r.text and "Kịch bản kiểm tra" in r.text
    assert "AC-01" in r.text
    assert "specs/active" not in r.text             # no internal file path in a customer document


def test_the_customer_pack_of_a_missing_spec_is_a_404(client):
    assert client.get("/dashboard/ba/pack?path=specs/none.md").status_code == 404


# ── acceptance ─────────────────────────────────────────────────────────────────


def _accept(client, results=("pass", "pass"), **over):
    data = {"path": SPEC_REL, "item_id": "8530", "confirmed_by": "Chị Lan", "channel": "họp",
            "outcome": "confirmed", "note": "",
            "res_0": results[0], "ev_0": "TC-1", "res_1": results[1], "ev_1": "TC-2"}
    data.update(over)
    return _post(client, "/dashboard/ba/accept", **data)


def test_acceptance_is_recorded_when_every_criterion_passes(client):
    assert _accept(client).status_code == 303
    [(_, _item, body)] = _comments(client)
    assert "ACCEPTED" in body and "2/2" in body
    rec = client.get(f"/dashboard/ba/acceptance?path={quote(SPEC_REL)}")
    assert rec.status_code == 200 and "ĐỦ ĐIỀU KIỆN NGHIỆM THU" in rec.text
    assert "TC-1" in rec.text


def test_acceptance_cannot_be_claimed_while_a_criterion_fails(client):
    _accept(client, results=("pass", "fail"))
    assert not _comments(client)
    assert client.get(f"/dashboard/ba/acceptance?path={quote(SPEC_REL)}").status_code == 404


def test_a_refusal_of_a_failing_delivery_is_recorded(client):
    _accept(client, results=("pass", "fail"), outcome="rejected")
    assert "ACCEPTANCE-REJECTED" in _comments(client)[0][2]


def test_the_criteria_come_from_the_file_not_from_the_form(client):
    """A posted AC text must never be able to claim the customer accepted something the
    spec does not say."""
    _accept(client, ac_text_0="Tặng khách 1 tỷ")
    md = client.get(f"/dashboard/ba/acceptance?path={quote(SPEC_REL)}&fmt=md").text
    assert "1 tỷ" not in md and "AC-01" in md


def test_an_unknown_result_value_reads_as_not_checked(client):
    r = _accept(client, results=("pass", "excellent"))
    assert r.status_code == 303
    assert not _comments(client)                    # AC-02 is unchecked, so it cannot be accepted


def test_an_undecided_spec_drift_blocks_acceptance(client):
    container = client.app.state.container
    from ai_autopilot.execution.result_contract import Deviation
    from ai_autopilot.models import WorkItemInfo
    client.portal.call(
        container.spec_drift_repo.add, WorkItemInfo(id=8530, title="t"), "",
        [Deviation(kind="assumption", summary="đã tự chọn xlsx")])
    _accept(client)
    assert not _comments(client)


# ── open questions ─────────────────────────────────────────────────────────────


def test_questions_tab_lists_the_open_question(client):
    html = client.get("/dashboard/ba?tab=questions").text
    assert "Kỳ báo cáo tối đa bao lâu?" in html


def test_answering_records_the_answer_and_closes_the_question(client):
    from ai_autopilot import assumptions, spec_library
    ws = client.app.state.container.config.workspace_directory
    text = spec_library.read(spec_library.resolve(ws, SPEC_REL))
    [q] = assumptions.extract(text)
    r = _post(client, "/dashboard/ba/questions/answer", path=SPEC_REL, key=q.key,
              answer="12 tháng", item_id="8530")
    assert r.status_code == 303
    assert "SPEC-ANSWER" in _comments(client)[0][2]
    html = client.get("/dashboard/ba?tab=questions").text
    assert "Đã chốt" in html and "12 tháng" in html


def test_an_empty_answer_is_not_an_answer(client):
    from ai_autopilot import assumptions, spec_library
    ws = client.app.state.container.config.workspace_directory
    [q] = assumptions.extract(spec_library.read(spec_library.resolve(ws, SPEC_REL)))
    _post(client, "/dashboard/ba/questions/answer", path=SPEC_REL, key=q.key, answer="  ")
    assert not _comments(client)
    assert "Kỳ báo cáo tối đa bao lâu?" in client.get("/dashboard/ba?tab=questions").text


def test_an_answer_for_a_vanished_question_is_ignored(client):
    _post(client, "/dashboard/ba/questions/answer", path=SPEC_REL, key="deadbeef", answer="x")
    assert not _comments(client)


# ── production feedback ────────────────────────────────────────────────────────


def test_repeated_tickets_form_one_candidate_and_other_tags_are_ignored(client):
    html = client.get("/dashboard/ba?tab=production").text
    assert "3 ticket" in html                       # the three "export timeout" tickets
    assert "lệch lề" not in html                    # a lone ticket is not a group
    assert "QAONLY" not in html                      # tagged Dev, not Support


def test_production_tab_survives_an_unreachable_tracker(client):
    async def boom(project, days, top=1000):
        raise RuntimeError("401")
    client.app.state.container.ado.get_raw_work_items = boom
    r = client.get("/dashboard/ba?tab=production")
    assert r.status_code == 200 and "Không đọc được" in r.text


def test_a_cluster_becomes_a_draft_requirement_not_a_started_one(client):
    html = client.get("/dashboard/ba?tab=production").text
    assert 'name="key" value="1"' in html
    r = _post(client, "/dashboard/ba/production/create", key="1", project="P")
    assert r.status_code == 303
    [created] = [c for c in client.calls if c[0] == "create"]
    assert created[2] == "User Story" and created[3] == "requirement-draft"
    assert "Phản hồi production" in created[1]
    assert not [c for c in client.calls if c[0] == "add_tag"]      # nothing was started


def test_creating_from_a_vanished_cluster_does_nothing(client):
    _post(client, "/dashboard/ba/production/create", key="999", project="P")
    assert not [c for c in client.calls if c[0] == "create"]


def test_no_tags_configured_says_so(client):
    client.app.state.container.config.production_feedback_tags = []
    assert "Chưa khai báo tag" in client.get("/dashboard/ba?tab=production").text


# ── metrics ────────────────────────────────────────────────────────────────────


def test_metrics_tab_shows_tiles_and_never_crashes_on_an_empty_install(client):
    r = client.get("/dashboard/ba?tab=metrics")
    assert r.status_code == 200
    assert "Spec cần chỉnh nhất" in r.text


# ── permissions ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path,perm", [
    ("/dashboard/ba/confirm", "requirements.approve"),
    ("/dashboard/ba/accept", "requirements.approve"),
    ("/dashboard/ba/questions/answer", "requirements.approve"),
    ("/dashboard/ba/readiness/comment", "tracker.write"),
    ("/dashboard/ba/impact/notify", "tracker.write"),
    ("/dashboard/ba/production/create", "requirements.write"),
])
def test_each_write_needs_the_right_permission(path, perm):
    from ai_autopilot import rbac
    assert rbac.required("POST", path) == perm


def test_the_ba_role_can_use_the_workbench_and_a_plain_viewer_cannot():
    from ai_autopilot import rbac
    ba = set(rbac.BUILTIN_ROLES["ba"][1])
    assert rbac.allowed(ba, rbac.required("POST", "/dashboard/ba/confirm"))
    assert rbac.allowed(ba, rbac.required("POST", "/dashboard/ba/production/create"))
    assert not rbac.allowed({"view"}, rbac.required("POST", "/dashboard/ba/confirm"))
