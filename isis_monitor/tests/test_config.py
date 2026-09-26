import pytest
from pathlib import Path
from dataclasses import replace

from unittest.mock import patch

from isis_monitor.config import (
    load_config, ConfigError, AppConfig, InstrumentConfig, editable_settings, update_config_file,
)


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
    assert replace(config, instruments=[]) == AppConfig(mcr_news_url="http://test.com/news")
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


def test_legacy_pvs_become_single_instrument(tmp_path):
    """With no [INSTRUMENT:*] sections, the [PVS] keys still define one instrument."""
    config = load_config(_write(
        tmp_path, "[PVS]\ncounts_pv = IN:WISH:COUNTS\nrun_name_pv = IN:WISH:TITLE\ninstrument_target = TS2\nnotify_counts = 50\n"
    ))
    assert config.instruments == [
        InstrumentConfig("WISH", "IN:WISH:COUNTS", 50.0, "TS2", run_name_pv="IN:WISH:TITLE")
    ]


def test_legacy_instrument_defaults_to_pearl(tmp_path):
    config = load_config(_write(tmp_path, ""))
    assert [i.name for i in config.instruments] == ["PEARL"]
    assert config.instruments[0].notify_counts == 130.0


def test_legacy_instrument_name_fallback(tmp_path):
    config = load_config(_write(tmp_path, "[PVS]\ncounts_pv = COUNTS\nrun_name_pv = TITLE\n"))
    assert config.instruments[0].name == "INSTRUMENT"


def test_instrument_sections(tmp_path):
    config = load_config(_write(tmp_path, """\
[PVS]
instrument_target = TS2
counts_pv = IGNORED
[INSTRUMENT:PEARL]
counts_pv = IN:PEARL:COUNTS
notify_counts = 200
beam_target = TS1
[INSTRUMENT:wish]
counts_pv = IN:WISH:COUNTS
notify_counts = 75
"""))
    assert config.instruments == [
        InstrumentConfig("PEARL", "IN:PEARL:COUNTS", 200.0, "TS1"),
        InstrumentConfig("WISH", "IN:WISH:COUNTS", 75.0, "TS2"),
    ]
    assert config.instruments[1].run_name_pv == "IN:WISH:DAE:WDTITLE"


def test_instrument_section_prefix_is_case_insensitive(tmp_path):
    config = load_config(_write(tmp_path, "[instrument:wish]\ncounts_pv = X\nnotify_counts = 5\n"))
    assert [i.name for i in config.instruments] == ["WISH"]


def test_instrument_unknown_key_warns(tmp_path, caplog):
    load_config(_write(tmp_path, "[INSTRUMENT:PEARL]\ncounts_pv = X\nnotify_counts = 5\nteams_url = http://x\n"))
    assert "ignoring unknown key(s): teams_url" in caplog.text


def test_instrument_default_section_keys_not_reported_as_unknown(tmp_path, caplog):
    load_config(_write(tmp_path, "[DEFAULT]\nfoo = 1\n[INSTRUMENT:PEARL]\ncounts_pv = X\nnotify_counts = 5\n"))
    assert "unknown key" not in caplog.text


def test_duplicate_section_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="Could not parse"):
        load_config(_write(tmp_path, "[INSTRUMENT:WISH]\ncounts_pv = X\n[INSTRUMENT:WISH]\ncounts_pv = Y\n"))


@pytest.mark.parametrize("extra, match", [
    ("[PVS]\nnotify_counts = 0\n", r"^\[PVS\] notify_counts must be"),
    ("[PVS]\ncounts_pv = X\nrun_name_pv = X\n", r"^\[PVS\] PV X is already used"),
])
def test_legacy_instrument_errors_name_pvs_section(tmp_path, extra, match):
    with pytest.raises(ConfigError, match=match):
        load_config(_write(tmp_path, extra))


@pytest.mark.parametrize("extra, match", [
    ("[INSTRUMENT:]\ncounts_pv = X\n", "needs an instrument name"),
    ("[INSTRUMENT:PE ARL]\ncounts_pv = X\nnotify_counts = 5\n", "may only contain"),
    ("[INSTRUMENT:A:B]\ncounts_pv = X\nnotify_counts = 5\n", "may only contain"),
    ("[INSTRUMENT:PEARL]\ncounts_pv = X\nnotify_counts = nan\n", "must be a positive number"),
    ("[INSTRUMENT:PEARL]\ncounts_pv = X\nnotify_counts = inf\n", "must be a positive number"),
    ("[INSTRUMENT:PEARL]\ncounts_pv = IN:WISH:DAE:WDTITLE\nnotify_counts = 5\n[INSTRUMENT:WISH]\n"
     "counts_pv = Y\nnotify_counts = 5\n", "already used by instrument PEARL"),
    ("[INSTRUMENT:PEARL]\ncounts_pv = X\n", "notify_counts is required"),
    ("[INSTRUMENT:PEARL]\nnotify_counts = 5\n", "counts_pv is required"),
    ("[INSTRUMENT:PEARL]\ncounts_pv = X\nnotify_counts = lots\n", r"\[INSTRUMENT:PEARL\] notify_counts"),
    ("[INSTRUMENT:PEARL]\ncounts_pv = X\nnotify_counts = 0\n", "notify_counts must be a positive number"),
    ("[INSTRUMENT:PEARL]\ncounts_pv = X\nnotify_counts = 5\nbeam_target = Muons\n", "beam_target must be one of"),
    ("[INSTRUMENT:PEARL]\ncounts_pv = X\nnotify_counts = 5\n[INSTRUMENT:pearl]\ncounts_pv = Y\nnotify_counts = 5\n",
     "defined more than once"),
    ("[INSTRUMENT:PEARL]\ncounts_pv = X\nnotify_counts = 5\n[INSTRUMENT:WISH]\ncounts_pv = X\nnotify_counts = 5\n",
     "already used by instrument PEARL"),
    ("[INSTRUMENT:PEARL]\ncounts_pv = AC:TS1:BEAM:CURR\nnotify_counts = 5\n", "already used by the TS1 beam"),
])
def test_invalid_instrument_sections(tmp_path, extra, match):
    with pytest.raises(ConfigError, match=match):
        load_config(_write(tmp_path, extra))


