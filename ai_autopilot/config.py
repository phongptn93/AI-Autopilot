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

import os
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


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
    ado_project: str = ""
    ado_pat: str = ""
    oauth_app_id: str = ""
    oauth_app_secret: str = ""

    # ── Triggering ──
    trigger_tag: str = "autopilot"
    processed_tag: str = "autopilot-done"
    review_tag: str = "autopilot-review"
    poll_interval_seconds: int = 30
    # ADO work-item states that are eligible for processing. The default suits the
    # Agile/Scrum/Basic templates; adjust to match your board (e.g. add "Doing",
    # "Committed", "Approved", or localized state names).
    trigger_states: list[str] = Field(
        default_factory=lambda: ["New", "To Do", "Proposed", "Active"]
    )

    # ── Repository / execution ──
    repo_working_directory: str = ""
    base_branch: str = "development"
    repos: list[RepoConfig] = Field(default_factory=list)
    max_concurrent: int = 1
    task_timeout_minutes: int = 30
    dry_run: bool = False
    # Run each execution in its own git worktree so concurrent items never share
    # a checkout (required for safe max_concurrent > 1). Disable to fall back to
    # in-place checkout in the repo directory.
    use_worktrees: bool = True
    worktrees_dir: str = ""  # empty → <system temp>/ai-autopilot-worktrees

    # ── Claude execution ──
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

    # ── Feedback loop / PR babysitter ──
    feedback_loop_enabled: bool = False
    max_revisions: int = 3

    # ── Scheduled loops (dependency sweeper, changelog drafter, …) ──
    scheduled_loops: list[ScheduledLoop] = Field(default_factory=list)

    # ── Auto-review ──
    auto_review_enabled: bool = True
    block_on_severity: str = "Critical,High"

    # ── Scheduling ──
    schedule_start: str = ""
    schedule_end: str = ""
    schedule_days: str = "Mon,Tue,Wed,Thu,Fri"

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
