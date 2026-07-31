"""Editable-settings form: field specs, form parsing, and persistence.

Drives the ``/dashboard/settings`` page. Pure helpers (parsing/coercion/merge)
are kept here so they can be unit-tested without a running server.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str  # text | password | int | bool | select | list | stateset | stateone | map
    section: str
    help: str = ""
    options: tuple[str, ...] = field(default_factory=tuple)


# Order here is the order rendered on the page. Sections group consecutive fields.
FIELDS: tuple[Field, ...] = (
    # ── Workspace & Repository ──
    Field("workspace_directory", "Workspace directory", "text", "Workspace & Repository",
          "Folder holding the shared .claude (skills/rules/MCP). Claude runs HERE and the agent "
          "picks which repo subfolder to edit. Blank = legacy mode (run inside one repo)."),
    Field("base_branch", "Base branch", "text", "Workspace & Repository",
          "Branch new feature branches are cut from."),
    Field("repo_descriptions", "Repo descriptions", "list", "Workspace & Repository",
          "What each repo is, so the agent picks the right one. One 'RepoName = description' per "
          "line, e.g. 'Backend-Fresh = .NET API', 'Dxfac-gitops = deploy manifests, don't edit'."),
    # ── Azure DevOps Connection ──
    Field("ado_organization", "Organization URL", "text", "Azure DevOps Connection",
          "e.g. https://dev.azure.com/your-org"),
    Field("ado_project", "Project (work items)", "text", "Azure DevOps Connection"),
    Field("code_project", "Code project (repos/PRs)", "text", "Azure DevOps Connection",
          "Project where the git repos, PRs and build pipelines live, if different from the "
          "work-item project. Blank = same. (Cross-project setup.)"),
    Field("ado_pat", "Personal Access Token", "password", "Azure DevOps Connection",
          "Leave blank to keep the current token."),
    # ── Tags & Trigger ──
    Field("trigger_tag", "Trigger tag", "text", "Tags & Trigger",
          "Work items with this tag get processed."),
    Field("assignee_trigger_tag", "Assignee trigger tag", "text", "Tags & Trigger",
          "Also process items with THIS shared tag, but only those assigned to the user below "
          "(e.g. 'ai-autopilot' shared across a team). Blank = off."),
    Field("assignee_trigger_user", "↳ handled by (assignee)", "text", "Tags & Trigger",
          "Assignee (name/email) this machine claims for the shared tag above. "
          "Blank = use the auto-transition assignee."),
    Field("trigger_states", "Trigger states", "stateset", "Tags & Trigger",
          "ADO states eligible for processing — tick from your board, or add custom ones below."),
    Field("reprocess_on_reopen", "Reprocess when reopened", "bool", "Tags & Trigger",
          "When a handled item is dragged back to a trigger state, clear its autopilot "
          "tags so it runs again. (Only trigger states the autopilot doesn't set itself.)"),
    Field("restart_tag", "♻️ Restart tag (force clean re-run)", "text", "Tags & Trigger",
          "Tag an item with this to WIPE its SDLC progress and reprocess from scratch, "
          "from any state, using your latest comments. Reopen resumes mid-loop; restart "
          "redoes from stage 0. Blank = off."),
    Field("poll_interval_seconds", "Poll interval (seconds)", "int", "Tags & Trigger"),
    # ── Outcomes → tag + state ──
    # The policy table: for each outcome, the ADO tag to add and the ADO state to
    # set. Blank = skip. This is the single source of truth for tagging + state.
    Field("state_in_progress", "⏳ In progress — ADO state", "stateone", "Outcomes → tag + state",
          "State when the autopilot starts working an item (no tag)."),
    Field("review_tag", "🔍 Review — tag", "text", "Outcomes → tag + state",
          "Tag added when a draft PR opens (awaiting review); item is held."),
    Field("state_in_review", "🔍 Review — ADO state", "stateone", "Outcomes → tag + state",
          "State when a draft PR opens (awaiting human review)."),
    Field("processed_tag", "✅ Done — tag", "text", "Outcomes → tag + state",
          "Tag added when an item is handled (also used for report / failed unless overridden)."),
    Field("resolved_state", "✅ Done — ADO state", "stateone", "Outcomes → tag + state",
          "State when an item is resolved with a PR (Resolved / Closed / Done)."),
    Field("state_report", "📝 Report — ADO state", "stateone", "Outcomes → tag + state",
          "State when a plan is commented in report mode (tag = Done tag)."),
    Field("escalation_tag", "🙋 Needs human — tag", "text", "Outcomes → tag + state",
          "Tag added when the agent escalates; held items are skipped."),
    Field("state_needs_human", "🙋 Needs human — ADO state", "stateone", "Outcomes → tag + state",
          "State when the agent escalates and holds the item for a human."),
    Field("failed_tag", "⛔ Failed — tag", "text", "Outcomes → tag + state",
          "Tag added when the autopilot gives up after retries. Blank = use the Done tag."),
    Field("state_failed", "⛔ Failed — ADO state", "stateone", "Outcomes → tag + state",
          "State when the autopilot gives up after exhausting retries."),
    # ── Board columns ──
    Field("board_review_state", "Column: Ready for review", "stateone", "Board columns",
          "Items in this ADO state show in a 'Ready for review' board column. Blank = no column. "
          "Use a state the autopilot doesn't set."),
    Field("board_deploy_state", "Column: Ready to deploy", "stateone", "Board columns",
          "Items in this ADO state show in a 'Ready to deploy' board column. Blank = no column. "
          "Use a state the autopilot doesn't set."),
    Field("done_states", "Done states (→ Done column)", "stateset", "Board columns",
          "ADO states that count as Done on the board (e.g. Ready to Testing, Closed). "
          "Items a human moved to any of these show in the Done column."),
    Field("board_max_per_column", "Max cards / column", "int", "Board columns",
          "Show at most this many cards per column, then a 'Load more'. 0 = show all."),
    Field("board_drop_map", "Drag & drop (column => tag/state)", "list", "Board columns",
          "Enable dragging cards: one 'Column => value' per line. Value is a tag, or an ADO state "
          "if prefixed with @. E.g. 'In review => autopilot-review', 'Ready to deploy => @Ready to Deploy'."),
    # ── Auto transitions ──
    Field("auto_transition_enabled", "Enable auto transitions", "bool", "Auto transitions",
          "Move the work item when its PR is merged, mark it deployed when a deploy build "
          "succeeds, and roll a parent forward as its children progress. Which state each "
          "step sets is configured PER WORK-ITEM TYPE on the State flow page."),
    Field("auto_transition_assignee", "Only for assignee (auto transitions)", "text", "Auto transitions",
          "Restrict auto transitions to work items assigned to this person (name/email substring). "
          "Blank = any assignee. Does not affect normal task processing."),
    Field("on_merge_state", "On PR merged → state (fallback)", "stateone", "Auto transitions",
          "State to set when a PR the autopilot opened is merged (also marks it done). Used only "
          "for types NO flow covers — an ADO state belongs to a type, so one value here is "
          "rejected for every type that lacks it. Configure per type at /dashboard/flow."),
    Field("parent_rollup_map", "Parent roll-up (child = parent, fallback)", "list",
          "Auto transitions",
          "One 'Child state = Parent state' per line, in progression order, e.g. "
          "'Ready to Testing = Implement Done'. The parent follows its least-advanced child, and "
          "is HELD unless every child state has a line — so a one-line map never fires. Per-type "
          "roll-up lives on the parent's flow at /dashboard/flow."),
    Field("on_deploy_state", "On deploy success → state (fallback)", "stateone",
          "Auto transitions",
          "When a deploy pipeline build succeeds, move items sitting in their merge state to "
          "this state. Blank = deploy monitor off. Per-type values at /dashboard/flow."),
    Field("deploy_pipeline_id", "Deploy pipeline id", "int", "Auto transitions",
          "ADO build definition id of the deploy pipeline. 0 = watch any successful build on the branch."),
    Field("deploy_branch", "Deploy branch", "text", "Auto transitions",
          "Branch the deploy builds run on (blank = base branch)."),
    # ── Execution & Autonomy ──
    Field("execution_mode", "Execution mode", "select", "Execution & Autonomy",
          "interactive = launch a Remote-Control Claude session per task you can /rc into and steer; "
          "headless = autonomous SDK run (no human attach).",
          ("interactive", "headless")),
    Field("autonomy_level", "Autonomy level", "select", "Execution & Autonomy",
          "report = comment only, assisted = draft PR, unattended = auto PR.",
          ("report", "assisted", "unattended")),
    Field("claude_model", "Claude model", "select", "Execution & Autonomy",
          "Model the CLI runs each task with. Blank = the bundled CLI's own default — NOT "
          "guaranteed to stay the same across CLI updates. Pick one explicitly for "
          "predictable cost/speed/quality.",
          ("", "sonnet", "opus", "fable", "haiku")),
    Field("use_worktrees", "Isolate tasks (git worktree)", "bool", "Execution & Autonomy",
          "Run each task in its own git worktree so concurrent tasks never touch your main "
          "checkout. Turn off to run in-place in the shared workspace."),
    Field("max_concurrent", "Max concurrent", "int", "Execution & Autonomy",
          "Restart required to take effect."),
    Field("task_timeout_minutes", "Task timeout (minutes)", "int", "Execution & Autonomy"),
    Field("claude_effort_task", "⚡ Effort — task runs", "select", "Execution & Autonomy",
          "How hard the model reasons on real code work. Blank = the model's default. "
          "Raise to xhigh/max for demanding refactors; only LOWER it after checking quality "
          "on your own work, since this is the path that writes code.",
          ("", "low", "medium", "high", "xhigh", "max")),
    Field("claude_effort_agentic", "⚡ Effort — agentic chat", "select", "Execution & Autonomy",
          "The Teams agent turn: real ADO lookups, but a chat reply rather than an edit. "
          "medium keeps quality at a fraction of the latency someone is waiting through.",
          ("", "low", "medium", "high", "xhigh", "max")),
    Field("claude_effort_chat", "⚡ Effort — short chat calls", "select", "Execution & Autonomy",
          "Classify an intent, reword a looked-up list, write one persona message, pick a "
          "command for an @mention. These choose or rephrase — they never reason about code — "
          "so low is nearly free of risk and noticeably faster.",
          ("", "low", "medium", "high", "xhigh", "max")),
    Field("use_specialized_agents", "🧩 Route commands to specialist agents", "bool",
          "Execution & Autonomy",
          "Send /spec /qc /security /review /test /impact to their purpose-built subagents "
          "(.claude/agents) for expert results. Degrades to the generic skill if missing."),
    Field("reuse_claude_session", "🧠 Reuse Claude session / branch", "bool",
          "Execution & Autonomy",
          "Resume the agent's conversation per branch across revise rounds — follow-ups keep "
          "prior context (cheaper, more consistent). Falls back to fresh if resume fails."),
    Field("claude_session_ttl_hours", "↳ Session reuse TTL (hours)", "int",
          "Execution & Autonomy",
          "Only resume a session this fresh; older → start clean. Default 24."),
    Field("dry_run", "Dry run", "bool", "Execution & Autonomy",
          "Log only — never execute or write to ADO."),
    # ── 🛡️ Guardrails & policy ──
    Field("policy_protected_paths", "🛡️ Protected paths (never modify)", "list",
          "🛡️ Guardrails & policy",
          "Glob patterns the autopilot must NEVER change — one per line, e.g. 'k8s/*', "
          "'.github/*', '*.env', 'Dockerfile'. A run touching any of these is blocked "
          "before a PR opens. Empty = off."),
    Field("policy_max_files_changed", "🛡️ Max files changed / run", "int",
          "🛡️ Guardrails & policy",
          "Blast-radius cap: block a run that changes more files than this (a 'small "
          "fix' rewriting half the repo needs a human). 0 = off."),
    # ── 🧪 Quality gates ──
    Field("auto_review_enabled", "Auto security review", "bool", "🧪 Quality gates"),
    Field("learning_loop_enabled", "🧠 Learning loop", "bool", "🧪 Quality gates",
          "Remember what auto-review flagged per repo and inject recent lessons into the "
          "next run's brief, so the agent stops repeating them. Off = brief unchanged."),
    Field("test_gate_enabled", "🧪 Auto-test-gate", "bool", "🧪 Quality gates",
          "Run the repo's test suite in the worktree before opening a PR; a red run blocks "
          "the PR and lowers the run score. Off = no test run."),
    Field("test_command", "↳ Test command", "text", "🧪 Quality gates",
          "Command to run the tests (in the repo worktree). Blank = auto-detect "
          "(pytest / dotnet test / npm test); no runner found = skipped, never blocks."),
    Field("test_timeout_seconds", "↳ Test timeout (seconds)", "int", "🧪 Quality gates",
          "Kill the test run after this long and treat it as failed. Default 600."),
    Field("pr_scoring_enabled", "Score each run (0–100)", "bool", "🧪 Quality gates",
          "Grade each run from objective signals; below the review threshold → hold for human."),
    Field("pr_score_auto_min", "Score ≥ this → auto-resolve", "int", "🧪 Quality gates",
          "Only at unattended autonomy. Default 85."),
    Field("pr_score_review_min", "Score < this → escalate", "int", "🧪 Quality gates",
          "Below this the run is held for a human instead of review/done. Default 60."),
    # ── 🔁 PR review & feedback ──
    Field("feedback_loop_enabled", "🔁 PR feedback loop", "bool", "🔁 PR review & feedback",
          "Watch open autopilot PRs for new human review comments and auto-revise the branch to "
          "address them. Restart required to take effect."),
    Field("max_revisions", "↳ Max PR revisions / item", "int", "🔁 PR review & feedback",
          "Cap auto-revisions per work item so a review back-and-forth can't run away. Default 3."),
    Field("pr_reviewer_tracking_enabled", "👀 Track PR reviewers", "bool",
          "🔁 PR review & feedback",
          "Watch reviewer lists on ALL active PRs: dashboard status, auto-review when the bot "
          "is added as reviewer, polite overdue reminders. Restart required."),
    Field("pr_auto_review_on_added", "↳ Auto-review when bot added", "bool",
          "🔁 PR review & feedback",
          "Bot added as PR reviewer → structured AI review + vote. Re-arms on new commits."),
    Field("pr_reviewer_reminder_hours", "↳ Remind reviewers after (hours)", "int",
          "🔁 PR review & feedback",
          "A reviewer with no vote after this many hours gets one polite PR reminder. 0 = off."),
    Field("pr_reviewer_reminder_repeat_hours", "↳ Repeat the reminder every (hours)", "int",
          "🔁 PR review & feedback",
          "Keep nudging a reviewer who still hasn't voted, this many hours after the last "
          "reminder. 0 = nudge once then stay quiet."),
    Field("pr_advisory_max_per_commit", "↳ Max advisory reviews / commit", "int",
          "🔁 PR review & feedback",
          "How often /review (and other comment-only commands) may run against the SAME "
          "commit. Re-reviewing unchanged code repeats itself; push a commit to reset. "
          "0 = unlimited."),
    Field("pr_auto_review_max_per_pr", "↳ Max auto-reviews / PR", "int",
          "🔁 PR review & feedback",
          "Lifetime ceiling on auto-reviews for one PR. Auto-review re-arms on every new "
          "commit, so a push-heavy PR can otherwise be reviewed many times. 0 = unlimited."),
    Field("pr_review_max_concurrent", "↳ Max parallel PR reviews", "int",
          "🔁 PR review & feedback",
          "Concurrency cap for PR review work, separate from Max concurrent so a batch of "
          "PRs cannot starve task execution. 0 = share Max concurrent."),
    Field("pr_bot_identity", "↳ Bot identity override", "text", "🔁 PR review & feedback",
          "Email / uniqueName of the bot reviewer account. Blank = auto-detect the PAT's own "
          "identity via connectionData."),
    Field("pr_reviewer_target_branches", "↳ Only these target branches", "list",
          "🔁 PR review & feedback",
          "One branch per line (e.g. dxfac/development). Only PRs merging INTO these branches "
          "are tracked / reviewed / shown. Empty = all targets."),
    Field("comment_reprocess_enabled", "💬 React to WI comments", "bool", "🔁 PR review & feedback",
          "A new human comment on an autopilot-owned item (held / in review / done) re-runs it "
          "with your comment as top-priority guidance — no restart tag needed."),
    Field("max_comment_rounds", "↳ Max comment rounds / item", "int", "🔁 PR review & feedback",
          "Cap human↔bot comment rounds per item so a back-and-forth can't run away. Default 5."),
    Field("comment_mention_enabled", "↳ Answer an @mention on a PR", "bool", "🔁 PR review & feedback",
          "Treat an @mention of the bot on a pull request as addressing it, with no /command "
          "needed — how a human naturally asks a teammate. The intent is inferred into one of "
          "the /commands and defaults to ADVISORY, so an ambiguous mention never becomes a "
          "code change and push."),
    # ── Dependency scheduling ──
    Field("dependency_scheduling_enabled", "Order by link graph", "bool",
          "Dependency scheduling",
          "Wait on Predecessor links, never run Related items together (0 tokens). "
          "Off = plain priority order."),
    Field("sibling_conflict_scheduling", "Sibling soft-conflict", "bool",
          "Dependency scheduling",
          "Treat same-Parent + same-category siblings as a soft conflict even without a link."),
    Field("scheduler_max_dispatch", "Max dispatch / cycle", "int", "Dependency scheduling",
          "Cap items marked ready per poll cycle. 0 = no cap (max_concurrent still throttles)."),
    Field("scheduler_use_ai_conflicts", "Use AI-found conflicts", "bool", "Dependency scheduling",
          "Feed hidden conflicts the Planning Analyze confirmed back into scheduling as "
          "soft-conflicts, so the poller won't run those items concurrently."),
    Field("scheduler_ai_conflict_min_score", "AI conflict min score", "int", "Dependency scheduling",
          "Only AI conflicts scoring at least this (0–100) affect scheduling. Default 60."),
    Field("scheduler_history_limit", "History to keep", "int", "Dependency scheduling",
          "How many recent scheduling decisions (that held work back) to keep for the "
          "Planning history panel. 0 = keep only the live view."),
    # ── Closed-loop SDLC (v2) ──
    Field("sdlc_loop_enabled", "Enable SDLC loop", "bool", "Closed-loop SDLC (v2)",
          "Drive items through profile-selected SDLC stages (gate + revise + escalate + handoff). "
          "Off = one-shot behaviour, unchanged. Headless only."),
    Field("sdlc_profile", "This machine's profile", "select", "Closed-loop SDLC (v2)",
          "Role this machine runs. Blank = fall through to type-map / default.",
          ("", "ba", "dev", "qc", "review", "design", "full")),
    Field("sdlc_default_profile", "Default profile", "select", "Closed-loop SDLC (v2)",
          "Used when neither a per-item sdlc:* tag nor this machine's profile resolves.",
          ("full", "dev", "ba", "qc", "review", "design")),
    Field("sdlc_max_iterations", "Max revise iterations", "int", "Closed-loop SDLC (v2)",
          "Shared budget across all stages of one item before escalating to a human. Default 3."),
    Field("sdlc_advance_on_draft", "Advance on draft PR", "bool", "Closed-loop SDLC (v2)",
          "Apply the handoff state even for a draft PR. Off = a draft awaits human review."),
    Field("sdlc_profile_states", "Handoff (profile => state)", "map",
          "Closed-loop SDLC (v2)",
          "One 'profile => ADO state' per line — set when that profile completes, so the next "
          "machine's trigger_states picks it up. E.g. 'ba => Ready for Dev'."),
    Field("sdlc_type_profiles", "Type → profile", "map",
          "Closed-loop SDLC (v2)",
          "Optional: map a work-item type to a profile, e.g. 'Bug => dev', 'User Story => full'."),
    # ── Planning workbench ──
    Field("planning_ai_analysis", "AI conflict analysis", "bool", "Planning workbench",
          "The Analyze action runs bounded Claude judges over keyword-overlapping pairs "
          "(tokens). Off = link-graph grouping only (0 tokens)."),
    Field("planning_ai_max_pairs", "AI max pairs / analyze", "int", "Planning workbench",
          "Cap on how many suspicious pairs get an AI judge per Analyze click. Default 6."),
    Field("planning_ai_min_score", "AI min score to flag", "int", "Planning workbench",
          "A judge verdict must score at least this (0–100) to be shown. Default 50."),
    Field("planning_ai_timeout_seconds", "AI judge timeout (s)", "int", "Planning workbench",
          "Per-judge Claude timeout during Analyze. Default 120."),
    Field("conflict_ai_min_token_len", "Keyword min length", "int", "Planning workbench",
          "Shortest keyword the pre-filter considers when pairing items. Default 4."),
    Field("conflict_ai_extra_stopwords", "Extra stopwords", "list", "Planning workbench",
          "Project-specific noise words to ignore when matching keywords (comma/newline)."),
    Field("planning_schedule_default_hour", "Schedule default hour", "int", "Planning workbench",
          "Hour (0–23, local) pre-filled in the Schedule date-time picker. Default 21."),
    Field("planning_load_limit", "Load limit", "int", "Planning workbench",
          "Max work items the Load button fetches for an assignee. Default 200."),
    Field("planning_live_refresh_seconds", "Live schedule refresh (s)", "int", "Planning workbench",
          "Auto-refresh the read-only Live schedule panel every N seconds. 0 = off."),
    Field("planning_start_state", "Start → state", "stateone", "Planning workbench",
          "State the Start action moves an item to (so the poller picks it up) if it isn't "
          "already in a trigger state. Blank = the first trigger state."),
    # ── Notifications ──
    Field("teams_webhook_url", "MS Teams webhook URL", "password", "Notifications",
          "One-way channel: Teams Workflows \"Post to a channel when a webhook request is "
          "received\" URL. Started/completed/error notices and reviewer reminders post here. "
          "Blank = Teams notifications off."),
    # `teams_webhook_urls` (the old bare-URL textarea) is deliberately NOT a Field any more:
    # the "📣 Teams channels" card above edits `teams_webhook_channels` instead, with a name
    # and an active switch per channel. Leaving it out of FIELDS means parse_form never emits
    # the key, so an existing value is preserved rather than wiped — it is still honoured by
    # `teams_webhook_targets`, and the card is seeded from it so nothing is stranded.
    Field("smtp_host", "SMTP host", "text", "Notifications", "Blank = email off."),
    Field("smtp_port", "SMTP port", "int", "Notifications", "Default 587 (STARTTLS)."),
    Field("smtp_user", "SMTP user", "text", "Notifications"),
    Field("smtp_password", "SMTP password", "password", "Notifications"),
    Field("email_from", "Email from", "text", "Notifications"),
    Field("email_to", "Email to", "text", "Notifications", "Recipient address(es)."),
    Field("zalo_oa_access_token", "Zalo OA access token", "password", "Notifications",
          "Blank = Zalo off."),
    Field("zalo_recipient_user_id", "Zalo recipient user id", "text", "Notifications"),
    # ── 💬 Teams bot (2-way chat) ──
    Field("teams_agent_enabled", "💬 Two-way Teams bot", "bool", "💬 Teams bot (2-way chat)",
          "Reply and act on button clicks in Teams (approve/reject, chat commands) via a "
          "registered Azure Bot / Agent ID — fill in the App ID/tenant/secret fields below. "
          "Also requires `pip install .[teams-bot]`. Restart required."),
    Field("bot_persona_name", "🎭 Bot persona name", "text", "💬 Teams bot (2-way chat)",
          "How the bot refers to itself in Teams replies (e.g. 'AI Autopilot'). "
          "Used when it composes ticket acknowledgements / free-text answers."),
    Field("bot_persona_voice", "↳ Bot persona voice", "text", "💬 Teams bot (2-way chat)",
          "Tone/register guide handed to Claude so the bot's replies read like a "
          "consistent, proactive teammate. Blank = terse machine style."),
    Field("teams_review_skill", "↳ PR review skill", "text", "💬 Teams bot (2-way chat)",
          "Skill the bot runs to review a PR from chat (real diff-vs-codebase review "
          "that posts findings on the PR). Must exist in the workspace's .claude/skills."),
    Field("teams_agentic_enabled", "↳ Agentic free-text (Claude turn)", "bool",
          "💬 Teams bot (2-way chat)",
          "Route free-text through a real Claude agent turn (tools + skills) instead of "
          "the fixed intent classifier — more natural, but a Claude run per message."),
    Field("teams_agent_session_memory", "↳ Remember the conversation", "bool",
          "💬 Teams bot (2-way chat)",
          "Each reply continues the Claude session from the previous message in the SAME "
          "thread, so a thread behaves like a conversation instead of restating which PR or "
          "item you meant every time. Bounded by the session-reuse TTL above."),
    Field("teams_agent_max_concurrent", "↳ Max concurrent chat replies", "int",
          "💬 Teams bot (2-way chat)",
          "How many chat replies may hold a Claude process at once. Separate from 'Max "
          "concurrent' (which governs 30-minute task runs) — sharing it would put the whole "
          "team's chat in single file. 0 = no cap."),
    Field("teams_agent_nlu_enabled", "↳ Understand free-text (read-only)", "bool",
          "💬 Teams bot (2-way chat)",
          "Free-text Teams messages that don't match a /command are classified by Claude "
          "into items/prs/status/help — never an action. Costs one Claude call per "
          "unmatched message. Off = unmatched text just gets the command list."),
    Field("teams_agent_digest_interval_hours", "↳ Daily digest every (hours)", "int",
          "💬 Teams bot (2-way chat)",
          "Proactively post a full activity digest to every channel/chat the bot has "
          "been added to: autopilot run stats, auto-reviews + reminders sent, PRs "
          "opened/merged, /log tickets, PRs ready to merge, oldest stuck PRs, and a "
          "per-person work item standup. 0 = off. Requires the bot to have been "
          "messaged/added at least once so its conversation is stored (persists "
          "across restarts)."),
    Field("teams_agent_app_id", "↳ Agent (App) ID", "text", "💬 Teams bot (2-way chat)",
          "Azure Bot's Application (client) ID. Also requires `pip install .[teams-bot]`."),
    Field("teams_agent_tenant_id", "↳ Tenant ID", "text", "💬 Teams bot (2-way chat)",
          "Directory (tenant) ID the App registration lives in."),
    Field("teams_agent_app_secret", "↳ Agent app secret", "password", "💬 Teams bot (2-way chat)",
          "Client secret from Certificates & secrets on the App registration."),
    # ── Web / Security ──
    Field("dashboard_auth_password", "Dashboard password", "password", "Web / Security",
          "Password to access this dashboard (HTTP Basic — any username). Stored as a "
          "PBKDF2 hash, never plaintext. Blank = keep the current one. On first start with "
          "no password set, the CLI prompts for one."),
    Field("config_export_password", "Full-export password", "password", "Web / Security",
          "Encrypts the full config export (the download that INCLUDES secrets). You need "
          "this same password to decrypt the exported file. Blank = keep the current one."),
)

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
    "teams_webhook_url", "smtp_password", "zalo_oa_access_token",
    "dashboard_auth_password", "dashboard_auth_password_hash", "config_export_password",
})

# Keys excluded from an exported/shared config. Everything else in the Settings
# model IS exported, so new config knobs are shared automatically. Two groups:
#   • secrets — sharing them leaks credentials
#   • machine/host-specific — sharing them pins a teammate to this host's paths,
#     ports, tenants or per-host trigger tag
EXPORT_EXCLUDE = frozenset({
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
    "database_url", "health_host", "health_port", "plugins_directory",
    "trigger_tag",          # per-host default tag
    "repos",                # RepoConfig entries embed local filesystem paths
})

# Keys stripped even from the FULL (with-secrets) export: the export/auth
# mechanism's own material — embedding it would be pointless (the export key)
# or a hash of a credential rather than the credential itself.
FULL_EXPORT_EXCLUDE = frozenset({"config_export_password", "dashboard_auth_password_hash"})


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


def apply_to_config(config: Any, updates: Mapping[str, Any]) -> None:
    """Apply updates to the live Settings object (so running services see them)."""
    for key, value in updates.items():
        setattr(config, key, value)


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
