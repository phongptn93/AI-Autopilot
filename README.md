<div align="center">

# 🤖 AI Autopilot

**An autonomous software engineer that turns Azure DevOps work items into reviewed pull requests.**

Polls your ADO board, understands each tagged work item, and drives **Claude Code** to implement it end‑to‑end — branch → code → self‑review → PR — then reports back on ADO, Teams, Zalo and email. A built‑in web dashboard lets you watch, plan, and steer everything.

![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)
![Claude Agent SDK](https://img.shields.io/badge/Claude-Agent%20SDK-8A63D2)
![Tests](https://img.shields.io/badge/tests-pytest-brightgreen)
![Status](https://img.shields.io/badge/status-active-success)

</div>

---

## ✨ Highlights

| | |
|---|---|
| 🎯 **Tag‑driven autopilot** | Picks up work items by tag + state, classifies (BE/FE/Bug/QC/Requirement), and routes to the right skill. |
| 🧑‍✈️ **Three autonomy levels** | `report` (comment only) → `assisted` (draft PR) → `unattended` (auto PR + resolve). Roll out trust gradually. |
| 🧭 **Planning workbench** | Load your work, let AI group it & flag hidden conflicts, then **start now** or **schedule** a run. |
| 🔀 **Dependency‑aware scheduling** | Orders work by the ADO link graph (0 tokens) and avoids running conflicting items concurrently — with an AI conflict feed‑back loop. |
| 🔁 **Closed‑loop SDLC (v2)** | Optional multi‑stage engine (analyze → design → implement → test → review → PR) with per‑stage gating and multi‑machine handoff. |
| 🛡️ **Safe by design** | Isolated git worktrees, auto security review, objective run scoring, and a single tag/state policy table. |
| 📊 **Live dashboard** | Overview · Board · Planning · History · Settings · Config — full‑width, filterable, drag‑and‑drop. |
| 🔌 **Extensible** | Python plugins (pre/post/skill hooks), scheduled loops, multi‑tenant, and Teams/Zalo/Email notifications. |

> **v2.0 — Python rewrite.** A from‑scratch port of the original .NET 8 worker
> (kept under [`legacy-dotnet/`](legacy-dotnet/)). Claude is now driven through the
> official [`claude-agent-sdk`](https://pypi.org/project/claude-agent-sdk/), so token
> usage, cost and results return as **structured data**.

---

## 🔄 How it works

```
Azure DevOps                 AI Autopilot                       Claude Code
     │                            │                                  │
     │  tag + trigger state       │                                  │
     ├───────────────────────────►│  classify · RBAC · schedule      │
     │                            │  (dependency + AI conflicts)     │
     │                            │  git worktree ─ isolated branch  │
     │                            ├─────────────────────────────────►│
     │                            │        implement the item        │
     │                            │◄─────────────────────────────────┤
     │                            │  auto‑review → score → PR        │
     │   comment · tag · state    │                                  │
     │◄───────────────────────────┤                                  │
```

Every `poll_interval_seconds` (default 30s) the poller fetches pending items, gates them
through RBAC and the schedule window, orders them with the dependency scheduler
(deferred items are re‑evaluated next cycle), runs the ready ones, scores the result, and
applies the configured **outcome** (tag + state) plus a comment. Progress is persisted, so
a restart resumes exactly where it left off.

---

## 🚀 Quick start

```bash
# 1. Install (Python 3.11+)
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Configure — keep secrets in the environment, not YAML
cp config.example.yaml config.yaml
export ANTHROPIC_API_KEY=sk-ant-...
export AUTOPILOT_ADO_PAT=...

# 3. Run
python -m ai_autopilot
```

**Windows:** run `run.bat` — it creates the venv, installs deps on first run, copies
`config.yaml` from the example, and starts the service (`run.bat install` reinstalls deps).

The service listens on **`:5080`** by default:

| Endpoint | Purpose |
|----------|---------|
| `/dashboard` | Overview · Board · Planning · History · Settings · Config · Capabilities |
| `/health` | Readiness checks (ado / claude / disk) as JSON |
| `/metrics` | Prometheus metrics |
| `/api/webhook/ado` | ADO Service Hook → instant pickup |

---

## 📊 Dashboard

| Page | What it does |
|------|--------------|
| **Overview** | Run metrics (success / failed / tokens) and recent activity. |
| **Board** | Live Kanban of every autopilot item; drag‑and‑drop, search / type / date filters, per‑column cap + *Load more*, 15s auto‑refresh. |
| **Planning** | Load your assigned work, run AI grouping & conflict analysis, then **Start now** or **Schedule**. Live scheduling view with history. |
| **History** | Paginated, filterable log of every execution (skill, PR, duration, tokens). |
| **Settings** | Edit all configuration with grouped sections, an *Active tags* overview, live‑apply, and Export/Import (secrets excluded). |
| **Configuration** | Read‑only snapshot of the live config (secrets shown as set / not‑set). |

---

## ⚙️ Configuration

Settings load from `config.yaml`, overridden by `AUTOPILOT_*` environment variables
(nested keys use `__`, e.g. `AUTOPILOT_SMTP__PASSWORD`). **Secrets should always come from
the environment.** A full, explained reference lives in
[**`docs/ai-autopilot-user-guide.html`**](docs/ai-autopilot-user-guide.html) and
[`config.example.yaml`](config.example.yaml).

Most‑used keys:

| Key | Default | Description |
|-----|---------|-------------|
| `ado_organization` / `ado_project` | — | ADO org URL and the work‑item project |
| `code_project` | — | Project holding repos/PRs, if different from the work‑item project |
| `ado_pat` 🔒 | — | PAT (Work Items R/W, Code R/W). Prefer `AUTOPILOT_ADO_PAT` |
| `workspace_directory` | — | Root holding the shared `.claude/` and repo subfolders |
| `trigger_tag` | `<host>-autopilot` | Per‑machine tag that triggers processing |
| `assignee_trigger_tag` | `ai-autopilot` | Shared team tag — processed only for `assignee_trigger_user` |
| `trigger_states` | `New, To Do, Proposed, Active` | ADO states eligible for pickup |
| `autonomy_level` | `assisted` | `report` / `assisted` / `unattended` (L1 / L2 / L3) |
| `execution_mode` | `interactive` | `interactive` (steerable session) or `headless` |
| `use_worktrees` | `true` | Isolated git worktree per task (required for `max_concurrent > 1`) |
| `max_concurrent` | `1` | Concurrent executions |
| `dependency_scheduling_enabled` | `true` | Order work by the ADO link graph (0 tokens) |
| `pr_scoring_enabled` | `true` | Grade each run; weak runs are held for a human |
| `sdlc_loop_enabled` | `false` | Opt into the closed‑loop SDLC engine (below) |
| `dry_run` | `false` | Log only — never execute or write to ADO |

### Outcomes → tag + state

A single policy table maps each outcome to the ADO **tag** to add and **state** to set
(blank = skip): `in_progress` · `review` · `done` · `report` · `needs_human` · `failed`.
This is the one place that controls all tagging and state transitions — edit it in
**Settings → Outcomes**.

---

## 🧭 Key capabilities

### Autonomy levels

| Level | Value | Behaviour |
|-------|-------|-----------|
| L1 | `report` | Classify and comment what it *would* do; no code changes |
| L2 | `assisted` | Execute and open a **draft** PR for human review *(default)* |
| L3 | `unattended` | Execute and open a normal PR, auto‑resolving the item |

### Dependency‑aware scheduling

Reads the ADO link graph and, without spending tokens, waits on **Predecessor** links and
never runs **Related** items concurrently (they'd fight in git). Deferred items re‑evaluate
each cycle, so waves emerge naturally. The **Planning → Analyze** action can additionally
ask Claude to flag *hidden* conflicts; confirmed ones feed back into scheduling
(`scheduler_use_ai_conflicts`).

### Closed‑loop SDLC engine (v2) — opt‑in

Drives an item through a **profile‑selected** sequence of stages — `analyze` · `design` ·
`implement` · `test` · `review` · `pr` — gating each with the run scorer, revising on
failure under one **shared budget**, and escalating when it's spent. Built‑in profiles:
`full`, `dev` (`implement→review→pr`), `ba`, `qc`, `review`, `design` (extend via
`sdlc_profiles`).

**Profile selection** (highest wins): `sdlc:<name>` item tag → `sdlc_stages` →
`sdlc_profile` → `sdlc_type_profiles[type]` → `sdlc_default_profile`.

**Multi‑machine handoff** — each machine runs one role and sets an ADO state the next
machine triggers on:

| Machine | `sdlc_profile` | `trigger_states` | `sdlc_profile_states` |
|---------|----------------|------------------|-----------------------|
| BA | `ba` | New, Proposed | `{ ba: "Ready for Dev" }` |
| Dev | `dev` | Ready for Dev | `{ dev: "Ready to Test" }` |
| Tester | `qc` | Ready to Test | `{ qc: "Ready to Deploy" }` |

Progress is persisted per item so a crash resumes mid‑loop; a startup check refuses a
handoff into the machine's own `trigger_states`. **Headless‑only. Default off → zero
behaviour change.**

### Scheduled loops & PR babysitter

```yaml
scheduled_loops:
  - name: dependency-sweeper
    prompt: "/update-deps"
    cron: "0 6 * * 1"          # Mondays 06:00
  - name: changelog-drafter
    prompt: "/draft-changelog"
    interval_minutes: 1440      # daily
```

Enable `feedback_loop_enabled` to have the **PR babysitter** watch open autopilot PRs for
unresolved review comments and feed them back to Claude to revise (bounded by
`max_revisions`).

---

## 🧩 Skill routing

| Condition | Category | Skill |
|-----------|----------|-------|
| Title `[BE]` / backend keywords | BackendTask | `/implement-task-be {id}` |
| Title `[FE]` / frontend keywords | FrontendTask | `/implement-task-fe {id}` |
| Title `[QC]` / `[TEST]` | TestTask | `/qc-test-management {id}` |
| WorkItemType `Bug` | Bug | `/bugfix-workflow {id}` |
| WorkItemType `Requirement` / `User Story` | Requirement | `/analyze-requirement {id}` |

> In AI‑native mode (a `workspace_directory` is set) the agent chooses the right skill(s)
> itself; hardcoded routing is the legacy fallback.

---

## 🔌 Plugins

Drop a `*.py` file in `plugins/` that subclasses `PreProcessor`, `PostProcessor`, or
`SkillProvider`:

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

---

## 🏗️ Architecture

```
ai_autopilot/
├── app.py / __main__.py     # FastAPI app factory + uvicorn entry
├── config.py                # pydantic-settings (YAML + env)
├── container.py             # composition root / dependency injection
├── models/                  # WorkItemInfo, ExecutionResult, TaskCategory
├── ado/                     # auth (PAT/OAuth), REST client, notifier
├── execution/               # Claude SDK wrapper, executor, reviewer, scorer, SDLC engine
├── routing/                 # classify → prioritise → schedule → route → decompose
├── services/                # background poller, state-sync, scheduled loops
├── data/                    # SQLAlchemy async engine, entities, repositories
├── dashboard/               # Jinja2 server-rendered dashboard + settings form
├── notifications/           # Teams, Zalo, Email channels
├── plugins/                 # Python plugin loader (pre/post/skill hooks)
├── security.py · scheduling.py · tracking.py · multitenant.py · webhook.py
└── health.py · metrics.py · logging_config.py

tests/                       # pytest unit tests
docs/                        # HTML guides (user guide, technical notes)
legacy-dotnet/               # original .NET 8 implementation (reference)
```

---

## 🛠️ Development

```bash
pytest              # run unit tests
ruff check .        # lint
mypy ai_autopilot   # type-check
```

## 📦 Deployment

```bash
docker compose up --build                 # app on :5080
docker compose --profile monitoring up    # + Prometheus + Grafana
```

Kubernetes manifests live in [`k8s/`](k8s/).

## 📚 Documentation

| Doc | Contents |
|-----|----------|
| [`docs/ai-autopilot-user-guide.html`](docs/ai-autopilot-user-guide.html) | Full usage & configuration guide (every setting explained). |
| [`docs/planning-sdlc-v2-full-guide.html`](docs/planning-sdlc-v2-full-guide.html) | Technical deep‑dive on Planning + SDLC v2. |
| [`config.example.yaml`](config.example.yaml) | Annotated example configuration. |

---

## 🧱 Tech stack

Python 3.11 · FastAPI · uvicorn · Claude Agent SDK · httpx · SQLAlchemy (async) + aiosqlite ·
pydantic‑settings · APScheduler · prometheus‑client · structlog · Jinja2 · pytest
