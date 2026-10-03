import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import patch, MagicMock, AsyncMock

import aiohttp
import pytest

from isis_monitor.notifiers import (
    _retry_after, HTTPNotifier, TeamsNotifier, DummyNotifier, NotificationChannel, Notifier, WebhookNotifier,
)
from isis_monitor.messages import Notification, Severity


# ---------------------------------------------------------------------------
# DummyNotifier
# ---------------------------------------------------------------------------

async def test_dummy_notifier(caplog):
    caplog.set_level(logging.INFO)
    notifier = DummyNotifier()
    notification = Notification(title="Test", text="Test message", emoji="🔔")
    await notifier.send(notification)
    assert "🔔 Test" in caplog.text
    assert "Test message" in caplog.text


# ---------------------------------------------------------------------------
# TeamsNotifier — helpers
# ---------------------------------------------------------------------------

def make_status_session(*statuses: int, headers=None):
    """A mock ClientSession whose successive ``async with session.post(...)``
    responses have each of `statuses` in turn."""
    session = MagicMock(closed=False)
    session.responses = [
        MagicMock(status=status, headers=headers or {}, text=AsyncMock(return_value="body")) for status in statuses
    ]
    contexts = [MagicMock() for _ in statuses]
    for ctx, resp in zip(contexts, session.responses):
        ctx.__aenter__.return_value = resp
    session.post.side_effect = contexts
    return session


@pytest.fixture
def no_retry_delay():
    with patch.object(HTTPNotifier, "RETRY_DELAYS", (0.0, 0.0)):
        yield


# ---------------------------------------------------------------------------
# TeamsNotifier — send
# ---------------------------------------------------------------------------

async def test_teams_notifier_sends_request():
    """Downstream routing (e.g. Power Automate) reads `channel` and `summary`
    from inside `content`, not just the top-level `summary` — both must be present."""
    notifier = TeamsNotifier("http://fake.webhook.url")
    notifier._session = make_status_session(200)

    await notifier.send(Notification(title="Test title", text="Test message", channel="TS1"))

    notifier._session.post.assert_called_once()
    args, kwargs = notifier._session.post.call_args
    assert args[0] == "http://fake.webhook.url"
    payload = kwargs["json"]
    assert payload["summary"] == "Test title | Test message"
    card = payload["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    body_texts = [item["text"] for item in card["body"] if "text" in item]
    assert "Test message" in body_texts
    assert card["channel"] == "TS1"
    assert card["summary"] == payload["summary"]


async def test_teams_notifier_logs_error_on_bad_status(caplog):
    notifier = TeamsNotifier("http://fake.webhook.url")
    notifier._session = make_status_session(400)

    with caplog.at_level(logging.ERROR):
        await notifier.send(Notification(title="Test title", text="Test message"))

    assert "HTTP 400: body" in caplog.text


# ---------------------------------------------------------------------------
# WebhookNotifier
# ---------------------------------------------------------------------------

async def test_webhook_notifier_posts_signed_json():
    notifier = WebhookNotifier("http://127.0.0.1:8765/ingest", b"secret")
    notifier._session = make_status_session(200)
    when = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)

    await notifier.send(Notification(
        title="PEARL: New run started", text="Run 2", severity=Severity.GOOD, emoji="🚀",
        facts=[("Duration", "1h 0m")], timestamp=when, channel="Experiment Updates", topic="PEARL",
    ))

    args, kwargs = notifier._session.post.call_args
    assert args[0] == "http://127.0.0.1:8765/ingest"
    headers, body = kwargs["headers"], kwargs["data"]
    expected = hmac.new(b"secret", headers["X-Timestamp"].encode() + b"." + body, hashlib.sha256).hexdigest()
    assert headers["X-Signature"] == expected
    assert headers["Content-Type"] == "application/json"
    payload = json.loads(body)
    assert payload["v"] == 1 and payload["id"]
    assert payload["title"] == "PEARL: New run started"
    assert payload["severity"] == "good"
    assert payload["facts"] == [["Duration", "1h 0m"]]
    assert payload["timestamp"] == "2026-01-02T03:04:00+00:00"
    assert payload["topic"] == "PEARL"
    assert "summary" not in payload and "channel" not in payload  # the site doesn't use them


