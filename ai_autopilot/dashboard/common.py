"""Dashboard helpers shared by the route modules: templates, flash messages,
formatting, caches and view-model builders. Routes live in ``dashboard.routes``."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import (
    RedirectResponse,
)
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from ai_autopilot import (
    delivery,
)
from ai_autopilot import (
    workspaces as workspaces_mod,
)
from ai_autopilot.config import (
    matches_any_user,
)
from ai_autopilot.container import Container
from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.services.pr_feedback import parse_work_item_id
from ai_autopilot.workspace import discover_repos

_log = get_logger("dashboard")

def _group_drifts(rows: list) -> list:
    """Deviations grouped by work item — the unit a BA actually edits.

    A flat list would have them open the same item once per deviation, and the "mark
    updated" action is per item too. Order of first appearance is kept, so the newest
    report stays at the top.
    """
    groups: OrderedDict[int, dict] = OrderedDict()
    for row in rows:
        group = groups.get(row.work_item_id)
        if group is None:
            group = groups[row.work_item_id] = {
                "work_item_id": row.work_item_id, "title": row.title,
                "project": row.project, "pr_url": row.pr_url,
                "created_at": row.created_at, "resolved_at": row.resolved_at,
                "resolved_by": row.resolved_by, "rows": [],
            }
        group["rows"].append(row)
        group["pr_url"] = group["pr_url"] or row.pr_url
    return [_DriftGroup(**g) for g in groups.values()]


class _DriftGroup(dict):
    """Attribute access for the template (``group.rows`` reads better than ``group['rows']``)."""

    __getattr__ = dict.get


_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# The sidebar is rendered on every page, including the few that build their context
# without ``_ctx`` — so it is a global, not a context key someone can forget.
from ai_autopilot.dashboard import nav as _nav  # noqa: E402

_TEMPLATES.env.globals["nav_groups"] = _nav.groups
_TEMPLATES.env.globals["page_title"] = _nav.page_title
_TEMPLATES.env.globals["page_hint"] = _nav.page_hint
_TEMPLATES.env.globals["page_group"] = _nav.group_of


def _human_error(text: object) -> str:
    """A tracker error in words a user can act on — never env-var names."""
    raw = str(text or "")
    if "No PAT and no OAuth" in raw:
        return "chưa có thông tin đăng nhập Azure DevOps — hoàn tất bước Kết nối ở Cài đặt nhanh."
    if "401" in raw or "Unauthorized" in raw:
        return "Azure DevOps từ chối thông tin đăng nhập (401) — PAT sai hoặc đã hết hạn."
    if "403" in raw:
        return "PAT không đủ quyền cho thao tác này (403)."
    return raw


_TEMPLATES.env.filters["human_error"] = _human_error


def _inline_md(text: object) -> Markup:
    """`code` and **bold** in a one-line explanation — escaped first, so a tag or a
    display name read from ADO can never inject markup."""
    out = str(escape(str(text or "")))
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", out)
    return Markup(out)


_TEMPLATES.env.filters["inline_md"] = _inline_md

# ── Flash messages ────────────────────────────────────────────────────────────
# The outcome of a POST used to travel in the query string of the redirect that follows
# it (`/dashboard/settings?saved=1`, `?import_error=<text>`). The POST itself was always
# correct; the problem is what the GET then carries:
#
#   • refreshing the page replays the banner, claiming a save that did not happen
#   • the URL is bookmarkable and shareable WITH the banner, so it lies later
#   • free-form error text has to be URL-encoded into it, which caps its length and
#     leaves an unreadable address bar
#
# A one-shot cookie fixes all three: it is set on the POST response, consumed and cleared
# on the next GET, and never appears in the URL. Only a short CODE travels — the wording
# lives here, so nothing user-supplied is ever echoed back into the page.
_FLASH_COOKIE = "autopilot_flash"

FLASH_MESSAGES: dict[str, tuple[str, str]] = {
    "saved": ("green", "✅ Đã lưu và áp dụng. Field có nhãn <em>restart</em> cần khởi động lại."),
    "reloaded": ("green", "↻ Đã nạp lại cấu hình từ file vào tiến trình đang chạy."),
    "imported": ("green", "⬆ Đã nạp và áp dụng cấu hình. PAT và trigger tag của máy này "
                          "không đổi."),
    "imported_full": ("green", "🔓 Đã restore cấu hình đầy đủ (kèm secret). Khởi động lại để "
                               "áp dụng phần lồng nhau (tenants, repos)."),
    "flow_saved": ("green", "✅ Đã lưu flow theo work item type và áp dụng ngay "
                            "(không cần khởi động lại)."),
    "flow_invalid": ("red", "⛔ Chưa lưu — xem các lỗi bên dưới. Giá trị bạn vừa nhập "
                            "vẫn được giữ."),
    "compacted": ("green", "🧹 Đã dọn: gộp các dòng trùng nghĩa và bỏ những bài học "
                           "không hành động được (kiểu «re-read the review comments on that PR»)."),
    "compact_clean": ("green", "Không có gì để dọn — danh sách đã gọn."),
    "file_created": ("green", "✅ Đã tạo work item từ các finding bạn chọn — mở tab Board "
                              "hoặc Overview để thấy chúng."),
    "file_partial": ("amber", "Tạo được một phần — có finding không tạo được work item. "
                              "Xem log để biết lý do (thường là sai project hoặc loại item)."),
    "file_failed": ("red", "⛔ Không tạo được work item nào. Kiểm tra project và loại work "
                           "item có đúng với process template của ADO không."),
    "file_none_picked": ("amber", "Chưa tick finding nào."),
    "pool_approved": ("green", "✅ Đã duyệt — máy trạm sẽ nhận dòng này ở nhịp đồng bộ kế tiếp."),
    "pool_rejected": ("amber", "Đã từ chối. Giữ lại bản ghi để máy nào còn dòng đó gửi lên "
                               "cũng không làm nó quay lại hàng chờ."),
    "lesson_added": ("green", "✅ Đã nạp tri thức. Nó vào brief của run kế tiếp ngay — "
                              "tri thức bạn nhập luôn được ưu tiên trước bài học máy tự rút."),
    "lesson_edited": ("green", "✅ Đã sửa. Ngày ghi và số lần tái phát được giữ nguyên."),
    "lesson_none_added": ("amber", "Không có dòng nào được ghi — nội dung trống, hoặc đã có "
                                   "sẵn một dòng cùng nghĩa (nó chỉ tăng số lần đếm)."),
    "lesson_no_workspace": ("red", "⛔ Chưa cấu hình <code>workspace_directory</code> — "
                                   "tri thức không có chỗ để lưu."),
    "lens_saved": ("green", "✅ Đã lưu quy trình bảng (BA / Dev / QC…) và áp dụng ngay."),
    "lens_reset": ("green", "↩ Đã khôi phục bộ quy trình mặc định (BA / Dev / QC)."),
    "lens_invalid": ("red", "⛔ Chưa lưu — xem các lỗi bên dưới. Giá trị bạn vừa nhập "
                            "vẫn được giữ."),
    "roles_saved": ("green", "✅ Đã lưu vai trò (stage · cửa vào · cửa ra) và áp dụng ngay."),
    "setup_ws_missing": ("amber", "⚠️ Đã lưu, nhưng thư mục workspace <b>không tồn tại</b> trên "
                                  "máy này — mọi run sẽ lỗi. Quay lại bước Mã nguồn để sửa."),
    "setup_ws_no_claude": ("amber", "ℹ️ Đã lưu. Thư mục workspace chưa có <code>.claude/</code> "
                                    "(skills, rules, MCP) — autopilot vẫn chạy, nhưng không có "
                                    "skill riêng của dự án."),
    "req_title_required": ("red", "⛔ Cần tiêu đề cho yêu cầu."),
    "req_dry_run": ("amber", "Máy đang <code>dry_run</code> — không ghi gì lên tracker. Tắt ở "
                             "Thiết lập để dùng chức năng này."),
    "req_create_failed": ("red", "⛔ Tracker từ chối tạo work item — kiểm tra loại item có tồn tại "
                                 "trong dự án và quyền của PAT (xem log)."),
    "req_created": ("green", "✅ Đã tạo yêu cầu (bản nháp, tag <code>requirement-draft</code>)."),
    "req_created_analysing": ("green", "🤖 Đã tạo yêu cầu và giao cho BA agent — spec sẽ hiện ở "
                                       "Thư viện spec khi phân tích xong."),
    "req_no_item": ("red", "⛔ Cần mã work item để góp ý hoặc duyệt."),
    "req_feedback_empty": ("amber", "Viết nội dung góp ý trước khi gửi."),
    "req_feedback_sent": ("green", "💬 Đã gửi góp ý lên work item."),
    "req_approved": ("green", "✅ Đã duyệt spec (comment SPEC APPROVED trên work item)."),
    "req_approved_dev": ("green", "✅ Đã duyệt spec và giao cho vai Dev."),
    "lesson_promoted": ("green", "📌 Đã nâng thành quy tắc — từ giờ nó nằm trong file rule mà "
                                 "mọi run đều nạp, không còn là suy đoán trong skill."),
    "lessons_pruned": ("green", "🧹 Đã dọn các bài học cũ chỉ gặp một lần."),
    "preset_applied": ("green", "✅ Đã áp dụng preset. Các thiết lập cũ được ghi trong Audit "
                                "(<code>config.preset_applied</code>) nếu cần đặt lại."),
    "preset_unknown": ("red", "⛔ Không có preset đó."),
    "preset_chain_invalid": ("red", "⛔ Chuỗi vai chưa hợp lệ — hai vai chung một state vào, "
                                    "hoặc một vai xong rơi vào Trigger state. Sửa tên state "
                                    "rồi áp dụng lại; chưa có gì được ghi."),
    "roles_cleared": ("green", "↩ Đã gỡ toàn bộ vai trò — máy quay về dùng Trigger states."),
    "reset_done": ("green", "↩ Đã đưa các thiết lập về mặc định và áp dụng ngay. "
                            "Xem đúng những gì đã đổi ở <a href='/dashboard/audit'>Audit</a>."),
    "reset_nothing": ("green", "✅ Không có gì để đặt lại — phần này đang ở đúng giá trị "
                               "mặc định."),
    "setting_claimed": ("green", "🖥 Máy này đã giành quyền thiết lập đó — trung tâm sẽ "
                                 "không ghi đè nữa, và bạn sửa được ngay tại chỗ."),
    "setting_released": ("green", "🛰 Đã trả thiết lập đó về cho trung tâm — lần đồng bộ "
                                  "tới máy này sẽ nhận lại giá trị chung."),
    "offer_accepted": ("green", "✅ Đã nhận vào tri thức của máy này."),
    "offer_declined": ("amber", "🚫 Đã từ chối — dòng này sẽ KHÔNG quay lại ở nhịp "
                                "đồng bộ sau."),
    # ⬆️ Self-update. "Started" is not "done": the process is about to go away and come
    # back, so the banner has to describe a thing in progress, not a result.
    "update_started": ("green", "⬆️ Đang cập nhật. Máy sẽ ngừng nhận việc mới, đợi các "
                                "task hiện tại xong rồi cài và khởi động lại — trang này "
                                "sẽ mất kết nối một lát, tải lại sau ít phút."),
    "update_none": ("amber", "Không có bản mới nào để cài."),
    "update_found": ("green", "⬆️ Có bản mới — xem khung <b>Cập nhật</b> bên dưới để cài."),
    "update_uptodate": ("green", "✅ Đang chạy bản mới nhất."),
    "update_check_failed": ("red", "⛔ Không hỏi được GitHub Releases (mạng, proxy, hoặc giới "
                                   "hạn 60 lượt/giờ). Thử lại sau ít phút."),
    "update_unavailable": ("red", "⛔ Dịch vụ cập nhật không chạy trong tiến trình này."),
    "update_busy": ("amber", "Một lượt cập nhật đang chạy rồi."),
    "update_blocked_editable": ("amber", "Bản cài này là <code>pip install -e .</code> từ "
                                         "một checkout git — cập nhật bằng "
                                         "<code>git pull</code>, không phải bằng wheel."),
    "update_blocked_container": ("amber", "Đang chạy trong container: image mới là việc "
                                          "của orchestrator. Mọi thứ pip ghi vào đây sẽ "
                                          "mất ở lần khởi động lại kế tiếp."),
    # Sync outcomes are split three ways on purpose: "nothing changed" is a SUCCESS and
    # the most common one, and reporting it with the same green tick as "six settings
    # were rewritten" teaches people to stop reading the banner.
    "fleet_synced_same": ("green", "✅ Đã gọi về trung tâm — máy này vốn đã khớp, "
                                   "không có gì phải đổi."),
    "fleet_synced_changed": ("green", "🔄 Đã đồng bộ và áp dụng ngay. Xem danh sách thiết "
                                      "lập vừa đổi ở khung bên dưới."),
    "fleet_sync_failed": ("red", "⛔ Không đồng bộ được — lý do cụ thể ở khung "
                                 "<b>Lần gọi gần nhất</b> bên dưới."),
    "fleet_no_agent": ("red", "⛔ Tiến trình này không chạy fleet agent. Vai đang là "
                              "<code>worker</code> nhưng service chưa khởi động — "
                              "khởi động lại autopilot."),
    # Queued is NOT done: the worker picks a command up when it next asks, so the
    # banner says where to watch for the outcome instead of claiming one.
    "fleet_cmd_queued": ("green", "📨 Đã xếp lệnh — máy trạm nhận ở lần hỏi kế tiếp (thường "
                                  "dưới 1 phút). Kết quả hiện ở mục <b>Lệnh gần đây</b> của máy."),
    "fleet_cmd_all_queued": ("green", "📨 Đã xếp lệnh cho các máy đang online. Theo dõi kết quả "
                                      "ở từng máy bên dưới."),
    "fleet_cmd_all_none": ("amber", "Không có máy nào cần lệnh này (không máy online, hoặc mọi "
                                    "máy đã có lệnh đó đang chờ)."),
    "fleet_cmd_duplicate": ("amber", "Máy này đã có đúng lệnh đó đang chờ — không xếp thêm."),
    "fleet_cmd_invalid": ("red", "⛔ Lệnh không hợp lệ."),
    "fleet_cmd_unknown_worker": ("red", "⛔ Trung tâm không biết máy trạm này."),
    "fleet_cmd_cancelled": ("green", "✕ Đã huỷ lệnh."),
    "fleet_cmd_not_pending": ("amber", "Lệnh không còn ở trạng thái chờ — máy trạm đã nhận nó."),
    "fleet_dispatch_queued": ("green", "🎯 Đã giao việc — máy trạm sẽ gắn tag của nó lên work "
                                       "item ở lần hỏi kế tiếp và poller nhận ở vòng sau."),
    "fleet_dispatch_bad_id": ("red", "⛔ Nhập mã work item (số)."),
    "fleet_dispatch_nobody": ("amber", "Không có máy nào nhận được việc lúc này: không máy nào "
                                       "online, hoặc tất cả đang tạm dừng / đang cập nhật."),
    "fleet_local_pause": ("amber", "⏸ Máy này đã tạm dừng nhận việc mới. Run đang chạy vẫn "
                                   "chạy tiếp. Khởi động lại tiến trình cũng sẽ bỏ tạm dừng."),
    "fleet_local_resume": ("green", "▶ Máy này tiếp tục nhận việc."),
    "fleet_local_failed": ("red", "⛔ Không làm được — tiến trình này không chạy poller."),
    "sec_suppressed": ("green", "✅ Đã suppress — ghi cả DB lẫn "
                                "<code>.autopilot/security-suppressions.yaml</code>."),
    "sec_reopened": ("green", "↩ Đã mở lại finding."),
    "sec_filed": ("green", "🐞 Đã tạo Bug trên ADO cho finding."),
    "sec_file_failed": ("red", "⛔ Không tạo được Bug — xem log."),
    "sec_scan_started": ("green", "▶ Đang quét ở nền — làm mới trang sau ít phút."),
    "sec_scan_busy": ("amber", "⏳ Repo này đang được quét — chờ lượt hiện tại xong."),
    "err_sec_reason": ("red", "⛔ Suppress cần lý do."),
    "err_sec_repo_required": ("red", "⛔ Nhập hoặc chọn đường dẫn repo cần quét."),
    "err_sec_repo_invalid": ("red", "⛔ Đường dẫn repo không tồn tại trên máy chạy autopilot — "
                                    "kiểm tra lại (đường dẫn tuyệt đối, trên chính máy này)."),
    "session_closed": ("green", "✕ Đã đóng phiên — branch không bị thay đổi, thread trên PR đã "
                                "được báo."),
    "session_not_open": ("amber", "Phiên này không còn mở (có thể vừa xong)."),
    "conflict_resolve_started": ("green", "🔧 Đang giải conflict ở nền — kết quả hiện ở dòng "
                                          "tương ứng và trong comment trên PR."),
    "conflict_scan_started": ("green", "🔄 Đang quét PR — làm mới trang sau ít giây."),
    "conflict_not_resolvable": ("amber", "⏳ PR này đang được giải, hoặc đã hết conflict."),
    "conflict_session_closed": ("green", "✕ Đã đóng phiên interactive — branch không bị thay đổi."),
    "conflict_not_in_session": ("amber", "PR này không có phiên interactive nào đang mở."),
    "err_conflict_missing": ("red", "⛔ Không tìm thấy conflict."),
    "err_conflict_service": ("red", "⛔ Dịch vụ theo dõi conflict không chạy trong tiến trình "
                                    "này — bật <code>pr_conflict_tracking_enabled</code> rồi "
                                    "khởi động lại."),
    "sec_settings_saved": ("green", "✅ Đã lưu cấu hình quét và áp dụng ngay."),
    "sec_verify_started": ("green", "🧪 Đang dựng PoC trong worktree tách biệt — làm mới trang "
                                    "sau vài phút; kết quả hiện ở nhãn verified/unconfirmed."),
    "sec_verify_busy": ("amber", "⏳ Finding này đang được verify."),
    "err_sec_no_worktrees": ("red", "⛔ Verify cần <code>use_worktrees: true</code> để chạy PoC "
                                    "tách biệt khỏi checkout thật."),
    "err_sec_missing": ("red", "⛔ Không tìm thấy finding."),
    "loops_saved": ("green", "✅ Đã lưu lịch chạy và áp dụng ngay (không cần khởi động lại)."),
    "loop_deleted": ("green", "🗑 Đã xoá lịch chạy."),
    "loop_started": ("green", "▶ Đã chạy ngay — bấm <b>xem trực tiếp</b> ở dòng tương ứng để "
                              "theo dõi; báo cáo sẽ hiện ở "
                              "<a href='/dashboard/reports'>Reports</a> khi xong."),
    "loop_busy": ("red", "⏳ Lịch chạy này đang chạy dở — lần bấm này được bỏ qua để không "
                         "có hai agent cùng làm một việc trên cùng repo."),
    "err_loop_name": ("red", "⚠️ Chưa lưu — mỗi lịch chạy cần một tên riêng (không trùng)."),
    "err_loop_cadence": ("red", "⚠️ Chưa lưu — cron không hợp lệ và cũng không có "
                                "interval (phút). Lịch sẽ không bao giờ chạy."),
    "err_loop_blocked": ("red", "⛔ Lịch chạy này chưa chạy được — xem dòng đỏ ngay dưới tên nó "
                          "(thiếu repo hoặc cron không hợp lệ). Sửa xong bấm lại."),
    "err_loop_missing": ("red", "⚠️ Không tìm thấy lịch chạy đó (có thể vừa bị xoá)."),
    "ws_saved": ("green", "✅ Đã lưu workspace và áp dụng ngay (không cần khởi động lại)."),
    "ws_invalid": ("red", "⛔ Chưa lưu — xem các lỗi bên dưới. Giá trị bạn vừa nhập "
                          "vẫn được giữ."),
    "err_no_file": ("red", "⚠️ Chưa chọn tệp."),
    "err_invalid": ("red", "⚠️ Tệp không hợp lệ hoặc không phải YAML cấu hình."),
    "err_nothing": ("red", "⚠️ Tệp không chứa setting nào áp dụng được."),
    "err_password": ("red", "⚠️ Cần mật khẩu của tệp."),
    "err_wrong_password": ("red", "⚠️ Sai mật khẩu, hoặc tệp bị hỏng."),
    "err_role_tag_clash": (
        "red",
        "⛔ Chưa lưu — một <b>Run-now tag</b> của vai trò trùng tag đã có nghĩa khác "
        "(xem log). Đặt tên riêng cho nó rồi lưu lại.",
    ),
    "file_all_already": (
        "warn",
        "Mọi finding đã chọn đều có work item rồi — không tạo lại. "
        "Muốn tạo lại thì bật «tạo lại» ở đúng dòng đó.",
    ),
    "err_run_tag_clash": (
        "red",
        "⛔ Chưa lưu — <b>Run-now tag</b> trùng một tag đang có nghĩa khác. Xem chi "
        "tiết trong log; đặt một tên riêng (ví dụ <code>&lt;trigger-tag&gt;-run</code>) "
        "rồi lưu lại.",
    ),
    "err_no_export_password": (
        "red",
        "⚠️ Chưa đặt <b>Full-export password</b> — file sẽ không được bảo vệ. Đặt "
        "<code>config_export_password</code> ở mục <b>🔐 Web &amp; bảo mật</b> rồi export lại.",
    ),
}


def _flash(url: str, code: str) -> RedirectResponse:
    """Redirect to ``url`` carrying a one-shot ``code`` in a cookie, not the query string."""
    response = RedirectResponse(url, status_code=303)
    if code in FLASH_MESSAGES:
        response.set_cookie(
            _FLASH_COOKIE, code, max_age=30, httponly=True, samesite="lax",
            path="/dashboard",
        )
    else:  # a code we don't have wording for would show nothing — fail loudly in the log
        _log.error("unknown flash code — no banner will be shown", code=code)
    return response


def _take_flash(request: Request) -> tuple[str, str] | None:
    """``(colour, message)`` for this request's flash, or None. Caller must clear it."""
    return FLASH_MESSAGES.get(request.cookies.get(_FLASH_COOKIE) or "")


