import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import pytest

import main
from isis_monitor.config import AppConfig, InstrumentConfig
from isis_monitor.daemon_state import DaemonState
from isis_monitor.ipc import IPCClient, IPCServer
from isis_monitor.notifiers import DummyNotifier, TeamsNotifier
from isis_monitor.storage import SQLiteStateStore
from isis_monitor.tests.helpers import FakePVWS, never_answers, wait_until
from main import SingleInstanceLock, StateLogHandler


@pytest.fixture(autouse=True)
def no_exec():
    """A real exec would replace the pytest process itself."""
    with patch("main.os.execve", side_effect=AssertionError("os.execve called in a test")) as execve:
        yield execve


class TestStateLogHandler:
    def test_emit_calls_update_log(self):
        """StateLogHandler.emit should forward the formatted message."""
        mock_state = MagicMock()
        handler = StateLogHandler(mock_state)
        handler.setFormatter(logging.Formatter("%(levelname)s - %(message)s"))

        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello world", args=(), exc_info=None,
        )
        handler.emit(record)

        mock_state.update_log.assert_called_once_with("INFO - hello world")

    def test_emit_handles_exception_gracefully(self, caplog):
        """If state.update_log raises, handleError should be called and not propagate."""
        mock_state = MagicMock()
        mock_state.update_log.side_effect = RuntimeError("State broken")
        handler = StateLogHandler(mock_state)

        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test", args=(), exc_info=None,
        )
        # Should not raise
        handler.emit(record)


def test_single_instance_lock_writes_pid_and_releases(tmp_path):
    lock_file = tmp_path / "sub" / "test.lock"
    with SingleInstanceLock(lock_file):
        assert lock_file.read_text().strip() == str(os.getpid())
    # The file is kept (unlinking lock files is racy) but the lock is released.
    assert lock_file.exists()
    with SingleInstanceLock(lock_file):
        pass


def test_single_instance_lock_rejects_second_holder(tmp_path):
    lock_file = tmp_path / "test.lock"
    with SingleInstanceLock(lock_file):
        with pytest.raises(RuntimeError, match="Lock file already held"):
            with SingleInstanceLock(lock_file):
                pass


def test_apply_snapshot_to_tui():
    from main import _apply_snapshot_to_tui
    from isis_monitor.tui import RichTUI
    tui = RichTUI(60, 60, 4, 50)
    snap = {
        "beam_states": {
            "TS1": {"current": 42.0, "power": "high"}
        },
        "mcr_news": "Test news",
        "instruments": {"PEARL": {"run_name": "R1", "counts": 5.0, "notify_counts": 130.0, "beam_target": "TS1"}},
    }
    _apply_snapshot_to_tui(tui, snap)
    assert tui.mcr_news == "Test news"
    assert tui.instruments["PEARL"]["run_name"] == "R1"
    assert "TS1" in tui.beam_states
    assert tui.beam_states["TS1"]["current"] == 42.0


# ---------------------------------------------------------------------------
# StateLogHandler from other threads
# ---------------------------------------------------------------------------

async def test_state_log_handler_hands_off_records_from_other_threads():
    """DaemonState is loop-confined; a record logged in a worker thread must be
    applied on the loop thread, not in the worker."""
    state = MagicMock()
    applied_on = []
    state.update_log.side_effect = lambda msg: applied_on.append((threading.get_ident(), msg))
    handler = StateLogHandler(state, asyncio.get_running_loop())
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord("t", logging.INFO, "", 0, "from worker", (), None)

    await asyncio.to_thread(handler.emit, record)
    await wait_until(lambda: applied_on)
    assert applied_on == [(threading.get_ident(), "from worker")]

    handler.emit(logging.LogRecord("t", logging.INFO, "", 0, "on loop", (), None))
    assert applied_on[-1] == (threading.get_ident(), "on loop")


# ---------------------------------------------------------------------------
# run_until_stopped / signals
# ---------------------------------------------------------------------------

async def test_run_until_stopped_cancels_on_stop():
    cancelled = asyncio.Event()

    async def forever():
        try:
            await asyncio.sleep(3600)
        finally:
            cancelled.set()

    stop = asyncio.Event()
    task = asyncio.create_task(main.run_until_stopped(forever(), stop))
    await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert cancelled.is_set()


async def test_run_until_stopped_propagates_errors_and_returns_on_completion():
    async def boom():
        raise ValueError("crashed")

    async def quick():
        return 1

    with pytest.raises(ValueError, match="crashed"):
        await main.run_until_stopped(boom(), asyncio.Event())
    await asyncio.wait_for(main.run_until_stopped(quick(), asyncio.Event()), 1)


async def test_run_until_stopped_propagates_outer_cancellation():
    inner_cancelled = asyncio.Event()

    async def forever():
        try:
            await asyncio.sleep(3600)
        finally:
            inner_cancelled.set()

    task = asyncio.create_task(main.run_until_stopped(forever(), asyncio.Event()))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(inner_cancelled.wait(), 1)


async def test_install_signal_handlers_sets_stop_event():
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    main.install_signal_handlers(stop)
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stop.wait(), 1)
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)


