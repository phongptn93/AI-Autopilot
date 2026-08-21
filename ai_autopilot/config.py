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

import contextlib
import html
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

# Appended to every task/feedback brief. Two behaviours worth pinning down for a model
# that will run unattended:
#
# SCOPE — left alone, a capable model widens a task: it applies its own judgement about
# what the work "should" be, adds steps nobody asked for, and the diff stops matching the
# ticket. Under unattended autonomy nobody catches that before the branch moves, so the
# boundary has to be stated rather than assumed.
#
# LENGTH — written deliverables (spec updates, HTML reports, fix notes) run long by
# default, and padding is worse than useless in a document a human has to review. Note
# this asks for calibration, NOT brevity: cutting substance to look tidy is the opposite
# of what is wanted.
#
# Deliberately absent: any instruction to verify, double-check or re-review its own work.
# The model already does that, and repeating it compounds into wasted work without
# improving the result. Verification here is the deterministic gates (the test gate runs
# the repo's real tests; the scorer scores the run), not a request to the model.
AGENT_CONDUCT_INSTRUCTION = (
    "SCOPE: deliver what the work item asks, at the scope it intends. Make routine "
    "judgement calls yourself. If the request looks mistaken, or a better approach "
    "exists, say so in one sentence in your PR/comment and then do the task as asked — "
    "do not quietly narrow, widen or reshape it. Finish the whole task, and stop short "
    "of changes clearly beyond what was asked.\n"
    "LENGTH: match any document you write to what the task needs — cover the substance, "
    "and do not pad with filler sections, restated summaries or boilerplate."
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
    plain = _plain(text)
    low = plain.lower()
    for cmd in commands or ():
        cmd = (cmd or "").strip()
        if cmd and low.startswith(cmd.lower()):
            return plain
    return None


@dataclass(frozen=True)
class WebhookTarget:
    """One notification channel: a human-recognisable ``name`` and the URL to post to.

    The name exists for the log. A Workflows URL is a long opaque token, so a failure could
    only ever be reported by host — every channel in the same tenant looks identical there,
    which made "which channel is broken?" unanswerable.
    """

    url: str
    name: str = ""

    @property
    def label(self) -> str:
        """What to put in a log line — never the URL, which carries the posting token."""
        return self.name or _host(self.url) or "unnamed"


def _host(url: str) -> str:
    """Host of a URL, or '' — used to identify a webhook without logging its token."""
    from urllib.parse import urlsplit

    with contextlib.suppress(ValueError):
        return urlsplit(url).hostname or ""
    return ""


@dataclass(frozen=True)
class BotIdentity:
    """Who "the autopilot" is on Azure DevOps, for recognising an @mention of it.

    ``identity_id`` is the ADO identity GUID (auto-detected via ``connectionData``);
    ``display_name`` is what a mention actually renders as. ``claimed`` is the configured
    user (``assignee_trigger_user`` / ``auto_transition_assignee``) so a mention of the
    operator's own account counts too — the bot posts under that identity."""

    identity_id: str = ""
    display_name: str = ""
    claimed: str = ""


# Azure DevOps has TWO representations of an @mention, and a comment can only be matched
# against the one it actually arrives in:
#
#   storage form  @<11111111-2222-3333-4444-555555555555>
#   rendered form <a href="#" data-vss-mention="version:2.0,1111…">@Phong Pham</a>
#
# The REST API returns comments in the STORAGE form — the rendered anchor is what the web
# UI paints. Matching only the anchor is why a real "@AI Autopilot fix this" on a pull
# request was dropped in silence: no command, no log, no reply. Both forms are matched, and
# the attribute of the rendered one is captured whole with the GUID picked out separately —
# the "version:" part has changed shape before, and a mention carrying no GUID must still be
# matchable by name.
_MENTION_RE = re.compile(
    r"<(?P<tag>a|span)\b[^>]*\bdata-vss-mention\s*=\s*\"(?P<attr>[^\"]*)\"[^>]*>"
    r"(?P<label>.*?)</(?P=tag)>",
    re.IGNORECASE | re.DOTALL,
)
_GUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_GUID_RE = re.compile(_GUID, re.IGNORECASE)
# Storage form. The label is the GUID itself — there is no display name to fall back on,
# which is fine: a GUID identifies the bot better than any name could.
_MENTION_STORAGE_RE = re.compile(rf"@&lt;({_GUID})&gt;|@<({_GUID})>", re.IGNORECASE)


def _mentions(text: str) -> list[tuple[int, int, str, str]]:
    """Every @mention in ``text`` as ``(start, end, guid, label)``, in document order.

    Covers both ADO representations (see above). ``guid`` is "" when the rendered form
    carries no GUID; ``label`` is "" for the storage form, which has no display name."""
    found: list[tuple[int, int, str, str]] = []
    for m in _MENTION_RE.finditer(text):
        guid = _GUID_RE.search(m.group("attr") or "")
        label = _plain(m.group("label") or "").lstrip("@").strip()
        found.append((m.start(), m.end(), guid.group(0) if guid else "", label))
    for m in _MENTION_STORAGE_RE.finditer(text):
        found.append((m.start(), m.end(), m.group(1) or m.group(2) or "", ""))
    return sorted(found)


def _plain(text: str) -> str:
    """Comment HTML → plain text (ADO stores comments as HTML).

    Storage-form mentions go first: ``@<GUID>`` is not an HTML tag, so tag-stripping alone
    would leave a bare ``@`` — and a leading one shifts the text enough that ``/ai …`` after
    a mention of a colleague no longer starts with the command."""
    text = _MENTION_STORAGE_RE.sub(" ", text or "")
    return html.unescape(_HTML_TAG_RE.sub(" ", text)).strip()


def find_bot_mention(text: str | None, bot: BotIdentity | None) -> str | None:
    """If ``text`` @mentions the autopilot, return the comment as PLAIN text with that
    mention removed; otherwise ``None``.

    An @mention is how a human naturally addresses a bot, but ``match_command`` only ever
    matched a LEADING ``/command`` — so "@AI Autopilot review this" on a pull request was
    silently ignored (the rendered text starts with the display NAME, so not even putting
    the bot's email in ``comment_command`` helped).

    Identification is by GUID when the mention carries one AND we know ours: that survives
    a rename and can't collide with a same-named colleague. Only when there's no GUID to
    compare do we fall back to the display name."""
    if not text or bot is None:
        return None
    for start, end, guid, label in _mentions(text):
        if guid and bot.identity_id:
            # Definitive either way — never second-guess a GUID with a name.
            if guid.lower() != bot.identity_id.lower():
                continue
        elif not (
            (bot.display_name and matches_user(None, label, bot.display_name))
            or (bot.claimed and matches_user(None, label, bot.claimed))
        ):
            continue
        # Drop just this mention, keep the rest of what they wrote as the instruction.
        return _plain(text[:start] + " " + text[end:])
    return None


# Short purpose label per known /command, so the "reply to me" hint tells a reviewer what
# each one DOES instead of listing bare verbs. A command with no entry here (an operator
# added their own to ``comment_command``) still appears — just without a label.
_COMMAND_LABELS = {
    "/ai": "sửa code theo nhận xét",
    "/spec": "cập nhật spec",
    "/test": "viết test",
    "/review": "review code",
    "/qc": "phân tích test case",
    "/security": "soát bảo mật (OWASP)",
    "/impact": "phân tích ảnh hưởng",
    "/summary": "tóm tắt dễ hiểu",
}


def split_commands(
    commands: list[str], advisory: list[str]
) -> tuple[list[str], list[str]]:
    """Split configured trigger prefixes into ``(action, advisory)``, preserving the
    configured order.

    Only SLASH commands come back: ``comment_command`` may also carry a bare account
    ("dxfactory@nois.vn"), which addresses the bot but isn't something a human types as
    a command, so it must never appear in a hint offering commands to type."""
    slash = [c for c in commands if c.startswith("/")]
    advisory_set = {a.lower() for a in advisory}
    action = [c for c in slash if c.lower() not in advisory_set]
    advise = [c for c in slash if c.lower() in advisory_set]
    return action, advise


def _render_command_hint(action: list[str], advise: list[str], *, html_out: bool) -> str:
    """Render the shared "reply to me" command hint in HTML (ADO comments) or Markdown
    (Teams). One renderer so the two surfaces can never drift apart again."""
    def fmt(cmds: list[str]) -> str:
        parts = []
        for cmd in cmds:
            code = f"<code>{cmd}</code>" if html_out else f"`{cmd}`"
            label = _COMMAND_LABELS.get(cmd.lower())
            parts.append(f"{code} {label}" if label else code)
        return " · ".join(parts)

    def bold(text: str) -> str:
        return f"<b>{text}</b>" if html_out else f"**{text}**"

    groups = []
    if action:
        groups.append(f"{bold('sửa code:')} {fmt(action)}")
    if advise:
        groups.append(f"{bold('chỉ nhận xét:')} {fmt(advise)}")
    if not groups:
        return ""
    body = f"💬 Reply để tôi làm tiếp — {' — '.join(groups)}."
    return f"<sub>{body}</sub>" if html_out else body


def is_ambiguous_user(claimed: str) -> bool:
    """True when ``claimed`` is a single bare word — a value that cannot identify a person.

    "Phong" appears in *Phong Pham* and in *Phong Nguyen*, so as a substring it claims both
    people's work. An email, or a name with more than one word, does not have that problem.
    """
    c = (claimed or "").strip()
    return bool(c) and "@" not in c and len(c.split()) == 1


def parse_hhmm(value: str | None) -> tuple[int, int] | None:
    """``"09:00"`` → ``(9, 0)``; ``None`` for anything that isn't a real time of day.

    A schedule is only worth having if a typo is visible: "9am", "24:00" and "09.00" all
    parse as *some* number under a looser rule and would silently move the digest to an
    hour nobody chose. Returning None instead lets doctor name the bad value and the
    loop refuse to run on it.
    """
    parts = (value or "").strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def describe_users(claimed: list[str] | tuple[str, ...], limit: int = 4) -> str:
    """The allowed-commander roster for a refusal message, bounded.

    Lives here rather than in one of the two services that print it, so the PR babysitter
    and the reviewer tracker can't drift into wording the same refusal differently.
    """
    shown = list(claimed[:limit])
    rest = len(claimed) - len(shown)
    return " · ".join(shown) + (f" (+{rest} nữa)" if rest > 0 else "")


def matches_any_user(
    email: str | None, name: str | None, claimed: list[str] | tuple[str, ...]
) -> bool:
    """True if the identity matches ANY of the ``claimed`` accounts.

    An empty list means unrestricted, matching :func:`matches_user` with a blank value — so
    "nobody configured" keeps meaning "everyone allowed" rather than "nobody allowed".

    This exists because a team shares a pull request: scoping commands to one account meant a
    colleague's ``/ai`` was refused with "I only take orders from …", which is right for
    deciding whose WORK ITEMS this machine picks up but wrong for who may ask it a question.
    """
    entries = [c for c in (claimed or []) if (c or "").strip()]
    if not entries:
        return True
    return any(matches_user(email, name, c) for c in entries)


def matches_user(email: str | None, name: str | None, claimed: str) -> bool:
    """True if the identity (email/display name) matches the ``claimed`` user — used so, on
    a multi-machine setup, each person's own machine handles only their own commands. A
    blank ``claimed`` (single machine / unrestricted) matches everyone.

    An email or a multi-word name matches as a case-insensitive SUBSTRING, which is what
    makes real ADO identities work: configuring ``Phong Pham`` has to match the display
    name ADO actually returns, *"Phong Pham (Industrial - Head of P&T)"*.

    A single bare word is different. Substring-matching ``Phong`` also matched *Phong
    Nguyen*, so this machine would pick up a colleague's work items and run an agent on
    them. There is no string rule that can tell those two people apart, so a lone word must
    IDENTIFY the person rather than appear inside them: it has to equal the display name or
    the email's local part. Erring toward matching nobody is the safe direction — the
    autopilot idles instead of acting on someone else's item, and ``doctor`` says why.
    """
    c = (claimed or "").strip().lower()
    if not c:
        return True
    if not is_ambiguous_user(c):
        return c in f"{email or ''} {name or ''}".lower()
    local = (email or "").split("@")[0].strip().lower()
    return c == local or c == (name or "").strip().lower()


class RepoConfig(BaseModel):
    """A source repository, optionally bound to specific task categories."""

    name: str = ""
    path: str = ""
    base_branch: str = "development"
    categories: list[str] = Field(default_factory=list)


class WorkspaceConfig(BaseModel):
    """One ADO work-item project (or a group of them) bound to its own workspace folder.

    The unit that makes ONE autopilot instance serve SEVERAL projects: an item is routed
    to the workspace whose ``ado_projects`` names its ``System.TeamProject``, and the run
    then executes against THAT workspace's directory / repos / base branch.

    Every override is blank-means-inherit — a workspace that only names its project and
    folder still gets the global trigger tags, states, autonomy, quality gates and so on.
    That is deliberate: the point of a global config is that adding a second project
    costs two lines, not a duplicated settings page.

    Distinct from :class:`TenantConfig`, which carries its OWN organization and PAT. A
    workspace shares the instance's single ADO connection (one org, one credential).
    """

    name: str = ""                  # display label; blank → first project, else folder name
    enabled: bool = True
    # Work-item projects routed to this workspace. Matched case-insensitively against
    # System.TeamProject. Empty = the workspace is inert (nothing routes to it).
    ado_projects: list[str] = Field(default_factory=list)
    # ── overrides (blank/empty = inherit the root Settings value) ──
    code_project: str = ""          # project holding the repos/PRs for these items
    workspace_directory: str = ""   # folder with the shared .claude + repo subfolders
    repo_working_directory: str = ""
    base_branch: str = ""
    allowed_repos: list[str] = Field(default_factory=list)
    repo_descriptions: list[str] = Field(default_factory=list)
    trigger_tag: str = ""
    # ── state vocabulary overrides ──
    # ADO state names belong to a PROJECT's process, not just to a work-item type: two
    # projects on one instance can name the same lifecycle stage differently ("Ready to
    # Review" vs "Ready for Review"), and ``work_item_flows`` alone cannot express that
    # — it keys on type. A workspace that names its own flows/done-states therefore owns
    # the vocabulary for the projects routed to it; blank = inherit the root's.
    work_item_flows: list[Any] = Field(default_factory=list)
    done_states: list[str] = Field(default_factory=list)
    parent_rollup_map: list[str] = Field(default_factory=list)

    @property
    def label(self) -> str:
        """What to show in a log line or on the dashboard."""
        if self.name:
            return self.name
        if self.ado_projects:
            return self.ado_projects[0]
        return Path(self.workspace_directory).name if self.workspace_directory else "workspace"

    def serves(self, project: str) -> bool:
        """True if ``project`` (an ADO ``System.TeamProject``) routes to this workspace."""
        target = (project or "").strip().lower()
        return bool(target) and any(
            (p or "").strip().lower() == target for p in self.ado_projects
        )

    def overrides(self) -> dict[str, Any]:
        """The non-blank overrides, as a ``Settings`` update dict.

        Only keys the workspace actually sets appear — a blank string or empty list
        means "inherit", so it must not overwrite the root value with emptiness.
        """
        out: dict[str, Any] = {}
        for key in (
            "code_project", "workspace_directory", "repo_working_directory",
            "base_branch", "trigger_tag",
        ):
            value = (getattr(self, key) or "").strip()
            if value:
                out[key] = value
        for key in (
            "allowed_repos", "repo_descriptions",
            "work_item_flows", "done_states", "parent_rollup_map",
        ):
            value = getattr(self, key) or []
            if value:
                out[key] = list(value)
        return out


# One workspace, written as a single line on the Settings page:
#
#   Khatoco, CMMS = C:\ws\dxfactory | base=main | code=DxFactory | repos=Backend,Frontend
#
# Left of "=" are the ADO work-item project(s) routed here; right is the workspace folder;
# everything after a "|" is an optional override. The structured ``workspaces:`` list in
# config.yaml stays available for anything this one-liner cannot express.
_WS_OPTION_KEYS = {
    "base": "base_branch",
    "base_branch": "base_branch",
    "code": "code_project",
    "code_project": "code_project",
    "repo": "repo_working_directory",
    "repo_working_directory": "repo_working_directory",
    "repos": "allowed_repos",
    "allowed_repos": "allowed_repos",
    "tag": "trigger_tag",
    "trigger_tag": "trigger_tag",
    "name": "name",
    "desc": "repo_descriptions",
}


def parse_workspace_line(line: str) -> WorkspaceConfig | None:
    """Parse one ``projects = directory | key=value …`` line, or ``None`` if unusable.

    A line with no ``=`` before the first ``|`` cannot say which project maps where, so it
    is dropped rather than guessed at — a mis-parsed line would silently route work items
    into the wrong repository.
    """
    raw = (line or "").strip()
    if not raw or raw.startswith("#"):
        return None
    head, *option_parts = raw.split("|")
    if "=" not in head:
        return None
    projects_raw, directory = head.split("=", 1)
    projects = [p.strip() for p in projects_raw.split(",") if p.strip()]
    if not projects:
        return None
    ws = WorkspaceConfig(ado_projects=projects, workspace_directory=directory.strip())
    for part in option_parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        field_name = _WS_OPTION_KEYS.get(key.strip().lower())
        value = value.strip()
        if not field_name or not value:
            continue
        if field_name in ("allowed_repos", "repo_descriptions"):
            setattr(ws, field_name, [v.strip() for v in value.split(",") if v.strip()])
        else:
            setattr(ws, field_name, value)
    return ws


def _as_workspace(value: Any) -> WorkspaceConfig:
    """A ``WorkspaceConfig`` from either a model or the plain dict a live save leaves.

    Invalid entries degrade to a disabled, empty workspace rather than raising: a
    hand-edited ``config.yaml`` must not take the whole dashboard down, and a workspace
    that resolves to nothing is visible on the Workspaces page and in ``doctor``."""
    if isinstance(value, WorkspaceConfig):
        return value
    if isinstance(value, dict):
        with contextlib.suppress(Exception):
            return WorkspaceConfig(**value)
    return WorkspaceConfig(enabled=False)


def parse_workspace_map(lines: list[str]) -> list[WorkspaceConfig]:
    """Parse the dashboard's workspace textarea into workspaces, dropping bad lines."""
    out: list[WorkspaceConfig] = []
    for line in lines or []:
        ws = parse_workspace_line(line)
        if ws is not None:
            out.append(ws)
    return out


def format_workspace_line(ws: WorkspaceConfig) -> str:
    """Render a workspace back to its one-line form (inverse of ``parse_workspace_line``)."""
    parts = [f"{', '.join(ws.ado_projects)} = {ws.workspace_directory}"]
    for key, field_name in (
        ("base", "base_branch"), ("code", "code_project"),
        ("repo", "repo_working_directory"), ("tag", "trigger_tag"), ("name", "name"),
    ):
        value = (getattr(ws, field_name) or "").strip()
        if value:
            parts.append(f"{key}={value}")
    if ws.allowed_repos:
        parts.append(f"repos={','.join(ws.allowed_repos)}")
    return " | ".join(parts)


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
    # Per-tenant extra channels, mirroring Settings.teams_webhook_urls.
    teams_webhook_urls: list[str] = Field(default_factory=list)
    # Per-tenant named channels, mirroring Settings.teams_webhook_channels.
    teams_webhook_channels: list[Any] = Field(default_factory=list)
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
    # ADO project whose WORKSPACE this loop runs in (multi-workspace setups). Blank =
    # the root workspace. Only the workspace is taken from it — a loop has no work item.
    project: str = ""
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
        env_file=".env",
        env_file_encoding="utf-8",
        yaml_file=_yaml_path(),
        yaml_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Azure DevOps connection ──
    ado_organization: str = ""
    ado_project: str = ""              # where the WORK ITEMS live (the DEFAULT project)
    # Additional work-item projects polled by this SAME connection (same org, same
    # credential). ``ado_project`` stays the default — the one used when an item's own
    # project is unknown, and the one a newly created work item lands in. Every project
    # named here (and every project named by a workspace below) is polled in ONE
    # org-level WIQL query, not one query per project.
    ado_projects: list[str] = Field(default_factory=list)
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
    # Additional accounts allowed to issue /commands and @mentions, beyond the machine's
    # owner. A pull request is shared, so scoping commands to one person meant a colleague's
    # "/ai fix this" was refused with "I only take orders from …" — correct for deciding
    # whose work ITEMS this machine picks up, wrong for who may ask it to do something on a
    # PR it already owns. Each entry is an email or a full name, matched like the owner
    # (see matches_user). Read via ``effective_command_users``, never on its own.
    # Empty owner AND empty list = anyone may command (single-machine default).
    command_users: list[str] = Field(default_factory=list)
    # Open the /command gate to EVERYONE, without having to enumerate the team. The owner
    # is normally always set (it decides whose work items this machine picks up), so the
    # roster is never empty in practice and a colleague's "/ai …" is refused with
    # "Trên máy này tôi chỉ nhận lệnh từ …". Clearing the owner to fix that would also
    # widen which ITEMS get processed — the wrong knob. This flag scopes only the command
    # gate (work-item /commands, PR /commands, @mentions); ownership is untouched.
    commands_from_anyone: bool = False
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
    # Per-work-item-type state flows. ADO states belong to a TYPE ("Ready to Deploy"
    # exists on Bug/Task but not Requirement), so a single project-wide state for a
    # lifecycle stage is rejected outright for the types that don't define it — and the
    # only states safe to share are the ones every type has, which flattens the flow.
    # A flow names a group of types and the state each stage sets for them; empty list
    # = every stage falls back to the flat fields below. Edited at /dashboard/flow,
    # validated against the project's real types. See ai_autopilot/flows.py.
    #
    # Deliberately `list[Any]`, not `list[dict]`: a typed list makes pydantic reject a
    # malformed hand-edit at load, which would stop the autopilot from starting at all
    # over a config typo. The resolvers skip entries that aren't mappings, and
    # flows.validate_flows reports them in the editor and in `doctor` — a far better
    # place to learn about it than a boot loop.
    work_item_flows: list[Any] = Field(default_factory=list)
    # Flat fallbacks, used for any type no flow claims.
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
    # When to close the console launched for an interactive task. The CLI is a REPL:
    # it writes its result and then sits idle forever, so nothing closes on its own.
    #   "pr_closed" – keep it (and its scratch worktree) alive while the PR is open,
    #                 close once the PR is merged/abandoned (default). Review feedback
    #                 can then still be worked in the SAME session/worktree.
    #   "result"    – close as soon as the task writes its result.
    #   "never"     – leave every console open (old behaviour; they pile up).
    interactive_close_on: str = "pr_closed"
    # PR feedback on an item an interactive session built: run the rework inside that
    # session's own scratch worktree and RESUME its conversation, instead of paying a
    # fresh worktree + a fresh read of the codebase. Requires the scratch to still be
    # there, i.e. interactive_close_on = "pr_closed".
    interactive_resume_on_rework: bool = True
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

    # ── Delivery (PM) view ──
    # Records a row whenever a work item's ADO state changes, which is what makes lead
    # time, cycle time and the flow chart possible — see services/delivery_tracker.py.
    # Turning this OFF does not just hide a chart: it stops the clock, and the history
    # for that period can never be recovered.
    delivery_history_enabled: bool = True
    delivery_history_interval_minutes: int = 10
    delivery_history_retention_days: int = 180
    # Work items read per recording cycle (most-recently-changed first). An item that
    # has not changed cannot have changed state, so the cap only bounds cost.
    delivery_max_items: int = 500
    # Default reporting window on the Delivery page.
    delivery_window_days: int = 14
    # When a wait becomes something a PM should be told about. Defaults suit a team on
    # 1–2 week sprints; raise them for a monthly release cadence.
    delivery_merge_hours: int = 24    # approved, unblocked, still not merged
    delivery_review_hours: int = 24   # PR open, nobody has voted
    delivery_stale_days: int = 3      # in progress, no state change

    # ── Multi-workspace (one connection, several projects) ──
    # What to call the workspace backed by the global fields above (its directory, base
    # branch and default project). Shown first on the Workspaces page and in the
    # dashboard's workspace selector.
    default_workspace_name: str = ""
    # Structured form, managed on /dashboard/workspaces (or edited in config.yaml).
    workspaces: list[WorkspaceConfig] = Field(default_factory=list)
    # One-line form, edited on the Settings page:
    #   "Khatoco = C:\\ws\\dxfactory | base=main | code=DxFactory"
    # Merged with ``workspaces`` (a structured entry wins on the same project) so the
    # dashboard stays usable without giving up the expressive YAML form.
    workspace_map: list[str] = Field(default_factory=list)

    # ── Multi-tenant ──
    tenants: list[TenantConfig] = Field(default_factory=list)

    # ── Plugins ──
    # Plugins are TRUSTED code: every .py in plugins_directory is imported and run
    # with full access to the Container (ADO client + PAT, DB, executor). Loading is
    # OPT-IN so a file dropped into ./plugins can't silently achieve code execution.
    plugins_enabled: bool = False
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

    # ── Batched runs: linked items share ONE run, then split into one PR each ──
    # The scheduler's default answer to "these items touch the same area" is to
    # serialise them into separate waves — correct, but each wave re-reads the same
    # code from scratch and the second run can still land on top of the first.
    # Batching inverts that for a *linked cluster*: one agent run holds all of them
    # in context and produces one branch + one PR PER WORK ITEM, so review and
    # merge stay per-item. Off by default — it changes how work is dispatched.
    batch_related_enabled: bool = False
    # Hard cap on cluster size. A batch is one Claude context and one failure
    # domain, so keep it small; anything larger is dispatched item-by-item.
    batch_max_items: int = 3
    # Stack the PRs (item 2 branches off item 1, and targets it) instead of cutting
    # every branch from the base. Stacked = no conflicts when the items touch the
    # same files, at the cost of a merge ORDER. Independent = merge in any order.
    batch_stacked_prs: bool = True

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

    # ── Reviewers on the PRs the autopilot opens ──
    # Add the work item's ASSIGNEE as a reviewer on the PR opened for it. The person who
    # owns the ticket is the one who should look at the code, and nobody reviews a PR they
    # were never told about — ADO notifies on being added. Best-effort: a failure here is
    # logged, never fails the run (the PR is already open). Off = ADO's own default
    # reviewer policy applies, unchanged.
    pr_add_assignee_as_reviewer: bool = False
    # Extra reviewers added to every PR, as ADO identity GUIDs (the reviewers endpoint is
    # keyed on identity id, not email — read one off an existing reviewer in the PR API).
    pr_extra_reviewer_ids: list[str] = Field(default_factory=list)
    # Mark added reviewers REQUIRED — ADO then blocks completing the PR until they vote.
    # Off = optional reviewer (notified, not blocking), which is the safer default.
    pr_reviewers_required: bool = False

    # ── PR reviewer tracking (all active PRs in the configured repos) ──
    # Watch every active PR's reviewer list: detect newly-added reviewers, track their
    # votes for the dashboard, auto-review when the BOT is the added reviewer, and
    # politely remind human reviewers who haven't voted.
    pr_reviewer_tracking_enabled: bool = False
    # When the bot's own identity is added as a reviewer → run an AI code review on the
    # PR, post structured findings, and cast a vote. Re-arms when new commits land.
    # Off by default — casting a vote is consequential enough to need its own explicit
    # opt-in even after pr_reviewer_tracking_enabled is on.
    pr_auto_review_on_added: bool = False
    # Seconds between reviewer-list scans (independent of the babysitter cadence).
    pr_reviewer_poll_interval_seconds: int = 30
    # Hours a human reviewer may sit vote-less before ONE polite reminder is posted on
    # the PR (ADO then emails the thread participants). 0 = reminders off.
    pr_reviewer_reminder_hours: int = 24
    # Hours between REPEAT reminders for a reviewer who still hasn't voted. 0 = never
    # repeat, which nudges once and then goes quiet — exactly when a stuck PR needs it
    # most. Set e.g. 24 to keep nudging daily until they vote.
    pr_reviewer_reminder_repeat_hours: int = 0
    # How many times ONE advisory command (/review, /qc …) may run against the same PR at
    # the same commit. Advisory commands don't spend the revision budget because they
    # change nothing — but each is still a full agent run, so without a ceiling N replies
    # cost N runs for findings that barely differ. 0 = unlimited.
    pr_advisory_max_per_commit: int = 2
    # Ceiling on auto-reviews for one PR across its whole life. Auto-review re-arms on
    # every new commit, so a push-heavy PR can otherwise be reviewed a dozen times.
    # 0 = unlimited (the behaviour before this setting existed).
    pr_auto_review_max_per_pr: int = 0
    # Concurrency cap for PR review work (auto-review + comment commands), separate from
    # `max_concurrent` so a batch of PRs cannot starve the task execution that actually
    # implements work items. 0 = fall back to `max_concurrent`.
    pr_review_max_concurrent: int = 0
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
    # Free-text understanding for READ-ONLY queries (e.g. "PR nào của tôi đang bị
    # block?") — routed through Claude ONLY to classify into {items, prs, status, help,
    # unknown} + a filter, never to execute anything itself. Structurally cannot reach
    # any code-mutating action (no such intent exists) — see teams_agent.py. Costs one
    # Claude call per unmatched message; toggle off to keep structured /commands only.
    teams_agent_nlu_enabled: bool = True
    # Skill the bot runs to review a PR from chat (real diff-vs-codebase review that
    # posts findings on the PR). Must exist in the workspace's .claude/skills.
    teams_review_skill: str = "review-pr"
    # Route free-text through a genuine Claude agent turn (tools + skills) instead of
    # the fixed intent classifier — more natural/agentic, but a Claude run per message.
    teams_agentic_enabled: bool = False
    # How many chat replies may hold a Claude process AT ONCE. Replies are answered off
    # the Teams turn (see teams_agent._run_deferred), so without a cap a busy channel
    # spawns one CLI per message and the box thrashes — starving the work pipeline it is
    # supposed to be reporting on. Deliberately NOT max_concurrent (default 1): that
    # governs 30-minute worktree tasks, and reusing it would make the whole team's chat
    # single-file. 0 = no cap.
    teams_agent_max_concurrent: int = 3
    # Let the agentic chat REMEMBER the conversation: each reply resumes the Claude session
    # from the previous message in the same thread instead of starting cold. Without it every
    # message was an independent session, so a thread could not be a conversation — you had
    # to restate which PR or item you meant every single time.
    #
    # Scoped per Teams conversation id, which in a channel already encodes the THREAD, so
    # threads never bleed into each other. Bounded by claude_session_ttl_hours (24h): a
    # thread colder than that starts clean, because stale context misleads more than it
    # helps. Deliberately its OWN switch rather than reuse_claude_session — that one governs
    # 30-minute worktree task runs and must stay independently tunable.
    teams_agent_session_memory: bool = True

    # ── Reasoning effort (how hard the model thinks per call) ──
    # "low" | "medium" | "high" | "xhigh" | "max"; blank = the model's own default.
    # Every call used to run at that default, including the trivial ones — picking one of a
    # dozen intents, rewording data Python had already fetched. Those reach the same answer
    # far cheaper and faster on a low setting, and the chat path is exactly where a person is
    # sitting there waiting.
    #
    # Short, tool-less calls: classify an intent, reword a looked-up list, write one persona
    # message, pick a command for an @mention. They choose or rephrase, never reason about
    # code, so "low" is nearly free of risk.
    claude_effort_chat: str = "low"
    # The agentic Teams turn: real tool lookups against ADO, up to 12 turns, but still a chat
    # reply rather than an edit.
    claude_effort_agentic: str = "medium"
    # Task execution / PR review — real multi-file work. Left BLANK on purpose: the vendor
    # guidance is to re-sweep effort on your own evals before lowering it, and there is no
    # eval for these runs here. The knob exists so it can be tuned (or raised to "xhigh")
    # without a code change.
    claude_effort_task: str = ""
    # ── Bot persona (Teams voice) ──
    # The bot composes its key replies (free-text answers, ticket acknowledgements)
    # in THIS voice via Claude, so it reads like a consistent, proactive teammate
    # rather than a terse machine. ``name`` = how it refers to itself; ``voice`` =
    # the tone/register guide handed to Claude. Falls back to a plain line if the
    # compose call fails, so a reply is never lost.
    bot_persona_name: str = "AI Autopilot"
    bot_persona_voice: str = (
        "một kỹ sư đồng đội tận tâm, chủ động và lễ phép. Xưng 'em', gọi người dùng "
        "là 'anh/chị'. Trả lời ngắn gọn, rõ ràng, tự nhiên; chủ động gợi ý bước tiếp "
        "theo và hỏi lại khi thiếu thông tin; báo tiến độ/blocker trung thực. Không "
        "máy móc, không sáo rỗng, không bịa thông tin ngoài dữ liệu được cung cấp."
    )
    # Proactively post a team-overview digest to every channel/chat the bot has been
    # added to, every N hours (e.g. 24 = once daily). 0 = off (default) — this pushes
    # unsolicited messages into a shared channel, so it's opt-in. Requires the bot to
    # have been @mentioned or added at least once so its conversation gets stored
    # (survives restarts — the reference is persisted, not kept only in memory).
    teams_agent_digest_interval_hours: int = 0
    # Fixed local wall-clock time to post it instead, "HH:MM" (e.g. "09:00"). Wins over
    # the interval when set, because the interval counts from process START: a restart
    # at 14:00 moves a 24h digest to 14:00 for good, so the one message the team is
    # meant to read every morning arrives at a different hour after every deploy.
    # Local time on the host, like the planned runs in planning_analyzer.
    teams_agent_digest_at: str = ""

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
    # Also treat an @MENTION of the bot (no slash needed) as addressing it — how a human
    # naturally asks a teammate. The intent is then INFERRED from the comment text into one
    # of the commands above, defaulting to an ADVISORY one: an ambiguous mention
    # ("@bot sao chỗ này chậm vậy?") must never turn into a code change + push. Only
    # wording that clearly asks for a change routes to an action command.
    comment_mention_enabled: bool = True
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

    # ── Policy engine (hard guardrails on what a run may change) ──
    # Enforced deterministically BEFORE a PR opens (like the review/test gates) —
    # the agent cannot talk its way past these. Empty/0 = rule off.
    # Glob patterns of files the autopilot must never modify, e.g.
    # ["k8s/*", ".github/*", "*.env", "Dockerfile"].
    policy_protected_paths: list[str] = Field(default_factory=list)
    # Blast-radius cap: block a run that changes more files than this (0 = off).
    policy_max_files_changed: int = 0

    # ── Retrospective learning loop ──
    # Capture auto-review findings per repo and inject the recent ones into the next
    # run's brief, so the agent stops repeating flagged mistakes. Opt-in — off = the
    # brief is unchanged and nothing is written.
    learning_loop_enabled: bool = False
    # How many of the most recent lessons are injected into a brief. Too few and the
    # loop forgets; too many and the brief drowns in old findings. 0 = record only
    # (keep learning, stop injecting).
    lessons_max_injected: int = 8

    # ── Auto-test-gate ──
    # Run the target repo's test suite in the worktree after the agent edits but
    # BEFORE a PR is opened; a red run blocks the PR (mirrors auto-review) and feeds
    # the CI signal into pr_scorer. Opt-in — off = no behaviour change.
    test_gate_enabled: bool = False
    # Explicit test command; blank = auto-detect (pytest / dotnet test / npm test)
    # from the files in the worktree. No runner detected = skip (never blocks).
    test_command: str = ""
    test_timeout_seconds: int = 600

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

    # ── Spec drift & PR traceability ──
    # The agent is told to DECIDE rather than ask, so every ambiguity it resolves is a
    # decision taken on the team's behalf that the written item does not reflect. When
    # on, the run reports those decisions and the control plane files them: a prefixed
    # comment (⚠️ SPEC-DRIFT — stable, so it stays queryable), a tag, a PR comment for
    # the reviewer, and a row the BA ticks off once the spec is back in line.
    spec_drift_enabled: bool = True
    spec_drift_tag: str = "spec-update-needed"
    # Hold the item for a human instead of letting it advance while the spec is stale.
    # Off by default: a drift is a documentation debt, not a reason to stop delivery.
    spec_drift_holds_item: bool = False
    # Verify after the fact that every PR the autopilot opens names its work item, and
    # attach the link when it does not. An instruction in the brief is advice a model
    # can drop on a long run; this is the check that notices.
    pr_require_work_item_link: bool = True

    # ── Process health (periodic review of HOW the team works) ──
    # A process doc assigns standing reviews — ad-hoc ratio each sprint, escaped
    # defects each month, tag catalogue each quarter. This computes them and pushes
    # the findings to the notification channels; it never changes a work item, because
    # every finding is a judgement call the process assigns to a person.
    process_health_enabled: bool = False
    process_health_interval_hours: int = 168      # weekly; 0 = compute on demand only
    process_health_window_days: int = 14          # one sprint
    process_health_blocked_days: int = 3          # doc: blocked >3 days → PM follow-up
    process_health_adhoc_threshold_pct: float = 30.0
    # Tag taxonomy. Classification = what a ticket IS (exactly one per ticket);
    # ad-hoc = the unplanned subset of those; routing = "somebody must look", meant to
    # be removed once they have.
    process_health_classification_tags: list[str] = Field(
        default_factory=lambda: [
            "BUG", "Change Request", "New Request", "Pre-Sales", "Support", "Spike", "Ops",
        ]
    )
    process_health_adhoc_tags: list[str] = Field(
        default_factory=lambda: ["Support", "Spike", "Ops", "Pre-Sales"]
    )
    process_health_routing_tags: list[str] = Field(
        default_factory=lambda: ["PM - Need Review", "Product - Review"]
    )

    # ── Scheduling & quiet hours ──
    # IANA timezone every window below is read in, e.g. "Asia/Ho_Chi_Minh". REQUIRED for
    # any window to apply: the system clock is usually UTC (containers, VMs), so applying
    # "18:00" to it would silently mean 01:00 for a team in UTC+7. Blank = no windows.
    timezone: str = ""
    # When a run may START. Outside it the poller idles; work already running continues.
    schedule_start: str = ""
    schedule_end: str = ""
    schedule_days: str = "Mon,Tue,Wed,Thu,Fri"
    # When we may PING a human. Deliberately separate from the schedule: a team is
    # usually happy for the autopilot to keep working in the evening — what they do not
    # want is a phone going off at 22:40 about something nobody can act on until
    # morning. Notices raised outside the window are HELD (never dropped) and delivered
    # as ONE summary when it opens. Blank = notify at any hour, as before.
    notify_hours_start: str = ""
    notify_hours_end: str = ""
    notify_days: str = "Mon,Tue,Wed,Thu,Fri"
    # Ceiling on held notices, so a quiet weekend cannot grow the table without bound.
    # Oldest are dropped first and the summary says how many.
    notify_quiet_max_held: int = 200

    # ── Board ──
    # Max cards shown per board column before a "Load more" appears (0 = no cap).
    board_max_per_column: int = 20

    # ── Web / health ──
    health_port: int = 5080
    health_host: str = "0.0.0.0"
    # Base URL the dashboard is reachable at FROM A READER'S BROWSER, e.g.
    # "https://autopilot.example.com". Used to link chat messages back to the pages
    # that hold the detail. Blank = no link is offered: a Teams digest is read on a
    # phone, and a link built from health_host ("0.0.0.0:5080") resolves for nobody.
    dashboard_public_url: str = ""
    # Security for the web surface. Both are OPT-IN (empty → disabled, preserving
    # current behaviour) but STRONGLY recommended because health_host defaults to
    # 0.0.0.0 (all interfaces):
    #   dashboard_auth_token – when set, the /dashboard/* UI requires HTTP Basic
    #     auth (any username; password = this token). Protects config/PAT writes,
    #     run triggers and record deletion from anyone on the network. Legacy
    #     plaintext form; prefer dashboard_auth_password_hash below.
    #   dashboard_auth_password_hash – PBKDF2 hash of the dashboard password
    #     (security.hash_password). Preferred over the plaintext token: on first
    #     start with neither set, the CLI prompts for a password and persists its
    #     hash here. Either one being set enables the /dashboard auth gate.
    #   webhook_secret – when set, POST /api/webhook/* requires a matching
    #     `X-Webhook-Secret` header (or `?secret=`), so the run/command trigger
    #     can't be fired by an unauthenticated request.
    dashboard_auth_token: str = ""
    dashboard_auth_password_hash: str = ""
    webhook_secret: str = ""
    # Symmetric password that encrypts the FULL config export (the download that
    # deliberately includes secrets — PAT, SMTP/Zalo tokens, per-tenant PATs).
    # Must stay usable to encrypt, so it is stored as-is (not hashed); the same
    # value is needed to decrypt the exported file. Override in config.yaml / env.
    config_export_password: str = "Export#12345"

    # ── Persistence ──
    database_url: str = "sqlite+aiosqlite:///autopilot.db"

    # ── Notifications: MS Teams ──
    teams_webhook_url: str = ""
    # Additional Teams Workflows webhook URLs — every notice is posted to ALL of them, so one
    # autopilot can report into several channels (e.g. #dev, #qc, a manager's channel) without
    # running a second instance. Kept separate from ``teams_webhook_url`` rather than turning
    # that into a list: it is an existing setting that people already have in config.yaml,
    # .env and per-tenant overrides, and changing its type would break every one of them.
    # Read them together via the ``teams_webhooks`` property, never individually.
    teams_webhook_urls: list[str] = Field(default_factory=list)
    # Named channels — the shape the dashboard edits: one entry per channel, each with a
    # ``name`` you'll recognise in a log line, its ``url``, and an ``active`` switch so a
    # channel can be muted without losing its URL (the URL is a Workflows token you cannot
    # get back once deleted, so "delete to mute" was the wrong affordance).
    #
    #   teams_webhook_channels:
    #     - {name: "#dev",  url: "https://…", active: true}
    #     - {name: "#qc",   url: "https://…", active: false}
    #
    # ``list[Any]`` rather than a typed model for the same reason as work_item_flows: a
    # malformed hand-edit must not stop the autopilot from starting. Entries that aren't
    # mappings are skipped, and doctor reports them. A bare string is accepted as a URL so
    # pasting a plain list still works.
    teams_webhook_channels: list[Any] = Field(default_factory=list)

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

    # ── Multi-project / multi-workspace resolution ───────────────────────────
    #
    # Three questions, answered in one place so no caller has to re-derive them:
    #   which projects do we poll?      → effective_ado_projects
    #   which workspace owns an item?   → workspace_for
    #   what config does its run use?   → scoped_for_project
    #
    # All three degrade to the single-project behaviour when nothing extra is
    # configured, so an existing config.yaml keeps working untouched.

    @property
    def effective_workspaces(self) -> list[WorkspaceConfig]:
        """Every enabled workspace: the one-line ``workspace_map`` entries plus the
        structured ``workspaces`` list.

        A structured entry WINS over a one-liner that claims the same project — the
        YAML form is the more expressive one, so it is the one an operator reaches for
        when the line form was not enough, and silently letting the line override it
        would undo exactly the customisation they went there to make.

        Entries are coerced from plain dicts when needed: a live save from the
        dashboard writes settings back with a bare ``setattr``, which does NOT run
        pydantic validation, so ``workspaces`` holds dicts until the next restart.
        Reading them as models regardless is what lets a saved workspace take effect
        immediately instead of appearing to save and doing nothing until a reboot."""
        structured = [ws for ws in map(_as_workspace, self.workspaces) if ws.enabled]
        claimed = {
            (p or "").strip().lower() for ws in structured for p in ws.ado_projects
        }
        out = list(structured)
        for ws in parse_workspace_map(self.workspace_map):
            if not ws.enabled:
                continue
            remaining = [
                p for p in ws.ado_projects if (p or "").strip().lower() not in claimed
            ]
            if remaining:
                out.append(ws.model_copy(update={"ado_projects": remaining}))
        return out

    @property
    def effective_ado_projects(self) -> list[str]:
        """Every work-item project this instance polls, default first, de-duplicated
        case-insensitively. Blank when no project is configured at all (offline setup)."""
        out: list[str] = []
        seen: set[str] = set()
        candidates = [self.ado_project, *self.ado_projects]
        candidates += [p for ws in self.effective_workspaces for p in ws.ado_projects]
        for project in candidates:
            name = (project or "").strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
        return out

    def workspace_for(self, project: str) -> WorkspaceConfig | None:
        """The workspace serving ``project``, or ``None`` to use the root config."""
        for ws in self.effective_workspaces:
            if ws.serves(project):
                return ws
        return None

    def code_project_for(self, project: str = "") -> str:
        """Project holding the repos/PRs for work items in ``project``.

        Resolution order: the owning workspace's ``code_project`` → the global
        ``code_project`` → the work item's own project → the default project. The last
        two matter for multi-project setups: with several work-item projects a single
        global ``code_project`` would send EVERY project's PRs to one repo host, so an
        item falls back to its OWN project before the default one."""
        ws = self.workspace_for(project)
        if ws and ws.code_project.strip():
            return ws.code_project.strip()
        if self.code_project.strip():
            return self.code_project.strip()
        return (project or "").strip() or self.ado_project

    def scoped_for_project(self, project: str) -> Settings:
        """This config as seen by a run on ``project`` — workspace overrides applied.

        Returns ``self`` unchanged when the project has no workspace of its own, so the
        common single-workspace case allocates nothing. The copy is a snapshot: a live
        settings edit lands on the root object, and the next run re-derives from it."""
        name = (project or "").strip()
        updates: dict[str, Any] = {}
        ws = self.workspace_for(name) if name else None
        if ws is not None:
            updates.update(ws.overrides())
        if name and name.lower() != (self.ado_project or "").strip().lower():
            updates["ado_project"] = name
            # An item's own project is the better fallback for its repos than the
            # DEFAULT project's — recomputed here so the scoped copy is self-consistent
            # (its code_project no longer depends on which project it came from).
            updates.setdefault("code_project", self.code_project_for(name))
        if not updates:
            return self
        return self.model_copy(update=updates)

    @property
    def effective_trigger_tags(self) -> list[str]:
        """All trigger tags (primary + extras + any per-workspace tag), de-duplicated,
        blanks dropped.

        Workspace tags belong here because the poll is a SINGLE query for the whole
        instance: a workspace that overrides ``trigger_tag`` but whose tag never
        reached the query would simply never see a work item — an override that
        silently disables the workspace it was meant to configure."""
        out: list[str] = []
        workspace_tags = [ws.trigger_tag for ws in self.effective_workspaces]
        for tag in [self.trigger_tag, *self.trigger_tags, *workspace_tags]:
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
    def teams_webhook_targets(self) -> list[WebhookTarget]:
        """Every Teams channel to post to, in order, as ``(name, url)``.

        Three sources, oldest first so an existing setup keeps its behaviour:
        ``teams_webhook_url`` (the original single channel), ``teams_webhook_channels``
        (the named list the dashboard edits), then the legacy ``teams_webhook_urls``.

        Inactive channels are dropped HERE, so no caller can forget to check the switch.
        Duplicates are removed by URL while keeping the first name seen — listing the same
        channel twice must not notify it twice, which is the whole reason this is one
        property instead of three reads.
        """
        seen: set[str] = set()
        out: list[WebhookTarget] = []

        def add(url: str, name: str = "") -> None:
            url = (url or "").strip()
            if url and url not in seen:
                seen.add(url)
                out.append(WebhookTarget(name=(name or "").strip(), url=url))

        add(self.teams_webhook_url, "primary")
        for entry in self.teams_webhook_channels or []:
            if isinstance(entry, str):        # a plain pasted URL
                add(entry)
            elif isinstance(entry, dict) and entry.get("active", True):
                add(str(entry.get("url") or ""), str(entry.get("name") or ""))
        for url in self.teams_webhook_urls or []:
            add(url)
        return out

    @property
    def teams_webhooks(self) -> list[str]:
        """Just the URLs of :attr:`teams_webhook_targets` — kept so callers that only need
        "how many / which URLs" don't have to know about names."""
        return [t.url for t in self.teams_webhook_targets]

    @property
    def muted_teams_channels(self) -> list[str]:
        """Names of channels switched off — reported by ``doctor`` so a muted channel is a
        visible choice rather than a notification that quietly never arrives."""
        return [
            str(e.get("name") or e.get("url") or "(unnamed)")
            for e in (self.teams_webhook_channels or [])
            if isinstance(e, dict) and not e.get("active", True)
        ]

    @property
    def command_user(self) -> str:
        """The user THIS machine acts for — ``assignee_trigger_user`` falling back to
        ``auto_transition_assignee``. This is the machine's OWNER: the identity the bot posts
        under, and the primary account whose /commands it obeys.

        For "who else may command it", read ``effective_command_users`` — never this alone."""
        return self.assignee_trigger_user or self.auto_transition_assignee

    @property
    def effective_command_users(self) -> list[str]:
        """Every account allowed to issue /commands and @mentions — the owner plus
        ``command_users``. De-duplicated, blanks dropped, order kept.

        Empty (no owner and no extras) = unrestricted, which is what a single-machine setup
        wants. Mirrors ``effective_trigger_tags``: a primary scalar plus an extras list,
        read through one property so no gate can consult half of it."""
        out: list[str] = []
        for user in [self.command_user, *(self.command_users or [])]:
            u = (user or "").strip()
            if u and u not in out:
                out.append(u)
        return out

    @property
    def command_allowlist(self) -> list[str]:
        """Who may issue /commands and @mentions — the roster, or ``[]`` (= anyone) when
        ``commands_from_anyone`` is on.

        Deliberately separate from ``effective_command_users``: that property also decides
        which ITEMS this machine owns (shared assignee-trigger tag), so opening the command
        gate through it would make the machine start picking up colleagues' work items too.
        Command gates read THIS; ownership keeps reading the roster."""
        return [] if self.commands_from_anyone else self.effective_command_users

    @property
    def command_catalog(self) -> list[tuple[str, bool, str]]:
        """``(command, is_advisory, label)`` for every configured slash command — the menu
        offered when inferring what a bare @mention meant."""
        action, advise = split_commands(self.comment_commands, self.advisory_commands)
        return [
            (cmd, cmd in advise, _COMMAND_LABELS.get(cmd.lower(), ""))
            for cmd in action + advise
        ]

    @property
    def comment_command_hint_html(self) -> str:
        """The "reply to me with /command" hint for ADO comments, DERIVED from
        ``comment_command`` + ``comment_advisory_commands``.

        Single source of truth: it used to be hand-written in three places
        (``pr_monitor``, ``reviewer_tracker`` ×2) which had drifted apart — one dropped
        ``/review``, another dropped ``/summary`` — and none reflected an operator who
        customised ``comment_command``, so the bot advertised commands it would ignore.
        Empty string when no slash command is configured (command trigger off), and the
        callers then omit the hint entirely rather than printing a dangling label."""
        action, advise = split_commands(self.comment_commands, self.advisory_commands)
        return _render_command_hint(action, advise, html_out=True)

    @property
    def comment_command_hint_markdown(self) -> str:
        """Markdown form of :attr:`comment_command_hint_html`, for Teams chat replies."""
        action, advise = split_commands(self.comment_commands, self.advisory_commands)
        return _render_command_hint(action, advise, html_out=False)

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