# A loop started from the page outlives its request by minutes. Held so the task is not
# garbage-collected mid-run — asyncio keeps only a weak reference to a bare create_task,
# and a collected task is a cancelled audit with no error anywhere.
_BACKGROUND_RUNS: set[asyncio.Task] = set()


def _int_or_zero(value: object) -> int:
    """Form field → non-negative int. Anything unparseable is 0, i.e. "no interval"."""
    try:
        return max(0, int(str(value or "").strip() or 0))
    except (TypeError, ValueError):
        return 0


def _workspace_agents(cfg) -> list[str]:
    """Sub-agent names defined in the workspace's ``.claude/agents``, sorted.

    Read from disk rather than kept in a list here: these are the user's own agents, and
    a page offering a name the workspace does not define would schedule a loop that
    delegates to nothing.
    """
    workspace = getattr(cfg, "workspace_directory", "") or ""
    root = Path(workspace or ".") / ".claude" / "agents"
    try:
        return sorted(p.stem for p in root.glob("*.md") if p.stem.lower() != "readme")
    except OSError:
        return []


def _workspace_repos(cfg) -> list[str]:
    """Repo names in the workspace — what the loop's Repo field expects to be given."""

    return discover_repos(getattr(cfg, "workspace_directory", "") or "")


def _next_run(scheduler, name: str) -> str:
    """When APScheduler will next fire this loop, as text; "" when it has no job.

    An empty answer on an enabled loop is the interesting case — it means the job never
    registered — so the page shows it rather than an optimistic guess from the cron.
    """
    if scheduler is None:
        return ""
    try:
        job = scheduler._scheduler.get_job(name)  # noqa: SLF001 — same package's service
        return job.next_run_time.strftime("%Y-%m-%d %H:%M") if job and job.next_run_time else ""
    except Exception:  # noqa: BLE001 — a schedule readout must never 500 the page
        return ""


