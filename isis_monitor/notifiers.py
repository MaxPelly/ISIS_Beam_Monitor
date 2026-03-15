import logging
import asyncio
from abc import ABC, abstractmethod
from typing import Optional, List

import aiohttp

logger = logging.getLogger(__name__)


class Notifier(ABC):
    """Abstract interface for any notification method."""
    @abstractmethod
    async def send(self, message: str, channel: Optional[str] = None):
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

    def _create_payload(self, message: str, channel: Optional[str] = None) -> dict:
        title = f"{channel} Beam Update" if channel else "MCR Update"
        return {
            "type": "message",
            "summary": message,
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "summary": message,
                    "channel": channel,
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": [
                        {"type": "TextBlock", "size": "Medium", "weight": "Bolder", "text": title},
                        {"type": "TextBlock", "text": message, "wrap": True}
                    ]
                }
            }]
        }

    async def send(self, message: str, channel: Optional[str] = None):
        if not self.webhook_url:
            return

        payload = self._create_payload(message, channel)
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
    async def send(self, message: str, channel: Optional[str] = None):
        prefix = f"[DUMMY NOTIFIER - {channel}]" if channel else "[DUMMY NOTIFIER]"
        logger.info(f"{prefix} {message}")


class NotificationChannel:
    """Manages a group of notifiers for a specific topic."""
    def __init__(self, name: str):
        self.name = name
        self.notifiers: List[Notifier] = []

    def add_notifier(self, notifier: Notifier):
        self.notifiers.append(notifier)

    async def broadcast(self, message: str, channel: Optional[str] = None):
        """Sends the message to all registered notifiers in parallel."""
        if not self.notifiers:
            logger.debug(
                f"Channel '{self.name}' has no notifiers configured; skipping broadcast."
            )
            return
        await asyncio.gather(*(n.send(message, channel) for n in self.notifiers))
