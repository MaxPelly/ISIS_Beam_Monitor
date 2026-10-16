import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from isis_monitor import __version__
from isis_monitor.messages import Notification, Severity
from isis_monitor.notifiers import (
    DummyNotifier,
    NotificationChannel,
    Notifier,
    TeamsNotifier,
    WebhookNotifier,
    _retry_after,
)

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
        MagicMock(status=status, headers=headers or {}, content=MagicMock(read=AsyncMock(return_value=b"body")))
        for status in statuses
    ]
    contexts = [MagicMock() for _ in statuses]
    for ctx, resp in zip(contexts, session.responses, strict=True):
        ctx.__aenter__.return_value = resp
    session.post.side_effect = contexts
    return session


@pytest.fixture
def no_retry_delay():
    """Skip the waits between retries, whatever a notifier's RETRY_DELAYS;
    yields the mock so tests can check the delays asked for."""
    with patch("isis_monitor.notifiers.asyncio.sleep", new=AsyncMock()) as sleep:
        yield sleep


# ---------------------------------------------------------------------------
# TeamsNotifier — send
# ---------------------------------------------------------------------------

async def test_teams_notifier_sends_request():
    """Downstream routing (e.g. Power Automate) reads `channel` and `summary`
    from inside `content`, not just the top-level `summary` — both must be present."""
    notifier = TeamsNotifier("http://fake.webhook.url")
    notifier._session = make_status_session(200)

    await notifier.send(Notification(title="Test title", text="Test message"), "TS1")

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


def test_teams_card_breaks_markdown_links_in_remote_text():
    """A run title can't become a disguised link in the Experiment Updates channel."""
    evil = "[Session expired - sign in](https://evil.example)"
    payload = TeamsNotifier("http://x")._create_payload(
        Notification(title=f"PEARL: {evil}", text=evil, facts=[("Previous run", evil)]), "")
    card = json.dumps(payload["attachments"][0]["content"]["body"])
    assert "](" not in card and card.count("]\\u200b(") == 3
    assert "](" not in payload["summary"]  # read by plain-text clients and flows


async def test_bad_status_logs_an_error_with_a_bounded_body_excerpt_without_escapes(caplog, no_retry_delay):
    notifier = TeamsNotifier("http://fake.webhook.url")
    notifier._session = make_status_session(400)
    notifier._session.responses[0].content.read.return_value = b"\x1b[2Jbad"
    await notifier.send(Notification(title="t", text="x"))
    notifier._session.responses[0].content.read.assert_awaited_once_with(200)
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.ERROR, "Teams webhook returned HTTP 400: [2Jbad")]


# ---------------------------------------------------------------------------
# WebhookNotifier
# ---------------------------------------------------------------------------

async def test_webhook_notifier_posts_signed_json():
    notifier = WebhookNotifier("http://127.0.0.1:8765/ingest", b"secret")
    notifier._session = make_status_session(200)
    when = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)

    await notifier.send(Notification(
        title="PEARL: New run started", text="Run 2 \ud800", severity=Severity.GOOD, emoji="🚀",
        facts=[("Duration", "1h 0m")], timestamp=when, topic="PEARL",
    ), "Experiment Updates")

    args, kwargs = notifier._session.post.call_args
    assert args[0] == "http://127.0.0.1:8765/ingest"
    headers, body = kwargs["headers"], kwargs["data"]
    expected = hmac.new(b"secret", headers["X-Timestamp"].encode() + b"." + body, hashlib.sha256).hexdigest()
    assert headers["X-Signature"] == expected
    assert headers["Content-Type"] == "application/json"
    payload = json.loads(body)
    assert payload["v"] == 1 and payload["id"]
    assert payload["title"] == "PEARL: New run started" and payload["text"] == "Run 2 ?"  # a lone surrogate
    assert payload["severity"] == "good"
    assert payload["facts"] == [["Duration", "1h 0m"]]
    assert payload["timestamp"] == "2026-01-02T03:04:00+00:00"
    assert payload["topic"] == "PEARL" and payload["monitor_version"] == __version__
    assert "summary" not in payload and "channel" not in payload  # the site doesn't use them


async def test_webhook_notifier_retries_with_the_same_id(no_retry_delay, caplog):
    notifier = WebhookNotifier("http://127.0.0.1:8765/ingest", b"secret")
    notifier._session = make_status_session(503, 200)

    await notifier.send(Notification(title="t", text="x"))

    ids = [json.loads(c.kwargs["data"])["id"] for c in notifier._session.post.call_args_list]
    assert len(ids) == 2 and ids[0] == ids[1]
    assert "Push webhook returned HTTP 503" in caplog.text
    no_retry_delay.assert_awaited_once_with(WebhookNotifier.RETRY_DELAYS[0])


