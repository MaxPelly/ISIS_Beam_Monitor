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
    ts1_beam_current_pv: str = "AC:TS1:BEAM:CURR"
    ts2_beam_current_pv: str = "AC:TS2:BEAM:CURR"
    muon_beam_current_pv: str = "AC:MUON:BEAM:CURR"

    # BEAM_BOUNDARIES (tuples of cutoff thresholds)
    ts1_boundaries: tuple = (0.0, 50.0, 140.0)
    ts2_boundaries: tuple = (0.0, 10.0, 30.0)
    muon_boundaries: tuple = (0.0, 2.0, 5.0)

    # TIMEOUTS_INTERVALS
    mcr_poll_interval: float = 60.0
    beam_reconnect_interval: float = 5.0
    webhook_timeout: float = 10.0

    # TUI settings
    history_maxlen: int = 60    # samples retained per beam target
    sample_interval: float = 60.0  # seconds between graph samples
    refresh_per_second: int = 4
    logs_maxlen: int = 50

    # LOGGING
    log_file: str = "monitor.log"
    log_level: str = "INFO"
    log_max_bytes: int = 5_000_000
    log_backup_count: int = 3


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
    counts_pv = config.get("PVS", "counts_pv", fallback="IN:PEARL:CS:DASHBOARD:TAB:2:1:VALUE")
    run_name_pv = config.get("PVS", "run_name_pv", fallback="IN:PEARL:DAE:WDTITLE")
    ts1_beam_current_pv = config.get("PVS", "ts1_beam_current_pv", fallback="AC:TS1:BEAM:CURR")
    ts2_beam_current_pv = config.get("PVS", "ts2_beam_current_pv", fallback="AC:TS2:BEAM:CURR")
    muon_beam_current_pv = config.get("PVS", "muon_beam_current_pv", fallback="AC:MUON:BEAM:CURR")

    # BEAM_BOUNDARIES
    def _parse_tuple(section, key, default):
        raw = config.get(section, key, fallback="")
        if not raw:
            return default
        try:
            return tuple(float(x.strip()) for x in raw.split(","))
        except ValueError as e:
            raise ConfigError(f"Invalid comma-separated floats for {key}: {e}")
            
    ts1_boundaries = _parse_tuple("BEAM_BOUNDARIES", "ts1_boundaries", (0.0, 50.0, 140.0))
    ts2_boundaries = _parse_tuple("BEAM_BOUNDARIES", "ts2_boundaries", (0.0, 10.0, 30.0))
    muon_boundaries = _parse_tuple("BEAM_BOUNDARIES", "muon_boundaries", (0.0, 2.0, 5.0))

    # TIMEOUTS_INTERVALS
    mcr_poll_interval = config.getfloat("TIMEOUTS_INTERVALS", "mcr_poll_interval", fallback=60.0)
    beam_reconnect_interval = config.getfloat("TIMEOUTS_INTERVALS", "beam_reconnect_interval", fallback=5.0)
    webhook_timeout = config.getfloat("TIMEOUTS_INTERVALS", "webhook_timeout", fallback=10.0)

    # LOGGING
    log_file = config.get("LOGGING", "log_file", fallback="monitor.log")
    log_level = config.get("LOGGING", "log_level", fallback="INFO")
    log_max_bytes = config.getint("LOGGING", "log_max_bytes", fallback=5_000_000)
    log_backup_count = config.getint("LOGGING", "log_backup_count", fallback=3)

    # TUI (fully optional section)
    try:
        history_maxlen = config.getint("TUI", "history_maxlen", fallback=60)
        sample_interval = config.getfloat("TUI", "sample_interval", fallback=60.0)
        refresh_per_second = config.getint("TUI", "refresh_per_second", fallback=4)
        logs_maxlen = config.getint("TUI", "logs_maxlen", fallback=50)
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
        ts1_beam_current_pv=ts1_beam_current_pv,
        ts2_beam_current_pv=ts2_beam_current_pv,
        muon_beam_current_pv=muon_beam_current_pv,
        ts1_boundaries=ts1_boundaries,
        ts2_boundaries=ts2_boundaries,
        muon_boundaries=muon_boundaries,
        mcr_poll_interval=mcr_poll_interval,
        beam_reconnect_interval=beam_reconnect_interval,
        webhook_timeout=webhook_timeout,
        history_maxlen=history_maxlen,
        sample_interval=sample_interval,
        refresh_per_second=refresh_per_second,
        logs_maxlen=logs_maxlen,
        log_file=log_file,
        log_level=log_level,
        log_max_bytes=log_max_bytes,
        log_backup_count=log_backup_count,
    )
