import configparser
import contextlib
import hashlib
import ipaddress
import logging
import math
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, List, Optional, Tuple
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

logger = logging.getLogger("isis_monitor.config")

# The three beam targets, by the one name used everywhere: config, state,
# cards, push topics and the Teams channel.
BEAM_NAMES = ("TS1", "TS2", "Muons")
_BEAM_ALIASES = {"ts1": "TS1", "ts2": "TS2", "muon": "Muons", "muons": "Muons"}
BEAM_SECTION_PREFIX = "BEAM:"

# Teams payload channel for an instrument's run cards: "experiment" sends the
# experiment NotificationChannel's name ("Experiment Updates"), "instrument"
# sends the instrument's own name.
CHANNEL_MODES = ("experiment", "instrument")

INSTRUMENT_SECTION_PREFIX = "INSTRUMENT:"
INSTRUMENT_KEYS = ("notify_counts", "beam_target", "channel")  # besides the name, from the section
# [NOTIFICATIONS] keys the TUI may edit, mapped to their AppConfig field.
EDITABLE_NOTIFICATION_KEYS = {
    "timezone": "notifications_timezone",
    "debounce_seconds": "debounce_seconds",
    "stall_minutes": "stall_minutes",
    "finish_warning_minutes": "finish_warning_minutes",
    "summary_time": "summary_time",
}
_EDITABLE_INSTRUMENT_KEYS = ("name", *INSTRUMENT_KEYS)
_WEEK_MINUTES = 7 * 24 * 60
# (min, max, whether min itself is excluded) for numeric settings, which also
# rejects nan and inf. Out-of-range values would busy-loop (0 intervals),
# disable timeouts (aiohttp treats 0 as none), crash (negative deque sizes,
# timedelta overflow, e.g. a huge stall_minutes at the first stall), exhaust
# memory or, for an infinite debounce, silently stop beam cards.
_BOUNDS = {
    "mcr_poll_interval": (5, 86400, False),
    "beam_reconnect_interval": (0.5, 3600, False),
    "webhook_timeout": (1, 300, False),
    "push_timeout": (0.5, 30, False),
    "sample_interval": (1, 3600, False),
    "history_maxlen": (1, 1_000, False),  # the TUI fetches this many per beam in one IPC reply
    "refresh_per_second": (1, 60, False),
    "logs_maxlen": (1, 10_000, False),
    "retention_days": (1, 365, False),
    "debounce_seconds": (0, 3600, False),
    "stall_minutes": (0, _WEEK_MINUTES, True),
    "finish_warning_minutes": (0, _WEEK_MINUTES, False),
    "tui_reconnect_initial": (0, 3600, True),
    "tui_reconnect_max": (0, 3600, True),
}
# The daemon keeps retention_days of samples per beam in memory.
MAX_HISTORY_SAMPLES = 100_000
# The name becomes part of the derived run-name PV, so it must be one PV segment.
_INSTRUMENT_NAME_RE = re.compile(r"[A-Z0-9_-]+")


def beam_name(value: str) -> Optional[str]:
    """The beam target a config value names (any case; "Muon" too), or None."""
    return _BEAM_ALIASES.get(value.strip().lower())


@dataclass(frozen=True)
class BeamTargetConfig:
    """One beam target, from a `[BEAM:<NAME>]` section."""
    name: str  # one of BEAM_NAMES
    pv: str  # its beam current, in µA
    # Power cutoffs in µA: off at or below the first; low below the second;
    # medium below the third; else high.
    boundaries: Tuple[float, float, float]


