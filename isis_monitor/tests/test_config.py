import pytest
from pathlib import Path
from isis_monitor.config import load_config, ConfigError, AppConfig


def test_load_config_success(tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text("""\
[DATA]
mcr_news_url = http://test.com/news
isis_websocket_url = wss://test.com/ws

[WEBHOOKS]
news_teams_url = http://test.teams/news
beam_teams_url = http://test.teams/beam
experiment_teams_url = http://test.teams/exp
""")
    config = load_config(config_file)
    assert config.mcr_news_url == "http://test.com/news"
    assert config.isis_websocket_url == "wss://test.com/ws"
    assert config.news_teams_url == "http://test.teams/news"
    assert config.beam_teams_url == "http://test.teams/beam"
    assert config.experiment_teams_url == "http://test.teams/exp"
    # PV defaults
    assert config.counts_pv == "IN:PEARL:CS:DASHBOARD:TAB:2:1:VALUE"
    assert config.run_name_pv == "IN:PEARL:DAE:WDTITLE"


def test_load_config_custom_pvs(tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text("""\
[DATA]
mcr_news_url = http://test.com/news
isis_websocket_url = wss://test.com/ws

[WEBHOOKS]
news_teams_url =
beam_teams_url =
experiment_teams_url =

[PVS]
counts_pv = IN:MYINST:COUNTS
run_name_pv = IN:MYINST:RUNNAME
""")
    config = load_config(config_file)
    assert config.counts_pv == "IN:MYINST:COUNTS"
    assert config.run_name_pv == "IN:MYINST:RUNNAME"


def test_load_config_missing_file():
    with pytest.raises(ConfigError, match="not found"):
        load_config(Path("non_existent_file.ini"))


def test_load_config_missing_mcr_url(tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text("""\
[DATA]
isis_websocket_url = wss://test.com/ws
""")
    with pytest.raises(ConfigError, match="mcr_news_url"):
        load_config(config_file)


def test_load_config_missing_data_section(tmp_path):
    """A config file with no [DATA] section at all should raise ConfigError."""
    config_file = tmp_path / "config.ini"
    config_file.write_text("""\
[WEBHOOKS]
news_teams_url =
beam_teams_url =
experiment_teams_url =
""")
    with pytest.raises(ConfigError, match="mcr_news_url"):
        load_config(config_file)


def test_load_config_boundary_too_few_values(tmp_path):
    """Boundary tuple with fewer than 3 values should raise ConfigError."""
    config_file = tmp_path / "config.ini"
    config_file.write_text("""\
[DATA]
mcr_news_url = http://test.com
isis_websocket_url = wss://test.com

[WEBHOOKS]
news_teams_url =
beam_teams_url =
experiment_teams_url =

[BEAM_BOUNDARIES]
ts1_boundaries = 0.0, 50.0
""")
    with pytest.raises(ConfigError, match="exactly 3"):
        load_config(config_file)


def test_load_config_boundary_too_many_values(tmp_path):
    """Boundary tuple with more than 3 values should raise ConfigError."""
    config_file = tmp_path / "config.ini"
    config_file.write_text("""\
[DATA]
mcr_news_url = http://test.com
isis_websocket_url = wss://test.com

[WEBHOOKS]
news_teams_url =
beam_teams_url =
experiment_teams_url =

[BEAM_BOUNDARIES]
ts1_boundaries = 0.0, 50.0, 140.0, 200.0
""")
    with pytest.raises(ConfigError, match="exactly 3"):
        load_config(config_file)


def test_load_config_empty_websocket_url_logs_warning(tmp_path, caplog):
    """Empty isis_websocket_url should log a WARNING, not raise."""
    import logging
    config_file = tmp_path / "config.ini"
    config_file.write_text("""\
[DATA]
mcr_news_url = http://test.com/news
isis_websocket_url =

[WEBHOOKS]
news_teams_url =
beam_teams_url =
experiment_teams_url =
""")
    with caplog.at_level(logging.WARNING, logger="isis_monitor.config"):
        config = load_config(config_file)
    assert config.isis_websocket_url == ""
    assert "isis_websocket_url" in caplog.text


def test_load_config_daemon_and_tui_client_values(tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text("""\
[DATA]
mcr_news_url = http://test.com/news
isis_websocket_url = wss://test.com/ws

[WEBHOOKS]
news_teams_url =
beam_teams_url =
experiment_teams_url =

[DAEMON]
db_path = /tmp/beam.db
socket_path = /tmp/beam.sock
lock_file = /tmp/beam.lock
retention_days = 7

[TUI_CLIENT]
socket_path = /tmp/beam.sock
reconnect_initial = 2
reconnect_max = 20
""")
    config = load_config(config_file)
    assert config.daemon_db_path == "/tmp/beam.db"
    assert config.daemon_socket_path == "/tmp/beam.sock"
    assert config.daemon_lock_file == "/tmp/beam.lock"
    assert config.retention_days == 7
    assert config.tui_socket_path == "/tmp/beam.sock"
    assert config.tui_reconnect_initial == 2
    assert config.tui_reconnect_max == 20


def test_load_config_invalid_retention_days(tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text("""\
[DATA]
mcr_news_url = http://test.com/news
isis_websocket_url = wss://test.com/ws

[WEBHOOKS]
news_teams_url =
beam_teams_url =
experiment_teams_url =

[DAEMON]
retention_days = 0
""")
    with pytest.raises(ConfigError, match="retention_days"):
        load_config(config_file)


def test_load_config_notifications_defaults(tmp_path):
    """[NOTIFICATIONS] is optional — defaults apply when the section is absent."""
    config_file = tmp_path / "config.ini"
    config_file.write_text("""\
[DATA]
mcr_news_url = http://test.com/news
isis_websocket_url = wss://test.com/ws

[WEBHOOKS]
news_teams_url =
beam_teams_url =
experiment_teams_url =
""")
    config = load_config(config_file)
    assert config.fun_mode is False
    assert config.notifications_timezone == "Europe/London"
    assert config.debounce_seconds == 20.0


def test_load_config_notifications_custom_values(tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text("""\
[DATA]
mcr_news_url = http://test.com/news
isis_websocket_url = wss://test.com/ws

[WEBHOOKS]
news_teams_url =
beam_teams_url =
experiment_teams_url =

[NOTIFICATIONS]
fun_mode = true
timezone = America/New_York
debounce_seconds = 5
""")
    config = load_config(config_file)
    assert config.fun_mode is True
    assert config.notifications_timezone == "America/New_York"
    assert config.debounce_seconds == 5.0


def test_load_config_instrument_target_defaults(tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text("""\
[DATA]
mcr_news_url = http://test.com/news
isis_websocket_url = wss://test.com/ws

[WEBHOOKS]
news_teams_url =
beam_teams_url =
experiment_teams_url =
""")
    config = load_config(config_file)
    assert config.instrument_target == "TS1"
    assert config.stall_minutes == 15.0


def test_load_config_instrument_target_custom_values(tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text("""\
[DATA]
mcr_news_url = http://test.com/news
isis_websocket_url = wss://test.com/ws

[WEBHOOKS]
news_teams_url =
beam_teams_url =
experiment_teams_url =

[PVS]
instrument_target = TS2

[NOTIFICATIONS]
stall_minutes = 10
""")
    config = load_config(config_file)
    assert config.instrument_target == "TS2"
    assert config.stall_minutes == 10.0


def test_load_config_invalid_instrument_target(tmp_path):
    """A typo like the display label "Muons" instead of the state_key "Muon"
    must be rejected at load time rather than silently degrading to
    "unknown" in every run-card fact."""
    config_file = tmp_path / "config.ini"
    config_file.write_text("""\
[DATA]
mcr_news_url = http://test.com/news
isis_websocket_url = wss://test.com/ws

[WEBHOOKS]
news_teams_url =
beam_teams_url =
experiment_teams_url =

[PVS]
instrument_target = Muons
""")
    with pytest.raises(ConfigError, match="instrument_target"):
        load_config(config_file)


def test_load_config_valid_instrument_targets(tmp_path):
    for target in ("TS1", "TS2", "Muon"):
        config_file = tmp_path / f"config_{target}.ini"
        config_file.write_text(f"""\
[DATA]
mcr_news_url = http://test.com/news
isis_websocket_url = wss://test.com/ws

[WEBHOOKS]
news_teams_url =
beam_teams_url =
experiment_teams_url =

[PVS]
instrument_target = {target}
""")
        config = load_config(config_file)
        assert config.instrument_target == target


def test_load_config_mcr_page_url_and_summary_time_defaults(tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text("""\
[DATA]
mcr_news_url = http://test.com/news
isis_websocket_url = wss://test.com/ws

[WEBHOOKS]
news_teams_url =
beam_teams_url =
experiment_teams_url =
""")
    config = load_config(config_file)
    assert config.mcr_page_url == ""
    assert config.summary_time == "08:00"


def test_load_config_mcr_page_url_and_summary_time_custom(tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text("""\
[DATA]
mcr_news_url = http://test.com/news
isis_websocket_url = wss://test.com/ws
mcr_page_url = https://example.com/mcr

[WEBHOOKS]
news_teams_url =
beam_teams_url =
experiment_teams_url =

[NOTIFICATIONS]
summary_time = 07:30
""")
    config = load_config(config_file)
    assert config.mcr_page_url == "https://example.com/mcr"
    assert config.summary_time == "07:30"


def test_load_config_invalid_summary_time(tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text("""\
[DATA]
mcr_news_url = http://test.com/news
isis_websocket_url = wss://test.com/ws

[WEBHOOKS]
news_teams_url =
beam_teams_url =
experiment_teams_url =

[NOTIFICATIONS]
summary_time = not-a-time
""")
    with pytest.raises(ConfigError, match="summary_time"):
        load_config(config_file)


def _write(tmp_path, extra: str) -> Path:
    config_file = tmp_path / "config.ini"
    config_file.write_text("[DATA]\nmcr_news_url = http://test.com/news\n" + extra)
    return config_file


def test_load_config_defaults_match_dataclass_defaults(tmp_path):
    """The loader's fallbacks and AppConfig's defaults come from one place."""
    config = load_config(_write(tmp_path, ""))
    assert config == AppConfig(mcr_news_url="http://test.com/news")
    assert config.mcr_poll_interval == 60.0


@pytest.mark.parametrize("section, line", [
    ("TIMEOUTS_INTERVALS", "mcr_poll_interval = soon"),
    ("DAEMON", "retention_days = 1.5"),
    ("LOGGING", "log_max_bytes = big"),
    ("NOTIFICATIONS", "fun_mode = maybe"),
    ("BEAM_BOUNDARIES", "ts1_boundaries = 0, a, 2"),
])
def test_load_config_invalid_value_raises_config_error(tmp_path, section, line):
    key = line.split(" = ")[0]
    with pytest.raises(ConfigError, match=rf"\[{section}\] {key}"):
        load_config(_write(tmp_path, f"[{section}]\n{line}\n"))


def test_load_config_blank_numeric_value_uses_default(tmp_path):
    config = load_config(_write(tmp_path, "[DAEMON]\nretention_days =\n[TUI]\nhistory_maxlen =\n"))
    assert config.retention_days == 7
    assert config.history_maxlen == 60


def test_load_config_tui_socket_defaults_to_daemon_socket(tmp_path):
    config = load_config(_write(tmp_path, "[DAEMON]\nsocket_path = /run/beam.sock\n"))
    assert config.tui_socket_path == "/run/beam.sock"


def test_load_config_boolean_and_renamed_keys(tmp_path):
    config = load_config(_write(
        tmp_path, "[NOTIFICATIONS]\nfun_mode = yes\ntimezone = UTC\n"
    ))
    assert config.fun_mode is True
    assert config.notifications_timezone == "UTC"


@pytest.mark.parametrize("initial, maximum, match", [
    ("0", "5", "positive"),
    ("10", "5", "cannot be greater"),
])
def test_load_config_invalid_tui_reconnect(tmp_path, initial, maximum, match):
    with pytest.raises(ConfigError, match=match):
        load_config(_write(
            tmp_path, f"[TUI_CLIENT]\nreconnect_initial = {initial}\nreconnect_max = {maximum}\n"
        ))


def test_load_config_custom_boundaries(tmp_path):
    config = load_config(_write(tmp_path, "[BEAM_BOUNDARIES]\nts2_boundaries = 1, 2.5 , 3\n"))
    assert config.ts2_boundaries == (1.0, 2.5, 3.0)
    assert config.ts1_boundaries == (0.0, 50.0, 140.0)


def test_load_config_summary_time_out_of_range(tmp_path):
    with pytest.raises(ConfigError, match="summary_time"):
        load_config(_write(tmp_path, "[NOTIFICATIONS]\nsummary_time = 25:00\n"))
