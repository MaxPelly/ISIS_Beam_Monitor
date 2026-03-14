import asyncio
import logging
import re
from datetime import datetime
import aiohttp
from typing import Optional

from isis_monitor.config import AppConfig
from isis_monitor.notifiers import NotificationChannel

logger = logging.getLogger(__name__)

class MCRNewsMonitor:
    def __init__(self, config: AppConfig, channel: NotificationChannel, notify_current: bool = False, tui=None):
        self.url = config.mcr_news_url
        self.channel = channel
        self.notify_current = notify_current
        self.tui = tui
        self.old_news: Optional[str] = None

    async def get_news(self, session: aiohttp.ClientSession) -> Optional[str]:
        try:
            async with session.get(self.url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    feed = await response.text()
                    # Clean up the text just like the original script
                    cleaned = re.sub(r"\s+", " ", re.split(r"\r\n[0-9]{2}", feed)[0].replace("\r\n", ""))
                    return cleaned
                else:
                    logger.warning(f"Failed to fetch MCR news. Status: {response.status}")
        except asyncio.TimeoutError:
            logger.warning("Timeout while fetching MCR news.")
        except Exception as e:
            logger.warning(f"Connection Error while fetching MCR news: {e}")
        return None

    async def run(self):
        logger.info(f"MCR Monitor started. Watching {self.url}...")
        
        async with aiohttp.ClientSession() as session:
            # Initial fetch to populate old_news
            if not self.notify_current:
                while self.old_news is None:
                    self.old_news = await self.get_news(session)
                    if self.old_news:
                        logger.info(f"Current MCR News: {self.old_news}")
                        if self.tui:
                            self.tui.update_mcr_news(self.old_news)
                    else:
                        await asyncio.sleep(60)
            else:
                self.old_news = ""

            # Main polling loop
            while True:
                await asyncio.sleep(60)
                new_news = await self.get_news(session)
                
                if new_news and new_news != self.old_news:
                    self.old_news = new_news
                    msg = f"New MCR Update: {new_news}"
                    logger.info(msg)
                    if self.tui:
                        self.tui.update_mcr_news(new_news)
                    await self.channel.broadcast(new_news)
                elif new_news:
                    logger.debug("No new MCR news.")
