import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from isis_monitor.notifiers import TeamsNotifier, DummyNotifier, NotificationChannel
import aiohttp


# ---------------------------------------------------------------------------
# DummyNotifier
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dummy_notifier(caplog):
    import logging
    caplog.set_level(logging.INFO)
    notifier = DummyNotifier()
    await notifier.send("Test message", "TestChannel")
    assert "[DUMMY NOTIFIER - TestChannel] Test message" in caplog.text


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

    with patch("isis_monitor.notifiers.aiohttp.ClientSession", return_value=mock_session):
        await notifier.send("Test message", "TestChannel")

    mock_session.post.assert_called_once()
    args, kwargs = mock_session.post.call_args
    assert args[0] == "http://fake.webhook.url"
    assert kwargs["json"]["summary"] == "Test message"
    assert kwargs["json"]["attachments"][0]["content"]["channel"] == "TestChannel"


@pytest.mark.asyncio
async def test_teams_notifier_no_url():
    notifier = TeamsNotifier("")
    mock_session = make_mock_session()

    with patch("isis_monitor.notifiers.aiohttp.ClientSession", return_value=mock_session):
        await notifier.send("Test message", "TestChannel")

    mock_session.post.assert_not_called()


@pytest.mark.asyncio
async def test_teams_notifier_logs_error_on_bad_status(caplog):
    import logging
    notifier = TeamsNotifier("http://fake.webhook.url")
    mock_session = make_mock_session(status=400, response_text="Bad Request")

    with patch("isis_monitor.notifiers.aiohttp.ClientSession", return_value=mock_session):
        with caplog.at_level(logging.ERROR):
            await notifier.send("Test message")

    assert "400" in caplog.text


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

    await channel.broadcast("Broadcast message", "SubChannel")

    mock_notifier1.send.assert_called_once_with("Broadcast message", "SubChannel")
    mock_notifier2.send.assert_called_once_with("Broadcast message", "SubChannel")


@pytest.mark.asyncio
async def test_notification_channel_empty_logs_debug(caplog):
    """Broadcast on a channel with no notifiers should log at DEBUG level."""
    import logging
    channel = NotificationChannel("Beam Updates")

    with caplog.at_level(logging.DEBUG):
        await channel.broadcast("some message")

    assert "Beam Updates" in caplog.text
    assert "no notifiers" in caplog.text
