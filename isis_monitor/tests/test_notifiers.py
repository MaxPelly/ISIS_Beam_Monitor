import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from isis_monitor.notifiers import TeamsNotifier, DummyNotifier, NotificationChannel
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
    assert kwargs["json"]["summary"] == "Test title"
    card = kwargs["json"]["attachments"][0]["content"]
    assert card["type"] == "AdaptiveCard"
    body_texts = [item["text"] for item in card["body"] if "text" in item]
    assert "Test message" in body_texts


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

    mock_notifier1.send.assert_called_once_with(notification)
    mock_notifier2.send.assert_called_once_with(notification)


@pytest.mark.asyncio
async def test_notification_channel_empty_logs_debug(caplog):
    """Broadcast on a channel with no notifiers should log at DEBUG level."""
    import logging
    channel = NotificationChannel("Beam Updates")

    with caplog.at_level(logging.DEBUG):
        await channel.broadcast(Notification(title="Title", text="some message"))

    assert "Beam Updates" in caplog.text
    assert "no notifiers" in caplog.text
