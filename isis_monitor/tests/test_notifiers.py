import asyncio
import pytest
from dataclasses import replace
from unittest.mock import patch, MagicMock, AsyncMock
from isis_monitor.notifiers import TeamsNotifier, DummyNotifier, NotificationChannel, Notifier
from isis_monitor.messages import Notification, Severity
import aiohttp


# ---------------------------------------------------------------------------
# DummyNotifier
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dummy_notifier(caplog):
    import logging
    caplog.set_level(logging.INFO)
    notifier = DummyNotifier()
    notification = Notification(title="Test", text="Test message", emoji="🔔")
    await notifier.send(notification)
    assert "🔔 Test" in caplog.text
    assert "Test message" in caplog.text


# ---------------------------------------------------------------------------
# TeamsNotifier — helpers
# ---------------------------------------------------------------------------

def make_mock_session(status: int = 200, response_text: str = "OK"):
    """Return a mock for aiohttp.ClientSession usable as ``async with ... as session``.

    ``session.post(url, ...)`` returns a sync MagicMock so that
    ``async with session.post(...) as resp`` works without a coroutine mismatch.
    """
    # The response object yielded by `async with session.post(...) as resp`
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.text = AsyncMock(return_value=response_text)

    # The context-manager returned by session.post(url, ...)
    post_ctx = MagicMock()
    post_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    post_ctx.__aexit__ = AsyncMock(return_value=None)

    # The session itself  (async with aiohttp.ClientSession() as session)
    mock_session = MagicMock()
    mock_session.post.return_value = post_ctx
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    return mock_session



