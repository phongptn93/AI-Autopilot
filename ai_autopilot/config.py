"""Application configuration.

Ported from the .NET ``AutopilotConfig`` (bound from ``appsettings.json``).
Settings are loaded, in increasing order of precedence, from:

1. Defaults defined on the model.
2. A YAML file (``config.yaml`` by default, or the path in ``AUTOPILOT_CONFIG_FILE``).
3. Environment variables prefixed with ``AUTOPILOT_`` (nested via ``__``),
   e.g. ``AUTOPILOT_ADO_PAT`` or ``AUTOPILOT_SMTP__PASSWORD``.

Keeping secrets (PATs, SMTP/Zalo tokens) in environment variables rather than the
YAML file is strongly recommended — see ``config.example.yaml``.
"""

from __future__ import annotations

import html
import os
import re
import socket
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

# ── Bot comment identity ──────────────────────────────────────────────────────
# The autopilot posts ADO comments under the OPERATOR'S OWN identity (its PAT /
# OAuth app), so a bot comment's author is indistinguishable from a human's. To
# still tell them apart, every bot-authored comment carries this fixed signature in
# its CONTENT; detection checks for the signature, never the author. It is a module
# constant on purpose — NOT a Settings field — because if an operator could edit it,
# a stale value would silently break human-vs-bot comment detection.
BOT_ICON = "🤖"
# Bold badge stamped at the START of every bot comment, so a reader knows it's the autopilot
# at a glance. The 🤖 is the REQUIRED part — detection (is_bot_signed) keys off it, which is
# how the bot tells its own comments from a human's despite sharing the operator's identity.
# The bold wrapper + name are cosmetic (detection ignores tags via html.unescape).
BOT_COMMENT_PREFIX = f"<b>{BOT_ICON} AI-Autopilot</b> · "
# Handed to the agent so any comment IT posts (directly, via the ADO MCP) also carries the
# 🤖 marker — otherwise the poller/babysitter would read the agent's own review/RCA comment
# as a HUMAN comment, and if it happened to start with a command prefix it would re-trigger.
BOT_COMMENT_INSTRUCTION = (
    f"IMPORTANT: prefix EVERY comment you post on a work item or pull request with "
    f"'{BOT_ICON} AI-Autopilot · ' so the autopilot recognises it as automated — never as a "
    "human command. Do not start any comment with a slash command like /ai or /review."
)


def is_bot_signed(text: str | None) -> bool:
    """True if ``text`` was authored by the autopilot (leads with the bot icon).

    Detection is by CONTENT, never author — the bot posts under the operator's own ADO
    identity, so the author field is identical for bot and human. Matches ``BOT_ICON``
    after ``html.unescape`` (ADO stores comments HTML-encoded, so 🤖 comes back as
    ``&#129302;`` — this also keeps recognising older signed comments). The only rare
    false positive is a human typing the robot emoji into a comment."""
    return BOT_ICON in html.unescape(text or "")


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def match_command(text: str | None, commands: list[str] | tuple[str, ...]) -> str | None:
    """If a comment is addressed to the autopilot, return its PLAIN text; otherwise ``None``.

    ``commands`` are the accepted trigger prefixes — e.g. ``["/ai", "/review",
    "dxfactory@nois.vn"]`` — so a user can type ``/ai <instruction>``, ``/review <what>``,
    or naturally address the bot's account (``phong.pham@nois.vn: <instruction>``). A
    comment matches when its plain text starts with any prefix (case-insensitive). The
    WHOLE plain text (command + note) is returned — kept intact so the agent can interpret
    the intent (``/ai`` vs ``/review`` …) itself. HTML tags/entities are stripped first,
    since ADO stores comments as HTML. No prefix matches → ``None``."""
    plain = html.unescape(_HTML_TAG_RE.sub(" ", text or "")).strip()
    low = plain.lower()
    for cmd in commands or ():
        cmd = (cmd or "").strip()
        if cmd and low.startswith(cmd.lower()):
            return plain
    return None