DEFAULT_BEAMS = (
    BeamTargetConfig("TS1", "AC:TS1:BEAM:CURR", (0.0, 50.0, 140.0)),
    BeamTargetConfig("TS2", "AC:TS2:BEAM:CURR", (0.0, 10.0, 30.0)),
    BeamTargetConfig("Muons", "AC:MUON:BEAM:CURR", (0.0, 2.0, 5.0)),
)
_BEAM_KEYS = ("pv", "boundaries")


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
    # The beam reported on run cards, one of BEAM_NAMES; None (unset) means
    # the instrument's target station from CS:INSTLIST (see BeamMonitor).
    beam_target: Optional[str] = None
    channel: str = "experiment"  # one of CHANNEL_MODES
    # "notify_counts = auto, N": each run's target comes from the experiment's
    # recent run totals, with notify_counts (N) used until there are enough.
    auto_counts: bool = False
    # The INI section it was read from, for error messages
    section: str = field(default="", compare=False, repr=False)
    # Its PVs' prefix: IN:<NAME>: unless CS:INSTLIST says otherwise
    pv_prefix: str = ""

    def __post_init__(self):
        self.pv_prefix = self.pv_prefix or f"IN:{self.name}:"

    @property
    def counts_pv(self) -> str:  # total µA·h collected this run
        return f"{self.pv_prefix}DAE:TOTALUAMPS"

    @property
    def run_name_pv(self) -> str:
        return f"{self.pv_prefix}DAE:WDTITLE"

    @property
    def run_state_pv(self) -> str:  # the DAE's run state, e.g. RUNNING or SETUP
        return f"{self.pv_prefix}DAE:RUNSTATE_STR"

    @property
    def run_number_pv(self) -> str:  # as text, e.g. "00127920"
        return f"{self.pv_prefix}DAE:RUNNUMBER"

    @property
    def rb_number_pv(self) -> str:  # the experiment's RB number, as text
        return f"{self.pv_prefix}DAE:_RBNUMBER"


@dataclass
class AppConfig:
    mcr_news_url: str = _ini("DATA", "")
    isis_websocket_url: str = _ini("DATA", "")
    mcr_page_url: str = _ini("DATA", "")  # optional "Open MCR news" button link

    # Blank webhook URL disables that channel's Teams notifier.
    news_teams_url: str = _ini("WEBHOOKS", "")
    beam_teams_url: str = _ini("WEBHOOKS", "")
    experiment_teams_url: str = _ini("WEBHOOKS", "")

    # Optional signed-JSON webhook for every channel (e.g. the push_site
    # submodule); a blank url disables it.
    push_url: str = _ini("PUSH", "", "url")
    push_secret_file: str = _ini("PUSH", "", "secret_file")
    push_timeout: float = _ini("PUSH", 2.0, "timeout")
    push_secret: bytes = field(default=b"", repr=False)  # set by the daemon from read_push_secret()

    # The beam_target for instruments that don't set one and that CS:INSTLIST
    # doesn't put on TS1, TS2 or MUON (or doesn't list)
    instrument_target: str = _ini("PVS", "TS1")
    # From the [BEAM:<NAME>] sections, in BEAM_NAMES order
    beams: Tuple[BeamTargetConfig, ...] = DEFAULT_BEAMS

    mcr_poll_interval: float = _ini("TIMEOUTS_INTERVALS", 60.0)
    beam_reconnect_interval: float = _ini("TIMEOUTS_INTERVALS", 5.0)
    webhook_timeout: float = _ini("TIMEOUTS_INTERVALS", 10.0)

    history_maxlen: int = _ini("TUI", 60)  # samples shown per beam target
    refresh_per_second: int = _ini("TUI", 4)
    logs_maxlen: int = _ini("TUI", 50)

    # Relative paths (these, [LOGGING] file and [PUSH] secret_file) are resolved
    # against the config file's directory. The socket and lock stay out of
    # shared /tmp, where another local user could pre-create them.
    daemon_db_path: str = _ini("DAEMON", "beam_monitor.db", "db_path")
    daemon_socket_path: str = _ini("DAEMON", "isis_beam_monitor.sock", "socket_path")
    daemon_lock_file: str = _ini("DAEMON", "isis_beam_monitor.lock", "lock_file")
    retention_days: int = _ini("DAEMON", 7)
    # Seconds between beam history samples, which are also when state is saved
    sample_interval: float = _ini("DAEMON", 60.0)

    tui_reconnect_initial: float = _ini("TUI_CLIENT", 1.0, "reconnect_initial")
    tui_reconnect_max: float = _ini("TUI_CLIENT", 15.0, "reconnect_max")

    # The daemon logs to stderr (journald under systemd), and also to this
    # rotating file if it's set.
    log_file: str = _ini("LOGGING", "", "file")
    log_level: str = _ini("LOGGING", "INFO", "level")

    notifications_timezone: str = _ini("NOTIFICATIONS", "Europe/London", "timezone")
    debounce_seconds: float = _ini("NOTIFICATIONS", 20.0)
    stall_minutes: float = _ini("NOTIFICATIONS", 15.0)
    # How far ahead of a run reaching notify_counts its "about to finish" card is sent
    finish_warning_minutes: float = _ini("NOTIFICATIONS", 15.0)
    summary_time: str = _ini("NOTIFICATIONS", "08:00")  # local HH:MM the daily summary is sent at

    # Built from the [INSTRUMENT:<NAME>] sections
    instruments: List[InstrumentConfig] = field(default_factory=list)


