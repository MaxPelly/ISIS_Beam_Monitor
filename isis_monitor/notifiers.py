import logging
import asyncio
import contextlib
from abc import ABC, abstractmethod
from dataclasses import replace
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

    async def close(self) -> None:
        """Release any resources held by the notifier."""


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

        summary = notification.to_summary()

        card = {
            # Not part of the Adaptive Card schema — kept for downstream (e.g.
            # Power Automate) flows that route on these two fields.
            "summary": summary,
            "channel": notification.channel,
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.2",
            "body": body,
        }

        if notification.url:
            card["actions"] = [
                {"type": "Action.OpenUrl", "title": notification.url_label, "url": notification.url}
            ]

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
    """Manages a group of notifiers for a specific topic.

    broadcast() only queues a notification; a worker task sends them in
    order, so a slow or hung webhook (up to webhook_timeout per send) never
    holds up the caller — notably the beam WebSocket loop, which would
    otherwise stop reading and miss keepalive pongs.
    """
    QUEUE_SIZE = 100  # beyond this (e.g. a long Teams outage) the oldest are dropped
    CLOSE_TIMEOUT = 5.0  # how long close() waits for queued notifications to send

    def __init__(self, name: str):
        self.name = name
        self.notifiers: List[Notifier] = []
        self._queue: Optional[asyncio.Queue] = None
        self._worker: Optional[asyncio.Task] = None
        self._closed = False

    def add_notifier(self, notifier: Notifier):
        self.notifiers.append(notifier)

    async def broadcast(self, notification: Notification):
        """Queue the notification for every notifier and return straight away."""
        if not self.notifiers:
            logger.debug(
                f"Channel '{self.name}' has no notifiers configured; skipping broadcast."
            )
            return
        if self._closed:
            logger.warning(f"Channel '{self.name}' is closed; not sending: {notification.title}")
            return
        if not notification.channel:
            notification = replace(notification, channel=self.name)
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self.QUEUE_SIZE)
            self._worker = asyncio.create_task(self._send_loop())
        if self._queue.full():
            dropped = self._queue.get_nowait()
            self._queue.task_done()
            logger.warning(f"Channel '{self.name}' send queue full; dropped notification: {dropped.title}")
        self._queue.put_nowait(notification)

    async def _send_loop(self) -> None:
        while True:
            notification = await self._queue.get()
            try:
                await self._send(notification)
            except Exception:  # never let one bad send stop all later ones
                logger.exception(f"Sending on channel '{self.name}' failed")
            finally:
                self._queue.task_done()

    async def _send(self, notification: Notification) -> None:
        """Send to all registered notifiers in parallel."""
        results = await asyncio.gather(
            *(n.send(notification) for n in self.notifiers), return_exceptions=True
        )
        for notifier, result in zip(self.notifiers, results):
            if isinstance(result, Exception):
                logger.error(f"{type(notifier).__name__} failed on channel '{self.name}': {result!r}")

    async def flush(self, timeout: Optional[float] = None) -> bool:
        """Wait until the queue is empty (including anything queued meanwhile);
        False on timeout."""
        if self._queue is None:
            return True
        try:
            await asyncio.wait_for(self._queue.join(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def close(self) -> None:
        self._closed = True
        if not await self.flush(self.CLOSE_TIMEOUT):
            logger.warning(f"Channel '{self.name}' closed with {self._queue.qsize()} notification(s) unsent")
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        await asyncio.gather(*(n.close() for n in self.notifiers), return_exceptions=True)
