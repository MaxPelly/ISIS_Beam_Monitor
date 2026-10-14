import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import main
from isis_monitor import messages
from isis_monitor.config import DEFAULT_BEAMS, AppConfig, InstrumentConfig
from isis_monitor.daemon_state import DaemonState
from isis_monitor.instrument import InstrumentTracker
from isis_monitor.ipc import ipc_request
from isis_monitor.notifiers import DummyNotifier, TeamsNotifier, WebhookNotifier
from isis_monitor.storage import SQLiteStateStore
from isis_monitor.tests.helpers import FakePVWS, fake_channel, never_answers, serving, wait_until
from main import SingleInstanceLock


@pytest.fixture(autouse=True)
def no_exec():
    """A real exec would replace the pytest process itself."""
    with patch("main.os.execve", side_effect=AssertionError("os.execve called in a test")) as execve:
        yield execve


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
        with pytest.raises(main.LockHeldError, match="Lock file already held"):
            with SingleInstanceLock(lock_file):
                pass



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

def _saved_instrument(inst: InstrumentConfig, **changes) -> dict:
    """A snapshot entry for `inst`, as its tracker saves one, with `changes`."""
    tracker = InstrumentTracker(inst, fake_channel(), lambda _target: "unknown", stall_minutes=10)
    return {**json.loads(json.dumps(tracker.to_snapshot())), **changes}