def _parse_boundaries(raw: str) -> Tuple[float, float, float]:
    result = tuple(float(x.strip()) for x in raw.split(","))
    if len(result) != 3:
        raise ValueError(f"must have exactly 3 comma-separated values, got {len(result)}")
    if not all(math.isfinite(x) for x in result) or list(result) != sorted(result):
        raise ValueError("must be finite and in increasing order (equal values are allowed)")
    return result


def _read_value(parser: configparser.ConfigParser, section: str, key: str, kind: type):
    if kind is str:
        return parser.get(section, key)
    return {int: parser.getint, float: parser.getfloat, bool: parser.getboolean}[kind](section, key)


def _warn_unknown(parser: configparser.ConfigParser, config_path: Path) -> None:
    """One warning naming every section and key the config doesn't use,
    e.g. typos or settings retired by an upgrade."""
    known: dict = {}
    for f in fields(AppConfig):
        if "section" in f.metadata:
            known.setdefault(f.metadata["section"], set()).add(f.metadata["key"] or f.name)
    unknown = []
    for section in parser.sections():
        upper = section.upper()
        keys = (INSTRUMENT_KEYS if upper.startswith(INSTRUMENT_SECTION_PREFIX)
                else _BEAM_KEYS if upper.startswith(BEAM_SECTION_PREFIX) else known.get(section))
        if keys is None:
            unknown.append(f"[{section}]")
            continue
        # options() also lists any [DEFAULT] keys, which aren't this section's fault.
        unknown += [f"[{section}] {key}" for key in parser.options(section)
                    if key not in keys and key not in parser.defaults()]
    if unknown:
        logger.warning(f"{config_path}: ignoring unknown setting(s): {', '.join(unknown)}")


def _read_beams(parser: configparser.ConfigParser) -> Tuple[BeamTargetConfig, ...]:
    """DEFAULT_BEAMS, with any [BEAM:<NAME>] section's settings applied."""
    beams = {b.name: b for b in DEFAULT_BEAMS}
    seen = set()
    for section in parser.sections():
        if not section.upper().startswith(BEAM_SECTION_PREFIX):
            continue
        name = beam_name(section[len(BEAM_SECTION_PREFIX):])
        if name is None:
            raise ConfigError(f"[{section}] must name a beam target: {', '.join(BEAM_NAMES)}")
        if name in seen:
            raise ConfigError(f"Beam target {name} is configured more than once")
        seen.add(name)
        changes = {}
        if pv := parser.get(section, "pv", fallback="").strip():
            changes["pv"] = pv
        if raw := parser.get(section, "boundaries", fallback="").strip():
            try:
                changes["boundaries"] = _parse_boundaries(raw)
            except ValueError as exc:
                raise ConfigError(f"[{section}] boundaries {exc}") from exc
        beams[name] = replace(beams[name], **changes)
    return tuple(beams[name] for name in BEAM_NAMES)


def _read_instruments(
    parser: configparser.ConfigParser, config: AppConfig
) -> List[InstrumentConfig]:
    """Instruments from the [INSTRUMENT:<NAME>] sections."""
    sections = [s for s in parser.sections() if s.upper().startswith(INSTRUMENT_SECTION_PREFIX)]
    if not sections:
        raise ConfigError("At least one [INSTRUMENT:<NAME>] section is required, e.g. [INSTRUMENT:PEARL]")

    instruments = []
    for section in sections:
        name = section[len(INSTRUMENT_SECTION_PREFIX):].strip().upper()
        if not name:
            raise ConfigError(f"[{section}] needs an instrument name, e.g. [INSTRUMENT:PEARL]")
        if not _INSTRUMENT_NAME_RE.fullmatch(name):
            raise ConfigError(
                f"[{section}] instrument name may only contain letters, digits, '_' and '-'"
            )
        raw_counts = parser.get(section, "notify_counts", fallback="").strip()
        if not raw_counts:
            raise ConfigError(f"[{section}] notify_counts is required")
        mode, sep, fallback = raw_counts.partition(",")
        auto_counts = mode.strip().lower() == "auto"
        if auto_counts != bool(sep):
            raise ConfigError(f"[{section}] notify_counts must be a number or 'auto, <number>'")
        try:
            notify_counts = float(fallback if auto_counts else raw_counts)
        except ValueError as exc:
            raise ConfigError(f"[{section}] notify_counts: {exc}") from exc
        raw_target = parser.get(section, "beam_target", fallback="").strip()
        beam_target = (beam_name(raw_target) or raw_target) if raw_target else None
        channel = parser.get(section, "channel", fallback="").strip().lower() or "experiment"
        instruments.append(InstrumentConfig(
            name, notify_counts, beam_target, channel=channel, auto_counts=auto_counts, section=section
        ))
    return instruments


