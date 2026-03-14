import configparser
from dataclasses import dataclass, field
from pathlib import Path
import logging

logger = logging.getLogger("isis_monitor.config")


class ConfigError(Exception):
    """Raised when the configuration file is missing or contains invalid values."""
    pass


@dataclass
class AppConfig:
    # DATA
    mcr_news_url: str
    isis_websocket_url: str

    # WEBHOOKS
    news_teams_url: str
    beam_teams_url: str
    experiment_teams_url: str

    # PVS — instrument-specific; override in [PVS] for non-PEARL instruments
    counts_pv: str = "IN:PEARL:CS:DASHBOARD:TAB:2:1:VALUE"
    run_name_pv: str = "IN:PEARL:DAE:WDTITLE"

    # TUI graph settings
    history_maxlen: int = 60    # samples retained per beam target
    sample_interval: float = 60.0  # seconds between graph samples


def load_config(config_path: Path) -> AppConfig:
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    config = configparser.ConfigParser(interpolation=None)
    config.read(config_path)

    # DATA
    mcr_news_url = config.get("DATA", "mcr_news_url", fallback="")
    if not mcr_news_url:
        raise ConfigError(
            f"[DATA] mcr_news_url is required. Please edit config file: {config_path}"
        )

    isis_websocket_url = config.get("DATA", "isis_websocket_url", fallback="")

    # WEBHOOKS
    news_teams_url = config.get("WEBHOOKS", "news_teams_url", fallback="")
    beam_teams_url = config.get("WEBHOOKS", "beam_teams_url", fallback="")
    experiment_teams_url = config.get("WEBHOOKS", "experiment_teams_url", fallback="")

    # PVS (optional — defaults to PEARL instrument values)
    counts_pv = config.get(
        "PVS", "counts_pv", fallback="IN:PEARL:CS:DASHBOARD:TAB:2:1:VALUE"
    )
    run_name_pv = config.get(
        "PVS", "run_name_pv", fallback="IN:PEARL:DAE:WDTITLE"
    )

    # TUI (fully optional section)
    try:
        history_maxlen = config.getint("TUI", "history_maxlen", fallback=60)
        sample_interval = config.getfloat("TUI", "sample_interval", fallback=60.0)
    except (ValueError, configparser.Error) as exc:
        raise ConfigError(f"[TUI] section contains invalid values: {exc}") from exc

    return AppConfig(
        mcr_news_url=mcr_news_url,
        isis_websocket_url=isis_websocket_url,
        news_teams_url=news_teams_url,
        beam_teams_url=beam_teams_url,
        experiment_teams_url=experiment_teams_url,
        counts_pv=counts_pv,
        run_name_pv=run_name_pv,
        history_maxlen=history_maxlen,
        sample_interval=sample_interval,
    )