def _reschedule(request: Request) -> int:
    """Apply the saved schedule to the running service; returns how many jobs are live."""
    scheduler = getattr(request.app.state, "loop_scheduler", None)
    if scheduler is None:
        return 0
    try:
        return int(scheduler.reload())
    except Exception as exc:  # noqa: BLE001 — the config is saved either way
        _log.error("loop reschedule failed — restart to apply", error=describe_exc(exc))
        return 0


# A rejected Flow save has to carry two things back to the GET: the reasons (free text,
# one per problem) and the values the operator typed, so their work isn't thrown away.
# Neither fits the flash cookie — a cookie is ~4KB and this is unbounded prose — so the
# payload is held server-side under a random token and only the token travels.
_FLOW_ERROR_COOKIE = "autopilot_flow_errors"
_FLOW_REJECTS: OrderedDict[str, dict] = OrderedDict()
_FLOW_REJECTS_MAX = 32   # bounded: this is a hand-off buffer, not storage


def _flow_reject(errors: list[str], flows: list[dict]) -> RedirectResponse:
    token = secrets.token_urlsafe(12)
    _FLOW_REJECTS[token] = {"errors": errors, "flows": flows}
    while len(_FLOW_REJECTS) > _FLOW_REJECTS_MAX:
        _FLOW_REJECTS.popitem(last=False)
    response = _flash("/dashboard/flow", "flow_invalid")
    response.set_cookie(
        _FLOW_ERROR_COOKIE, token, max_age=120, httponly=True, samesite="lax",
        path="/dashboard",
    )
    return response


