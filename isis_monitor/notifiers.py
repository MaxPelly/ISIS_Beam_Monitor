import logging
import requests
import asyncio
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

class Notifier(ABC):
    """Abstract interface for any notification method."""
    @abstractmethod
    async def send(self, message: str, channel: str = None):
        pass

class TeamsNotifier(Notifier):
    """Sends notifications to a Microsoft Teams Webhook."""
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def _create_payload(self, message: str, channel: str = None) -> dict:
        title = "Beam Update" if channel else "MCR Update"
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

    async def send(self, message: str, channel: str = None):
        if not self.webhook_url:
            return

        payload = self._create_payload(message, channel)
        try:
            # Run blocking I/O in a separate thread
            await asyncio.to_thread(requests.post, self.webhook_url, json=payload, timeout=10)
        except Exception as e:
            logger.error(f"Failed to send Teams webhook: {e}")

class DummyNotifier(Notifier):
    """A dummy notifier for testing purposes that logs the message instead of sending."""
    async def send(self, message: str, channel: str = None):
        prefix = f"[DUMMY NOTIFIER - {channel}]" if channel else "[DUMMY NOTIFIER]"
        logger.info(f"{prefix} {message}")

class NotificationChannel:
    """
    Manages a group of notifiers for a specific topic (e.g., 'Beam' or 'Experiment').
    """
    def __init__(self, name: str):
        self.name = name
        self.notifiers = []

    def add_notifier(self, notifier: Notifier):
        self.notifiers.append(notifier)

    async def broadcast(self, message: str, channel: str = None):
        """Sends the message to all registered notifiers in parallel."""
        if not self.notifiers:
            return
        
        await asyncio.gather(*(n.send(message, channel) for n in self.notifiers))
