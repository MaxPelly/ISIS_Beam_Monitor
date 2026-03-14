import pytest
import asyncio
import aiohttp
from unittest.mock import AsyncMock, MagicMock, patch
from isis_monitor.config import AppConfig
from isis_monitor.notifiers import NotificationChannel
from isis_monitor.mcr import MCRNewsMonitor


@pytest.fixture
def mock_config():
    return AppConfig(
        mcr_news_url="http://test.url/mcr",
        isis_websocket_url="",
        news_teams_url="",
        beam_teams_url="",
        experiment_teams_url="",
    )


@pytest.fixture
def mock_channel():
    channel = NotificationChannel("Test")
    channel.broadcast = AsyncMock()
    return channel


# ---------------------------------------------------------------------------
# get_news()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcr_get_news_success(mock_config, mock_channel):
    monitor = MCRNewsMonitor(mock_config, mock_channel)

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.text = AsyncMock(
        return_value="Current news text\r\n\r\n12 more lines\r\n"
    )
    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__.return_value = mock_response
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

    result = await monitor.get_news(mock_session)
    assert result == "Current news text"


@pytest.mark.asyncio
async def test_mcr_get_news_success_three_digit_line_number(mock_config, mock_channel):
    """Regex must handle 3-digit line-number prefixes."""
    monitor = MCRNewsMonitor(mock_config, mock_channel)

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.text = AsyncMock(
        return_value="News content\r\n123 old line\r\n"
    )
    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__.return_value = mock_response
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

    result = await monitor.get_news(mock_session)
    assert result == "News content"


@pytest.mark.asyncio
async def test_mcr_get_news_failure(mock_config, mock_channel):
    monitor = MCRNewsMonitor(mock_config, mock_channel)

    mock_response = AsyncMock()
    mock_response.status = 500
    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__.return_value = mock_response
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

    result = await monitor.get_news(mock_session)
    assert result is None


@pytest.mark.asyncio
async def test_mcr_get_news_timeout(mock_config, mock_channel):
    monitor = MCRNewsMonitor(mock_config, mock_channel)

    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__.side_effect = asyncio.TimeoutError()
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

    result = await monitor.get_news(mock_session)
    assert result is None


# ---------------------------------------------------------------------------
# run() — polling loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcr_run_broadcasts_on_news_change(mock_config, mock_channel):
    """Verify that the polling loop broadcasts exactly once when news changes.

    Uses notify_current=False so the initial fetch seeds old_news="News A".
    Sequence: initial fetch → "News A", poll-1 → "News A" (no broadcast),
              poll-2 → "News B" (broadcast + stop).
    """
    monitor = MCRNewsMonitor(mock_config, mock_channel, notify_current=False)

    news_items = ["News A", "News A", "News B"]
    call_index = 0

    async def fake_get_news(_session):
        nonlocal call_index
        news = news_items[call_index] if call_index < len(news_items) else "News B"
        call_index += 1
        # Set stop after we've delivered the last item
        if call_index >= len(news_items):
            stop_event.set()
        return news

    stop_event = asyncio.Event()

    with patch.object(monitor, "get_news", side_effect=fake_get_news), \
         patch("isis_monitor.mcr.asyncio.sleep", new_callable=AsyncMock), \
         patch("isis_monitor.mcr.aiohttp.TCPConnector"), \
         patch("isis_monitor.mcr.aiohttp.ClientSession") as mock_cls:

        mock_session = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        await monitor.run(stop_event)

    mock_channel.broadcast.assert_called_once_with("News B")


@pytest.mark.asyncio
async def test_mcr_run_stops_on_event(mock_config, mock_channel):
    """run() exits promptly when stop_event is set."""
    monitor = MCRNewsMonitor(mock_config, mock_channel, notify_current=True)

    stop_event = asyncio.Event()
    stop_event.set()  # pre-set — loop should exit immediately

    with patch("isis_monitor.mcr.asyncio.sleep", new_callable=AsyncMock), \
         patch("isis_monitor.mcr.aiohttp.TCPConnector"), \
         patch("isis_monitor.mcr.aiohttp.ClientSession") as mock_cls:

        mock_session = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)

        await monitor.run(stop_event)

    mock_channel.broadcast.assert_not_called()
