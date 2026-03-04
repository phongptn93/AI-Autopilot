# ADO Autopilot

.NET Worker Service tu dong xu ly Azure DevOps work items bang Claude Code CLI.

## How it works

```
ADO Board                    ADO Autopilot                     Claude CLI
  |                               |                                |
  |  tag "autopilot" + New/ToDo   |                                |
  |------------------------------>|                                |
  |                               |  classify (BE/FE/Bug/Req...)   |
  |                               |  git checkout -b feature/xxx   |
  |                               |------------------------------->|
  |                               |     /skill-command {id}        |
  |                               |<-------------------------------|
  |                               |  git commit + push             |
  |    comment + tag "done"       |                                |
  |<------------------------------|                                |
```

**Poll** ADO board -> **Classify** work item -> **Route** to skill -> **Execute** Claude CLI -> **Notify** result on ADO + Teams + Zalo.

## Setup

### 1. Config

Edit `appsettings.json`:

```json
{
  "Autopilot": {
    "AdoOrganization": "https://dev.azure.com/<your-org>",
    "AdoProject": "<project-name>",
    "AdoPat": "<personal-access-token>",
    "RepoWorkingDirectory": "C:\\path\\to\\your\\git\\repo",
    "ClaudeCliPath": "claude",
    "TriggerTag": "autopilot",
    "ProcessedTag": "autopilot-done",
    "BaseBranch": "development",
    "PollIntervalSeconds": 30,
    "MaxConcurrent": 1,
    "TaskTimeoutMinutes": 30,
    "DryRun": false
  }
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `AdoOrganization` | Yes | Azure DevOps org URL |
| `AdoProject` | Yes | Project name for work item tracking |
| `AdoPat` | Yes | Personal Access Token (scope: Work Items R/W, Code R/W) |
| `RepoWorkingDirectory` | Yes | Path to git repo with source code (must have `.git` and `BaseBranch`) |
| `ClaudeCliPath` | No | Path to `claude` CLI. Default: `claude` (assumes in PATH) |
| `TriggerTag` | No | Tag that triggers processing. Default: `autopilot` |
| `ProcessedTag` | No | Tag added after processing. Default: `autopilot-done` |
| `BaseBranch` | No | Base branch for new feature branches. Default: `development` |
| `PollIntervalSeconds` | No | Poll interval. Default: `30` |
| `MaxConcurrent` | No | Max concurrent executions. Default: `1` |
| `TaskTimeoutMinutes` | No | Timeout per task. Default: `30` |
| `DryRun` | No | Log only, no execution. Default: `false` |
| `TeamsWebhookUrl` | No | MS Teams Incoming Webhook URL. Leave empty to disable |
| `ZaloOaAccessToken` | No | Zalo OA access token. Leave empty to disable |
| `ZaloRecipientUserId` | No | Zalo user ID to receive notifications |

### 2. Create PAT

1. Go to `https://dev.azure.com/<your-org>`
2. Click **User Settings** (gear icon, top right) -> **Personal Access Tokens**
3. Click **+ New Token**
4. Scopes: **Work Items (Read & Write)**, **Code (Read & Write)**
5. Copy token -> paste into `AdoPat`

### 3. Prepare git repo

```bash
cd <RepoWorkingDirectory>
git checkout development
git pull origin development
```

### 4. Run

```bash
cd src/AdoAutopilot
dotnet run
```

## Usage

### Create work item on ADO Board

1. Create a work item (Task, Bug, Requirement, User Story)
2. Add tag **`autopilot`**
3. Set state: **New**, **To Do**, or **Proposed**
4. Autopilot picks it up on next poll cycle

### Skill routing

Work items are classified by title prefix, type, and keywords:

