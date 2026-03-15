import asyncio
import logging
import re
from datetime import datetime
import aiohttp
from typing import Optional

from isis_monitor.config import AppConfig
from isis_monitor.notifiers import NotificationChannel
from isis_monitor.protocols import TUIProtocol

logger = logging.getLogger(__name__)

# Matches the line-number prefix that separates news entries (2+ digits for robustness)
_FEED_SPLIT_RE = re.compile(r"\r\n[0-9]{2,}")


class MCRNewsMonitor:
    def __init__(
        self,
        config: AppConfig,
        channel: NotificationChannel,
        notify_current: bool = False,
        tui: Optional[TUIProtocol] = None,
    ):
        self.config = config
        self.url = config.mcr_news_url
        self.channel = channel
        self.notify_current = notify_current
        self.tui = tui
        self.old_news: Optional[str] = None

    async def get_news(self, session: aiohttp.ClientSession) -> Optional[str]:
        try:
            async with session.get(
                self.url, timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    feed = await response.text()
                    parts = _FEED_SPLIT_RE.split(feed)
                    cleaned = re.sub(r"\s+", " ", parts[0].replace("\r\n", "")).strip()
                    if not cleaned:
                        logger.warning(
                            "MCR feed parsed to empty string; "
                            "upstream feed format may have changed."
                        )
                        return None
                    return cleaned
                else:
                    logger.warning(f"Failed to fetch MCR news. Status: {response.status}")
        except asyncio.TimeoutError:
            logger.warning("Timeout while fetching MCR news.")
        except Exception as e:
            logger.warning(f"Connection error while fetching MCR news: {e}")
        return None

    async def run(self, stop_event: Optional[asyncio.Event] = None):
        logger.info(f"MCR Monitor started. Watching {self.url}...")

        # TCPConnector with DNS TTL avoids stale connections on long-running sessions
        connector = aiohttp.TCPConnector(ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector) as session:
            # Initial fetch to establish baseline
            if not self.notify_current:
                while self.old_news is None:
                    if stop_event and stop_event.is_set():
                        return
                    self.old_news = await self.get_news(session)
                    if self.old_news:
                        logger.info(f"Current MCR News: {self.old_news}")
                        if self.tui:
                            self.tui.update_mcr_news(self.old_news)
                    else:
                        await asyncio.sleep(self.config.mcr_poll_interval)
            else:
                self.old_news = ""

            # Main polling loop
            consecutive_failures = 0
            while stop_event is None or not stop_event.is_set():
                try:
                    sleep_secs = self.config.mcr_poll_interval * min(
                        2 ** consecutive_failures, 8
                    )
                    await asyncio.sleep(sleep_secs)
                except asyncio.CancelledError:
                    return

                if stop_event and stop_event.is_set():
                    return

                new_news = await self.get_news(session)
                if new_news and new_news != self.old_news:
                    consecutive_failures = 0
                    self.old_news = new_news
                    logger.info(f"New MCR Update: {new_news}")
                    if self.tui:
                        self.tui.update_mcr_news(new_news)
                    await self.channel.broadcast(new_news)
                elif new_news:
                    consecutive_failures = 0
                    logger.debug("No new MCR news.")
                else:
                    consecutive_failures += 1
                    logger.debug(
                        f"MCR fetch failed (attempt {consecutive_failures}); "
                        f"next retry in {sleep_secs * min(2, 8):.0f}s."
                    )
