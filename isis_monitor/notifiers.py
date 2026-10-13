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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, List, Optional, Tuple

import aiohttp

from isis_monitor import __version__
from isis_monitor.messages import Notification, Severity, fmt_time, strip_controls

logger = logging.getLogger(__name__)

_SEVERITY_STYLE = {
    Severity.INFO: "default",
    Severity.GOOD: "good",
    Severity.WARNING: "warning",
    Severity.ATTENTION: "attention",
}


def _no_links(text: str) -> str:
    """Card text with markdown links broken by a zero-width space, so remote
    text (e.g. a run title "[Sign in](https://evil)") shows as typed rather
    than as a disguised link; bare URLs stay visible as themselves."""
    return text.replace("](", "]\u200b(")


def _retry_after(value: object, cap: float) -> Optional[float]:
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
    """Abstract interface for any notification method.

    queue() only queues a notification; a worker task sends them in order,
    so a slow or hung destination never holds up the caller (notably the
    beam WebSocket loop, which would otherwise stop reading and miss
    keepalive pongs) or any other notifier.
    """
    QUEUE_SIZE = 100  # beyond this (e.g. a long Teams outage) the oldest are dropped
    _queue: Optional[asyncio.Queue] = None
    _worker: Optional[asyncio.Task] = None
    _closed = False

    @abstractmethod
    async def send(self, notification: Notification, route: str = ""):
        """`route` is the Teams channel name the NotificationChannel chose."""

    def queue(self, notification: Notification, route: str = "") -> None:
        """Queue the notification for sending and return straight away."""
        name = type(self).__name__
        if self._closed:
            logger.warning(f"{name} is closed; not sending: {notification.title}")
            return
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self.QUEUE_SIZE)
            self._worker = asyncio.create_task(self._send_loop())
        if self._queue.full():
            dropped, _ = self._queue.get_nowait()
            self._queue.task_done()
            logger.warning(f"{name} send queue full; dropped notification: {dropped.title}")
        self._queue.put_nowait((notification, route))

    async def _send_loop(self) -> None:
        while True:
            notification, route = await self._queue.get()
            try:
                await self.send(notification, route)
            except Exception:  # never let one bad send stop all later ones
                logger.exception(f"{type(self).__name__} failed to send: {notification.title}")
            finally:
                self._queue.task_done()

    async def flush(self, timeout: Optional[float] = None) -> bool:
        """Wait until the queue is empty (including anything queued meanwhile);
        False on timeout."""
        if self._queue is None:
            return True
        try:
            await asyncio.wait_for(self._queue.join(), timeout)
            return True
        except TimeoutError:
            return False

    async def close(self, timeout: float = 5.0) -> None:
        """Give queued notifications up to `timeout` seconds to go out, then
        stop the worker and release any resources."""
        self._closed = True
        if not await self.flush(timeout):
            logger.warning(f"{type(self).__name__} closed with {self._queue.qsize()} notification(s) unsent")
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        await self._release()

    async def _release(self) -> None:
        """Release any resources held by the notifier."""


