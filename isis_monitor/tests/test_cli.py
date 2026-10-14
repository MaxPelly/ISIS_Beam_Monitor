import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from isis_monitor import cli
from isis_monitor.daemon import SingleInstanceLock
from isis_monitor.tests.helpers import app_config, never_answers, run_cli, serving, write_ini


async def test_run_stop_without_daemon_exits_1(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        await cli.run_stop(app_config(tmp_path))
    assert exc.value.code == 1
    assert "No answer from the daemon" in capsys.readouterr().err


@pytest.mark.parametrize("result, expected", [
    ({"shutdown": "ok"}, "stopping cleanly"),
    ({"something": "else"}, "Daemon responded"),
])
async def test_run_stop_reports_daemon_reply(tmp_path, capsys, result, expected):
    async with serving(tmp_path, command_handler=AsyncMock(return_value=result)):
        await cli.run_stop(app_config(tmp_path))
    assert expected in capsys.readouterr().out


async def test_run_stop_error_reply_exits_1(tmp_path, capsys):
    async with serving(tmp_path, command_handler=AsyncMock(side_effect=RuntimeError("x"))):
        with pytest.raises(SystemExit):
            await cli.run_stop(app_config(tmp_path))
    assert "internal_error" in capsys.readouterr().err


@pytest.mark.parametrize("result, out", [
    ({"beam": True, "mcr": False}, "PVWS: reconnecting\nMCR news: already reconnecting\n"),
])
async def test_reconnect_command(tmp_path, capsys, result, out):
    async with serving(tmp_path, command_handler=AsyncMock(return_value=result)) as server:
        await cli.run_reconnect(app_config(tmp_path))
        assert capsys.readouterr().out == out
        server.command_handler = AsyncMock(return_value={"error": "unknown_command"})
        with pytest.raises(SystemExit):
            await cli.run_reconnect(app_config(tmp_path))
    assert "Daemon returned an error: {'error': 'unknown_command'}" in capsys.readouterr().err


async def test_status_command_reports_an_error_reply(tmp_path, capsys):
    async with serving(tmp_path):  # no status builder, so get_status is unknown
        with pytest.raises(SystemExit):
            await cli.run_status(app_config(tmp_path))
    assert "Daemon returned an error: unknown_method" in capsys.readouterr().err


def test_parse_args_modes():
    args = cli.parse_args(["daemon", "c.ini", "--dummy"])
    assert (args.mode, args.dummy, args.notify_current) == ("daemon", True, None)
    assert cli.parse_args(["stop", "c.ini"]).mode == "stop"
    assert cli.parse_args(["config", "c.ini"]).mode == "config"
    assert cli.parse_args(["status", "c.ini", "--json"]).json is True
    assert cli.parse_args(["reconnect", "c.ini"]).mode == "reconnect"
    with pytest.raises(SystemExit):
        cli.parse_args([])


def test_main_logs_config_warnings_once_logging_is_set_up(tmp_path, caplog):
    ini = Path(write_ini(tmp_path))
    ini.write_text(ini.read_text() + "[NOTIFICATIONS]\nfun_mode = true\n")
    order = []
    with patch("isis_monitor.cli.configure_logging", side_effect=lambda *a: order.append("logging")), \
            patch("isis_monitor.cli.run_stop", new_callable=AsyncMock), patch.object(cli.sys, "argv", ["isis-monitor", "stop", str(ini)]):
        caplog.handler.emit = lambda record, emit=caplog.handler.emit: (order.append("warning"), emit(record))
        cli.main()
    assert order[0] == "logging" and set(order[1:]) == {"warning"} and "[NOTIFICATIONS] fun_mode" in caplog.text


def test_main_reports_config_error(tmp_path, capsys):
    with pytest.raises(SystemExit):
        run_cli("stop", str(tmp_path / "missing.ini"))
    assert "Configuration error" in capsys.readouterr().err


def test_main_daemon_refuses_second_instance(tmp_path, capsys):
    with SingleInstanceLock(tmp_path / "d.lock"), patch("isis_monitor.cli.run_daemon") as run_daemon:
        with pytest.raises(SystemExit):
            run_cli("daemon", write_ini(tmp_path))
    run_daemon.assert_not_called()
    assert "Lock file already held" in capsys.readouterr().err


def test_configure_logging_sets_the_daemons_level(tmp_path):
    with patch("isis_monitor.cli.logging.basicConfig") as basic:
        cli.configure_logging("daemon", app_config(tmp_path, log_level="DEBUG", log_file=""))
    assert basic.call_args.kwargs["level"] == "DEBUG"


@pytest.mark.parametrize("mode, log_file, handlers", [
    ("daemon", "", [logging.StreamHandler]),
    ("daemon", "monitor.log", [logging.StreamHandler, cli.RotatingFileHandler]),
    ("stop", "monitor.log", None),  # basicConfig's default: stderr
])
def test_only_the_daemon_writes_the_log_file(tmp_path, mode, log_file, handlers):
    config = app_config(tmp_path, log_file=log_file and str(tmp_path / log_file))
    with patch("isis_monitor.cli.logging.basicConfig") as basic:
        cli.configure_logging(mode, config)
    given = basic.call_args.kwargs.get("handlers")
    assert (given and [type(h) for h in given]) == handlers
    for h in given or []:
        h.close()
    assert (tmp_path / "monitor.log").exists() is (cli.RotatingFileHandler in (handlers or []))


@pytest.mark.parametrize("mode, target", [
    ("daemon", "run_daemon"), ("stop", "run_stop"), ("config", "run_config"),
    ("status", "run_status"), ("reconnect", "run_reconnect"),
])
def test_main_dispatches_each_mode(tmp_path, mode, target):
    with patch(f"isis_monitor.cli.{target}", new_callable=AsyncMock, return_value=False) as runner:
        run_cli(mode, write_ini(tmp_path))
    runner.assert_awaited_once()


def test_main_reads_the_push_secret_only_for_the_daemon(tmp_path, capsys):
    ini = write_ini(tmp_path)
    with open(ini, "a") as f:
        f.write(f"[PUSH]\nurl = http://127.0.0.1:1/ingest\nsecret_file = {tmp_path / 'missing'}\n")
    with patch("isis_monitor.cli.run_stop", new_callable=AsyncMock) as run_stop:
        run_cli("stop", ini)
    run_stop.assert_awaited_once()
    with patch("isis_monitor.cli.run_daemon", new_callable=AsyncMock) as run_daemon, pytest.raises(SystemExit):
        run_cli("daemon", ini)
    run_daemon.assert_not_called()
    assert "secret_file could not be read" in capsys.readouterr().err


def test_keyboard_interrupt_exits_quietly(tmp_path, capsys):
    with patch("isis_monitor.cli.run_daemon", new_callable=AsyncMock, side_effect=KeyboardInterrupt):
        run_cli("daemon", write_ini(tmp_path))
    assert "Stopping monitors" in capsys.readouterr().out
    with patch("isis_monitor.cli.run_config", new_callable=AsyncMock, side_effect=KeyboardInterrupt), \
            pytest.raises(SystemExit) as exc:
        run_cli("config", write_ini(tmp_path))
    assert exc.value.code == 130 and capsys.readouterr().out == ""


async def test_config_command_lets_ctrl_c_interrupt_input(tmp_path):
    with patch("isis_monitor.cli.signal.signal") as set_handler, patch("isis_monitor.cli.run_config_editor", new_callable=AsyncMock):
        await cli.run_config(app_config(tmp_path))
    set_handler.assert_called_once_with(cli.signal.SIGINT, cli.signal.default_int_handler)


async def test_config_command_without_a_daemon(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        await cli.run_config(app_config(tmp_path))
    assert exc.value.code == 1
    assert "Could not reach the daemon" in capsys.readouterr().out


async def test_run_stop_gives_up_on_a_daemon_that_does_not_answer(tmp_path, capsys):
    with patch("isis_monitor.ipc.STOP_FLUSH_TIMEOUT", 0.05), patch("isis_monitor.ipc.REQUEST_TIMEOUT", 0.05):
        async with serving(tmp_path, command_handler=never_answers):
            with pytest.raises(SystemExit) as exc:
                await cli.run_stop(app_config(tmp_path))
    assert exc.value.code == 1
    assert "No reply to 'command' within" in capsys.readouterr().err