# ---------------------------------------------------------------------------
# TeamsNotifier — send
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_teams_notifier_sends_request():
    notifier = TeamsNotifier("http://fake.webhook.url")
    mock_session = make_mock_session(status=200)
    notification = Notification(title="Test title", text="Test message")

    with patch("isis_monitor.notifiers.aiohttp.ClientSession", return_value=mock_session):
        await notifier.send(notification)

    mock_session.post.assert_called_once()
    args, kwargs = mock_session.post.call_args
    assert args[0] == "http://fake.webhook.url"
    assert kwargs["json"]["summary"] == "Test title | Test message"
    card = kwargs["json"]["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    body_texts = [item["text"] for item in card["body"] if "text" in item]
    assert "Test message" in body_texts


@pytest.mark.asyncio
async def test_teams_notifier_payload_includes_channel_and_content_summary():
    """Downstream routing (e.g. Power Automate) reads these two fields from
    inside `content`, not just the top-level `summary` — both must be present."""
    notifier = TeamsNotifier("http://fake.webhook.url")
    mock_session = make_mock_session(status=200)
    notification = Notification(title="Test title", text="Test message", channel="TS1")

    with patch("isis_monitor.notifiers.aiohttp.ClientSession", return_value=mock_session):
        await notifier.send(notification)

    payload = mock_session.post.call_args.kwargs["json"]
    card = payload["attachments"][0]["content"]
    assert card["channel"] == "TS1"
    assert card["summary"] == payload["summary"]


@pytest.mark.asyncio
async def test_teams_notifier_no_url():
    notifier = TeamsNotifier("")
    mock_session = make_mock_session()
    notification = Notification(title="Test title", text="Test message")

    with patch("isis_monitor.notifiers.aiohttp.ClientSession", return_value=mock_session):
        await notifier.send(notification)

    mock_session.post.assert_not_called()


@pytest.mark.asyncio
async def test_teams_notifier_logs_error_on_bad_status(caplog):
    import logging
    notifier = TeamsNotifier("http://fake.webhook.url")
    mock_session = make_mock_session(status=400, response_text="Bad Request")
    notification = Notification(title="Test title", text="Test message")

    with patch("isis_monitor.notifiers.aiohttp.ClientSession", return_value=mock_session):
        with caplog.at_level(logging.ERROR):
            await notifier.send(notification)

    assert "400" in caplog.text


# ---------------------------------------------------------------------------
# TeamsNotifier — card structure
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("severity, style", [
    (Severity.INFO, "default"),
    (Severity.GOOD, "good"),
    (Severity.WARNING, "warning"),
    (Severity.ATTENTION, "attention"),
])
def test_create_payload_severity_style(severity, style):
    notifier = TeamsNotifier("http://fake.webhook.url")
    notification = Notification(title="Title", text="Text", severity=severity)

    payload = notifier._create_payload(notification)

    card = payload["attachments"][0]["content"]
    header_container = card["body"][0]
    assert header_container["style"] == style


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


def test_create_payload_includes_flavour_line():
    notifier = TeamsNotifier("http://fake.webhook.url")
    notification = Notification(title="Title", text="Text", flavour="Beam's back, baby.")

    payload = notifier._create_payload(notification)

    card = payload["attachments"][0]["content"]
    texts = [item["text"] for item in card["body"] if "text" in item]
    assert "_Beam's back, baby._" in texts


def test_create_payload_includes_url_action():
    notifier = TeamsNotifier("http://fake.webhook.url")
    notification = Notification(title="Title", text="Text", url="https://example.com/news")

    payload = notifier._create_payload(notification)

    card = payload["attachments"][0]["content"]
    assert card["actions"] == [
        {"type": "Action.OpenUrl", "title": "Open", "url": "https://example.com/news"}
    ]


def test_create_payload_no_url_means_no_actions():
    notifier = TeamsNotifier("http://fake.webhook.url")
    notification = Notification(title="Title", text="Text")

    payload = notifier._create_payload(notification)

    card = payload["attachments"][0]["content"]
    assert "actions" not in card


# ---------------------------------------------------------------------------
# NotificationChannel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notification_channel():
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


@pytest.mark.asyncio
async def test_notification_channel_does_not_override_explicit_channel():
    channel = NotificationChannel("TestChannel")
    mock_notifier = MagicMock()
    mock_notifier.send = AsyncMock()
    channel.add_notifier(mock_notifier)

    notification = Notification(title="Title", text="Text", channel="TS1")
    await channel.broadcast(notification)
    await channel.flush()

    mock_notifier.send.assert_called_once_with(notification)


@pytest.mark.asyncio
async def test_notification_channel_empty_logs_debug(caplog):
    """Broadcast on a channel with no notifiers should log at DEBUG level."""
    import logging
    channel = NotificationChannel("Beam Updates")

    with caplog.at_level(logging.DEBUG):
        await channel.broadcast(Notification(title="Title", text="some message"))

    assert "Beam Updates" in caplog.text
    assert "no notifiers" in caplog.text


class _FailingNotifier(DummyNotifier):
    async def send(self, notification):
        raise RuntimeError("webhook exploded")


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_teams_notifier_logs_connection_error(caplog):
    notifier = TeamsNotifier("http://example.invalid/hook")
    session = MagicMock()
    session.closed = False
    session.post.side_effect = aiohttp.ClientConnectionError("refused")
    notifier._session = session

    await notifier.send(Notification(title="t", text="x"))
    assert "Failed to send Teams webhook: refused" in caplog.text


@pytest.mark.asyncio
async def test_teams_notifier_reuses_and_closes_session():
    notifier = TeamsNotifier("http://example.invalid/hook")
    session = await notifier._get_session()
    assert await notifier._get_session() is session
    await notifier.close()
    assert session.closed
    assert notifier._session is None
    await notifier.close()  # idempotent


@pytest.mark.asyncio
async def test_notification_channel_close_closes_all_notifiers():
    channel = NotificationChannel("Beam")
    teams = TeamsNotifier("http://example.invalid/hook")
    await teams._get_session()
    channel.add_notifier(teams)
    channel.add_notifier(DummyNotifier())  # base-class close() is a no-op
    await channel.close()
    assert teams._session is None


def test_create_payload_includes_timestamp_line():
    from datetime import datetime, timezone
    notifier = TeamsNotifier("http://x")
    ts = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
    payload = notifier._create_payload(Notification(title="t", text="x", timestamp=ts))
    body = payload["attachments"][0]["content"]["body"]
    assert body[-1]["isSubtle"] is True
    assert "Jan" in body[-1]["text"]



class _SlowNotifier(Notifier):
    def __init__(self, delay):
        self.delay = delay
        self.sent = []

    async def send(self, notification):
        await asyncio.sleep(self.delay)
        self.sent.append(notification.title)


@pytest.mark.asyncio
async def test_broadcast_returns_without_waiting_for_a_slow_webhook():
    """A hung webhook mustn't hold up the caller (e.g. the beam WebSocket loop)."""
    channel = NotificationChannel("Beam")
    slow = _SlowNotifier(10)
    channel.add_notifier(slow)
    await asyncio.wait_for(channel.broadcast(Notification(title="a", text="")), 0.1)
    assert slow.sent == []
    channel.CLOSE_TIMEOUT = 0.05
    await channel.close()  # gives up on the unsent one rather than hanging


@pytest.mark.asyncio
async def test_queued_notifications_are_sent_in_order_and_flushed_on_close():
    channel = NotificationChannel("Beam")
    slow = _SlowNotifier(0.01)
    channel.add_notifier(slow)
    for title in "abc":
        await channel.broadcast(Notification(title=title, text=""))
    await channel.close()
    assert slow.sent == ["a", "b", "c"]


@pytest.mark.asyncio
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



@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_broadcast_after_close_is_ignored(caplog):
    channel = NotificationChannel("Beam")
    slow = _SlowNotifier(0)
    channel.add_notifier(slow)
    await channel.close()
    await channel.broadcast(Notification(title="late", text=""))
    await asyncio.sleep(0.01)
    assert slow.sent == [] and channel._worker is None
    assert "is closed; not sending: late" in caplog.text