def _take_flow_reject(request: Request) -> dict:
    """The pending rejection for this request (``{}`` if none). Consumes it."""
    return _FLOW_REJECTS.pop(request.cookies.get(_FLOW_ERROR_COOKIE) or "", {})


# Same hand-off as the Flow editor, for the same reason: a rejected save must come back
# with both the reasons and what the operator typed.
_LENS_ERROR_COOKIE = "autopilot_lens_errors"
_LENS_REJECTS: OrderedDict[str, dict] = OrderedDict()


def _lens_reject(errors: list[str], lenses: list[dict]) -> RedirectResponse:
    token = secrets.token_urlsafe(12)
    _LENS_REJECTS[token] = {"errors": errors, "lenses": lenses}
    while len(_LENS_REJECTS) > _FLOW_REJECTS_MAX:
        _LENS_REJECTS.popitem(last=False)
    response = _flash("/dashboard/board-views", "lens_invalid")
    response.set_cookie(
        _LENS_ERROR_COOKIE, token, max_age=120, httponly=True, samesite="lax",
        path="/dashboard",
    )
    return response


def _take_lens_reject(request: Request) -> dict:
    """The pending rejection for this request (``{}`` if none). Consumes it."""
    return _LENS_REJECTS.pop(request.cookies.get(_LENS_ERROR_COOKIE) or "", {})


