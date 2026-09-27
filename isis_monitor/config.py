import configparser
import contextlib
import hashlib
import logging
import math
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("isis_monitor.config")

# Must match the state_key values of isis_monitor.beam.BEAM_TARGETS — not
# imported directly to avoid a circular import (beam.py imports config.py).
BEAM_TARGET_KEYS = ("TS1", "TS2", "Muon")

# Teams payload channel for an instrument's run cards: "experiment" sends the
# experiment NotificationChannel's name ("Experiment Updates"), "instrument"
# sends the instrument's own name.
CHANNEL_MODES = ("experiment", "instrument")

INSTRUMENT_SECTION_PREFIX = "INSTRUMENT:"
_INSTRUMENT_KEYS = ("notify_counts", "beam_target", "channel")
# Keys no longer used in instrument sections, with why, for the load-time warning.
_RETIRED_INSTRUMENT_KEYS = {"counts_pv": "progress is now read from IN:<NAME>:DAE:TOTALUAMPS"}
# [NOTIFICATIONS] keys the TUI may edit, mapped to their AppConfig field.
EDITABLE_NOTIFICATION_KEYS = {
    "fun_mode": "fun_mode",
    "timezone": "notifications_timezone",
    "debounce_seconds": "debounce_seconds",
    "stall_minutes": "stall_minutes",
    "finish_warning_minutes": "finish_warning_minutes",
    "summary_time": "summary_time",
}
_EDITABLE_INSTRUMENT_KEYS = ("name", *_INSTRUMENT_KEYS)
MAX_DEBOUNCE_SECONDS = 3600
MAX_STALL_MINUTES = 7 * 24 * 60
# (min, max) for numeric settings only edited by hand. Out-of-range values
# would busy-loop (0 intervals), disable timeouts (aiohttp treats 0 as none),
# crash (negative deque sizes, timedelta overflow) or exhaust memory.
_BOUNDS = {
    "mcr_poll_interval": (5, 86400),
    "beam_reconnect_interval": (0.5, 3600),
    "webhook_timeout": (1, 300),
    "sample_interval": (1, 3600),
    "history_maxlen": (1, 100_000),
    "refresh_per_second": (1, 60),
    "logs_maxlen": (1, 10_000),
    "retention_days": (1, 365),
    "log_max_bytes": (0, 1_000_000_000),
    "log_backup_count": (0, 100),
}
# The daemon keeps retention_days of samples per beam in memory.
MAX_HISTORY_SAMPLES = 100_000
# The name becomes part of the derived run-name PV, so it must be one PV segment.
_INSTRUMENT_NAME_RE = re.compile(r"[A-Z0-9_-]+")


class ConfigError(Exception):
    """Raised when the configuration file is missing or contains invalid values."""


class ConfigChangedError(ConfigError):
    """Raised when the config file changed since the revision an edit was based on."""


def _ini(section: str, default, key: str = ""):
    """A config field read from `[section] key` (key defaults to the field name)."""
    return field(default=default, metadata={"section": section, "key": key})


