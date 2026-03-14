import pytest
import aiohttp
from unittest.mock import AsyncMock, MagicMock, patch
from isis_monitor.config import AppConfig
from isis_monitor.notifiers import NotificationChannel
from isis_monitor.mcr import MCRNewsMonitor
import asyncio

@pytest.fixture
def mock_config():
    return AppConfig(
        mcr_news_url="http://test.url/mcr",
        isis_websocket_url="",
        news_teams_url="",
        beam_teams_url="",
        experiment_teams_url=""
    )

@pytest.fixture
def mock_channel():
    channel = NotificationChannel("Test")
    channel.broadcast = AsyncMock()
    return channel

@pytest.mark.asyncio
async def test_mcr_get_news_success(mock_config, mock_channel):
    monitor = MCRNewsMonitor(mock_config, mock_channel)
    
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.text = AsyncMock(return_value="Current news text\r\n\r\n12 more lines\r\n")
    
    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__.return_value = mock_response
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

    result = await monitor.get_news(mock_session)
    # the regex substitution cleans this to just the first part without extra spaces
    assert result == "Current news text"

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
    # make get() context manager raise a timeout error
    mock_session.get.return_value.__aenter__.side_effect = asyncio.TimeoutError()
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

    result = await monitor.get_news(mock_session)
    assert result is None
