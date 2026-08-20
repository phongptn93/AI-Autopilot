# AI Autopilot — Technical Notes

> Engineering reference for the AI Autopilot service. Describes the system **as it
> is built today**, not an aspiration. Companion deck:
> `AI-Autopilot-Technical-Architecture.pptx`.

**Version:** 2.0 (Python rewrite) · **Runtime:** Python 3.11, FastAPI/uvicorn ·
**Agent:** Claude Agent SDK + interactive Claude Code (Remote Control).

---

## 1. What it is

A background service that autonomously processes **Azure DevOps (ADO) work items**.
It polls a board, picks up items carrying a trigger tag, hands each one to Claude to
implement (branch → code → commit → push → PR), auto-reviews the change, and reports
back on ADO (state + tags + comment) and chat channels. It then keeps the item's ADO
state in sync as its PR is merged and deployed.

The design goal is a **control plane around an autonomous agent**: Python owns
scheduling, isolation, idempotency, state and reporting; Claude owns the actual
engineering reasoning and file edits.

---

## 2. Process model & composition

- **`app.py`** — FastAPI application factory + `lifespan`. Exposes `/health`,
  `/metrics`, `/api/webhook/ado`, and the dashboard router. On startup it builds the
  `Container` and launches four long-running background services; on shutdown it stops
  them and disposes the container.
- **`container.py`** — the composition root. A single `Container` instance holds every
  singleton for the process lifetime (HTTP client, DB, repositories, ADO auth/client,
  notifier + channels, executor + reviewer + retry + feedback, router/decomposer,
  schedule guard, RBAC, cost tracker, tenants, plugins, webhook queue). The FastAPI app
  stores it on `app.state`; background services consume it.

### Background services (all `start()`/`stop()` in the lifespan)

| Service | File | Responsibility |
|---|---|---|
| `AdoPollerService` | `services/poller.py` | The heart — poll the board, dispatch work, finalise results |
| `StateSyncService` | `services/state_sync.py` | Auto state transitions: merge → state, deploy → state, parent roll-up |
| `PrMonitorService` | `services/pr_monitor.py` | Track open autopilot PRs |
| `LoopScheduler` | `services/loop_scheduler.py` | Run skills on a cron/interval cadence |

Everything is **cooperative async** on a single event loop; there is no thread pool.
Concurrency is bounded by an `asyncio` semaphore (`max_concurrent`).

---

## 3. The poller pipeline

One poll cycle (`_poll_and_process`, every `poll_interval_seconds`, default 30s):

1. **Finalise live sessions** — pick up any interactive session that has written its
   `result.json` and apply its outcome (see §5). Also reconciles orphaned live
   sessions left by a restart.
2. **Schedule-window guard** — skip outside configured working hours.
3. **Drain webhook queue** — items pushed by an ADO Service Hook get processed first.
4. **Reconcile reopened** — items a human dragged back into a *trigger state* have their
   skip tags cleared so they re-enter the pipeline.
5. **Query pending** — WIQL for items whose **tag** is one of the trigger tags and whose
   **state** is one of the trigger states.
6. **Filter** — drop items already processed this run, or carrying a skip tag
   (`processed` / `review` / `escalation` / **`live`**), or under retry backoff / retry
   exhaustion.
7. **Classify & prioritise** — route each item to a category, sort by priority.
8. **Process** — run each through `_process` under the concurrency gate.

`_process` runs RBAC, pre-processor plugins, then dispatches to the **AI-native** path
(`_process_agent`) when a workspace is configured, or the **legacy** hardcoded
classify→skill→git path otherwise.

---

## 4. Two execution modes

`_process_agent` chooses by `execution_mode`:

### Headless (default) — `run_agent`
Runs Claude through the **Claude Agent SDK** from the task's scratch workspace. The
control plane streams events, then reads the structured result the agent writes to
`.autopilot/runs/<id>.json`. Token usage and cost come back as structured data.

### Interactive — `dispatch_interactive`
Launches a real, **Remote-Control-enabled** Claude Code session (a separate console) for
the item. A human can attach from claude.ai to watch or steer it. It is fire-and-forget:
the session writes the same `result.json` contract; the poller finalises it on a later
cycle. On dispatch the item is tagged **`autopilot-live`** (a skip tag) so a restart
never launches a duplicate console for the same item.