@pytest.mark.parametrize("line, match", [
    ("timezone = Nowhere/City", "not a known timezone"),
    ("timezone = ../etc", "not a known timezone"),
    ("debounce_seconds = -1", "debounce_seconds must be between"),
    ("debounce_seconds = nan", "debounce_seconds must be between"),
    ("debounce_seconds = inf", "debounce_seconds must be between"),
    ("stall_minutes = 0", "stall_minutes must be above 0"),
    ("stall_minutes = inf", "stall_minutes must be above 0"),
    ("stall_minutes = 1e20", "stall_minutes must be above 0"),
    ("stall_minutes = nan", "stall_minutes must be above 0"),
])
def test_invalid_notification_settings(tmp_path, line, match):
    """Caught at load time, so a bad edit can't leave the daemon failing on restart."""
    with pytest.raises(ConfigError, match=match):
        load_config(_write(tmp_path, f"[NOTIFICATIONS]\n{line}\n"))


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
counts_pv = IN:PEARL:COUNTS
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
            "stall_minutes": "10", "summary_time": "08:00",
        },
        "instruments": [{"name": "PEARL", "counts_pv": "IN:PEARL:COUNTS", "notify_counts": "130", "beam_target": "TS1"}],
    }


def test_editable_settings_keep_full_precision():
    config = AppConfig(debounce_seconds=2.5, instruments=[InstrumentConfig("X", "C", 1234567.0, "TS1")])
    settings = editable_settings(config)
    assert settings["notifications"]["debounce_seconds"] == "2.5"
    assert settings["instruments"][0]["notify_counts"] == "1234567"


def test_update_config_file_round_trips_and_keeps_other_settings(tmp_path):
    path = _editable_file(tmp_path)
    settings = editable_settings(load_config(path))
    settings["notifications"]["fun_mode"] = "true"
    settings["instruments"][0]["notify_counts"] = "200"
    settings["instruments"].append({"name": "wish", "counts_pv": "IN:WISH:COUNTS", "notify_counts": "50", "beam_target": "TS2"})

    returned = update_config_file(path, settings)

    reloaded = load_config(path)
    assert reloaded == returned
    assert reloaded.fun_mode is True and reloaded.stall_minutes == 10.0
    assert reloaded.beam_teams_url == "http://secret"
    assert [(i.name, i.notify_counts, i.beam_target) for i in reloaded.instruments] == [
        ("PEARL", 200.0, "TS1"), ("WISH", 50.0, "TS2"),
    ]
    assert "a comment" not in path.read_text()  # documented limitation
    assert (tmp_path / "config.ini.bak").read_text() == EDITABLE_BASE
    assert path.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_update_config_file_removes_dropped_instruments(tmp_path):
    path = _editable_file(tmp_path, EDITABLE_BASE + "[Instrument:WISH]\ncounts_pv = W\nnotify_counts = 5\n")
    update_config_file(path, {"instruments": [{"name": "WISH", "counts_pv": "W", "notify_counts": "5"}]})
    assert [i.name for i in load_config(path).instruments] == ["WISH"]


def test_update_config_file_migrates_legacy_pvs_config(tmp_path):
    path = _editable_file(tmp_path, "[DATA]\nmcr_news_url = http://x\n[PVS]\ncounts_pv = IN:WISH:C\nnotify_counts = 40\n")
    update_config_file(path, editable_settings(load_config(path)))
    assert "[INSTRUMENT:WISH]" in path.read_text()
    assert load_config(path).instruments == [InstrumentConfig("WISH", "IN:WISH:C", 40.0, "TS1")]


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
    ({"instruments": [{"counts_pv": "C", "notify_counts": "5"}]}, "needs an instrument name"),
    ({"instruments": [{"name": "A B", "counts_pv": "C", "notify_counts": "5"}]}, "may only contain"),
    ({"instruments": [{"name": "PEARL", "counts_pv": "C", "notify_counts": "0"}]}, "must be a positive number"),
    ({"instruments": [{"name": "X", "counts_pv": "C", "notify_counts": "5"},
                      {"name": "x", "counts_pv": "D", "notify_counts": "5"}]}, "defined more than once"),
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
