"""Editable-settings form: field specs, form parsing, and persistence.

Drives the ``/dashboard/settings`` page. Pure helpers (parsing/coercion/merge)
are kept here so they can be unit-tested without a running server.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from ai_autopilot.logging_config import describe_exc, get_logger

_log = get_logger("dashboard.settings")


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str  # text | password | int | float | bool | select | list | stateset | stateone | map
    section: str
    help: str = ""
    options: tuple[str, ...] = field(default_factory=tuple)
    # Example shown in an empty list/map box. Every map used to print the SAME one
    # ("ba => Ready for Dev"), so the tag map and the type map both told you to type
    # a state — the fields that are hardest to tell apart were the ones lying.
    placeholder: str = ""
    # "Only relevant when <key> is one of <values>". A setting that does nothing on
    # THIS machine still had to be read and dismissed by everyone configuring it — the
    # fleet block showed four worker-only fields to a central, which reads as "fill
    # these in".
    #
    # The page resolves this in two steps, because "hide it" and "never lose a value"
    # are both true and only look contradictory:
    #   • inapplicable AND empty  → HIDDEN. There is nothing to see and nothing to lose.
    #   • inapplicable AND set    → shown dimmed with the reason. A value somebody
    #                               configured must not vanish from the page whose job
    #                               is to show the configuration.
    # Each section then offers one toggle to reveal what it hid, so the hiding is never
    # something you have to know about to undo. Re-evaluated live as the controlling
    # field changes — picking "worker" lights up the worker fields immediately.
    #
    # For a BOOL controlling field the values are ("1",): a ticked checkbox reads as
    # "1" and an unticked one as "" on both sides (see :func:`control_value`).
    show_when_key: str = ""
    show_when_values: tuple[str, ...] = field(default_factory=tuple)
    # Offers a "generate" button next to the input (password kinds). For a shared secret
    # nobody should be inventing by hand.
    generate: bool = False
    # Password kinds only: the 👁 button fetches and shows the STORED value, instead of
    # only un-masking whatever was typed into an empty box. Reserved for a secret whose
    # whole purpose is to be copied somewhere else — the fleet token has to be typed
    # into every worker, and a shared secret you cannot read back is one you have to
    # rotate across the fleet just to find out what it was.
    reveal: bool = False


# Order here is the order rendered on the page. Sections group consecutive fields.
# Declares the fields themselves; the parent-switch relationships are applied below,
# in ``_DEPENDS_ON``, so they can be read as one table instead of hunting a keyword
# argument through nine hundred lines.
_BASE_FIELDS: tuple[Field, ...] = (
    # ── Workspace & Repository ──
    Field("workspace_directory", "Thư mục workspace", "text", "📁 Workspace & repo",
          "Thư mục chứa .claude dùng chung (skills/rules/MCP). Claude chạy TẠI ĐÂY và agent tự "
          "chọn thư mục repo con cần sửa. Để trống = chế độ cũ (chạy trong một repo). Đây là "
          "workspace MẶC ĐỊNH — muốn chạy nhiều workspace, dùng trang Workspaces (trang này sửa "
          "chính field này làm mục đầu tiên)."),
    Field("base_branch", "Branch gốc", "text", "📁 Workspace & repo",
          "Branch để cắt các feature branch mới."),
    Field("repo_descriptions", "Mô tả repo", "list", "📁 Workspace & repo",
          "Mỗi repo là gì, để agent chọn đúng repo. Mỗi dòng một 'RepoName = description', vd "
          "'Backend-Fresh = .NET API', 'Dxfac-gitops = deploy manifests, don't edit'."),
    # ── Azure DevOps Connection ──
    Field("ado_organization", "URL organization", "text", "🔌 Kết nối Azure DevOps",
          "vd https://dev.azure.com/your-org"),
    Field("ado_project", "Project (work item)", "text", "🔌 Kết nối Azure DevOps",
          "Project work item MẶC ĐỊNH — nơi tạo item mới và là nơi mặc định cho mọi thứ không có "
          "project riêng. Trang này chỉ cấu hình kết nối ADO; workspace nào có work item nằm trên "
          "JIRA thì khai báo ở trang Workspaces, theo từng workspace — PR vẫn luôn đi qua kết nối "
          "này."),
    Field("ado_projects", "↳ Thêm project (work item)", "list", "🔌 Kết nối Azure DevOps",
          "Các project work item bổ sung, poll trên CÙNG kết nối này (mỗi dòng một project). Tất "
          "cả gộp trong một query nên thêm project không tốn thêm lượt poll. Dùng 'Extra "
          "workspaces' ở trên để gán thư mục/repo riêng cho một project; nếu không, project dùng "
          "workspace cấu hình ở đây."),
    Field("code_project", "Project chứa code (repo/PR)", "text", "🔌 Kết nối Azure DevOps",
          "Project chứa git repo, PR và build pipeline, nếu khác project work item. Để trống = "
          "giống nhau. (Cấu hình chéo project.)"),
    Field("ado_pat", "Personal Access Token (PAT)", "password", "🔌 Kết nối Azure DevOps",
          "Để trống = giữ token hiện tại."),
    # ── Tags & Trigger ──
    Field("trigger_tag", "Tag kích hoạt", "text", "🏷️ Tag & điều kiện nhận việc",
          "Work item có tag này sẽ được xử lý."),
    Field("assignee_trigger_tag", "Tag kích hoạt theo người được giao", "text",
          "🏷️ Tag & điều kiện nhận việc",
          "Xử lý thêm các item có tag dùng chung NÀY, nhưng chỉ item giao cho người bên dưới (vd "
          "'ai-autopilot' dùng chung cả team). Để trống = tắt."),
    Field("assignee_trigger_user", "↳ do ai xử lý (assignee)", "text",
          "🏷️ Tag & điều kiện nhận việc",
          "Assignee (tên/email) mà máy này nhận cho tag dùng chung ở trên. Để trống = dùng "
          "assignee của auto-transition. Đây cũng là OWNER: tài khoản mà máy này mặc định nghe "
          "/commands và @mention."),
    Field("command_users", "↳ người khác được ra lệnh", "list", "🏷️ Tag & điều kiện nhận việc",
          "Tài khoản bổ sung (email hoặc họ tên đầy đủ, mỗi dòng một) được ra /commands và "
          "@mention trên PR — đồng đội đang review PR của bạn có thể yêu cầu sửa mà không bị từ "
          "chối. KHÔNG đổi việc work item của ai được nhận. Owner trống VÀ danh sách này trống = "
          "ai cũng ra lệnh được. Dùng email đầy đủ; chỉ tên riêng thì không khớp ai (xem doctor)."),
    Field("commands_from_anyone", "↳ cho BẤT KỲ AI ra lệnh", "bool", "🏷️ Tag & điều kiện nhận việc",
          "Nhận /commands và @mention từ mọi tài khoản, không cần liệt kê ở trên. Chỉ mở cổng ra "
          "lệnh — work item nào máy này nhận vẫn giới hạn theo owner. Tắt để quay về danh sách."),
    Field("trigger_states", "State kích hoạt", "stateset", "🏷️ Tag & điều kiện nhận việc",
          "Các state ADO được phép xử lý — tick từ board, hoặc thêm state riêng bên dưới."),
    Field("reprocess_on_reopen", "Chạy lại khi bị mở lại", "bool", "🏷️ Tag & điều kiện nhận việc",
          "Khi item đã xử lý bị kéo về state kích hoạt, xoá các tag autopilot để nó chạy lại. "
          "(Chỉ áp dụng cho state kích hoạt mà autopilot không tự đặt.)"),
    Field("restart_tag", "♻️ Tag chạy lại từ đầu", "text", "🏷️ Tag & điều kiện nhận việc",
          "Gắn tag này vào item để XOÁ tiến độ SDLC và xử lý lại từ đầu, từ bất kỳ state nào, "
          "dùng các comment mới nhất của bạn. Reopen chạy tiếp giữa vòng; restart làm lại từ "
          "stage 0. Để trống = tắt."),
    Field("stage_entry_tag", "▶ Tag chạy ngay (dùng chung)", "text", "🏷️ Tag & điều kiện nhận việc",
          "Gắn tag này vào item để chạy role mà state HIỆN TẠI của nó chỉ định, ngay tại chỗ — "
          "cách chạy một role cố ý không có trong query poll. Bị gỡ khi nhận việc. Tag không tự "
          "chỉ định role, nên ở state KHÔNG có role nào chờ thì rơi về profile mặc định — thường "
          "là cả pipeline. Muốn chạy một role cụ thể từ bất kỳ state nào, gán cho role đó tag "
          "chạy ngay riêng ở trang Roles. Để trống = không có tag dùng chung (tag riêng từng role "
          "vẫn hoạt động)."),
    Field("poll_interval_seconds", "Chu kỳ quét (giây)", "int", "🏷️ Tag & điều kiện nhận việc"),
    # ── Outcomes → tag + state ──
    # The policy table: for each outcome, the ADO tag to add and the ADO state to
    # set. Blank = skip. This is the single source of truth for tagging + state.
    Field("state_in_progress", "⏳ Đang làm — state ADO", "stateone", "🎯 Kết quả → tag + state",
          "State khi autopilot bắt đầu làm một item (không gắn tag)."),
    Field("review_tag", "🔍 Chờ review — tag", "text", "🎯 Kết quả → tag + state",
          "Tag gắn khi mở draft PR (chờ review); item bị giữ lại."),
    Field("state_in_review", "🔍 Chờ review — state ADO", "stateone", "🎯 Kết quả → tag + state",
          "State khi mở draft PR (chờ người review)."),
    Field("processed_tag", "✅ Xong — tag", "text", "🎯 Kết quả → tag + state",
          "Tag gắn khi item đã xử lý xong (cũng dùng cho report / failed nếu không đặt riêng)."),
    Field("resolved_state", "✅ Xong — state ADO", "stateone", "🎯 Kết quả → tag + state",
          "State khi item được resolve kèm PR (Resolved / Closed / Done)."),
    Field("state_report", "📝 Báo cáo — state ADO", "stateone", "🎯 Kết quả → tag + state",
          "State khi kế hoạch được comment ở chế độ report (tag = tag Xong)."),
    Field("escalation_tag", "🙋 Cần người — tag", "text", "🎯 Kết quả → tag + state",
          "Tag gắn khi agent chuyển cho người; item đang bị giữ sẽ được bỏ qua."),
    Field("state_needs_human", "🙋 Cần người — state ADO", "stateone", "🎯 Kết quả → tag + state",
          "State khi agent chuyển cho người và giữ item lại."),
    Field("failed_tag", "⛔ Thất bại — tag", "text", "🎯 Kết quả → tag + state",
          "Tag gắn khi autopilot bỏ cuộc sau khi đã thử lại. Để trống = dùng tag Xong."),
    Field("state_failed", "⛔ Thất bại — state ADO", "stateone", "🎯 Kết quả → tag + state",
          "State khi autopilot bỏ cuộc sau khi hết lượt thử lại."),
    # ── Board columns ──
    Field("board_review_state", "Cột: Ready for review", "stateset", "🗂️ Cột trên Board",
          "Các state ADO hiện ở cột 'Ready for review' trên board. Để trống = không có cột. Dùng "
          "state mà autopilot không tự đặt. Được chọn nhiều — một cột chứa cả một chặng của quy "
          "trình, không phải một state."),
    # Listed in board order (review → deploy → testing): the build goes onto the test
    # environment before QC can verify it, and a settings page that lists them in a
    # different order than the board teaches the wrong sequence.
    Field("board_deploy_state", "Cột: Ready for deploy", "stateset", "🗂️ Cột trên Board",
          "Các state ADO hiện ở cột 'Ready for deploy' trên board — đã duyệt, chờ lên môi trường "
          "test. Để trống = không có cột. Vd Ready for Deploy."),
    Field("board_testing_state", "Cột: Ready for testing", "stateset", "🗂️ Cột trên Board",
          "Các state ADO hiện ở cột 'Ready for testing' trên board, ngay sau Ready for deploy — "
          "đã lên môi trường test và QC có thể kiểm tra. Liệt kê mọi state trong chặng của QC (vd "
          "Ready for Testing, In Testing, Ready for UAT, In UAT) để gộp vào một cột thay vì mỗi "
          "state một cột. Để trống = không có cột."),
    Field("done_states", "State coi là Done (→ cột Done)", "stateset", "🗂️ Cột trên Board",
          "Các state ADO được tính là Done trên board (vd Ready to Testing, Closed). Item do "
          "người chuyển sang bất kỳ state nào trong số này sẽ hiện ở cột Done."),
    Field("board_max_per_column", "Số thẻ tối đa / cột", "int", "🗂️ Cột trên Board",
          "Mỗi cột hiện tối đa bấy nhiêu thẻ, rồi có nút 'Tải thêm'. 0 = hiện hết."),
    Field("board_drop_map", "Kéo thả (cột => tag/state)", "list", "🗂️ Cột trên Board",
          "Bật kéo thả thẻ: mỗi dòng một 'Column => value'. Value là tag, hoặc state ADO nếu có "
          "tiền tố @. Vd 'In review => autopilot-review', 'Ready for deploy => @Ready for "
          "Deploy'. Để trống = không kéo thả được (board ghi rõ thay vì giả vờ). Ai đọc cột nào "
          "và cột nào đến lượt ai được cấu hình riêng tại /dashboard/board-views (Board "
          "processes)."),
    # ── 🚚 Delivery (PM view) ──
    Field("delivery_history_enabled", "Ghi lịch sử state", "bool", "🚚 Bàn giao (góc nhìn PM)",
          "Ghi lại mọi thay đổi state của work item để trang Delivery đo lead time, cycle time và "
          "biểu đồ flow. TẮT là dừng đồng hồ — lịch sử của khoảng thời gian đó KHÔNG THỂ khôi "
          "phục sau này."),
    Field("delivery_history_interval_minutes", "↳ Kiểm tra mỗi (phút)", "int",
          "🚚 Bàn giao (góc nhìn PM)",
          "Bao lâu kiểm tra thay đổi state một lần. Mỗi lần kiểm tra tốn hai lượt gọi API bất kể "
          "số item; chu kỳ không có gì thay đổi thì không ghi gì."),
    Field("delivery_history_retention_days", "↳ Giữ lịch sử (ngày)", "int",
          "🚚 Bàn giao (góc nhìn PM)",
          "Các lần chuyển state cũ hơn sẽ bị xoá. Đây cũng là giới hạn xa nhất mà mọi xu hướng "
          "trên trang có thể nhìn lại. 0 = giữ mãi."),
    Field("delivery_window_days", "Kỳ báo cáo mặc định (ngày)", "int", "🚚 Bàn giao (góc nhìn PM)",
          "Kỳ báo cáo mặc định khi mở trang Delivery. Mỗi số liệu được so với kỳ liền trước."),



    Field("delivery_max_items", "Số work item đọc mỗi lần", "int", "🚚 Bàn giao (góc nhìn PM)",
          "Item thay đổi gần nhất lấy trước. Item không thay đổi thì không thể đổi state, nên giá "
          "trị này chỉ để giới hạn chi phí."),
    Field("dashboard_public_url", "🔗 URL công khai của dashboard", "text",
          "🚚 Bàn giao (góc nhìn PM)",
          "Địa chỉ mà TRÌNH DUYỆT CỦA NGƯỜI ĐỌC truy cập được dashboard này, vd "
          "https://autopilot.example.com. Bản digest trên Teams dùng nó để link về trang "
          "Delivery. Để trống = không kèm link — digest được đọc trên điện thoại, và URL dựng từ "
          "địa chỉ bind (0.0.0.0) thì không ai mở được."),
    # ── Auto transitions ──
    Field("auto_transition_enabled", "Bật tự chuyển state", "bool", "🔀 Tự chuyển state",
          "Chuyển work item khi PR được merge, đánh dấu đã deploy khi build deploy thành công, và "
          "đẩy parent lên theo tiến độ của các con. Mỗi bước đặt state nào được cấu hình THEO "
          "TỪNG LOẠI WORK ITEM ở trang State flow."),
    Field("auto_transition_assignee", "Chỉ áp dụng cho assignee", "text", "🔀 Tự chuyển state",
          "Chỉ auto-transition các work item giao cho người này (chuỗi con của tên/email). Để "
          "trống = mọi assignee. Không ảnh hưởng việc xử lý task bình thường."),
    Field("on_publish_state", "Khi PR publish (draft → ready) → state (dự phòng)",
          "stateone", "🔀 Tự chuyển state",
          "State đặt khi tác giả chuyển PR RA KHỎI draft — lúc thật sự có người được nhờ xem. "
          "Thiếu nó, stage review phải gánh cả hai, nên item hiện 'ready for review' trong khi PR "
          "vẫn là draft. Để trống = stage publish không làm gì. Theo loại tại /dashboard/flow."),
    Field("on_merge_state", "Khi PR merge → state (dự phòng)", "stateone", "🔀 Tự chuyển state",
          "State đặt khi PR do autopilot mở được merge (đồng thời đánh dấu done). CHỈ dùng cho "
          "loại KHÔNG có flow nào bao — state ADO thuộc về một loại, nên một giá trị ở đây bị từ "
          "chối với mọi loại không có state đó. Cấu hình theo loại tại /dashboard/flow."),
    Field("parent_rollup_map", "Cha theo con (con = cha, dự phòng)", "list",
          "🔀 Tự chuyển state",
          "Mỗi dòng một 'Child state = Parent state', theo thứ tự tiến triển, vd 'Ready to "
          "Testing = Implement Done'. Parent đi theo con chậm nhất, và bị GIỮ LẠI trừ khi mọi "
          "state của con đều có dòng — nên map chỉ một dòng sẽ không bao giờ chạy. Roll-up theo "
          "loại nằm ở flow của parent tại /dashboard/flow."),
    Field("on_deploy_state", "Khi deploy thành công → state (dự phòng)", "stateone",
          "🔀 Tự chuyển state",
          "Khi build của deploy pipeline thành công, chuyển các item đang ở state merge sang "
          "state này. Để trống = tắt theo dõi deploy. Giá trị theo loại tại /dashboard/flow."),
    Field("deploy_pipeline_id", "ID pipeline deploy", "int", "🔀 Tự chuyển state",
          "ID build definition ADO của pipeline deploy. 0 = theo dõi mọi build thành công trên "
          "branch."),
    Field("deploy_branch", "Branch deploy", "text", "🔀 Tự chuyển state",
          "Branch mà build deploy chạy trên đó (trống = branch gốc)."),
    # ── Execution & Autonomy ──
    Field("execution_mode", "Chế độ chạy", "select", "⚙️ Thực thi & mức tự chủ",
          "interactive = mở một phiên Claude Remote-Control cho mỗi task, bạn có thể /rc vào để "
          "lái; headless = chạy SDK tự động (không ai gắn vào).",
          ("interactive", "headless")),
    Field("interactive_close_on", "↳ Đóng console khi", "select", "⚙️ Thực thi & mức tự chủ",
          "CLI là REPL: ghi kết quả xong rồi ngồi chờ mãi, nên cửa sổ không tự đóng. pr_closed = "
          "giữ nó (và worktree tạm) sống khi PR còn mở, để feedback review được xử lý trong CÙNG "
          "session, rồi đóng khi merge/abandon. result = đóng ngay khi task xong. never = để mọi "
          "console mở (sẽ chồng chất).",
          ("pr_closed", "result", "never")),
    Field("interactive_idle_timeout_minutes", "↳ Bỏ phiên im lặng sau (phút)",
          "int", "⚙️ Thực thi & mức tự chủ",
          "Session đang chạy mà không sinh ra gì trong khoảng này sẽ bị đóng và item được trả về "
          "board kèm lý do. Thiếu nó, một session bị treo (lệnh MCP không bao giờ trả về, console "
          "đã chết) giữ item mãi — luồng headless luôn có 'Task timeout', đây là bản tương ứng. "
          "Tính thời gian IM LẶNG, không phải thời gian chạy, nên việc dài vẫn an toàn; nên để "
          "rộng nếu bạn điều khiển session bằng tay. Worktree tạm được giữ lại, nên bấm ▶ Run sẽ "
          "chạy tiếp. 0 = không giới hạn (không khuyến nghị)."),
    Field("interactive_resume_on_rework", "↳ Tiếp tục phiên cũ khi làm lại", "bool",
          "⚙️ Thực thi & mức tự chủ",
          "Feedback trên PR chạy trong chính worktree của session đó và TIẾP TỤC hội thoại của "
          "nó, thay vì worktree mới và đọc lại codebase từ đầu. Claude Code lưu transcript theo "
          "thư mục, nên chạy ở chỗ khác là mất context."),
    Field("interactive_bypass_permissions", "↳ ⚠️ Bỏ qua hỏi quyền (bypassPermissions)",
          "bool", "⚙️ Thực thi & mức tự chủ",
          "Session không bao giờ dừng hỏi trước khi gọi Bash/MCP, nên vẫn chạy tiếp khi không ai "
          "attach. Cái giá: brief được dựng từ nội dung work item, nên một prompt injection trong "
          "ticket có thể chạy bất kỳ lệnh nào trên MÁY NÀY mà không hỏi. Tắt = session dùng "
          "Permission mode như mọi lần chạy, và chờ người attach."),
    Field("autonomy_level", "Mức tự chủ", "select", "⚙️ Thực thi & mức tự chủ",
          "report = chỉ comment, assisted = draft PR, unattended = tự mở PR.",
          ("report", "assisted", "unattended")),
    Field("claude_model", "Model Claude", "select", "⚙️ Thực thi & mức tự chủ",
          "Model CLI dùng để chạy từng task. Để trống = mặc định của CLI đi kèm — KHÔNG đảm bảo "
          "giữ nguyên qua các lần cập nhật CLI. Chọn rõ một model để chi phí/tốc độ/chất lượng ổn "
          "định.",
          ("", "sonnet", "opus", "fable", "haiku")),
    Field("use_worktrees", "Tách riêng từng task (git worktree)", "bool",
          "⚙️ Thực thi & mức tự chủ",
          "Chạy mỗi task trong git worktree riêng để các task chạy song song không đụng vào "
          "checkout chính của bạn. Tắt để chạy tại chỗ trong workspace dùng chung."),
    Field("max_concurrent", "Số task chạy song song", "int", "⚙️ Thực thi & mức tự chủ",
          "Cần khởi động lại mới có hiệu lực."),
    Field("task_timeout_minutes", "Thời gian tối đa / task (phút)", "int",
          "⚙️ Thực thi & mức tự chủ"),
    Field("claude_effort_task", "⚡ Effort — chạy task", "select", "⚙️ Thực thi & mức tự chủ",
          "Mức suy luận của model khi làm code thật. Để trống = mặc định của model. Nâng lên "
          "xhigh/max cho refactor khó; chỉ HẠ xuống sau khi đã kiểm tra chất lượng trên chính "
          "việc của bạn, vì đây là luồng viết code.",
          ("", "low", "medium", "high", "xhigh", "max")),
    Field("claude_effort_agentic", "⚡ Effort — chat agentic", "select", "⚙️ Thực thi & mức tự chủ",
          "Lượt agent trên Teams: tra cứu ADO thật, nhưng trả lời chat chứ không sửa code. medium "
          "giữ chất lượng với độ trễ chỉ bằng một phần nhỏ, trong khi có người đang chờ.",
          ("", "low", "medium", "high", "xhigh", "max")),
    Field("claude_effort_chat", "⚡ Effort — chat ngắn", "select", "⚙️ Thực thi & mức tự chủ",
          "Phân loại ý định, diễn đạt lại danh sách đã tra, viết một tin nhắn persona, chọn lệnh "
          "cho @mention. Các việc này chỉ chọn hoặc diễn đạt lại — không suy luận về code — nên "
          "low gần như không rủi ro và nhanh hơn rõ rệt.",
          ("", "low", "medium", "high", "xhigh", "max")),
    Field("use_specialized_agents", "🧩 Chuyển lệnh cho agent chuyên trách", "bool",
          "⚙️ Thực thi & mức tự chủ",
          "Gửi /spec /qc /security /review /test /impact tới các subagent chuyên dụng "
          "(.claude/agents) để có kết quả chuyên sâu. Nếu thiếu thì lùi về skill chung."),
    Field("reuse_claude_session", "🧠 Dùng lại phiên Claude theo branch", "bool",
          "⚙️ Thực thi & mức tự chủ",
          "Tiếp tục hội thoại của agent theo từng branch qua các vòng revise — lượt sau giữ "
          "context cũ (rẻ hơn, nhất quán hơn). Nếu resume lỗi thì chạy mới."),
    Field("claude_session_ttl_hours", "↳ Thời hạn dùng lại phiên (giờ)", "int",
          "⚙️ Thực thi & mức tự chủ",
          "Chỉ dùng lại phiên còn mới hơn mức này; cũ hơn → bắt đầu mới. Mặc định 24."),
    Field("dry_run", "Chạy thử (dry run)", "bool", "⚙️ Thực thi & mức tự chủ",
          "Chỉ ghi log — không bao giờ chạy hay ghi lên ADO."),
    # ── 🛡️ Guardrails & policy ──
    Field("policy_protected_paths", "🛡️ Đường dẫn được bảo vệ (không bao giờ sửa)", "list",
          "🛡️ Rào chắn & chính sách",
          "Các glob pattern autopilot KHÔNG BAO GIỜ được sửa — mỗi dòng một, vd 'k8s/*', "
          "'.github/*', '*.env', 'Dockerfile'. Lần chạy đụng vào bất kỳ file nào trong đó bị chặn "
          "trước khi mở PR. Để trống = tắt."),
    Field("policy_max_files_changed", "🛡️ Số file sửa tối đa / lần chạy", "int",
          "🛡️ Rào chắn & chính sách",
          "Giới hạn phạm vi ảnh hưởng: chặn lần chạy sửa nhiều file hơn số này (một 'small fix' "
          "viết lại nửa repo cần người xem). 0 = tắt."),
    # ── 🧪 Quality gates ──
    Field("auto_review_enabled", "Tự review bảo mật", "bool", "🧪 Cổng chất lượng"),
    Field("learning_loop_enabled", "🧠 Vòng học hỏi", "bool", "🧪 Cổng chất lượng",
          "Ghi nhớ những gì auto-review đã bắt lỗi theo từng repo và đưa bài học gần đây vào "
          "brief của lần chạy sau, để agent không lặp lại. Tắt = brief giữ nguyên. Xem những gì "
          "đã học ở trang Learning."),
    Field("lessons_max_injected", "↳ Số bài học đưa vào / lần chạy", "int", "🧪 Cổng chất lượng",
          "Bao nhiêu bài học gần nhất được đưa vào brief. Mặc định 8. 0 = vẫn ghi nhận nhưng "
          "không đưa vào."),
    Field("test_gate_enabled", "🧪 Cổng test tự động", "bool", "🧪 Cổng chất lượng",
          "Chạy bộ test của repo trong worktree trước khi mở PR; test đỏ sẽ chặn PR và hạ điểm "
          "lần chạy. Tắt = không chạy test."),
    Field("test_gate_block_when_not_run", "↳ Chặn PR khi không chạy được test",
          "bool", "🧪 Cổng chất lượng",
          "Runner không khởi động được (thiếu tool, môi trường chưa sẵn sàng) thì không nói lên "
          "gì về thay đổi. Tắt = PR vẫn đi tiếp, gắn nhãn 'tests not run'. Bật = bị chặn như test "
          "đỏ."),
    Field("test_commands", "↳ Lệnh test theo từng repo", "list", "🧪 Cổng chất lượng",
          "Mỗi dòng một repo, <code>Repo = command</code>. Project có nhiều stack không thể dùng "
          "chung một lệnh — đặt dotnet test thì mọi thay đổi frontend bị kiểm bằng sai runner, "
          "đặt npm test thì mọi thay đổi backend cũng vậy. Repo không có dòng ở đây sẽ dùng lệnh "
          "bên dưới, rồi tới tự phát hiện.",
          placeholder="Backend-Fresh = dotnet test --nologo"),
    Field("test_timeouts", "↳ Timeout test theo từng repo (giây)", "list", "🧪 Cổng chất lượng",
          "Mỗi dòng một repo, <code>Repo = seconds</code>. Solution .NET restore và build từ "
          "worktree mới tốn gấp nhiều lần một lượt unit test frontend; dùng một con số cho cả hai "
          "thì hoặc backend bị timeout, hoặc frontend treo cả 15 phút mới có người biết. Timeout "
          "sẽ CHẶN PR.",
          placeholder="Backend-Fresh = 1800"),
    Field("test_command", "↳ Lệnh test (dự phòng cho mọi repo)", "text", "🧪 Cổng chất lượng",
          "Lệnh chạy test (trong worktree của repo). Để trống = tự phát hiện (pytest / dotnet "
          "test / npm test); không tìm thấy runner = bỏ qua, không bao giờ chặn."),
    Field("test_timeout_seconds", "↳ Timeout test (giây)", "int", "🧪 Cổng chất lượng",
          "Dừng test sau khoảng này và coi là thất bại. Mặc định 600."),
    Field("test_install_dependencies", "↳ Cài dependency Node trước", "bool",
          "🧪 Cổng chất lượng",
          "Worktree mới chưa có node_modules, nên `npm test` tự phát hiện sẽ không tìm thấy `ng` "
          "/ `jest`. Bật = chạy `npm ci` (rồi `npm install` nếu lock lệch) trước bộ test, ~2 phút "
          "với monorepo lớn. Không cài được = bỏ qua kèm lý do — máy không chạy được test sẽ "
          "không bao giờ bị báo là test fail."),
    Field("pr_scoring_enabled", "Chấm điểm mỗi lần chạy (0–100)", "bool", "🧪 Cổng chất lượng",
          "Chấm điểm mỗi lần chạy từ tín hiệu khách quan; dưới ngưỡng review → giữ chờ người."),
    Field("pr_score_auto_min", "Điểm ≥ mức này → tự resolve", "int", "🧪 Cổng chất lượng",
          "Chỉ áp dụng ở mức tự chủ unattended. Mặc định 85."),
    Field("pr_score_review_min", "Điểm < mức này → chuyển người", "int", "🧪 Cổng chất lượng",
          "Dưới mức này lần chạy bị giữ chờ người thay vì review/xong. Mặc định 60."),
    # ── 🔁 PR review & feedback ──
    Field("feedback_loop_enabled", "🔁 Vòng phản hồi PR", "bool", "🔁 Review PR & phản hồi",
          "Theo dõi các PR autopilot đang mở để bắt comment review mới của người và tự revise "
          "branch để xử lý. Cần khởi động lại để có hiệu lực."),
    Field("max_revisions", "↳ Số lần sửa PR tối đa / item", "int", "🔁 Review PR & phản hồi",
          "Giới hạn số lần tự sửa mỗi work item để vòng review qua lại không chạy mãi. Mặc định "
          "3."),
    Field("pr_add_assignee_as_reviewer", "🧑‍⚖️ Thêm assignee làm reviewer PR", "bool",
          "🔁 Review PR & phản hồi",
          "Khi autopilot mở PR cho một work item, thêm ASSIGNEE của item đó làm reviewer (ADO sẽ "
          "báo cho họ). Làm hết sức có thể — không bao giờ làm fail lần chạy."),
    Field("pr_extra_reviewer_ids", "↳ Reviewer thêm (identity ID)", "list",
          "🔁 Review PR & phản hồi",
          "Thêm vào mọi PR, ngoài assignee. Là GUID định danh ADO, mỗi dòng một — API reviewer "
          "dùng id, không dùng email."),
    Field("pr_reviewers_required", "↳ Đánh dấu là bắt buộc", "bool",
          "🔁 Review PR & phản hồi",
          "Reviewer bắt buộc chặn hoàn tất PR tới khi họ vote. Tắt = tuỳ chọn (chỉ được thông "
          "báo)."),
    Field("pr_reviewer_tracking_enabled", "👀 Theo dõi reviewer PR", "bool",
          "🔁 Review PR & phản hồi",
          "Theo dõi danh sách reviewer trên MỌI PR đang mở: trạng thái trên dashboard, tự review "
          "khi bot được thêm làm reviewer, nhắc nhẹ khi quá hạn. Cần khởi động lại."),
    Field("pr_auto_review_on_added", "↳ Tự review khi bot được thêm", "bool",
          "🔁 Review PR & phản hồi",
          "Bot được thêm làm reviewer PR → AI review có cấu trúc + vote. Tự bật lại khi có "
          "commit mới."),


    Field("pr_conflict_tracking_enabled", "⚔️ Theo dõi conflict merge PR", "bool",
          "🔁 Review PR & phản hồi",
          "Phát hiện các PR đang mở bị ADO báo conflict: một comment PR + một thông báo mỗi đợt, "
          "trang Conflicts, và một dòng trong báo cáo delivery. Chỉ đọc. Cần khởi động lại."),
    Field("pr_conflict_autoresolve", "↳ Tự gỡ conflict trên PR của autopilot", "bool",
          "🔁 Review PR & phản hồi",
          "Trên PR từ branch của bot: merge target vào (không bao giờ rebase / force-push), để "
          "agent xử lý các hunk, và CHỈ push nếu không còn marker, không đụng file nào khác, và "
          "test + cổng bảo mật đều qua. Ngược lại thì huỷ và hỏi người."),
    Field("pr_conflict_command", "↳ Lệnh yêu cầu gỡ conflict", "text", "🔁 Review PR & phản hồi",
          "Comment trên PR để yêu cầu gỡ conflict trên BẤT KỲ PR nào (người trong danh sách). "
          "Trống = tắt."),
    Field("pr_conflict_max_files", "↳ Số file conflict tối đa", "int", "🔁 Review PR & phản hồi",
          "Nhiều file conflict hơn mức này là va chạm cấu trúc — chuyển người mà không tốn "
          "token nào."),
    Field("pr_conflict_max_attempts", "↳ Số lần thử / commit đích", "int",
          "🔁 Review PR & phản hồi",
          "Số lần thử tự động với CÙNG commit target (cùng đầu vào → cùng conflict). Có push mới "
          "lên target, hoặc có người yêu cầu, thì được thử thêm."),
    Field("pr_conflict_allow_preexisting_failures", "↳ Vẫn push khi branch đích đã đỏ",
          "bool", "🔁 Review PR & phản hồi",
          "Test đỏ sau khi giải conflict → branch target được test riêng và so sánh các lỗi. Bật "
          "= push khi bản giải KHÔNG thêm lỗi nào của chính nó (PR vẫn thừa hưởng phần đỏ của "
          "target). Tắt = escalate, chỉ rõ target là nguyên nhân. Lỗi do bản giải THÊM VÀO luôn "
          "bị chặn."),
    Field("pr_conflict_poll_minutes", "↳ Quét mỗi (phút)", "int", "🔁 Review PR & phản hồi",
          "Bao lâu kiểm tra conflict trên các PR đang mở một lần."),
    Field("pr_session_hours", "Giới hạn phiên PR tương tác (giờ)", "int",
          "🔁 Review PR & phản hồi",
          "Với execution_mode interactive, việc giải conflict và lệnh /ai trên PR mở một session "
          "Remote-Control để bạn attach. Sau khoảng này mà chưa có kết quả thì session bị đóng "
          "(branch giữ nguyên) và báo cho người."),
    Field("pr_advisory_max_per_commit", "↳ Số lần review góp ý tối đa / commit", "int",
          "🔁 Review PR & phản hồi",
          "Số lần /review (và các lệnh chỉ comment khác) được chạy trên CÙNG một commit. Review "
          "lại code không đổi chỉ lặp lại; push commit mới để đặt lại. 0 = không giới hạn."),
    Field("pr_auto_review_max_per_pr", "↳ Số lần tự review tối đa / PR", "int",
          "🔁 Review PR & phản hồi",
          "Giới hạn tổng số lần auto-review cho một PR. Auto-review kích hoạt lại sau mỗi commit "
          "mới, nên PR push nhiều có thể bị review rất nhiều lần. 0 = không giới hạn."),
    Field("pr_review_max_concurrent", "↳ Số review PR song song tối đa", "int",
          "🔁 Review PR & phản hồi",
          "Giới hạn song song cho việc review PR, tách khỏi Max concurrent để một loạt PR không "
          "chiếm hết chỗ chạy task. 0 = dùng chung Max concurrent."),
    Field("pr_bot_identity", "↳ Ghi đè identity của bot", "text", "🔁 Review PR & phản hồi",
          "Email / uniqueName của tài khoản bot reviewer. Trống = tự nhận identity của PAT qua "
          "connectionData."),
    Field("pr_reviewer_target_branches", "↳ Chỉ các branch đích này", "list",
          "🔁 Review PR & phản hồi",
          "Mỗi dòng một branch (vd dxfac/development). CHỈ PR merge VÀO các branch này mới được "
          "theo dõi / review / hiển thị. Để trống = mọi target."),
    Field("comment_reprocess_enabled", "💬 Phản hồi comment trên work item", "bool",
          "🔁 Review PR & phản hồi",
          "Comment mới của người trên item autopilot đang giữ (held / in review / done) sẽ chạy "
          "lại item với comment đó là chỉ dẫn ưu tiên cao nhất — không cần tag restart."),
    Field("max_comment_rounds", "↳ Số vòng comment tối đa / item", "int", "🔁 Review PR & phản hồi",
          "Giới hạn số vòng comment người↔bot mỗi item để không qua lại mãi. Mặc định 5."),
    Field("pr_commands_on_any_pr", "↳ …kể cả khi bot không phải reviewer", "bool",
          "🔁 Review PR & phản hồi",
          "Trên PR không do autopilot mở, bình thường nó chỉ trả lời ở nơi được THÊM LÀM REVIEWER "
          "— lời mời đó là sự đồng ý. Bật để coi việc được nhắc tên trong comment là sự đồng ý, "
          "khỏi phải thêm bot trước. Vẫn chỉ người trong danh sách ra lệnh mới ra lệnh được. Tốn "
          "API: mỗi chu kỳ đọc mọi PR đang mở trong phạm vi, không chỉ PR có bot."),
    Field("comment_mention_enabled", "↳ Trả lời @mention trên PR", "bool",
          "🔁 Review PR & phản hồi",
          "Coi @mention bot trên pull request là đang gọi bot, không cần /command — như cách "
          "người ta tự nhiên nhờ đồng đội. Ý định được suy ra thành một trong các /command và mặc "
          "định là ADVISORY, nên mention mơ hồ không bao giờ thành sửa code và push."),
    # ── Dependency scheduling ──
    Field("dependency_scheduling_enabled", "Xếp thứ tự theo link", "bool",
          "🔗 Xếp lịch theo phụ thuộc",
          "Chờ link Predecessor, không bao giờ chạy cùng lúc các item Related (0 token). Tắt = "
          "theo thứ tự ưu tiên."),
    Field("sibling_conflict_scheduling", "Coi item anh em là xung đột mềm", "bool",
          "🔗 Xếp lịch theo phụ thuộc",
          "Coi các item cùng Parent + cùng loại là xung đột mềm dù không có link."),
    Field("scheduler_max_dispatch", "Số item phát tối đa / chu kỳ", "int",
          "🔗 Xếp lịch theo phụ thuộc",
          "Giới hạn số item đánh dấu sẵn sàng mỗi chu kỳ quét. 0 = không giới hạn "
          "(max_concurrent vẫn điều tiết)."),
    Field("scheduler_use_ai_conflicts", "Dùng xung đột do AI tìm ra", "bool",
          "🔗 Xếp lịch theo phụ thuộc",
          "Đưa các xung đột ẩn mà Phân tích (Lập kế hoạch) đã xác nhận vào xếp lịch như xung "
          "đột mềm, để poller không chạy các item đó cùng lúc."),
    Field("scheduler_ai_conflict_min_score", "Điểm tối thiểu của xung đột AI", "int",
          "🔗 Xếp lịch theo phụ thuộc",
          "Chỉ xung đột AI có điểm từ mức này (0–100) mới ảnh hưởng xếp lịch. Mặc định 60."),
    Field("scheduler_history_limit", "Số quyết định giữ lại", "int", "🔗 Xếp lịch theo phụ thuộc",
          "Số quyết định lập lịch gần đây (những lần giữ việc lại) được lưu cho panel lịch sử "
          "Planning. 0 = chỉ giữ chế độ xem trực tiếp."),
    Field("batch_related_enabled", "Gộp các item có link", "bool", "🔗 Xếp lịch theo phụ thuộc",
          "Chạy một cụm có liên kết (chuỗi Related / Predecessor) trong MỘT lần chạy agent, mở "
          "một branch + một PR cho mỗi work item, thay vì tách thành các đợt riêng. Chỉ chế độ "
          "agent headless."),
    Field("batch_max_items", "Số item tối đa / lô", "int", "🔗 Xếp lịch theo phụ thuộc",
          "Cụm lớn nhất được gộp. Cụm lớn hơn sẽ phát từng item một. Mặc định 3."),
    Field("batch_stacked_prs", "Xếp chồng PR trong lô", "bool", "🔗 Xếp lịch theo phụ thuộc",
          "Branch của item 2 tách từ item 1 và nhắm vào nó (không conflict, thứ tự merge cố "
          "định). Tắt = mọi branch cắt từ branch gốc (merge thứ tự nào cũng được, có thể "
          "conflict)."),
    # ── Closed-loop SDLC (v2) ──
    # Ordered as the three questions the engine asks, in the order it asks them:
    # is the loop on, WHICH stages run, how much rework is allowed, and WHERE the
    # item goes when they finish. The two hand-off maps sit together, under the
    # switch that decides whether a draft PR delays them.
    Field("sdlc_loop_enabled", "Bật vòng SDLC", "bool", "♾️ Vòng SDLC khép kín (v2)",
          "Đưa item qua các stage SDLC do profile chọn (gate + revise + escalate + handoff). Tắt "
          "= chạy một lần như cũ, không đổi. Chỉ headless."),
    # — which stages run, most specific first (this is the resolution order) —
    Field("sdlc_profile", "Profile: cố định cho máy này", "select", "♾️ Vòng SDLC khép kín (v2)",
          "Role mà máy NÀY chạy, bất kể item là gì. Để trống = quyết định theo từng item, bên "
          "dưới. Thứ tự ưu tiên: tag 'sdlc:<profile>' của chính item (nút ▶ Run trên Board đặt "
          "tag này) → field này → map theo loại → mặc định.",
          ("", "ba", "dev", "qc", "review", "design", "full")),
    Field("sdlc_type_profiles", "↳ Profile: theo loại work item", "map",
          "♾️ Vòng SDLC khép kín (v2)",
          "Mỗi dòng một 'work-item type => profile'. Nhờ đó Bug chạy trọn từ đầu đến cuối trong "
          "khi Requirement dừng chờ người: 'Bug => full', 'User Story => ba'.",
          placeholder="Bug => full"),
    Field("sdlc_default_profile", "↳ Profile: mặc định", "select", "♾️ Vòng SDLC khép kín (v2)",
          "Dùng khi không có gì ở trên áp dụng — không có tag trên item, không cố định profile, "
          "không có dòng theo loại.",
          ("full", "dev", "ba", "qc", "review", "design")),
    # — how much rework the engine may do before it asks a person —
    Field("sdlc_interactive_gate", "Cổng test sau phiên tương tác", "bool",
          "♾️ Vòng SDLC khép kín (v2)",
          "Chế độ interactive + bật relay: khi session kết thúc, chạy cổng test trên branch của "
          "nó. Đỏ → item được giữ lại cho người xử lý (test fail ghi trong comment) thay vì "
          "chuyển cho role kế tiếp. Theo test_gate_enabled."),
    Field("sdlc_max_iterations", "Số vòng sửa tối đa", "int", "♾️ Vòng SDLC khép kín (v2)",
          "Ngân sách chung cho mọi stage của một item trước khi chuyển người. Mặc định 3."),
    # — where the item goes when the profile finishes —
    Field("sdlc_advance_on_draft", "Chuyển tiếp cả khi PR còn draft", "bool",
          "♾️ Vòng SDLC khép kín (v2)",
          "Áp dụng các bước bàn giao bên dưới kể cả khi PR vẫn là draft. Tắt (khuyến nghị) = "
          "draft chờ người review trước khi gọi role kế tiếp."),
    # Hand-off used to be edited here, keyed by profile, under a heading about a loop
    # that does not gate it — it applies with the loop OFF. It now lives beside the
    # door it feeds, on /dashboard/roles, because one role's way out IS the next
    # role's way in and no page ever showed those two facts together.
    # ── 🧪 QC test cases ──
    # A QC run's two outputs: a file in the repo, and a work item in ADO. Both existed
    # only in config.yaml, which is how the two complaints that produced them — test
    # cases scattered somewhere new every run, and "I cannot see the test cases on the
    # work item" — were unanswerable from a screen.
    Field("qc_test_case_path", "Test case → đường dẫn trong workspace", "text",
          "🧾 Test case QC",
          "Nơi lần chạy QC ghi các test case đã viết, tính từ gốc WORKSPACE — KHÔNG nằm trong "
          "repo, nên không bao giờ lọt vào pull request. '{id}' là id của work item. Lựa chọn này "
          "chỉ có ích nếu LẦN NÀO CŨNG GIỐNG NHAU — để trống là không nói gì và để agent tự chọn, "
          "chính là lý do chúng bị rải rác. Mặc định 'qc/{id}'. ⚠️ File ở đây nằm ngoài git và "
          "chỉ có trên máy này: hãy BẬT thiết lập bên dưới, nếu không ngoài server này không ai "
          "thấy các case."),
    Field("qc_create_test_case_items", "Tạo thêm work item Test Case cho mỗi case", "bool",
          "🧾 Test case QC",
          "Mỗi case một ADO Test Case, liên kết với item nó kiểm, các bước nằm ở tab Test. Đây là "
          "thứ QC thật sự dùng — và khi đường dẫn ở trên nằm ngoài mọi repo, đây là bản DUY NHẤT "
          "rời khỏi máy này. Tắt = chỉ có file trong workspace."),
    Field("qc_create_bug_items", "Tạo Bug cho mỗi case FAIL", "bool",
          "🧾 Test case QC",
          "Mỗi case fail một ADO Bug, liên kết với item QC đang chạy (link child, lùi về Related "
          "nếu process template không cho phép). Chạy lại không tạo trùng: case đã có Bug liên "
          "kết thì bỏ qua. Mặc định tắt — case fail không phải lúc nào cũng là lỗi sản phẩm, "
          "thường là test sai hoặc môi trường hỏng, và các Bug đó rơi vào board dùng chung. Bật "
          "khi quy trình yêu cầu truy vết Requirement → TC + Bug."),
    # ── Planning workbench ──
    Field("planning_ai_analysis", "Phân tích xung đột bằng AI", "bool", "🧭 Lập kế hoạch",
          "Thao tác Analyze chạy các Claude judge có giới hạn trên các cặp trùng từ khoá (tốn "
          "token). Tắt = chỉ nhóm theo đồ thị liên kết (0 token)."),
    Field("planning_ai_max_pairs", "Số cặp AI xét tối đa / lần phân tích", "int", "🧭 Lập kế hoạch",
          "Giới hạn số cặp nghi ngờ được AI xét mỗi lần bấm Phân tích. Mặc định 6."),
    Field("planning_ai_min_score", "Điểm AI tối thiểu để cảnh báo", "int", "🧭 Lập kế hoạch",
          "Kết luận của AI phải đạt ít nhất mức này (0–100) mới hiện ra. Mặc định 50."),
    Field("planning_ai_timeout_seconds", "Timeout mỗi lượt AI (giây)", "int", "🧭 Lập kế hoạch",
          "Timeout Claude cho mỗi lượt xét khi Phân tích. Mặc định 120."),
    Field("conflict_ai_min_token_len", "Độ dài từ khoá tối thiểu", "int", "🧭 Lập kế hoạch",
          "Từ khoá ngắn nhất bộ lọc xét khi ghép cặp item. Mặc định 4."),
    Field("conflict_ai_extra_stopwords", "Từ bỏ qua bổ sung", "list", "🧭 Lập kế hoạch",
          "Từ nhiễu riêng của dự án, bỏ qua khi so khớp từ khoá (phẩy/xuống dòng)."),
    Field("planning_schedule_default_hour", "Giờ hẹn mặc định", "int", "🧭 Lập kế hoạch",
          "Giờ (0–23, giờ địa phương) điền sẵn trong ô hẹn giờ. Mặc định 21."),
    Field("planning_load_limit", "Số item tải tối đa", "int", "🧭 Lập kế hoạch",
          "Số work item tối đa nút Tải lấy về cho một assignee. Mặc định 200."),
    Field("planning_live_refresh_seconds", "Làm mới lịch trực tiếp (giây)", "int",
          "🧭 Lập kế hoạch",
          "Tự làm mới khung Lịch trực tiếp (chỉ đọc) mỗi N giây. 0 = tắt."),
    Field("planning_start_state", "Bắt đầu → state", "stateone", "🧭 Lập kế hoạch",
          "State mà thao tác Start chuyển item sang (để poller nhận) nếu nó chưa ở state kích "
          "hoạt. Để trống = state kích hoạt đầu tiên."),
    # ── Working hours & quiet time ──
    Field("timezone", "Múi giờ", "text", "🕘 Giờ làm việc",
          "Múi giờ IANA dùng để đọc mọi khung giờ bên dưới, vd Asia/Ho_Chi_Minh. BẮT BUỘC cho giờ "
          "yên lặng (thiếu nó thì giờ yên lặng luôn tắt). Đừng dựa vào đồng hồ máy: server hay "
          "container thường chạy UTC, nên '18:00' ở đó là 01:00 với team ở UTC+7."),
    Field("schedule_start", "Giờ làm việc — từ", "text", "🕘 Giờ làm việc",
          "HH:MM. Khi nào một lần chạy được BẮT ĐẦU. Ngoài khung này poller nghỉ; việc đang chạy "
          "vẫn tiếp tục. Để trống = không giới hạn (chạy bất kỳ giờ nào)."),
    Field("schedule_end", "Giờ làm việc — đến", "text", "🕘 Giờ làm việc",
          "HH:MM. Giờ kết thúc SỚM hơn giờ bắt đầu nghĩa là khung qua đêm (22:00–06:00)."),
    Field("schedule_days", "Giờ làm việc — ngày", "text", "🕘 Giờ làm việc",
          "vd Mon,Tue,Wed,Thu,Fri. Trống = mọi ngày."),





    # ── Spec drift & PR traceability ──
    Field("spec_drift_enabled", "Báo lệch spec", "bool", "📐 Lệch spec & truy vết PR",
          "Agent được dặn TỰ QUYẾT thay vì hỏi, nên mỗi chỗ mơ hồ nó giải quyết là một quyết định "
          "thay cho team mà work item không phản ánh. Khi bật, các quyết định đó được ghi thành "
          "comment ⚠️ SPEC-DRIFT, một tag, một comment PR, và một dòng trên /dashboard/specs để "
          "BA đánh dấu."),
    Field("spec_drift_sla_days", "↳ Hạn quyết lệch spec (ngày)", "int",
          "📐 Lệch spec & truy vết PR",
          "Điểm lệch spec chưa được BA quyết quá số ngày này hiện đỏ trên trang Lệch spec "
          "(quá nửa hạn: vàng).",
          show_when_key="spec_drift_enabled", show_when_values=("1",)),
    Field("spec_drift_tag", "↳ Tag gắn tới khi spec được cập nhật", "text",
          "📐 Lệch spec & truy vết PR",
          "Gắn lên item tới khi có người đánh dấu spec đã khớp lại."),
    Field("spec_drift_holds_item", "↳ Giữ item lại chờ người", "bool",
          "📐 Lệch spec & truy vết PR",
          "Mặc định tắt: drift là nợ tài liệu, không phải lý do dừng giao hàng. Bật để chặn item "
          "tiến lên khi spec của nó đã lỗi thời."),
    Field("pr_require_work_item_link", "Mỗi PR phải gắn work item", "bool",
          "📐 Lệch spec & truy vết PR",
          "Kiểm tra lại với ADO sau khi tạo, và tự gắn nếu thiếu. Một chỉ dẫn trong brief chỉ là "
          "lời khuyên mà model có thể bỏ qua khi chạy lâu — và khi đó, không ai phát hiện."),

    # ── Process health ──
    Field("process_health_enabled", "Báo cáo sức khoẻ quy trình", "bool", "🩺 Sức khoẻ quy trình",
          "Tính các đợt rà soát định kỳ mà tài liệu quy trình giao — tỷ lệ năng lực dành cho việc "
          "ngoài kế hoạch, lỗi lọt theo module, item bị kẹt, tag bị mục — và đẩy kết quả lên các "
          "kênh thông báo. Chỉ đọc: chỉ báo cáo, không bao giờ sửa."),
    Field("process_health_interval_hours", "↳ Mỗi (giờ)", "int", "🩺 Sức khoẻ quy trình",
          "168 = hằng tuần. 0 = chỉ tính khi được yêu cầu."),
    Field("process_health_window_days", "↳ Khoảng đo (ngày)", "int", "🩺 Sức khoẻ quy trình",
          "Mỗi lần đo nhìn lại bao xa. 14 = một sprint."),
    Field("process_health_blocked_days", "↳ Bị chặn lâu hơn (ngày)", "int",
          "🩺 Sức khoẻ quy trình",
          "Item bị gắn Blocked và không ai động tới lâu như vậy sẽ vào danh sách cần theo dõi."),
    Field("process_health_adhoc_threshold_pct", "↳ Ngưỡng cảnh báo việc phát sinh (%)", "float",
          "🩺 Sức khoẻ quy trình",
          "Cảnh báo khi việc ngoài kế hoạch vượt tỷ lệ này. Bỏ qua khi mẫu nhỏ — một ticket "
          "ad-hoc trong tuần vắng là 100% và chẳng nói lên gì."),

    # ── Notifications ──
    # Neither `teams_webhook_url` nor `teams_webhook_urls` is a Field. Both are edited as
    # rows of the "📣 Teams channels" card, which renders INSIDE this section — one list, in
    # one place. Having a single-URL field here as well as the card meant two controls for
    # the same setting, in two parts of the page, with no way to tell which one applied.
    #
    # Keeping them out of FIELDS is also what protects them: parse_form emits a key for every
    # Field, so a `list` field would have parsed an untouched empty textarea as [] and wiped
    # an existing multi-channel setup on the first save of any unrelated setting. They stay
    # honoured by `teams_webhook_targets`, seed the card, and are absorbed into it on save.







    # ── 🔔 Cảnh báo (alerts) ──
    # Everything that decides WHETHER a human is interrupted lives here, in one
    # place. It used to be spread over five sections — thresholds under Delivery,
    # the digest under the Teams bot, quiet hours under Working hours, reviewer
    # nudges under PR review — while "Notifications" held only credentials, so the
    # page could be read end to end without ever finding the alert settings.
    Field("alert_events", "Gửi cảnh báo cho sự kiện nào", "text", "🔔 Cảnh báo — khi nào báo",
          "Danh sách sự kiện, cách nhau bởi dấu phẩy: started, completed, failed, error, "
          "reminder, digest. Bỏ trống = gửi tất cả. Mặc định KHÔNG có 'started' — tin "
          "'bot vừa nhận việc #123' không giúp ai quyết định gì, nhưng nhân đôi số tin."),
    Field("alert_min_severity", "Mức tối thiểu để gửi", "select", "🔔 Cảnh báo — khi nào báo",
          "Lọc sau danh sách sự kiện. info = gửi mọi thứ đã bật · warning = chỉ việc cần "
          "xem trong ngày (chạy lỗi, reviewer chưa vote) · critical = chỉ việc đang bị chặn.",
          ("info", "warning", "critical")),
    Field("delivery_merge_hours", "Ngưỡng: PR đã duyệt mà chưa merge (giờ)", "int",
          "🔔 Cảnh báo — khi nào báo",
          "Cảnh báo PR đã duyệt, không bị chặn mà VẪN chưa merge sau khoảng này. Đây là lãng phí "
          "thuần — việc đã xong và chỉ cần một cú click."),
    Field("delivery_review_hours", "Ngưỡng: PR chưa ai review (giờ)", "int",
          "🔔 Cảnh báo — khi nào báo",
          "Báo PR chưa ai vote sau khoảng này, kèm tên các reviewer đang được chờ."),
    Field("delivery_stale_days", "Ngưỡng: việc đứng im (ngày)", "int",
          "🔔 Cảnh báo — khi nào báo",
          "Cảnh báo item đang làm mà STATE không đổi trong khoảng này. Sửa nội dung và comment "
          "không tính là tiến triển — đó chính là mục đích."),
    Field("delivery_max_age_days", "Ngưỡng: bỏ qua việc chờ quá lâu (ngày)", "int",
          "🔔 Cảnh báo — khi nào báo",
          "Việc đã chờ lâu hơn bấy nhiêu ngày coi như tồn đọng, không nêu trong digest nữa "
          "— chỉ hiện số lượng đã ẩn. 0 = liệt kê hết, dù cũ tới đâu."),
    Field("alert_dedup_enabled", "Không lặp lại cảnh báo đã báo", "bool",
          "🔔 Cảnh báo — khi nào báo",
          "Một việc đã báo sẽ chỉ nhắc lại khi NẶNG THÊM (thời gian chờ tăng gấp đôi) hoặc "
          "sau số giờ dưới đây. Tắt = mọi việc quá ngưỡng đều xuất hiện lại trong từng digest."),
    Field("alert_repeat_hours", "↳ Nhắc lại sau (giờ)", "int", "🔔 Cảnh báo — khi nào báo",
          "Số giờ trước khi một cảnh báo chưa ai xử lý được nêu lại. 0 = không bao giờ lặp."),
    Field("alert_snooze_default_days", "↳ Số ngày mặc định của /snooze", "int",
          "🔔 Cảnh báo — khi nào báo",
          "Khi gõ `/snooze <id>` mà không ghi số ngày thì ẩn bấy nhiêu ngày."),
    Field("pr_reviewer_reminder_hours", "Nhắc reviewer chưa vote sau (giờ)", "int",
          "🔔 Cảnh báo — khi nào báo",
          "Reviewer chưa vote sau bấy nhiêu giờ sẽ nhận một lời nhắc lịch sự trên PR. 0 = tắt."),
    Field("pr_reviewer_reminder_repeat_hours", "↳ Lặp lại lời nhắc mỗi (giờ)", "int",
          "🔔 Cảnh báo — khi nào báo",
          "Tiếp tục nhắc reviewer chưa vote, sau số giờ này kể từ lần nhắc trước. 0 = nhắc một "
          "lần rồi thôi."),
    Field("teams_agent_digest_interval_hours", "Digest định kỳ mỗi (giờ)", "int",
          "🔔 Cảnh báo — khi nào báo",
          "Chủ động đăng bản tổng hợp hoạt động đầy đủ lên mọi channel/chat mà bot đã được thêm "
          "vào: thống kê lần chạy autopilot, auto-review + lời nhắc đã gửi, PR đã mở/merge, "
          "ticket /log, PR sẵn sàng merge, PR kẹt lâu nhất, và standup work item theo từng người. "
          "0 = tắt. Bot cần được nhắn/thêm vào ít nhất một lần để lưu hội thoại (giữ qua các lần "
          "khởi động lại)."),
    Field("teams_agent_digest_at", "↳ Hoặc gửi cố định lúc (HH:MM)", "text",
          "🔔 Cảnh báo — khi nào báo",
          "Đăng vào một giờ cố định theo giờ địa phương, vd 09:00. Ưu tiên hơn khoảng thời gian ở "
          "trên — vốn tính từ lúc khởi động tiến trình, nên khởi động lại lúc 14:00 sẽ dời digest "
          "24h sang 14:00 vĩnh viễn. Để trống = dùng khoảng thời gian."),
    Field("digest_skip_when_empty", "Không gửi digest khi không có gì mới", "bool",
          "🔔 Cảnh báo — khi nào báo",
          "Không có việc nào quá ngưỡng VÀ không có gì xong trong kỳ → im lặng. Một tin "
          "'✅ không có gì tắc' mỗi sáng dạy người đọc lướt qua digest, và rồi lướt qua "
          "luôn hôm nó có tin thật."),
    Field("digest_respect_quiet_hours", "Digest tuân theo khung giờ báo", "bool",
          "🔔 Cảnh báo — khi nào báo",
          "Giữ digest trong cùng khung giờ với mọi thông báo khác. Trước đây digest là "
          "kênh DUY NHẤT bỏ qua giờ im lặng nên vẫn ping lúc 3h sáng."),
    Field("notify_hours_start", "Khung giờ được phép báo — từ", "text", "🔔 Cảnh báo — khi nào báo",
          "HH:MM. Khi nào được PING người. Cố ý tách khỏi khung làm việc: team thường vui khi "
          "autopilot làm tiếp buổi tối — điều họ không muốn là điện thoại reo lúc 22:40 về việc "
          "không ai xử lý được trước sáng mai. Để trống = thông báo bất kỳ giờ nào."),
    Field("notify_hours_end", "Khung giờ được phép báo — đến", "text", "🔔 Cảnh báo — khi nào báo",
          "HH:MM. Thông báo phát sinh ngoài khung được GIỮ LẠI (không bao giờ bỏ) và gửi thành "
          "MỘT bản tóm tắt khi khung mở. Comment ADO không bao giờ bị giữ — comment là hồ sơ trên "
          "work item, không phải sự làm phiền."),
    Field("notify_days", "Khung giờ được phép báo — ngày", "text", "🔔 Cảnh báo — khi nào báo",
          "vd Mon,Tue,Wed,Thu,Fri. Trống = mọi ngày."),
    Field("notify_window_applies_to", "↳ Khung giờ áp dụng cho", "select",
          "🔔 Cảnh báo — khi nào báo",
          "all = mọi thông báo (ngoài giờ bị giữ, gửi gộp khi mở khung) · digest = CHỈ "
          "digest định kỳ (Delivery, sức khoẻ quy trình, cập nhật) — ngoài giờ thì bỏ; mọi "
          "thông báo về một việc (chạy xong, conflict, lỗi, nhắc) gửi NGAY mọi lúc.",
          ("all", "digest")),
    Field("notify_quiet_max_held", "↳ Tối đa thông báo giữ lại ngoài giờ", "int",
          "🔔 Cảnh báo — khi nào báo",
          "Giới hạn hàng đợi bị giữ để một cuối tuần yên lặng không làm nó phình vô hạn. Bỏ cái "
          "cũ nhất trước và bản tóm tắt ghi rõ đã bỏ bao nhiêu."),
    # ── Notifications: kênh nhận (credentials) ──
    Field("smtp_host", "SMTP host", "text", "📣 Kênh thông báo", "Trống = tắt email."),
    Field("smtp_port", "SMTP port", "int", "📣 Kênh thông báo", "Mặc định 587 (STARTTLS)."),
    Field("smtp_user", "SMTP user", "text", "📣 Kênh thông báo"),
    Field("smtp_password", "Mật khẩu SMTP", "password", "📣 Kênh thông báo"),
    Field("email_from", "Email gửi đi (from)", "text", "📣 Kênh thông báo"),
    Field("email_to", "Email nhận (to)", "text", "📣 Kênh thông báo", "Địa chỉ người nhận."),
    Field("zalo_oa_access_token", "Access token Zalo OA", "password", "📣 Kênh thông báo",
          "Trống = tắt Zalo."),
    Field("zalo_recipient_user_id", "User id người nhận Zalo", "text", "📣 Kênh thông báo"),
    # ── 💬 Teams bot (2-way chat) ──
    Field("teams_agent_enabled", "💬 Teams bot hai chiều", "bool", "💬 Teams bot (chat 2 chiều)",
          "Trả lời và xử lý bấm nút trên Teams (approve/reject, lệnh chat) qua Azure Bot / Agent "
          "ID đã đăng ký — điền các field App ID/tenant/secret bên dưới. Cần thêm `pip install "
          ".[teams-bot]`. Cần khởi động lại."),
    Field("bot_persona_name", "🎭 Tên hiển thị của bot", "text", "💬 Teams bot (chat 2 chiều)",
          "Tên bot tự xưng trong câu trả lời trên Teams (vd 'AI Autopilot'). Dùng khi soạn lời "
          "xác nhận ticket / câu trả lời tự do."),
    Field("bot_persona_voice", "↳ Giọng văn của bot", "text", "💬 Teams bot (chat 2 chiều)",
          "Hướng dẫn giọng điệu/văn phong đưa cho Claude để câu trả lời của bot nhất quán, như "
          "một đồng đội chủ động. Để trống = kiểu máy, cụt lủn."),
    Field("teams_review_skill", "↳ Skill review PR", "text", "💬 Teams bot (chat 2 chiều)",
          "Skill bot chạy để review PR từ chat (review thật diff so với codebase và đăng kết quả "
          "lên PR). Phải có trong .claude/skills của workspace."),
    Field("teams_agentic_enabled", "↳ Chat tự do bằng agent (lượt Claude)", "bool",
          "💬 Teams bot (chat 2 chiều)",
          "Đưa tin nhắn tự do qua một lượt agent Claude thật (tools + skill) thay vì bộ phân "
          "loại ý định cố định — tự nhiên hơn, nhưng mỗi tin là một lần chạy Claude."),
    Field("teams_agent_session_memory", "↳ Nhớ mạch hội thoại", "bool",
          "💬 Teams bot (chat 2 chiều)",
          "Mỗi câu trả lời tiếp tục session Claude của tin nhắn trước trong CÙNG thread, nên một "
          "thread hoạt động như cuộc hội thoại thay vì lần nào cũng phải nói lại PR hay item nào. "
          "Giới hạn bởi TTL session-reuse ở trên."),
    Field("teams_agent_max_concurrent", "↳ Số câu trả lời chat song song tối đa", "int",
          "💬 Teams bot (chat 2 chiều)",
          "Số câu trả lời chat được giữ tiến trình Claude cùng lúc. Tách khỏi 'Max concurrent' "
          "(dùng cho task chạy 30 phút) — dùng chung sẽ khiến chat của cả team phải xếp hàng một. "
          "0 = không giới hạn."),
    Field("teams_agent_nlu_enabled", "↳ Hiểu tin nhắn tự do (chỉ đọc)", "bool",
          "💬 Teams bot (chat 2 chiều)",
          "Tin nhắn Teams dạng tự do không khớp /command sẽ được Claude phân loại thành "
          "items/prs/status/help — không bao giờ là hành động. Tốn một lượt gọi Claude cho mỗi "
          "tin không khớp. Tắt = tin không khớp chỉ nhận danh sách lệnh."),


    Field("teams_agent_app_id", "↳ Agent (App) ID", "text", "💬 Teams bot (chat 2 chiều)",
          "Application (client) ID của Azure Bot. Cần thêm `pip install .[teams-bot]`."),
    Field("teams_agent_tenant_id", "↳ Tenant ID", "text", "💬 Teams bot (chat 2 chiều)",
          "Directory (tenant) ID chứa App registration."),
    Field("teams_agent_app_secret", "↳ App secret của agent", "password",
          "💬 Teams bot (chat 2 chiều)",
          "Client secret ở mục Certificates & secrets của App registration."),
    # ── Fleet ──
    Field("fleet_role", "Vai của máy này", "select", "🛰 Fleet",
          "Blank = máy độc lập (mặc định, không đổi gì). 'central' = VM trung tâm giữ cấu hình "
          "chung và theo dõi máy trạm. 'worker' = máy trạm, kéo cấu hình từ trung tâm và gửi "
          "heartbeat về.", ("", "central", "worker")),
    Field("fleet_central_url", "↳ URL trung tâm", "text", "🛰 Fleet",
          "Địa chỉ VM trung tâm, vd http://vm-autopilot:8080. Chỉ máy trạm gọi đi — trung tâm "
          "KHÔNG cần gọi ngược về, nên máy sau NAT/VPN vẫn tham gia được. Để trống trên máy "
          "trạm = không bao giờ gọi về: máy vẫn chạy với cấu hình sẵn có và không xuất hiện "
          "trên trang Fleet.",
          show_when_key="fleet_role", show_when_values=("worker",)),
    Field("fleet_token", "↳ Token chung", "password", "🛰 Fleet",
          "Bí mật chung của cả đội, phải khớp ở 2 phía. Bấm ✨ để sinh ngẫu nhiên trên trung tâm, "
          "👁 để xem lại token đang lưu mà copy sang máy trạm (nên đặt qua biến môi trường "
          "AUTOPILOT_FLEET_TOKEN). "
          "Trung tâm KHÔNG bật API fleet khi token rỗng — một endpoint mở sẽ phát toàn bộ cấu "
          "hình chung cho bất kỳ ai gọi tới.",
          show_when_key="fleet_role", show_when_values=("central", "worker"),
          generate=True, reveal=True),
    Field("fleet_worker_name", "↳ Tên máy trạm", "text", "🛰 Fleet",
          "Tên hiển thị trên trang Fleet. Blank = hostname. Giữ nguyên qua các lần khởi động "
          "lại thì lịch sử của máy mới gom về một dòng.",
          show_when_key="fleet_role", show_when_values=("worker",)),
    Field("fleet_local_keys", "↳ Khoá máy trạm tự giữ", "list", "🛰 Fleet",
          "Mỗi dòng một tên setting mà trung tâm KHÔNG được ghi đè trên MÁY NÀY — dùng khi máy "
          "cần giữ riêng một thiết lập (vd 'sdlc_profile' để máy này luôn chạy vai qc dù đội "
          "để dev). Secrets, trigger_tag, workspaces, repos, database_url và toàn bộ fleet_* đã "
          "luôn được giữ sẵn — không cần khai lại.",
          placeholder="sdlc_profile",
          show_when_key="fleet_role", show_when_values=("worker",)),
    Field("fleet_sync_interval_minutes", "↳ Chu kỳ đồng bộ (phút)", "int", "🛰 Fleet",
          "Máy trạm gọi về mỗi bấy nhiêu phút. Tối thiểu 1 phút.",
          show_when_key="fleet_role", show_when_values=("worker",)),
    Field("fleet_knowledge_accept", "↳ Nhận tri thức từ trung tâm", "select", "🛰 Fleet",
          "auto = áp dụng ngay khi trung tâm duyệt. manual = xếp hàng chờ, bạn bấm Nhận "
          "trên trang Tri thức. Dù chọn cách nào, khi bạn XOÁ một dòng của đội thì đó là "
          "từ chối vĩnh viễn — nó không quay lại ở nhịp đồng bộ sau.",
          ("auto", "manual"),
          show_when_key="fleet_role", show_when_values=("worker",)),
    Field("fleet_offline_after_minutes", "↳ Coi là offline sau (phút)", "int", "🛰 Fleet",
          "Trung tâm: máy im lặng lâu hơn mức này sẽ hiện đỏ trên trang Fleet.",
          show_when_key="fleet_role", show_when_values=("central",)),
    Field("fleet_alert_offline", "↳ Báo khi máy trạm offline", "bool", "🛰 Fleet",
          "Trung tâm gửi thông báo (qua các kênh đã cấu hình) khi một máy trạm im lặng quá "
          "ngưỡng offline, và một lần nữa khi máy đó gọi về lại. Mỗi đợt chỉ báo một lần.",
          show_when_key="fleet_role", show_when_values=("central",)),
    Field("fleet_command_expire_minutes", "↳ Lệnh hết hạn sau (phút)", "int", "🛰 Fleet",
          "Lệnh gửi máy trạm mà không được xác nhận trong khoảng này sẽ chuyển 'hết hạn' — "
          "máy đang tắt hoặc chạy bản cũ không hiểu lệnh.",
          show_when_key="fleet_role", show_when_values=("central",)),
    Field("fleet_disk_warn_gb", "↳ Cảnh báo disk trống dưới (GB)", "float", "🛰 Fleet",
          "Máy trạm có dung lượng trống thấp hơn mức này sẽ hiện đỏ trên trang Fleet.",
          show_when_key="fleet_role", show_when_values=("central",)),
    Field("fleet_command_poll_seconds", "↳ Hỏi lệnh mỗi (giây)", "int", "🛰 Fleet",
          "Máy trạm hỏi trung tâm có lệnh mới không (tạm dừng, cập nhật, nhận việc…). Nhẹ "
          "hơn heartbeat nhiều nên chạy dày hơn. Tối thiểu 15 giây.",
          show_when_key="fleet_role", show_when_values=("worker",)),
    Field("fleet_accept_commands", "↳ Nhận lệnh từ trung tâm", "bool", "🛰 Fleet",
          "Tắt = máy vẫn báo cáo và đồng bộ cấu hình nhưng TỪ CHỐI mọi lệnh điều khiển từ xa.",
          show_when_key="fleet_role", show_when_values=("worker",)),
    Field("fleet_accept_remote_update", "↳ Cho phép trung tâm cập nhật máy này", "bool",
          "🛰 Fleet",
          "Lệnh 'Cập nhật' cài bản mới lên máy này (đợi run đang chạy xong rồi mới cài). "
          "Tắt nếu máy này phải được cập nhật bằng tay.",
          show_when_key="fleet_role", show_when_values=("worker",)),
    # ── ⬆️ Cập nhật ──
    Field("update_check_enabled", "⬆️ Kiểm tra bản mới", "bool", "⬆️ Cập nhật",
          "Hỏi GitHub Releases xem có bản mới hơn không, và hiện một nút để cập nhật. "
          "KHÔNG bao giờ tự cài — luôn cần bạn bấm. Tắt = không hỏi, không hiện gì."),
    Field("update_repo", "↳ Repo phát hành", "text", "⬆️ Cập nhật",
          "Dạng owner/repo. Đổi khi bạn chạy một bản fork riêng."),
    Field("update_check_interval_hours", "↳ Hỏi mỗi (giờ)", "int", "⬆️ Cập nhật",
          "GitHub cho 60 lượt/giờ/IP với truy cập ẩn danh, nên đừng đặt quá dày. "
          "Mặc định 6 giờ."),
    Field("update_drain_timeout_minutes", "↳ Chờ task đang chạy tối đa (phút)", "int",
          "⬆️ Cập nhật",
          "Cập nhật sẽ ngừng nhận việc mới rồi đợi các task hiện tại xong. Quá hạn này "
          "thì HUỶ cập nhật chứ không cắt ngang — restart giữa chừng sẽ đánh mọi run "
          "đang chạy thành FAILED 'Interrupted (process restarted)'."),
    Field("update_restart_mode", "↳ Cách khởi động lại", "select", "⬆️ Cập nhật",
          "auto = tự chọn theo hệ điều hành (khuyến nghị). exec = thay ảnh tiến trình "
          "(POSIX). spawn = bật tiến trình mới rồi thoát (Windows). exit = chỉ thoát, "
          "để supervisor (systemd/Docker) dựng lại.",
          ("auto", "exec", "spawn", "exit")),
    # ── Web / Security ──
    Field("dashboard_auth_password", "Mật khẩu dashboard", "password", "🔐 Web & bảo mật",
          "Mật khẩu truy cập dashboard này (HTTP Basic — username bất kỳ). Lưu dạng hash PBKDF2, "
          "không bao giờ lưu plaintext. Để trống = giữ mật khẩu hiện tại. Lần đầu khởi động chưa "
          "có mật khẩu, CLI sẽ hỏi."),
    Field("config_export_password", "Mật khẩu export đầy đủ", "password", "🔐 Web & bảo mật",
          "Mã hoá bản export cấu hình đầy đủ (bản tải về CÓ KÈM secret). Cần đúng mật khẩu này để "
          "giải mã file đã export. Để trống = giữ mật khẩu hiện tại."),
    # ── ⚖️ Tự chủ & an toàn ── (appended last so every #sec-N anchor before it stays put)
    Field("trust_ladder_enabled", "Tự chủ theo uy tín", "bool", "⚖️ Tự chủ & an toàn",
          "Mỗi (project, loại việc) tự lên/xuống mức tự chủ theo kết quả thật: merge sạch "
          "nhiều thì được nới, bị trả lại liên tiếp thì bị siết. autonomy_level là trần."),
    Field("trust_min_runs", "↳ Số lần chạy tối thiểu để lên mức", "int", "⚖️ Tự chủ & an toàn",
          "Cần đủ số lần chạy đã có kết quả kể từ lần đổi mức gần nhất mới xét lên mức."),
    Field("trust_promote_rate", "↳ Tỉ lệ merge sạch để lên mức", "float", "⚖️ Tự chủ & an toàn",
          "Tỉ lệ lần chạy merge mà không bị sửa / trả lại / mở lại phải đạt ngưỡng này (0–1)."),
    Field("trust_max_level", "↳ Mức tối đa", "int", "⚖️ Tự chủ & an toàn",
          "0 chỉ lập kế hoạch · 1 PR nháp · 2 PR sẵn sàng review · 3 tự động hoàn toàn "
          "(chỉ khi nâng lên 3)."),
    Field("risk_gate_enabled", "Chặn thay đổi rủi ro cao", "bool", "⚖️ Tự chủ & an toàn",
          "Run đụng migration DB, auth/permission, hạ tầng deploy, manifest phụ thuộc — hoặc "
          "sửa quá nhiều file — sẽ chờ người duyệt thay vì chuyển tiếp."),
    Field("risk_patterns", "↳ Mẫu file rủi ro", "list", "⚖️ Tự chủ & an toàn",
          "Mỗi dòng một glob; ** đi qua nhiều thư mục; mẫu không có / khớp tên file ở mọi nơi."),
    Field("risk_max_files", "↳ Ngưỡng số file", "int", "⚖️ Tự chủ & an toàn",
          "Một run sửa nhiều hơn số file này được coi là rủi ro. 0 = tắt."),
    Field("risk_gate_tag", "↳ Tag chờ duyệt rủi ro", "text", "⚖️ Tự chủ & an toàn",
          "Gắn lên item bị giữ để lọc trên board."),
    Field("plan_first_tag", "Tag \"lập kế hoạch trước\"", "text", "⚖️ Tự chủ & an toàn",
          "Item mang tag này chỉ được lập kế hoạch (comment), chờ duyệt rồi mới code. Duyệt "
          "ngay trong Hộp quyết định."),
    Field("plan_approved_tag", "↳ Tag duyệt kế hoạch", "text", "⚖️ Tự chủ & an toàn",
          "Gắn tag này (hoặc bấm Duyệt kế hoạch trong Hộp quyết định) để autopilot triển khai."),
    Field("plan_pending_tag", "↳ Tag chờ duyệt kế hoạch", "text", "⚖️ Tự chủ & an toàn",
          "Tự gắn sau khi đăng kế hoạch; tự gỡ khi bắt đầu chạy bản đã duyệt."),
    Field("plan_first_min_points", "↳ Story points tối thiểu", "float", "⚖️ Tự chủ & an toàn",
          "Item có story points ≥ N cũng phải lập kế hoạch trước. 0 = chỉ theo tag."),
    Field("item_budget_tokens", "Ngân sách token mỗi item", "int", "⚖️ Tự chủ & an toàn",
          "Tổng token qua mọi lần chạy của một item vượt mức này thì giữ item chờ người, không "
          "tự chạy lại. 0 = tắt."),
    Field("circuit_breaker_failures", "Ngắt mạch sau N lỗi liên tiếp", "int", "⚖️ Tự chủ & an toàn",
          "N run lỗi liên tiếp → máy tự tạm dừng nhận việc và báo một lần; tiếp tục bằng một "
          "nút trong Hộp quyết định. 0 = tắt."),
    Field("run_now_lease", "Khoá chạy-ngay giữa các máy", "select", "⚖️ Tự chủ & an toàn",
          "auto = chỉ khi máy thuộc fleet; on / off = ép bật / tắt. Nhiều máy cùng thấy tag "
          "chạy-ngay thì chỉ máy nhận việc trước mới chạy.", ("auto", "on", "off"),
          show_when_key="fleet_role", show_when_values=("central", "worker")),
    Field("run_now_claim_settle_seconds", "↳ Chờ trước khi chốt máy chạy (giây)", "int", "⚖️ Tự chủ & an toàn",
          "Chờ comment nhận việc của máy khác đến rồi mới xem máy nào chạy.",
          show_when_key="fleet_role", show_when_values=("central", "worker")),
    Field("pr_session_watch_seconds", "Kiểm tra phiên giải conflict mỗi (giây)", "int", "⚖️ Tự chủ & an toàn",
          "Phiên interactive giải conflict xong được kiểm tra và push trong vòng chừng này giây."),
    Field("pr_conflict_claim_settle_seconds", "Chờ chốt máy giải PR conflict (giây)", "int", "⚖️ Tự chủ & an toàn",
          "Nhiều máy cùng thấy một PR conflict: chờ chừng này giây rồi comment nhận việc "
          "sớm nhất thắng — một PR chỉ một phiên.",
          show_when_key="fleet_role", show_when_values=("central", "worker")),
)

# ── Parent switches ──────────────────────────────────────────────────────────────
# ``child key -> (controlling key, values that make it apply)``.
#
# The "↳" prefix in a label already says "this belongs to the switch above me", but a
# prefix is a convention the page cannot act on: ~40 of these stayed on screen, fully
# editable, while the switch that gives them any meaning was off. Someone raising the
# test timeout on a machine with the test gate disabled is configuring nothing, and
# there was no way to tell from the page.
#
# ON is spelled "1" for a bool parent (see :func:`control_value`). Only relationships
# where the child is genuinely INERT while the parent is off belong here — a field
# that merely *relates* to another keeps being shown, because hiding something that
# still has an effect is worse than the clutter this removes.
_DEPENDS_ON: dict[str, tuple[str, tuple[str, ...]]] = {
    # Execution & Autonomy — the interactive console has no counterpart headless.
    "interactive_close_on": ("execution_mode", ("interactive",)),
    "interactive_idle_timeout_minutes": ("execution_mode", ("interactive",)),
    "interactive_resume_on_rework": ("execution_mode", ("interactive",)),
    "interactive_bypass_permissions": ("execution_mode", ("interactive",)),
    "claude_session_ttl_hours": ("reuse_claude_session", ("1",)),
    # ⬆️ Cập nhật — all four are dead weight on a machine that is not checking.
    "update_repo": ("update_check_enabled", ("1",)),
    "update_check_interval_hours": ("update_check_enabled", ("1",)),
    "update_drain_timeout_minutes": ("update_check_enabled", ("1",)),
    "update_restart_mode": ("update_check_enabled", ("1",)),
    # 🧪 Quality gates
    "lessons_max_injected": ("learning_loop_enabled", ("1",)),
    "test_commands": ("test_gate_enabled", ("1",)),
    "test_timeouts": ("test_gate_enabled", ("1",)),
    "test_command": ("test_gate_enabled", ("1",)),
    "test_gate_block_when_not_run": ("test_gate_enabled", ("1",)),
    "test_timeout_seconds": ("test_gate_enabled", ("1",)),
    "pr_score_auto_min": ("pr_scoring_enabled", ("1",)),
    "pr_score_review_min": ("pr_scoring_enabled", ("1",)),
    # 🔁 PR review & feedback
    "max_revisions": ("feedback_loop_enabled", ("1",)),
    "pr_auto_review_on_added": ("pr_reviewer_tracking_enabled", ("1",)),
    "pr_conflict_autoresolve": ("pr_conflict_tracking_enabled", ("1",)),
    "pr_conflict_command": ("pr_conflict_tracking_enabled", ("1",)),
    "pr_conflict_max_files": ("pr_conflict_tracking_enabled", ("1",)),
    "pr_conflict_max_attempts": ("pr_conflict_tracking_enabled", ("1",)),
    "pr_conflict_allow_preexisting_failures": ("pr_conflict_tracking_enabled", ("1",)),
    "pr_conflict_poll_minutes": ("pr_conflict_tracking_enabled", ("1",)),
    "max_comment_rounds": ("comment_reprocess_enabled", ("1",)),
    # 🚚 Delivery — the history switch is what starts the clock at all.
    "delivery_history_interval_minutes": ("delivery_history_enabled", ("1",)),
    "delivery_history_retention_days": ("delivery_history_enabled", ("1",)),
    # Dependency scheduling
    "scheduler_ai_conflict_min_score": ("scheduler_use_ai_conflicts", ("1",)),
    "batch_max_items": ("batch_related_enabled", ("1",)),
    "batch_stacked_prs": ("batch_related_enabled", ("1",)),
    # Planning workbench
    "planning_ai_max_pairs": ("planning_ai_analysis", ("1",)),
    "planning_ai_min_score": ("planning_ai_analysis", ("1",)),
    "planning_ai_timeout_seconds": ("planning_ai_analysis", ("1",)),
    # Closed-loop SDLC
    "sdlc_profile": ("sdlc_loop_enabled", ("1",)),
    "sdlc_type_profiles": ("sdlc_loop_enabled", ("1",)),
    "sdlc_default_profile": ("sdlc_loop_enabled", ("1",)),
    "sdlc_max_iterations": ("sdlc_loop_enabled", ("1",)),
    "sdlc_interactive_gate": ("sdlc_loop_enabled", ("1",)),
    "trust_min_runs": ("trust_ladder_enabled", ("1",)),
    "trust_promote_rate": ("trust_ladder_enabled", ("1",)),
    "trust_max_level": ("trust_ladder_enabled", ("1",)),
    "risk_patterns": ("risk_gate_enabled", ("1",)),
    "risk_max_files": ("risk_gate_enabled", ("1",)),
    "risk_gate_tag": ("risk_gate_enabled", ("1",)),
    "sdlc_advance_on_draft": ("sdlc_loop_enabled", ("1",)),
    # Process health
    "process_health_interval_hours": ("process_health_enabled", ("1",)),
    "process_health_window_days": ("process_health_enabled", ("1",)),
    "process_health_blocked_days": ("process_health_enabled", ("1",)),
    "process_health_adhoc_threshold_pct": ("process_health_enabled", ("1",)),
    # Spec drift
    "spec_drift_tag": ("spec_drift_enabled", ("1",)),
    "spec_drift_holds_item": ("spec_drift_enabled", ("1",)),
    # 🔔 Cảnh báo
    "alert_repeat_hours": ("alert_dedup_enabled", ("1",)),
    # 💬 Teams bot — every one of these is read only by the bot.
    "bot_persona_name": ("teams_agent_enabled", ("1",)),
    "bot_persona_voice": ("teams_agent_enabled", ("1",)),
    "teams_review_skill": ("teams_agent_enabled", ("1",)),
    "teams_agentic_enabled": ("teams_agent_enabled", ("1",)),
    "teams_agent_session_memory": ("teams_agent_enabled", ("1",)),
    "teams_agent_max_concurrent": ("teams_agent_enabled", ("1",)),
    "teams_agent_nlu_enabled": ("teams_agent_enabled", ("1",)),
    "teams_agent_app_id": ("teams_agent_enabled", ("1",)),
    "teams_agent_tenant_id": ("teams_agent_enabled", ("1",)),
    "teams_agent_app_secret": ("teams_agent_enabled", ("1",)),
}


def _with_dependencies(fields: tuple[Field, ...]) -> tuple[Field, ...]:
    """Apply ``_DEPENDS_ON`` to the declared fields.

    A field that already carries its own ``show_when_key`` keeps it — the table is a
    convenience, never an override.
    """
    out = []
    for f in fields:
        dep = _DEPENDS_ON.get(f.key)
        out.append(
            replace(f, show_when_key=dep[0], show_when_values=dep[1])
            if dep and not f.show_when_key else f
        )
    return tuple(out)


FIELDS: tuple[Field, ...] = _with_dependencies(_BASE_FIELDS)

# Every parent named above must be a real field, or the page would hide a child behind
# a switch that does not exist — silently, and only on the machine whose config happens
# to reach that branch. Cheap to check once at import.
_UNKNOWN_PARENTS = {
    parent for parent, _ in _DEPENDS_ON.values()
} - {f.key for f in _BASE_FIELDS}
if _UNKNOWN_PARENTS:  # pragma: no cover - a typo caught at import time
    raise RuntimeError(f"_DEPENDS_ON names unknown settings: {sorted(_UNKNOWN_PARENTS)}")


def control_value(current: Mapping[str, Any], key: str) -> str:
    """The controlling field's value as the page compares it.

    A checkbox has no meaningful ``value`` — it is ticked or it is not — so a bool
    reads as ``"1"`` / ``""``. The browser does the same thing in
    ``settings.html``, which is what keeps the server-rendered state and the live
    re-evaluation from disagreeing.
    """
    value = current.get(key)
    if isinstance(value, bool):
        return "1" if value else ""
    return str(value or "")


def applies(f: Field, current: Mapping[str, Any]) -> bool:
    """Does this field do anything on a machine configured like ``current``?"""
    if not f.show_when_key:
        return True
    return control_value(current, f.show_when_key) in f.show_when_values


def model_defaults(config: Any) -> dict[str, Any]:
    """Every settings field's out-of-the-box value, for :func:`has_value`."""
    out: dict[str, Any] = {}
    for key, info in getattr(type(config), "model_fields", {}).items():
        try:
            out[key] = info.get_default(call_default_factory=True)
        except Exception:  # noqa: BLE001 — a factory that needs context is not a default
            continue
    return out


