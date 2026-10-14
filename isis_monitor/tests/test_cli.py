import contextlib
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from isis_monitor import cli
from isis_monitor.daemon import SingleInstanceLock
from isis_monitor.tests.helpers import (
    app_config,
    never_answers,
    run_cli,
    serving,
    write_ini,
)


@pytest.mark.parametrize("runner, result, out", [
    (cli.run_stop, {"shutdown": "ok"}, "Shutdown signal sent — daemon is stopping cleanly.\n"),
    (cli.run_stop, {"something": "else"}, "Daemon responded: {'something': 'else'}\n"),
    (cli.run_reconnect, {"beam": True, "mcr": False}, "PVWS: reconnecting\nMCR news: already reconnecting\n"),
])
async def test_commands_report_the_daemons_reply(tmp_path, capsys, runner, result, out):
    async with serving(tmp_path, command_handler=AsyncMock(return_value=result)):
        await runner(app_config(tmp_path))
    assert capsys.readouterr().out == out


async def _failing(_name):
    raise RuntimeError("x")


async def _unknown_command(_name):
    return {"error": "unknown_command"}


@pytest.mark.parametrize("runner, server, error", [
    (cli.run_stop, None, "No answer from the daemon"),  # None: no daemon at all
    (cli.run_stop, {"command_handler": _failing}, "Daemon returned an error: internal_error"),
    (cli.run_stop, {"command_handler": never_answers}, "No reply to 'command' within"),
    (cli.run_reconnect, {"command_handler": _unknown_command}, "Daemon returned an error: {'error': 'unknown_command'}"),
    (cli.run_status, {}, "Daemon returned an error: unknown_method"),  # no status builder
    (cli.run_config, None, "Could not reach the daemon"),
])
async def test_commands_exit_1_without_a_good_reply(tmp_path, capsys, runner, server, error):
    short = 0.05 if server and server.get("command_handler") is never_answers else 10  # only for the one that hangs
    with patch("isis_monitor.ipc.STOP_FLUSH_TIMEOUT", short), patch("isis_monitor.ipc.REQUEST_TIMEOUT", short):
        async with serving(tmp_path, **server) if server is not None else contextlib.nullcontext():
            with pytest.raises(SystemExit) as exc:
                await runner(app_config(tmp_path))
    assert exc.value.code == 1
    assert error in capsys.readouterr().err


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


def test_main_daemon_refuses_second_instance(tmp_path, capsys):
    with SingleInstanceLock(tmp_path / "d.lock"), patch("isis_monitor.cli.run_daemon") as run_daemon:
        with pytest.raises(SystemExit):
            run_cli("daemon", write_ini(tmp_path))
    run_daemon.assert_not_called()
    assert "Lock file already held" in capsys.readouterr().err


@pytest.mark.parametrize("mode, log_file, handlers", [
    ("daemon", "", [logging.StreamHandler]),
    ("daemon", "monitor.log", [logging.StreamHandler, cli.RotatingFileHandler]),
    ("stop", "monitor.log", None),  # basicConfig's default: stderr
])
def test_only_the_daemon_writes_the_log_file_at_its_level(tmp_path, mode, log_file, handlers):
    config = app_config(tmp_path, log_level="DEBUG", log_file=log_file and str(tmp_path / log_file))
    with patch("isis_monitor.cli.logging.basicConfig") as basic:
        cli.configure_logging(mode, config)
    assert basic.call_args.kwargs["level"] == ("DEBUG" if mode == "daemon" else logging.WARNING)
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


def test_main_reports_config_errors_and_reads_the_push_secret_only_for_the_daemon(tmp_path, capsys):
    with pytest.raises(SystemExit):
        run_cli("stop", str(tmp_path / "missing.ini"))
    assert "Configuration error" in capsys.readouterr().err

    ini = write_ini(tmp_path)
    with open(ini, "a") as f:
        f.write(f"[PUSH]\nurl = http://127.0.0.1:1/ingest\nsecret_file = {tmp_path / 'missing'}\n")
    with patch("isis_monitor.cli.run_stop", new_callable=AsyncMock) as run_stop:
        run_cli("stop", ini)
    run_stop.assert_awaited_once()
    with patch("isis_monitor.cli.run_daemon", new_callable=AsyncMock) as run_daemon, pytest.raises(SystemExit):
        run_cli("daemon", ini)
    run_daemon.assert_not_called()
    assert "Configuration error" in (err := capsys.readouterr().err) and "secret_file could not be read" in err


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