| Condition | Category | Skill |
|-----------|----------|-------|
| Title starts with `[BE]` | BackendTask | `/implement-task-be {id}` |
| Title starts with `[FE]` | FrontendTask | `/implement-task-fe {id}` |
| Title starts with `[DB]` | DatabaseTask | `/sql-migration {id}` |
| Title starts with `[QC]` | TestTask | `/qc-test-management {id}` |
| WorkItemType = `Bug` | Bug | `/bugfix-workflow {id}` |
| WorkItemType = `Requirement` | Requirement | `/analyze-requirement {id}` |
| Keywords: api, endpoint, controller... | BackendTask | `/implement-task-be {id}` |
| Keywords: component, angular, page... | FrontendTask | `/implement-task-fe {id}` |

### Execution flow

1. **Poll** - Query ADO for items tagged `autopilot` in New/To Do/Proposed state
2. **Classify** - Determine category (BE, FE, Bug, DB, QC, Requirement)
3. **Route** - Map category to Claude CLI skill command
4. **Notify Started** - Comment on ADO + set state to Active
5. **Execute** - `git checkout -b feature/{id}` -> `claude /skill {id}` -> `git commit` -> `git push`
6. **Notify Completed** - Comment result on ADO + add `autopilot-done` tag
7. On success with PR -> set state to Resolved

### DryRun mode

Set `"DryRun": true` to test without executing Claude or modifying ADO:

```
[DRY-RUN] Would execute: claude /analyze-requirement 2152 for #2152
[DRY-RUN] Would comment on #2152: Completed
```

## Notifications

ADO Autopilot sends notifications qua multiple channels (cau hinh trong `appsettings.json`):

### MS Teams (Workflows Webhook)

1. Mo **MS Teams** -> vao **Channel** muon nhan thong bao
2. Click **...** (More options) ben canh ten channel -> **Workflows**
3. Tim template: **"Post to a channel when a webhook request is received"**
4. Dat ten: `ADO Autopilot` -> chon channel -> **Add workflow**
5. **Copy URL** webhook (dang `https://prod-xx.westus.logic.azure.com/workflows/...`)
6. Dan vao `TeamsWebhookUrl` trong config

### Zalo OA

1. Tao Official Account tai https://oa.zalo.me
2. Vao **Quan ly** -> **API** -> lay **Access Token**
3. Lay `user_id` cua nguoi nhan (user phai follow OA truoc)
4. Dan vao `ZaloOaAccessToken` va `ZaloRecipientUserId`

### Notification events

| Event | Message |
|-------|---------|
| Work item picked up | 🤖 Processing #ID - skill, category |
| Execution completed | ✅ Completed #ID - duration, branch, PR link, files |
| Execution failed | ❌ Failed #ID - skill, error |
| Unexpected error | ⚠️ Error #ID - error message |

## Architecture

```
src/AdoAutopilot/
├── Ado/
│   ├── AdoAuthService.cs      # Auth (PAT + OAuth fallback)
│   ├── AdoClient.cs           # ADO REST API client
│   ├── AdoNotifier.cs         # Comment/tag/state updates + broadcast to channels
│   └── AdoPollerService.cs    # Background polling service
├── Execution/
│   └── ClaudeExecutor.cs      # Shell out to Claude CLI
├── Models/
│   ├── AutopilotConfig.cs     # Configuration model
│   ├── ExecutionResult.cs     # Execution outcome
│   └── WorkItemInfo.cs        # Work item model + TaskCategory enum
├── Notifications/
│   ├── INotificationChannel.cs # Channel interface + message model
│   ├── TeamsNotifier.cs       # MS Teams Incoming Webhook
│   └── ZaloNotifier.cs        # Zalo OA API
├── Routing/
│   └── TaskRouter.cs          # Classify + route work items to skills
├── Program.cs                 # DI setup + hosted service
└── appsettings.json           # Configuration

tests/AdoAutopilot.Tests/
└── TaskRouterTests.cs         # 19 unit tests for classification/routing
```

## Tech Stack

- .NET 8 Worker Service
- Azure DevOps REST API 7.1
- Claude Code CLI
- xUnit (tests)