async def test_webhook_notifier_retries_with_the_same_id(no_retry_delay, caplog):
    notifier = WebhookNotifier("http://127.0.0.1:8765/ingest", b"secret")
    notifier._session = make_status_session(503, 200)

    await notifier.send(Notification(title="t", text="x"))

    ids = [json.loads(c.kwargs["data"])["id"] for c in notifier._session.post.call_args_list]
    assert len(ids) == 2 and ids[0] == ids[1]
    assert "Push webhook returned HTTP 503" in caplog.text


# ---------------------------------------------------------------------------
# TeamsNotifier — card structure
# ---------------------------------------------------------------------------

def test_create_payload_header_style_follows_severity():
    notifier = TeamsNotifier("http://fake.webhook.url")
    styles = {}
    for severity in Severity:
        payload = notifier._create_payload(Notification(title="Title", text="Text", severity=severity))
        styles[severity] = payload["attachments"][0]["content"]["body"][0]["style"]
    assert styles == {
        Severity.INFO: "default", Severity.GOOD: "good",
        Severity.WARNING: "warning", Severity.ATTENTION: "attention",
    }


def test_create_payload_includes_facts():
    notifier = TeamsNotifier("http://fake.webhook.url")
    notification = Notification(
        title="Title", text="Text", facts=[("Current", "3.500 uA"), ("Previous", "off")]
    )

    payload = notifier._create_payload(notification)

    card = payload["attachments"][0]["content"]
    fact_sets = [item for item in card["body"] if item["type"] == "FactSet"]
    assert len(fact_sets) == 1
    assert fact_sets[0]["facts"] == [
        {"title": "Current", "value": "3.500 uA"},
        {"title": "Previous", "value": "off"},
    ]


def test_create_payload_includes_flavour_and_timestamp_lines():
    notifier = TeamsNotifier("http://fake.webhook.url")
    ts = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
    notification = Notification(title="Title", text="Text", flavour="Beam's back, baby.", timestamp=ts)

    payload = notifier._create_payload(notification)

    body = payload["attachments"][0]["content"]["body"]
    texts = [item["text"] for item in body if "text" in item]
    assert "_Beam's back, baby._" in texts
    assert body[-1]["isSubtle"] is True
    assert "Jan" in body[-1]["text"]


def test_create_payload_url_action_only_when_url_given():
    notifier = TeamsNotifier("http://fake.webhook.url")
    notification = Notification(title="Title", text="Text", url="https://example.com/news")

    card = notifier._create_payload(notification)["attachments"][0]["content"]
    assert card["actions"] == [
        {"type": "Action.OpenUrl", "title": "Open", "url": "https://example.com/news"}
    ]

    card = notifier._create_payload(replace(notification, url=None))["attachments"][0]["content"]
    assert "actions" not in card


# ---------------------------------------------------------------------------
# NotificationChannel
# ---------------------------------------------------------------------------

async def test_notification_channel_fills_in_only_a_blank_channel():
    channel = NotificationChannel("TestChannel")

    mock_notifier1 = MagicMock()
    mock_notifier1.send = AsyncMock()
    mock_notifier2 = MagicMock()
    mock_notifier2.send = AsyncMock()

    channel.add_notifier(mock_notifier1)
    channel.add_notifier(mock_notifier2)

    notification = Notification(title="Title", text="Broadcast message")
    await channel.broadcast(notification)
    await channel.flush()

    # broadcast() fills in a blank channel with the NotificationChannel's own
    # name, so the notifiers receive a copy rather than the exact same object.
    expected = replace(notification, channel="TestChannel")
    mock_notifier1.send.assert_called_once_with(expected)
    mock_notifier2.send.assert_called_once_with(expected)

    explicit = Notification(title="Title", text="Text", channel="TS1")
    await channel.broadcast(explicit)
    await channel.flush()
    mock_notifier1.send.assert_called_with(explicit)


async def test_notification_channel_empty_logs_debug(caplog):
    """Broadcast on a channel with no notifiers should log at DEBUG level."""
    channel = NotificationChannel("Beam Updates")

    with caplog.at_level(logging.DEBUG):
        await channel.broadcast(Notification(title="Title", text="some message"))

    assert "Beam Updates" in caplog.text
    assert "no notifiers" in caplog.text


class _FailingNotifier(DummyNotifier):
    async def send(self, notification):
        raise RuntimeError("webhook exploded")