**Session lifetime.** The CLI is a REPL: it writes its result and then sits idle
forever, so nothing closes the console on its own. On dispatch the console's pid is
recorded in `.autopilot/runs/<id>.session.json` (on disk, so a restarted autopilot can
still close a session it did not launch). `interactive_close_on` decides when
`close_interactive` fires — it kills the whole tree (`taskkill /T` on Windows, the
process group elsewhere) after checking the pid is still ours, so pid reuse can't hit a
bystander:

| Value | Behaviour |
|---|---|
| `pr_closed` *(default)* | Console **and** scratch survive finalisation while the PR is open, so review feedback can be worked in the same session/worktree. The PR babysitter's scan closes both once the item has no active PR (merged or abandoned) — guarded on the item having already written a result, so a task that simply has no PR yet is never touched. |
| `result` | Closed the moment the task writes its result (scratch released with it). |
| `never` | Left open — consoles pile up; for debugging only. |

A work item deleted from ADO always closes immediately: nothing can reuse it.

**Re-entry resumes the session too.** A human comment or @mention on an item the bot
already worked goes through `_dispatch_with_comment` → `dispatch_interactive`, which used
to *wipe* the deterministic `agent-<id>` scratch and open a virgin console. It now reuses
that scratch when a session reserved it, closes the old console, and launches with
`--continue` — same worktree, same conversation, plus the new comment.

### Process health (`process_health.py` + `ProcessHealthService`)
A team's process doc assigns standing reviews — ad-hoc ratio each sprint, escaped
defects per module each month, tag catalogue each quarter — and those are the first
duties to lapse, because each is a manual query nobody owns on a busy week. The metrics
module is a **leaf** and **pure** (items in, numbers out) so every threshold is testable
without ADO; the service fetches, renders and broadcasts through the notifier's single
fan-out. It **only reads**: every finding is a judgement the process assigns to a
person, and silently "fixing" one would erase the evidence that the process needs
attention. Two guards keep it credible — a clean report is computed but not sent (a
digest that pings daily saying "nothing" gets muted, and then the real one is too), and
a metric whose ADO field the project never defined reports nothing rather than a
confident zero.

**Rework resumes the session** (`interactive_resume_on_rework`, default on). Claude Code
keys its transcripts by cwd, so running the rework anywhere else means starting from
zero. When `/ai` feedback lands on a PR an interactive session opened, `revise` closes
that (idle) console, then runs in the session's **own scratch worktree** with
`resume = <newest transcript id for that cwd>` — the branch, the build state and the
conversation are all already there. Any failure to reuse falls back to a normal fresh
checkout: a rework is never blocked by the optimisation.

Both modes converge on the same `result.json` contract and the same outcome handling.

---

## 5. Per-task isolation (worktree scratch)

Each AI-native task runs in its own scratch workspace so it never touches the user's main
checkout and concurrent tasks never collide (`_acquire_agent_scratch`):

- Base dir: **`<workspace>/.aiwt`** — a dotfolder inside the workspace (so
  `discover_repos` skips it). Override with `worktrees_dir`.
- Layout: `<base>/agent-<id>[-<uuid>]/` containing a **copy** of the shared `.claude`
  (so teardown can never delete the real one) plus a **`git worktree`** of each allowed
  repo.
- **Per-repo base branch** (`_resolve_base_ref`): tries `origin/<base_branch>`, then the
  repo's own default (`origin/HEAD`), then `main`/`master`/`develop`/`development`. A repo
  with no usable base is skipped rather than aborting the whole scratch — different repos
  in one workspace can have different base branches.
- **Interactive** sessions use a **deterministic** path `agent-<id>` (cleaned first) so an
  orphaned session can still be finalised after a restart.