def has_value(
    f: Field,
    current: Mapping[str, Any],
    secrets_set: Mapping[str, bool],
    defaults: Mapping[str, Any] | None = None,
) -> bool:
    """Has somebody actually configured this, or is it just sitting at its default?

    Decides between hiding an inapplicable field and dimming it. "Non-empty" is the
    wrong test and hid almost nothing: every int and bool ships with a default, so
    ``30`` in "coi là offline sau (phút)" counted as a decision someone made and the
    field stayed on screen for a worker that can never use it. A value equal to the
    default is not a decision — hiding it loses nothing, because turning the switch
    back brings the same number with it.

    Secrets never reach ``current`` (they are not echoed back), so their answer comes
    from ``secrets_set`` instead.
    """
    if f.kind == "password":
        return bool(secrets_set.get(f.key))
    value = current.get(f.key)
    if defaults is not None and f.key in defaults and value == defaults[f.key]:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return bool(str(value or "").strip())


# Fields that only take effect after a restart (the value is captured at startup).
RESTART_REQUIRED = frozenset({"max_concurrent"})

# Never echo these values back into the form. Scalar (password-kind) fields only: a
# secret must round-trip as "blank = keep the stored value", which the password input
# does and a list textarea does NOT — a blank textarea parses as an empty list, so
# marking a list field secret would silently WIPE it the first time anyone saved the
# page. ``teams_webhook_urls`` is therefore not listed here; it stays in
# EXPORT_EXCLUDE, which is where the real leak risk (sharing a config file) lives.
# Anyone who can open this page can already rewrite the ADO PAT, so they are trusted
# with the webhook URLs too.
SECRET_KEYS = frozenset({
    "ado_pat", "teams_agent_app_secret",
    # The fleet token is a password field like the rest, and leaving it out of this set
    # was not a security hole (a password input never renders its value) — it broke the
    # page's ability to SAY it is set: the box read "not set" on a central with a
    # perfectly good token, which is the exact sentence that sends someone rotating a
    # working secret across the fleet. It is still readable back on demand — see the
    # `reveal` flag, which is a different question from whether it is echoed into HTML.
    "fleet_token",
    "teams_webhook_url", "smtp_password", "zalo_oa_access_token",
    "dashboard_auth_password", "dashboard_auth_password_hash", "config_export_password",
})

