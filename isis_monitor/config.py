import configparser
import logging
from dataclasses import dataclass, field, fields
from pathlib import Path

logger = logging.getLogger("isis_monitor.config")

# Must match the state_key values of isis_monitor.beam.BEAM_TARGETS — not
# imported directly to avoid a circular import (beam.py imports config.py).
_INSTRUMENT_TARGETS = ("TS1", "TS2", "Muon")


class ConfigError(Exception):
    """Raised when the configuration file is missing or contains invalid values."""


def _ini(section: str, default, key: str = ""):
    """A config field read from `[section] key` (key defaults to the field name)."""
    return field(default=default, metadata={"section": section, "key": key})


@dataclass
class AppConfig:
    mcr_news_url: str = _ini("DATA", "")
    isis_websocket_url: str = _ini("DATA", "")
    mcr_page_url: str = _ini("DATA", "")  # optional "Open MCR news" button link

    # Blank webhook URL disables that channel's Teams notifier.
    news_teams_url: str = _ini("WEBHOOKS", "")
    beam_teams_url: str = _ini("WEBHOOKS", "")
    experiment_teams_url: str = _ini("WEBHOOKS", "")

    # Instrument-specific; override in [PVS] for non-PEARL instruments
    counts_pv: str = _ini("PVS", "IN:PEARL:CS:DASHBOARD:TAB:2:1:VALUE")
    run_name_pv: str = _ini("PVS", "IN:PEARL:DAE:WDTITLE")
    ts1_beam_current_pv: str = _ini("PVS", "AC:TS1:BEAM:CURR")
    ts2_beam_current_pv: str = _ini("PVS", "AC:TS2:BEAM:CURR")
    muon_beam_current_pv: str = _ini("PVS", "AC:MUON:BEAM:CURR")
    instrument_target: str = _ini("PVS", "TS1")  # which beam target's state to report in run cards

    # off / low / medium cutoffs in uA
    ts1_boundaries: tuple = _ini("BEAM_BOUNDARIES", (0.0, 50.0, 140.0))
    ts2_boundaries: tuple = _ini("BEAM_BOUNDARIES", (0.0, 10.0, 30.0))
    muon_boundaries: tuple = _ini("BEAM_BOUNDARIES", (0.0, 2.0, 5.0))

    mcr_poll_interval: float = _ini("TIMEOUTS_INTERVALS", 60.0)
    beam_reconnect_interval: float = _ini("TIMEOUTS_INTERVALS", 5.0)
    webhook_timeout: float = _ini("TIMEOUTS_INTERVALS", 10.0)

    history_maxlen: int = _ini("TUI", 60)  # samples shown per beam target
    sample_interval: float = _ini("TUI", 60.0)  # seconds between history samples
    refresh_per_second: int = _ini("TUI", 4)
    logs_maxlen: int = _ini("TUI", 50)

    daemon_db_path: str = _ini("DAEMON", "beam_monitor.db", "db_path")
    daemon_socket_path: str = _ini("DAEMON", "/tmp/isis_beam_monitor.sock", "socket_path")
    daemon_lock_file: str = _ini("DAEMON", "/tmp/isis_beam_monitor.lock", "lock_file")
    retention_days: int = _ini("DAEMON", 7)

    # tui_socket_path falls back to daemon_socket_path when unset
    tui_socket_path: str = _ini("TUI_CLIENT", "/tmp/isis_beam_monitor.sock", "socket_path")
    tui_reconnect_initial: float = _ini("TUI_CLIENT", 1.0, "reconnect_initial")
    tui_reconnect_max: float = _ini("TUI_CLIENT", 15.0, "reconnect_max")

    log_file: str = _ini("LOGGING", "monitor.log")
    log_level: str = _ini("LOGGING", "INFO")
    log_max_bytes: int = _ini("LOGGING", 5_000_000)
    log_backup_count: int = _ini("LOGGING", 3)

    fun_mode: bool = _ini("NOTIFICATIONS", False)
    notifications_timezone: str = _ini("NOTIFICATIONS", "Europe/London", "timezone")
    debounce_seconds: float = _ini("NOTIFICATIONS", 20.0)
    stall_minutes: float = _ini("NOTIFICATIONS", 15.0)
    summary_time: str = _ini("NOTIFICATIONS", "08:00")  # local HH:MM the daily summary is sent at


def _parse_boundaries(raw: str) -> tuple:
    result = tuple(float(x.strip()) for x in raw.split(","))
    if len(result) != 3:
        raise ValueError(f"must have exactly 3 comma-separated values, got {len(result)}")
    return result


def _read_value(parser: configparser.ConfigParser, section: str, key: str, kind: type):
    if kind is str:
        return parser.get(section, key)
    if kind is tuple:
        return _parse_boundaries(parser.get(section, key))
    return {int: parser.getint, float: parser.getfloat, bool: parser.getboolean}[kind](section, key)


def _validate(config: AppConfig, config_path: Path) -> None:
    if not config.mcr_news_url:
        raise ConfigError(f"[DATA] mcr_news_url is required. Please edit config file: {config_path}")
    if not config.isis_websocket_url:
        logger.warning(
            "isis_websocket_url is empty — BeamMonitor will not run. "
            "Please set [DATA] isis_websocket_url in your config file."
        )
    if config.instrument_target not in _INSTRUMENT_TARGETS:
        raise ConfigError(
            f"[PVS] instrument_target must be one of {', '.join(_INSTRUMENT_TARGETS)}, "
            f"got '{config.instrument_target}'"
        )
    try:
        hour, minute = (int(x) for x in config.summary_time.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        raise ConfigError(f"[NOTIFICATIONS] summary_time must be 'HH:MM', got '{config.summary_time}'")
    if config.retention_days <= 0:
        raise ConfigError("[DAEMON] retention_days must be a positive integer")
    if config.tui_reconnect_initial <= 0 or config.tui_reconnect_max <= 0:
        raise ConfigError("[TUI_CLIENT] reconnect values must be positive")
    if config.tui_reconnect_initial > config.tui_reconnect_max:
        raise ConfigError("[TUI_CLIENT] reconnect_initial cannot be greater than reconnect_max")


def load_config(config_path: Path) -> AppConfig:
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path)

    values = {}
    for f in fields(AppConfig):
        section, key = f.metadata["section"], f.metadata["key"] or f.name
        if not parser.has_option(section, key):
            continue
        # A blank non-string value (e.g. "retention_days =") means "use the default".
        if f.type is not str and not parser.get(section, key).strip():
            continue
        try:
            values[f.name] = _read_value(parser, section, key, f.type)
        except ValueError as exc:
            raise ConfigError(f"[{section}] {key}: {exc}") from exc

    values.setdefault("tui_socket_path", values.get("daemon_socket_path", AppConfig.tui_socket_path))
    config = AppConfig(**values)
    _validate(config, config_path)
    return config