def matches_user(email: str | None, name: str | None, claimed: str) -> bool:
    """True if the identity (email/display name) matches the ``claimed`` user — used so, on
    a multi-machine setup, each person's own machine handles only their own commands. A
    blank ``claimed`` (single machine / unrestricted) matches everyone. Case-insensitive
    substring so an email or a name fragment both work."""
    c = (claimed or "").strip().lower()
    return not c or c in f"{email or ''} {name or ''}".lower()


class RepoConfig(BaseModel):
    """A source repository, optionally bound to specific task categories."""

    name: str = ""
    path: str = ""
    base_branch: str = "development"
    categories: list[str] = Field(default_factory=list)


class TenantConfig(BaseModel):
    """An isolated ADO organization/project served by the same autopilot instance."""

    name: str = ""
    ado_organization: str = ""
    ado_project: str = ""
    ado_pat: str = ""
    trigger_tag: str = "autopilot"
    processed_tag: str = "autopilot-done"
    repo_working_directory: str = ""
    base_branch: str = "development"
    repos: list[RepoConfig] = Field(default_factory=list)
    teams_webhook_url: str = ""
    allowed_users: list[str] = Field(default_factory=list)
    approver_users: list[str] = Field(default_factory=list)
    allowed_skills: list[str] = Field(default_factory=list)


class SdlcStage(BaseModel):
    """One stage in the closed-loop SDLC engine (Phase 1).

    A stage runs in the item's scratch worktree, is gated, and either advances,
    revises (under the shared budget), or escalates.

    By default a stage states its ``goal`` and **Claude chooses the right skill(s)**
    from the workspace itself (the AI-native philosophy — nothing pinned). Set
    ``skill`` only to PIN a specific command (``"/implement-task-be {id}"``) or the
    special value ``"route"`` (defer to the category router) when you want
    determinism. ``gate`` ``soft`` is advisory; ``hard`` consumes budget on failure.
    """

    name: str                      # unique key, e.g. "implement"
    role: str = ""                 # ba|design|dev|qc|review|pr (free string, for display)
    goal: str = ""                 # what the stage should achieve (Claude picks the skill)
    skill: str = ""                # OPTIONAL pin: "/implement-task-be {id}" | "route" | ""
    artifact_skill: str = ""       # optional HTML producer, e.g. "technical-note" (Phase 2)
    gate: str = "hard"             # soft|hard
    produces_pr: bool = False      # this stage opens the PR


class ScheduledLoop(BaseModel):
    """A recurring autonomous loop (loop-engineering pattern).

    Runs ``prompt`` (a skill command) against a repo on a cadence — e.g. a
    dependency sweeper, changelog drafter, or CI sweeper — opening a PR with any
    resulting changes. Either ``interval_minutes`` or ``cron`` sets the cadence.
    """

    name: str
    prompt: str  # e.g. "/update-deps" or a free-form instruction
    interval_minutes: int = 0  # used when cron is empty
    cron: str = ""  # standard 5-field cron expression (overrides interval)
    repo_path: str = ""  # empty → repo_working_directory
    base_branch: str = ""  # empty → base_branch
    branch_prefix: str = "autopilot/loop"
    draft_pr: bool = True
    enabled: bool = True


def _yaml_path() -> Path:
    return Path(os.getenv("AUTOPILOT_CONFIG_FILE", "config.yaml"))


def config_file_path() -> Path:
    """Path of the YAML config file the dashboard reads from / writes to."""
    return _yaml_path()