# ---------------------------------------------------------------------------
# TeamsNotifier — card structure
# ---------------------------------------------------------------------------

def test_create_payload_header_style_follows_severity():
    notifier = TeamsNotifier("http://fake.webhook.url")
    styles = {}
    for severity in Severity:
        payload = notifier._create_payload(Notification(title="Title", text="Text", severity=severity), "")
        styles[severity] = payload["attachments"][0]["content"]["body"][0]["style"]
    assert styles == {
        Severity.INFO: "default", Severity.GOOD: "good",
        Severity.WARNING: "warning", Severity.ATTENTION: "attention",
    }


def test_create_payload_facts_footer_and_url_action():
    notifier = TeamsNotifier("http://fake.webhook.url")
    ts = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
    notification = Notification(
        title="Title", text="Text", facts=[("Current", "3.500 uA"), ("Previous", "off")],
        timestamp=ts, url="https://example.com/news",
    )

    card = notifier._create_payload(notification, "")["attachments"][0]["content"]
    fact_sets = [item for item in card["body"] if item["type"] == "FactSet"]
    assert len(fact_sets) == 1
    assert fact_sets[0]["facts"] == [
        {"title": "Current", "value": "3.500 uA"},
        {"title": "Previous", "value": "off"},
    ]
    # The card ends with the time and version, subtly.
    footer = card["body"][-1]
    assert footer["isSubtle"] is True and footer["size"] == "Small"
    assert "Jan" in footer["text"] and footer["text"].endswith(f"ISIS Beam Monitor v{__version__}")
    assert card["actions"] == [
        {"type": "Action.OpenUrl", "title": "Open", "url": "https://example.com/news"}
    ]

    # Without facts, timestamp or URL: no FactSet, just the version, no action.
    card = notifier._create_payload(Notification(title="Title", text="Text"), "")["attachments"][0]["content"]
    assert [item["type"] for item in card["body"]] == ["Container", "TextBlock", "TextBlock"]
    assert card["body"][-1]["text"] == f"ISIS Beam Monitor v{__version__}"
    assert "actions" not in card


# ---------------------------------------------------------------------------
# NotificationChannel
# ---------------------------------------------------------------------------

def test_notification_channel_routes_by_its_name_or_its_route(caplog):
    notifiers = [MagicMock(spec=Notifier), MagicMock(spec=Notifier)]
    channel = NotificationChannel("TestChannel")
    for notifier in notifiers:
        channel.add_notifier(notifier)
    notification = Notification(title="Title", text="Broadcast message")
    channel.broadcast(notification)
    # By default every notification is routed to the channel's own name.
    for notifier in notifiers:
        notifier.queue.assert_called_once_with(notification, "TestChannel")

    routed = NotificationChannel("Beam", route=lambda n: n.beam)
    routed.add_notifier(notifiers[0])
    beam_card = Notification(title="Title", text="Text", beam="Muons")
    routed.broadcast(beam_card)
    notifiers[0].queue.assert_called_with(beam_card, "Muons")

    # A channel with no notifiers just says so, at DEBUG level.
    with caplog.at_level(logging.DEBUG):
        NotificationChannel("Beam Updates").broadcast(Notification(title="Title", text="some message"))
    assert [r.levelno for r in caplog.records] == [logging.DEBUG]
    assert "Beam Updates" in caplog.text and "no notifiers" in caplog.text


class _FailingNotifier(DummyNotifier):
    async def send(self, notification, route=""):
        raise RuntimeError("webhook exploded")


async def test_a_failing_notifier_doesnt_stop_the_others_or_its_own_queue(caplog):
    channel = NotificationChannel("Beam")
    failing, good = _FailingNotifier(), _SlowNotifier(0)
    channel.add_notifier(failing)
    channel.add_notifier(good)

    channel.broadcast(Notification(title="a", text="x"))
    channel.broadcast(Notification(title="b", text="x"))
    assert await failing.flush(1) and await good.flush(1)
    assert good.sent == ["a", "b"]
    assert "_FailingNotifier failed to send: a" in caplog.text and "_FailingNotifier failed to send: b" in caplog.text
    assert "webhook exploded" in caplog.text
    await asyncio.gather(failing.close(), good.close())


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
    notifier._session.responses[0].content.read.side_effect = aiohttp.ClientPayloadError("dropped")

    await notifier.send(Notification(title="t", text="x"))
    assert notifier._session.post.call_count == 2


