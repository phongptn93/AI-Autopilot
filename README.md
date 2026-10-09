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
| 🔐 **Security scanner** | `ai-autopilot scan` — SAST + SCA + secrets (builtin rules, gitleaks, semgrep, trivy/npm/dotnet/pip‑audit) plus an OWASP‑driven AI pass that triages them. Fingerprinted findings with a baseline, suppressions with reason + expiry, SARIF export, an exit code CI can gate on, and a deterministic secret check on every PR. |
| 📊 **Live dashboard** | Overview · Board · Planning · Reviews · Queue · Analytics · Learning · Audit · History · Settings · Config — full‑width, filterable, drag‑and‑drop, password‑lockable. |
| 🧠 **Retrospective learning** | What auto‑review flags is remembered **per repo** and injected into the next brief, so the agent stops re‑earning the same findings. The **Learning** page shows every lesson, which ones feed the next run, and lets you prune a wrong one — History badges each run it warned (`🧠 N`). |
| 🩺 **`ai-autopilot doctor`** | Audits whether the configuration is *coherent* — the gap `/health` cannot see. Every check came from a failure diagnosed by hand: a Teams bot switched on with no app id (silently absent, not broken), a messaging endpoint on loopback, concurrency without worktrees, a setting whose companion switch is off. Config‑only: no network, no writes, safe in CI. |
| 🔌 **Extensible** | Python plugins (pre/post/skill hooks), scheduled loops, multi‑tenant, and Teams/Zalo/Email notifications. |
| ⚔️ **PR merge conflicts** | Tracks every active PR ADO cannot merge (since when, which files), tells the PR and the team **once**, and lists it first on the delivery report — a conflicted PR is never "ready to merge". Resolves on request (`/resolve`, or the Conflicts page) or automatically on its own PRs — following `execution_mode`, so in **interactive** mode it opens a Remote‑Control session you can attach to and steer: merges the target **into** the branch (no rebase, no force‑push), lets the agent settle only the conflicted hunks, and pushes only if no marker is left, no other file was touched, and tests + the security gate pass — otherwise it aborts and the branch is untouched. |
| 👀 **PR reviewer tracking** | Watches every active PR's reviewer list (any author) — auto‑reviews + votes when the bot is added as reviewer, reminds overdue human reviewers, and answers role commands (`/spec /qc /security /impact ...`) routed to specialist subagents. Under `execution_mode: interactive` an action (`/ai …`) opens a Remote‑Control session on the PR branch you can attach to; the session never pushes — tests and the auto‑review run first, then the push (never forced). |
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