# ---------------------------------------------------------------------------
# build_channels
# ---------------------------------------------------------------------------

def _config(tmp_path, **overrides) -> AppConfig:
    return replace(
        AppConfig(
            mcr_news_url="http://127.0.0.1:9/news",
            daemon_db_path=str(tmp_path / "state.db"),
            daemon_socket_path=str(tmp_path / "d.sock"),
            tui_socket_path=str(tmp_path / "d.sock"),
            daemon_lock_file=str(tmp_path / "d.lock"),
            log_file=str(tmp_path / "monitor.log"),
            mcr_poll_interval=3600,
        ),
        **overrides,
    )


def test_build_channels_dummy_mode_uses_dummy_everywhere(tmp_path):
    beam, exp, mcr = main.build_channels(_config(tmp_path, beam_teams_url="http://x"), dummy=True)
    for ch in (beam, exp, mcr):
        assert [type(n) for n in ch.notifiers] == [DummyNotifier]


def test_build_channels_only_configured_webhooks(tmp_path):
    beam, exp, mcr = main.build_channels(
        _config(tmp_path, beam_teams_url="http://beam", news_teams_url="http://news", webhook_timeout=3),
        dummy=False,
    )
    assert [n.webhook_url for n in beam.notifiers] == ["http://beam"]
    assert beam.notifiers[0].timeout == 3
    assert exp.notifiers == []
    assert isinstance(mcr.notifiers[0], TeamsNotifier)


# ---------------------------------------------------------------------------
# state_persistence_loop
# ---------------------------------------------------------------------------

async def test_state_persistence_loop_samples_trims_and_persists(tmp_path):
    config = _config(tmp_path, sample_interval=0.01, retention_days=1)
    state = DaemonState()
    state.update_health("beam", "connected")
    state.update_beam_state("TS1", 150.0, "high")
    stale = datetime.now(timezone.utc) - timedelta(days=2)
    state.append_beam_sample("TS2", 1.0, "low", ts=stale)
    store = SQLiteStateStore(tmp_path / "state.db")
    store.write_samples([(stale, "TS2", 1.0, "low")])
    store.commit()
    stop = asyncio.Event()

    task = asyncio.create_task(main.state_persistence_loop(config, state, store, stop))
    await wait_until(lambda: len(state.history["TS1"]) >= 2)
    stop.set()
    await asyncio.wait_for(task, 1)

    rows = store.load_recent_samples(stale - timedelta(days=1))
    assert all(r["timestamp"] > stale.isoformat() for r in rows)  # stale row pruned
    assert {r["target"] for r in rows} == {"TS1"}  # TS2 and Muons have no reading
    assert all(ts > stale for ts, _, _ in state.history["TS2"])  # stale sample trimmed
    assert json.loads(store.load_snapshot("daemon_state"))["beam_states"]["TS1"]["power"] == "high"
    store.close()


async def test_state_persistence_loop_samples_only_while_the_beam_feed_is_connected(tmp_path):
    config = _config(tmp_path, sample_interval=0.01)
    state = DaemonState()
    state.update_beam_state("TS1", 150.0, "high")  # e.g. restored, beam health still "unknown"
    store = SQLiteStateStore(tmp_path / "state.db")
    stop = asyncio.Event()

    task = asyncio.create_task(main.state_persistence_loop(config, state, store, stop))
    try:
        await wait_until(lambda: store.load_snapshot("daemon_state") is not None)
        assert not state.history["TS1"]
        state.update_health("beam", "connected")
        await wait_until(lambda: len(state.history["TS1"]) >= 1)
        state.update_health("beam", "disconnected")
        await asyncio.sleep(0.05)  # let any sample already under way finish
        sampled = len(state.history["TS1"])
        await asyncio.sleep(0.05)
        assert len(state.history["TS1"]) == sampled
    finally:
        stop.set()
        await asyncio.wait_for(task, 1)
        store.close()


async def test_state_persistence_loop_survives_database_errors(tmp_path, caplog):
    config = _config(tmp_path, sample_interval=0.01)
    store = MagicMock()
    store.write_samples.side_effect = [sqlite3.OperationalError("disk I/O error"), None, None, None]
    store.run = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    stop = asyncio.Event()

    task = asyncio.create_task(main.state_persistence_loop(config, DaemonState(), store, stop))
    await wait_until(lambda: store.commit.call_count >= 1)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert "Failed to persist daemon state" in caplog.text


# ---------------------------------------------------------------------------
# run_daemon end to end
# ---------------------------------------------------------------------------

def _persisted_samples(db):
    store = SQLiteStateStore(db)
    try:
        return store.load_recent_samples(datetime.now(timezone.utc) - timedelta(hours=1))
    finally:
        store.close()


DAEMON_ARGS = argparse.Namespace(dummy=True, notify_current=False)


@contextlib.asynccontextmanager
async def daemon(config):
    stop = asyncio.Event()
    with patch("main.install_signal_handlers"):
        task = asyncio.create_task(main.run_daemon(config, DAEMON_ARGS, stop))
        try:
            await wait_until(lambda: os.path.exists(config.daemon_socket_path))
            yield task, stop
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)