# Keys that must never LEAVE this machine: a secret, or something that only makes
# sense against this host's disk, ports and identity. Sharing one either leaks a
# credential or pins a teammate to folders that do not exist on their machine.
NEVER_SHARED = frozenset({
    # ── secrets ──
    "ado_pat", "oauth_app_id", "oauth_app_secret",
    "smtp_host", "smtp_port", "smtp_user", "smtp_password",
    "zalo_oa_access_token", "zalo_recipient_user_id",
    "teams_webhook_url", "teams_webhook_urls", "teams_webhook_channels",
    "email_to", "email_from",
    "teams_agent_app_id", "teams_agent_app_secret", "teams_agent_tenant_id",
    "tenants",              # each tenant embeds its own ado_pat
    "dashboard_auth_token", "dashboard_auth_password_hash", "webhook_secret",
    "config_export_password",
    # ── machine / host specific ──
    "workspace_directory", "repo_working_directory", "worktrees_dir",
    # Both carry this host's filesystem paths, exactly like workspace_directory —
    # sharing them would pin a teammate to folders that do not exist on their machine.
    "workspace_map", "workspaces",
    "database_url", "health_host", "health_port", "plugins_directory",
    "dashboard_allow_remote_without_auth",
    "trigger_tag",          # per-host default tag
    # ── fleet wiring: identity, never shared ──
    # A central that exported these would turn every worker that applied the document
    # into a second central pointed at itself, and hand the fleet token to anything
    # that could read one machine's config. They say WHO a machine is in the fleet,
    # which is the one thing the fleet cannot be allowed to overwrite.
    "fleet_role", "fleet_central_url", "fleet_token", "fleet_worker_name",
    "fleet_local_keys", "fleet_sync_interval_minutes", "fleet_offline_after_minutes",
    # Same rule, and the second one would be actively wrong: the promotion threshold
    # is a CENTRAL's editorial policy about what it redistributes, meaningless on a
    # machine that only contributes. The first is each machine's own answer to "do I
    # share what I learn" — not the centre's to decide.
    "fleet_knowledge_sync", "fleet_knowledge_auto_promote",
    # Each side's own answer: whether THIS worker obeys, how often it asks, and how the
    # central watches. A centre that could switch "accept commands" back on remotely
    # would make the switch meaningless.
    "fleet_command_poll_seconds", "fleet_accept_commands", "fleet_accept_remote_update",
    "fleet_alert_offline", "fleet_command_expire_minutes", "fleet_disk_warn_gb",
    "repos",               # RepoConfig entries embed local filesystem paths
})