class HTTPNotifier(Notifier):
    """Base for notifiers that POST a JSON payload to a URL.

    Rate limiting (429), server errors and network failures are retried
    after each of RETRY_DELAYS (or the server's Retry-After, capped at
    MAX_RETRY_AFTER); this notifier's later notifications wait meanwhile,
    so this is kept short and bounded.

    `session` is shared with other notifiers and closed by its owner;
    without one, the notifier makes and closes its own.
    """
    RETRY_DELAYS = (2.0, 4.0)  # seconds before the 2nd and 3rd attempts
    MAX_RETRY_AFTER = 60.0  # cap on a server's Retry-After, so it can't stall the queue
    LABEL = "webhook"  # how log messages refer to the destination

    def __init__(self, webhook_url: str, timeout: float = 10.0, session: Optional[aiohttp.ClientSession] = None):
        self.webhook_url = webhook_url
        self.timeout = timeout
        self._session = session
        self._owns_session = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def _release(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    @abstractmethod
    def _create_payload(self, notification: Notification, route: str) -> dict:
        """The JSON payload to POST for a notification."""

    def _request_kwargs(self, payload: dict) -> dict:
        """Keyword arguments for session.post(); called again for each attempt."""
        return {"json": payload}

    async def send(self, notification: Notification, route: str = ""):
        payload = self._create_payload(notification, route)
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
                try:  # an excerpt for the log: bounded, and no terminal escapes
                    body = strip_controls((await resp.content.read(200)).decode(errors="replace"))
                except aiohttp.ClientError:  # e.g. dropped mid-body; the status still counts
                    body = ""
                # 429 (rate limited) and 5xx are temporary; any other 4xx won't
                # get better by resending the same request.
                retryable = resp.status == 429 or resp.status >= 500
                return (
                    f"{self.LABEL} returned HTTP {resp.status}: {body}",
                    retryable,
                    _retry_after(resp.headers.get("Retry-After"), self.MAX_RETRY_AFTER),
                )
        except (aiohttp.ClientConnectionError, TimeoutError) as e:
            return f"Failed to send {self.LABEL}: {e}", True, None
        except Exception as e:  # its message may hold the URL, which is a secret
            return f"Failed to send {self.LABEL}: {type(e).__name__}", False, None


class TeamsNotifier(HTTPNotifier):
    """Sends notifications to a Microsoft Teams Incoming Webhook as Adaptive Cards."""
    LABEL = "Teams webhook"

    def _create_payload(self, notification: Notification, route: str) -> dict:
        header_text = _no_links(f"{notification.emoji} {notification.title}".strip())
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
            {"type": "TextBlock", "text": _no_links(notification.text), "wrap": True},
        ]

        if notification.facts:
            body.append({
                "type": "FactSet",
                "facts": [
                    {"title": _no_links(key), "value": _no_links(value)} for key, value in notification.facts
                ],
            })

        footer = f"ISIS Beam Monitor v{__version__}"
        if notification.timestamp:
            footer = f"{fmt_time(notification.timestamp)} · {footer}"
        body.append({
            "type": "TextBlock",
            "text": footer,
            "isSubtle": True,
            "size": "Small",
            "wrap": True,
        })

        summary = _no_links(notification.to_summary())

        card = {
            # Not part of the Adaptive Card schema — kept for downstream (e.g.
            # Power Automate) flows that route on these two fields.
            "summary": summary,
            "channel": route,
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

    def __init__(self, url: str, secret: bytes, timeout: float = 2.0, session: Optional[aiohttp.ClientSession] = None):
        super().__init__(url, timeout=timeout, session=session)
        self.secret = secret

    def _create_payload(self, notification: Notification, route: str) -> dict:
        n = notification
        return {
            "v": 1,
            "id": str(uuid.uuid4()),
            "title": n.title,
            "text": n.text,
            "severity": n.severity.value,
            "emoji": n.emoji,
            "facts": [list(fact) for fact in n.facts],
            "url": n.url,
            "url_label": n.url_label,
            "timestamp": n.timestamp.astimezone(timezone.utc).isoformat() if n.timestamp else None,
            "topic": n.topic,
            "monitor_version": __version__,
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
    async def send(self, notification: Notification, route: str = ""):
        logger.info(f"[DUMMY NOTIFIER] {notification.to_plain_text()}")


class NotificationChannel:
    """A category of notifications (e.g. "Beam Updates") and the notifiers
    they go to. A notifier may belong to several channels.

    `route` picks the Teams channel name for each notification, which
    downstream (e.g. Power Automate) flows route on; by default it's the
    channel's own name.
    """

    def __init__(self, name: str, route: Optional[Callable[[Notification], str]] = None):
        self.name = name
        self.route = route or (lambda _n: name)
        self.notifiers: List[Notifier] = []

    def add_notifier(self, notifier: Notifier):
        self.notifiers.append(notifier)

    def broadcast(self, notification: Notification) -> None:
        """Queue the notification on every notifier and return straight away."""
        if not self.notifiers:
            logger.debug(f"Channel '{self.name}' has no notifiers configured; skipping broadcast.")
            return
        route = self.route(notification)
        for notifier in self.notifiers:
            notifier.queue(notification, route)