async def test_run_daemon_serves_ipc_and_shuts_down_cleanly_with_tui_attached(tmp_path):
    update = {"type": "update", "pv": AppConfig.ts1_beam_current_pv, "value": 150.0}
    async with FakePVWS([update]) as pvws:
        config = _config(tmp_path, isis_websocket_url=pvws.url, sample_interval=0.02)
        root_handlers = list(logging.getLogger().handlers)
        async with daemon(config) as (task, _stop):
            client = IPCClient(config.daemon_socket_path)
            await client.connect()
            await client.request({"method": "subscribe_updates"})

            async def ts1_power():
                reply = await client.request({"method": "get_snapshot"})
                return reply["snapshot"]["beam_states"]["TS1"]["power"]

            for _ in range(200):
                if await ts1_power() == "high":
                    break
                await asyncio.sleep(0.01)
            assert await ts1_power() == "high"
            await wait_until(lambda: _persisted_samples(tmp_path / "state.db"))

            replies = {
                name: (await client.request({"method": "command", "name": name}))["result"]
                for name in ("force_reconnect_mcr", "bogus")
            }
            assert replies["force_reconnect_mcr"] == {"mcr": True}
            assert replies["bogus"] == {"error": "unknown_command", "name": "bogus"}

            shutdown = await client.request({"method": "command", "name": "shutdown"})
            assert shutdown["result"] == {"shutdown": "ok"}
            await asyncio.wait_for(task, 5)  # exits even though our client is still attached
            await client.close()

    assert not os.path.exists(config.daemon_socket_path)
    assert logging.getLogger().handlers == root_handlers  # StateLogHandler removed
    store = SQLiteStateStore(tmp_path / "state.db")
    snap = json.loads(store.load_snapshot("daemon_state"))
    store.close()
    assert snap["health"]["daemon"] == "stopping"
    assert snap["beam_states"]["TS1"]["power"] == "high"


async def test_run_daemon_restores_history_and_state_on_restart(tmp_path):
    ts = datetime.now(timezone.utc) - timedelta(minutes=5)
    store = SQLiteStateStore(tmp_path / "state.db")
    store.write_samples([(ts, "TS2", 7.0, "low")])
    store.upsert_snapshot("daemon_state", json.dumps({"mcr_news": "old news", "total_runs_completed": 30}))
    store.commit()
    store.close()

    config = _config(tmp_path, instruments=[InstrumentConfig("PEARL", 130.0, "TS1")])
    async with daemon(config):
        client = IPCClient(config.daemon_socket_path)
        await client.connect()
        snap = (await client.request({"method": "get_snapshot"}))["snapshot"]
        history = (await client.request({"method": "get_history"}))["history"]
        beam_only = (await client.request({"method": "command", "name": "force_reconnect_beam"}))["result"]
        both = (await client.request({"method": "command", "name": "force_reconnect_all"}))["result"]
        await client.close()

    assert snap["mcr_news"] == "old news"
    assert snap["instruments"]["PEARL"]["total_runs"] == 30  # legacy total moves to the first instrument
    assert history["TS2"][0]["current"] == 7.0
    assert set(beam_only) == {"beam"}
    assert set(both) == {"beam", "mcr"}


# ---------------------------------------------------------------------------
# TUI client side
# ---------------------------------------------------------------------------

def test_apply_event_to_tui_dispatches_each_event_type():
    tui = MagicMock()
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for message in [
        {"event": "beam", "payload": {"beam": "TS1", "current": 5, "power": "low"}},
        {"event": "mcr", "payload": {"news": "hi"}},
        {"event": "log", "payload": {"message": "line"}},
        {"event": "sample", "payload": {"beam": "TS2", "timestamp": ts.isoformat(), "current": 1, "power": "off"}},
        {"event": "sample", "payload": {"beam": "TS2"}},  # no timestamp: ignored
        {"event": "health", "payload": {"component": "beam", "status": "connected"}},
        {"event": "run", "payload": {"instrument": "WISH", "run_name": "Run 9"}},
        {"event": "counts", "payload": {"instrument": "WISH", "counts": 12}},
    ]:
        main._apply_event_to_tui(tui, message)

    assert tui.update_instrument.call_args_list == [call("WISH", run_name="Run 9"), call("WISH", counts=12.0)]

    tui.update_beam_state.assert_called_once_with("TS1", 5.0, "low")
    tui.update_mcr_news.assert_called_once_with("hi")
    tui.add_history_sample.assert_called_once_with("TS2", ts, 1.0, "off")
    assert tui.update_log.call_args_list == [call("line"), call("Health: beam -> connected")]