> **New here?** [`docs/QUICK-SETUP.md`](docs/QUICK-SETUP.md) is the 5-minute version — install, first config, `doctor`, and turning on Fleet.

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
| `/dashboard` | Overview · Board · Board processes · Planning · History · Learning · Settings · Config · Capabilities |
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
| **Board** | Live Kanban of every autopilot item; drag‑and‑drop, search / type / date filters, per‑column cap + *Load more*, 15s auto‑refresh. A **lens per process** (BA → Dev → QC · your own) folds the pipeline columns — and the ADO states or tags a hand‑off parks in — into the few lanes that role reads, marking the ones where the ball is in its court: each tab carries the count waiting on that role, with an **Only my turn** toggle and a **▶ Run** button that releases the parking tag and starts that role's SDLC stages once a human has read the previous role's output. |
| **Board processes** | Define those lenses: the ordered **stages** each process reads and which of them are **its turn**. Work is a relay — the item moves, the turn moves with it, nobody re‑tags anything. The editor shows how many items wait on each process right now, flags a process with no turn of its own or two claiming the same column, and lists the tags really on your board. Tags stay optional, for a stream one process owns end to end. Saved to `board_lenses` and applied live. |
| **Planning** | Load your assigned work, run AI grouping & conflict analysis, then **Start now** or **Schedule**. Live scheduling view with history. |
| **Reviews** | Every active PR grouped by target branch — status badge, reviewer votes, conflicts, age, linked work item. Command‑palette reference for the role commands. |
| **History** | Paginated, filterable log of every execution (skill, PR, duration). Each row shows **which model** served the run and what it **cost** — hover the token count for the input / output / cache split. A run whose usage was never reported shows `—`, never `0`. |
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
| `trigger_states` | `New, To Do, Proposed, Active` | ADO states eligible for pickup. Role doors amend it: an **auto** role's wait state is always polled, a **manual** role's is dropped even when ticked — Settings labels each state with the rule that decides it |
| `stage_entry_tag` | `autopilot-run` | One-shot **run now** tag — skips the trigger tag and state checks, removed on pickup. Bare tag runs the role the current state names; **`autopilot-run:<role>`** (e.g. `autopilot-run:qc`) forces that role from any state, no configuration needed |
| `owner_can_command` | `true` | Does the owner (`assignee_trigger_user`) get to issue `/commands` and @mentions? Turn off for a shared machine that takes one person's items but obeys only `command_users`. Ownership is unaffected |
| — | — | **🔎 Trigger check** at `/dashboard/trigger-check?id=<id>`: replays the poller's pickup rules for one work item — project, ownership, state (and which role decided it), hold tags, run-now tags, the role that would run — and says why it will or will not be picked up |
| `autonomy_level` | `assisted` | `report` / `assisted` / `unattended` (L1 / L2 / L3) |
| `execution_mode` | `interactive` | `interactive` (steerable session) or `headless` |
| `interactive_close_on` | `pr_closed` | When an interactive console is closed: `pr_closed` (keep it + its worktree while the PR is open, close on merge/abandon), `result`, or `never` |
| `interactive_resume_on_rework` | `true` | PR feedback runs in that session's own worktree and **resumes its conversation** instead of a fresh worktree + fresh read |
| — | — | **Task room** at `/dashboard/task/<id>`: one page per work item — timeline, runs, agent log, the brief it ran on, spec drift, PR **diffs** and HTML preview |
| `timezone` | — | IANA zone every time window is read in, e.g. `Asia/Ho_Chi_Minh`. **Required** for quiet hours; the work window falls back to the machine's zone |
| — | — | **🔔 Alerts** — one Settings section holding everything that decides whether a human is interrupted. Thresholds, the digest, quiet hours and reviewer nudges used to live in five different sections while *Notifications* held only credentials |
| `alert_events` | `completed,failed,error,reminder,digest` | Which event kinds may be broadcast at all. `started` is **off by default** — "the bot picked up #123" is not actionable and doubles the traffic. Blank = everything |
| `alert_min_severity` | `info` | Floor applied after the event list: `info` / `warning` (somebody should look today) / `critical` (blocked right now) |
| `alert_dedup_enabled` | `true` | An alert already reported returns only when it **escalates** (the wait doubled) or after `alert_repeat_hours`. Without it, one PR stuck for a week is eight identical digest lines |
| `alert_repeat_hours` | `24` | Hours before an unactioned alert is raised again. `0` = report once, then only on escalation |
| `digest_skip_when_empty` | `true` | Nothing over a threshold **and** nothing shipped → the digest is not sent. A daily "✅ nothing is stuck" teaches a channel to skim past it |
| `digest_respect_quiet_hours` | `true` | Hold the digest inside `notify_hours_*` like every other notice. It used to be the one sender that ignored them |
| — | — | Per-channel routing: each entry in `teams_webhook_channels` may carry its own `events` / `severity`, so a dev channel takes failures while a PM channel takes the digest. Omit both and the channel inherits the global policy |
| — | — | In chat: **`/alerts`** (what is open, and what is silenced), **`/ack <id>`** (I am on it), **`/snooze <id> [days]`**, **`/unack <id>`** |
| `notify_hours_start` / `notify_hours_end` | — | When the autopilot may PING a human. Outside it, notices are **held** and delivered as one summary when the window opens |
| `notify_days` | `Mon…Fri` | Days the notification window applies |
| `notify_quiet_max_held` | `200` | Ceiling on held notices; oldest dropped first, the summary says how many |
| `spec_drift_enabled` | `true` | Agent reports what it had to decide that the item never settled; the autopilot files it as a **⚠️ SPEC-DRIFT** comment + tag + a row on `/dashboard/specs` |
| `spec_drift_tag` | `spec-update-needed` | Tag applied until a human marks the spec updated |
| `spec_drift_holds_item` | `false` | Hold the item for a human instead of letting it advance while the spec is stale |
| `pr_require_work_item_link` | `true` | Verify every PR names its work item — and attach the link when it does not |
| `process_health_enabled` | `false` | Periodic **process-health digest**: ad-hoc ratio, escaped defects per module, stuck items, tag rot. Read-only — it reports, never edits |
| `process_health_interval_hours` | `168` | How often the digest runs (0 = on demand only) |
| `process_health_window_days` | `14` | Window it measures (one sprint) |
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

