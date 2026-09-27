import os
import pytest
from pathlib import Path
from dataclasses import replace

from unittest.mock import patch

from isis_monitor.config import (
    load_config, ConfigError, ConfigChangedError, AppConfig, InstrumentConfig, config_revision,
    editable_settings, update_config_file,
)


def _write(tmp_path, extra: str) -> Path:
    """A config with mcr_news_url, `extra`, and a PEARL instrument unless `extra` has instruments."""
    if "[INSTRUMENT:" not in extra:
        extra += "\n[INSTRUMENT:PEARL]\nnotify_counts = 130\n"
    config_file = tmp_path / "config.ini"
    config_file.write_text("[DATA]\nmcr_news_url = http://test.com/news\n" + extra)
    return config_file


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

[INSTRUMENT:PEARL]
notify_counts = 130
""")
    config = load_config(config_file)
    assert config.mcr_news_url == "http://test.com/news"
    assert config.isis_websocket_url == "wss://test.com/ws"
    assert config.news_teams_url == "http://test.teams/news"
    assert config.beam_teams_url == "http://test.teams/beam"
    assert config.experiment_teams_url == "http://test.teams/exp"
    assert config.instruments == [InstrumentConfig("PEARL", 130.0, "TS1")]


def test_load_config_missing_file():
    with pytest.raises(ConfigError, match="not found"):
        load_config(Path("non_existent_file.ini"))


def test_load_config_missing_mcr_url(tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text("[DATA]\nisis_websocket_url = wss://test.com/ws\n[INSTRUMENT:PEARL]\nnotify_counts = 1\n")
    with pytest.raises(ConfigError, match="mcr_news_url"):
        load_config(config_file)


def test_load_config_requires_an_instrument_section(tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text("[DATA]\nmcr_news_url = http://x\n[PVS]\ninstrument_target = TS2\n")
    with pytest.raises(ConfigError, match=r"At least one \[INSTRUMENT:<NAME>\] section is required"):
        load_config(config_file)


def test_load_config_empty_websocket_url_logs_warning(tmp_path, caplog):
    """Empty isis_websocket_url should log a WARNING, not raise."""
    import logging
    with caplog.at_level(logging.WARNING, logger="isis_monitor.config"):
        config = load_config(_write(tmp_path, "isis_websocket_url =\n"))
    assert config.isis_websocket_url == ""
    assert "isis_websocket_url" in caplog.text


def test_load_config_daemon_and_tui_client_values(tmp_path):
    config = load_config(_write(tmp_path, """\
[DAEMON]
db_path = /tmp/beam.db
socket_path = /tmp/beam.sock
lock_file = /tmp/beam.lock
retention_days = 7

[TUI_CLIENT]
socket_path = /tmp/beam.sock
reconnect_initial = 2
reconnect_max = 20
"""))
    assert config.daemon_db_path == "/tmp/beam.db"
    assert config.daemon_socket_path == "/tmp/beam.sock"
    assert config.daemon_lock_file == "/tmp/beam.lock"
    assert config.retention_days == 7
    assert config.tui_socket_path == "/tmp/beam.sock"
    assert config.tui_reconnect_initial == 2
    assert config.tui_reconnect_max == 20


def test_load_config_notifications_custom_values(tmp_path):
    config = load_config(_write(tmp_path, """\
mcr_page_url = https://example.com/mcr

