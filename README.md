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
| 📊 **Live dashboard** | Overview · Board · Planning · Reviews · Queue · Analytics · Learning · Audit · History · Settings · Config — full‑width, filterable, drag‑and‑drop, password‑lockable. |
| 🧠 **Retrospective learning** | What auto‑review flags is remembered **per repo** and injected into the next brief, so the agent stops re‑earning the same findings. The **Learning** page shows every lesson, which ones feed the next run, and lets you prune a wrong one — History badges each run it warned (`🧠 N`). |
| 🩺 **`ai-autopilot doctor`** | Audits whether the configuration is *coherent* — the gap `/health` cannot see. Every check came from a failure diagnosed by hand: a Teams bot switched on with no app id (silently absent, not broken), a messaging endpoint on loopback, concurrency without worktrees, a setting whose companion switch is off. Config‑only: no network, no writes, safe in CI. |
| 🔌 **Extensible** | Python plugins (pre/post/skill hooks), scheduled loops, multi‑tenant, and Teams/Zalo/Email notifications. |
| 👀 **PR reviewer tracking** | Watches every active PR's reviewer list (any author) — auto‑reviews + votes when the bot is added as reviewer, reminds overdue human reviewers, and answers role commands (`/spec /qc /security /impact ...`) routed to specialist subagents. |
| 💬 **Two‑way Teams bot** | Optional Azure Bot integration — `/items /prs /review /status` plus free‑text read‑only queries ("PR nào của tôi đang bị block?"), never code‑mutating from chat. |

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
| `/dashboard` | Overview · Board · Planning · History · Learning · Settings · Config · Capabilities |
| `/health` | Readiness checks (ado / claude / disk) as JSON |
| `/metrics` | Prometheus metrics |
| `/api/webhook/ado` | ADO Service Hook → instant pickup (work items **and** PR comments) |
| `/api/messages` | Teams bot messaging endpoint (only mounted when `teams_agent_enabled` + Agent ID/secret/tenant are set) |

**⚡ How fast are `/ai` / `/review` replies picked up?** Three lanes, fastest wins:

| Lane | Latency | Setup |
|------|---------|-------|
| **Hot lane** (built-in) | ~`pr_hot_poll_interval_seconds` (3s) | None. Once the bot engages a PR, that PR is re-polled fast for `pr_hot_window_minutes` — follow-up replies feel chat-like **even on localhost**. |
| **Webhook** | ~1s, incl. the *first* command | Reachable URL required. *Project Settings → Service Hooks → Web Hooks* → event **“Pull request commented on”** → `http://<autopilot-host>:5080/api/webhook/ado`. On a local machine, expose the port first (`devtunnel host -p 5080` or `ngrok http 5080`) and use that public URL. |
| **Global poll** (fallback) | ≤ `pr_poll_interval_seconds` (15s) | None — always on, so a missed webhook or a cooled-down PR is only ever slow, never lost. |

The webhook endpoint filters bot-signed comments and plain chatter — only real
`/commands` trigger an inspection.

---

## 📊 Dashboard

| Page | What it does |
|------|--------------|
| **Overview** | Run metrics (success / failed / tokens) and recent activity. |
| **Board** | Live Kanban of every autopilot item; drag‑and‑drop, search / type / date filters, per‑column cap + *Load more*, 15s auto‑refresh. |
| **Planning** | Load your assigned work, run AI grouping & conflict analysis, then **Start now** or **Schedule**. Live scheduling view with history. |
| **Reviews** | Every active PR grouped by target branch — status badge, reviewer votes, conflicts, age, linked work item. Command‑palette reference for the role commands. |
| **History** | Paginated, filterable log of every execution (skill, PR, duration, tokens). |
| **Settings** | Edit all configuration in 16 grouped sections, an *Active tags* overview, live‑apply, and two transfer modes (below). |
| **Configuration** | Read‑only snapshot of the live config (secrets shown as set / not‑set). |
| **Queue** | Work the autopilot is holding for a human, with the reason, and one‑click **Resume**. |
| **Analytics** | Throughput, success / PR rate, runs‑per‑item, tokens‑per‑PR and duration over 7 / 14 / 30 / 90 days. |
| **Audit** | Append‑only log of every consequential action: config change, secret export, ticket created, resume, review. |

### Locking the dashboard

Set `dashboard_auth_password_hash` (via the Settings UI, which hashes it) to require a
login. Browsers get a **login page**; `curl` / probes / scripts keep working with HTTP
Basic (`-u :password`) and still receive a plain `401`. The session is a signed cookie —
no server‑side store, so it survives a restart — and its signing key is derived from the
password itself, so **changing the password logs every session out**.

`ai-autopilot doctor` raises an **error** if the dashboard is bound to `0.0.0.0` with no
password: anyone who reaches that port can otherwise read the whole config and click
Resume or Export.

### Export / import — two modes

| Mode | File | Carries |
|------|------|---------|
| **Share** | `.yaml` | Org, tags, states, pipeline map, thresholds. **No** PAT / token / webhook, and none of this host's paths, ports or trigger tag. |
| **Backup / migrate** | `.enc` | Everything **including** the PAT and every token, encrypted with `config_export_password`. |

