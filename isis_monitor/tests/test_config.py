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
heartbeat_interval = 15

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
    assert config.heartbeat_interval == 15
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