async def test_notification_channel_logs_failing_notifier_and_still_delivers(caplog):
    channel = NotificationChannel("Beam")
    good = DummyNotifier()
    good.send = AsyncMock()
    channel.add_notifier(_FailingNotifier())
    channel.add_notifier(good)

    await channel.broadcast(Notification(title="t", text="x"))
    await channel.flush()

    good.send.assert_awaited_once()
    assert "_FailingNotifier failed on channel 'Beam'" in caplog.text
    assert "webhook exploded" in caplog.text


@pytest.mark.parametrize("error, attempts, logged", [
    (aiohttp.ClientConnectionError("refused"), 3, "Failed to send Teams webhook: refused (after 3 attempts)"),
    # Unexpected: not retried, and only its type is logged, since aiohttp
    # errors such as InvalidURL carry the (secret) webhook URL.
    (aiohttp.InvalidURL("https://hook.invalid/secret-token"), 1, "Failed to send Teams webhook: InvalidURL"),
])
async def test_teams_notifier_retries_only_connection_errors(caplog, no_retry_delay, error, attempts, logged):
    notifier = TeamsNotifier("http://example.invalid/hook")
    notifier._session = MagicMock(closed=False)
    notifier._session.post.side_effect = error

    await notifier.send(Notification(title="t", text="x"))
    assert notifier._session.post.call_count == attempts
    assert logged in caplog.text
    assert "secret-token" not in caplog.text


@pytest.mark.parametrize("statuses, attempts", [
    ((429, 200), 2),
    ((503, 502, 200), 3),
    ((500, 500, 500), 3),  # gives up after the last attempt
    ((400,), 1),  # a bad request won't get better by resending it
    ((404,), 1),
    ((302,), 1),  # a redirect isn't followed (it would drop the body) or retried
])
async def test_teams_notifier_retries_only_temporary_http_errors(statuses, attempts, no_retry_delay, caplog):
    notifier = TeamsNotifier("http://example.invalid/hook")
    notifier._session = make_status_session(*statuses)

    await notifier.send(Notification(title="t", text="x"))
    assert notifier._session.post.call_count == attempts
    assert notifier._session.post.call_args.kwargs["allow_redirects"] is False
    gave_up = any(r.levelno == logging.ERROR for r in caplog.records)
    assert gave_up is (statuses[-1] != 200)


async def test_teams_notifier_retries_a_5xx_whose_body_cannot_be_read(no_retry_delay):
    notifier = TeamsNotifier("http://example.invalid/hook")
    notifier._session = make_status_session(503, 200)
    notifier._session.responses[0].text.side_effect = aiohttp.ClientPayloadError("dropped")

    await notifier.send(Notification(title="t", text="x"))
    assert notifier._session.post.call_count == 2


async def test_teams_notifier_waits_between_retries(caplog):
    notifier = TeamsNotifier("http://example.invalid/hook")
    notifier._session = make_status_session(503, 503, 503)

    with patch("isis_monitor.notifiers.asyncio.sleep", new=AsyncMock()) as sleep:
        with caplog.at_level(logging.WARNING):
            await notifier.send(Notification(title="t", text="x"))

    assert [c.args[0] for c in sleep.await_args_list] == [2.0, 4.0]
    assert "retrying in 2s" in caplog.text


async def test_teams_notifier_honours_retry_after():
    notifier = TeamsNotifier("http://example.invalid/hook")
    notifier._session = make_status_session(429, 200, headers={"Retry-After": "7"})

    with patch("isis_monitor.notifiers.asyncio.sleep", new=AsyncMock()) as sleep:
        await notifier.send(Notification(title="t", text="x"))

    sleep.assert_awaited_once_with(7.0)


async def test_webhook_notifier_caps_retry_after_lower_than_teams():
    notifier = WebhookNotifier("http://127.0.0.1:8765/ingest", b"secret")
    notifier._session = make_status_session(429, 429, 429, headers={"Retry-After": "60"})

    with patch("isis_monitor.notifiers.asyncio.sleep", new=AsyncMock()) as sleep:
        await notifier.send(Notification(title="t", text="x"))

    assert [c.args[0] for c in sleep.await_args_list] == [5.0, 5.0]


