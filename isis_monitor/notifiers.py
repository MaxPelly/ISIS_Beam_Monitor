import logging
import asyncio
import contextlib
import hashlib
import hmac
import json
import math
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional, Tuple

import aiohttp

from isis_monitor.messages import Notification, Severity, fmt_time

logger = logging.getLogger(__name__)

_SEVERITY_STYLE = {
    Severity.INFO: "default",
    Severity.GOOD: "good",
    Severity.WARNING: "warning",
    Severity.ATTENTION: "attention",
}


MAX_RETRY_AFTER = 60.0  # cap on a server's Retry-After, so it can't stall the channel


def _retry_after(value: object, cap: float = MAX_RETRY_AFTER) -> Optional[float]:
    """Seconds to wait from a Retry-After header (seconds or an HTTP date),
    capped at `cap`; None if missing or unreadable."""
    if not isinstance(value, str):
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:  # e.g. the obsolete asctime form, which means GMT
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    return min(max(seconds, 0.0), cap) if math.isfinite(seconds) else None


class Notifier(ABC):
    """Abstract interface for any notification method."""
    @abstractmethod
    async def send(self, notification: Notification):
        pass

    async def close(self) -> None:
        """Release any resources held by the notifier."""


class HTTPNotifier(Notifier):
    """Base for notifiers that POST a JSON payload to a URL.

    Rate limiting (429), server errors and network failures are retried
    after each of RETRY_DELAYS (or the server's Retry-After, capped at
    MAX_RETRY_AFTER); the channel's worker waits meanwhile, so this is kept
    short and bounded.
    """
    RETRY_DELAYS = (2.0, 4.0)  # seconds before the 2nd and 3rd attempts
    MAX_RETRY_AFTER = MAX_RETRY_AFTER
    LABEL = "webhook"  # how log messages refer to the destination

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

    @abstractmethod
    def _create_payload(self, notification: Notification) -> dict:
        """The JSON payload to POST for a notification."""

    def _request_kwargs(self, payload: dict) -> dict:
        """Keyword arguments for session.post(); called again for each attempt."""
        return {"json": payload}

    async def send(self, notification: Notification):
        payload = self._create_payload(notification)
        for attempt, delay in enumerate((*self.RETRY_DELAYS, None), 1):
            error, retryable, retry_after = await self._post(payload)
            if error is None:
                return
            if delay is None or not retryable:
                suffix = f" (after {attempt} attempts)" if attempt > 1 else ""
                logger.error(f"{error}{suffix}")
                return
            if retry_after is not None:
                delay = retry_after
            logger.warning(f"{error}; retrying in {delay:g}s")
            await asyncio.sleep(delay)

    async def _post(self, payload: dict) -> Tuple[Optional[str], bool, Optional[float]]:
        """One attempt: (error message or None on success, whether to retry,
        the server's Retry-After in seconds if it gave one)."""
        try:
            session = await self._get_session()
            # A redirect would turn the POST into a bodyless GET whose 200
            # looks like success, so it's reported as a failure instead.
            async with session.post(
                self.webhook_url,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                allow_redirects=False,
                **self._request_kwargs(payload),
            ) as resp:
                if 200 <= resp.status < 300:
                    return None, False, None
                try:
                    body = await resp.text()
                except aiohttp.ClientError:  # e.g. dropped mid-body; the status still counts
                    body = ""
                # 429 (rate limited) and 5xx are temporary; any other 4xx won't
                # get better by resending the same request.
                retryable = resp.status == 429 or resp.status >= 500
                return (
                    f"{self.LABEL} returned HTTP {resp.status}: {body[:200]}",
                    retryable,
                    _retry_after(resp.headers.get("Retry-After"), self.MAX_RETRY_AFTER),
                )
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
            return f"Failed to send {self.LABEL}: {e}", True, None
        except Exception as e:  # its message may hold the URL, which is a secret
            return f"Failed to send {self.LABEL}: {type(e).__name__}", False, None


class TeamsNotifier(HTTPNotifier):
    """Sends notifications to a Microsoft Teams Incoming Webhook as Adaptive Cards."""
    LABEL = "Teams webhook"

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

class WebhookNotifier(HTTPNotifier):
    """POSTs notifications as signed JSON, e.g. to the optional push site.

    Each request carries X-Timestamp (Unix seconds) and X-Signature, the hex
    HMAC-SHA256 of "<timestamp>.<body>" under the shared secret, so the
    receiver can reject forged or replayed requests. Every attempt is signed
    afresh; the payload's `id` stays the same, so a receiver can drop repeats.
    """
    LABEL = "Push webhook"
    # Shorter than Teams': the channel's worker waits on retries, and this
    # extra receiver shouldn't hold up the Teams cards queued behind it.
    RETRY_DELAYS = (1.0, 2.0)
    MAX_RETRY_AFTER = 5.0

    def __init__(self, url: str, secret: bytes, timeout: float = 2.0):
        super().__init__(url, timeout=timeout)
        self.secret = secret

    def _create_payload(self, notification: Notification) -> dict:
        n = notification
        return {
            "v": 1,
            "id": str(uuid.uuid4()),
            "title": n.title,
            "text": n.text,
            "summary": n.to_summary(),
            "severity": n.severity.value,
            "emoji": n.emoji,
            "facts": [list(fact) for fact in n.facts],
            "flavour": n.flavour,
            "url": n.url,
            "url_label": n.url_label,
            "timestamp": n.timestamp.astimezone(timezone.utc).isoformat() if n.timestamp else None,
            "channel": n.channel,
            "topic": n.topic,
        }

    def _request_kwargs(self, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(self.secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        return {"data": body, "headers": {
            "Content-Type": "application/json",
            "X-Timestamp": timestamp,
            "X-Signature": signature,
        }}


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