The full export is **refused when `config_export_password` is empty** — encrypting under an
empty password produces a valid‑looking `.enc` whose key anyone can reproduce, so the file
would carry your PAT while looking protected.

Neither mode carries the dashboard password: a restore never clobbers the target host's own
credential, which does mean a **freshly migrated instance starts unlocked** until you set
one. Every full export is recorded in the Audit log.

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
| `batch_related_enabled` | `false` | Run a linked cluster as ONE agent run that opens **one PR per work item** (headless only) |
| `pr_scoring_enabled` | `true` | Grade each run; weak runs are held for a human |
| `sdlc_loop_enabled` | `false` | Opt into the closed‑loop SDLC engine (below) |
| `claude_model` | *(CLI default)* | `sonnet` / `opus` / `fable` / `haiku` — pin explicitly instead of trusting the bundled CLI's own default |
| `pr_reviewer_tracking_enabled` | `false` | Watch reviewer lists on every active PR (see [PR reviewer tracking](#-pr-reviewer-tracking)) |
| `teams_agent_enabled` | `false` | Two‑way Teams bot (see [Microsoft Teams bot](#-microsoft-teams-bot)) |
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

## 👀 PR reviewer tracking

Enable `pr_reviewer_tracking_enabled` to watch the reviewer list of **every active PR**
(any author, not just autopilot‑created ones) in the configured repos:

- **Add the bot as a reviewer** on any PR → it runs a structured AI review (summary ·
  findings by severity · checklist · verdict), posts it, and casts its own vote
  (`pr_auto_review_on_added`, **off by default** — casting a vote is consequential enough
  to need its own opt‑in). Re‑arms on new commits; never re‑reviews a failed attempt in a
  loop.
- **Human reviewers** are tracked for the **Reviews** dashboard page, and a polite
  reminder is posted (PR comment + Teams/Email/Zalo) if a reviewer sits vote‑less past
  `pr_reviewer_reminder_hours` (default 24h, `0` = off). Set
  `pr_reviewer_reminder_repeat_hours` to keep nudging every N hours until they vote —
  the default `0` nudges once and then stays quiet, so a PR stuck for a week is never
  mentioned again.
- **`pr_reviewer_target_branches`** restricts tracking/review/dashboard to PRs merging
  into specific branches — empty = every target.
- **Role commands** — reply on any PR the bot reviews:

  | Command | Role | Action | Type |
  |---------|------|--------|------|
  | `/ai <ask>` | DEV | Make the change, commit & push | action |
  | `/spec` | BA | Refresh spec files + sync the ADO work item (`update-spec`) | action |
  | `/test` | DEV/QC | Write/adjust automated tests | action |
  | `/review` | DEV | Code review | read‑only |
  | `/qc` | QC | Test‑scope analysis, proposed cases | read‑only |
  | `/security` | SA | OWASP‑focused review of the diff | read‑only |
  | `/impact` | BA | Blast‑radius / impact analysis | read‑only |
  | `/summary` | all | Plain‑language PR summary | read‑only |

  When `use_specialized_agents` is on (default), each routes to a purpose‑built subagent
  (`agent-spec-updater`, `agent-qc-manual`, `agent-security-reviewer`, `agent-pr-reviewer`,
  `agent-test-writer`, `agent-requirement-analyst`) for expert results, falling back to
  the generic skill if that subagent isn't present.

The bot's identity is auto‑detected from the ADO PAT (`connectionData`) — override with
`pr_bot_identity` if needed. For clean audit trails, register a **dedicated ADO service
account** for the bot rather than reusing a personal PAT.

### Cost ceilings

Review work is easy to run away with, because it is triggered by other people's activity
rather than by the autopilot's own queue:

| Setting | Default | Bounds |
|---------|---------|--------|
| `max_revisions` | `3` | Code revisions per work item. **Released when the PR merges or is abandoned** — the budget is per work item, so nothing else frees it. |
| `pr_advisory_max_per_commit` | `2` | How often `/review` (and the other read‑only commands) may run against the **same commit**. They rightly don't spend the revision budget, but each is still a full agent run, and re‑reviewing unchanged code repeats itself. Push a commit to reset. |
| `pr_auto_review_max_per_pr` | `0` (unlimited) | Lifetime auto‑reviews for one PR. Auto‑review re‑arms on every new commit, so a push‑heavy PR can otherwise be reviewed a dozen times. |
| `pr_review_max_concurrent` | `0` (share) | Parallel PR‑review work. `0` shares `max_concurrent` with task execution, so a batch of PRs can crowd out the runs that actually implement work items — set it lower to keep execution slots free. |

Run `ai-autopilot doctor` after changing these: it flags the combinations that read as
configured but can never fire (auto‑review with tracking off, a repeat‑reminder interval
with reminders disabled, a `pr_bot_identity` with pasted quotes that will never match).

### How a review is isolated

| Path | Trigger | Checkout |
|------|---------|----------|
| `review_pr` | `/review <repo> <pr>` in Teams, or a pasted PR link | Scratch **git worktree**, removed afterwards |
| Advisory command | `/review` `/qc` `/security` replied on a PR comment | **None** — `git fetch` the branch, review `origin/<branch>` in place |
| Auto‑review | Bot added as a reviewer | Same as advisory |

The advisory path skips the worktree on purpose: materialising a whole tree, then removing
it, is most of the latency of a run that by contract changes nothing.

All three **deny the file‑mutating tools** (`Write`, `Edit`, `MultiEdit`, `NotebookEdit`)
rather than only asking the agent not to change anything — advisory runs execute against
the *shared* workspace checkout, and even the worktree path can `git push`, so the worktree
isolates you from a stray edit but not the PR branch.

It is **not** a sandbox: `Bash` stays available because the review needs `git diff`, and a
shell can write files. The advisory path therefore also diffs `git status` around the run
and warns if the checkout changed.

---

## 💬 Microsoft Teams bot

Optional two‑way bot (`pip install .[teams-bot]`), additive to the existing one‑way
`teams_webhook_url` notifications — that keeps working unchanged either way. Registers
`/api/messages` via the [Microsoft 365 Agents SDK](https://github.com/microsoft/Agents-for-python)
once `teams_agent_enabled` + the Azure Bot's App ID / secret / tenant are all set. A
sideload‑ready manifest template lives in [`teams-app/`](teams-app/README.md).

**Commands** (chat 1:1 or @mention in a channel):

| Command | Does |
|---------|------|
| `/help` | Command list |
| `/status` | Quick health check |
| `/items` | Your own ADO work items (Teams email ↔ ADO assignee) |
| `/prs` | PRs you're author or reviewer on, with vote status |
| `/review <repo> <pr-id>` | Ask the bot to re‑review a PR it's already a reviewer on |

Unmatched free text is classified (read‑only intents only) by a single tool‑less Claude
call — e.g. *"PR nào của tôi đang bị block?"* — toggle with `teams_agent_nlu_enabled`.

**Deliberately not supported from Teams:** code‑mutating `/ai`, or casting a vote *as the
human who clicked a button*. The bot only holds app‑only credentials (no per‑user
delegated token), so it can act as itself but never impersonate the clicking user —
code changes still require replying directly on the PR in ADO, where the full diff
context is visible.

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
├── services/                # background poller, PR babysitter, reviewer tracker, state-sync
├── data/                    # SQLAlchemy async engine, entities, repositories
├── dashboard/               # Jinja2 server-rendered dashboard + settings form
├── notifications/           # Teams, Zalo, Email channels
├── teams_agent.py           # Two-way Microsoft Teams bot (optional, /api/messages)
├── plugins/                 # Python plugin loader (pre/post/skill hooks)
├── security.py · scheduling.py · tracking.py · multitenant.py · webhook.py
└── health.py · metrics.py · logging_config.py

tests/                       # pytest unit tests
docs/                        # HTML guides (user guide, technical notes)
teams-app/                   # Teams app manifest template (sideload package)
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

## 🩺 Troubleshooting

### `ValueError: the greenlet library is required` / `DLL load failed while importing _greenlet` (Windows)

SQLAlchemy's async engine needs `greenlet`'s compiled extension to load. This almost
always means the package was installed **into the system-wide Python** instead of an
isolated virtual environment (a stale or mismatched `greenlet` from a previous install
conflicts with the new one). Fix, in order:

1. **Install into a fresh venv** — don't `pip install` straight into your system Python:
   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   pip install ai_autopilot-<version>-py3-none-any.whl
   ```
2. Already on system Python? Reinstall `greenlet` clean:
   ```powershell
   pip uninstall greenlet -y
   pip install --no-cache-dir --force-reinstall greenlet
   ```
3. Confirm you're on 64‑bit Python: `python -c "import struct; print(struct.calcsize('P')*8)"` → must print `64`.
4. Install the [Microsoft Visual C++ Redistributable (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe) — the classic cause of "DLL load failed" for compiled Python extensions on Windows.
5. Still stuck on a brand‑new Python release? Fall back to a more established minor version (`py -3.12 -m venv .venv`) — `requires-python >= 3.11` supports 3.11–3.13 with the widest wheel coverage across the whole dependency tree.

## 📚 Documentation

| Doc | Contents |
|-----|----------|
| [`docs/ai-autopilot-user-guide.html`](docs/ai-autopilot-user-guide.html) | Full usage & configuration guide (every setting explained). |
| [`docs/planning-sdlc-v2-full-guide.html`](docs/planning-sdlc-v2-full-guide.html) | Technical deep‑dive on Planning + SDLC v2. |
| [`config.example.yaml`](config.example.yaml) | Annotated example configuration. |

---

## 🧱 Tech stack

Python 3.11 · FastAPI · uvicorn · Claude Agent SDK · httpx · SQLAlchemy (async) + aiosqlite ·
pydantic‑settings · APScheduler · prometheus‑client · structlog · Jinja2 · pytest ·
Microsoft 365 Agents SDK (optional, `teams-bot` extra)