MIN_PUSH_SECRET_BYTES = 32
MAX_PUSH_SECRET_BYTES = 4096  # the same limits as the push site, so both accept the same file


def read_push_secret(config: AppConfig) -> bytes:
    """The [PUSH] shared secret (surrounding whitespace dropped), or b"" when
    push is disabled. Only the daemon needs it, so it isn't read by load_config()."""
    if not config.push_url:
        return b""
    path = Path(config.push_secret_file)
    try:
        with path.open("rb") as f:
            secret = f.read(MAX_PUSH_SECRET_BYTES + 1).strip()
        world_readable = path.stat().st_mode & 0o004
    except OSError as exc:
        raise ConfigError(f"[PUSH] secret_file could not be read: {exc}") from exc
    if not MIN_PUSH_SECRET_BYTES <= len(secret) <= MAX_PUSH_SECRET_BYTES:
        raise ConfigError(
            f"[PUSH] secret_file must hold {MIN_PUSH_SECRET_BYTES} to {MAX_PUSH_SECRET_BYTES} characters"
        )
    if world_readable:
        logger.warning(f"[PUSH] secret_file {path} is world-readable; consider chmod o-r")
    return secret


def _check_choice(section: str, key: str, value: str, choices) -> None:
    if value not in choices:
        raise ConfigError(f"[{section}] {key} must be one of {', '.join(choices)}, got '{value}'")


def _validate_instruments(config: AppConfig) -> None:
    # Instruments' PVs aren't checked for clashes: their prefixes are only
    # known at runtime, and two instruments can't share one without sharing
    # a name, which is refused below.
    beam_pvs = {}
    for beam in config.beams:
        if beam.pv in beam_pvs:
            raise ConfigError(f"[{BEAM_SECTION_PREFIX}{beam.name}] PV {beam.pv} is already used by the "
                              f"{beam_pvs[beam.pv]} beam")
        beam_pvs[beam.pv] = beam.name
    names = set()
    for inst in config.instruments:
        section = inst.section or f"{INSTRUMENT_SECTION_PREFIX}{inst.name}"
        if inst.name in names:
            raise ConfigError(f"Instrument {inst.name} is defined more than once")
        names.add(inst.name)
        if inst.beam_target is not None:
            _check_choice(section, "beam_target", inst.beam_target, BEAM_NAMES)
        _check_choice(section, "channel", inst.channel, CHANNEL_MODES)
        # Also rejects nan and inf, which would never be reached.
        if not (math.isfinite(inst.notify_counts) and inst.notify_counts > 0):
            raise ConfigError(f"[{section}] notify_counts must be a positive number")


