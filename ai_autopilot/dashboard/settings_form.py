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
          "Folder holding the shared .claude (skills/rules/MCP). Claude runs HERE and the agent "
          "picks which repo subfolder to edit. Blank = legacy mode (run inside one repo). "
          "This is the DEFAULT workspace — to run several, use the Workspaces page, which edits "
          "this same field as its first entry."),
    Field("base_branch", "Branch gốc", "text", "📁 Workspace & repo",
          "Branch để cắt các feature branch mới."),
    Field("repo_descriptions", "Mô tả repo", "list", "📁 Workspace & repo",
          "What each repo is, so the agent picks the right one. One 'RepoName = description' per "
          "line, e.g. 'Backend-Fresh = .NET API', 'Dxfac-gitops = deploy manifests, don't edit'."),
    # ── Azure DevOps Connection ──
    Field("ado_organization", "URL organization", "text", "🔌 Kết nối Azure DevOps",
          "vd https://dev.azure.com/your-org"),
    Field("ado_project", "Project (work item)", "text", "🔌 Kết nối Azure DevOps",
          "The DEFAULT work-item project — where new items are created and where anything "
          "without a project of its own is assumed to live. This page configures the ADO "
          "connection only; a workspace whose work items live in JIRA declares that on the "
          "Workspaces page, per workspace — pull requests stay on this connection either way."),
    Field("ado_projects", "↳ Thêm project (work item)", "list", "🔌 Kết nối Azure DevOps",
          "Additional work-item projects polled on this SAME connection (one per line). All of "
          "them are covered by a single query, so adding projects costs no extra polling. Use "
          "'Extra workspaces' above to give a project its own folder/repos; without one it uses "
          "the workspace configured here."),
    Field("code_project", "Project chứa code (repo/PR)", "text", "🔌 Kết nối Azure DevOps",
          "Project where the git repos, PRs and build pipelines live, if different from the "
          "work-item project. Blank = same. (Cross-project setup.)"),
    Field("ado_pat", "Personal Access Token (PAT)", "password", "🔌 Kết nối Azure DevOps",
          "Để trống = giữ token hiện tại."),
    # ── Tags & Trigger ──
    Field("trigger_tag", "Tag kích hoạt", "text", "🏷️ Tag & điều kiện nhận việc",
          "Work item có tag này sẽ được xử lý."),
    Field("assignee_trigger_tag", "Tag kích hoạt theo người được giao", "text",
          "🏷️ Tag & điều kiện nhận việc",
          "Also process items with THIS shared tag, but only those assigned to the user below "
          "(e.g. 'ai-autopilot' shared across a team). Blank = off."),
    Field("assignee_trigger_user", "↳ do ai xử lý (assignee)", "text",
          "🏷️ Tag & điều kiện nhận việc",
          "Assignee (name/email) this machine claims for the shared tag above. "
          "Blank = use the auto-transition assignee. This is also the OWNER: the account "
          "whose /commands and @mentions this machine obeys by default."),
    Field("command_users", "↳ người khác được ra lệnh", "list", "🏷️ Tag & điều kiện nhận việc",
          "Extra accounts (email or full name, one per line) that may issue /commands and "
          "@mentions on a PR — a teammate reviewing your PR can ask for a fix without it "
          "being refused. Does NOT change whose work items get picked up. Owner blank AND "
          "this list empty = anyone may command. Use a full email; a lone first name matches "
          "nobody (see doctor)."),
    Field("commands_from_anyone", "↳ cho BẤT KỲ AI ra lệnh", "bool", "🏷️ Tag & điều kiện nhận việc",
          "Accept /commands and @mentions from every account, without listing them above. "
          "Only opens the command gate — which work items this machine picks up is still "
          "scoped to the owner. Turn off to go back to the roster."),
    Field("trigger_states", "State kích hoạt", "stateset", "🏷️ Tag & điều kiện nhận việc",
          "Các state ADO được phép xử lý — tick từ board, hoặc thêm state riêng bên dưới."),
    Field("reprocess_on_reopen", "Chạy lại khi bị mở lại", "bool", "🏷️ Tag & điều kiện nhận việc",
          "When a handled item is dragged back to a trigger state, clear its autopilot "
          "tags so it runs again. (Only trigger states the autopilot doesn't set itself.)"),
    Field("restart_tag", "♻️ Tag chạy lại từ đầu", "text", "🏷️ Tag & điều kiện nhận việc",
          "Tag an item with this to WIPE its SDLC progress and reprocess from scratch, "
          "from any state, using your latest comments. Reopen resumes mid-loop; restart "
          "redoes from stage 0. Blank = off."),
    Field("stage_entry_tag", "▶ Tag chạy ngay (dùng chung)", "text", "🏷️ Tag & điều kiện nhận việc",
          "Tag an item with this to start the role its CURRENT state names, right where "
          "it stands — the way to run a role whose door is deliberately not in the poll "
          "query. Consumed on pickup. It names no role itself, so on a state NO role "
          "waits in it falls through to the default profile — often the whole pipeline. "
          "To start one named role from any state, give that role its own run-now tag on "
          "the Roles page. Blank = no shared tag (per-role tags still work)."),
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
          "ADO states that show in a 'Ready for review' board column. Blank = no column. "
          "Use states the autopilot doesn't set. Several are allowed — a column holds a "
          "whole leg of the ladder, not one state."),
    # Listed in board order (review → deploy → testing): the build goes onto the test
    # environment before QC can verify it, and a settings page that lists them in a
    # different order than the board teaches the wrong sequence.
    Field("board_deploy_state", "Cột: Ready for deploy", "stateset", "🗂️ Cột trên Board",
          "ADO states that show in a 'Ready for deploy' board column — approved, waiting to "
          "go onto the test environment. Blank = no column. E.g. Ready for Deploy."),
    Field("board_testing_state", "Cột: Ready for testing", "stateset", "🗂️ Cột trên Board",
          "ADO states that show in a 'Ready for testing' board column, right after Ready for "
          "deploy — it is on the test environment and QC can verify it. List every state of "
          "QC's leg here (e.g. Ready for Testing, In Testing, Ready for UAT, In UAT) so they "
          "fold into one column instead of each earning its own. Blank = no column."),
    Field("done_states", "State coi là Done (→ cột Done)", "stateset", "🗂️ Cột trên Board",
          "ADO states that count as Done on the board (e.g. Ready to Testing, Closed). "
          "Items a human moved to any of these show in the Done column."),
    Field("board_max_per_column", "Số thẻ tối đa / cột", "int", "🗂️ Cột trên Board",
          "Mỗi cột hiện tối đa bấy nhiêu thẻ, rồi có nút 'Tải thêm'. 0 = hiện hết."),
    Field("board_drop_map", "Kéo thả (cột => tag/state)", "list", "🗂️ Cột trên Board",
          "Enable dragging cards: one 'Column => value' per line. Value is a tag, or an ADO state "
          "if prefixed with @. E.g. 'In review => autopilot-review', 'Ready for deploy => @Ready for Deploy'. "
          "Empty = cards are not draggable (the board says so rather than pretending). "
          "Who reads which columns, and whose turn each one is, is configured separately at "
          "/dashboard/board-views (Board processes)."),
    # ── 🚚 Delivery (PM view) ──
    Field("delivery_history_enabled", "Ghi lịch sử state", "bool", "🚚 Bàn giao (góc nhìn PM)",
          "Log every work-item state change so the Delivery page can measure lead time, "
          "cycle time and the flow chart. Turning this OFF stops the clock — the history "
          "for that period CANNOT be recovered later."),
    Field("delivery_history_interval_minutes", "↳ Kiểm tra mỗi (phút)", "int",
          "🚚 Bàn giao (góc nhìn PM)",
          "How often to look for state changes. Two API calls per check regardless of how "
          "many items there are; a cycle where nothing moved writes nothing."),
    Field("delivery_history_retention_days", "↳ Giữ lịch sử (ngày)", "int",
          "🚚 Bàn giao (góc nhìn PM)",
          "Older transitions are dropped. This also caps how far back any trend on the "
          "page can look. 0 = keep forever."),
    Field("delivery_window_days", "Kỳ báo cáo mặc định (ngày)", "int", "🚚 Bàn giao (góc nhìn PM)",
          "Reporting period the Delivery page opens on. Each figure is compared against "
          "the window immediately before it."),



    Field("delivery_max_items", "Số work item đọc mỗi lần", "int", "🚚 Bàn giao (góc nhìn PM)",
          "Most-recently-changed first. An item that has not changed cannot have changed "
          "state, so this only bounds cost."),
    Field("dashboard_public_url", "🔗 URL công khai của dashboard", "text",
          "🚚 Bàn giao (góc nhìn PM)",
          "Where this dashboard is reachable FROM A READER'S BROWSER, e.g. "
          "https://autopilot.example.com. The Teams digest links back to the Delivery "
          "page with it. Blank = no link is offered — a digest is read on a phone, and a "
          "URL built from the bind address (0.0.0.0) resolves for nobody."),
    # ── Auto transitions ──
    Field("auto_transition_enabled", "Bật tự chuyển state", "bool", "🔀 Tự chuyển state",
          "Move the work item when its PR is merged, mark it deployed when a deploy build "
          "succeeds, and roll a parent forward as its children progress. Which state each "
          "step sets is configured PER WORK-ITEM TYPE on the State flow page."),
    Field("auto_transition_assignee", "Chỉ áp dụng cho assignee", "text", "🔀 Tự chuyển state",
          "Restrict auto transitions to work items assigned to this person (name/email substring). "
          "Blank = any assignee. Does not affect normal task processing."),
    Field("on_publish_state", "Khi PR publish (draft → ready) → state (dự phòng)",
          "stateone", "🔀 Tự chuyển state",
          "State to set when the author takes a PR OUT of draft — the moment somebody is "
          "actually being asked to look. Without it the review stage has to stand for "
          "both, so an item reads 'ready for review' while its PRs are still drafts. "
          "Blank = the publish stage does nothing. Per type at /dashboard/flow."),
    Field("on_merge_state", "Khi PR merge → state (dự phòng)", "stateone", "🔀 Tự chuyển state",
          "State to set when a PR the autopilot opened is merged (also marks it done). Used only "
          "for types NO flow covers — an ADO state belongs to a type, so one value here is "
          "rejected for every type that lacks it. Configure per type at /dashboard/flow."),
    Field("parent_rollup_map", "Cha theo con (con = cha, dự phòng)", "list",
          "🔀 Tự chuyển state",
          "One 'Child state = Parent state' per line, in progression order, e.g. "
          "'Ready to Testing = Implement Done'. The parent follows its least-advanced child, and "
          "is HELD unless every child state has a line — so a one-line map never fires. Per-type "
          "roll-up lives on the parent's flow at /dashboard/flow."),
    Field("on_deploy_state", "Khi deploy thành công → state (dự phòng)", "stateone",
          "🔀 Tự chuyển state",
          "When a deploy pipeline build succeeds, move items sitting in their merge state to "
          "this state. Blank = deploy monitor off. Per-type values at /dashboard/flow."),
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
          "The CLI is a REPL: it writes its result and then sits idle forever, so nothing "
          "closes the window on its own. pr_closed = keep it (and its scratch worktree) alive "
          "while the PR is open, so review feedback is worked in the SAME session, then close "
          "on merge/abandon. result = close as soon as the task finishes. never = leave every "
          "console open (they pile up).",
          ("pr_closed", "result", "never")),
    Field("interactive_idle_timeout_minutes", "↳ Bỏ phiên im lặng sau (phút)",
          "int", "⚙️ Thực thi & mức tự chủ",
          "A live session that has produced nothing for this long is closed and its item "
          "released back to the board with the reason. Without it a wedged session (an MCP "
          "call that never returns, a console that died) holds the item forever — the "
          "headless path has always had 'Task timeout', this is its counterpart. Counts "
          "SILENCE, not runtime, so long work is safe; keep it generous if you steer "
          "sessions by hand. The scratch worktree is kept, so pressing ▶ Run resumes. "
          "0 = no ceiling (not recommended)."),
    Field("interactive_resume_on_rework", "↳ Tiếp tục phiên cũ khi làm lại", "bool",
          "⚙️ Thực thi & mức tự chủ",
          "PR feedback runs in that session's own worktree and RESUMES its conversation, "
          "instead of a fresh worktree and a fresh read of the codebase. Claude Code keys "
          "transcripts by folder, so running anywhere else throws the context away."),
    Field("interactive_bypass_permissions", "↳ ⚠️ Bỏ qua hỏi quyền (bypassPermissions)",
          "bool", "⚙️ Thực thi & mức tự chủ",
          "The session never stops to ask before a Bash/MCP call, so it proceeds while "
          "nobody is attached. The cost: its brief is built from work-item text, so a "
          "prompt injection in a ticket runs any command on THIS machine, unasked. Off = "
          "the session uses the Permission mode like any run, and waits for whoever "
          "attaches."),
    Field("autonomy_level", "Mức tự chủ", "select", "⚙️ Thực thi & mức tự chủ",
          "report = chỉ comment, assisted = draft PR, unattended = tự mở PR.",
          ("report", "assisted", "unattended")),
    Field("claude_model", "Model Claude", "select", "⚙️ Thực thi & mức tự chủ",
          "Model the CLI runs each task with. Blank = the bundled CLI's own default — NOT "
          "guaranteed to stay the same across CLI updates. Pick one explicitly for "
          "predictable cost/speed/quality.",
          ("", "sonnet", "opus", "fable", "haiku")),
    Field("use_worktrees", "Tách riêng từng task (git worktree)", "bool",
          "⚙️ Thực thi & mức tự chủ",
          "Run each task in its own git worktree so concurrent tasks never touch your main "
          "checkout. Turn off to run in-place in the shared workspace."),
    Field("max_concurrent", "Số task chạy song song", "int", "⚙️ Thực thi & mức tự chủ",
          "Cần khởi động lại mới có hiệu lực."),
    Field("task_timeout_minutes", "Thời gian tối đa / task (phút)", "int",
          "⚙️ Thực thi & mức tự chủ"),
    Field("claude_effort_task", "⚡ Effort — chạy task", "select", "⚙️ Thực thi & mức tự chủ",
          "How hard the model reasons on real code work. Blank = the model's default. "
          "Raise to xhigh/max for demanding refactors; only LOWER it after checking quality "
          "on your own work, since this is the path that writes code.",
          ("", "low", "medium", "high", "xhigh", "max")),
    Field("claude_effort_agentic", "⚡ Effort — chat agentic", "select", "⚙️ Thực thi & mức tự chủ",
          "The Teams agent turn: real ADO lookups, but a chat reply rather than an edit. "
          "medium keeps quality at a fraction of the latency someone is waiting through.",
          ("", "low", "medium", "high", "xhigh", "max")),
    Field("claude_effort_chat", "⚡ Effort — chat ngắn", "select", "⚙️ Thực thi & mức tự chủ",
          "Classify an intent, reword a looked-up list, write one persona message, pick a "
          "command for an @mention. These choose or rephrase — they never reason about code — "
          "so low is nearly free of risk and noticeably faster.",
          ("", "low", "medium", "high", "xhigh", "max")),
    Field("use_specialized_agents", "🧩 Chuyển lệnh cho agent chuyên trách", "bool",
          "⚙️ Thực thi & mức tự chủ",
          "Send /spec /qc /security /review /test /impact to their purpose-built subagents "
          "(.claude/agents) for expert results. Degrades to the generic skill if missing."),
    Field("reuse_claude_session", "🧠 Dùng lại phiên Claude theo branch", "bool",
          "⚙️ Thực thi & mức tự chủ",
          "Resume the agent's conversation per branch across revise rounds — follow-ups keep "
          "prior context (cheaper, more consistent). Falls back to fresh if resume fails."),
    Field("claude_session_ttl_hours", "↳ Thời hạn dùng lại phiên (giờ)", "int",
          "⚙️ Thực thi & mức tự chủ",
          "Chỉ dùng lại phiên còn mới hơn mức này; cũ hơn → bắt đầu mới. Mặc định 24."),
    Field("dry_run", "Chạy thử (dry run)", "bool", "⚙️ Thực thi & mức tự chủ",
          "Chỉ ghi log — không bao giờ chạy hay ghi lên ADO."),
    # ── 🛡️ Guardrails & policy ──
    Field("policy_protected_paths", "🛡️ Đường dẫn được bảo vệ (không bao giờ sửa)", "list",
          "🛡️ Rào chắn & chính sách",
          "Glob patterns the autopilot must NEVER change — one per line, e.g. 'k8s/*', "
          "'.github/*', '*.env', 'Dockerfile'. A run touching any of these is blocked "
          "before a PR opens. Empty = off."),
    Field("policy_max_files_changed", "🛡️ Số file sửa tối đa / lần chạy", "int",
          "🛡️ Rào chắn & chính sách",
          "Blast-radius cap: block a run that changes more files than this (a 'small "
          "fix' rewriting half the repo needs a human). 0 = off."),
    # ── 🧪 Quality gates ──
    Field("auto_review_enabled", "Tự review bảo mật", "bool", "🧪 Cổng chất lượng"),
    Field("learning_loop_enabled", "🧠 Vòng học hỏi", "bool", "🧪 Cổng chất lượng",
          "Remember what auto-review flagged per repo and inject recent lessons into the "
          "next run's brief, so the agent stops repeating them. Off = brief unchanged. "
          "See what it has learned on the Learning page."),
    Field("lessons_max_injected", "↳ Số bài học đưa vào / lần chạy", "int", "🧪 Cổng chất lượng",
          "Bao nhiêu bài học gần nhất được đưa vào brief. Mặc định 8. 0 = vẫn ghi nhận nhưng "
          "không đưa vào."),
    Field("test_gate_enabled", "🧪 Cổng test tự động", "bool", "🧪 Cổng chất lượng",
          "Run the repo's test suite in the worktree before opening a PR; a red run blocks "
          "the PR and lowers the run score. Off = no test run."),
    Field("test_gate_block_when_not_run", "↳ Chặn PR khi không chạy được test",
          "bool", "🧪 Cổng chất lượng",
          "A runner that cannot start (missing tool, environment not ready) says nothing "
          "about the change. Off = the PR goes through, marked 'tests not run'. On = it is "
          "blocked like a red run."),
    Field("test_commands", "↳ Lệnh test theo từng repo", "list", "🧪 Cổng chất lượng",
          "One line per repo, <code>Repo = command</code>. A project with more than one "
          "stack cannot be served by a single command — set dotnet test and every "
          "frontend change is checked by the wrong runner, set npm test and every "
          "backend one is. A repo with no line here falls back to the command below, "
          "then to auto-detection.",
          placeholder="Backend-Fresh = dotnet test --nologo"),
    Field("test_timeouts", "↳ Timeout test theo từng repo (giây)", "list", "🧪 Cổng chất lượng",
          "One line per repo, <code>Repo = seconds</code>. A .NET solution restoring and "
          "building from a fresh worktree takes several times what a frontend unit run "
          "does; one number for both means either the backend times out or the frontend "
          "hangs for a quarter of an hour before anyone is told. A timeout BLOCKS the PR.",
          placeholder="Backend-Fresh = 1800"),
    Field("test_command", "↳ Lệnh test (dự phòng cho mọi repo)", "text", "🧪 Cổng chất lượng",
          "Command to run the tests (in the repo worktree). Blank = auto-detect "
          "(pytest / dotnet test / npm test); no runner found = skipped, never blocks."),
    Field("test_timeout_seconds", "↳ Timeout test (giây)", "int", "🧪 Cổng chất lượng",
          "Dừng test sau khoảng này và coi là thất bại. Mặc định 600."),
    Field("test_install_dependencies", "↳ Cài dependency Node trước", "bool",
          "🧪 Cổng chất lượng",
          "A fresh worktree has no node_modules, so a detected `npm test` cannot find `ng` "
          "/ `jest`. On = `npm ci` (then `npm install` if the lock is out of sync) before the "
          "suite, ~2 min on a large monorepo. Cannot install = skipped with the reason — "
          "a machine that cannot run tests is never reported as failing tests."),
    Field("pr_scoring_enabled", "Chấm điểm mỗi lần chạy (0–100)", "bool", "🧪 Cổng chất lượng",
          "Chấm điểm mỗi lần chạy từ tín hiệu khách quan; dưới ngưỡng review → giữ chờ người."),
    Field("pr_score_auto_min", "Điểm ≥ mức này → tự resolve", "int", "🧪 Cổng chất lượng",
          "Chỉ áp dụng ở mức tự chủ unattended. Mặc định 85."),
    Field("pr_score_review_min", "Điểm < mức này → chuyển người", "int", "🧪 Cổng chất lượng",
          "Dưới mức này lần chạy bị giữ chờ người thay vì review/xong. Mặc định 60."),
    # ── 🔁 PR review & feedback ──
    Field("feedback_loop_enabled", "🔁 Vòng phản hồi PR", "bool", "🔁 Review PR & phản hồi",
          "Watch open autopilot PRs for new human review comments and auto-revise the branch to "
          "address them. Restart required to take effect."),
    Field("max_revisions", "↳ Số lần sửa PR tối đa / item", "int", "🔁 Review PR & phản hồi",
          "Giới hạn số lần tự sửa mỗi work item để vòng review qua lại không chạy mãi. Mặc định "
          "3."),
    Field("pr_add_assignee_as_reviewer", "🧑‍⚖️ Thêm assignee làm reviewer PR", "bool",
          "🔁 Review PR & phản hồi",
          "When the autopilot opens a PR for a work item, add that item's ASSIGNEE as a "
          "reviewer (ADO notifies them). Best-effort — never fails the run."),
    Field("pr_extra_reviewer_ids", "↳ Reviewer thêm (identity ID)", "list",
          "🔁 Review PR & phản hồi",
          "Added to every PR on top of the assignee. ADO identity GUIDs, one per line — the "
          "reviewers API is keyed on the id, not the email."),
    Field("pr_reviewers_required", "↳ Đánh dấu là bắt buộc", "bool",
          "🔁 Review PR & phản hồi",
          "Reviewer bắt buộc chặn hoàn tất PR tới khi họ vote. Tắt = tuỳ chọn (chỉ được thông "
          "báo)."),
    Field("pr_reviewer_tracking_enabled", "👀 Theo dõi reviewer PR", "bool",
          "🔁 Review PR & phản hồi",
          "Watch reviewer lists on ALL active PRs: dashboard status, auto-review when the bot "
          "is added as reviewer, polite overdue reminders. Restart required."),
    Field("pr_auto_review_on_added", "↳ Tự review khi bot được thêm", "bool",
          "🔁 Review PR & phản hồi",
          "Bot được thêm làm reviewer PR → AI review có cấu trúc + vote. Tự bật lại khi có "
          "commit mới."),


    Field("pr_conflict_tracking_enabled", "⚔️ Theo dõi conflict merge PR", "bool",
          "🔁 Review PR & phản hồi",
          "Detect active PRs ADO reports as conflicted: one PR comment + one notification per "
          "episode, the Conflicts page, and a delivery-report row. Read-only. Restart required."),
    Field("pr_conflict_autoresolve", "↳ Tự gỡ conflict trên PR của autopilot", "bool",
          "🔁 Review PR & phản hồi",
          "On PRs from the bot's branches: merge the target in (never rebase / force-push), "
          "let the agent settle the hunks, and push ONLY if no marker is left, no other file "
          "was touched, and tests + the security gate pass. Otherwise abort and ask a person."),
    Field("pr_conflict_command", "↳ Lệnh yêu cầu gỡ conflict", "text", "🔁 Review PR & phản hồi",
          "Comment trên PR để yêu cầu gỡ conflict trên BẤT KỲ PR nào (người trong danh sách). "
          "Trống = tắt."),
    Field("pr_conflict_max_files", "↳ Số file conflict tối đa", "int", "🔁 Review PR & phản hồi",
          "Nhiều file conflict hơn mức này là va chạm cấu trúc — chuyển người mà không tốn "
          "token nào."),
    Field("pr_conflict_max_attempts", "↳ Số lần thử / commit đích", "int",
          "🔁 Review PR & phản hồi",
          "Automatic attempts against the SAME target commit (same inputs → same conflict). "
          "A new push to the target, or a person asking, allows another."),
    Field("pr_conflict_allow_preexisting_failures", "↳ Vẫn push khi branch đích đã đỏ",
          "bool", "🔁 Review PR & phản hồi",
          "Red tests after a resolution → the target branch is tested alone and the failures "
          "compared. On = push when the resolution adds NO failure of its own (the PR "
          "inherits the target's red either way). Off = escalate, naming the target as the "
          "cause. Failures the resolution ADDS always block."),
    Field("pr_conflict_poll_minutes", "↳ Quét mỗi (phút)", "int", "🔁 Review PR & phản hồi",
          "Bao lâu kiểm tra conflict trên các PR đang mở một lần."),
    Field("pr_session_hours", "Giới hạn phiên PR tương tác (giờ)", "int",
          "🔁 Review PR & phản hồi",
          "Under execution_mode interactive, a conflict resolution and an /ai action on a PR "
          "open a Remote-Control session you can attach to. With no result after this long it "
          "is closed (branch untouched) and a person is told."),
    Field("pr_advisory_max_per_commit", "↳ Số lần review góp ý tối đa / commit", "int",
          "🔁 Review PR & phản hồi",
          "How often /review (and other comment-only commands) may run against the SAME "
          "commit. Re-reviewing unchanged code repeats itself; push a commit to reset. "
          "0 = unlimited."),
    Field("pr_auto_review_max_per_pr", "↳ Số lần tự review tối đa / PR", "int",
          "🔁 Review PR & phản hồi",
          "Lifetime ceiling on auto-reviews for one PR. Auto-review re-arms on every new "
          "commit, so a push-heavy PR can otherwise be reviewed many times. 0 = unlimited."),
    Field("pr_review_max_concurrent", "↳ Số review PR song song tối đa", "int",
          "🔁 Review PR & phản hồi",
          "Concurrency cap for PR review work, separate from Max concurrent so a batch of "
          "PRs cannot starve task execution. 0 = share Max concurrent."),
    Field("pr_bot_identity", "↳ Ghi đè identity của bot", "text", "🔁 Review PR & phản hồi",
          "Email / uniqueName của tài khoản bot reviewer. Trống = tự nhận identity của PAT qua "
          "connectionData."),
    Field("pr_reviewer_target_branches", "↳ Chỉ các branch đích này", "list",
          "🔁 Review PR & phản hồi",
          "One branch per line (e.g. dxfac/development). Only PRs merging INTO these branches "
          "are tracked / reviewed / shown. Empty = all targets."),
    Field("comment_reprocess_enabled", "💬 Phản hồi comment trên work item", "bool",
          "🔁 Review PR & phản hồi",
          "Comment mới của người trên item autopilot đang giữ (held / in review / done) sẽ chạy "
          "lại item với comment đó là chỉ dẫn ưu tiên cao nhất — không cần tag restart."),
    Field("max_comment_rounds", "↳ Số vòng comment tối đa / item", "int", "🔁 Review PR & phản hồi",
          "Giới hạn số vòng comment người↔bot mỗi item để không qua lại mãi. Mặc định 5."),
    Field("pr_commands_on_any_pr", "↳ …kể cả khi bot không phải reviewer", "bool",
          "🔁 Review PR & phản hồi",
          "On a PR the autopilot did not open, it normally answers only where it was ADDED "
          "AS A REVIEWER — that invitation is the consent. Turn this on to accept being "
          "named in a comment as the consent instead, so nobody has to add the bot first. "
          "Only the people on the command roster can still command it. Costs API calls: "
          "every active PR in scope is read each cycle, not just the ones the bot sits on."),
    Field("comment_mention_enabled", "↳ Trả lời @mention trên PR", "bool",
          "🔁 Review PR & phản hồi",
          "Treat an @mention of the bot on a pull request as addressing it, with no /command "
          "needed — how a human naturally asks a teammate. The intent is inferred into one of "
          "the /commands and defaults to ADVISORY, so an ambiguous mention never becomes a "
          "code change and push."),
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
          "How many recent scheduling decisions (that held work back) to keep for the "
          "Planning history panel. 0 = keep only the live view."),
    Field("batch_related_enabled", "Gộp các item có link", "bool", "🔗 Xếp lịch theo phụ thuộc",
          "Run a linked cluster (Related / Predecessor chain) as ONE agent run that opens one "
          "branch + one PR per work item, instead of serialising it into separate waves. "
          "Headless agent mode only."),
    Field("batch_max_items", "Số item tối đa / lô", "int", "🔗 Xếp lịch theo phụ thuộc",
          "Cụm lớn nhất được gộp. Cụm lớn hơn sẽ phát từng item một. Mặc định 3."),
    Field("batch_stacked_prs", "Xếp chồng PR trong lô", "bool", "🔗 Xếp lịch theo phụ thuộc",
          "Item 2 branches off item 1 and targets it (no conflicts, fixed merge order). "
          "Off = every branch cut from the base branch (any merge order, may conflict)."),
    # ── Closed-loop SDLC (v2) ──
    # Ordered as the three questions the engine asks, in the order it asks them:
    # is the loop on, WHICH stages run, how much rework is allowed, and WHERE the
    # item goes when they finish. The two hand-off maps sit together, under the
    # switch that decides whether a draft PR delays them.
    Field("sdlc_loop_enabled", "Bật vòng SDLC", "bool", "♾️ Vòng SDLC khép kín (v2)",
          "Drive items through profile-selected SDLC stages (gate + revise + escalate + handoff). "
          "Off = one-shot behaviour, unchanged. Headless only."),
    # — which stages run, most specific first (this is the resolution order) —
    Field("sdlc_profile", "Profile: cố định cho máy này", "select", "♾️ Vòng SDLC khép kín (v2)",
          "The role THIS machine runs, whatever the item is. Blank = decide per item, below. "
          "Resolution order: an item's own 'sdlc:<profile>' tag (which the Board's ▶ Run "
          "sets) → this field → the type map → the default.",
          ("", "ba", "dev", "qc", "review", "design", "full")),
    Field("sdlc_type_profiles", "↳ Profile: theo loại work item", "map",
          "♾️ Vòng SDLC khép kín (v2)",
          "One 'work-item type => profile' per line. This is how a Bug runs end to end while "
          "a Requirement stops for a human: 'Bug => full', 'User Story => ba'.",
          placeholder="Bug => full"),
    Field("sdlc_default_profile", "↳ Profile: mặc định", "select", "♾️ Vòng SDLC khép kín (v2)",
          "Dùng khi không có gì ở trên áp dụng — không có tag trên item, không cố định profile, "
          "không có dòng theo loại.",
          ("full", "dev", "ba", "qc", "review", "design")),
    # — how much rework the engine may do before it asks a person —
    Field("sdlc_interactive_gate", "Cổng test sau phiên tương tác", "bool",
          "♾️ Vòng SDLC khép kín (v2)",
          "Interactive mode + relay on: when a session finishes, run the test gate on its "
          "branch. Red → the item is held for a person (failing tests in the comment) "
          "instead of being handed to the next role. Follows test_gate_enabled."),
    Field("sdlc_max_iterations", "Số vòng sửa tối đa", "int", "♾️ Vòng SDLC khép kín (v2)",
          "Ngân sách chung cho mọi stage của một item trước khi chuyển người. Mặc định 3."),
    # — where the item goes when the profile finishes —
    Field("sdlc_advance_on_draft", "Chuyển tiếp cả khi PR còn draft", "bool",
          "♾️ Vòng SDLC khép kín (v2)",
          "Apply the hand-offs below even when the PR is still a draft. Off (recommended) = a "
          "draft waits for human review before the next role is called."),
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
          "Where a QC run writes the test cases it wrote, relative to the WORKSPACE root — "
          "NOT inside a repo, so they never land in a pull request. '{id}' is the work item's "
          "id. The choice only helps if it is the SAME every time — blank says nothing and "
          "lets the agent pick, which is what scattered them. Default 'qc/{id}'. "
          "⚠️ Files here are outside git and live on this machine only: keep the setting "
          "below ON, or nobody but this server ever sees the cases."),
    Field("qc_create_test_case_items", "Tạo thêm work item Test Case cho mỗi case", "bool",
          "🧾 Test case QC",
          "One ADO Test Case per case, linked to the item it tests, steps on the Test tab. "
          "This is what QC actually works from — and with the path above pointing outside "
          "every repo, it is the ONLY copy that leaves this machine. Off = the workspace "
          "file alone."),
    Field("qc_create_bug_items", "Tạo Bug cho mỗi case FAIL", "bool",
          "🧾 Test case QC",
          "One ADO Bug per failing case, linked to the item QC was running against "
          "(child link, falling back to Related where the process template refuses it). "
          "Re-running does not duplicate: a case whose Bug is already linked is skipped. "
          "Off by default — a failing case is not always a product defect, it is often a "
          "wrong test or a broken environment, and those Bugs land on a shared board. "
          "Turn it on where the process requires Requirement → TC + Bug traceability."),
    # ── Planning workbench ──
    Field("planning_ai_analysis", "Phân tích xung đột bằng AI", "bool", "🧭 Lập kế hoạch",
          "The Analyze action runs bounded Claude judges over keyword-overlapping pairs "
          "(tokens). Off = link-graph grouping only (0 tokens)."),
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
          "State the Start action moves an item to (so the poller picks it up) if it isn't "
          "already in a trigger state. Blank = the first trigger state."),
    # ── Working hours & quiet time ──
    Field("timezone", "Múi giờ", "text", "🕘 Giờ làm việc",
          "IANA zone every time window below is read in, e.g. Asia/Ho_Chi_Minh. REQUIRED for "
          "quiet hours (they stay off without it). Do not rely on the machine clock: a server "
          "or container usually runs as UTC, so '18:00' there is 01:00 for a team in UTC+7."),
    Field("schedule_start", "Giờ làm việc — từ", "text", "🕘 Giờ làm việc",
          "HH:MM. When a run may START. Outside it the poller idles; work already running "
          "continues. Blank = no window (run at any hour)."),
    Field("schedule_end", "Giờ làm việc — đến", "text", "🕘 Giờ làm việc",
          "HH:MM. Giờ kết thúc SỚM hơn giờ bắt đầu nghĩa là khung qua đêm (22:00–06:00)."),
    Field("schedule_days", "Giờ làm việc — ngày", "text", "🕘 Giờ làm việc",
          "vd Mon,Tue,Wed,Thu,Fri. Trống = mọi ngày."),





    # ── Spec drift & PR traceability ──
    Field("spec_drift_enabled", "Báo lệch spec", "bool", "📐 Lệch spec & truy vết PR",
          "The agent is told to DECIDE rather than ask, so every ambiguity it resolves is a "
          "decision taken on the team's behalf that the work item does not reflect. When on, "
          "those are filed as a ⚠️ SPEC-DRIFT comment, a tag, a PR comment, and a row on "
          "/dashboard/specs a BA ticks off."),
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
          "Off by default: a drift is documentation debt, not a reason to stop delivery. Turn "
          "on to stop the item advancing while its spec is stale."),
    Field("pr_require_work_item_link", "Mỗi PR phải gắn work item", "bool",
          "📐 Lệch spec & truy vết PR",
          "Verified against ADO after the fact, and attached when missing. An instruction in "
          "the brief is advice a model can drop on a long run — and when it does, nothing "
          "notices."),

    # ── Process health ──
    Field("process_health_enabled", "Báo cáo sức khoẻ quy trình", "bool", "🩺 Sức khoẻ quy trình",
          "Computes the standing reviews a process doc assigns — share of capacity that went "
          "to unplanned work, escaped defects per module, stuck items, tag rot — and pushes "
          "the findings to the notification channels. Read-only: it reports, never edits."),
    Field("process_health_interval_hours", "↳ Mỗi (giờ)", "int", "🩺 Sức khoẻ quy trình",
          "168 = hằng tuần. 0 = chỉ tính khi được yêu cầu."),
    Field("process_health_window_days", "↳ Khoảng đo (ngày)", "int", "🩺 Sức khoẻ quy trình",
          "Mỗi lần đo nhìn lại bao xa. 14 = một sprint."),
    Field("process_health_blocked_days", "↳ Bị chặn lâu hơn (ngày)", "int",
          "🩺 Sức khoẻ quy trình",
          "Item bị gắn Blocked và không ai động tới lâu như vậy sẽ vào danh sách cần theo dõi."),
    Field("process_health_adhoc_threshold_pct", "↳ Ngưỡng cảnh báo việc phát sinh (%)", "float",
          "🩺 Sức khoẻ quy trình",
          "Flag when unplanned work exceeds this share. Ignored on small samples — one ad-hoc "
          "ticket in a quiet week is 100% and says nothing."),

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
          "Flag a PR that is approved, unblocked and STILL not merged after this long. "
          "This is pure waste — the work is finished and the fix is one click."),
    Field("delivery_review_hours", "Ngưỡng: PR chưa ai review (giờ)", "int",
          "🔔 Cảnh báo — khi nào báo",
          "Báo PR chưa ai vote sau khoảng này, kèm tên các reviewer đang được chờ."),
    Field("delivery_stale_days", "Ngưỡng: việc đứng im (ngày)", "int",
          "🔔 Cảnh báo — khi nào báo",
          "Flag an in-progress item whose STATE has not changed in this long. Edits and "
          "comments do not count as movement — that is the whole point."),
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
          "Keep nudging a reviewer who still hasn't voted, this many hours after the last "
          "reminder. 0 = nudge once then stay quiet."),
    Field("teams_agent_digest_interval_hours", "Digest định kỳ mỗi (giờ)", "int",
          "🔔 Cảnh báo — khi nào báo",
          "Proactively post a full activity digest to every channel/chat the bot has "
          "been added to: autopilot run stats, auto-reviews + reminders sent, PRs "
          "opened/merged, /log tickets, PRs ready to merge, oldest stuck PRs, and a "
          "per-person work item standup. 0 = off. Requires the bot to have been "
          "messaged/added at least once so its conversation is stored (persists "
          "across restarts)."),
    Field("teams_agent_digest_at", "↳ Hoặc gửi cố định lúc (HH:MM)", "text",
          "🔔 Cảnh báo — khi nào báo",
          "Post it at a fixed local time instead, e.g. 09:00. Wins over the interval "
          "above, which counts from process start — so a restart at 14:00 moves a 24h "
          "digest to 14:00 permanently. Blank = use the interval."),
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
          "HH:MM. When a human may be PINGED. Deliberately separate from the work window: a "
          "team is usually happy for the autopilot to keep working in the evening — what they "
          "do not want is a phone going off at 22:40 about something nobody can act on until "
          "morning. Blank = notify at any hour."),
    Field("notify_hours_end", "Khung giờ được phép báo — đến", "text", "🔔 Cảnh báo — khi nào báo",
          "HH:MM. Notices raised outside the window are HELD (never dropped) and delivered as "
          "ONE summary when it opens. ADO comments are never held — a comment is the record on "
          "the work item, not an interruption."),
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
          "Ceiling on the held queue so a quiet weekend cannot grow it without bound. Oldest "
          "are dropped first and the summary says how many."),
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
          "Reply and act on button clicks in Teams (approve/reject, chat commands) via a "
          "registered Azure Bot / Agent ID — fill in the App ID/tenant/secret fields below. "
          "Also requires `pip install .[teams-bot]`. Restart required."),
    Field("bot_persona_name", "🎭 Tên hiển thị của bot", "text", "💬 Teams bot (chat 2 chiều)",
          "How the bot refers to itself in Teams replies (e.g. 'AI Autopilot'). "
          "Used when it composes ticket acknowledgements / free-text answers."),
    Field("bot_persona_voice", "↳ Giọng văn của bot", "text", "💬 Teams bot (chat 2 chiều)",
          "Tone/register guide handed to Claude so the bot's replies read like a "
          "consistent, proactive teammate. Blank = terse machine style."),
    Field("teams_review_skill", "↳ Skill review PR", "text", "💬 Teams bot (chat 2 chiều)",
          "Skill the bot runs to review a PR from chat (real diff-vs-codebase review "
          "that posts findings on the PR). Must exist in the workspace's .claude/skills."),
    Field("teams_agentic_enabled", "↳ Chat tự do bằng agent (lượt Claude)", "bool",
          "💬 Teams bot (chat 2 chiều)",
          "Đưa tin nhắn tự do qua một lượt agent Claude thật (tools + skill) thay vì bộ phân "
          "loại ý định cố định — tự nhiên hơn, nhưng mỗi tin là một lần chạy Claude."),
    Field("teams_agent_session_memory", "↳ Nhớ mạch hội thoại", "bool",
          "💬 Teams bot (chat 2 chiều)",
          "Each reply continues the Claude session from the previous message in the SAME "
          "thread, so a thread behaves like a conversation instead of restating which PR or "
          "item you meant every time. Bounded by the session-reuse TTL above."),
    Field("teams_agent_max_concurrent", "↳ Số câu trả lời chat song song tối đa", "int",
          "💬 Teams bot (chat 2 chiều)",
          "How many chat replies may hold a Claude process at once. Separate from 'Max "
          "concurrent' (which governs 30-minute task runs) — sharing it would put the whole "
          "team's chat in single file. 0 = no cap."),
    Field("teams_agent_nlu_enabled", "↳ Hiểu tin nhắn tự do (chỉ đọc)", "bool",
          "💬 Teams bot (chat 2 chiều)",
          "Free-text Teams messages that don't match a /command are classified by Claude "
          "into items/prs/status/help — never an action. Costs one Claude call per "
          "unmatched message. Off = unmatched text just gets the command list."),


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
          "Password to access this dashboard (HTTP Basic — any username). Stored as a "
          "PBKDF2 hash, never plaintext. Blank = keep the current one. On first start with "
          "no password set, the CLI prompts for one."),
    Field("config_export_password", "Mật khẩu export đầy đủ", "password", "🔐 Web & bảo mật",
          "Encrypts the full config export (the download that INCLUDES secrets). You need "
          "this same password to decrypt the exported file. Blank = keep the current one."),
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
    manual = [x.strip() for x in re.split(r"[,\n]", str(form.get(f"{key}__manual", ""))) if x.strip()]
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