async def test_tui_connection_loop_syncs_streams_and_reconnects(tmp_path):
    config = _config(tmp_path, history_maxlen=2, tui_reconnect_initial=0.01, tui_reconnect_max=0.02)
    state = DaemonState()
    state.update_mcr_news("hello")
    for i in range(5):
        state.append_beam_sample("TS1", float(i), "low")
    state.update_log("old log")
    tui = MagicMock()
    clients = []

    server = IPCServer(tmp_path / "d.sock", state, AsyncMock(return_value={}))
    await server.start()
    task = asyncio.create_task(main.tui_connection_loop(config, tui, clients.append))
    try:
        await wait_until(lambda: any(c is not None for c in clients))
        tui.update_mcr_news.assert_called_with("hello")
        history = tui.set_history_snapshot.call_args[0][0]
        assert [r["current"] for r in history["TS1"]] == [3.0, 4.0]  # limited to history_maxlen
        assert call("old log") in tui.update_log.call_args_list

        state.update_beam_state("TS2", 40.0, "high")
        await wait_until(lambda: call("TS2", 40.0, "high") in tui.update_beam_state.call_args_list)

        await server.stop()  # daemon goes away: TUI shows it and retries
        await wait_until(lambda: call("disconnected") in tui.update_connection_state.call_args_list)
        assert clients[-1] is None

        server = IPCServer(tmp_path / "d.sock", state, AsyncMock(return_value={}))
        await server.start()
        await wait_until(lambda: sum(c is not None for c in clients) >= 2)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await server.stop()


async def test_handle_tui_key():
    stop, tui, tasks = asyncio.Event(), MagicMock(), set()

    main.handle_tui_key("r", None, stop, tui, tasks)
    tui.update_log.assert_called_with("Not connected to the daemon.")

    client = MagicMock()
    client.request = AsyncMock(return_value={"ok": True, "result": {"beam": True}})
    main.handle_tui_key("R", client, stop, tui, tasks)
    assert len(tasks) == 1
    await asyncio.gather(*tasks)
    await asyncio.sleep(0)
    assert not tasks  # done tasks are discarded
    client.request.assert_awaited_with({"method": "command", "name": "force_reconnect_all"})
    tui.update_log.assert_called_with("Reconnect request result: {'beam': True}")

    client.request = AsyncMock(side_effect=ConnectionError("gone"))
    main.handle_tui_key("r", client, stop, tui, tasks)
    await asyncio.gather(*tasks)
    tui.update_log.assert_called_with("Reconnect request failed: gone")

    main.handle_tui_key("c", None, stop, tui, tasks, AsyncMock())
    assert not tasks

    edit_config = AsyncMock()
    main.handle_tui_key("C", client, stop, tui, tasks, edit_config)
    await asyncio.gather(*tasks)
    edit_config.assert_awaited_once_with()

    main.handle_tui_key("x", client, stop, tui, tasks)
    assert not stop.is_set()
    main.handle_tui_key("Q", client, stop, tui, tasks)
    assert stop.is_set()


# ---------------------------------------------------------------------------
# run_stop / CLI
# ---------------------------------------------------------------------------