# Settings each machine answers for ITSELF. Not secret, not a path — every one of them
# has a perfectly good value that is simply DIFFERENT per machine, so a central serving
# one value is serving the wrong one to everybody else.
#
# This group was missing, and every key in it was being shared. The symptom people hit
# first is the mildest one: ``sdlc_profile`` is documented as "the role THIS machine
# runs", so a central pushing it turns the whole fleet into one role — which is why
# every fleet install ends up hand-writing `sdlc_profile` into `fleet_local_keys`.
# Needing the same manual workaround on every machine is the signal that a default is
# wrong, not that operators are careless.
#
# Kept as its own name rather than folded into NEVER_SHARED because the UI owes a
# different sentence for each: "this is a secret" and "your machine decides this" are
# not the same answer, and a page that says the first about `max_concurrent` is lying.
MACHINE_LOCAL = frozenset({
    # Which work this machine takes, and who it answers to. `trigger_tag` (singular)
    # was already here; the plural went on being shared even though
    # `effective_trigger_tags` merges the two — so a central could hand every worker
    # the same stream and have two machines fight over one work item, which is the
    # exact outcome `trigger_tag`'s own comment says it exists to prevent.
    "trigger_tags",
    "assignee_trigger_user",   # "the assignee THIS machine claims" — and its command owner
    "sdlc_profile",            # "the role THIS machine runs, whatever the item is"
    # What this machine is physically able to do. A 16-core VM and a laptop cannot
    # share a concurrency limit, and `interactive` opens a desktop console — a headless
    # server has nowhere to put it.
    "max_concurrent",
    "execution_mode",
    # Scheduled agent runs. LoopScheduler has no cross-machine coordination at all, so
    # a shared list is N machines running the same cron: N Claude runs, N pull requests,
    # N times the spend, for one job. Per-machine is the only safe default until
    # something can say WHICH machine owns a loop.
    "scheduled_loops",
    # Companions of keys that were already local. Leaving these shared made the pairs
    # disagree: `repos`/`workspace_directory` local but the whitelist over them shared,
    # and the Teams bot's credentials local but its ON switch shared — which starts a
    # bot with no credentials and fails in a loop.
    "allowed_repos",
    "default_workspace_name",
    # The rest of the "Workspace & Repository" section. Every other thing describing
    # this machine's checkout is already local — `workspace_directory`, `workspaces`,
    # `workspace_map`, `repos`, `allowed_repos`, `default_workspace_name` — so the
    # section had one editable field and two the central quietly put back on the next
    # beat, which is the single most confusing state a settings page can be in: you
    # change a value, it saves, and minutes later it is the old one again.
    #
    # `base_branch` is "the branch new feature branches are cut from", i.e. a property
    # of the clone sitting on THIS disk, and a workspace entry already overrides it
    # locally — so the flat key was the only path by which a central could contradict
    # a machine about its own repo. It is not cosmetic: StateSync._check_deploys asks
    # ADO for builds on `deploy_branch or base_branch`, so a central serving the wrong
    # branch name makes the deploy watcher find nothing and every item sits in its
    # merge state forever, reported only as one "deploy stage found no successful
    # build" line at info level.
    #
    # `repo_descriptions` describes the repos in this machine's workspace, and the list
    # of those repos was already local — a central's descriptions are for repos a
    # worker may not even have cloned.
    "base_branch",
    "repo_descriptions",
    # Who reviews this machine's pull requests, and who may drive this machine. These
    # name PEOPLE, and a central naming them for everybody is the one thing it is worst
    # placed to decide.
    #
    # `pr_extra_reviewer_ids` holds ADO identity GUIDs, and a GUID belongs to ONE
    # organization: a machine pointed at a second org cannot even resolve the ids the
    # central serves it, so every PR it opens quietly fails to add the reviewers the
    # settings page says it adds. The bot's own identity (`pr_bot_identity`) was already
    # local for exactly that reason — the reviewers it adds were not, which is the same
    # fact half-applied.
    #
    # The switch and its modifier go with the list, not against it. Leaving them shared
    # is the pattern already described above for the Teams bot: a central turning on
    # "add the assignee" and "mark them required" on a machine whose reviewer ids do not
    # resolve blocks that machine's PRs on people ADO cannot find.
    "pr_extra_reviewer_ids", "pr_add_assignee_as_reviewer", "pr_reviewers_required",
    # Which branches this machine's reviewer tracking watches — branch topology, the
    # same reason `base_branch` is local.
    "pr_reviewer_target_branches",
    # `assignee_trigger_user` is already local and its own help says it is "the OWNER:
    # the account whose /commands and @mentions this machine obeys". These two are the
    # rest of that sentence and were being decided elsewhere: `command_users` is who ELSE
    # may drive this machine, and `auto_transition_assignee` is what the local owner
    # FALLS BACK TO when it is blank — so leaving it blank on a worker handed the choice
    # of whose items this machine acts on to the central.
    "command_users", "auto_transition_assignee",
    # Whether THIS machine takes approved knowledge on sight or queues it for a person
    # here. Not a secret and not an identity — it is the machine's own policy about what
    # it accepts, which is exactly what this group is for.
    "fleet_knowledge_accept",
    # ⬆️ Self-update — properties of the INSTALL sitting on this disk. Whether this
    # machine can take a wheel at all depends on how it was installed (editable checkout,
    # wheel, container) and how it comes back depends on what launched it, so a central
    # serving one answer would be serving the wrong one to most of the fleet.
    "update_check_enabled", "update_repo", "update_check_interval_hours",
    "update_drain_timeout_minutes", "update_restart_mode",
    "teams_agent_enabled",
    "pr_bot_identity",         # the identity of THIS machine's PAT
    # 💬 Teams bot — the whole section. The bot runs on whichever machine holds the
    # credentials, and its credentials were never shareable, so a central pushing the
    # switch and the behaviour knobs configures a bot that cannot start: persona and
    # review skill for nobody, and an enabled flag that fails in a loop.
    "bot_persona_name", "bot_persona_voice", "teams_review_skill",
    "teams_agentic_enabled", "teams_agent_session_memory",
    "teams_agent_max_concurrent", "teams_agent_nlu_enabled",
    # 🔔 Cảnh báo — who gets interrupted, how often, and inside which hours. Every
    # channel these reach is per-machine already, so the thresholds that decide the
    # noise belong with the machine that makes it. A team that wants one policy sets
    # it once and leaves it; a machine that is being watched more closely for a week
    # does not have to argue with the central about it.
    "alert_events", "alert_min_severity",
    "delivery_merge_hours", "delivery_review_hours", "delivery_stale_days",
    "delivery_max_age_days",
    "alert_dedup_enabled", "alert_repeat_hours", "alert_snooze_default_days",
    "pr_reviewer_reminder_hours", "pr_reviewer_reminder_repeat_hours",
    "teams_agent_digest_interval_hours", "teams_agent_digest_at",
    "digest_skip_when_empty", "digest_respect_quiet_hours",
    "notify_hours_start", "notify_hours_end", "notify_days", "notify_quiet_max_held",
    "notify_window_applies_to",
})