@dataclass
class InstrumentConfig:
    """One monitored instrument, from an `[INSTRUMENT:<NAME>]` section."""
    name: str
    notify_counts: float  # total µA·h collected at which "run about to finish" is sent
    beam_target: str  # which beam target's state to report in run cards
    counts_pv: str = ""  # total µA·h collected this run; derived from the name when blank
    run_name_pv: str = ""  # derived from the name when blank
    channel: str = "experiment"  # one of CHANNEL_MODES
    # The INI section it was read from, for error messages
    section: str = field(default="", compare=False, repr=False)

    def __post_init__(self):
        if not self.counts_pv:
            self.counts_pv = f"IN:{self.name}:DAE:TOTALUAMPS"
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
    # [INSTRUMENT:<NAME>] sections (counts_pv now only supplies the name).
    # instrument_target is also the default beam_target for instrument
    # sections that don't set one.
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
    # How far ahead of a run reaching notify_counts its "about to finish" card is sent
    finish_warning_minutes: float = _ini("NOTIFICATIONS", 15.0)
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
        for key in sorted(unknown & set(_RETIRED_INSTRUMENT_KEYS)):
            logger.warning(f"[{section}] ignoring {key}: {_RETIRED_INSTRUMENT_KEYS[key]}")
        unknown -= set(_RETIRED_INSTRUMENT_KEYS)
        if unknown:
            logger.warning(f"[{section}] ignoring unknown key(s): {', '.join(sorted(unknown))}")
        raw_counts = parser.get(section, "notify_counts", fallback="").strip()
        if not raw_counts:
            raise ConfigError(f"[{section}] notify_counts is required")
        try:
            notify_counts = float(raw_counts)
        except ValueError as exc:
            raise ConfigError(f"[{section}] notify_counts: {exc}") from exc
        beam_target = parser.get(section, "beam_target", fallback="").strip() or config.instrument_target
        channel = parser.get(section, "channel", fallback="").strip().lower() or "experiment"
        instruments.append(InstrumentConfig(name, notify_counts, beam_target, channel=channel, section=section))
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
        if inst.beam_target not in BEAM_TARGET_KEYS:
            raise ConfigError(
                f"[{section}] beam_target must be one of {', '.join(BEAM_TARGET_KEYS)}, "
                f"got '{inst.beam_target}'"
            )
        if inst.channel not in CHANNEL_MODES:
            raise ConfigError(
                f"[{section}] channel must be one of {', '.join(CHANNEL_MODES)}, got '{inst.channel}'"
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
    if config.instrument_target not in BEAM_TARGET_KEYS:
        raise ConfigError(
            f"[PVS] instrument_target must be one of {', '.join(BEAM_TARGET_KEYS)}, "
            f"got '{config.instrument_target}'"
        )
    try:
        hour, minute = (int(x) for x in config.summary_time.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        raise ConfigError(f"[NOTIFICATIONS] summary_time must be 'HH:MM', got '{config.summary_time}'")
    try:
        ZoneInfo(config.notifications_timezone)
    except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
        raise ConfigError(f"[NOTIFICATIONS] timezone '{config.notifications_timezone}' is not a known timezone")
    # Bounded (which also rejects nan/inf): an infinite debounce silently stops
    # beam cards, and a huge stall_minutes overflows timedelta at the first stall.
    if not 0 <= config.debounce_seconds <= MAX_DEBOUNCE_SECONDS:
        raise ConfigError(f"[NOTIFICATIONS] debounce_seconds must be between 0 and {MAX_DEBOUNCE_SECONDS}")
    if not 0 < config.stall_minutes <= MAX_STALL_MINUTES:
        raise ConfigError(f"[NOTIFICATIONS] stall_minutes must be above 0 and at most {MAX_STALL_MINUTES}")
    if not 0 <= config.finish_warning_minutes <= MAX_STALL_MINUTES:
        raise ConfigError(f"[NOTIFICATIONS] finish_warning_minutes must be between 0 and {MAX_STALL_MINUTES}")
    for name, (low, high) in _BOUNDS.items():
        value = getattr(config, name)
        if not low <= value <= high:  # also rejects nan
            meta = next(f.metadata for f in fields(AppConfig) if f.name == name)
            raise ConfigError(
                f"[{meta['section']}] {meta['key'] or name} must be between {low} and {high}, got {value}"
            )
    samples = 86400 * config.retention_days / config.sample_interval
    if samples > MAX_HISTORY_SAMPLES:
        raise ConfigError(
            f"[DAEMON] retention_days / [TUI] sample_interval would keep {samples:.0f} samples per beam "
            f"in memory (at most {MAX_HISTORY_SAMPLES}); shorten retention or lengthen the interval"
        )
    if config.tui_reconnect_initial <= 0 or config.tui_reconnect_max <= 0:
        raise ConfigError("[TUI_CLIENT] reconnect values must be positive")
    if config.tui_reconnect_initial > config.tui_reconnect_max:
        raise ConfigError("[TUI_CLIENT] reconnect_initial cannot be greater than reconnect_max")
    _validate_instruments(config)


def load_config(config_path: Path) -> AppConfig:
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    return parse_config(_read_parser(config_path), config_path)


def _read_parser(config_path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(config_path)
    except configparser.Error as exc:  # e.g. the same section written twice
        raise ConfigError(f"Could not parse {config_path}: {exc}") from exc
    return parser


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


def _format_value(value: Any) -> str:
    """Render a setting as it would be written in the INI file."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def editable_settings(config: AppConfig) -> dict:
    """The settings the TUI may edit, as INI strings:
    {"notifications": {key: value}, "instruments": [{name, notify_counts, beam_target, channel}]}."""
    return {
        "notifications": {
            key: _format_value(getattr(config, attr)) for key, attr in EDITABLE_NOTIFICATION_KEYS.items()
        },
        "instruments": [
            {
                "name": inst.name,
                "notify_counts": _format_value(inst.notify_counts),
                "beam_target": inst.beam_target,
                "channel": inst.channel,
            }
            for inst in config.instruments
        ],
    }


def _check_string_map(value: Any, allowed: tuple, what: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{what} must be an object")
    for key, item in value.items():
        if key not in allowed:
            raise ConfigError(f"{what}: '{key}' can't be edited")
        if not isinstance(item, str) or "\n" in item or "\r" in item:
            raise ConfigError(f"{what}: '{key}' must be a single-line string")
    return value


def _apply_settings(parser: configparser.ConfigParser, settings: Any) -> None:
    """Apply editable_settings()-shaped `settings` to `parser`. Either part may
    be left out; "instruments", when given, replaces every instrument section."""
    if not isinstance(settings, dict):
        raise ConfigError("settings must be an object")
    notifications = _check_string_map(
        settings.get("notifications", {}), tuple(EDITABLE_NOTIFICATION_KEYS), "notifications"
    )
    instruments = settings.get("instruments")
    if instruments is not None:
        if not isinstance(instruments, list) or not instruments:
            raise ConfigError("instruments must be a non-empty list")
        for inst in instruments:
            _check_string_map(inst, _EDITABLE_INSTRUMENT_KEYS, "instrument")

    if notifications and not parser.has_section("NOTIFICATIONS"):
        parser.add_section("NOTIFICATIONS")
    for key, value in notifications.items():
        parser.set("NOTIFICATIONS", key, value)

    if instruments is None:
        return
    for section in parser.sections():
        if section.upper().startswith(INSTRUMENT_SECTION_PREFIX):
            parser.remove_section(section)
    for inst in instruments:
        # The name is checked by parse_config, along with everything else.
        section = f"{INSTRUMENT_SECTION_PREFIX}{inst.get('name', '').strip().upper()}"
        if parser.has_section(section):
            raise ConfigError(f"Instrument {inst['name'].strip().upper()} is defined more than once")
        parser.add_section(section)
        for key in _INSTRUMENT_KEYS:
            if inst.get(key, "").strip():
                parser.set(section, key, inst[key].strip())


def _write_atomically(parser: configparser.ConfigParser, config_path: Path) -> None:
    """Replace `config_path` with `parser`'s contents, keeping its permissions
    and a copy of the old file as <name>.bak. Readers never see a partial file."""
    target = config_path.resolve()  # write through a symlink, not over it
    shutil.copy2(target, target.with_name(target.name + ".bak"))
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            parser.write(fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, stat.S_IMODE(target.stat().st_mode))
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    # Make the rename itself durable across a power cut. The new file is
    # already in place, so a failure here mustn't be reported as a failed write.
    try:
        dir_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        logger.warning(f"Could not fsync {target.parent} after writing {target.name}: {exc}")


def config_revision(config_path: Path) -> str:
    """An identifier that changes whenever the config file's contents do."""
    return hashlib.sha256(config_path.read_bytes()).hexdigest()


def update_config_file(config_path: Path, settings: Any, revision: Optional[str] = None) -> AppConfig:
    """Apply `settings` (see editable_settings) to the config file and return
    the new config. Nothing is written unless the result is valid and, if
    `revision` is given, the file still matches that config_revision() — so
    an edit based on a stale read can't overwrite someone else's changes.

    configparser can't round-trip comments, so the rewritten file has none;
    config.ini.example documents every setting.
    """
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")
    parser = _read_parser(config_path)
    _apply_settings(parser, settings)
    config = parse_config(parser, config_path)
    if revision is not None and config_revision(config_path) != revision:
        raise ConfigChangedError(f"{config_path} has changed since it was read; reload and try again")
    _write_atomically(parser, config_path)
    return config