[NOTIFICATIONS]
fun_mode = yes
timezone = America/New_York
debounce_seconds = 5
stall_minutes = 10
finish_warning_minutes = 30
summary_time = 07:30
"""))
    assert config.fun_mode is True
    assert config.notifications_timezone == "America/New_York"
    assert config.debounce_seconds == 5.0
    assert config.stall_minutes == 10.0
    assert config.finish_warning_minutes == 30.0
    assert config.summary_time == "07:30"
    assert config.mcr_page_url == "https://example.com/mcr"


def test_load_config_instrument_target_must_be_a_state_key(tmp_path):
    """A typo like the display label "Muons" instead of the state_key "Muon"
    must be rejected at load time rather than silently degrading to
    "unknown" in every run-card fact."""
    for target in ("TS1", "TS2", "Muon"):
        config = load_config(_write(tmp_path, f"[PVS]\ninstrument_target = {target}\n"))
        assert config.instrument_target == target
    with pytest.raises(ConfigError, match="instrument_target"):
        load_config(_write(tmp_path, "[PVS]\ninstrument_target = Muons\n"))


def test_load_config_defaults_match_dataclass_defaults(tmp_path):
    """The loader's fallbacks and AppConfig's defaults come from one place."""
    config = load_config(_write(tmp_path, ""))
    assert replace(config, instruments=[]) == AppConfig(mcr_news_url="http://test.com/news")
    assert config.mcr_poll_interval == 60.0
    assert (config.fun_mode, config.notifications_timezone, config.debounce_seconds) == (
        False, "Europe/London", 20.0)
    assert (config.instrument_target, config.stall_minutes, config.finish_warning_minutes) == ("TS1", 15.0, 15.0)
    assert (config.mcr_page_url, config.summary_time) == ("", "08:00")
    # With no [PVS] or [INSTRUMENT:*] sections the one instrument is PEARL.
    assert [(i.name, i.notify_counts) for i in config.instruments] == [("PEARL", 130.0)]


NOTIF, PEARL = "[NOTIFICATIONS]\n", "[INSTRUMENT:PEARL]\n"


@pytest.mark.parametrize("extra, match", [
    # Malformed values name the section and key.
    ("[TIMEOUTS_INTERVALS]\nmcr_poll_interval = soon\n", r"\[TIMEOUTS_INTERVALS\] mcr_poll_interval"),
    ("[DAEMON]\nretention_days = 1.5\n", r"\[DAEMON\] retention_days"),
    ("[LOGGING]\nlog_max_bytes = big\n", r"\[LOGGING\] log_max_bytes"),
    (NOTIF + "fun_mode = maybe\n", r"\[NOTIFICATIONS\] fun_mode"),
    ("[BEAM_BOUNDARIES]\nts1_boundaries = 0, a, 2\n", r"\[BEAM_BOUNDARIES\] ts1_boundaries"),
    ("[BEAM_BOUNDARIES]\nts1_boundaries = 0.0, 50.0\n", "exactly 3"),
    ("[INSTRUMENT:WISH]\n[INSTRUMENT:WISH]\n", "Could not parse"),  # duplicate section
    # Out-of-range numbers
    ("[TUI_CLIENT]\nreconnect_initial = 0\nreconnect_max = 5\n", "positive"),
    ("[TUI_CLIENT]\nreconnect_initial = 10\nreconnect_max = 5\n", "cannot be greater"),
    ("[TIMEOUTS_INTERVALS]\nmcr_poll_interval = 0\n", r"\[TIMEOUTS_INTERVALS\] mcr_poll_interval must be between 5"),
    ("[TIMEOUTS_INTERVALS]\nbeam_reconnect_interval = -1\n", "beam_reconnect_interval must be between"),
    ("[TIMEOUTS_INTERVALS]\nwebhook_timeout = 0\n", "webhook_timeout must be between 1 and 300"),
    ("[TUI]\nsample_interval = nan\n", "sample_interval must be between"),
    ("[TUI]\nhistory_maxlen = -5\n", "history_maxlen must be between"),
    ("[TUI]\nlogs_maxlen = 0\n", "logs_maxlen must be between"),
    ("[DAEMON]\nretention_days = 100000000\n", "retention_days must be between"),
    ("[LOGGING]\nlog_backup_count = -1\n", "log_backup_count must be between"),
    ("[DAEMON]\nretention_days = 30\n[TUI]\nsample_interval = 1\n", "samples per beam"),
    # [INSTRUMENT:*] sections
    ("[INSTRUMENT:]\n", "needs an instrument name"),
    ("[INSTRUMENT:PE ARL]\nnotify_counts = 5\n", "may only contain"),
    ("[INSTRUMENT:A:B]\nnotify_counts = 5\n", "may only contain"),
    (PEARL + "notify_counts = 0\n", r"^\[INSTRUMENT:PEARL\] notify_counts must be a positive number"),
    (PEARL + "notify_counts = nan\n", "must be a positive number"),
    (PEARL + "notify_counts = inf\n", "must be a positive number"),
    (PEARL, "notify_counts is required"),
    (PEARL + "notify_counts = lots\n", r"\[INSTRUMENT:PEARL\] notify_counts"),
    (PEARL + "notify_counts = 5\nbeam_target = Muons\n", "beam_target must be one of"),
    (PEARL + "notify_counts = 5\nchannel = teams\n",
     r"\[INSTRUMENT:PEARL\] channel must be one of experiment, instrument"),
    (PEARL + "notify_counts = 5\n[INSTRUMENT:pearl]\nnotify_counts = 5\n", "defined more than once"),
    ("[PVS]\nts1_beam_current_pv = IN:PEARL:DAE:TOTALUAMPS\n" + PEARL + "notify_counts = 5\n",
     "already used by the TS1 beam"),
    ("[PVS]\nts2_beam_current_pv = IN:PEARL:DAE:WDTITLE\n" + PEARL + "notify_counts = 5\n",
     r"^\[INSTRUMENT:PEARL\] PV IN:PEARL:DAE:WDTITLE is already used by the TS2 beam"),
    # [NOTIFICATIONS], caught at load time so a bad edit can't leave the daemon failing on restart
    (NOTIF + "timezone = Nowhere/City\n", "not a known timezone"),
    (NOTIF + "timezone = ../etc\n", "not a known timezone"),
    (NOTIF + "debounce_seconds = -1\n", "debounce_seconds must be between"),
    (NOTIF + "debounce_seconds = nan\n", "debounce_seconds must be between"),
    (NOTIF + "debounce_seconds = inf\n", "debounce_seconds must be between"),
    (NOTIF + "stall_minutes = 0\n", "stall_minutes must be above 0"),
    (NOTIF + "stall_minutes = 1e20\n", "stall_minutes must be above 0"),
    (NOTIF + "stall_minutes = nan\n", "stall_minutes must be above 0"),
    (NOTIF + "finish_warning_minutes = -1\n", "finish_warning_minutes must be between"),
    (NOTIF + "finish_warning_minutes = nan\n", "finish_warning_minutes must be between"),
    (NOTIF + "summary_time = not-a-time\n", "summary_time"),
    (NOTIF + "summary_time = 25:00\n", "summary_time"),
])
def test_invalid_config_raises_config_error(tmp_path, extra, match):
    with pytest.raises(ConfigError, match=match):
        load_config(_write(tmp_path, extra))


def test_load_config_blank_numeric_value_uses_default(tmp_path):
    config = load_config(_write(tmp_path, "[DAEMON]\nretention_days =\n[TUI]\nhistory_maxlen =\n"))
    assert config.retention_days == 7
    assert config.history_maxlen == 60


def test_load_config_tui_socket_defaults_to_daemon_socket(tmp_path):
    config = load_config(_write(tmp_path, "[DAEMON]\nsocket_path = /run/beam.sock\n"))
    assert config.tui_socket_path == "/run/beam.sock"


def test_load_config_custom_boundaries(tmp_path):
    config = load_config(_write(tmp_path, "[BEAM_BOUNDARIES]\nts2_boundaries = 1, 2.5 , 3\n"))
    assert config.ts2_boundaries == (1.0, 2.5, 3.0)
    assert config.ts1_boundaries == (0.0, 50.0, 140.0)


def test_instrument_sections(tmp_path):
    """Section names are case-insensitive and instrument names upper-cased."""
    config = load_config(_write(tmp_path, """\
