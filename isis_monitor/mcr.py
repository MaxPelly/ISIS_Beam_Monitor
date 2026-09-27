import asyncio
import contextlib
import logging
import random
import re
from datetime import datetime, timezone
import aiohttp
from typing import Optional

from isis_monitor.config import AppConfig
from isis_monitor.messages import mcr_news
from isis_monitor.notifiers import NotificationChannel
from isis_monitor.protocols import MonitorSinkProtocol

logger = logging.getLogger(__name__)

# Matches the line-number prefix that separates news entries (2+ digits for robustness)
_FEED_SPLIT_RE = re.compile(r"\r\n[0-9]{2,}")


class MCRNewsMonitor:
    def __init__(
        self,
        config: AppConfig,
        channel: NotificationChannel,
        notify_current: bool = False,
        sink: Optional[MonitorSinkProtocol] = None,
        rng: Optional[random.Random] = None,
    ):
        self.config = config
        self.url = config.mcr_news_url
        self.channel = channel
        self.notify_current = notify_current
        self.sink = sink
        self._rng = rng or random.Random()
        self.old_news: Optional[str] = None
        self._force_reconnect = asyncio.Event()

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

    def _set_health(self, status: str) -> None:
        if self.sink:
            self.sink.update_health("mcr", status)

    async def _wait(self, seconds: float) -> bool:
        """Sleep for `seconds`, or less if a reconnect is requested (returns True)."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._force_reconnect.wait(), timeout=seconds)
        requested = self._force_reconnect.is_set()
        self._force_reconnect.clear()
        return requested

    def _publish_news(self, news: str) -> None:
        self.old_news = news
        if self.sink:
            self.sink.update_mcr_news(news)

    async def run(self) -> None:
        """Poll until cancelled."""
        logger.info(f"MCR Monitor started. Watching {self.url}...")
        self._set_health("starting")

        # TCPConnector with DNS TTL avoids stale connections on long-running sessions
        connector = aiohttp.TCPConnector(ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector) as session:
            if self.notify_current:
                self.old_news = ""  # so the first successful poll is broadcast
            else:
                while (baseline := await self.get_news(session)) is None:
                    self._set_health("error")
                    await self._wait(self.config.mcr_poll_interval)
                self._set_health("connected")
                logger.info(f"Current MCR News: {baseline}")
                self._publish_news(baseline)

            failures = 0
            while True:
                if await self._wait(self.config.mcr_poll_interval * min(2 ** failures, 8)):
                    failures = 0
                    self._set_health("reconnecting")

                news = await self.get_news(session)
                if news is None:
                    failures += 1
                    self._set_health("error")
                    logger.debug(f"MCR fetch failed (attempt {failures}).")
                    continue

                failures = 0
                self._set_health("connected")
                if news == self.old_news:
                    logger.debug("No new MCR news.")
                    continue

                logger.info(f"New MCR Update: {news}")
                self._publish_news(news)
                rng = self._rng if self.config.fun_mode else None
                notification = mcr_news(
                    news, datetime.now(timezone.utc),
                    url=self.config.mcr_page_url or None, rng=rng,
                )
                await self.channel.broadcast(notification)

    def request_reconnect(self) -> bool:
        if self._force_reconnect.is_set():
            return False
        self._force_reconnect.set()
        return True