@pytest.mark.parametrize("statuses, headers, delays", [
    ((503, 503, 503), None, [2.0, 4.0]),  # the default RETRY_DELAYS
    ((429, 200), {"Retry-After": "7"}, [7.0]),  # the server's wait instead
])
async def test_teams_notifier_waits_between_retries(statuses, headers, delays, no_retry_delay, caplog):
    notifier = TeamsNotifier("http://example.invalid/hook")
    notifier._session = make_status_session(*statuses, headers=headers)

    with caplog.at_level(logging.WARNING):
        await notifier.send(Notification(title="t", text="x"))

    assert [c.args[0] for c in no_retry_delay.await_args_list] == delays
    assert f"retrying in {delays[0]:g}s" in caplog.text


def test_retry_after_parsing():
    in_30s = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)

    assert _retry_after("5", 60.0) == 5.0
    assert _retry_after("3600", 60.0) == 60.0  # capped
    assert _retry_after("3600", 5.0) == 5.0
    assert 25 <= _retry_after(in_30s, 60.0) <= 30
    assert _retry_after("Mon, 01 Jan 2001 00:00:00 GMT", 60.0) == 0.0  # already past
    assert _retry_after("Sun Nov  6 08:49:37 1994", 60.0) == 0.0  # asctime form, naive
    for bad in (None, "", "soon", "nan", "inf"):
        assert _retry_after(bad, 60.0) is None, bad


async def test_closing_cancels_a_send_waiting_to_retry():
    notifier = TeamsNotifier("http://example.invalid/hook")
    notifier._session = make_status_session(503, 200)
    with patch.object(TeamsNotifier, "RETRY_DELAYS", (60.0, 60.0)):
        notifier.queue(Notification(title="t", text="x"))
        await asyncio.sleep(0)  # let the worker make its first attempt
        await asyncio.wait_for(notifier.close(0.05), timeout=2)
    assert notifier._session.post.call_count == 1


async def test_teams_notifier_closes_its_own_session_but_leaves_a_shared_one():
    notifier = TeamsNotifier("http://example.invalid/hook")
    session = await notifier._get_session()
    assert await notifier._get_session() is session  # reused
    await notifier.close()
    assert session.closed
    assert notifier._session is None
    await notifier.close()  # idempotent

    async with aiohttp.ClientSession() as session:
        notifier = TeamsNotifier("http://example.invalid/hook", session=session)
        assert await notifier._get_session() is session
        await notifier.close()
        assert not session.closed


class _SlowNotifier(Notifier):
    def __init__(self, delay):
        self.delay = delay
        self.sent = []

    async def send(self, notification, route=""):
        await asyncio.sleep(self.delay)
        self.sent.append(notification.title)


async def test_a_hung_notifier_doesnt_delay_the_caller_or_the_others():
    """A Teams webhook that never answers doesn't hold up the caller (e.g. the
    beam WebSocket loop), or push, or the reverse."""
    for hung_first in (True, False):
        hung, quick = _SlowNotifier(10), _SlowNotifier(0)
        channel = NotificationChannel("Beam")
        for notifier in (hung, quick) if hung_first else (quick, hung):
            channel.add_notifier(notifier)
        channel.broadcast(Notification(title="a", text=""))
        assert hung.sent == quick.sent == []  # broadcast returns without waiting
        channel.broadcast(Notification(title="b", text=""))
        assert await quick.flush(1)
        assert quick.sent == ["a", "b"] and hung.sent == []
        # close gives up on the unsent one rather than hanging
        await asyncio.wait_for(asyncio.gather(hung.close(0.01), quick.close()), 1)


async def test_queued_notifications_are_sent_in_order_flushed_on_close_and_ignored_after(caplog):
    slow = _SlowNotifier(0.01)
    for title in "abc":
        slow.queue(Notification(title=title, text=""))
    await slow.close()
    assert slow.sent == ["a", "b", "c"]

    worker = slow._worker
    slow.queue(Notification(title="late", text=""))
    await asyncio.sleep(0.01)
    # Neither queued nor a new worker started for it.
    assert slow.sent == ["a", "b", "c"] and slow._queue.empty()
    assert slow._worker is worker and worker.done()
    assert "_SlowNotifier is closed; not sending: late" in caplog.text


async def test_full_queue_drops_the_oldest_notification(caplog):
    slow = _SlowNotifier(0.05)
    slow.QUEUE_SIZE = 2
    for title in "abcd":
        slow.queue(Notification(title=title, text=""))
    await slow.close()
    # queue() doesn't yield, so all four arrive before the worker takes one.
    assert slow.sent == ["c", "d"]
    assert "dropped notification: a" in caplog.text and "dropped notification: b" in caplog.text