[PVS]
instrument_target = TS2
[INSTRUMENT:PEARL]
notify_counts = 200
beam_target = TS1
[instrument:wish]
notify_counts = 75
"""))
    assert config.instruments == [
        InstrumentConfig("PEARL", 200.0, "TS1"),
        InstrumentConfig("WISH", 75.0, "TS2"),
    ]
    assert config.instruments[1].run_name_pv == "IN:WISH:DAE:WDTITLE"


def test_instrument_unknown_key_warns(tmp_path, caplog):
    load_config(_write(tmp_path, "[INSTRUMENT:PEARL]\nnotify_counts = 5\nteams_url = http://x\n"))
    assert "ignoring unknown key(s): teams_url" in caplog.text


def test_instrument_default_section_keys_not_reported_as_unknown(tmp_path, caplog):
    load_config(_write(tmp_path, "[DEFAULT]\nfoo = 1\n[INSTRUMENT:PEARL]\nnotify_counts = 5\n"))
    assert "unknown key" not in caplog.text


# ---------------------------------------------------------------------------
# editable_settings / update_config_file
# ---------------------------------------------------------------------------

EDITABLE_BASE = """\
[DATA]
mcr_news_url = http://test.com/news
# a comment that won't survive a rewrite
[WEBHOOKS]
beam_teams_url = http://secret
[NOTIFICATIONS]
stall_minutes = 10
[INSTRUMENT:PEARL]
notify_counts = 130
"""


def _editable_file(tmp_path, text=EDITABLE_BASE) -> Path:
    path = tmp_path / "config.ini"
    path.write_text(text)
    path.chmod(0o600)
    return path


def test_editable_settings_are_ini_strings(tmp_path):
    config = load_config(_editable_file(tmp_path))
    assert editable_settings(config) == {
        "notifications": {
            "fun_mode": "false", "timezone": "Europe/London", "debounce_seconds": "20",
            "stall_minutes": "10", "finish_warning_minutes": "15", "summary_time": "08:00",
        },
        "instruments": [{"name": "PEARL", "notify_counts": "130", "beam_target": "TS1", "channel": "experiment"}],
    }


def test_editable_settings_keep_full_precision():
    config = AppConfig(debounce_seconds=2.5, instruments=[InstrumentConfig("X", 1234567.0, "TS1")])
    settings = editable_settings(config)
    assert settings["notifications"]["debounce_seconds"] == "2.5"
    assert settings["instruments"][0]["notify_counts"] == "1234567"


def test_update_config_file_round_trips_and_keeps_other_settings(tmp_path):
    path = _editable_file(tmp_path)
    settings = editable_settings(load_config(path))
    settings["notifications"]["fun_mode"] = "true"
    settings["instruments"][0]["notify_counts"] = "200"
    settings["instruments"][0]["channel"] = "instrument"
    settings["instruments"].append({"name": "wish", "notify_counts": "50", "beam_target": "TS2"})

    returned = update_config_file(path, settings)

    reloaded = load_config(path)
    assert reloaded == returned
    assert reloaded.fun_mode is True and reloaded.stall_minutes == 10.0
    assert reloaded.beam_teams_url == "http://secret"
    assert [(i.name, i.notify_counts, i.beam_target, i.channel) for i in reloaded.instruments] == [
        ("PEARL", 200.0, "TS1", "instrument"), ("WISH", 50.0, "TS2", "experiment"),
    ]
    assert "channel = instrument" in path.read_text()
    assert "a comment" not in path.read_text()  # documented limitation
    assert (tmp_path / "config.ini.bak").read_text() == EDITABLE_BASE
    assert path.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_update_config_file_removes_dropped_instruments(tmp_path):
    path = _editable_file(tmp_path, EDITABLE_BASE + "[Instrument:WISH]\nnotify_counts = 5\n")
    update_config_file(path, {"instruments": [{"name": "WISH", "notify_counts": "5"}]})
    assert [i.name for i in load_config(path).instruments] == ["WISH"]


def test_update_config_file_only_notifications_leaves_instruments(tmp_path):
    path = _editable_file(tmp_path)
    update_config_file(path, {"notifications": {"summary_time": "09:30"}})
    config = load_config(path)
    assert config.summary_time == "09:30"
    assert [i.name for i in config.instruments] == ["PEARL"]


def test_update_config_file_writes_through_symlink(tmp_path):
    real = _editable_file(tmp_path)
    link = tmp_path / "link.ini"
    link.symlink_to(real)
    update_config_file(link, {"notifications": {"fun_mode": "true"}})
    assert link.is_symlink()
    assert load_config(real).fun_mode is True


@pytest.mark.parametrize("settings, match", [
    ([], "settings must be an object"),
    ({"notifications": []}, "notifications must be an object"),
    ({"notifications": {"log_level": "DEBUG"}}, "'log_level' can't be edited"),
    ({"notifications": {"fun_mode": True}}, "must be a single-line string"),
    ({"notifications": {"summary_time": "08:00\n[DATA]"}}, "must be a single-line string"),
    ({"notifications": {"timezone": "Mars/Base"}}, "not a known timezone"),
    ({"instruments": []}, "non-empty list"),
    ({"instruments": ["PEARL"]}, "instrument must be an object"),
    ({"instruments": [{"name": "PEARL", "teams_url": "x"}]}, "'teams_url' can't be edited"),
    ({"instruments": [{"notify_counts": "5"}]}, "needs an instrument name"),
    ({"instruments": [{"name": "PEARL", "notify_counts": "0"}]}, "must be a positive number"),
    ({"instruments": [{"name": "X", "notify_counts": "5"},
                      {"name": "x", "notify_counts": "5"}]}, "defined more than once"),
])
def test_update_config_file_rejects_invalid_settings_without_writing(tmp_path, settings, match):
    path = _editable_file(tmp_path)
    with pytest.raises(ConfigError, match=match):
        update_config_file(path, settings)
    assert path.read_text() == EDITABLE_BASE
    assert not (tmp_path / "config.ini.bak").exists()


def test_update_config_file_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        update_config_file(tmp_path / "nope.ini", {})


def test_update_config_file_cleans_up_temp_file_on_write_failure(tmp_path):
    path = _editable_file(tmp_path)
    with patch("isis_monitor.config.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            update_config_file(path, {"notifications": {"fun_mode": "true"}})
    assert path.read_text() == EDITABLE_BASE
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_update_config_file_checks_revision(tmp_path):
    path = _editable_file(tmp_path)
    revision = config_revision(path)
    update_config_file(path, {"notifications": {"fun_mode": "true"}}, revision)
    assert config_revision(path) != revision

    with pytest.raises(ConfigChangedError, match="has changed since it was read"):
        update_config_file(path, {"notifications": {"fun_mode": "false"}}, revision)
    assert load_config(path).fun_mode is True


def test_update_config_file_directory_fsync_failure_is_only_a_warning(tmp_path, caplog):
    path = _editable_file(tmp_path)
    real_open = os.open

    def failing_dir_open(target, flags, *args):
        if Path(target) == tmp_path.resolve():
            raise OSError("fsync unsupported")
        return real_open(target, flags, *args)

    with patch("isis_monitor.config.os.open", side_effect=failing_dir_open):
        update_config_file(path, {"notifications": {"fun_mode": "true"}})
    assert load_config(path).fun_mode is True
    assert "Could not fsync" in caplog.text


def test_instrument_progress_pv_is_derived_from_the_name(tmp_path, caplog):
    config = load_config(_write(
        tmp_path, "[INSTRUMENT:POLARIS]\ncounts_pv = IN:POLARIS:CS:DASHBOARD:TAB:2:1:VALUE\nnotify_counts = 300\n"
    ))
    assert config.instruments[0].counts_pv == "IN:POLARIS:DAE:TOTALUAMPS"
    assert "[INSTRUMENT:POLARIS] ignoring unknown key(s): counts_pv" in caplog.text


def test_update_config_file_drops_old_counts_pv_and_rejects_editing_it(tmp_path):
    path = _editable_file(tmp_path, EDITABLE_BASE + "[INSTRUMENT:WISH]\ncounts_pv = OLD\nnotify_counts = 5\n")
    update_config_file(path, editable_settings(load_config(path)))
    assert "counts_pv" not in path.read_text().split("[INSTRUMENT:PEARL]")[1]

    with pytest.raises(ConfigError, match="'counts_pv' can't be edited"):
        update_config_file(path, {"instruments": [{"name": "WISH", "counts_pv": "X", "notify_counts": "5"}]})


def test_instrument_channel_defaults_to_experiment_and_can_be_instrument(tmp_path):
    config = load_config(_write(tmp_path, """\
[INSTRUMENT:PEARL]
notify_counts = 130
[INSTRUMENT:WISH]
notify_counts = 50
channel = Instrument
"""))
    assert [(i.name, i.channel) for i in config.instruments] == [("PEARL", "experiment"), ("WISH", "instrument")]