async def test_run_stop_without_daemon_exits_1(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        await main.run_stop(_config(tmp_path))
    assert exc.value.code == 1
    assert "Could not connect" in capsys.readouterr().out


@pytest.mark.parametrize("result, expected", [
    ({"shutdown": "ok"}, "stopping cleanly"),
    ({"something": "else"}, "Daemon responded"),
])
async def test_run_stop_reports_daemon_reply(tmp_path, capsys, result, expected):
    server = IPCServer(tmp_path / "d.sock", DaemonState(), AsyncMock(return_value=result))
    await server.start()
    try:
        await main.run_stop(_config(tmp_path))
    finally:
        await server.stop()
    assert expected in capsys.readouterr().out


async def test_run_stop_error_reply_exits_1(tmp_path, capsys):
    server = IPCServer(tmp_path / "d.sock", DaemonState(), AsyncMock(side_effect=RuntimeError("x")))
    await server.start()
    try:
        with pytest.raises(SystemExit):
            await main.run_stop(_config(tmp_path))
    finally:
        await server.stop()
    assert "internal_error" in capsys.readouterr().out


def test_parse_args_modes():
    args = main.parse_args(["daemon", "c.ini", "--dummy"])
    assert (args.mode, args.dummy, args.notify_current) == ("daemon", True, None)
    assert main.parse_args(["tui", "c.ini"]).mode == "tui"
    assert main.parse_args(["stop", "c.ini"]).mode == "stop"
    with pytest.raises(SystemExit):
        main.parse_args([])


def test_main_reports_config_error(tmp_path, capsys):
    with patch.object(main.sys, "argv", ["main.py", "stop", str(tmp_path / "missing.ini")]):
        with pytest.raises(SystemExit):
            main.main()
    assert "Configuration error" in capsys.readouterr().out


def test_main_daemon_refuses_second_instance(tmp_path, capsys):
    ini = tmp_path / "c.ini"
    ini.write_text(f"[DATA]\nmcr_news_url = http://x\n[DAEMON]\nlock_file = {tmp_path / 'd.lock'}\n")
    with SingleInstanceLock(tmp_path / "d.lock"), \
         patch.object(main.sys, "argv", ["main.py", "daemon", str(ini)]), \
         patch("main.configure_logging"), \
         patch("main.run_daemon") as run_daemon:
        with pytest.raises(SystemExit):
            main.main()
    run_daemon.assert_not_called()
    assert "Lock file already held" in capsys.readouterr().out


def test_configure_logging_resolves_relative_path_next_to_main(tmp_path):
    with patch("main.RotatingFileHandler") as handler_cls, patch("main.logging.basicConfig") as basic:
        main.configure_logging("rel.log", "debug", 10, 2)
        main.configure_logging(str(tmp_path / "abs.log"), "nonsense", 10, 2)
    paths = [c.args[0] for c in handler_cls.call_args_list]
    assert paths == [Path(main.__file__).parent / "rel.log", tmp_path / "abs.log"]
    assert [c.kwargs["level"] for c in basic.call_args_list] == [logging.DEBUG, logging.WARNING]


def _ini(tmp_path) -> str:
    ini = tmp_path / "c.ini"
    ini.write_text(f"[DATA]\nmcr_news_url = http://x\n[DAEMON]\nlock_file = {tmp_path / 'd.lock'}\n")
    return str(ini)


@pytest.mark.parametrize("mode, target", [("daemon", "run_daemon"), ("tui", "run_tui"), ("stop", "run_stop")])
def test_main_dispatches_each_mode(tmp_path, mode, target):
    with patch.object(main.sys, "argv", ["main.py", mode, _ini(tmp_path)]), \
         patch("main.configure_logging"), \
         patch(f"main.{target}", new_callable=AsyncMock, return_value=False) as runner:
        main.main()
    runner.assert_awaited_once()


def test_main_keyboard_interrupt_exits_quietly(tmp_path, capsys):
    with patch.object(main.sys, "argv", ["main.py", "stop", _ini(tmp_path)]), \
         patch("main.configure_logging"), \
         patch("main.run_stop", new_callable=AsyncMock, side_effect=KeyboardInterrupt):
        main.main()
    assert "Stopping monitors" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Config editing and restart
# ---------------------------------------------------------------------------

def _daemon_ini(tmp_path) -> Path:
    ini = tmp_path / "live.ini"
    ini.write_text(
        "[DATA]\nmcr_news_url = http://127.0.0.1:9/news\n"
        "[INSTRUMENT:PEARL]\nnotify_counts = 130\n"
    )
    return ini


@contextlib.asynccontextmanager
async def daemon_with_file(tmp_path):
    config = _config(tmp_path)
    args = argparse.Namespace(dummy=True, notify_current=False, config=_daemon_ini(tmp_path))
    stop = asyncio.Event()
    with patch("main.install_signal_handlers"):
        task = asyncio.create_task(main.run_daemon(config, args, stop))
        await wait_until(lambda: os.path.exists(config.daemon_socket_path))
        client = IPCClient(config.daemon_socket_path)
        await client.connect()
        try:
            yield task, client, args.config
        finally:
            await client.close()
            stop.set()
            await asyncio.wait_for(task, 5)


async def _revision(client) -> str:
    return (await client.request({"method": "get_config"}))["revision"]


async def test_daemon_get_config_reads_the_file(tmp_path):
    async with daemon_with_file(tmp_path) as (task, client, ini):
        reply = await client.request({"method": "get_config"})
    assert reply["config"]["instruments"][0]["name"] == "PEARL"
    assert reply["beam_targets"] == ["TS1", "TS2", "Muon"]
    assert reply["channel_modes"] == ["experiment", "instrument"]
    assert len(reply["revision"]) == 64
    assert task.result() is False  # plain stop, no restart


async def test_daemon_update_config_requires_revision(tmp_path):
    async with daemon_with_file(tmp_path) as (task, client, ini):
        reply = await client.request({"method": "update_config", "settings": {}})
        assert (reply["ok"], reply["error"]) == (False, "invalid_request")
        assert not task.done()


async def test_daemon_rejects_update_based_on_stale_read(tmp_path):
    """A hand edit (or another TUI's save) since get_config must not be overwritten."""
    async with daemon_with_file(tmp_path) as (task, client, ini):
        revision = await _revision(client)
        ini.write_text(ini.read_text() + "[INSTRUMENT:WISH]\nnotify_counts = 5\n")
        reply = await client.request({
            "method": "update_config", "revision": revision, "settings": {"notifications": {"fun_mode": "true"}},
        })
        assert (reply["ok"], reply["error"]) == (False, "config_changed")
        assert "[INSTRUMENT:WISH]" in ini.read_text() and "fun_mode" not in ini.read_text()
        assert not task.done()


async def test_concurrent_updates_only_one_is_applied(tmp_path):
    """Two TUIs saving at once from the same read: the edits are serialised,
    and the loser gets restart_pending (or the connection closes as the
    daemon restarts) rather than overwriting the winner's change."""
    async with daemon_with_file(tmp_path) as (task, client, ini):
        revision = await _revision(client)
        other = IPCClient(tmp_path / "d.sock")
        await other.connect()
        try:
            results = await asyncio.gather(
                client.request({"method": "update_config", "revision": revision,
                                "settings": {"notifications": {"fun_mode": "true"}}}),
                other.request({"method": "update_config", "revision": revision,
                               "settings": {"notifications": {"summary_time": "09:30"}}}),
                return_exceptions=True,
            )
        finally:
            await other.close()
        assert await asyncio.wait_for(task, 5) is True

    winners = [r for r in results if isinstance(r, dict) and r.get("restarting")]
    losers = [r for r in results if r not in winners]
    assert len(winners) == 1
    assert isinstance(losers[0], ConnectionError) or losers[0]["error"] == "restart_pending"
    text = ini.read_text()
    assert ("fun_mode = true" in text) != ("summary_time = 09:30" in text)


async def test_daemon_rejects_invalid_config_and_keeps_running(tmp_path):
    async with daemon_with_file(tmp_path) as (task, client, ini):
        before = ini.read_text()
        reply = await client.request({
            "method": "update_config", "revision": await _revision(client), "settings": {"instruments": []},
        })
        assert (reply["ok"], reply["error"]) == (False, "invalid_config")
        assert "non-empty" in reply["detail"]
        assert ini.read_text() == before
        assert not task.done()


async def test_daemon_reports_config_write_failure(tmp_path):
    async with daemon_with_file(tmp_path) as (task, client, ini):
        with patch("main.update_config_file", side_effect=PermissionError("read-only")):
            reply = await client.request({"method": "update_config", "revision": "r", "settings": {}})
        assert (reply["ok"], reply["error"], reply["detail"]) == (False, "config_write_failed", "read-only")
        assert not task.done()


async def test_daemon_update_config_writes_file_and_requests_restart(tmp_path):
    async with daemon_with_file(tmp_path) as (task, client, ini):
        reply = await client.request({
            "method": "update_config",
            "revision": await _revision(client),
            "settings": {"notifications": {"fun_mode": "true"}},
        })
        assert reply == {"ok": True, "restarting": True, "version": 1}
        assert await asyncio.wait_for(task, 5) is True
    assert "fun_mode = true" in ini.read_text()


async def test_daemon_restart_command_requests_restart(tmp_path):
    async with daemon_with_file(tmp_path) as (task, client, ini):
        reply = await client.request({"method": "command", "name": "restart"})
        assert reply["result"] == {"restart": "ok"}
        assert await asyncio.wait_for(task, 5) is True


def test_main_restarts_after_lock_is_released(tmp_path, no_exec):
    ini = _ini(tmp_path)
    lock_free = []

    def fake_exec(*_args):
        with SingleInstanceLock(tmp_path / "d.lock"):  # would raise if still held
            lock_free.append(True)

    no_exec.side_effect = fake_exec
    with patch.object(main.sys, "argv", ["main.py", "daemon", ini]), \
         patch.object(main.sys, "orig_argv", ["python3", "-u", "main.py", "daemon", ini]), \
         patch("main.configure_logging"), \
         patch("main.run_daemon", new_callable=AsyncMock, return_value=True):
        main.main()

    exe, argv, env = no_exec.call_args.args
    assert (exe, argv) == (main.sys.executable, [main.sys.executable, "-u", "main.py", "daemon", ini])
    assert env[main.RESTARTED_ENV] == "1"
    assert lock_free == [True]


def test_main_reports_failed_restart(tmp_path, no_exec, capsys):
    no_exec.side_effect = FileNotFoundError("no such interpreter")
    with patch.object(main.sys, "argv", ["main.py", "daemon", _ini(tmp_path)]), \
         patch("main.configure_logging"), \
         patch("main.run_daemon", new_callable=AsyncMock, return_value=True):
        with pytest.raises(SystemExit) as exc_info:
            main.main()
    assert exc_info.value.code == 1
    assert "Failed to restart daemon: no such interpreter" in capsys.readouterr().err


@pytest.mark.parametrize("restarted, expected", [(False, True), (True, False)])
def test_restarted_daemon_does_not_replay_notify_current(tmp_path, monkeypatch, restarted, expected):
    if restarted:
        monkeypatch.setenv(main.RESTARTED_ENV, "1")
    with patch.object(main.sys, "argv", ["main.py", "daemon", _ini(tmp_path), "-n"]), \
         patch("main.configure_logging"), \
         patch("main.run_daemon", new_callable=AsyncMock, return_value=False) as run_daemon:
        main.main()
    assert run_daemon.await_args.args[1].notify_current is expected
    assert main.RESTARTED_ENV not in os.environ


@contextlib.contextmanager
def tui_terminal():
    """A pipe standing in for the TTY, with termios/tty/RichTUI mocked."""
    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd, "r")
    tui = MagicMock()
    try:
        with patch.object(main.sys, "stdin", stdin), \
             patch("main.termios") as termios_mock, \
             patch("main.tty") as tty_mock, \
             patch("main.RichTUI", return_value=tui), \
             patch("main.install_signal_handlers"):
            termios_mock.tcgetattr.return_value = "saved"
            yield tui, write_fd, termios_mock, tty_mock
    finally:
        os.close(write_fd)
        stdin.close()