# Where a team's notices go. These ARE credentials — a Teams Workflows URL is itself
# the authorisation to post — so they never belong in the YAML somebody downloads and
# emails to a colleague. But inside one fleet they are exactly what should be the same
# everywhere: a worker that reports to a different channel than the rest of the team is
# a worker nobody reads.
#
# Two audiences, two answers, which is why the single filter had to be split. The fleet
# document is served over an authenticated endpoint to machines that already hold the
# shared token; the export is a file that leaves the building.
#
# The line inside this group is TRANSPORT versus RECIPIENT. How a machine sends — the
# Teams Workflows URL, the SMTP server and the account it authenticates as, the Zalo OA
# token — is team plumbing: identical everywhere, tedious to paste onto each machine,
# and genuinely the central's to serve. WHO gets the message is not plumbing, it is a
# person: `email_to` and the Zalo recipient name individuals, and a central serving them
# means one operator's phone is the one that buzzes for every machine in the fleet, with
# no way to opt a machine out except by claiming the key. Those two stay with the machine
# that makes the noise, next to the alert thresholds already there for the same reason.
FLEET_ONLY_SHARED = frozenset({
    "teams_webhook_url", "teams_webhook_urls", "teams_webhook_channels",
    "smtp_host", "smtp_port", "smtp_user", "smtp_password",
    "email_from",
    "zalo_oa_access_token",
})