def _config(tmp_path, **overrides) -> AppConfig:
    return replace(
        AppConfig(
            mcr_news_url="http://127.0.0.1:9/news",
            daemon_db_path=str(tmp_path / "state.db"),
            daemon_socket_path=str(tmp_path / "d.sock"),
            daemon_lock_file=str(tmp_path / "d.lock"),
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


def test_build_channels_shares_one_push_webhook_and_session(tmp_path):
    session = MagicMock()
    channels = main.build_channels(
        _config(tmp_path, beam_teams_url="http://beam", push_url="http://push", push_secret=b"s" * 32, push_timeout=1),
        dummy=False, session=session,
    )
    (push,) = {ch.notifiers[-1] for ch in channels}  # one notifier, so one queue: the feed keeps its order
    assert isinstance(push, WebhookNotifier)
    assert (push.webhook_url, push.secret, push.timeout) == ("http://push", b"s" * 32, 1)
    assert [type(n) for n in channels[0].notifiers] == [TeamsNotifier, WebhookNotifier]
    assert channels[0].notifiers[0]._session is push._session is session


# The Teams payload's channel, which Power Automate flows route on, for one
# card of each kind. These are the values from before routing moved out of
# Notification (1.1.1); they mustn't change.
TEAMS_ROUTES = [
    ("beam", "beam_change", "TS1"),
    ("beam", "startup_status", "Muons"),
    ("beam", "daily_summary", "TS2"),
    ("exp", "run_started", "Experiment Updates"),
    ("exp", "run_started_own_channel", "WISH"),
    ("exp", "experiment_finished_own_channel", "WISH"),
    ("mcr", "mcr_news", "MCR News"),
]


def _card(kind):
    now = datetime.now(timezone.utc)
    hour = timedelta(hours=1)
    run = dict(new_run_name="run", prev_run_name="old", prev_duration=hour, prev_counts=5, time_now=now, target=10)
    return {
        "beam_change": lambda: messages.beam_change(
            "TS1", prev_state="high", new_state="off", beam_val=0, prev_val=150, high_threshold=140,
            time_in_prev_state=hour, time_now=now),
        "startup_status": lambda: messages.startup_status("Muons", state="high", beam_val=5, time_now=now),
        "daily_summary": lambda: messages.daily_summary(
            "TS2", uptime_pct=99, trips=0, longest_on_streak=hour, sparkline="", instrument_runs={}, time_now=now),
        "run_started": lambda: messages.run_started("PEARL", **run),
        "run_started_own_channel": lambda: messages.run_started("WISH", **run),
        "experiment_finished_own_channel": lambda: messages.experiment_finished(
            "WISH", rb_number="123", runs=2, uah=5, duration=hour, partial=False, time_now=now),
        "mcr_news": lambda: messages.mcr_news("news", time_now=now),
    }[kind]()


async def test_teams_channel_field_is_unchanged_for_every_kind_of_card(tmp_path):
    config = _config(
        tmp_path, beam_teams_url="http://beam", experiment_teams_url="http://exp", news_teams_url="http://news",
        instruments=[InstrumentConfig("PEARL", 100, "TS1"), InstrumentConfig("WISH", 100, "TS2", channel="instrument")],
    )
    beam, exp, mcr = main.build_channels(config, dummy=False)
    channels = {"beam": beam, "exp": exp, "mcr": mcr}
    posted = []

    async def post(self, payload):
        posted.append(payload["attachments"][0]["content"]["channel"])
        return None, False, None
    with patch.object(TeamsNotifier, "_post", post):
        for channel, kind, _ in TEAMS_ROUTES:
            channels[channel].broadcast(_card(kind))
            (teams,) = channels[channel].notifiers
            await teams.flush(1)
    for ch in channels.values():
        await ch.notifiers[0].close()
    assert posted == [route for _, _, route in TEAMS_ROUTES]


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
async def daemon(config, args=DAEMON_ARGS):
    stop = asyncio.Event()
    with patch("main.install_signal_handlers"):
        task = asyncio.create_task(main.run_daemon(config, args, stop))
        try:
            await wait_until(lambda: os.path.exists(config.daemon_socket_path))
            yield task, stop
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)


async def test_run_daemon_serves_ipc_and_shuts_down_cleanly_with_a_client_attached(tmp_path):
    update = {"type": "update", "pv": DEFAULT_BEAMS[0].pv, "value": 150.0}
    async with FakePVWS([update]) as pvws:
        config = _config(tmp_path, isis_websocket_url=pvws.url, sample_interval=0.02)
        async with daemon(config) as (task, _stop):
            def request(payload):
                return ipc_request(Path(config.daemon_socket_path), payload)

            async def ts1_power():
                return (await request({"method": "get_status"}))["status"]["beams"][0]["power"]

            for _ in range(200):
                if await ts1_power() == "high":
                    break
                await asyncio.sleep(0.01)
            assert await ts1_power() == "high"
            await wait_until(lambda: _persisted_samples(tmp_path / "state.db"))

            bogus = (await request({"method": "command", "name": "bogus"}))["result"]
            assert bogus == {"error": "unknown_command", "name": "bogus"}

            _reader, attached = await asyncio.open_unix_connection(config.daemon_socket_path)
            shutdown = await request({"method": "command", "name": "shutdown"})
            assert shutdown["result"] == {"shutdown": "ok"}
            await asyncio.wait_for(task, 5)  # exits even though a client is still connected
            attached.close()

    assert not os.path.exists(config.daemon_socket_path)
    store = SQLiteStateStore(tmp_path / "state.db")
    snap = json.loads(store.load_snapshot("daemon_state"))
    store.close()
    assert snap["beam_states"]["TS1"]["power"] == "high"


async def test_run_daemon_closes_each_notifier_once_then_the_session(tmp_path):
    config = _config(tmp_path, beam_teams_url="http://127.0.0.1:9/beam", push_url="http://127.0.0.1:9/ingest",
                     push_secret=b"s" * 32)
    args = argparse.Namespace(dummy=False, notify_current=False)
    order = []
    with patch.object(main.Notifier, "close", autospec=True, side_effect=lambda n, t: order.append(n)), \
            patch.object(main.aiohttp.ClientSession, "close", autospec=True,
                         side_effect=lambda s: order.append("session")):
        async with daemon(config, args):
            pass
    # (The MCR monitor closes a session of its own earlier.)
    assert [type(n) for n in order if n != "session"] == [TeamsNotifier, WebhookNotifier]
    assert order[-1] == "session"


async def test_run_daemon_restores_history_and_state_on_restart(tmp_path):
    ts = datetime.now(timezone.utc) - timedelta(minutes=5)
    store = SQLiteStateStore(tmp_path / "state.db")
    pearl = InstrumentConfig("PEARL", 130.0, "TS1")
    saved = _saved_instrument(pearl, run_name="Run 30", run_started_at=ts.isoformat(), counts=12.5, rb_number="A")
    store.write_samples([(ts, "TS2", 7.0, "low")])
    store.upsert_snapshot("daemon_state", json.dumps({"mcr_news": "old news", "instruments": {"PEARL": saved}}))
    store.commit()
    store.close()

    config = _config(tmp_path, instruments=[pearl])
    with patch("main.MCRNewsMonitor", wraps=main.MCRNewsMonitor) as mcr_cls:
        async with daemon(config):
            status = (await ipc_request(Path(config.daemon_socket_path), {"method": "get_status"}))["status"]
            reconnect = (await ipc_request(Path(config.daemon_socket_path),
                                           {"method": "command", "name": "force_reconnect_all"}))["result"]

    assert status["mcr"]["news"] == "old news"
    assert mcr_cls.call_args.kwargs["old_news"] == "old news"
    (inst,) = status["instruments"]
    assert (inst["name"], inst["title"], inst["rb"], inst["beam"]) == ("PEARL", "Run 30", "A", "TS1")
    assert [v for v, _ in status["beams"][1]["history"]["values"] if v is not None] == [7.0]
    assert set(reconnect) == {"beam", "mcr"}



# ---------------------------------------------------------------------------
# run_stop / CLI
# ---------------------------------------------------------------------------

async def test_run_stop_without_daemon_exits_1(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        await main.run_stop(_config(tmp_path))
    assert exc.value.code == 1
    assert "No answer from the daemon" in capsys.readouterr().out


@pytest.mark.parametrize("result, expected", [
    ({"shutdown": "ok"}, "stopping cleanly"),
    ({"something": "else"}, "Daemon responded"),
])
async def test_run_stop_reports_daemon_reply(tmp_path, capsys, result, expected):
    async with serving(tmp_path, command_handler=AsyncMock(return_value=result)):
        await main.run_stop(_config(tmp_path))
    assert expected in capsys.readouterr().out


async def test_run_stop_error_reply_exits_1(tmp_path, capsys):
    async with serving(tmp_path, command_handler=AsyncMock(side_effect=RuntimeError("x"))):
        with pytest.raises(SystemExit):
            await main.run_stop(_config(tmp_path))
    assert "internal_error" in capsys.readouterr().out


async def test_status_command_prints_the_daemons_status(tmp_path, capsys):
    async with daemon_with_file(tmp_path):
        await main.run_status(_config(tmp_path))
        assert capsys.readouterr().out.startswith("ISIS Beam Monitor v")
        await main.run_status(_config(tmp_path), as_json=True)
    status = json.loads(capsys.readouterr().out)
    assert status["kind"] == "status" and [b["name"] for b in status["beams"]] == ["TS1", "TS2", "Muons"]


@pytest.mark.parametrize("result, out", [
    ({"beam": True, "mcr": False}, "PVWS: reconnecting\nMCR news: already reconnecting\n"),
])
async def test_reconnect_command(tmp_path, capsys, result, out):
    async with serving(tmp_path, command_handler=AsyncMock(return_value=result)) as server:
        await main.run_reconnect(_config(tmp_path))
        assert capsys.readouterr().out == out
        server.command_handler = AsyncMock(return_value={"error": "unknown_command"})
        with pytest.raises(SystemExit):
            await main.run_reconnect(_config(tmp_path))
    assert "Daemon returned an error: {'error': 'unknown_command'}" in capsys.readouterr().out


async def test_status_command_reports_an_error_reply(tmp_path, capsys):
    async with serving(tmp_path):  # no status builder, so get_status is unknown
        with pytest.raises(SystemExit):
            await main.run_status(_config(tmp_path))
    assert "Daemon returned an error: unknown_method" in capsys.readouterr().out


def test_parse_args_modes():
    args = main.parse_args(["daemon", "c.ini", "--dummy"])
    assert (args.mode, args.dummy, args.notify_current) == ("daemon", True, None)
    assert main.parse_args(["stop", "c.ini"]).mode == "stop"
    assert main.parse_args(["config", "c.ini"]).mode == "config"
    assert main.parse_args(["status", "c.ini", "--json"]).json is True
    assert main.parse_args(["reconnect", "c.ini"]).mode == "reconnect"
    with pytest.raises(SystemExit):
        main.parse_args([])


def _ini(tmp_path) -> str:
    ini = tmp_path / "c.ini"
    ini.write_text(f"[DATA]\nmcr_news_url = http://x\n[DAEMON]\nlock_file = {tmp_path / 'd.lock'}\n"
                   "[INSTRUMENT:PEARL]\nnotify_counts = 130\n")
    return str(ini)


def run_main(*argv):
    """main.main() with the given command line, and logging setup stubbed out."""
    with patch.object(main.sys, "argv", ["main.py", *argv]), patch("main.configure_logging"):
        main.main()


def test_main_logs_config_warnings_once_logging_is_set_up(tmp_path, caplog):
    ini = Path(_ini(tmp_path))
    ini.write_text(ini.read_text() + "[NOTIFICATIONS]\nfun_mode = true\n")
    order = []
    with patch("main.configure_logging", side_effect=lambda *a: order.append("logging")), \
            patch("main.run_stop", new_callable=AsyncMock), patch.object(main.sys, "argv", ["main.py", "stop", str(ini)]):
        caplog.handler.emit = lambda record, emit=caplog.handler.emit: (order.append("warning"), emit(record))
        main.main()
    assert order[0] == "logging" and set(order[1:]) == {"warning"} and "[NOTIFICATIONS] fun_mode" in caplog.text


def test_main_reports_config_error(tmp_path, capsys):
    with pytest.raises(SystemExit):
        run_main("stop", str(tmp_path / "missing.ini"))
    assert "Configuration error" in capsys.readouterr().out


def test_main_daemon_refuses_second_instance(tmp_path, capsys):
    with SingleInstanceLock(tmp_path / "d.lock"), patch("main.run_daemon") as run_daemon:
        with pytest.raises(SystemExit):
            run_main("daemon", _ini(tmp_path))
    run_daemon.assert_not_called()
    assert "Lock file already held" in capsys.readouterr().out


def test_configure_logging_sets_the_daemons_level(tmp_path):
    with patch("main.logging.basicConfig") as basic:
        main.configure_logging("daemon", _config(tmp_path, log_level="DEBUG", log_file=""))
    assert basic.call_args.kwargs["level"] == "DEBUG"


@pytest.mark.parametrize("mode, log_file, handlers", [
    ("daemon", "", [logging.StreamHandler]),
    ("daemon", "monitor.log", [logging.StreamHandler, main.RotatingFileHandler]),
    ("stop", "monitor.log", None),  # basicConfig's default: stderr
])
def test_only_the_daemon_writes_the_log_file(tmp_path, mode, log_file, handlers):
    config = _config(tmp_path, log_file=log_file and str(tmp_path / log_file))
    with patch("main.logging.basicConfig") as basic:
        main.configure_logging(mode, config)
    given = basic.call_args.kwargs.get("handlers")
    assert (given and [type(h) for h in given]) == handlers
    for h in given or []:
        h.close()
    assert (tmp_path / "monitor.log").exists() is (main.RotatingFileHandler in (handlers or []))


@pytest.mark.parametrize("mode, target", [
    ("daemon", "run_daemon"), ("stop", "run_stop"), ("config", "run_config"),
    ("status", "run_status"), ("reconnect", "run_reconnect"),
])
def test_main_dispatches_each_mode(tmp_path, mode, target):
    with patch(f"main.{target}", new_callable=AsyncMock, return_value=False) as runner:
        run_main(mode, _ini(tmp_path))
    runner.assert_awaited_once()


def test_main_reads_the_push_secret_only_for_the_daemon(tmp_path, capsys):
    ini = _ini(tmp_path)
    with open(ini, "a") as f:
        f.write(f"[PUSH]\nurl = http://127.0.0.1:1/ingest\nsecret_file = {tmp_path / 'missing'}\n")
    with patch("main.run_stop", new_callable=AsyncMock) as run_stop:
        run_main("stop", ini)
    run_stop.assert_awaited_once()
    with patch("main.run_daemon", new_callable=AsyncMock) as run_daemon, pytest.raises(SystemExit):
        run_main("daemon", ini)
    run_daemon.assert_not_called()
    assert "secret_file could not be read" in capsys.readouterr().out


def test_main_keyboard_interrupt_exits_quietly(tmp_path, capsys):
    with patch("main.run_stop", new_callable=AsyncMock, side_effect=KeyboardInterrupt):
        run_main("stop", _ini(tmp_path))
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


def _file_args(tmp_path):
    return argparse.Namespace(dummy=True, notify_current=False, config=_daemon_ini(tmp_path))


@contextlib.asynccontextmanager
async def daemon_with_file(tmp_path):
    args = _file_args(tmp_path)
    config = _config(tmp_path)
    async with daemon(config, args) as (task, _stop):
        yield task, Client(config.daemon_socket_path), args.config


class Client:
    """Sends each request on a connection of its own, as the CLI does."""

    def __init__(self, path):
        self.path = Path(path)

    def request(self, payload):
        return ipc_request(self.path, payload)


async def _revision(client) -> str:
    return (await client.request({"method": "get_config"}))["revision"]


async def test_daemon_get_config_reads_the_file(tmp_path):
    async with daemon_with_file(tmp_path) as (task, client, ini):
        reply = await client.request({"method": "get_config"})
    assert reply["config"]["instruments"][0]["name"] == "PEARL"
    assert reply["beam_targets"] == ["TS1", "TS2", "Muons"]
    assert reply["channel_modes"] == ["experiment", "instrument"]
    assert len(reply["revision"]) == 64
    assert task.result() is False  # plain stop, no restart


async def test_daemon_update_config_requires_revision(tmp_path):
    async with daemon_with_file(tmp_path) as (task, client, ini):
        reply = await client.request({"method": "update_config", "settings": {}})
        assert (reply["ok"], reply["error"]) == (False, "invalid_request")
        assert not task.done()


async def test_daemon_rejects_update_based_on_stale_read(tmp_path):
    """A hand edit (or another editor's save) since get_config must not be overwritten."""
    async with daemon_with_file(tmp_path) as (task, client, ini):
        revision = await _revision(client)
        ini.write_text(ini.read_text() + "[INSTRUMENT:WISH]\nnotify_counts = 5\n")
        reply = await client.request({
            "method": "update_config", "revision": revision, "settings": {"notifications": {"debounce_seconds": "30"}},
        })
        assert (reply["ok"], reply["error"]) == (False, "config_changed")
        assert "[INSTRUMENT:WISH]" in ini.read_text() and "debounce_seconds" not in ini.read_text()
        assert not task.done()


async def test_concurrent_updates_only_one_is_applied(tmp_path):
    """Two editors saving at once from the same read: the edits are serialised,
    and the loser gets restart_pending (or the connection closes as the
    daemon restarts) rather than overwriting the winner's change."""
    async with daemon_with_file(tmp_path) as (task, client, ini):
        revision = await _revision(client)
        results = await asyncio.gather(
            client.request({"method": "update_config", "revision": revision,
                            "settings": {"notifications": {"debounce_seconds": "30"}}}),
            client.request({"method": "update_config", "revision": revision,
                            "settings": {"notifications": {"summary_time": "09:30"}}}),
            return_exceptions=True,
        )
        assert await asyncio.wait_for(task, 5) is True

    winners = [r for r in results if isinstance(r, dict) and r.get("restarting")]
    losers = [r for r in results if r not in winners]
    assert len(winners) == 1
    assert isinstance(losers[0], ConnectionError) or losers[0]["error"] == "restart_pending"
    text = ini.read_text()
    assert ("debounce_seconds = 30" in text) != ("summary_time = 09:30" in text)


async def test_config_command_edits_and_saves_over_ipc(tmp_path, capsys):
    async with daemon_with_file(tmp_path) as (task, _client, ini):
        with patch("builtins.input", side_effect=["1", "UTC", "s", "y"]):
            await main.run_config(_config(tmp_path))
        assert await asyncio.wait_for(task, 5) is True  # restarting to apply it
    assert "timezone = UTC" in ini.read_text()
    assert "Saved. The daemon is restarting to apply it." in capsys.readouterr().out


async def test_config_command_quits_without_saving_when_input_closes(tmp_path, capsys):
    async with daemon_with_file(tmp_path) as (task, _client, ini):
        before = ini.read_text()
        with patch("builtins.input", side_effect=["1", "UTC", EOFError]):
            await main.run_config(_config(tmp_path))
        assert not task.done()
    assert ini.read_text() == before
    assert "Input closed; your changes were discarded." in capsys.readouterr().out


async def test_config_command_without_a_daemon(tmp_path, capsys):
    await main.run_config(_config(tmp_path))
    assert "Could not reach the daemon" in capsys.readouterr().out


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
            "settings": {"notifications": {"debounce_seconds": "30"}},
        })
        assert reply == {"ok": True, "restarting": True}
        assert await asyncio.wait_for(task, 5) is True
    assert "debounce_seconds = 30" in ini.read_text()


