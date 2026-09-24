import logging
import asyncio
from abc import ABC, abstractmethod
from typing import List, Optional

import aiohttp

from isis_monitor.messages import Notification, Severity, fmt_time

logger = logging.getLogger(__name__)

_SEVERITY_STYLE = {
    Severity.INFO: "default",
    Severity.GOOD: "good",
    Severity.WARNING: "warning",
    Severity.ATTENTION: "attention",
}


class Notifier(ABC):
    """Abstract interface for any notification method."""
    @abstractmethod
    async def send(self, notification: Notification):
        pass


class TeamsNotifier(Notifier):
    """Sends notifications to a Microsoft Teams Incoming Webhook."""
    def __init__(self, webhook_url: str, timeout: float = 10.0):
        self.webhook_url = webhook_url
        self.timeout = timeout
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Lazily creates and reuses a single ClientSession."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _create_payload(self, notification: Notification) -> dict:
        header_text = f"{notification.emoji} {notification.title}".strip()
        body: list = [
            {
                "type": "Container",
                "style": _SEVERITY_STYLE[notification.severity],
                "bleed": True,
                "items": [
                    {
                        "type": "TextBlock",
                        "size": "Medium",
                        "weight": "Bolder",
                        "text": header_text,
                        "wrap": True,
                    },
                ],
            },
            {"type": "TextBlock", "text": notification.text, "wrap": True},
        ]

        if notification.flavour:
            body.append({
                "type": "TextBlock",
                "text": f"_{notification.flavour}_",
                "isSubtle": True,
                "wrap": True,
            })

        if notification.facts:
            body.append({
                "type": "FactSet",
                "facts": [
                    {"title": key, "value": value} for key, value in notification.facts
                ],
            })

        if notification.timestamp:
            body.append({
                "type": "TextBlock",
                "text": fmt_time(notification.timestamp),
                "isSubtle": True,
                "size": "Small",
                "wrap": True,
            })

        card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.2",
            "body": body,
        }

        if notification.url:
            card["actions"] = [
                {"type": "Action.OpenUrl", "title": notification.url_label, "url": notification.url}
            ]

        plain_text = notification.to_plain_text()
        summary = plain_text.splitlines()[0] if plain_text else notification.title

        return {
            "type": "message",
            "summary": summary,
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card,
            }],
        }

    async def send(self, notification: Notification):
        if not self.webhook_url:
            return

        payload = self._create_payload(notification)
        try:
            session = await self._get_session()
            async with session.post(
                self.webhook_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.error(
                        f"Teams webhook returned HTTP {resp.status}: {body[:200]}"
                    )
        except Exception as e:
            logger.error(f"Failed to send Teams webhook: {e}")


class DummyNotifier(Notifier):
    """A dummy notifier for testing — logs the message instead of sending."""
    async def send(self, notification: Notification):
        logger.info(f"[DUMMY NOTIFIER] {notification.to_plain_text()}")


class NotificationChannel:
    """Manages a group of notifiers for a specific topic."""
    def __init__(self, name: str):
        self.name = name
        self.notifiers: List[Notifier] = []

    def add_notifier(self, notifier: Notifier):
        self.notifiers.append(notifier)

    async def broadcast(self, notification: Notification):
        """Sends the notification to all registered notifiers in parallel."""
        if not self.notifiers:
            logger.debug(
                f"Channel '{self.name}' has no notifiers configured; skipping broadcast."
            )
            return
        await asyncio.gather(*(n.send(notification) for n in self.notifiers), return_exceptions=True)