# Same hand-off as the Flow editor, for the same reason: a rejected save must come back
# with both the reasons and what the operator typed.
_WS_ERROR_COOKIE = "autopilot_ws_errors"
_WS_REJECTS: OrderedDict[str, dict] = OrderedDict()


def _ws_reject(errors: list[str], views: list) -> RedirectResponse:
    token = secrets.token_urlsafe(12)
    _WS_REJECTS[token] = {"errors": errors, "views": views}
    while len(_WS_REJECTS) > _FLOW_REJECTS_MAX:
        _WS_REJECTS.popitem(last=False)
    response = _flash("/dashboard/workspaces", "ws_invalid")
    response.set_cookie(
        _WS_ERROR_COOKIE, token, max_age=120, httponly=True, samesite="lax",
        path="/dashboard",
    )
    return response


def _take_ws_reject(request: Request) -> dict:
    return _WS_REJECTS.pop(request.cookies.get(_WS_ERROR_COOKIE) or "", {})


# Which workspace the dashboard is currently looking at. A cookie rather than a config
# field on purpose: this is a VIEW filter, personal to the browser, and it must never
# be mistaken for a switch that changes what the autopilot processes.
_WS_COOKIE = "autopilot_workspace"


def selected_workspace(request: Request) -> str:
    """The workspace id this request is scoped to — ``"all"`` when unscoped.

    A query parameter wins over the cookie so a link can carry the scope (and so the
    selector itself works without JavaScript)."""
    return (request.query_params.get("workspace")
            or request.cookies.get(_WS_COOKIE)
            or "all").strip()


def scope_of(request: Request, config) -> tuple[str, list[str] | None]:
    """``(workspace_id, projects_to_show)`` for this request.

    ``projects_to_show`` is ``None`` for "everything" and a (possibly empty) list for a
    specific workspace — the two are NOT interchangeable: an empty workspace must show
    nothing, not everything."""
    workspace_id = selected_workspace(request)
    return workspace_id, workspaces_mod.scope_projects(config, workspace_id)

_CATEGORY_LABELS = {
    "backendtask": ("BE", "cat-be"),
    "frontendtask": ("FE", "cat-fe"),
    "bug": ("Bug", "cat-bug"),
    "databasetask": ("DB", "cat-db"),
    "testtask": ("QC", "cat-qc"),
    "requirement": ("Req", "cat-req"),
}
_STATUS_CLASS = {
    "Success": "badge-success",
    "Failed": "badge-failed",
    "Running": "badge-running",
}
# Baseline work-item types offered in the Planning filter (merged with whatever
# types the loaded items actually have, so custom process types show up too).
_COMMON_WI_TYPES = ("Bug", "Task", "User Story", "Requirement", "Feature", "Test Case")


def _pr_age(created: str | None) -> str:
    """Human-readable age of a PR from its ISO creationDate (best-effort)."""
    if not created:
        return ""
    try:
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return ""
    delta = datetime.now(UTC) - dt
    days, secs = delta.days, delta.seconds
    if days >= 1:
        return f"{days}d"
    if secs >= 3600:
        return f"{secs // 3600}h"
    return f"{max(1, secs // 60)}m"


def _pr_status(pr: dict, approved: int, blocked: int, pending: int, conflicts: bool) -> str:
    """A single overall status token for the board badge."""
    if pr.get("isDraft"):
        return "draft"
    if conflicts:
        return "conflicts"
    if blocked:
        return "blocked"
    if approved and not pending:
        return "approved"
    if approved:
        return "partial"
    return "awaiting"


def _category_badge(category: str) -> tuple[str, str]:
    label, css = _CATEGORY_LABELS.get((category or "").lower(), (category or "—", "cat-default"))
    return label, css


def _model_label(name: str | None) -> str:
    """A model id trimmed to what a person reads in a table cell.

    "claude-haiku-4-5-20251001" is 25 characters of which four carry the meaning. The
    vendor prefix and the release date are dropped for display and kept in the cell's
    tooltip, so the column stays narrow without losing the exact id a cost review needs.
    """
    raw = (name or "").strip()
    if not raw:
        return ""
    extra = ""
    if " +" in raw:                       # "claude-opus-5 +1" — more than one model ran
        raw, _, tail = raw.partition(" +")
        extra = f" +{tail}"
    short = raw.removeprefix("claude-")
    head, sep, tail = short.rpartition("-")
    if sep and tail.isdigit() and len(tail) == 8:   # trailing YYYYMMDD
        short = head
    return short + extra


