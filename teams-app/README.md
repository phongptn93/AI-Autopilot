# AI Autopilot — Teams app package

Template for sideloading the two-way bot into Microsoft Teams (see
`ai_autopilot/teams_agent.py`).

## 1. Fill in your Agent ID

Replace **both** `<TEAMS_AGENT_APP_ID>` placeholders in `manifest.json` with your
Azure Bot's Application (client) ID — the same value as `teams_agent_app_id` in
`config.yaml`.

## 2. Zip it

```bash
cd teams-app
zip ai-autopilot-teams-app.zip manifest.json color.png outline.png
```

## 3. Sideload into Teams

Teams → **Apps → Manage your apps → Upload a custom app** → select the zip →
add it to the team/channel that should receive reminders and chat commands.

## 4. Prerequisites

- `teams_agent_enabled: true` + `teams_agent_app_id` / `teams_agent_app_secret` /
  `teams_agent_tenant_id` set (config.yaml or `.env`)
- `pip install .[teams-bot]`
- Azure Bot resource's **Messaging endpoint** pointed at
  `https://<your-public-host>/api/messages`
- Microsoft Teams channel enabled on the Azure Bot resource

## Commands once added

- `/help` — command list
- `/status` — quick health check
- `/review <repo> <pr-id>` — ask the bot to re-review a PR it's already a reviewer on

`icons/` are placeholder art (`color.png` 192×192, `outline.png` 32×32,
transparent) — swap for your own branding before publishing beyond internal
sideload.