def test_main_restarts_after_lock_is_released(tmp_path, no_exec):
    ini = _ini(tmp_path)
    lock_free = []

    def fake_exec(*_args):
        with SingleInstanceLock(tmp_path / "d.lock"):  # would raise if still held
            lock_free.append(True)

    no_exec.side_effect = fake_exec
    with patch.object(main.sys, "orig_argv", ["python3", "-u", "main.py", "daemon", ini]), \
         patch("main.run_daemon", new_callable=AsyncMock, return_value=True):
        run_main("daemon", ini)

    exe, argv, env = no_exec.call_args.args
    assert (exe, argv) == (main.sys.executable, [main.sys.executable, "-u", "main.py", "daemon", ini])
    assert env[main.RESTARTED_ENV] == "1"
    assert lock_free == [True]


def test_main_reports_failed_restart(tmp_path, no_exec, capsys):
    no_exec.side_effect = FileNotFoundError("no such interpreter")
    with patch("main.run_daemon", new_callable=AsyncMock, return_value=True), \
         pytest.raises(SystemExit) as exc_info:
        run_main("daemon", _ini(tmp_path))
    assert exc_info.value.code == 1
    assert "Failed to restart daemon: no such interpreter" in capsys.readouterr().err


@pytest.mark.parametrize("restarted, expected", [(False, True), (True, False)])
def test_restarted_daemon_does_not_replay_notify_current(tmp_path, monkeypatch, restarted, expected):
    if restarted:
        monkeypatch.setenv(main.RESTARTED_ENV, "1")
    with patch("main.run_daemon", new_callable=AsyncMock, return_value=False) as run_daemon:
        run_main("daemon", _ini(tmp_path), "-n")
    assert run_daemon.await_args.args[1].notify_current is expected
    assert main.RESTARTED_ENV not in os.environ




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
    pearl = InstrumentConfig("PEARL", 130.0, "TS1")
    store.upsert_snapshot("daemon_state", json.dumps({"instruments": {"PEARL": _saved_instrument(
        pearl, run_name="Run 9", run_started_at=started, counts=150.0, end_notified=True,
    )}}))
    store.commit()
    store.close()

    config = _config(tmp_path, instruments=[pearl])
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
    handlers = {}
    real_ipc = main.IPCServer

    def capture(*a):
        handlers["config"] = a[2]
        return real_ipc(*a)

    with patch("main.IPCServer", side_effect=capture):
        async with daemon(_config(tmp_path), _file_args(tmp_path)) as (task, stop):
            revision = (await handlers["config"]("get_config", {}))["revision"]
            stop.set()
            reply = await handlers["config"]("update_config", {"revision": revision, "settings": {}})
            assert await asyncio.wait_for(task, 5) is False
    assert (reply["ok"], reply["error"]) == (False, "restart_pending")