# What a downloadable/shareable config leaves out: secrets, this host's identity, and
# anything each machine answers for itself.
EXPORT_EXCLUDE = NEVER_SHARED | MACHINE_LOCAL

# What the FLEET document leaves out. The same list minus the notification channels,
# which a central may serve to its own workers even though a shared file must not carry
# them. Derived rather than written out, so a key added to either set above cannot be
# forgotten here.
FLEET_EXCLUDE = EXPORT_EXCLUDE - FLEET_ONLY_SHARED

# Keys stripped even from the FULL (with-secrets) export: the export/auth
# mechanism's own material — embedding it would be pointless (the export key)
# or a hash of a credential rather than the credential itself.
FULL_EXPORT_EXCLUDE = frozenset({"config_export_password", "dashboard_auth_password_hash"})


# How a setting is governed on the machine you are looking at. One vocabulary for the
# Settings page, the save handler and the doctor, so they cannot describe the same key
# three different ways.
OWNER_CENTRAL = "central"   # the fleet serves it; editing it here is undone on the next beat
OWNER_MACHINE = "machine"   # this machine decides, and always has
OWNER_CLAIMED = "claimed"   # shared by nature, but this machine took it via fleet_local_keys


def owner_of(key: str, config: Any) -> str:
    """Who decides ``key`` on this machine.

    Only a worker has a central to answer to; a standalone or central machine owns
    everything it holds, which is why both come back as :data:`OWNER_MACHINE`.
    """
    if (getattr(config, "fleet_role", "") or "") != "worker":
        return OWNER_MACHINE
    # FLEET_EXCLUDE, not EXPORT_EXCLUDE: the question here is what the central actually
    # SENDS, and the two differ by the notification channels. Reading the export filter
    # would have shown the webhook fields as this machine's own while the next
    # heartbeat quietly replaced them.
    if key in FLEET_EXCLUDE:
        return OWNER_MACHINE
    claimed = {str(k).strip() for k in (getattr(config, "fleet_local_keys", None) or [])}
    return OWNER_CLAIMED if key in claimed else OWNER_CENTRAL