def test_retry_after_parsing():
    in_30s = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)

    assert _retry_after("5") == 5.0
    assert _retry_after("3600") == 60.0  # capped
    assert _retry_after("3600", cap=5.0) == 5.0
    assert 25 <= _retry_after(in_30s) <= 30
    assert _retry_after("Mon, 01 Jan 2001 00:00:00 GMT") == 0.0  # already past
    assert _retry_after("Sun Nov  6 08:49:37 1994") == 0.0  # asctime form, naive
    for bad in (None, "", "soon", "nan", "inf"):
        assert _retry_after(bad) is None, bad


async def test_closing_the_channel_cancels_a_send_waiting_to_retry():
    notifier = TeamsNotifier("http://example.invalid/hook")
    notifier._session = make_status_session(503, 200)
    channel = NotificationChannel("Beam")
    channel.add_notifier(notifier)

    with patch.object(TeamsNotifier, "RETRY_DELAYS", (60.0, 60.0)), \
            patch.object(NotificationChannel, "CLOSE_TIMEOUT", 0.05):
        await channel.broadcast(Notification(title="t", text="x"))
        await asyncio.sleep(0)  # let the worker make its first attempt
        await asyncio.wait_for(channel.close(), timeout=2)

    assert notifier._session.post.call_count == 1


async def test_teams_notifier_reuses_and_closes_session():
    notifier = TeamsNotifier("http://example.invalid/hook")
    session = await notifier._get_session()
    assert await notifier._get_session() is session
    await notifier.close()
    assert session.closed
    assert notifier._session is None
    await notifier.close()  # idempotent


async def test_notification_channel_close_closes_all_notifiers():
    channel = NotificationChannel("Beam")
    teams = TeamsNotifier("http://example.invalid/hook")
    await teams._get_session()
    channel.add_notifier(teams)
    channel.add_notifier(DummyNotifier())  # base-class close() is a no-op
    await channel.close()
    assert teams._session is None


class _SlowNotifier(Notifier):
    def __init__(self, delay):
        self.delay = delay
        self.sent = []

    async def send(self, notification):
        await asyncio.sleep(self.delay)
        self.sent.append(notification.title)


async def test_broadcast_returns_without_waiting_for_a_slow_webhook():
    """A hung webhook mustn't hold up the caller (e.g. the beam WebSocket loop)."""
    channel = NotificationChannel("Beam")
    slow = _SlowNotifier(10)
    channel.add_notifier(slow)
    await asyncio.wait_for(channel.broadcast(Notification(title="a", text="")), 0.1)
    assert slow.sent == []
    channel.CLOSE_TIMEOUT = 0.05
    await channel.close()  # gives up on the unsent one rather than hanging


async def test_queued_notifications_are_sent_in_order_and_flushed_on_close():
    channel = NotificationChannel("Beam")
    slow = _SlowNotifier(0.01)
    channel.add_notifier(slow)
    for title in "abc":
        await channel.broadcast(Notification(title=title, text=""))
    await channel.close()
    assert slow.sent == ["a", "b", "c"]


async def test_full_queue_drops_the_oldest_notification(caplog):
    channel = NotificationChannel("Beam")
    channel.QUEUE_SIZE = 2
    slow = _SlowNotifier(0.05)
    channel.add_notifier(slow)
    for title in "abcd":
        await channel.broadcast(Notification(title=title, text=""))
    await channel.close()
    # broadcast() doesn't yield, so all four arrive before the worker takes one.
    assert slow.sent == ["c", "d"]
    assert "dropped notification: a" in caplog.text and "dropped notification: b" in caplog.text


async def test_worker_survives_an_unexpected_send_error(caplog):
    channel = NotificationChannel("Beam")
    slow = _SlowNotifier(0)
    channel.add_notifier(slow)
    with patch.object(channel, "_send", side_effect=[RuntimeError("boom"), None]) as send:
        await channel.broadcast(Notification(title="a", text=""))
        await channel.broadcast(Notification(title="b", text=""))
        await channel.flush(1)
    assert send.await_count == 2
    assert "Sending on channel 'Beam' failed" in caplog.text
    await channel.close()


async def test_broadcast_after_close_is_ignored(caplog):
    channel = NotificationChannel("Beam")
    slow = _SlowNotifier(0)
    channel.add_notifier(slow)
    await channel.close()
    await channel.broadcast(Notification(title="late", text=""))
    await asyncio.sleep(0.01)
    assert slow.sent == [] and channel._worker is None
    assert "is closed; not sending: late" in caplog.text