def _tokens_detail(record) -> str:
    """The tooltip behind a token count: where the tokens went, and what it cost.

    A single total cannot distinguish a run that re-read the whole repo from one that
    reasoned hard, and those have very different fixes. Rows written before the
    breakdown existed return "" so the UI shows the bare number rather than a row of
    fabricated zeroes.
    """
    parts = []
    for label, value in (
        ("Input", getattr(record, "input_tokens", None)),
        ("Output", getattr(record, "output_tokens", None)),
        ("Cache read", getattr(record, "cache_read_tokens", None)),
        ("Cache write", getattr(record, "cache_creation_tokens", None)),
    ):
        if value:
            parts.append(f"{label}: {value:,}")
    cost = getattr(record, "cost_usd", None)
    if cost is not None:
        parts.append(f"Chi phi: ${cost:.4f}")
    model = getattr(record, "model_used", None)
    if model:
        parts.append(f"Model: {model}")
    return " | ".join(parts)


def _status_class(status: str) -> str:
    return _STATUS_CLASS.get(status, "badge-retrying")


def _mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _fmt_duration(seconds: float) -> str:
    """How LONG something took. Stays precise: a run's duration is a measurement."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _fmt_ago(seconds: float) -> str:
    """How long AGO something happened — a different question from how long it took.

    Both used :func:`_fmt_duration`, which stops at hours and keeps one decimal, so the
    fleet page reported a machine last seen three days ago as "74.1h trước". That is a
    measurement pretending to be a fact about the world: nobody counts past a day in
    hours, and the .1 implies a precision that means nothing at that distance — a
    machine quiet for 74 hours is not meaningfully different from one quiet for 75.

    Precision therefore falls off with distance, the way people actually speak: seconds
    while it is live, then minutes, hours, days, weeks.
    """
    seconds = max(0.0, seconds)
    if seconds < 45:
        return "vài giây"
    if seconds < 90:
        return "1 phút"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f} phút"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.0f} giờ"
    days = hours / 24
    if days < 7:
        return f"{days:.0f} ngày"
    weeks = days / 7
    if weeks < 5:
        return f"{weeks:.0f} tuần"
    return f"{days / 30:.0f} tháng"


# How many ADO reads a page-level scan may have in the air at once. Bounded rather
# than unbounded: the PR link lookups are one request per pull request and can number
# in the hundreds, which is a denial of service aimed at ourselves.
_PR_SCAN_CONCURRENCY = 8


class _ScanCache:
    """A page's expensive scan, held so the page does not pay for it.

    Three dashboard pages (Overview, Reviews, Delivery) and the Board each used to run
    an Azure DevOps fan-out INSIDE the request handler, and each had grown its own
    half of the same answer — one had a TTL and no single-flight, one had neither, one
    cached its own failures as though they were data. Writing that four times is how
    they drifted, so it is written once here.

    Two ways to read it, because the pages genuinely differ:

    * :meth:`background` — hand back whatever we have and refresh BEHIND the render.
      For figures nobody reads to the second. The page never waits, not even when ADO
      does not answer at all.
    * :meth:`blocking` — wait for a value, but share ONE in-flight scan between every
      concurrent caller. For a view that must be current the moment it is asked for
      (the Board, right after somebody moved a card).

    A failed scan is never published as data and never marked fresh: the next reader
    retries instead of being served a silently short list. ``failed`` is how a page
    tells "we could not reach ADO" apart from "we have not counted yet", which are
    different sentences to whoever is reading the screen.
    """

    def __init__(self, name: str, ttl: float) -> None:
        self._name = name
        self._ttl = ttl
        self._at = 0.0
        self._value = None
        # Single-flight. Without it, N concurrent loads on a cold cache each started a
        # scan of their own — the load that made a page hang also throttled ADO into
        # 429s, whose backoff sleeps then made the next load slower still.
        self._lock = asyncio.Lock()
        # Strong reference to the in-flight background refresh: a bare create_task()
        # may be garbage collected mid-flight, silently abandoning the scan.
        self._task: asyncio.Task | None = None
        # A scan running right now, for coalesced(): shared, never stored.
        self._inflight: asyncio.Task | None = None
        self.failed = False

    @property
    def fresh(self) -> bool:
        return self._value is not None and time.monotonic() - self._at < self._ttl

    @property
    def value(self):
        return self._value

    def invalidate(self) -> None:
        """Drop freshness — the next read re-scans. For after a MUTATION: the Board
        showing the card where it used to be is not a stale figure, it is a wrong one."""
        self._at = 0.0

    def reset(self) -> None:
        """Forget everything, including the failure flag. Tests and config reloads."""
        self._at, self._value, self.failed = 0.0, None, False
        self._inflight = None

    async def _run(self, loader) -> None:
        async with self._lock:
            if self.fresh:
                return          # somebody refreshed while we waited for the lock
            try:
                self._value = await loader()
                self._at = time.monotonic()
                self.failed = False
            except Exception as exc:  # noqa: BLE001 — a panel is not worth the page
                self.failed = True
                _log.warning(f"{self._name} scan failed", error=describe_exc(exc))

    def _drain(self, task: asyncio.Task) -> None:
        """Consume a background task's exception so it is logged here rather than
        surfacing as asyncio's "Task exception was never retrieved" at GC time."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.warning(f"{self._name} refresh crashed", error=describe_exc(exc))

    def background(self, loader):
        """``(value, pending)``. Never awaits the loader.

        ``value`` is None only before the first scan has produced anything; ``pending``
        says that is because one is still running rather than because it failed.
        """
        if not self.fresh and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run(loader))
            self._task.add_done_callback(self._drain)
        return self._value, (self._value is None and not self.failed)

    async def blocking(self, loader):
        """Wait for a value, sharing one scan across concurrent callers."""
        if not self.fresh:
            await self._run(loader)
        return self._value

    async def coalesced(self, loader):
        """Join a scan already in flight rather than starting a second one.

        Deliberately NOT caching. The Board is the page people watch to see the agent
        move work, and the agent moves it in ADO directly — not only through this
        dashboard's own buttons. A TTL there would hold the board behind the autopilot's
        own hand-offs, which is a board showing a move that already happened. So the
        only thing shared here is a scan that is still running: concurrent tabs and
        users collapse onto one request, and nobody is shown anything older than the
        scan that was in flight when they asked.
        """
        task = self._inflight
        if task is None or task.done():
            task = self._inflight = asyncio.ensure_future(loader())
            task.add_done_callback(self._settle_inflight)
        # Shielded: a reader who disconnects must not cancel the scan the others joined.
        return await asyncio.shield(task)

    def _settle_inflight(self, task: asyncio.Task) -> None:
        if self._inflight is task:
            self._inflight = None
        if not task.cancelled() and task.exception() is not None:
            pass    # every awaiter receives it; retrieving here only silences the GC