def _validate(config: AppConfig, config_path: Path) -> None:
    if not config.mcr_news_url:
        raise ConfigError(f"[DATA] mcr_news_url is required. Please edit config file: {config_path}")
    if not config.isis_websocket_url:
        logger.warning(
            "isis_websocket_url is empty — BeamMonitor will not run. "
            "Please set [DATA] isis_websocket_url in your config file."
        )
    _check_choice("PVS", "instrument_target", config.instrument_target, BEAM_NAMES)
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
    for name, (low, high, low_excluded) in _BOUNDS.items():
        value = getattr(config, name)
        if not (low < value if low_excluded else low <= value) or not value <= high:  # also rejects nan
            meta = next(f.metadata for f in fields(AppConfig) if f.name == name)
            limits = f"above {low} and at most {high}" if low_excluded else f"between {low} and {high}"
            raise ConfigError(f"[{meta['section']}] {meta['key'] or name} must be {limits}, got {value}")
    samples = 86400 * config.retention_days / config.sample_interval
    if samples > MAX_HISTORY_SAMPLES:
        raise ConfigError(
            f"[DAEMON] retention_days / sample_interval would keep {samples:.0f} samples per beam "
            f"in memory (at most {MAX_HISTORY_SAMPLES}); shorten retention or lengthen the interval"
        )
    if config.tui_reconnect_initial > config.tui_reconnect_max:
        raise ConfigError("[TUI_CLIENT] reconnect_initial cannot be greater than reconnect_max")
    for name in ("news_teams_url", "beam_teams_url", "experiment_teams_url"):
        # The URL is a secret (whoever has it can post), so never send it in the clear.
        if getattr(config, name) and not getattr(config, name).startswith("https://"):
            raise ConfigError(f"[WEBHOOKS] {name} must start with https://")
    if config.push_url:
        if not config.push_url.startswith(("http://", "https://")):
            raise ConfigError("[PUSH] url must start with http:// or https://")
        if not config.push_secret_file:
            raise ConfigError("[PUSH] secret_file is required when url is set")
        if config.push_url.startswith("http://") and not _is_loopback(urlsplit(config.push_url).hostname or ""):
            logger.warning("[PUSH] url is plain http:// to another host, so notifications cross the network "
                           "unencrypted (they're signed, so can't be forged); use https:// unless it's local")
    _validate_instruments(config)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_config(config_path: Path) -> AppConfig:
    return read_config(config_path)[0]


def read_config(config_path: Path) -> Tuple[AppConfig, str]:
    """The config and its revision, an identifier that changes whenever the
    file's contents do, both from one read of the file."""
    parser, revision = _read_parser(config_path)
    return parse_config(parser, config_path), revision


def _read_parser(config_path: Path) -> Tuple[configparser.ConfigParser, str]:
    try:
        data = config_path.read_bytes()
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {config_path}") from None
    except OSError as exc:
        raise ConfigError(f"Could not read {config_path}: {exc}") from exc
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(data.decode("utf-8"), str(config_path))
    except (configparser.Error, UnicodeDecodeError) as exc:  # e.g. the same section written twice
        raise ConfigError(f"Could not parse {config_path}: {exc}") from exc
    return parser, hashlib.sha256(data).hexdigest()


_PATH_FIELDS = ("daemon_db_path", "daemon_socket_path", "daemon_lock_file", "log_file", "push_secret_file")


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

    for name in _PATH_FIELDS:
        path = values.get(name, getattr(AppConfig, name))
        if path:
            values[name] = str(config_path.absolute().parent / path)  # an absolute path is kept as is
    if "instrument_target" in values:
        values["instrument_target"] = beam_name(values["instrument_target"]) or values["instrument_target"]
    _warn_unknown(parser, config_path)
    config = AppConfig(**values, beams=_read_beams(parser))
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


def _format_notify_counts(inst: InstrumentConfig) -> str:
    """An instrument's notify_counts as written in the INI file."""
    counts = _format_value(inst.notify_counts)
    return f"auto, {counts}" if inst.auto_counts else counts


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
                "notify_counts": _format_notify_counts(inst),
                "beam_target": inst.beam_target or "",
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
        for key in INSTRUMENT_KEYS:
            if inst.get(key, "").strip():
                parser.set(section, key, inst[key].strip())


def _write_atomically(parser: configparser.ConfigParser, config_path: Path) -> None:
    """Replace `config_path` with `parser`'s contents, keeping its permissions
    and a copy of the old file as <name>.bak. Readers never see a partial file."""
    target = config_path.resolve()  # write through a symlink, not over it
    shutil.copy2(target, target.with_name(target.name + ".bak"))
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
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


def update_config_file(config_path: Path, settings: Any, revision: Optional[str] = None) -> None:
    """Apply `settings` (see editable_settings) to the config file. Nothing
    is written unless the result is valid and, if `revision` is given, the
    file still matches that revision from read_config() — so an edit based
    on a stale read can't overwrite someone else's changes.

    configparser can't round-trip comments, so the rewritten file has none;
    config.ini.example documents every setting.
    """
    parser, current = _read_parser(config_path)
    if revision is not None and current != revision:
        raise ConfigChangedError(f"{config_path} has changed since it was read; reload and try again")
    _apply_settings(parser, settings)
    parse_config(parser, config_path)  # validates
    _write_atomically(parser, config_path)
