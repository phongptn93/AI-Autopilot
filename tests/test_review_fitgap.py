"""🤝 Fit-gap: a customer's needs checked against what the product already does.

The agent is faked. What is pinned: the contract (statuses, module, effort, demo plan),
how the fit rate is scored (out-of-scope does not count against the offered modules),
per-module coverage, the customer copy carrying no code locations, and the page flow
from pasted email to a rerunnable review.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from starlette.testclient import TestClient

from ai_autopilot import review_studio as rs
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.services import review_studio as svc_mod

_EMAIL = """Tên khách hàng: CTCP LAVO — mỹ phẩm tóc, ODM. Đang dùng MISA + thủ công.
Module: DxMPM và DxWMS.
- Tự động lập kế hoạch sản xuất từ PO/đơn đặt hàng.
- Quy trình nhiều công đoạn theo từng SKU, không giới hạn số bước."""

_REPLY = """Đánh giá nhanh.

```json
{"verdict": "Đáp ứng phần lớn, demo được",
 "summary": "5 nhu cầu.",
 "traces": [
  {"ref": "N-01", "kind": "NEED", "spec": "Quy trình nhiều công đoạn theo SKU",
   "module": "DxMPM › Quy trình", "status": "config", "code": "Định tuyến công đoạn",
   "evidence": [{"file": "src/Routing/RoutingController.cs", "line": 12}],
   "fix": "Khai quy trình mẫu", "demo": "Màn Quy trình sản xuất"},
  {"ref": "N-02", "kind": "NEED", "spec": "Kế hoạch từ PO", "module": "DxMPM › Kế hoạch",
   "status": "fit", "code": "Có lập kế hoạch từ đơn hàng"},
  {"ref": "N-03", "kind": "NEED", "spec": "Phân bổ khu vực tự động", "module": "DxMPM",
   "status": "partial", "effort": "m", "fix": "Thêm khu vực vào thuật toán"},
  {"ref": "N-04", "kind": "NEED", "spec": "Yêu cầu xuất NVL từ sản xuất", "module": "DxWMS",
   "status": "gap", "effort": "L", "fix": "Phát triển luồng yêu cầu xuất",
   "searched": "MaterialRequest, xuất kho"},
  {"ref": "N-05", "kind": "NEED", "spec": "Tích hợp MISA", "status": "out", "effort": "huge"}
 ],
 "demo_plan": [{"step": "Đơn hàng → kế hoạch", "detail": "Tạo PO", "refs": ["N-02"]},
               "Sản xuất nhiều công đoạn"],
 "prep": ["10 SKU dầu gội mẫu (src/Seed/Sku.cs:3)"],
 "questions": [{"text": "Khu vực là gì với LAVO?", "options": ["Kho", "Xưởng"]}],
 "extras": ["Truy xuất lô theo mẻ"]}
```"""


def test_contract_scoring_and_rollups():
    result, prose = rs.parse(_REPLY, rs.KIND_FITGAP)
    assert result.parsed and prose.startswith("Đánh giá nhanh")
    t = {x.ref: x for x in result.traces}
    assert t["N-01"].status == "config" and t["N-01"].module.startswith("DxMPM")
    assert t["N-03"].effort == "M" and t["N-05"].effort == ""   # unknown size dropped
    assert t["N-02"].kind == "NEED"
    # fit 1 + config 1 + partial ½ over 4 judged (the out-of-scope need is not a miss)
    assert result.score(rs.KIND_FITGAP) == 62.5
    assert rs.effort_total(result) == {"S": 0, "M": 1, "L": 1, "XL": 0}
    assert rs.gaps(result, rs.KIND_FITGAP) == [2, 3]
    mods = {g["kind"]: g for g in rs.by_module(result)}
    assert mods["DxMPM"]["total"] == 3 and mods["DxWMS"]["score"] == 0
    assert "Khác" not in mods                                   # only the out-of-scope row
    assert result.demo_plan[1] == {"step": "Sản xuất nhiều công đoạn", "detail": "", "refs": []}
    again = rs.load(result.to_json())
    assert again.demo_plan == result.demo_plan and again.traces[0].demo == "Màn Quy trình sản xuất"


def test_pack_round_trip_and_headerless_text():
    packed = rs.fitgap_pack(_EMAIL, customer="LAVO", modules="DxMPM, DxWMS",
                            deadline="23/11/2026")
    out = rs.fitgap_unpack(packed)
    assert (out["customer"], out["modules"], out["deadline"]) == ("LAVO", "DxMPM, DxWMS",
                                                                  "23/11/2026")
    assert out["text"] == _EMAIL.strip()
    assert rs.fitgap_unpack("chỉ có chữ")["text"] == "chỉ có chữ"


def test_customer_copy_has_no_code_locations():
    result, _ = rs.parse(_REPLY, rs.KIND_FITGAP)
    md = rs.render_markdown("LAVO", rs.KIND_FITGAP, result, external=True)
    assert md.startswith("# 🤝 Fit-gap: LAVO")
    assert "Bảng fit-gap" in md and "Kịch bản demo" in md and "Câu hỏi khảo sát" in md
    assert "Mức đáp ứng: 62.5%" in md and "M (3–10 manday): 1" in md
    assert "RoutingController" not in md and "src/Seed" not in md
    internal = rs.render_markdown("LAVO", rs.KIND_FITGAP, result)
    assert "src/Routing/RoutingController.cs:12" in internal


def _app(tmp_path, monkeypatch):
    (tmp_path / "ws").mkdir()
    cfg = Settings(dry_run=True, workspace_directory=str(tmp_path / "ws"),
                   database_url=f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
    calls: list[str] = []

    async def fake_agent(self, prompt, *, resume=None, turns=None, cwd=None):
        calls.append(prompt)
        return SimpleNamespace(text=_REPLY, session_id="s", cost_usd=0.1, is_error=False)

    monkeypatch.setattr(svc_mod.ReviewStudio, "_agent", fake_agent)
    return create_app(cfg), calls


def _wait(client, rid):
    for _ in range(100):
        if client.get(f"/dashboard/review/{rid}/status").json()["status"] in ("done", "failed"):
            return
        asyncio.run(asyncio.sleep(0.02))
    raise AssertionError("review did not finish")


def test_pasted_email_to_review_and_rerun(tmp_path, monkeypatch):
    app, calls = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert "Fit-gap khách hàng" in client.get("/dashboard/review").text
        r = client.post("/dashboard/review/fitgap", data={
            "customer": "CTCP LAVO", "modules": "DxMPM, DxWMS", "deadline": "23/11/2026",
            "text": _EMAIL}, follow_redirects=False)
        rid = int(r.headers["location"].rsplit("/", 1)[1])
        _wait(client, rid)
        assert "CTCP LAVO" in calls[0] and "23/11/2026" in calls[0] and "PO/đơn đặt hàng" in calls[0]
        page = client.get(f"/dashboard/review/{rid}").text
        for want in ("Bảng fit-gap", "Kịch bản demo", "⚙️ Cấu hình / thiết lập",
                     "Câu hỏi khảo sát khách", "Nên giới thiệu thêm", "mức đáp ứng"):
            assert want in page, want
        report = client.get(f"/dashboard/review/{rid}/report.html?audience=external").text
        assert "Khách hàng: <b>CTCP LAVO</b>" in report and "RoutingController" not in report

        r = client.post(f"/dashboard/review/{rid}/rerun", follow_redirects=False)
        new = int(r.headers["location"].rsplit("/", 1)[1])
        _wait(client, new)
        assert new != rid and "CTCP LAVO" in calls[-1] and "DxMPM, DxWMS" in calls[-1]


def test_empty_request_is_refused(tmp_path, monkeypatch):
    app, calls = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        r = client.post("/dashboard/review/fitgap", data={"customer": "X", "text": "  "},
                        follow_redirects=False)
        assert "pane=p-fit" in r.headers["location"] and not calls


def test_uploaded_file_is_read_with_the_pasted_text(tmp_path, monkeypatch):
    app, calls = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        r = client.post("/dashboard/review/fitgap",
                        data={"customer": "LAVO", "text": "Ghi chú thêm"},
                        files={"file": ("yeu-cau.md", "# Nhu cầu\n- Truy xuất lô".encode())},
                        follow_redirects=False)
        _wait(client, int(r.headers["location"].rsplit("/", 1)[1]))
        assert "Ghi chú thêm" in calls[0] and "Truy xuất lô" in calls[0]