# The Reviews board scans every repo × active PR × reviewers on each load — cache the
# assembled view briefly so refreshes don't hammer ADO (the tracker updates state on its
# own 30s cadence anyway).
# Label + colour tone per action kind, in the order the report already sorts them.
# Kept here rather than in delivery.py so the pure module stays free of presentation.
_ACTION_LABELS: dict[str, tuple[str, str]] = {
    delivery.KIND_CONFLICT_PR: ("PR bị conflict", "red"),
    delivery.KIND_BLOCKED_PR: ("PR bị từ chối", "red"),
    delivery.KIND_MERGE_READY: ("Chờ merge", "red"),
    delivery.KIND_REVIEW_WAITING: ("Chờ review", "amber"),
    delivery.KIND_NEEDS_HUMAN: ("Autopilot cần người", "amber"),
    delivery.KIND_STALE: ("Đứng im", "grey"),
    delivery.KIND_FAILED: ("Run thất bại", "grey"),
}


def work_item_link_base(cfg) -> str:
    """Base URL for "open work item #id in Azure DevOps" links, or "" with no org.

    With several work-item projects polled, most pages do not know which project a
    given id belongs to (the DB records an id, not a project). Azure DevOps resolves
    a work item id ORG-wide, so the project-less form redirects to the right project —
    which is exactly right here, and strictly better than guessing the default project
    and handing the reader a link that 404s. A single-project setup keeps the explicit,
    unchanged URL."""
    org = (cfg.ado_organization or "").rstrip("/")
    if not org:
        return ""
    projects = cfg.effective_ado_projects
    if len(projects) == 1 and projects[0]:
        return f"{org}/{quote(projects[0])}/_workitems/edit"
    return f"{org}/_workitems/edit"


# The Reviews board is a scan of every repo × active PR × its reviewers. Held so a
# filter change — which is a question about data we already have — does not re-ask ADO.
_REVIEWS = _ScanCache("reviews", ttl=60.0)

# The board's work-item list: one WIQL plus a batched detail fetch, re-run by the page's
# own 15-second auto-refresh. Every open tab paid for its own, so N tabs on one board
# meant N identical scans; they are collapsed into one.
#
# ttl=0 is the point, not an oversight. A first attempt cached this for 12 seconds and a
# test caught what that costs: the board changes because the AUTOPILOT moves items in
# ADO, not only when somebody presses a button here, so a TTL holds the board behind the
# agent's own hand-offs. Only a scan that is still running is shared — see
# _ScanCache.coalesced.
_BOARD_ITEMS = _ScanCache("board items", ttl=0.0)

def forget_scans() -> None:
    """Drop every cached scan.

    Called when the process's view of Azure DevOps changes underneath us — a config
    reload that repoints the organization, or that changes which projects are polled.
    Holding a board of the PREVIOUS project's items for the rest of the TTL is not a
    stale figure, it is somebody else's board.

    Also called at app startup, which is what keeps these process-wide caches from
    leaking between apps in one process (every test client builds its own app).
    """
    # Looked up at CALL time, not bound at import: _PR_OUTCOMES is declared further
    # down this module, next to the scan it caches.
    for cache in (_PR_OUTCOMES, _REVIEWS, _BOARD_ITEMS):
        cache.reset()

# The three shapes we mint: a work-item id, ``pr-<id>``, and ``loop-<slug>`` for a
# scheduled agent (see ``activity.loop_key``). The slug charset is deliberately narrow —
# this string is interpolated into a path, and the guard exists so a key can never be
# anything but one of these.
_FEED_KEY_RE = re.compile(r"^(?:pr-[0-9]+|loop-[a-z0-9-]+|[0-9]+)$")


def _feed_key(raw: str) -> str:
    """Validate an activity-feed key from the URL before it reaches the filesystem.

    The key is a string now (PR runs are keyed ``pr-<id>``, scheduled agents
    ``loop-<slug>``) and it is interpolated straight into a path, so it is held to
    exactly the shapes we mint. Anything else yields "", which reads an empty feed
    rather than whatever ``../..`` pointed at.
    """
    return raw if _FEED_KEY_RE.match(raw) else ""


# "How many PRs have we merged" is not a figure anybody reads to the second, and the
# scan behind it is 1 + 3N list requests plus a link lookup per PR whose branch carries
# no work-item id. Minutes, not seconds: a 60s TTL bought nothing and paid for a full
# re-scan every minute.
_PR_OUTCOMES = _ScanCache("pr outcomes", ttl=300.0)


async def _gather_scan(coros) -> list:
    """Run scan reads concurrently under one semaphore, results in call order.

    Raises the FIRST failure rather than returning a short list — a page that quietly
    drops the repos it could not reach reports less work in flight than there is, which
    reads as progress. Every coroutine is still awaited before we raise, so none is left
    to surface later as an unretrieved exception.
    """
    gate = asyncio.Semaphore(_PR_SCAN_CONCURRENCY)

    async def guarded(coro):
        async with gate:
            return await coro

    out = await asyncio.gather(*(guarded(c) for c in coros), return_exceptions=True)
    for item in out:
        if isinstance(item, BaseException):
            raise item
    return list(out)


def _pr_outcomes_pending() -> dict:
    """Placeholder served while the very first scan is still running.

    Distinct from a FAILED scan (``ok`` False): "we have not counted yet" and "we asked
    ADO and it would not answer" are different things to a reader, and showing the
    error wording for a cold cache would have people chasing an outage that is not
    happening.
    """
    return {"merged": 0, "active": 0, "abandoned": 0, "ok": True,
            "pending": True, "merge_rate": None}


def _pr_outcomes_failed() -> dict:
    return {"merged": 0, "active": 0, "abandoned": 0, "ok": False,
            "pending": False, "merge_rate": None}