async def test_run_tui_c_hands_the_terminal_to_the_config_editor_and_back(tmp_path, capsys):
    async with daemon_with_file(tmp_path) as (_task, _client, _ini):
        with tui_terminal() as (tui, keys, termios_mock, tty_mock):
            stop = asyncio.Event()
            task = asyncio.create_task(main.run_tui(_config(tmp_path), stop))
            await wait_until(lambda: call("Subscribed to daemon updates.") in tui.update_log.call_args_list)

            os.write(keys, b"c")
            await wait_until(lambda: tui.stop.called)
            await wait_until(lambda: "=== Configuration ===" in capsys.readouterr().out)
            assert termios_mock.tcsetattr.call_count == 1  # line mode for the editor

            os.write(keys, b"q\n")  # quit the editor without changes
            await wait_until(lambda: tui.start.call_count == 2)
            assert tty_mock.setcbreak.call_count == 2  # back to key-at-a-time

            os.write(keys, b"q")
            await asyncio.wait_for(task, 2)
    assert tui.stop.call_count == 2


async def test_quitting_the_tui_cancels_an_open_config_editor(tmp_path, capsys):
    async with daemon_with_file(tmp_path) as (_task, _client, _ini):
        with tui_terminal() as (tui, keys, termios_mock, tty_mock):
            stop = asyncio.Event()
            task = asyncio.create_task(main.run_tui(_config(tmp_path), stop))
            await wait_until(lambda: call("Subscribed to daemon updates.") in tui.update_log.call_args_list)
            os.write(keys, b"c")
            await wait_until(lambda: "> " in capsys.readouterr().out)

            stop.set()  # e.g. Ctrl-C
            await asyncio.wait_for(task, 2)

    # Quitting doesn't bring the live display back just to tear it down.
    assert tui.start.call_count == 1 and tui.stop.call_count == 2
    assert termios_mock.tcsetattr.call_args_list[-1] == call(ANY, termios_mock.TCSADRAIN, "saved")