def writable_here(key: str, config: Any) -> bool:
    """May THIS machine persist a value for ``key``?

    Enforced in the save handler, not only in the template. A worker that writes a
    centrally-served setting has not configured anything — the next heartbeat puts the
    central's value back — so accepting the write means showing someone a "saved"
    banner for a change that silently disappears minutes later. Server-side because a
    disabled input is a suggestion, and because the same POST can arrive from curl.
    """
    return owner_of(key, config) != OWNER_CENTRAL


def fleet_settings(config: Any) -> dict[str, Any]:
    """What a central serves its own workers.

    Everything :func:`export_settings` shares, plus the notification channels — so a
    fleet reports to one place without somebody pasting the same webhook URL onto every
    machine. Never use this to build a file: that is what ``export_settings`` is for,
    and the difference between them is a credential.
    """
    data = _dump(config)
    return {k: v for k, v in data.items() if k not in FLEET_EXCLUDE}


def _dump(config: Any) -> dict[str, Any]:
    """Settings as plain JSON-able data (nested models included)."""
    if hasattr(config, "model_dump"):
        return config.model_dump(mode="json")
    return {f.key: getattr(config, f.key, None) for f in FIELDS}   # tests' stand-ins


def export_settings(config: Any) -> dict[str, Any]:
    """Shareable settings dict: EVERY Settings field except secrets and machine-
    specific values (see ``EXPORT_EXCLUDE``). Uses ``model_dump`` so nested models
    (scheduled_loops, sdlc_stages…) serialise to plain dicts for YAML."""
    if hasattr(config, "model_dump"):
        data = config.model_dump(mode="json")
    else:  # fallback for non-pydantic configs (tests)
        data = {f.key: getattr(config, f.key, None) for f in FIELDS}
    return {k: v for k, v in data.items() if k not in EXPORT_EXCLUDE}


def export_yaml(config: Any) -> str:
    """Serialise :func:`export_settings` to a YAML document for download."""
    return yaml.safe_dump(export_settings(config), sort_keys=False, allow_unicode=True)


def export_full_settings(config: Any) -> dict[str, Any]:
    """Full settings dict INCLUDING secrets (ADO PAT, SMTP/Zalo tokens, per-tenant
    PATs…). Unlike :func:`export_settings` this does NOT apply ``EXPORT_EXCLUDE`` —
    it is meant for an encrypted backup / machine migration, not for sharing. Only
    the export/auth mechanism's own material (``FULL_EXPORT_EXCLUDE``) is dropped."""
    if hasattr(config, "model_dump"):
        data = config.model_dump(mode="json")
    else:  # fallback for non-pydantic configs (tests)
        data = {f.key: getattr(config, f.key, None) for f in FIELDS}
    return {k: v for k, v in data.items() if k not in FULL_EXPORT_EXCLUDE}


def export_full_encrypted(config: Any, password: str) -> bytes:
    """Encrypt :func:`export_full_settings` (as YAML) under ``password``.

    Returns the encrypted envelope bytes for download; decrypt with the same
    password via ``ai_autopilot.security.decrypt_bytes``."""
    from ai_autopilot import security

    body = yaml.safe_dump(export_full_settings(config), sort_keys=False, allow_unicode=True)
    return security.encrypt_bytes(body.encode("utf-8"), password)


def import_settings(raw: str, valid_keys: set[str]) -> dict[str, Any]:
    """Parse an uploaded YAML config into an updates dict.

    Keeps only known Settings keys, and defensively drops secrets / machine-
    specific keys (``EXPORT_EXCLUDE``) even if the file contains them.
    """
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ValueError("Config file must be a YAML mapping")
    return {
        k: v for k, v in data.items() if k in valid_keys and k not in EXPORT_EXCLUDE
    }


def import_full_settings(blob: bytes, password: str, valid_keys: set[str]) -> dict[str, Any]:
    """Decrypt a full-export ``.enc`` blob (from :func:`export_full_encrypted`) and
    parse it into an updates dict.

    Unlike :func:`import_settings` this KEEPS secrets and machine-specific values —
    a full export is a deliberate backup/restore, not a share. Only keys that aren't
    valid Settings fields are dropped. The mechanism's own keys (export password,
    dashboard hash) were never in the export, so a restore never clobbers the target
    host's own credentials. Raises :class:`ValueError` on a wrong password / corrupt
    file (from :func:`security.decrypt_bytes`) or non-mapping YAML."""
    from ai_autopilot import security

    raw = security.decrypt_bytes(blob, password).decode("utf-8")
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ValueError("Config file must be a YAML mapping")
    return {k: v for k, v in data.items() if k in valid_keys}


def sections() -> list[tuple[str, list[Field]]]:
    """Return fields grouped by section, preserving declaration order."""
    grouped: dict[str, list[Field]] = {}
    for f in FIELDS:
        grouped.setdefault(f.section, []).append(f)
    return list(grouped.items())


def parse_form(form: Mapping[str, Any]) -> dict[str, Any]:
    """Coerce a submitted form into a typed updates dict.

    - checkboxes: present → True, absent → False
    - password: blank → omitted (keeps the existing secret)
    - int: blank/invalid → omitted
    """
    updates: dict[str, Any] = {}
    for f in FIELDS:
        if f.kind == "bool":
            updates[f.key] = f.key in form
        elif f.kind == "password":
            value = str(form.get(f.key, "")).strip()
            if value:
                updates[f.key] = value
        elif f.kind == "int":
            value = str(form.get(f.key, "")).strip()
            if value:
                with contextlib.suppress(ValueError):
                    updates[f.key] = int(value)
        elif f.kind == "float":
            value = str(form.get(f.key, "")).strip()
            if value:
                with contextlib.suppress(ValueError):
                    updates[f.key] = float(value)
        elif f.kind == "list":
            raw = str(form.get(f.key, ""))
            updates[f.key] = [x.strip() for x in re.split(r"[,\n]", raw) if x.strip()]
        elif f.kind == "stateset":
            updates[f.key] = parse_states(form, f.key)
        elif f.kind == "map":
            updates[f.key] = parse_map(form.get(f.key, ""))
        else:  # text, select
            updates[f.key] = str(form.get(f.key, "")).strip()
    return updates


def parse_map(raw: Any) -> dict[str, str]:
    """Parse a ``key => value`` textarea into a dict (one pair per line).

    Lines without ``=>`` or with a blank key are ignored; later duplicates win.
    """
    out: dict[str, str] = {}
    for line in str(raw).splitlines():
        if "=>" not in line:
            continue
        key, value = line.split("=>", 1)
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def parse_states(form: Mapping[str, Any], key: str) -> list[str]:
    """Collect a state set: ticked ``<key>__<state>`` checkboxes plus any custom
    states typed into the ``<key>__manual`` textarea, de-duplicated in order.

    The full candidate set is carried in ``_all_states__<key>`` (comma-joined), so
    a state is selected when its checkbox is present in the form.
    """
    candidates = [s for s in str(form.get(f"_all_states__{key}", "")).split(",") if s]
    picked = [s for s in candidates if f"{key}__{s}" in form]
    raw_manual = str(form.get(f"{key}__manual", ""))
    manual = [x.strip() for x in re.split(r"[,\n]", raw_manual) if x.strip()]
    out: list[str] = []
    for s in [*picked, *manual]:
        if s not in out:
            out.append(s)
    return out


def parse_repos(form: Mapping[str, Any]) -> list[str]:
    """Collect the ticked repo whitelist from ``repo__<name>`` checkboxes.

    The form carries the full discovered set in ``_all_repos`` (comma-joined); a
    repo is allowed when its checkbox is present. None ticked → ``[]`` (= all).
    """
    all_repos = [r for r in str(form.get("_all_repos", "")).split(",") if r]
    return [r for r in all_repos if f"repo__{r}" in form]