async def _scan_pr_outcomes(c: Container) -> dict:
    """The actual ADO scan behind :func:`_pr_outcomes`. Never called on the request path.

    Every read runs under one semaphore, so the cost is bounded by concurrency rather
    than by how many PRs the organization happens to have. It used to be strictly
    sequential: 1 + 3N list requests, and then ONE MORE request per PR whose branch
    name carries no work-item id — which on a real org is most of them, because humans
    do not name branches ``<prefix>/<id>-<slug>``. At ~200 ms a round trip that is a
    minute of dead time, which is exactly what made /dashboard/ look hung.

    Raises on failure rather than returning zeros: the cache must not publish a short
    count as though it were an answer.
    """
    counts: dict = {"merged": 0, "active": 0, "abandoned": 0, "ok": True, "pending": False}
    ours = await c.execution_repo.work_item_ids()
    repo_ids = [r["id"] for r in await c.ado.get_repositories() if r.get("id")]
    jobs = [
        (key, rid, fetch)
        for rid in repo_ids
        for key, fetch in (
            ("merged", c.ado.get_completed_pull_requests),
            ("active", c.ado.get_active_pull_requests),
            ("abandoned", c.ado.get_abandoned_pull_requests),
        )
    ]
    lists = await _gather_scan(fetch(rid) for _, rid, fetch in jobs)

    # Pass 1 — everything the branch name already answers, for free. Branch name FIRST
    # here, unlike everywhere else: every PR the autopilot opened is named
    # `<prefix>/<id>-<slug>`, so link-first would add a request per PR to compute a
    # percentage. The fallback below still closes the gap it is here for.
    unresolved: list[tuple[str, str, int]] = []   # (key, repo_id, pr_id)
    for (key, rid, _), prs in zip(jobs, lists, strict=True):
        for pr in prs:
            wid = parse_work_item_id(pr.get("sourceRefName", ""))
            if wid is None:
                unresolved.append((key, rid, pr.get("pullRequestId") or 0))
            elif wid in ours:
                counts[key] += 1

    # Pass 2 — ask ADO for the link only for the PRs the name could not answer, and ask
    # for all of them at once. Memoised client-side (see
    # AdoClient.get_pull_request_work_items), so a warm process mostly skips this.
    if unresolved:
        linked = await _gather_scan(
            c.ado.get_pull_request_work_items(rid, pid) for _, rid, pid in unresolved
        )
        for (key, _, _), ids in zip(unresolved, linked, strict=True):
            if ids and ids[0] in ours:
                counts[key] += 1

    decided = counts["merged"] + counts["abandoned"]
    counts["merge_rate"] = round(100 * counts["merged"] / decided) if decided else None
    return counts


def _pr_outcomes(c: Container) -> dict:
    """Merged / active / abandoned counts of the autopilot's OWN PRs, across every repo —
    the denominator for "is this actually shipping work, and at what cost".

    A PR is ours when its branch names a work item this autopilot has an execution record
    for. It used to be "the branch starts with a bot prefix", which was wrong in both
    directions and made the cost-per-PR figure meaningless:

      * ``feature/`` and ``fix/`` are what the team names branches too, so on a real project
        this counted 102 merged PRs when 7 were the autopilot's — reporting its cost per
        shipped PR as 719k tokens instead of 10.5M, 14x too cheap.
      * an agent-chosen prefix that isn't on the list (``dxfac/feature/6526-…``) was skipped
        even though it was ours.

    **This never blocks the page.** A stale figure is refreshed in the BACKGROUND and the
    caller is handed what we already have, because the alternative — waiting out a scan
    of every PR in the organization before a single byte of HTML is written — is what
    made the Overview appear to hang. Five stat cards are not worth a page load.
    """
    value, pending = _PR_OUTCOMES.background(lambda: _scan_pr_outcomes(c))
    if value is not None:
        return value
    return _pr_outcomes_pending() if pending else _pr_outcomes_failed()


# How long a run may go without producing an event before the page calls it out. The
# heartbeat in claude_client uses the same idea: a run producing events is working,
# one that has produced none for minutes is the case worth a person's eye.
_QUIET_WARN_SECONDS = 180
# Quiet this long is not "a long build step" any more — it is where to look first.
_QUIET_STUCK_SECONDS = 600

def _live_session_activity(c, item_id: int) -> tuple[str, float | None]:
    """What an interactive session last did, and how long ago. ``("", None)`` if unknown.

    Interactive runs write no activity feed: the feed comes from the SDK event stream,
    which only a headless run has. Their own transcript is the substitute, and it lives
    under the cwd the session was launched in — the isolated scratch normally, the
    shared workspace on an install that does not use worktrees. Both are tried, because
    picking only the first would leave the non-worktree install exactly as blind as
    before.
    """
    for cwd in (c.executor.interactive_scratch_dir(item_id), c.config.workspace_directory):
        if not cwd:
            continue
        line, quiet = c.executor.interactive_last_activity(cwd)
        if quiet is not None:
            return line, quiet
    return "", None


_REVIEW_STATUSES = ("awaiting", "approved", "blocked", "conflicts", "partial", "draft")

def _filter_reviews(prs: list[dict], qp, me: list[str]) -> list[dict]:
    """Apply the Reviews page filters to the scanned PR list.

    Twenty-two PRs in one list is a list nobody reads: the question is never
    "show me every PR", it is "which of these is waiting on ME", "what is blocked",
    "what is in this repo". Pure, so the answers can be tested without ADO.
    """
    out = prs
    q = (qp.get("q") or "").strip().lower()
    if q:
        out = [
            p for p in out
            if q in str(p["id"]) or q in p["title"].lower()
            or q in p["source"].lower() or q in (str(p["work_item"] or ""))
        ]
    status = (qp.get("status") or "all").strip().lower()
    if status in _REVIEW_STATUSES:
        # "draft" is a flag, not a status — a draft still has a review status of
        # its own, so asking for drafts must not depend on which one it is.
        out = [p for p in out if (p["is_draft"] if status == "draft" else p["status"] == status)]
    for key, field in (("repo", "repo"), ("author", "author"), ("target", "target")):
        want = (qp.get(key) or "all").strip()
        if want and want != "all":
            out = [p for p in out if p[field] == want]
    if (qp.get("mine") or "").strip() in {"1", "true", "yes"} and me:
        out = [
            p for p in out
            if any(matches_any_user(None, r["name"], me) and r["vote"] == 0
                   for r in p["reviewers"])
        ]
    return out




def _json_list(raw: str | None) -> list:
    """A JSON column back as a list — never an exception into a page render.

    These columns are written by another machine, so a value this process cannot parse
    is a real possibility (an older worker, a truncated write). The page showing one
    machine's snapshot as empty is recoverable; the page failing to render is not.
    """
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


def files_changed_count(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        return len(json.loads(raw))
    except (ValueError, TypeError):
        return 0
