import configparser
from dataclasses import dataclass
from pathlib import Path
import logging
import sys

logger = logging.getLogger("isis_monitor.config")

@dataclass
class AppConfig:
    # DATA
    mcr_news_url: str
    isis_websocket_url: str
    
    # WEBHOOKS
    news_teams_url: str
    beam_teams_url: str
    experiment_teams_url: str


def load_config(config_path: Path) -> AppConfig:
    if not config_path.exists():
        logger.critical(f"Config file not found: {config_path}")
        sys.exit(1)

    config = configparser.ConfigParser(interpolation=None)
    config.read(config_path)

    # DATA
    mcr_news_url = config.get("DATA", "mcr_news_url", fallback="")
    if not mcr_news_url:
        logger.critical(f"MCR news url is required. Please edit config file. Current config file is {config_path}")
        sys.exit(1)
        
    isis_websocket_url = config.get("DATA", "isis_websocket_url", fallback="")

    # WEBHOOKS
    news_teams_url = config.get("WEBHOOKS", "news_teams_url", fallback="")
    beam_teams_url = config.get("WEBHOOKS", "beam_teams_url", fallback="")
    experiment_teams_url = config.get("WEBHOOKS", "experiment_teams_url", fallback="")

    return AppConfig(
        mcr_news_url=mcr_news_url,
        isis_websocket_url=isis_websocket_url,
        news_teams_url=news_teams_url,
        beam_teams_url=beam_teams_url,
        experiment_teams_url=experiment_teams_url
    )
