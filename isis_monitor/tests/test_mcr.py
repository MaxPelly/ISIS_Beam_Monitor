import asyncio
import contextlib

import aiohttp
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from isis_monitor.config import AppConfig
from isis_monitor.daemon_state import DaemonState
from isis_monitor.tests.helpers import fake_channel
from isis_monitor import mcr
from isis_monitor.mcr import MCRNewsMonitor


@pytest.fixture
def mock_config():
    return AppConfig(mcr_news_url="http://test.url/mcr")


@pytest.fixture
def mock_channel():
    return fake_channel()


# ---------------------------------------------------------------------------
# get_news()
# ---------------------------------------------------------------------------

def news_session(status=200, text="", error=None, charset="utf-8"):
    """A mock aiohttp session whose get() responds with `status`/`text`, or raises `error` on entry."""
    session = MagicMock()
    ctx = session.get.return_value

    body = bytearray(text.encode())

    async def read(n):  # like aiohttp, a few bytes at a time rather than all n
        chunk = bytes(body[:min(n, 7)])
        del body[:len(chunk)]
        return chunk
    ctx.__aenter__.return_value = MagicMock(status=status, charset=charset, content=MagicMock(read=read))
    ctx.__aenter__.side_effect = error
    ctx.__aexit__ = AsyncMock(return_value=None)
    return session


def refused_session():
    session = MagicMock()
    session.get.side_effect = aiohttp.ClientConnectionError("refused")
    return session


@pytest.mark.parametrize("session, expected, log", [
    # The news is the text before the "<N> more lines" / old-line footer, whatever the digits.
    (news_session(text="Current news text\r\n\r\n12 more lines\r\n"), "Current news text", ""),
    (news_session(text="News content\r\n123 old line\r\n"), "News content", ""),
    (news_session(text="Beam on ✓\r\n12 more", charset="no-such-charset"), "Beam on ✓", ""),  # falls back to UTF-8
    (news_session(text="Beam\x1b[2J on\x07\r\n12 more"), "Beam [2J on", ""),  # control characters stripped
    # If the feed format changes and nothing splits it, the item is still short.
    (news_session(text="word " * 100_000), ("word " * 100_000)[:mcr.MAX_NEWS_CHARS - 1] + "…", ""),
    (news_session(status=500), None, "Status: 500"),
    (news_session(error=TimeoutError()), None, "Timeout"),
    (refused_session(), None, "Connection error while fetching MCR news: refused"),
    (news_session(text="  \r\n  "), None, "parsed to empty string"),
])
async def test_mcr_get_news(mock_config, mock_channel, caplog, session, expected, log):
    assert await MCRNewsMonitor(mock_config, mock_channel).get_news(session) == expected
    assert log in caplog.text


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


async def test_mcr_run_broadcasts_only_on_news_change_and_survives_a_failed_send(mock_config, mock_channel, caplog):
    sink = MagicMock(spec=DaemonState())
    monitor = MCRNewsMonitor(mock_config, mock_channel, notify_current=False, sink=sink)
    mock_channel.broadcast.side_effect = [RuntimeError("queue broken"), None]

    await run_script(monitor, "News A", "News A", "News B", "News C")

    assert "Failed to send the MCR news notification" in caplog.text
    assert [c.args[0].text for c in mock_channel.broadcast.call_args_list] == ["News B", "News C"]
    assert mock_channel.broadcast.call_args[0][0].emoji == "📰"
    assert [c.args[0] for c in sink.update_mcr_news.call_args_list] == ["News A", "News B", "News C"]
    assert monitor.old_news == "News C"


@pytest.mark.parametrize("old_news, feed, sent, waits", [
    (None, "News A", 1, 2),  # nothing restored, but -n: the first poll is announced
    # With the news restored from before a restart, there's no baseline fetch:
    # the first poll, straight away, announces anything posted since.
    ("News A", "News B", 1, 1),
    ("News A", "News A", 0, 1),
])
async def test_mcr_run_first_poll(mock_config, mock_channel, old_news, feed, sent, waits):
    monitor = MCRNewsMonitor(mock_config, mock_channel, notify_current=True, old_news=old_news)
    delays = await run_script(monitor, feed)
    assert delays == [mock_config.mcr_poll_interval] * waits
    assert [c.args[0].text for c in mock_channel.broadcast.call_args_list] == [feed] * sent
    assert monitor.old_news == feed


async def test_mcr_run_baseline_retries_until_news_available(mock_config, mock_channel):
    sink = MagicMock(spec=DaemonState())
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
    sink = MagicMock(spec=DaemonState())
    monitor = MCRNewsMonitor(mock_config, mock_channel, notify_current=True, sink=sink)

    delays = await run_script(monitor, None, None, None, None, "News", None)

    base = mock_config.mcr_poll_interval
    assert delays == [base, base * 2, base * 4, base * 8, base * 8, base, base * 2]
    statuses = [c.args[1] for c in sink.update_health.call_args_list]
    assert statuses[0] == "starting"
    assert "error" in statuses and "connected" in statuses


async def test_mcr_request_reconnect_polls_immediately_and_shutdown_cancels_the_wait(mock_config, mock_channel):
    from dataclasses import replace
    sink = MagicMock(spec=DaemonState())
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
        # Shutdown cancels run(); it must not sit out the poll interval.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)

    assert ("mcr", "reconnecting") in [c.args for c in sink.update_health.call_args_list]
    assert monitor.request_reconnect() is True  # flag was consumed