## ⚖️ Trust you can see — the decision inbox and earned autonomy

| Feature | Where | What it gives you |
|---|---|---|
| **🎯 Decision inbox** | Tổng quan → Hộp quyết định (first in the menu, red badge) | Every pending human decision in one list — held items, spec-drift points, PR conflicts, fleet machines, knowledge to approve, critical/high security findings — tiered 🔴 today / 🟡 this week / 🟢 FYI, each with its action inline (approve a plan, decide a drift point, resume a machine, promote a lesson…). |
| **⚖️ Trust ladder** (`trust_ladder_enabled`, opt-in) | Thiết lập → ⚖️ Tự chủ & an toàn | Autonomy is earned per (project, work type): plan → draft PR → ready-for-review PR → unattended, promoted after `trust_min_runs` clean merges at `trust_promote_rate`, demoted after two bad runs in a row. `autonomy_level` is the ceiling. |
| **🧨 Risk gate** (on) | same | A run touching migrations, auth/permission, deploy/infra or dependency manifests — or more than `risk_max_files` files — is held for a person instead of being handed on, whatever the autonomy. |
| **📋 Plan first** | tag `plan-first` | The agent posts an implementation plan as a comment and waits; **✅ Duyệt kế hoạch** in the inbox (tag `plan-approved`) lets it build exactly that plan. |
| **💸 Budget & circuit breaker** | same | `item_budget_tokens` holds an item that has spent its budget instead of retrying; `circuit_breaker_failures` (5) pauses the machine after N failed runs in a row and puts a resume button in the inbox. |
| **🔒 Run-now lease** | fleet | Several machines seeing the shared run-now tag claim the item on the work item; the earliest claim runs it. |
| **🎬 Run timeline** | Phòng task → Diễn biến | Every run, retry, revision, spec-drift decision, fleet dispatch and audit event on one timeline, plus a "why the agent decided this" panel. |
| **👍/👎 One-tap feedback** | buttons on the completion notice | A reviewer's vote (and reason) is stored per run; a 👎 with a reason becomes a lesson when the learning loop is on. |
| **⭐ North-star KPIs** | Phân tích & ROI | Autonomous completion rate, time to first PR, needs-human share, reviewer approval — and "—" (with what is missing) for anything not measurable yet. |
| **🧪 Replay evals** | `ai-autopilot evals harvest` / `evals replay` | An exam built from real merged work: each case replays a past item in a throwaway worktree at its base commit and scores tests, conflict markers and overlap with the human change. |
| **⌘K / Ctrl+K** | everywhere | Jump to any page, open a task room by id, or search settings. The layout also works on a phone. |

## 🧭 Dashboard map, presets and the BA workflow

