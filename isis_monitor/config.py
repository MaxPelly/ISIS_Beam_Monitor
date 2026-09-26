import configparser
import logging
import math
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import List

logger = logging.getLogger("isis_monitor.config")

# Must match the state_key values of isis_monitor.beam.BEAM_TARGETS — not
# imported directly to avoid a circular import (beam.py imports config.py).
_INSTRUMENT_TARGETS = ("TS1", "TS2", "Muon")

INSTRUMENT_SECTION_PREFIX = "INSTRUMENT:"
_INSTRUMENT_KEYS = ("counts_pv", "notify_counts", "beam_target")
# The name becomes part of the derived run-name PV, so it must be one PV segment.
_INSTRUMENT_NAME_RE = re.compile(r"[A-Z0-9_-]+")


class ConfigError(Exception):
    """Raised when the configuration file is missing or contains invalid values."""


def _ini(section: str, default, key: str = ""):
    """A config field read from `[section] key` (key defaults to the field name)."""
    return field(default=default, metadata={"section": section, "key": key})


@dataclass
class InstrumentConfig:
    """One monitored instrument, from an `[INSTRUMENT:<NAME>]` section."""
    name: str
    counts_pv: str
    notify_counts: float
    beam_target: str  # which beam target's state to report in run cards
    run_name_pv: str = ""  # derived from the name when blank
    # The INI section it was read from, for error messages
    section: str = field(default="", compare=False, repr=False)

    def __post_init__(self):
        if not self.run_name_pv:
            self.run_name_pv = f"IN:{self.name}:DAE:WDTITLE"


@dataclass
class AppConfig:
    mcr_news_url: str = _ini("DATA", "")
    isis_websocket_url: str = _ini("DATA", "")
    mcr_page_url: str = _ini("DATA", "")  # optional "Open MCR news" button link

    # Blank webhook URL disables that channel's Teams notifier.
    news_teams_url: str = _ini("WEBHOOKS", "")
    beam_teams_url: str = _ini("WEBHOOKS", "")
    experiment_teams_url: str = _ini("WEBHOOKS", "")

    # Legacy single-instrument settings, used only when there are no
    # [INSTRUMENT:<NAME>] sections. instrument_target is also the default
    # beam_target for instrument sections that don't set one.
    counts_pv: str = _ini("PVS", "IN:PEARL:CS:DASHBOARD:TAB:2:1:VALUE")
    run_name_pv: str = _ini("PVS", "IN:PEARL:DAE:WDTITLE")
    ts1_beam_current_pv: str = _ini("PVS", "AC:TS1:BEAM:CURR")
    ts2_beam_current_pv: str = _ini("PVS", "AC:TS2:BEAM:CURR")
    muon_beam_current_pv: str = _ini("PVS", "AC:MUON:BEAM:CURR")
    notify_counts: float = _ini("PVS", 130.0)
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

    # Built from the [INSTRUMENT:<NAME>] sections (or the legacy [PVS] keys)
    instruments: List[InstrumentConfig] = field(default_factory=list)


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


def _legacy_instrument_name(config: AppConfig) -> str:
    for pv in (config.counts_pv, config.run_name_pv):
        match = re.match(r"IN:([^:]+):", pv)
        if match:
            return match.group(1)
    return "INSTRUMENT"


def _read_instruments(
    parser: configparser.ConfigParser, config: AppConfig
) -> List[InstrumentConfig]:
    """Instruments from the [INSTRUMENT:<NAME>] sections, or one built from the
    legacy [PVS] keys when there are none."""
    sections = [s for s in parser.sections() if s.upper().startswith(INSTRUMENT_SECTION_PREFIX)]
    if not sections:
        return [InstrumentConfig(
            name=_legacy_instrument_name(config),
            counts_pv=config.counts_pv,
            notify_counts=config.notify_counts,
            beam_target=config.instrument_target,
            run_name_pv=config.run_name_pv,
            section="PVS",
        )]

    instruments = []
    for section in sections:
        name = section[len(INSTRUMENT_SECTION_PREFIX):].strip().upper()
        if not name:
            raise ConfigError(f"[{section}] needs an instrument name, e.g. [INSTRUMENT:PEARL]")
        if not _INSTRUMENT_NAME_RE.fullmatch(name):
            raise ConfigError(
                f"[{section}] instrument name may only contain letters, digits, '_' and '-'"
            )
        # options() also lists any [DEFAULT] keys, which aren't this section's fault.
        unknown = set(parser.options(section)) - set(_INSTRUMENT_KEYS) - set(parser.defaults())
        if unknown:
            logger.warning(f"[{section}] ignoring unknown key(s): {', '.join(sorted(unknown))}")
        counts_pv = parser.get(section, "counts_pv", fallback="").strip()
        if not counts_pv:
            raise ConfigError(f"[{section}] counts_pv is required")
        raw_counts = parser.get(section, "notify_counts", fallback="").strip()
        if not raw_counts:
            raise ConfigError(f"[{section}] notify_counts is required")
        try:
            notify_counts = float(raw_counts)
        except ValueError as exc:
            raise ConfigError(f"[{section}] notify_counts: {exc}") from exc
        beam_target = parser.get(section, "beam_target", fallback="").strip() or config.instrument_target
        instruments.append(InstrumentConfig(name, counts_pv, notify_counts, beam_target, section=section))
    return instruments


def _validate_instruments(config: AppConfig) -> None:
    names = set()
    pv_owners = {
        config.ts1_beam_current_pv: "the TS1 beam",
        config.ts2_beam_current_pv: "the TS2 beam",
        config.muon_beam_current_pv: "the Muon beam",
    }
    for inst in config.instruments:
        section = inst.section or f"{INSTRUMENT_SECTION_PREFIX}{inst.name}"
        if inst.name in names:
            raise ConfigError(f"Instrument {inst.name} is defined more than once")
        names.add(inst.name)
        if inst.beam_target not in _INSTRUMENT_TARGETS:
            raise ConfigError(
                f"[{section}] beam_target must be one of {', '.join(_INSTRUMENT_TARGETS)}, "
                f"got '{inst.beam_target}'"
            )
        # Also rejects nan and inf, which would never be reached.
        if not (math.isfinite(inst.notify_counts) and inst.notify_counts > 0):
            raise ConfigError(f"[{section}] notify_counts must be a positive number")
        # Each PV update is routed to exactly one owner, so PVs can't be shared.
        for pv in (inst.counts_pv, inst.run_name_pv):
            if pv in pv_owners:
                raise ConfigError(f"[{section}] PV {pv} is already used by {pv_owners[pv]}")
            pv_owners[pv] = f"instrument {inst.name}"


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
    _validate_instruments(config)


def load_config(config_path: Path) -> AppConfig:
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(config_path)
    except configparser.Error as exc:  # e.g. the same section written twice
        raise ConfigError(f"Could not parse {config_path}: {exc}") from exc
    return parse_config(parser, config_path)


def parse_config(parser: configparser.ConfigParser, config_path: Path) -> AppConfig:
    """Build and validate an AppConfig from an already-read parser."""
    values = {}
    for f in fields(AppConfig):
        if "section" not in f.metadata:
            continue  # not a single INI key, e.g. instruments
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
    config.instruments = _read_instruments(parser, config)
    _validate(config, config_path)
    return config
