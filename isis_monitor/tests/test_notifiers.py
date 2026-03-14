import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from isis_monitor.notifiers import TeamsNotifier, DummyNotifier, NotificationChannel

@pytest.mark.asyncio
async def test_dummy_notifier(caplog):
    import logging
    caplog.set_level(logging.INFO)
    notifier = DummyNotifier()
    await notifier.send("Test message", "TestChannel")
    assert "[DUMMY NOTIFIER - TestChannel] Test message" in caplog.text

@pytest.mark.asyncio
async def test_teams_notifier_sends_request():
    notifier = TeamsNotifier("http://fake.webhook.url")
    
    # We mock requests.post to ensure NO REAL WEBHOOKS are fired
    with patch("isis_monitor.notifiers.requests.post") as mock_post:
        await notifier.send("Test message", "TestChannel")
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "http://fake.webhook.url"
        assert kwargs["json"]["summary"] == "Test message"
        assert kwargs["json"]["attachments"][0]["content"]["channel"] == "TestChannel"

@pytest.mark.asyncio
async def test_teams_notifier_no_url():
    notifier = TeamsNotifier("")
    with patch("isis_monitor.notifiers.requests.post") as mock_post:
        await notifier.send("Test message", "TestChannel")
        mock_post.assert_not_called()

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
