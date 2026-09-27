import asyncio
import contextlib

import aiohttp
import pytest
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

async def test_mcr_get_news_success(mock_config, mock_channel):
    """The news is the text before the "<N> more lines" / old-line footer,
    whatever the number of digits."""
    monitor = MCRNewsMonitor(mock_config, mock_channel)
    for text, expected in [
        ("Current news text\r\n\r\n12 more lines\r\n", "Current news text"),
        ("News content\r\n123 old line\r\n", "News content"),
    ]:
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value=text)
        mock_session = MagicMock()
        mock_session.get.return_value.__aenter__.return_value = mock_response
        mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

        assert await monitor.get_news(mock_session) == expected


async def test_mcr_get_news_failure(mock_config, mock_channel):
    monitor = MCRNewsMonitor(mock_config, mock_channel)

    mock_response = AsyncMock()
    mock_response.status = 500
    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__.return_value = mock_response
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

    result = await monitor.get_news(mock_session)
    assert result is None


async def test_mcr_get_news_timeout(mock_config, mock_channel):
    monitor = MCRNewsMonitor(mock_config, mock_channel)

    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__.side_effect = asyncio.TimeoutError()
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

    result = await monitor.get_news(mock_session)
    assert result is None


async def test_mcr_get_news_empty_feed_returns_none(mock_config, mock_channel, caplog):
    monitor = MCRNewsMonitor(mock_config, mock_channel)
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.text = AsyncMock(return_value="  \r\n  ")
    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__.return_value = mock_response
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=None)

    assert await monitor.get_news(mock_session) is None
    assert "parsed to empty string" in caplog.text


async def test_mcr_get_news_connection_error_returns_none(mock_config, mock_channel):
    monitor = MCRNewsMonitor(mock_config, mock_channel)
    mock_session = MagicMock()
    mock_session.get.side_effect = aiohttp.ClientConnectionError("refused")
    assert await monitor.get_news(mock_session) is None


# ---------------------------------------------------------------------------
# run() — polling loop
# ---------------------------------------------------------------------------

class _Stop(Exception):
    """Raised by a fake get_news() to end run()'s otherwise-infinite loop."""


@contextlib.contextmanager
def fake_session():
    with patch("isis_monitor.mcr.aiohttp.TCPConnector"), \
         patch("isis_monitor.mcr.aiohttp.ClientSession") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        yield


def scripted_news(*items):
    """A get_news() replacement returning `items` in order, then raising _Stop."""
    remaining = list(items)

    async def get_news(_session):
        if not remaining:
            raise _Stop
        return remaining.pop(0)

    return get_news


async def run_script(monitor, *items, waits=None):
    """Run monitor.run() against scripted news with instant waits; returns wait delays."""
    delays = [] if waits is None else waits

    async def instant_wait(seconds):
        delays.append(seconds)
        return False

    with fake_session(), \
         patch.object(monitor, "get_news", side_effect=scripted_news(*items)), \
         patch.object(monitor, "_wait", side_effect=instant_wait):
        with pytest.raises(_Stop):
            await monitor.run()
    return delays


async def test_mcr_run_broadcasts_only_on_news_change(mock_config, mock_channel):
    sink = MagicMock()
    monitor = MCRNewsMonitor(mock_config, mock_channel, notify_current=False, sink=sink)

    await run_script(monitor, "News A", "News A", "News B")

    mock_channel.broadcast.assert_called_once()
    notification = mock_channel.broadcast.call_args[0][0]
    assert notification.text == "News B"
    assert notification.emoji == "📰"
    assert notification.flavour == ""  # fun_mode defaults to False
    assert [c.args[0] for c in sink.update_mcr_news.call_args_list] == ["News A", "News B"]
    assert monitor.old_news == "News B"


async def test_mcr_run_notify_current_broadcasts_first_poll(mock_config, mock_channel):
    monitor = MCRNewsMonitor(mock_config, mock_channel, notify_current=True)
    await run_script(monitor, "News A")
    mock_channel.broadcast.assert_called_once()
    assert mock_channel.broadcast.call_args[0][0].text == "News A"


async def test_mcr_run_fun_mode_adds_flavour(mock_config):
    import random
    from dataclasses import replace

    channel = NotificationChannel("Test")
    channel.broadcast = AsyncMock()
    monitor = MCRNewsMonitor(
        replace(mock_config, fun_mode=True), channel, notify_current=False, rng=random.Random(1)
    )
    await run_script(monitor, "News A", "News B")
    assert channel.broadcast.call_args[0][0].flavour != ""


async def test_mcr_run_baseline_retries_until_news_available(mock_config, mock_channel):
    sink = MagicMock()
    monitor = MCRNewsMonitor(mock_config, mock_channel, notify_current=False, sink=sink)
    delays = await run_script(monitor, None, None, "News A")
    base = mock_config.mcr_poll_interval
    assert delays == [base, base, base]  # two baseline retries, then the first poll wait
    sink.update_mcr_news.assert_called_once_with("News A")
    mock_channel.broadcast.assert_not_called()
    # The failing first fetches show as an error, not stuck at "starting".
    statuses = [c.args[1] for c in sink.update_health.call_args_list]
    assert statuses[:4] == ["starting", "error", "error", "connected"]


async def test_mcr_run_backoff_on_consecutive_failures(mock_config, mock_channel):
    """Poll delay doubles per consecutive failure, capped at 8x, and resets on success."""
    sink = MagicMock()
    monitor = MCRNewsMonitor(mock_config, mock_channel, notify_current=True, sink=sink)

    delays = await run_script(monitor, None, None, None, None, "News", None)

    base = mock_config.mcr_poll_interval
    assert delays == [base, base * 2, base * 4, base * 8, base * 8, base, base * 2]
    statuses = [c.args[1] for c in sink.update_health.call_args_list]
    assert statuses[0] == "starting"
    assert "error" in statuses and "connected" in statuses


async def test_mcr_run_cancels_promptly_mid_wait(mock_config, mock_channel):
    """Shutdown cancels run(); it must not sit out the poll interval."""
    from dataclasses import replace
    monitor = MCRNewsMonitor(replace(mock_config, mcr_poll_interval=3600), mock_channel, notify_current=True)
    with fake_session(), patch.object(monitor, "get_news", AsyncMock(return_value="x")):
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)


async def test_mcr_request_reconnect_polls_immediately(mock_config, mock_channel):
    from dataclasses import replace
    sink = MagicMock()
    monitor = MCRNewsMonitor(
        replace(mock_config, mcr_poll_interval=3600), mock_channel, notify_current=True, sink=sink
    )
    polled = asyncio.Event()

    async def get_news(_session):
        polled.set()
        return "News"

    with fake_session(), patch.object(monitor, "get_news", side_effect=get_news):
        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.05)
        assert not polled.is_set()
        assert monitor.request_reconnect() is True
        assert monitor.request_reconnect() is False  # already pending
        await asyncio.wait_for(polled.wait(), timeout=1.0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert ("mcr", "reconnecting") in [c.args for c in sink.update_health.call_args_list]
    assert monitor.request_reconnect() is True  # flag was consumed