async def test_run_tui_handles_several_keys_in_one_read_and_quits_on_eof(tmp_path):
    """No daemon running: the TUI keeps retrying, still handles keys, and
    restores the terminal when it quits."""
    with tui_terminal() as (tui, keys, termios_mock, tty_mock):
        stop = asyncio.Event()
        config = _config(tmp_path, tui_reconnect_initial=0.01, tui_reconnect_max=0.01)
        task = asyncio.create_task(main.run_tui(config, stop))
        await wait_until(lambda: call("disconnected") in tui.update_connection_state.call_args_list)
        os.write(keys, b"rc")  # arrive in one read; both handled
        await wait_until(lambda: tui.update_log.call_args_list.count(call("Not connected to the daemon.")) == 2)
        assert not task.done()

        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, keys)  # replaces the pipe's only writer, so stdin sees EOF
        os.close(null)
        await asyncio.wait_for(task, 2)
    assert stop.is_set()
    tty_mock.setcbreak.assert_called_once()
    termios_mock.tcsetattr.assert_called_once_with(ANY, termios_mock.TCSADRAIN, "saved")
    tui.start.assert_called_once()
    tui.stop.assert_called_once()


async def test_config_editor_saves_over_the_new_connection_after_reconnecting(tmp_path, capsys):
    config = _config(tmp_path, tui_reconnect_initial=0.05, tui_reconnect_max=0.05)
    clients = []

    def recording_client(path):
        clients.append(IPCClient(path))
        return clients[-1]

    async with daemon_with_file(tmp_path) as (_task, _client, ini):
        with tui_terminal() as (tui, keys, _termios, _tty), patch("main.IPCClient", side_effect=recording_client):
            task = asyncio.create_task(main.run_tui(config, asyncio.Event()))
            subscribed = call("Subscribed to daemon updates.")
            await wait_until(lambda: subscribed in tui.update_log.call_args_list)
            os.write(keys, b"c")
            await wait_until(lambda: "> " in capsys.readouterr().out)

            # The connection the editor started on drops; the TUI reconnects.
            tui.update_log.reset_mock()
            clients[-1].writer.transport.abort()
            await wait_until(lambda: subscribed in tui.update_log.call_args_list)
            assert len(clients) == 2

            for line in (b"1\n", b"true\n", b"s\n", b"y\n"):
                os.write(keys, line)
                await asyncio.sleep(0.05)
            await wait_until(lambda: "fun_mode = true" in ini.read_text())
            os.write(keys, b"\n")  # "Press Enter to return"
            await wait_until(lambda: tui.start.call_count == 2)
            os.write(keys, b"q")
            await asyncio.wait_for(task, 2)


async def test_failed_tui_action_is_logged(caplog):
    tasks = set()
    async def boom():
        raise ValueError("bad")
    main._track(asyncio.create_task(boom()), tasks)
    await asyncio.sleep(0.01)
    assert "TUI action failed" in caplog.text and "ValueError: bad" in caplog.text
    assert not tasks