async def test_run_stop_gives_up_on_a_daemon_that_does_not_answer(tmp_path, capsys):
    with patch("isis_monitor.ipc.STOP_FLUSH_TIMEOUT", 0.05), patch("isis_monitor.ipc.REQUEST_TIMEOUT", 0.05):
        async with serving(tmp_path, command_handler=never_answers):
            with pytest.raises(SystemExit) as exc:
                await main.run_stop(_config(tmp_path))
    assert exc.value.code == 1
    assert "No reply to 'command' within" in capsys.readouterr().out


async def test_run_daemon_publishes_the_status_with_a_push_url(tmp_path):
    from isis_monitor.tests.test_status import SECRET, receiver
    async with receiver() as site:
        config = _config(tmp_path, push_url=str(site.make_url("/ingest")), push_secret=SECRET,
                         instruments=[InstrumentConfig("PEARL", 130.0, "TS1")])
        async with daemon(config, argparse.Namespace(dummy=False, notify_current=False)):
            await wait_until(lambda: site.received)
    assert site.received[0]["kind"] == "status" and site.received[0]["instruments"][0]["name"] == "PEARL"



@pytest.mark.parametrize("dummy, push_url", [(True, "http://127.0.0.1:9/ingest"), (False, "")])
async def test_run_daemon_publishes_no_status_when_dummy_or_without_a_push_url(tmp_path, dummy, push_url):
    config = _config(tmp_path, push_url=push_url, push_secret=b"s" * 32)
    with patch("main.StatusPublisher") as publisher:
        async with daemon(config, argparse.Namespace(dummy=dummy, notify_current=False)):
            pass
    publisher.assert_not_called()


async def test_a_failing_state_save_on_shutdown_still_closes_the_notifiers(tmp_path, caplog):
    config = _config(tmp_path, beam_teams_url="https://127.0.0.1:9/beam")
    with patch.object(main.Notifier, "close", autospec=True) as close, \
            patch.object(main.SQLiteStateStore, "upsert_snapshot", side_effect=sqlite3.OperationalError("disk I/O")):
        async with daemon(config, argparse.Namespace(dummy=False, notify_current=False)):
            pass
    assert close.await_count == 1 and "Failed to save the state or stop the IPC server" in caplog.text
