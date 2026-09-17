"""MS Teams notifications via Workflows Webhook (Adaptive Card)."""

from __future__ import annotations

import asyncio

import httpx

from ai_autopilot.config import Settings, WebhookTarget
from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.notifications.base import (
    NotificationChannel,
    NotificationMessage,
    NotificationType,
)

_COLORS = {
    NotificationType.STARTED: "accent",
    NotificationType.ERROR: "warning",
}


class TeamsNotifier(NotificationChannel):
    name = "MS Teams"

    def __init__(self, config: Settings, http: httpx.AsyncClient) -> None:
        self._config = config
        self._http = http
        self._log = get_logger("notifications.teams")

    @property
    def is_enabled(self) -> bool:
        return bool(self._config.teams_webhooks)

    async def send(self, message: NotificationMessage) -> None:
        """Post the card to EVERY configured webhook, concurrently.

        One channel failing (revoked Workflows URL, a channel that was deleted) must not stop
        the others — otherwise adding a second channel would make notifications less reliable
        than having one. So each post is awaited independently and failures are logged per
        URL, never raised."""
        targets = self._interested(message)
        if not targets:
            return
        results = await self._deliver(targets, message)
        sent = sum(1 for _label, ok in results if ok)
        if sent != len(targets):
            # Name the channels that failed. "2 of 3 delivered" left you to guess which,
            # and every Workflows URL shares the same host, so the host was no help.
            failed = [label for label, ok in results if not ok]
            self._log.warning(
                "teams notification partially delivered",
                sent=sent, total=len(targets), failed=failed, title=message.title,
            )
        else:
            self._log.debug("teams notification sent", title=message.title, channels=sent)

    async def _deliver(
        self, targets: list[WebhookTarget], message: NotificationMessage
    ) -> list[tuple[str, bool]]:
        """Post to every target concurrently. ``(channel label, accepted)`` per target."""
        payload = self._payload(message)  # built once — identical for every channel
        results = await asyncio.gather(
            *(self._post(target, payload) for target in targets), return_exceptions=True
        )
        return [
            (t.label, r is True) for t, r in zip(targets, results, strict=False)
        ]

    async def probe(self, message: NotificationMessage) -> list[tuple[str, bool]]:
        """Send to EVERY configured channel and report what each one did.

        Exists because a Teams failure is invisible from the outside: ``send`` swallows
        a revoked Workflows URL into a log line, so an operator whose cards stopped
        arriving had no way to tell a dead webhook from a muted alert policy without
        reading the log of a machine they may not have. This bypasses per-channel
        routing on purpose — the question it answers is "can this channel be reached
        at all", not "would this notice have gone there".
        """
        return await self._deliver(self._config.teams_webhook_targets, message)

    def _interested(self, message: NotificationMessage) -> list[WebhookTarget]:
        """The channels that asked for THIS notice.

        A channel with no opinion of its own inherits the global alert policy, so an
        install that never touches per-channel routing behaves exactly as before. One
        that narrows a channel gets the point of having more than one: the dev channel
        can take failures only, while the PM channel takes the digest and nothing else.
        """
        cfg = self._config
        default_events = cfg.alert_event_set
        default_severity = cfg.alert_severity_floor
        event, severity = message.event, int(message.severity)
        wanted, skipped = [], []
        for target in cfg.teams_webhook_targets:
            if target.wants(event, severity, default_events=default_events,
                            default_severity=default_severity):
                wanted.append(target)
            else:
                skipped.append(target.label)
        if skipped:
            self._log.debug(
                "teams channels skipped by routing",
                alert_event=event, severity=message.severity.name, channels=skipped,
            )
        return wanted

    async def _post(self, target: WebhookTarget, payload: dict) -> bool:
        """True when this one webhook accepted the card. Never raises."""
        try:
            resp = await self._http.post(target.url, json=payload)
        except httpx.HTTPError as exc:
            # Identify the channel by NAME (falling back to host): the full URL carries the
            # token that authorises posting to it, so it must not land in the log.
            self._log.warning("teams webhook error", channel=target.label, error=describe_exc(exc))
            return False
        if resp.status_code >= 400:
            self._log.warning(
                "teams webhook failed", channel=target.label,
                status=resp.status_code, body=resp.text[:200],
            )
            return False
        return True

    @staticmethod
    def _payload(message: NotificationMessage) -> dict:
        color = _COLORS.get(message.type)
        if message.type is NotificationType.COMPLETED:
            color = "good" if message.result and message.result.success else "attention"
        color = color or "default"

        item = message.work_item
        # The id as a LINK when we know the item's URL. A card naming "#9083" made the
        # reader go find it by hand — on a phone, that is the difference between acting
        # on the notice and ignoring it. (Adaptive Card fact values render markdown.)
        label = f"#{item.id} {item.title}"
        wi = f"[{label}]({message.work_item_url})" if message.work_item_url else label
        facts: list[dict[str, str]] = [
            {"title": "Work Item", "value": wi},
            {"title": "Type", "value": item.work_item_type},
            {"title": "Category", "value": str(item.category)},
        ]
        # Where the item sits on the board NOW — the autopilot moves it as each stage
        # lands, and the card reported the work without reporting the move, so nobody
        # could tell from it whose turn the item had become.
        if item.state:
            facts.append({"title": "State", "value": item.state})
        # Who the item belongs to. On a shared channel the card said what was done and to
        # which item, but never for whom — so nobody reading it could tell whose work it was.
        facts.append({"title": "Assignee", "value": message.assignee})
        if message.skill:
            facts.append({"title": "Skill", "value": message.skill})
        if message.result is not None:
            r = message.result
            # Which ROLE ran. "Skill: agent" says how the work was executed, not what it
            # was — so a QC pass and the whole pipeline produced the same card, and the
            # branch name was the only clue either way.
            if r.profile:
                facts.append({"title": "SDLC role", "value": r.profile})
            facts.append({"title": "Duration", "value": _mmss(r.duration_seconds)})
            if r.branch_name:
                facts.append({"title": "Branch", "value": r.branch_name})
            if r.pr_url:
                facts.append({"title": "PR", "value": r.pr_url})
            if r.error:
                facts.append({"title": "Error", "value": r.error})
        if message.error:
            facts.append({"title": "Error", "value": message.error})

        # Call-to-action buttons — jump straight to the PR / dashboard from Teams.
        # (Action.OpenUrl works from webhook-posted cards; POST-back actions would need
        # a full Bot Framework bot, which this channel intentionally avoids.)
        actions = [
            {"type": "Action.OpenUrl", "title": label, "url": url}
            for label, url in (message.actions or [])
            if url
        ]
        content: dict = {
            "type": "AdaptiveCard",
            "body": [
                {
                    "type": "TextBlock",
                    "size": "Medium",
                    "weight": "Bolder",
                    "text": message.title,
                    "color": color,
                    "wrap": True,
                },
                {"type": "FactSet", "facts": facts},
            ],
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4",
        }
        if actions:
            content["actions"] = actions
        return {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": content,
                }
            ],
        }


def _mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