- `fetch` uses `--no-recurse-submodules` (some repos vendor a private/relative submodule
  that isn't reachable from a dev machine).
- **Teardown** (`release_scratch`): `git worktree remove --force` each repo, `worktree
  prune`, then remove the dir — best-effort, never raises. Interactive scratches are
  released only when their console is closed (see `interactive_close_on`), which is also
  why the console is killed *first*: on Windows a live CLI holding the scratch as cwd
  makes `worktree remove` fail. Startup pruning likewise skips any scratch that still
  holds a `*.session.json` handle.
- **Stale branch claims** (`_free_branch_for_checkout`, run before every `checkout -B` /
  `worktree add -B`): a scratch that outlived its run — an interactive session never
  finalised, a killed task — keeps its branch checked out, and git then refuses every
  later run on that item with *"'<branch>' is already used by worktree at …"*. Prune
  first (a dangling registration alone is the common case), then evict — but **only**
  worktrees under `_scratch_base` that hold nothing uncommitted. A human's worktree, or
  one with live edits, raises a message saying what to do instead: losing someone's work
  to unblock a robot is the worse failure. `prepare_stage_branch` passes its own worktree
  as `exclude` (on a resumed SDLC loop it legitimately already holds the branch) and logs
  an error if the checkout still didn't take — otherwise stages commit onto a detached
  HEAD and the `pr` stage silently finds nothing to push.

---

## 6. Outcome policy — the single mutation point

All ADO tag/state changes for a pipeline outcome flow through one function,
`outcome_policy(cfg, outcome) → (tag, state)`, applied by `_apply_outcome`. This keeps
the mapping configurable and in one place:

| Outcome | Tag | State (config key) |
|---|---|---|
| `in_progress` | — | `state_in_progress` |
| `review` | `review_tag` | `state_in_review` |
| `done` | `processed_tag` | `resolved_state` |
| `report` | `processed_tag` | `state_report` |
| `needs_human` | `escalation_tag` | `state_needs_human` |
| `failed` | `failed_tag` (or `processed_tag`) | `state_failed` |

Blank tag/state or `dry_run` → skipped. Best-effort: ADO-client failures are logged and
never block the pipeline. Autonomy level decides whether a completed run maps to `review`
(assisted → draft PR) or `done` (unattended).

---

## 7. Auto state transitions (`StateSyncService`)

Opt-in (`auto_transition_enabled`), ~90s loop, idempotent (seen-set / baseline), gated by
**trigger tag** and optionally **assignee** (`auto_transition_assignee`, matched against
display name *or* email):

- **Merge → state** — a merged PR on a bot branch moves its work item to `on_merge_state`
  (e.g. *Ready to Deploy*).
- **Deploy → state** — when a new successful deploy build appears, items sitting in
  `on_merge_state` advance to `on_deploy_state` (e.g. *Ready to Testing*). First scan sets
  a baseline instead of transitioning the backlog.
- **Parent roll-up** — a parent's state is derived from its children's states via a
  configurable `parent_rollup_map` (child state → parent state), using the least-advanced
  child.

### Cross-project scoping
Work items and code often live in **different ADO projects** (e.g. items in
`TLCL-DxFac`, repos/PRs/builds in `DxFactory`). `code_project` sets the project used for
all git/PR/build calls (`_git_url`) while work-item calls stay on `ado_project` (`_url`).
Without this, merge/deploy detection silently queries the wrong project and never fires.

---

## 8. Data & idempotency

- **Persistence**: SQLAlchemy **async** engine + aiosqlite. `Database` (engine/session),
  `ExecutionRepository` (execution records, cost, running-state recovery),
  `StateRepository` (a `PipelineState` per item: QUEUED / IN_PROGRESS / IN_REVIEW /
  NEEDS_HUMAN / …).
- **Idempotency / no double-processing** is layered:
  - ADO **skip tags** (`processed` / `review` / `escalation` / `live`) exclude an item
    from the pending query.
  - An in-memory `_processed` set (1-hour TTL) prevents same-cycle re-pick.
  - On startup, `requeue_in_progress` + `fail_running` recover runs left mid-flight by a
    crash; `prune_orphans` cleans scratch a crash left behind.
- **Known gap**: the live interactive-session map (`_live`) is in-memory. It is now
  backstopped by the `autopilot-live` tag + deterministic scratch + orphan-finalise, but
  the DB is not yet the single source of truth for in-flight work.

### Quality log — measuring rework, and learning from it (`quality_events`, `learning.py`)

Every "this had to be redone" counter in the system is a **budget** built to stop a
runaway loop, not a record: `pr_command_states.revisions` is zeroed when the PR closes,
`sdlc_loop_states` rows are deleted on success, `PrReviewerState` keeps only the
*current* vote, and `RetryPolicy` lives in a dict a restart empties. Each is cleared
precisely when the number finally means something — so "how many times did #123 get
sent back" was unanswerable.

`QualityEvent` is the append-only answer: one row per signal, never reset, written at
those same moments *before* the clearing. Kinds: `execution_retry`, `pr_revision`,
`sdlc_iteration`, `review_vote` (ADO −10..10, per reviewer), `review_finding`,
`test_failed`, `reopened`. Surfaced on **Dashboard → 🔁 Quality** (per-item tallies +
raw event log); `rework_rows()` is the per-item roll-up, `kind_totals()` the tiles.

**One funnel, two outcomes.** Call sites never touch the repository directly — they go
through `learning.QualityLog.record()`, which writes the event **and**, when the signal
carries a teachable reason, a lesson for the next brief. This is deliberate: the
learning loop was previously inert because every execution path *read* lessons
(`_lessons_injected`, `_build_brief`) while only the replaced legacy path
(`_run_in_workspace`) ever *wrote* one — `run_agent`, `dispatch_interactive` and the
SDLC loop all drew from a well nothing filled, so `.autopilot/lessons/` never even
existed. Recording and learning being the same call makes that gap unrepresentable.

- Teaches: `review_finding` (the findings), `test_failed` (the summary), `review_vote`
  when blocking (≤ −5), `reopened`. Does **not** teach: `execution_retry` (an infra
  flake implies no rule), `pr_revision` (the review comment is the lesson).
- Lessons from a work-item-level signal go to `lessons.SHARED_BUCKET` (`_workspace`),
  which `lessons.recent()` always reads alongside the named repos — a PR rejection
  belongs to no single repo, and filing it under one would hide it from every other
  brief.
- `learning_loop_enabled` with an empty `workspace_directory` now **logs a warning**
  instead of silently dropping the lesson: `lessons.recent` reads nothing back without
  a workspace, and the silent version is exactly what made the loop look enabled while
  doing nothing.

---

## 9. ADO integration (`ado/`)

- **`auth.py`** — PAT or OAuth (client-credentials) bearer; builds request headers.
- **`client.py`** — httpx wrapper over ADO REST 7.1: WIQL, work items (get/batch/tagged),
  git PRs (by status / completed), build pipelines, tags (add/remove), comments, state
  updates, children, identities. Splits URLs between `_url` (work items → `ado_project`)
  and `_git_url` (code → `code_project or ado_project`).
- **`notifier.py`** — posts ADO comments and fans out to the notification channels.

---

## 10. Dashboard & board (`dashboard/`)

Server-rendered Jinja2 (no SPA). Pages: **Overview**, **Board**, **History**, **Config**,
**Capabilities**, **Settings**.

- **Board** groups items into columns; a column is derived (in order) from the item's ADO
  state (done/hand-off states), then its persisted `PipelineState`, then its tags. Cards
  are **drag-and-drop**: dropping onto a column applies the mapped tag or state
  (`board_drop_map`, `@`-prefixed value = state, else tag), removing sibling drop-tags.
- **Settings** writes `config.yaml` and applies live on the running container. **Export
  never includes the PAT**; import ignores any `ado_pat` in the file.

---

## 11. Config & security

- **`config.py`** — pydantic-settings. Loads `config.yaml`, overridden by `AUTOPILOT_*`
  env vars. **Secrets come from the environment**; the PAT is never written to exported
  config and `config.yaml*` is git-ignored.
- **RBAC** (`security.py`) — optional allow-list of who may trigger the autopilot.
- **Dashboard auth** (`security.py` + `app.py`) — the `/dashboard/*` UI is gated by HTTP
  Basic. The password is stored as a **PBKDF2 hash** (`dashboard_auth_password_hash`),
  never plaintext; on first start with no password set, the CLI prompts for one and
  persists the hash. A legacy plaintext `dashboard_auth_token` still works.
- **Encrypted full export** — the safe `Export` strips secrets + host-specific keys;
  `🔒 Export full` (`/dashboard/settings/export-full`) exports **everything incl.
  secrets**, AES-encrypted (Fernet + PBKDF2) under `config_export_password`. Decrypt:
  ```python
  from ai_autopilot.security import decrypt_bytes
  yaml_text = decrypt_bytes(open("autopilot-config-full.enc", "rb").read(), "Export#12345").decode()
  print(yaml_text)   # full config, incl. ado_pat
  ```
  Restore it on another machine via **🔓 Import full** (`/dashboard/settings/import-full`):
  upload the `.enc` + type the export password. Secrets are restored (unlike the
  shareable import which strips them); the target's own dashboard password / export
  password are never overwritten (excluded from the export).
- **Schedule guard** (`scheduling.py`) — only act within configured windows.
- **Autonomy levels** — `report` (L1: comment only), `assisted` (L2: draft PR, default),
  `unattended` (L3: normal PR, auto-resolve).
- **Multitenant / plugins** — a tenant manager and a Python plugin loader
  (`PreProcessor` / `PostProcessor` / `SkillProvider` hooks).

---

## 12. Observability & reliability

- **Logging**: structlog (JSON lines to `logs/autopilot.log`). `check=False` git probes
  (e.g. base-branch existence) surface as `git failed` warnings — these are handled
  probes, not real failures.
- **Metrics**: prometheus-client at `/metrics` (tasks, cost, poll items…).
- **Health**: `/health` runs ADO / Claude / disk checks.
- **Retry**: `RetryPolicy` with bounded retries + backoff; exhausted items are dropped
  from the pending set and reported.

---

## 13. Testing & deployment

- **Tests**: pytest (`asyncio_mode=auto`), 168 unit tests. Fakes for ADO/executor/repos;
  no network. Covers outcome policy, poller finalise, worktree scratch base, board move,
  state-sync flows, config export/import (PAT omission), app smoke.
- **Run**: `python -m ai_autopilot` (or `run.bat` on Windows) → `:5080`.
- **Deploy**: `docker compose up --build`; Kubernetes manifests under `k8s/`.

---

## 14. Known gaps / roadmap (honest)

- **In-flight state not fully durable** — `_live` and several dedup sets are in-memory;
  mitigated by tags + deterministic scratch, but the DB should become the single source
  of truth for in-flight interactive work.
- **Deploy monitor precision** — with `deploy_pipeline_id: 0` it treats *any* successful
  build on the base branch as "deployed"; exact per-item association would need ADO
  build→work-item links.
- **Interactive scratch teardown** — a still-open Remote-Control console pins an empty
  scratch dir until closed (worktree content is already reclaimed).
- **Single global `base_branch`** — per-repo resolution papers over it with fallbacks;
  repos with divergent base branches produce benign "git failed" probe warnings.

---

## 15. Tech stack

Python 3.11 · FastAPI · uvicorn · Claude Agent SDK (`claude-agent-sdk`) + Claude Code
(Remote Control) · httpx · SQLAlchemy (async) + aiosqlite · pydantic-settings ·
prometheus-client · structlog · Jinja2 · APScheduler · pytest.
Legacy .NET 8 worker preserved under `legacy-dotnet/` for reference.

---

## 16. Đối chiếu với loop-engineering (loop · goal · schedule)

Autopilot là một hiện thực hóa production, chuyên biệt cho ADO, của playbook
[loop-engineering](https://github.com/cobusgreyling/loop-engineering). Nguyên tắc gốc:
**"loops discover, goals finish"** — loop đi khám phá việc, goal đưa từng việc về đích.

### Ba trục

| Trục | loop-engineering | Claude Code (native) | AI Autopilot |
|---|---|---|---|
| **Loop** (discover) | control loop chạy *ngoài* mọi conversation | `/loop` dynamic, background task tự re-invoke | **Poller 30s** quét WIQL tìm item pending — loop cấp service |
| **Goal** (finish) | goal lái một việc đến hoàn tất | autonomous `/loop` 1 goal + tự lên lịch wakeup | **mỗi work item = 1 goal**; `result.json` + `outcome_policy` + state machine đưa về done |
| **Schedule** (cadence) | cadence + event trigger (on-tag, post-merge) | `CronCreate`, `ScheduleWakeup` | **đa trigger**: `poll_interval` + ADO webhook + `LoopScheduler` cron + StateSync 90s |

### Sáu primitive

| Primitive | AI Autopilot |
|---|---|
| Automations/Scheduling | poll + webhook + cron loops + state-sync — đa nguồn |
| Worktrees | `.aiwt/agent-<id>` per-task, per-repo base resolve, gate `max_concurrent` |
| Skills | route tới `.claude` skills/rules/agents |
| Plugins & MCP | ADO MCP + plugin loader (Pre/Post/SkillProvider) + kênh notify |
| Sub-agents (maker/checker) | AutoReviewer (review trước PR) + FeedbackHandler (PR babysitter) |
| Memory/State | **SQLAlchemy DB** (PipelineState, ExecutionRepo) + ADO tags làm state bền |

### Autonomy
L1/L2/L3 của repo ↔ `report` / `assisted` (draft PR) / `unattended` — **khớp 1-1**.

### Autopilot đi xa hơn framework
- **Trọn vòng đời ticket**, không dừng ở PR: merge → Ready to Deploy → deploy build xanh
  → Ready to Testing → parent roll-up.
- **Cross-project scoping** (`code_project` vs `ado_project`).
- **2 execution mode** (headless SDK + interactive Remote-Control người lái được).
- **Idempotency nhiều lớp** (skip tags + in-memory TTL + DB recovery).

### Còn thiếu so với framework (thành thật)
- **"Get a score"** — ✅ đã có v1 (§17): rubric 0–100 + grade + gate. *Còn lại:* chưa cắm
  tín hiệu CI build + unresolved PR thread thật vào điểm (hiện dùng completion/PR/files).
- **Circuit breaker chi phí** toàn cục (chưa tự cắt khi token nổ).
- **Durable spine** — `_live` (session interactive) vẫn in-memory, chưa 100% xuống DB.

---

## 17. PR scoring — "get a score"

Hiện thực stage *checker/verify* của loop-engineering: mỗi run được **chấm 0–100** từ các
tín hiệu khách quan, ra một **grade** (A–F) và một **gate** quyết định điều gì xảy ra tiếp.
Điểm là **hàm thuần** của input (`execution/pr_scorer.py`) nên deterministic + dễ test;
control plane thu tín hiệu, module chỉ phán xử.

### Rubric (tổng 100)

| Thành phần | Điểm | Chấm theo |
|---|---|---|
| **delivery** | 35 | hoàn tất + có PR (35); hoàn tất không PR (20); lỗi/không xong (0) |
| **review** | 30 | verdict auto-review: −15/critical (cap 2), −2/warning (cap 10); *unknown → 18* |
| **ci** | 20 | CI/build trên nhánh PR: pass 20 / đỏ 0 / *unknown → 10* |
| **scope** | 15 | có file đổi (15) − 2×thread chưa resolve (cap 6) |

> Tín hiệu **unknown** (không chạy auto-review / chưa biết CI) chấm *neutral-thấp*, không phải
> full điểm — một run **không kiểm chứng được thì không thể đạt ngưỡng auto**. Đây là mặc định
> thành thật: không có bằng chứng chất lượng ≠ chất lượng cao. (Trần điểm khi thiếu review+CI = 78 → gate `review`.)

### Gate & tác động
`gate = auto` (≥ `pr_score_auto_min`, mặc định 85) · `review` (≥ `pr_score_review_min`, 60) ·
`escalate` (< 60). Trong `_handle_agent_result`: run **dưới ngưỡng review** (và không phải L1
report) bị **giữ lại cho người** (`needs_human`) thay vì âm thầm sang review/done. Điểm + breakdown
được đính vào **comment ADO** (badge grade màu). Cấu hình: `pr_scoring_enabled` /
`pr_score_auto_min` / `pr_score_review_min` (chỉnh được ở Settings).

### Giới hạn v1 (roadmap)
Điểm hiện tính từ `ExecutionResult` (completion, PR, files, needs_human). **Chưa** kéo trạng thái
CI build thật và số unresolved thread về (scorer đã có sẵn tham số `ci_passed` / `unresolved_threads`
để cắm sau). Test: `tests/test_pr_scorer.py` (10 case).