def parse_webhook_channels(form: Mapping[str, Any]) -> list[dict]:
    """Collect the Teams channel rows from ``wh{i}_name`` / ``wh{i}_url`` / ``wh{i}_active``.

    ``wh_count`` bounds ``i``. A row with a blank URL is dropped rather than reported: the
    page always renders one empty row so a channel can be added without a round trip, and an
    untouched empty row must not become an error. A ticked ``wh{i}_delete`` drops the row too.

    ``active`` is written explicitly (not omitted when off) so the saved YAML states the
    switch either way — a muted channel that merely *lacked* the key would read as an
    oversight, and the default is on.
    """
    try:
        count = int(str(form.get("wh_count", "0")))
    except ValueError:
        count = 0
    out: list[dict] = []
    for index in range(max(0, count)):
        prefix = f"wh{index}_"
        if form.get(f"{prefix}delete"):
            continue
        url = str(form.get(f"{prefix}url", "") or "").strip()
        if not url:
            continue
        out.append({
            "name": str(form.get(f"{prefix}name", "") or "").strip(),
            "url": url,
            "active": bool(form.get(f"{prefix}active")),
        })
    return out


def save_to_yaml(path: Path, updates: Mapping[str, Any]) -> None:
    """Merge ``updates`` into the YAML config file (creating it if needed)."""
    data: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    data.update(updates)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


# Never reset, whatever the scope. Losing these does not restore a default — it locks
# you out of the machine you are resetting (the dashboard password), orphans its data
# (the database), or silently disconnects it from the fleet it belongs to. A reset is
# for "put this configuration back the way it came", not "make this host unreachable".
RESET_KEEP = frozenset({
    "dashboard_auth_password_hash", "dashboard_auth_token", "config_export_password",
    "database_url", "health_host", "health_port",
    "fleet_role", "fleet_central_url", "fleet_token", "fleet_worker_name",
    "fleet_local_keys", "fleet_sync_interval_minutes", "fleet_offline_after_minutes",
    "fleet_accept_commands", "fleet_accept_remote_update",
})


def reset_plan(config: Any, section: str = "") -> dict[str, Any]:
    """What a reset would change, as ``{key: default}`` — nothing applied yet.

    Returned rather than performed so the page can SHOW the damage before anyone
    agrees to it. "Reset" with no preview is the one destructive button people press
    by accident, and a config file is exactly the thing nobody has a backup of.

    ``section`` limits it to one heading on the page; blank means every field. Only
    keys whose value actually differs from the default appear, so the preview is the
    list of things that will really move and an empty result honestly means "this is
    already stock".
    """
    defaults = model_defaults(config)
    wanted = [f for f in FIELDS if not section or f.section == section]
    out: dict[str, Any] = {}
    for f in wanted:
        if f.key in RESET_KEEP or f.key not in defaults:
            continue
        # A password field is never echoed to the page, so "is it different" cannot be
        # asked of the form — ask the live config instead.
        current = getattr(config, f.key, None)
        if current != defaults[f.key]:
            out[f.key] = defaults[f.key]
    return out


def run_now_tag_conflict(stage_entry_tag: str, config: Any) -> str:
    """Why this run-now tag must not be saved, or "" when it is fine.

    One collision is destructive rather than merely confusing. The run-now sweep reads
    every item carrying a TRIGGER tag and REMOVES the tag it matched on (one-shot: it
    is consumed on pickup). Set the run-now tag to the trigger tag and the first sweep
    strips ownership off every item the autopilot has — they vanish from every query it
    makes, with nothing said. That is unrecoverable by waiting; somebody has to re-tag
    the board by hand.

    The outcome tags are a softer clash: ``apply_outcome`` clears the other outcome
    tags whenever it applies one, so a run-now tag living in that set is wiped at
    random moments and the "run now" never happens.
    """
    tag = (stage_entry_tag or "").strip().lower()
    if not tag:
        return ""
    triggers = {t.strip().lower() for t in getattr(config, "effective_trigger_tags", []) or []}
    # The assignee-trigger tag is NOT in effective_trigger_tags, but it IS in the WIQL
    # the sweep reads (``_candidate_clause`` ORs it in), so an item claimed by it is
    # swept and stripped exactly like one carrying a plain trigger tag. Leaving it out
    # here would have guarded the front door and left the side one open.
    atag = str(getattr(config, "assignee_trigger_tag", "") or "").strip().lower()
    if atag:
        triggers.add(atag)
    if tag in triggers:
        return (
            "it is this machine's TRIGGER tag. The run-now sweep removes the tag it "
            "matched on, so the first pass would strip ownership from every work item "
            "the autopilot has and they would disappear from its queries."
        )
    outcomes = {
        str(t).strip().lower()
        for t in (
            getattr(config, "processed_tag", ""), getattr(config, "review_tag", ""),
            getattr(config, "escalation_tag", ""), getattr(config, "failed_tag", ""),
            getattr(config, "live_tag", ""), getattr(config, "restart_tag", ""),
        )
        if str(t).strip()
    }
    if tag in outcomes:
        return (
            "it is already one of the outcome tags. Applying any outcome clears the "
            "others, so this tag would be wiped at moments unrelated to running "
            "anything, and the run it was meant to trigger never happens."
        )
    return ""


def apply_to_config(config: Any, updates: Mapping[str, Any]) -> list[str]:
    """Apply updates to the live Settings object (so running services see them).

    ``Settings`` validates on assignment, so each value is coerced into the field's real
    type here — a ``sdlc_roles`` handed over as a dict of dicts (the shape a fleet sync
    and a form both produce) becomes a dict of ``SdlcRole`` before any reader sees it.

    One unusable value must not cost the other thirty: a central serving a single
    malformed key would otherwise leave a worker with a half-applied document and no
    idea which half. So each key is applied on its own and the failures are returned
    (and logged) rather than raised.
    """
    rejected: list[str] = []
    for key, value in updates.items():
        try:
            setattr(config, key, value)
        except Exception as exc:  # noqa: BLE001 — the bad key is the news, not a crash
            rejected.append(key)
            _log.error("setting rejected — value does not fit the field",
                       key=key, error=describe_exc(exc))
    return rejected


def reload_from_file(config: Any) -> list[str]:
    """Re-read config.yaml + env into the live Settings object, in place.

    Lets a direct edit of ``config.yaml`` (including fields not on the form, such
    as ``trigger_states`` and ``repos``) take effect without restarting the app.
    Returns the sorted list of keys whose value changed. Code changes (e.g. the
    skill router) still require a restart — only configuration is reloaded.
    """
    from ai_autopilot.config import load_settings

    fresh = load_settings()
    changed: list[str] = []
    for key in type(config).model_fields:
        new_value = getattr(fresh, key)
        if getattr(config, key) != new_value:
            changed.append(key)
        setattr(config, key, new_value)
    return sorted(changed)


# ── Policy questions ("Chính sách" scope on the Settings page) ──
#
# 180 fields are the right shape for changing ONE thing and the wrong shape for the
# conversation a team has to have once: what is this robot allowed to do on its own?
# Those answers are buried as switches named after the mechanism (`test_gate_block_when_
# not_run`), and a lead reading the page cannot tell which twenty of them are decisions
# and which hundred and sixty are plumbing.
#
# Each entry re-asks an EXISTING field as the decision it encodes. It is a second label,
# never a second input: the page renders the same control under the same name, so the
# save path, the fleet ownership rules and the search all see nothing new. A question
# whose field is removed simply drops out (the route filters against FIELDS), and a test
# fails on it so it gets deleted rather than silently counted.


# Legacy fallbacks that have a better home elsewhere. Shown on Settings ONLY when this
# machine already has a value — so it can still be read and cleared — and otherwise
# left off the page: an empty box for a setting nobody should start using is noise.
# ``parent_rollup_map`` is the flat roll-up from before per-type flows; roll-up is now
# set per type on /dashboard/flow, which also displays this legacy list.
HIDE_WHEN_EMPTY: frozenset[str] = frozenset({"parent_rollup_map"})


@dataclass(frozen=True)
class PolicyQuestion:
    key: str        # an existing Field key
    question: str   # the decision, asked the way a team lead would ask it
    why: str        # what each answer costs — one or two short sentences


POLICY_QUESTIONS: tuple[PolicyQuestion, ...] = (
    # ── How much trust it has to earn ──
    PolicyQuestion(
        "trust_ladder_enabled",
        "Có để autopilot tự kiếm quyền tự chủ theo kết quả thật không?",
        "Bật: mỗi loại việc bắt đầu ở PR nháp, merge sạch nhiều thì được nới, bị trả lại liên "
        "tiếp thì bị siết — không bao giờ vượt mức tự chủ bạn đặt."),
    PolicyQuestion(
        "risk_gate_enabled",
        "Khi một thay đổi đụng migration, auth hay hạ tầng, có bắt buộc người duyệt không?",
        "Bật (khuyên dùng): những run đó dừng lại chờ người thay vì tự chuyển tiếp, dù đang ở "
        "mức tự chủ nào."),
    PolicyQuestion(
        "circuit_breaker_failures",
        "Sau bao nhiêu run lỗi liên tiếp thì máy nên tự dừng lại?",
        "Chặn một sự cố (token hết hạn, repo hỏng) đốt tiền qua cả backlog. 0 = không bao giờ."),
    PolicyQuestion(
        "item_budget_tokens",
        "Một work item được tiêu tối đa bao nhiêu token trước khi phải hỏi người?",
        "Item vượt ngân sách sẽ dừng chờ người thay vì tự thử lại mãi. 0 = không giới hạn."),
    # ── How far it goes on its own ──
    PolicyQuestion(
        "autonomy_level",
        "Khi xử lý xong một work item, autopilot nên tự đi đến bước nào?",
        "report = chỉ comment kết quả; assisted = mở draft PR chờ người duyệt; "
        "unattended = tự mở PR. Càng tự chủ càng nhanh, nhưng càng ít điểm người kiểm."),
    PolicyQuestion(
        "execution_mode",
        "Khi chạy một task, có cần chừa chỗ cho người vào lái giữa chừng không?",
        "interactive = mỗi task là một phiên Remote-Control, bạn có thể /rc vào; "
        "headless = chạy tự động hoàn toàn, không ai gắn vào."),
    PolicyQuestion(
        "max_concurrent",
        "Khi có nhiều việc cùng lúc, autopilot nên chạy song song tối đa bao nhiêu task?",
        "Nhiều hơn = xong sớm hơn nhưng tốn tài nguyên máy và rate limit hơn. "
        "Cần khởi động lại mới có hiệu lực."),
    PolicyQuestion(
        "dry_run",
        "Có muốn autopilot chỉ chạy thử — ghi log mà không làm gì thật không?",
        "Bật = không chạy agent, không ghi gì lên ADO. Dùng khi mới cài hoặc đang thử cấu hình."),
    PolicyQuestion(
        "sdlc_loop_enabled",
        "Khi một work item đi qua nhiều stage, autopilot có nên tự chuyển tiếp giữa các "
        "stage không?",
        "Bật = đưa item qua các stage SDLC (gate, sửa lại, chuyển người, bàn giao). "
        "Tắt = chạy một lần rồi dừng. Chỉ áp dụng cho headless."),
    PolicyQuestion(
        "policy_max_files_changed",
        "Khi một lần chạy sửa quá nhiều file, autopilot nên dừng lại chờ người từ mức nào?",
        "Chặn lần chạy sửa nhiều file hơn số này — một 'small fix' viết lại nửa repo cần "
        "người xem. 0 = không giới hạn."),
    # ── What has to be proved before a PR ──
    PolicyQuestion(
        "test_gate_enabled",
        "Có bắt buộc test của repo phải xanh trước khi mở PR không?",
        "Bật = chạy test trong worktree; test đỏ sẽ chặn PR. Tắt = PR mở mà không ai chạy test."),
    PolicyQuestion(
        "test_gate_block_when_not_run",
        "Khi không chạy được test (thiếu tool, môi trường chưa sẵn sàng), autopilot nên "
        "chặn PR hay cho đi tiếp?",
        "Bật = chặn như test đỏ. Tắt = PR vẫn đi tiếp, gắn nhãn 'tests not run' để người "
        "review biết."),
    PolicyQuestion(
        "sdlc_interactive_gate",
        "Sau một phiên làm việc tương tác, có chạy test trước khi bàn giao cho role kế "
        "tiếp không?",
        "Bật = test đỏ thì item được giữ lại cho người xử lý thay vì chuyển tiếp. "
        "Theo công tắc cổng test."),
    PolicyQuestion(
        "auto_review_enabled",
        "Có cho autopilot tự review bảo mật thay đổi của chính nó trước khi mở PR không?",
        "Bật = mỗi thay đổi được rà lỗi bảo mật trước khi tới tay reviewer. "
        "Tắt = chỉ dựa vào review của người."),
    # ── When the code and the spec disagree ──
    PolicyQuestion(
        "spec_drift_enabled",
        "Khi agent tự quyết một chỗ spec mơ hồ, có cần ghi lại để BA xác nhận không?",
        "Bật = mỗi quyết định được ghi thành comment ⚠️ SPEC-DRIFT, tag và một dòng trên "
        "trang Specs. Tắt = quyết định chỉ nằm trong code."),
    PolicyQuestion(
        "spec_drift_holds_item",
        "Khi code đã lệch spec, autopilot có nên giữ item lại chờ người không?",
        "Mặc định không: lệch spec là nợ tài liệu, không phải lý do dừng giao hàng. "
        "Bật nếu team muốn spec luôn được sửa trước."),
    # ── Pull requests ──
    PolicyQuestion(
        "pr_conflict_autoresolve",
        "Khi PR của autopilot bị conflict, có cho phép nó tự gỡ conflict không?",
        "Chỉ merge target vào (không rebase, không force-push), và chỉ push khi không còn "
        "marker, test và cổng bảo mật đều qua. Ngược lại thì huỷ và hỏi người."),
    PolicyQuestion(
        "max_revisions",
        "Khi reviewer yêu cầu sửa PR nhiều lần, autopilot nên tự sửa tối đa mấy lần?",
        "Giới hạn để vòng review qua lại không chạy mãi; vượt mức thì chuyển cho người."),
    PolicyQuestion(
        "commands_from_anyone",
        "Có cho phép BẤT KỲ AI ra lệnh cho autopilot qua /command và @mention không?",
        "Bật = mọi tài khoản đều ra lệnh được. Tắt = chỉ owner và danh sách người được "
        "phép. Work item nào máy nhận vẫn theo owner."),
    # ── Scheduling and re-runs ──
    PolicyQuestion(
        "dependency_scheduling_enabled",
        "Khi các work item có link phụ thuộc, autopilot có nên chờ item đi trước xong "
        "rồi mới làm không?",
        "Bật = chờ link Predecessor và không chạy cùng lúc các item Related. "
        "Tắt = chỉ theo thứ tự ưu tiên."),
    PolicyQuestion(
        "reprocess_on_reopen",
        "Khi một item đã xử lý bị kéo về state kích hoạt, autopilot có nên làm lại không?",
        "Bật = xoá tag autopilot để item chạy lại. Tắt = item đã xử lý thì không bao giờ "
        "chạy lại."),
    PolicyQuestion(
        "learning_loop_enabled",
        "Có cho phép autopilot ghi nhớ lỗi đã bị bắt để tránh lặp lại ở lần sau không?",
        "Bật = bài học từ auto-review được đưa vào brief lần chạy sau của cùng repo. "
        "Xem ở trang Learning."),
    # ── Machines and noise ──
    PolicyQuestion(
        "fleet_accept_remote_update",
        "Có cho phép máy trung tâm cập nhật phiên bản autopilot trên máy này không?",
        "Bật = lệnh 'Cập nhật' từ trung tâm cài bản mới (đợi task đang chạy xong). "
        "Tắt nếu máy này phải được cập nhật bằng tay."),
    PolicyQuestion(
        "alert_min_severity",
        "Khi có sự kiện, autopilot nên báo người từ mức nghiêm trọng nào?",
        "info = mọi thứ đã bật · warning = chỉ việc cần xem trong ngày · "
        "critical = chỉ việc đang bị chặn."),
)