class Settings(BaseSettings):
    """Root configuration for the autopilot service."""

    model_config = SettingsConfigDict(
        env_prefix="AUTOPILOT_",
        env_nested_delimiter="__",
        yaml_file=_yaml_path(),
        yaml_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Azure DevOps connection ──
    ado_organization: str = ""
    ado_project: str = ""              # where the WORK ITEMS live
    # Project where the git repos / PRs / build pipelines live, if different from
    # ado_project (cross-project setup: work items in one project, code in another).
    # Blank = same as ado_project.
    code_project: str = ""
    ado_pat: str = ""
    oauth_app_id: str = ""
    oauth_app_secret: str = ""

    # ── Triggering ──
    # Default is host-specific (``<hostname>-autopilot``) so each machine claims a
    # distinct stream out of the box — handy with the Board/Overview tag filter and
    # avoids two laptops fighting over the same work items. Override in config.yaml
    # to share a stream across machines.
    trigger_tag: str = Field(default_factory=lambda: f"{socket.gethostname()}-autopilot")
    # Additional trigger tags (beyond ``trigger_tag``). Items carrying ANY of the
    # effective trigger tags are processed; the dashboard can filter Board/Overview
    # by tag. Useful to run several streams (e.g. per squad) on one autopilot.
    trigger_tags: list[str] = Field(default_factory=list)
    # Additional trigger mechanism: items carrying this SHARED tag are also picked
    # up, but ONLY when assigned to ``assignee_trigger_user`` — so a whole team can
    # share one tag (e.g. "ai-autopilot") while each machine handles only its
    # owner's items. Blank = disabled (only the trigger_tag(s) above apply).
    assignee_trigger_tag: str = "ai-autopilot"
    # The assignee (name/email substring) this machine claims for the shared tag.
    # Blank → falls back to ``auto_transition_assignee``.
    assignee_trigger_user: str = ""
    processed_tag: str = "autopilot-done"
    review_tag: str = "autopilot-review"
    # Applied when the agent escalates (needs_human); these items are held — the
    # poller skips them until a human removes the tag.
    escalation_tag: str = "autopilot-hold"
    # Tag added while an interactive session is live, so the poller doesn't
    # re-dispatch it (and re-open a duplicate console) after a restart. Removed
    # when the session finalises.
    live_tag: str = "autopilot-live"
    # Tag added when the autopilot gives up after exhausting retries. Blank = reuse
    # processed_tag (so the item is still marked handled and not re-polled).
    failed_tag: str = ""
    # Human control: tag an item with this to force a CLEAN re-run — wipe its saved
    # SDLC loop progress and reprocess from stage 0 (from ANY state), reading the
    # item's latest comments as fresh intent. The deliberate counterpart to reopen,
    # which *resumes* mid-loop. Blank = feature off.
    restart_tag: str = "autopilot-restart"
    # ADO ``System.State`` set at each pipeline stage. Blank = leave the work
    # item's ADO state unchanged for that stage (only tags/board move). These apply
    # in every execution mode (interactive / assisted / unattended). Match your
    # board's template (e.g. Agile: Active / Resolved / Closed).
    state_in_progress: str = "Active"   # when the autopilot starts working an item
    state_in_review: str = ""           # when a draft PR opens (awaiting review)
    state_needs_human: str = ""         # when escalated to a human (held)
    state_report: str = ""              # when a plan is commented in report mode
    state_failed: str = ""              # when the autopilot gives up after retries
    # ADO state set when an item is successfully resolved (Done with a PR).
    resolved_state: str = "Resolved"
    # Extra Board columns driven purely by the item's ADO state (read-only — the
    # autopilot does not set these; a human moves the item there). Blank = no
    # column. Use states the autopilot itself doesn't set (e.g. "Ready to Deploy").
    board_review_state: str = ""   # → "Ready for review" column
    board_deploy_state: str = ""   # → "Ready to deploy" column
    # ADO states that count as Done on the board (e.g. a human moved the item to
    # "Ready to Testing" / "Closed"). Items in any of these states show in the Done
    # column regardless of tags. Read-only: does not change the item.
    done_states: list[str] = Field(default_factory=list)
    # Drag & drop on the board: what to apply when a card is dropped in a column.
    # Each entry "Column => value" — a tag by default, or an ADO state if prefixed
    # with @, e.g. "In review => autopilot-review", "Done => @Closed". Columns not
    # listed are not drop targets.
    board_drop_map: list[str] = Field(default_factory=list)
    # When a human drags a handled item back to a trigger state (one the autopilot
    # never sets), clear its skip tags so it gets reprocessed. Guarded against loops:
    # only reopens on trigger states that are NOT the autopilot's own output states.
    reprocess_on_reopen: bool = True
    # Auto state transitions (opt-in). When a PR the autopilot opened is merged,
    # move the work item to on_merge_state + mark it done; when ALL children of a
    # parent are done, move the parent to parent_done_state. Blank state = skip that.
    auto_transition_enabled: bool = False
    # Branch prefixes the autopilot/agent uses — a PR is recognised as "ours" (for
    # merge detection + PR babysitter) when its source branch starts with one of these.
    bot_branch_prefixes: list[str] = Field(
        default_factory=lambda: ["feature/", "fix/", "bugfix/", "autopilot/"]
    )
    # Extra gate for auto transitions ONLY (not the main poller): restrict them to
    # work items assigned to this person (name/email substring, case-insensitive).
    # Blank = any assignee.
    auto_transition_assignee: str = ""
    on_merge_state: str = ""
    # Parent roll-up: an ordered "child state = parent state" map, e.g.
    # ["Active = Active", "Ready for Testing = Impl Done"]. Child and parent are
    # DIFFERENT work-item types with different state names. The parent follows its
    # LEAST-advanced child: parent = the parent-state mapped from the lowest child
    # stage. Only rolls up when every child is within the map. Empty = fall back to
    # the simple "all children done → parent_done_state".
    parent_rollup_map: list[str] = Field(default_factory=list)
    # Deploy monitor: when a deploy pipeline build succeeds, move items sitting in
    # on_merge_state to on_deploy_state. Blank on_deploy_state = monitor off.
    on_deploy_state: str = ""
    deploy_pipeline_id: int = 0     # ADO build definition id (0 = any build on the branch)
    deploy_branch: str = ""         # branch the deploy builds run on (blank = base_branch)
    poll_interval_seconds: int = 30
    # ADO work-item states that are eligible for processing. The default suits the
    # Agile/Scrum/Basic templates; adjust to match your board (e.g. add "Doing",
    # "Committed", "Approved", or localized state names).
    trigger_states: list[str] = Field(
        default_factory=lambda: ["New", "To Do", "Proposed", "Active"]
    )

    # ── Repository / execution ──
    repo_working_directory: str = ""
    # Directory Claude Code runs in (its cwd). When set, this should be the
    # *workspace* root that holds the shared ``.claude/`` (skills, rules, MCP) and
    # the source repos as subfolders — Claude needs to run from here to see the
    # project skills/rules, then edits the target repo subfolder. Blank → Claude
    # runs inside the repo/worktree itself (legacy behaviour).
    workspace_directory: str = ""
    base_branch: str = "development"
    # Repos (subfolders of the workspace) the agent is allowed to edit. Empty =
    # all discovered repos. Acts as a safety whitelist in AI-native mode.
    allowed_repos: list[str] = Field(default_factory=list)
    # What each repo is, so the agent picks the right one. Each entry
    # "RepoName = description", e.g. "Backend-Fresh = .NET API" / "Dxfac-gitops =
    # deploy manifests, don't edit for features". Shown in the agent brief.
    repo_descriptions: list[str] = Field(default_factory=list)
    repos: list[RepoConfig] = Field(default_factory=list)
    max_concurrent: int = 1
    task_timeout_minutes: int = 30
    dry_run: bool = False
    # Run each task in its own git worktree so concurrent tasks never share a
    # checkout — and, in AI-native (workspace) mode, so the agent never touches
    # your MAIN checkout (it works in an isolated scratch copy). Required for safe
    # max_concurrent > 1. Disable to run in-place in the repo / shared workspace.
    use_worktrees: bool = True
    worktrees_dir: str = ""  # empty → <system temp>/ai-autopilot-worktrees

    # ── Claude execution ──
    # How a work item is executed:
    #   "headless"    – autonomous Agent SDK run, no human interaction
    #   "interactive" – launch a real Claude Code session per task (Remote Control
    #                   enabled) the human can attach to / steer (default)
    execution_mode: str = "interactive"
    claude_model: str = ""  # empty → SDK default
    claude_max_turns: int = 0  # 0 → unbounded
    # Permission mode for autonomous runs. "acceptEdits" works when the process
    # runs as root (e.g. containers); "bypassPermissions" is fully autonomous but
    # the underlying CLI refuses to run it as root for safety — use it only when
    # the service runs as a non-root user.
    claude_permission_mode: str = "acceptEdits"
    claude_allowed_tools: list[str] = Field(default_factory=list)  # empty → all tools

    # ── Retry & recovery ──
    max_retries: int = 3
    retry_backoff_seconds: int = 60

    # ── Approval gate / autonomy ──
    # autonomy_level is the primary autonomy knob (loop-engineering L1/L2/L3):
    #   "report"     – classify + comment what it *would* do, no code changes
    #   "assisted"   – execute and open a DRAFT PR for human review (default)
    #   "unattended" – execute and open a normal PR, auto-resolving the item
    autonomy_level: str = "assisted"
    require_approval: bool = True  # legacy flag; superseded by autonomy_level
    approval_timeout_minutes: int = 120

    # ── RBAC ──
    allowed_users: list[str] = Field(default_factory=list)
    approver_users: list[str] = Field(default_factory=list)
    allowed_skills: list[str] = Field(default_factory=list)

    # ── Multi-tenant ──
    tenants: list[TenantConfig] = Field(default_factory=list)

    # ── Plugins ──
    plugins_directory: str = "plugins"

    # ── Cost tracking ──
    daily_budget_tokens: int = 0
    cost_alert_enabled: bool = False

    # ── Decomposition ──
    auto_decompose: bool = True

    # ── Dependency-aware scheduling (P1) ──
    # Order work by the ADO link graph instead of dispatching every pending item at
    # once: an item waits for its Predecessor (Dependency-Reverse) links to be done,
    # and two Related items never run concurrently (they likely touch the same area,
    # which would fight in git). Deferred items are re-evaluated next poll — waves
    # emerge across cycles. No tokens: it reads links, it does not call Claude.
    dependency_scheduling_enabled: bool = True
    # Heuristic add-on: treat sibling candidates (same Parent + same repo-category,
    # e.g. two [BE] tasks under one Requirement) as a Related soft-conflict even when
    # the BA didn't link them. Off by default — enable if siblings often collide.
    sibling_conflict_scheduling: bool = False
    # Cap on how many items the dependency scheduler marks *ready* per poll cycle.
    # 0 = no cap (the executor's max_concurrent semaphore still throttles real
    # parallelism). Set >0 to smooth bursts into smaller waves.
    scheduler_max_dispatch: int = 0
    # Feed AI-confirmed hidden conflicts back into scheduling: pairs the Planning
    # workbench's Analyze flagged (score ≥ threshold) become Related soft-conflicts
    # for the poller too, so the autopilot avoids running them concurrently.
    scheduler_use_ai_conflicts: bool = True
    scheduler_ai_conflict_min_score: int = 60
    # How many recent scheduling decisions to keep (for the Planning dashboard's
    # history / trend view). 0 = keep only the latest.
    scheduler_history_limit: int = 20

    # ── Feedback loop / PR babysitter ──
    feedback_loop_enabled: bool = False
    max_revisions: int = 3
    # After a /command changes the PR it was left on (e.g. the BE PR), also review the work
    # item's OTHER open DRAFT PRs (e.g. the FE PR) and adjust each only where the agent
    # judges it must stay consistent with that change. Draft-only = safe (never touches a
    # PR that's ready/merged). Off = handle only the PR the command was on.
    pr_adjust_related_drafts: bool = True
    # How often (seconds) the PR babysitter scans open PRs for new /command comments.
    pr_poll_interval_seconds: int = 15
    # Local-friendly fast lane (no webhook needed): once the bot engages a PR (a /command
    # was handled there), THAT PR alone is re-polled at this cadence, so follow-up replies
    # ("/ai fix issue 2" under the bot's review) are picked up chat-fast. The global scan
    # keeps its slower interval.
    pr_hot_poll_interval_seconds: int = 3
    # ...and stays on the fast lane this long after its last activity, then cools down.
    pr_hot_window_minutes: int = 10
    # Reuse ONE worktree per (item, branch) across /ai revise rounds instead of
    # materialising + tearing down a fresh checkout every command. Follow-ups start in
    # seconds and ignored build artefacts (node_modules, bin/obj) stay warm. The cached
    # worktree is reset hard to the PR head each round, and swept after the TTL.
    revise_worktree_reuse: bool = True
    revise_worktree_ttl_hours: int = 24
    # Resume the Claude Agent session per (repo, branch) across revise rounds so a
    # follow-up (/ai fix issue 2) keeps the context of the earlier run instead of
    # starting cold — cheaper (warm cache) and more consistent. Best-effort: if the
    # stored session can't be resumed (expired / not on this host) the run silently
    # falls back to a fresh session. Off by default.
    reuse_claude_session: bool = False
    # Only resume a stored session this fresh; older → start clean (stale context hurts
    # more than it helps once the branch has moved on).
    claude_session_ttl_hours: int = 24

    # ── PR reviewer tracking (all active PRs in the configured repos) ──
    # Watch every active PR's reviewer list: detect newly-added reviewers, track their
    # votes for the dashboard, auto-review when the BOT is the added reviewer, and
    # politely remind human reviewers who haven't voted.
    pr_reviewer_tracking_enabled: bool = False
    # When the bot's own identity is added as a reviewer → run an AI code review on the
    # PR, post structured findings, and cast a vote. Re-arms when new commits land.
    pr_auto_review_on_added: bool = True
    # Seconds between reviewer-list scans (independent of the babysitter cadence).
    pr_reviewer_poll_interval_seconds: int = 30
    # Hours a human reviewer may sit vote-less before ONE polite reminder is posted on
    # the PR (ADO then emails the thread participants). 0 = reminders off.
    pr_reviewer_reminder_hours: int = 24
    # Bot identity override (email / uniqueName / display name). Empty = auto-detect
    # the account behind this client's PAT via connectionData — usually right.
    pr_bot_identity: str = ""
    # Only track / review / show PRs whose TARGET (merge-into) branch is in this list —
    # names with or without the ``refs/heads/`` prefix. Empty = every target branch.
    pr_reviewer_target_branches: list[str] = Field(default_factory=list)

    # ── Two-way Microsoft Teams bot (approve/reject buttons + chat commands) ──
    # Opt-in and additive to the existing one-way Teams webhook (notifications/teams.py,
    # teams_webhook_url) — that keeps working unchanged. This registers /api/messages so
    # the bot can reply and act on button clicks. Requires the "teams-bot" extra
    # (pip install .[teams-bot]) — the route silently stays unmounted if the packages or
    # these three values aren't all present.
    teams_agent_enabled: bool = False
    teams_agent_app_id: str = ""       # "Agent ID" — the Azure Bot's Application (client) ID
    teams_agent_app_secret: str = ""
    teams_agent_tenant_id: str = ""

    # ── Comment reaction loop (steer the autopilot by just commenting) ──
    # When a NEW human comment appears on an autopilot-owned item (one that still
    # carries a trigger tag), re-enter it and act on the comment — covering held /
    # needs_human, in-review and done items uniformly, without any manual restart tag.
    # Loop-safe: the bot's own comments lead with the bot icon so they never re-trigger,
    # and a per-item baseline stops the same comment being handled twice.
    comment_reprocess_enabled: bool = True
    # Comma-separated command prefixes that address the autopilot in a comment, e.g.
    # "/ai fix the null check", "/review this endpoint", or the bot's own account
    # ("phong.pham@nois.vn: ..."). The full comment is fed to the agent, which interprets
    # the intent (/ai = do it, /review = review & report, …) — add commands freely with no
    # code change. Only commands from the user THIS machine acts for count (see
    # assignee_trigger_user / auto_transition_assignee). Blank = command trigger off.
    # Role-aware command set (BA / QC / DEV). ACTION commands change code/spec on the
    # branch; ADVISORY ones only post findings (see comment_advisory_commands). The full
    # comment text is passed to the agent, which follows the per-command guidance in
    # FeedbackHandler to pick the right skill.
    #   /ai      DEV  — interpret & make the change              (action)
    #   /spec    BA   — refresh spec files + sync ADO work item  (action, update-spec)
    #   /test    DEV  — write/adjust automated tests             (action, write-tests)
    #   /review  DEV  — general code review                      (advisory)
    #   /qc      QC   — analyse test scope, propose test cases   (advisory)
    #   /security SA  — OWASP review of the diff                 (advisory, security-review)
    #   /impact  BA   — impact / blast-radius analysis           (advisory)
    #   /summary all  — plain-language PR summary                (advisory)
    # Route each role command to its purpose-built subagent (.claude/agents) instead of a
    # generic run — expert, consistent results. Degrades gracefully: if the subagent isn't
    # present the run does the task directly with the mapped skill.
    use_specialized_agents: bool = True
    comment_command: str = "/ai, /review, /spec, /test, /qc, /security, /impact, /summary"
    # Of the commands above, which are ADVISORY (review-only): the agent reviews and posts
    # findings but makes NO code change, and "no file changes" counts as success. Everything
    # else (e.g. /ai, /spec, /test) is an ACTION command that revises the branch. Comma-separated.
    comment_advisory_commands: str = "/review, /qc, /security, /impact, /summary"
    # Cap on human↔bot comment rounds per item, so a back-and-forth can't run away.
    max_comment_rounds: int = 5
    # Seconds between comment scans — the /command loop runs on its OWN cadence, decoupled
    # from the heavier main work-item poll, so commands are picked up fast (default 15s)
    # without speeding up that poll. Raise it only if a very large board makes the per-item
    # comment fetch too heavy.
    comment_poll_interval_seconds: int = 15

    # ── Closed-loop SDLC engine (v2, Phase 1) ──
    # Opt-in. When true, items route to the SdlcLoopEngine which drives them through
    # a PROFILE-selected ordered list of SDLC stages, gating each with score_run,
    # revising under one SHARED budget, escalating on exhaustion, and handing off to
    # the next machine's role via an ADO state. False = zero behaviour change.
    sdlc_loop_enabled: bool = False
    # SHARED revise budget across ALL stages of one item.
    sdlc_max_iterations: int = 3
    # PRIMARY per-machine knob: this instance's active profile ("ba"|"dev"|"qc"|
    # "review"|"design"|"full"). Blank → fall through to type-map / default.
    sdlc_profile: str = ""
    # Explicit per-machine ordered stage list — overrides sdlc_profile when non-empty.
    sdlc_stages: list[SdlcStage] = Field(default_factory=list)
    # Profile used when nothing else resolves.
    sdlc_default_profile: str = "full"
    # Per-item override tag prefix: an item tagged "sdlc:dev" forces the dev profile.
    sdlc_profile_tag_prefix: str = "sdlc:"
    # Map work-item type → profile, e.g. {"Bug": "dev", "User Story": "full"}.
    sdlc_type_profiles: dict[str, str] = Field(default_factory=dict)
    # Extra / overriding profiles merged over the built-ins (name → ordered stage names).
    sdlc_profiles: dict[str, list[str]] = Field(default_factory=dict)
    # Handoff: profile name → ADO state to set when its stages complete (the next
    # machine's trigger_states pick it up). Blank/absent → reuse resolved_state.
    sdlc_profile_states: dict[str, str] = Field(default_factory=dict)
    # Whether to apply the handoff state even when the PR is a draft (default: no —
    # a draft awaits human review before advancing to the next role's machine).
    sdlc_advance_on_draft: bool = False

    # ── Scheduled loops (dependency sweeper, changelog drafter, …) ──
    scheduled_loops: list[ScheduledLoop] = Field(default_factory=list)

    # ── Auto-review ──
    auto_review_enabled: bool = True
    block_on_severity: str = "Critical,High"

    # ── PR scoring ("get a score": grade each run from objective signals) ──
    pr_scoring_enabled: bool = True
    pr_score_auto_min: int = 85     # ≥ this → eligible to auto-resolve (L3)
    pr_score_review_min: int = 60   # < this → escalate to human instead of review/done

    # ── Planning workbench ──
    # AI relationship analysis in the Planning workbench's "Analyze" action. When on,
    # each Analyze runs a few bounded Claude judges over suspicious pairs (tokens);
    # off = link-graph grouping only (0 tokens).
    planning_ai_analysis: bool = True
    planning_ai_max_pairs: int = 6
    # A judge verdict must score at least this (0–100) to be shown / fed back.
    planning_ai_min_score: int = 50
    # Per-judge Claude timeout (seconds) during Analyze.
    planning_ai_timeout_seconds: int = 120
    # Keyword pre-filter knobs (which pairs are worth an AI judge). Shorter tokens
    # or a longer stop-list = fewer/cheaper judges.
    conflict_ai_min_token_len: int = 4
    conflict_ai_extra_stopwords: list[str] = Field(default_factory=list)
    # Default hour (0–23, local) pre-filled in the "Schedule" date-time picker.
    planning_schedule_default_hour: int = 21
    # Max work items the Planning "Load" fetches for an assignee.
    planning_load_limit: int = 200
    # Auto-refresh the read-only "Live schedule" panel every N seconds (0 = off).
    planning_live_refresh_seconds: int = 0
    # State the "Start" action moves a selected item to (so the poller picks it up),
    # if it isn't already in a trigger state. Blank → the first trigger_states entry.
    planning_start_state: str = ""

    # ── Scheduling ──
    schedule_start: str = ""
    schedule_end: str = ""
    schedule_days: str = "Mon,Tue,Wed,Thu,Fri"

    # ── Board ──
    # Max cards shown per board column before a "Load more" appears (0 = no cap).
    board_max_per_column: int = 20

    # ── Web / health ──
    health_port: int = 5080
    health_host: str = "0.0.0.0"

    # ── Persistence ──
    database_url: str = "sqlite+aiosqlite:///autopilot.db"

    # ── Notifications: MS Teams ──
    teams_webhook_url: str = ""

    # ── Notifications: Zalo OA ──
    zalo_oa_access_token: str = ""
    zalo_recipient_user_id: str = ""

    # ── Notifications: Email (SMTP) ──
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_to: str = ""
    email_from: str = ""

    @property
    def has_auth(self) -> bool:
        return bool(self.ado_pat or self.oauth_app_id)

    @property
    def effective_trigger_tags(self) -> list[str]:
        """All trigger tags (primary + extras), de-duplicated, blanks dropped."""
        out: list[str] = []
        for tag in [self.trigger_tag, *self.trigger_tags]:
            t = (tag or "").strip()
            if t and t not in out:
                out.append(t)
        return out

    @property
    def reviewer_target_branches(self) -> list[str]:
        """Configured target-branch filter, short names (``refs/heads/`` stripped)."""
        return [
            b.strip().removeprefix("refs/heads/")
            for b in (self.pr_reviewer_target_branches or [])
            if b.strip()
        ]

    def target_in_scope(self, target_ref: str) -> bool:
        """True if a PR targeting ``target_ref`` should be tracked/shown. When the filter
        is empty every target is in scope; otherwise the short target name must match."""
        allowed = self.reviewer_target_branches
        if not allowed:
            return True
        return (target_ref or "").removeprefix("refs/heads/") in allowed

    @property
    def comment_commands(self) -> list[str]:
        """Accepted comment trigger prefixes (``comment_command`` split on commas), blanks
        dropped — e.g. ``"/ai, dxfactory@nois.vn"`` → ``["/ai", "dxfactory@nois.vn"]``."""
        return [t.strip() for t in (self.comment_command or "").split(",") if t.strip()]

    @property
    def advisory_commands(self) -> list[str]:
        """Review-only command prefixes (``comment_advisory_commands`` split on commas)."""
        return [t.strip() for t in (self.comment_advisory_commands or "").split(",") if t.strip()]

    @property
    def report_only(self) -> bool:
        """L1: only classify and comment, never change code."""
        return self.autonomy_level == "report"

    @property
    def pr_is_draft(self) -> bool:
        """L2 opens draft PRs; L3 (unattended) opens normal PRs."""
        return self.autonomy_level != "unattended"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence: init args > env vars > .env > YAML file > secrets dir
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


def load_settings() -> Settings:
    """Load settings from YAML + environment."""
    return Settings()