async def test_keys_typed_ahead_of_the_editor_are_passed_to_it(tmp_path, capsys):
    async with daemon_with_file(tmp_path) as (_task, _client, _ini):
        with tui_terminal() as (tui, keys, _termios, _tty):
            task = asyncio.create_task(main.run_tui(_config(tmp_path), asyncio.Event()))
            await wait_until(lambda: call("Subscribed to daemon updates.") in tui.update_log.call_args_list)
            os.write(keys, b"c1\nx")  # one read: open editor, choose 1, then a partial line
            await wait_until(lambda: "fun_mode [false]: " in capsys.readouterr().out)
            os.write(keys, b"true\n")  # the hidden partial "x" was dropped
            await wait_until(lambda: "  1) fun_mode                true" in capsys.readouterr().out)
            os.write(keys, b"2\nUTC\n")  # several lines in one read are split up
            await wait_until(lambda: "  2) timezone                UTC" in capsys.readouterr().out)
            os.write(keys, b"q\n")
            await wait_until(lambda: "Discard your changes?" in capsys.readouterr().out)
            os.write(keys, b"y\n")
            await wait_until(lambda: tui.start.call_count == 2)
            os.write(keys, b"q")
            await asyncio.wait_for(task, 2)



async def test_run_daemon_stops_other_loops_when_one_crashes(tmp_path):
    persistence_cancelled = asyncio.Event()

    async def persistence(*_args):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            persistence_cancelled.set()
            raise

    config = _config(tmp_path)
    with patch("main.install_signal_handlers"), \
         patch("main.LOOP_STOP_TIMEOUT", 0.1), \
         patch("main.state_persistence_loop", side_effect=persistence), \
         patch("main.daily_summary_loop", new_callable=AsyncMock, side_effect=RuntimeError("summary bug")):
        with pytest.raises(RuntimeError, match="summary bug"):
            await asyncio.wait_for(main.run_daemon(config, DAEMON_ARGS, asyncio.Event()), 5)
    assert persistence_cancelled.is_set()
    assert not os.path.exists(config.daemon_socket_path)  # teardown still ran


async def test_run_daemon_seeds_trackers_from_the_restored_snapshot(tmp_path):
    started = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    store = SQLiteStateStore(tmp_path / "state.db")
    store.upsert_snapshot("daemon_state", json.dumps({"instruments": {"PEARL": {
        "run_name": "Run 9", "run_started_at": started, "counts": 150.0, "end_notified": True,
    }}}))
    store.commit()
    store.close()

    config = _config(tmp_path, instruments=[InstrumentConfig("PEARL", 130.0, "TS1")])
    seeded = {}
    real_restore = main.BeamMonitor.restore_instruments

    def spy(self, saved):
        real_restore(self, saved)
        seeded.update({name: vars(t.state).copy() for name, t in self.instruments.items()})

    with patch.object(main.BeamMonitor, "restore_instruments", spy):
        async with daemon(config):
            pass
    assert seeded["PEARL"]["run_name"] == "Run 9" and seeded["PEARL"]["end_notified"] is True



async def test_config_save_is_refused_once_shutdown_has_started(tmp_path):
    """A save accepted after SIGTERM or a shutdown command would re-exec the daemon."""
    config = _config(tmp_path)
    args = argparse.Namespace(dummy=True, notify_current=False, config=_daemon_ini(tmp_path))
    stop = asyncio.Event()
    handlers = {}
    real_ipc = main.IPCServer

    def capture(*a):
        server = real_ipc(*a)
        handlers["config"] = a[3]
        return server

    with patch("main.install_signal_handlers"), patch("main.IPCServer", side_effect=capture):
        task = asyncio.create_task(main.run_daemon(config, args, stop))
        await wait_until(lambda: "config" in handlers)
        revision = (await handlers["config"]("get_config", {}))["revision"]
        stop.set()
        reply = await handlers["config"]("update_config", {"revision": revision, "settings": {}})
        assert await asyncio.wait_for(task, 5) is False
    assert (reply["ok"], reply["error"]) == (False, "restart_pending")


async def test_sync_tui_subscribes_before_fetching_state():
    client = MagicMock()
    client.request = AsyncMock(return_value={"ok": True})
    await main._sync_tui(client, MagicMock(), 60)
    methods = [c.args[0]["method"] for c in client.request.await_args_list]
    assert methods == ["subscribe_updates", "get_snapshot", "get_history", "get_logs"]
    assert all(c.kwargs["timeout"] == main.IPC_REQUEST_TIMEOUT for c in client.request.await_args_list)


async def test_run_stop_gives_up_on_a_daemon_that_does_not_answer(tmp_path, capsys):
    config = _config(tmp_path)
    server = IPCServer(Path(config.daemon_socket_path), DaemonState(), never_answers)
    await server.start()
    try:
        with patch("main.IPC_REQUEST_TIMEOUT", 0.05), pytest.raises(SystemExit) as exc:
            await main.run_stop(config)
    finally:
        with patch("isis_monitor.ipc.STOP_FLUSH_TIMEOUT", 0.05):
            await server.stop()
    assert exc.value.code == 1
    assert "didn't answer" in capsys.readouterr().out