The sidebar is grouped by the question you arrive with — **🏠 Tổng quan · 📋 Công việc ·
📈 Chất lượng & báo cáo · 🔐 Bảo mật & kiểm toán · 🔄 Quy trình · 🛠 Hệ thống** — and every
link describes itself on hover. Settings has a search box over all ~200 fields.

| Feature | Where | What it gives you |
|---|---|---|
| **SDLC presets** | Quy trình → Vai trò & preset (and the Setup wizard's policy step) | 🟢 Safe start · 🔵 BA → Dev → QC → Review relay · 🟣 Fully autonomous. Each previews exactly what changes; a relay chain that would hand off into a trigger state, or put two roles behind one door, is refused before anything is written. |
| **Steered-relay test gate** | `sdlc_interactive_gate` (on) | In interactive mode the relay now runs the test gate when a session finishes — a red suite holds the item for a person instead of handing it to QC. The per-stage revise loop stays headless-only (a session a person is driving cannot be re-prompted). |
| **Requirements & specs** | Công việc → Yêu cầu & Spec | BA writes a requirement (need + acceptance criteria) → optionally hands it to the BA role → reads the rendered spec and its sandboxed mockup → sends feedback or approves (hands to Dev). Every decision is a comment on the work item. Specs are found under `specs/`, `specs-dxfac/`, `docs/specs/` and linked to their item by an `ADO: #1234` line. |
| **Spec drift, decided point by point** | Chất lượng & báo cáo → Lệch spec | The agent now reports what the spec **says** (quoted) and what the code **does**; the ADO notice follows the team's four-part rule (decision · *Mục / Hiện tại / Điều chỉnh* table · out-of-scope · needs a decision). A BA decides each point — update the spec, fix the code, or keep as is — and the item closes itself with one summary comment when the last point is decided. Ageing against `spec_drift_sla_days` (7), filters, and a Markdown export for the BA. |
| **Learning loop that closes** | Chất lượng & báo cáo → Tri thức | A health strip (rules · learned · recurring · stale). A learned line that recurs ≥3 times although its skill already carried it gets **📌 promote to rule** (moved into the always-loaded `.claude/rules`). One-off lines older than 90 days drop out of the skill and the brief, with a one-click prune. A forced brief keeps recurring lines over newer one-offs. |
| **Setup wizard** | Hệ thống → Cài đặt nhanh | Presets instead of expert questions, "save & check" that proves what you typed, an immediate warning for a missing workspace folder, and a "try one item" ending. |

## 🛰 Fleet — one central, many worker machines

Every machine is a complete autopilot. Fleet mode adds a **centre**: one VM holds the shared
configuration and watches/controls every worker. Connectivity is **one-way** (worker → central),
so a machine behind NAT or VPN takes part without opening a port.

| Capability | What it does |
|---|---|
| **Shared config** | Workers pull the central's settings by hash; secrets, paths, tags and `fleet_*` never travel (filtered on both ends). `fleet_local_keys` keeps a setting per machine. |
| **Remote control** | Pause / resume / sync / update one machine or the whole fleet. Commands are a queue the worker polls every `fleet_command_poll_seconds` (60s) — each one ends `done`, `failed` (with the worker's reason), `expired` or `cancelled`. |
| **Dispatch** | Hand a work item to a named machine or the freest eligible one (role-aware, load = running ÷ capacity). The worker claims it with its own trigger tag. |
| **Health** | Disk, uptime, capacity, tracker auth, failure streak, last error, poller state — per machine, plus a fleet-wide KPI strip. |
| **Alerts** | One notice when a worker goes offline, one when it returns — through the normal channels and alert policy. |
| **Shared knowledge** | Lessons learned on one machine are pooled at the centre and handed back once approved (or corroborated by N machines). |

Workers keep the last word: `fleet_accept_commands: false` refuses remote commands,
`fleet_accept_remote_update: false` refuses remote installs, and a paused worker can always be
resumed from its own Fleet page. Full guide: [`docs/fleet-guide.md`](docs/fleet-guide.md).

## 🔐 Security scanning

The same engine behind the pre‑PR review gate is a standalone security tool for the
repositories the autopilot works on — see [`docs/security-scan.md`](docs/security-scan.md).

```bash
ai-autopilot scan --repo ../Backend                       # builtin rules + gitleaks + semgrep + SCA + AI (fast)
ai-autopilot scan --scope diff --base development         # only what this branch changed
ai-autopilot scan --no-ai --tools builtin,gitleaks --fail-on critical    # CI: no API key needed
ai-autopilot scan --format sarif --out scan.sarif         # ADO Advanced Security / GitHub code scanning
```

| | |
|---|---|
| **Scanners** | `builtin` (pure‑Python rules: secrets, C#/.NET, Angular/TS, Python, SQL/config — always on) · `gitleaks` · `semgrep` · `sca` (trivy / `dotnet list --vulnerable` / `npm audit` / `pip-audit`). Missing binaries are skipped **and said so**. |
| **AI pass** | `agent-security-reviewer` + the `security-review` skill, read‑only. Handed the scanners' findings first: triages them (confirmed / false positive, with a reason) and then hunts what regexes cannot see — BOLA/IDOR, missing role checks, mass assignment. |
| **Identity** | Every finding gets a fingerprint (tool + rule + file + normalised snippet, *not* the line number), so the second scan reports **new / known / fixed** and the gate only trips on new. |
| **Suppressions** | `<workspace>/.autopilot/security-suppressions.yaml` — fingerprint + reason + optional expiry; expired ones come back and the run says so. |
| **Outputs** | table · JSON · SARIF 2.1.0 · HTML · Markdown. Exit `0` pass · `1` gate failed · `2` usage · `3` could not run. |
| **Everywhere else** | Scan loops (`mode: scan`), the pre‑PR gate (`security_scan.pr_gate_tools` on the diff), and `/dashboard/security` (lifecycle, suppress, trend). `ai-autopilot doctor` reports tools that are configured but not installed. |
| **PoC verification** (`--verify`) | Each new high+ finding gets a proof‑of‑concept built and run in a **throwaway git worktree** (`security-verify-poc` skill): confirmed → `confidence: high` + PoC stored; not reproducible → badged *unconfirmed* with the reason. Nothing is ever committed or pushed. |
| **DAST** (`--dast <target>`) | Probes a **running**, `owner_confirmed` app for OWASP API Top 10 (BOLA/IDOR with two identities, missing auth, function‑level authz, headers, verbose errors) via the `security-dast` skill. Gates fail closed: host allowlist, private‑address check, request cap, rate limit, mutations off by default. |

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
| [`docs/QUICK-SETUP.md`](docs/QUICK-SETUP.md) | ⚡ 5-minute install & first-run checklist, including Fleet. |
| [`docs/fleet-guide.md`](docs/fleet-guide.md) | 🛰 Fleet: central & worker pages, remote commands, dispatch, health, alerts, every setting. |
| [`docs/ai-autopilot-user-guide.html`](docs/ai-autopilot-user-guide.html) | Full usage & configuration guide (every setting explained). |
| [`docs/planning-sdlc-v2-full-guide.html`](docs/planning-sdlc-v2-full-guide.html) | Technical deep‑dive on Planning + SDLC v2. |
| [`docs/security-scan.md`](docs/security-scan.md) | Security scanning: scanners, AI triage, fingerprints/baseline, suppressions, CLI & CI usage. |
| [`config.example.yaml`](config.example.yaml) | Annotated example configuration. |

---

## 🧱 Tech stack

Python 3.11 · FastAPI · uvicorn · Claude Agent SDK · httpx · SQLAlchemy (async) + aiosqlite ·
pydantic‑settings · APScheduler · prometheus‑client · structlog · Jinja2 · pytest ·
Microsoft 365 Agents SDK (optional, `teams-bot` extra)
