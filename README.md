# AI Autopilot

Autonomously process Azure DevOps work items with the **Claude Agent SDK**.

AI Autopilot polls an ADO board, classifies each work item tagged `autopilot`,
routes it to the matching Claude Code skill, runs Claude to implement it
(branch → code → commit → push), auto-reviews the change, opens a pull request,
and reports back on ADO, Microsoft Teams, Zalo and email.

> **v2.0 — Python rewrite.** This is a from-scratch Python port of the original
> .NET 8 worker service (preserved under [`legacy-dotnet/`](legacy-dotnet/)).
> The key upgrade: Claude is now driven through the official
> [`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/) instead of
> shelling out to the CLI and scraping stdout — so token usage, cost and results
> come back as **structured data**.

```
ADO Board                    AI Autopilot                      Claude Agent SDK
  |                               |                                |
  |  tag "autopilot" + New/ToDo   |                                |
  |------------------------------>|  classify (BE/FE/Bug/Req...)   |
  |                               |  git checkout -b feature/xxx   |
  |                               |------------------------------->|
  |                               |       /skill-command {id}      |
  |                               |<-------------------------------|
  |                               |  review → commit → push → PR   |
  |    comment + tag "done"       |                                |
  |<------------------------------|                                |
```

## Architecture

```
ai_autopilot/
├── app.py                 # FastAPI app factory + lifespan (replaces Program.cs)
├── __main__.py            # uvicorn entry point
├── config.py              # pydantic-settings config (YAML + env)
├── container.py           # composition root / dependency injection
├── logging_config.py      # structlog setup
├── metrics.py             # Prometheus metrics
├── health.py              # readiness checks (ado / claude / disk)
├── security.py            # RBAC policy
├── scheduling.py          # schedule-window guard
├── tracking.py            # token cost tracking + budget alerts
├── multitenant.py         # tenant manager
├── webhook.py             # ADO Service Hook queue
├── models/                # WorkItemInfo, ExecutionResult, TaskCategory
├── ado/                   # auth (PAT/OAuth), REST client, notifier
├── execution/             # Claude Agent SDK wrapper, executor, reviewer, retry, feedback
├── routing/               # classify → prioritise → route → decompose
├── notifications/         # Teams, Zalo, Email channels
├── data/                  # SQLAlchemy async engine, entities, repository
├── plugins/               # Python plugin loader (pre/post/skill hooks)
├── services/              # background poller + PR monitor
└── dashboard/             # Jinja2 server-rendered dashboard

tests/                     # pytest unit tests
legacy-dotnet/             # original .NET 8 implementation (reference)
```

## Quick start

```bash
# 1. Install (Python 3.11+)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Configure (see config.example.yaml + .env.example)
cp config.example.yaml config.yaml
export ANTHROPIC_API_KEY=sk-ant-...
export AUTOPILOT_ADO_PAT=...           # keep secrets in env, not YAML

# 3. Run
python -m ai_autopilot
```

**Windows:** just run `run.bat` — it creates the venv, installs dependencies on
first run, copies `config.yaml` from the example, and starts the service. Use
`run.bat install` to (re)install dependencies only.

The service listens on `:5080` by default:

| Endpoint            | Purpose                                  |
|---------------------|------------------------------------------|
| `/health`           | Readiness checks (JSON)                  |
| `/metrics`          | Prometheus metrics                       |
| `/api/webhook/ado`  | ADO Service Hook → instant pickup        |
| `/dashboard`        | Overview / History / Config / Capabilities |

## Configuration

Settings are loaded from `config.yaml` and overridden by `AUTOPILOT_*`
environment variables (nested keys use `__`). **Secrets should always come from
the environment.**

| Key | Env var | Default | Description |
|-----|---------|---------|-------------|
| `ado_organization` | `AUTOPILOT_ADO_ORGANIZATION` | — | ADO org URL |
| `ado_project` | `AUTOPILOT_ADO_PROJECT` | — | Project name |
| `ado_pat` | `AUTOPILOT_ADO_PAT` | — | Personal Access Token (Work Items R/W, Code R/W) |
| `repo_working_directory` | `AUTOPILOT_REPO_WORKING_DIRECTORY` | — | Local git repo path |
| `trigger_tag` | `AUTOPILOT_TRIGGER_TAG` | `autopilot` | Tag that triggers processing |
| `base_branch` | `AUTOPILOT_BASE_BRANCH` | `development` | Base for feature branches |
| `poll_interval_seconds` | `AUTOPILOT_POLL_INTERVAL_SECONDS` | `30` | Poll cadence |
| `max_concurrent` | `AUTOPILOT_MAX_CONCURRENT` | `1` | Concurrent executions |
| `use_worktrees` | `AUTOPILOT_USE_WORKTREES` | `true` | Run each execution in its own git worktree (safe parallelism) |
| `task_timeout_minutes` | `AUTOPILOT_TASK_TIMEOUT_MINUTES` | `30` | Per-task timeout |
| `autonomy_level` | `AUTOPILOT_AUTONOMY_LEVEL` | `assisted` | `report` / `assisted` / `unattended` (L1/L2/L3) |
| `feedback_loop_enabled` | `AUTOPILOT_FEEDBACK_LOOP_ENABLED` | `false` | Enable the PR babysitter |
| `dry_run` | `AUTOPILOT_DRY_RUN` | `false` | Log only, never execute |
| `claude_model` | `AUTOPILOT_CLAUDE_MODEL` | SDK default | Model override |

See [`config.example.yaml`](config.example.yaml) for the full set (retry, RBAC,
scheduling, multi-repo, multi-tenant, notifications, budget).

## Skill routing

| Condition | Category | Skill |
|-----------|----------|-------|
| Title starts with `[BE]` | BackendTask | `/implement-task-be {id}` |
| Title starts with `[FE]` | FrontendTask | `/implement-task-fe {id}` |
| Title starts with `[QC]` | TestTask | `/qc-test-management {id}` |
| WorkItemType = `Bug` | Bug | `/bugfix-workflow {id}` |
| WorkItemType = `Requirement`/`User Story` | Requirement | `/analyze-requirement {id}` |
| Keywords (api, controller, service…) | BackendTask | `/implement-task-be {id}` |
| Keywords (component, page, angular…) | FrontendTask | `/implement-task-fe {id}` |

## Autonomy & loops (loop-engineering)

Beyond reactive work-item processing, the service implements the
[loop-engineering](https://github.com/cobusgreyling/loop-engineering) patterns:

**Autonomy levels** (`autonomy_level`) — phased rollout from observation to full automation:

| Level | Value | Behaviour |
|-------|-------|-----------|
| L1 | `report` | Classify and comment what it *would* do; no code changes |
| L2 | `assisted` | Execute and open a **draft** PR for human review (default) |
| L3 | `unattended` | Execute and open a normal PR, auto-resolving the item |

**Isolated worktrees** — each execution runs in its own `git worktree`, so
`max_concurrent > 1` is safe (no shared-checkout collisions).

**PR babysitter** (`feedback_loop_enabled: true`) — watches open autopilot PRs for
unresolved human review comments and feeds them back to Claude to revise the
branch (bounded by `max_revisions`).

**Scheduled loops** (`scheduled_loops`) — run skills on a cadence (cron or
interval), each opening a PR. Use for dependency sweeps, changelog drafting,
CI sweeping, etc.:

```yaml
scheduled_loops:
  - name: dependency-sweeper
    prompt: "/update-deps"
    cron: "0 6 * * 1"        # Mondays 06:00
  - name: changelog-drafter
    prompt: "/draft-changelog"
    interval_minutes: 1440    # daily
```

## Plugins

Drop a `*.py` file in the `plugins/` directory that subclasses `PreProcessor`,
`PostProcessor` or `SkillProvider`:

```python
from ai_autopilot.plugins import PreProcessor
from ai_autopilot.models import WorkItemInfo

class TitleNormalizer(PreProcessor):
    name = "title-normalizer"
    version = "1.0.0"

    async def pre_process(self, item: WorkItemInfo) -> WorkItemInfo:
        item.title = item.title.strip()
        return item
```

## Development

```bash
pytest            # run unit tests
ruff check .      # lint
mypy ai_autopilot # type-check
```

## Deployment

```bash
docker compose up --build          # app on :5080
docker compose --profile monitoring up   # + Prometheus + Grafana
```

Kubernetes manifests live in [`k8s/`](k8s/).

## Tech stack

- Python 3.11 · FastAPI · uvicorn
- Claude Agent SDK (`claude-agent-sdk`)
- httpx · SQLAlchemy (async) + aiosqlite · pydantic-settings
- prometheus-client · structlog · Jinja2 · APScheduler
- pytest (tests)
