import argparse
import asyncio
import contextlib
import json
import os
import signal
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from isis_monitor import cli, daemon, messages
from isis_monitor.config import DEFAULT_BEAMS, InstrumentConfig
from isis_monitor.daemon import SingleInstanceLock
from isis_monitor.daemon_state import DaemonState
from isis_monitor.instrument import InstrumentTracker
from isis_monitor.ipc import ipc_request
from isis_monitor.notifiers import DummyNotifier, TeamsNotifier, WebhookNotifier
from isis_monitor.storage import SQLiteStateStore
from isis_monitor.tests.helpers import (
    FakePVWS,
    app_config,
    fake_channel,
    run_cli,
    wait_until,
    write_ini,
)


@pytest.fixture(autouse=True)
def no_exec():
    """A real exec would replace the pytest process itself."""
    with patch("isis_monitor.daemon.os.execve", side_effect=AssertionError("os.execve called in a test")) as execve:
        yield execve


def test_single_instance_lock(tmp_path):
    lock_file = tmp_path / "sub" / "test.lock"
    with SingleInstanceLock(lock_file):
        assert lock_file.read_text().strip() == str(os.getpid())
        with pytest.raises(daemon.LockHeldError, match="Lock file already held"):
            with SingleInstanceLock(lock_file):
                pass
    # The file is kept (unlinking lock files is racy) but the lock is released.
    assert lock_file.exists()
    with SingleInstanceLock(lock_file):
        pass


# ---------------------------------------------------------------------------
# run_until_stopped / signals
# ---------------------------------------------------------------------------

async def test_run_until_stopped():
    cancelled = asyncio.Event()

    async def forever():
        try:
            await asyncio.sleep(3600)
        finally:
            cancelled.set()

    # Setting the stop event cancels it.
    stop = asyncio.Event()
    task = asyncio.create_task(daemon.run_until_stopped(forever(), stop))
    await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert cancelled.is_set()

    # Cancelling run_until_stopped itself propagates to the coroutine.
    cancelled.clear()
    task = asyncio.create_task(daemon.run_until_stopped(forever(), asyncio.Event()))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(cancelled.wait(), 1)

    # The coroutine's own errors propagate, and returning ends it.
    async def boom():
        raise ValueError("crashed")

    async def quick():
        return 1

    with pytest.raises(ValueError, match="crashed"):
        await daemon.run_until_stopped(boom(), asyncio.Event())
    await asyncio.wait_for(daemon.run_until_stopped(quick(), asyncio.Event()), 1)


async def test_install_signal_handlers_sets_stop_event():
    stop = asyncio.Event()
    with patch.object(asyncio.get_running_loop(), "add_signal_handler") as add:
        daemon.install_signal_handlers(stop)
    assert [c.args[0] for c in add.call_args_list] == [signal.SIGINT, signal.SIGTERM]
    add.call_args_list[1].args[1]()  # as SIGTERM would, without signalling pytest
    assert stop.is_set()


# ---------------------------------------------------------------------------
# build_channels
# ---------------------------------------------------------------------------

def _saved_instrument(inst: InstrumentConfig, **changes) -> dict:
    """A snapshot entry for `inst`, as its tracker saves one, with `changes`."""
    tracker = InstrumentTracker(inst, fake_channel(), lambda _target: "unknown", stall_minutes=10)
    return {**json.loads(json.dumps(tracker.to_snapshot())), **changes}


def test_build_channels_only_configured_webhooks_or_dummy_everywhere(tmp_path):
    beam, exp, mcr = daemon.build_channels(
        app_config(tmp_path, beam_teams_url="http://beam", news_teams_url="http://news", webhook_timeout=3),
        dummy=False,
    )
    assert [n.webhook_url for n in beam.notifiers] == ["http://beam"]
    assert beam.notifiers[0].timeout == 3
    assert exp.notifiers == []
    assert isinstance(mcr.notifiers[0], TeamsNotifier)

    for ch in daemon.build_channels(app_config(tmp_path, beam_teams_url="http://x"), dummy=True):
        assert [type(n) for n in ch.notifiers] == [DummyNotifier]


def test_build_channels_shares_one_push_webhook_and_session(tmp_path):
    session = MagicMock()
    channels = daemon.build_channels(
        app_config(tmp_path, beam_teams_url="http://beam", push_url="http://push", push_secret=b"s" * 32, push_timeout=1),
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
    config = app_config(
        tmp_path, beam_teams_url="http://beam", experiment_teams_url="http://exp", news_teams_url="http://news",
        instruments=[InstrumentConfig("PEARL", 100, "TS1"), InstrumentConfig("WISH", 100, "TS2", channel="instrument")],
    )
    beam, exp, mcr = daemon.build_channels(config, dummy=False)
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
    config = app_config(tmp_path, sample_interval=0.01, retention_days=2)
    state = DaemonState()
    state.update_health("beam", "connected")
    state.update_beam_state("TS1", 150.0, "high")
    now = datetime.now(timezone.utc)
    stale, yesterday = now - timedelta(days=3), now - timedelta(hours=25)
    for ts in (stale, yesterday):
        state.append_beam_sample("TS2", 1.0, "low", ts=ts)
    store = SQLiteStateStore(tmp_path / "state.db")
    store.write_samples([(stale, "TS2", 1.0, "low"), (yesterday, "TS2", 1.0, "low")])
    store.commit()
    stop = asyncio.Event()

    task = asyncio.create_task(daemon.state_persistence_loop(config, state, store, stop))
    await wait_until(lambda: len(state.history["TS1"]) >= 2)
    stop.set()
    await asyncio.wait_for(task, 1)

    rows = store.load_recent_samples(stale - timedelta(days=1))
    assert [r["target"] for r in rows if r["timestamp"] <= yesterday.isoformat()] == ["TS2"]  # kept 2 days
    assert {r["target"] for r in rows if r["timestamp"] > yesterday.isoformat()} == {"TS1"}  # TS2, Muons: no reading
    assert not state.history["TS2"]  # only the last 24h kept in memory
    assert json.loads(store.load_snapshot("daemon_state"))["beam_states"]["TS1"]["power"] == "high"
    store.close()


async def test_state_persistence_loop_samples_only_while_the_beam_feed_is_connected(tmp_path):
    config = app_config(tmp_path, sample_interval=0.01)
    state = DaemonState()
    state.update_beam_state("TS1", 150.0, "high")  # e.g. restored, beam health still "unknown"
    store = SQLiteStateStore(tmp_path / "state.db")
    stop = asyncio.Event()
    ticks = []  # one per pass of the loop, which saves a snapshot each time
    snapshot = state.snapshot
    state.snapshot = lambda: (ticks.append(1), snapshot())[1]

    task = asyncio.create_task(daemon.state_persistence_loop(config, state, store, stop))
    try:
        await wait_until(lambda: ticks)
        assert not state.history["TS1"]
        state.update_health("beam", "connected")
        await wait_until(lambda: len(state.history["TS1"]) >= 1)
        state.update_health("beam", "disconnected")
        passes = len(ticks)
        await wait_until(lambda: len(ticks) >= passes + 1)  # a pass already under way may still sample
        sampled = len(state.history["TS1"])
        await wait_until(lambda: len(ticks) >= passes + 4)
        assert len(state.history["TS1"]) == sampled
    finally:
        stop.set()
        await asyncio.wait_for(task, 1)
        store.close()


async def test_state_persistence_loop_survives_database_errors(tmp_path, caplog):
    config = app_config(tmp_path, sample_interval=0.01)
    store = MagicMock(spec=SQLiteStateStore)
    store.write_samples.side_effect = [sqlite3.OperationalError("disk I/O error"), None, None, None]
    store.run = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    stop = asyncio.Event()

    task = asyncio.create_task(daemon.state_persistence_loop(config, DaemonState(), store, stop))
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


def _saved_snapshot(db) -> dict:
    store = SQLiteStateStore(db)
    try:
        return json.loads(store.load_snapshot("daemon_state"))
    finally:
        store.close()


DAEMON_ARGS = argparse.Namespace(dummy=True, notify_current=False)
LIVE_ARGS = argparse.Namespace(dummy=False, notify_current=False)


@contextlib.asynccontextmanager
async def running_daemon(config, args=DAEMON_ARGS):
    stop = asyncio.Event()
    with patch("isis_monitor.daemon.install_signal_handlers"):
        task = asyncio.create_task(daemon.run_daemon(config, args, stop))
        try:
            await wait_until(lambda: os.path.exists(config.daemon_socket_path))
            yield task, stop
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)


async def test_run_daemon_serves_ipc_and_shuts_down_cleanly_with_a_client_attached(tmp_path):
    update = {"type": "update", "pv": DEFAULT_BEAMS[0].pv, "value": 150.0}
    async with FakePVWS([update]) as pvws:
        config = app_config(tmp_path, isis_websocket_url=pvws.url, sample_interval=0.02)
        async with running_daemon(config) as (task, _stop):
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

            bogus = await request({"method": "command", "name": "bogus"})
            assert bogus == {"ok": False, "error": "unknown_command", "name": "bogus"}

            _reader, attached = await asyncio.open_unix_connection(config.daemon_socket_path)
            shutdown = await request({"method": "command", "name": "shutdown"})
            assert shutdown["result"] == {"shutdown": "ok"}
            await asyncio.wait_for(task, 5)  # exits even though a client is still connected
            attached.close()

    assert not os.path.exists(config.daemon_socket_path)
    assert _saved_snapshot(tmp_path / "state.db")["beam_states"]["TS1"]["power"] == "high"


async def test_run_daemon_teardown_closes_each_notifier_once_then_the_session_whatever_fails(tmp_path, caplog):
    """A failing state save, IPC server stop or notifier close still leaves the rest closed."""
    config = app_config(tmp_path, beam_teams_url="http://127.0.0.1:9/beam", push_url="http://127.0.0.1:9/ingest",
                        push_secret=b"s" * 32)
    order = []

    def close(notifier, _timeout):
        order.append(notifier)
        if type(notifier) is TeamsNotifier:
            raise RuntimeError("stuck")

    with patch.object(daemon.Notifier, "close", autospec=True, side_effect=close), \
            patch.object(daemon.aiohttp.ClientSession, "close", autospec=True,
                         side_effect=lambda s: order.append("session")), \
            patch.object(daemon.SQLiteStateStore, "upsert_snapshot", side_effect=sqlite3.OperationalError("disk I/O")), \
            patch.object(daemon.IPCServer, "stop", side_effect=OSError("gone")) as stop_ipc:
        async with running_daemon(config, LIVE_ARGS):
            pass
    # (The MCR monitor closes a session of its own earlier.)
    assert [type(n) for n in order if n != "session"] == [TeamsNotifier, WebhookNotifier]
    assert order[-1] == "session"
    assert "Failed to save the state" in caplog.text and "Failed to stop the IPC server" in caplog.text
    stop_ipc.assert_awaited_once()  # despite the failed save
    assert "Closing TeamsNotifier failed: RuntimeError('stuck')" in caplog.text


async def test_run_daemon_restores_history_and_state_on_restart(tmp_path):
    ts = datetime.now(timezone.utc) - timedelta(minutes=5)
    store = SQLiteStateStore(tmp_path / "state.db")
    pearl = InstrumentConfig("PEARL", 130.0, "TS1")
    saved = _saved_instrument(pearl, run_name="Run 30", run_started_at=ts.isoformat(), counts=12.5, rb_number="A",
                              end_notified=True)
    store.write_samples([(ts, "TS2", 7.0, "low")])
    store.upsert_snapshot("daemon_state", json.dumps({"mcr_news": "old news", "instruments": {"PEARL": saved}}))
    store.commit()
    store.close()

    config = app_config(tmp_path, instruments=[pearl])
    seeded = {}
    real_restore = daemon.BeamMonitor.restore_instruments

    def spy(self, saved):
        real_restore(self, saved)
        seeded.update({name: vars(t.state).copy() for name, t in self.instruments.items()})

    with patch("isis_monitor.daemon.MCRNewsMonitor", wraps=daemon.MCRNewsMonitor) as mcr_cls, \
            patch.object(daemon.BeamMonitor, "restore_instruments", spy):
        async with running_daemon(config):
            status = (await ipc_request(Path(config.daemon_socket_path), {"method": "get_status"}))["status"]
            reconnect = (await ipc_request(Path(config.daemon_socket_path),
                                           {"method": "command", "name": "force_reconnect_all"}))["result"]

    assert status["mcr"]["news"] == "old news"
    assert mcr_cls.call_args.kwargs["old_news"] == "old news"
    (inst,) = status["instruments"]
    assert (inst["name"], inst["title"], inst["rb"], inst["beam"]) == ("PEARL", "Run 30", "A", "TS1")
    # The trackers are seeded with all they saved, not just what the status shows.
    assert seeded["PEARL"]["run_name"] == "Run 30" and seeded["PEARL"]["end_notified"] is True
    assert [v for v, _ in status["beams"][1]["history"]["values"] if v is not None] == [7.0]
    assert set(reconnect) == {"beam", "mcr"}


async def test_run_daemon_tears_down_when_a_loop_crashes_or_ipc_fails_to_start(tmp_path):
    persistence_cancelled = asyncio.Event()

    async def persistence(*_args):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            persistence_cancelled.set()
            raise

    config = app_config(tmp_path)
    with patch("isis_monitor.daemon.install_signal_handlers"), \
         patch("isis_monitor.daemon.LOOP_STOP_TIMEOUT", 0.1), \
         patch("isis_monitor.daemon.state_persistence_loop", side_effect=persistence), \
         patch("isis_monitor.daemon.daily_summary_loop", new_callable=AsyncMock, side_effect=RuntimeError("summary bug")):
        with pytest.raises(RuntimeError, match="summary bug"):
            await asyncio.wait_for(daemon.run_daemon(config, DAEMON_ARGS, asyncio.Event()), 5)
    assert persistence_cancelled.is_set()  # the other loops are stopped
    assert not os.path.exists(config.daemon_socket_path)  # teardown still ran

    # With no IPC server there are no loops to stop, but the state is still saved.
    (tmp_path / "state.db").unlink()
    with patch("isis_monitor.daemon.install_signal_handlers"), \
         patch.object(daemon.IPCServer, "start", side_effect=OSError("address in use")):
        with pytest.raises(OSError, match="address in use"):
            await asyncio.wait_for(daemon.run_daemon(config, DAEMON_ARGS, asyncio.Event()), 5)
    assert _saved_snapshot(tmp_path / "state.db")["mcr_news"]


async def test_run_daemon_publishes_the_status_with_a_push_url(tmp_path):
    from isis_monitor.tests.test_status import SECRET, receiver
    async with receiver() as site:
        config = app_config(tmp_path, push_url=str(site.make_url("/ingest")), push_secret=SECRET,
                            instruments=[InstrumentConfig("PEARL", 130.0, "TS1")])
        async with running_daemon(config, LIVE_ARGS):
            await wait_until(lambda: site.received)
    assert site.received[0]["kind"] == "status" and site.received[0]["instruments"][0]["name"] == "PEARL"


async def test_a_unix_push_url_posts_over_that_socket(tmp_path):
    from aiohttp import web

    from isis_monitor.tests.test_status import SECRET
    received = []

    async def ingest(request):
        received.append((request.path, json.loads(await request.read())))
        return web.Response(status=204)
    app = web.Application()
    app.router.add_post("/ingest/status", ingest)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.UnixSite(runner, str(tmp_path / "ingest.sock")).start()
    try:
        config = app_config(tmp_path, push_url=f"unix:{tmp_path / 'ingest.sock'}", push_secret=SECRET)
        async with running_daemon(config, LIVE_ARGS):
            await wait_until(lambda: received)
    finally:
        await runner.cleanup()
    assert received[0][0] == "/ingest/status" and received[0][1]["kind"] == "status"
    session = MagicMock()
    push = daemon.push_session(config, session)
    try:
        (notifier,) = {n for ch in daemon.build_channels(config, False, session, push) for n in ch.notifiers}
        assert (notifier.webhook_url, notifier._session) == ("http://localhost/ingest", push)
    finally:
        await push.close()


@pytest.mark.parametrize("dummy, push_url", [(True, "http://127.0.0.1:9/ingest"), (False, "")])
async def test_run_daemon_publishes_no_status_when_dummy_or_without_a_push_url(tmp_path, dummy, push_url):
    config = app_config(tmp_path, push_url=push_url, push_secret=b"s" * 32)
    with patch("isis_monitor.daemon.StatusPublisher") as publisher:
        async with running_daemon(config, argparse.Namespace(dummy=dummy, notify_current=False)):
            pass
    publisher.assert_not_called()


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
    config = app_config(tmp_path)
    async with running_daemon(config, args) as (task, _stop):
        yield task, Client(config.daemon_socket_path), args.config


class Client:
    """Sends each request on a connection of its own, as the CLI does."""

    def __init__(self, path):
        self.path = Path(path)

    def request(self, payload):
        return ipc_request(self.path, payload)


async def _revision(client) -> str:
    return (await client.request({"method": "get_config"}))["revision"]


async def test_daemon_answers_requests_that_leave_it_running(tmp_path, capsys):
    """One daemon run for every request that must neither change the file nor restart it."""
    async with daemon_with_file(tmp_path) as (task, client, ini):
        before = ini.read_text()

        await cli.run_status(app_config(tmp_path))
        assert capsys.readouterr().out.startswith("ISIS Beam Monitor v")
        await cli.run_status(app_config(tmp_path), as_json=True)
        status = json.loads(capsys.readouterr().out)
        assert status["kind"] == "status" and [b["name"] for b in status["beams"]] == ["TS1", "TS2", "Muons"]

        reply = await client.request({"method": "get_config"})
        assert reply["config"]["instruments"][0]["name"] == "PEARL"
        assert reply["beam_targets"] == ["TS1", "TS2", "Muons"]
        assert reply["channel_modes"] == ["experiment", "instrument"]
        assert len(reply["revision"]) == 64
        revision = reply["revision"]

        reply = await client.request({"method": "update_config", "settings": {}})
        assert (reply["ok"], reply["error"]) == (False, "invalid_request")  # no revision

        reply = await client.request({"method": "update_config", "revision": revision, "settings": {"instruments": []}})
        assert (reply["ok"], reply["error"]) == (False, "invalid_config")
        assert "non-empty" in reply["detail"]

        with patch("isis_monitor.daemon.update_config_file", side_effect=PermissionError("read-only")):
            reply = await client.request({"method": "update_config", "revision": "r", "settings": {}})
        assert (reply["ok"], reply["error"], reply["detail"]) == (False, "config_write_failed", "read-only")

        with patch("builtins.input", side_effect=["1", "UTC", EOFError]):
            await cli.run_config(app_config(tmp_path))
        assert "Input closed; your changes were discarded." in capsys.readouterr().out
        assert ini.read_text() == before

        # A hand edit (or another editor's save) since get_config must not be overwritten.
        ini.write_text(before + "[INSTRUMENT:WISH]\nnotify_counts = 5\n")
        reply = await client.request({
            "method": "update_config", "revision": revision, "settings": {"notifications": {"debounce_seconds": "30"}},
        })
        assert (reply["ok"], reply["error"]) == (False, "config_changed")
        assert "[INSTRUMENT:WISH]" in ini.read_text() and "debounce_seconds" not in ini.read_text()

        # A file broken by hand: get_config says why.
        ini.write_text("[DATA]\n[DATA]\n")
        reply = await client.request({"method": "get_config"})
        assert (reply["ok"], reply["error"]) == (False, "invalid_config") and "Could not parse" in reply["detail"]

        assert not task.done()
    assert task.result() is False  # plain stop, no restart


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
        assert await asyncio.wait_for(task, 5) is True  # restarting to apply it

    winners = [r for r in results if isinstance(r, dict) and r.get("restarting")]
    losers = [r for r in results if r not in winners]
    assert winners == [{"ok": True, "restarting": True}]
    assert isinstance(losers[0], ConnectionError) or losers[0]["error"] == "restart_pending"
    text = ini.read_text()
    assert ("debounce_seconds = 30" in text) != ("summary_time = 09:30" in text)


async def test_config_command_edits_and_saves_over_ipc(tmp_path, capsys):
    async with daemon_with_file(tmp_path) as (task, _client, ini):
        with patch("builtins.input", side_effect=["1", "UTC", "s", "y"]):
            await cli.run_config(app_config(tmp_path))
        assert await asyncio.wait_for(task, 5) is True  # restarting to apply it
    assert "timezone = UTC" in ini.read_text()
    assert "Saved. The daemon is restarting to apply it." in capsys.readouterr().out


async def test_config_save_is_refused_once_shutdown_has_started(tmp_path):
    """A save accepted after SIGTERM or a shutdown command would re-exec the daemon."""
    handlers = {}
    real_ipc = daemon.IPCServer

    def capture(*a):
        handlers["config"] = a[2]
        return real_ipc(*a)

    with patch("isis_monitor.daemon.IPCServer", side_effect=capture):
        async with running_daemon(app_config(tmp_path), _file_args(tmp_path)) as (task, stop):
            revision = (await handlers["config"]("get_config", {}))["revision"]
            stop.set()
            reply = await handlers["config"]("update_config", {"revision": revision, "settings": {}})
            assert await asyncio.wait_for(task, 5) is False
    assert (reply["ok"], reply["error"]) == (False, "restart_pending")


def test_main_restarts_after_lock_is_released(tmp_path, no_exec):
    ini = write_ini(tmp_path)
    lock_free = []

    def fake_exec(*_args):
        with SingleInstanceLock(tmp_path / "d.lock"):  # would raise if still held
            lock_free.append(True)

    no_exec.side_effect = fake_exec
    with patch.object(cli.sys, "orig_argv", ["python3", "-u", "isis-monitor", "daemon", ini]), \
         patch("isis_monitor.cli.run_daemon", new_callable=AsyncMock, return_value=True):
        run_cli("daemon", ini)

    exe, argv, env = no_exec.call_args.args
    assert (exe, argv) == (cli.sys.executable, [cli.sys.executable, "-u", "isis-monitor", "daemon", ini])
    assert env[daemon.RESTARTED_ENV] == "1"
    assert lock_free == [True]


def test_main_reports_failed_restart(tmp_path, no_exec, capsys):
    no_exec.side_effect = FileNotFoundError("no such interpreter")
    with patch("isis_monitor.cli.run_daemon", new_callable=AsyncMock, return_value=True), \
         pytest.raises(SystemExit) as exc_info:
        run_cli("daemon", write_ini(tmp_path))
    assert exc_info.value.code == 1
    assert "Failed to restart daemon: no such interpreter" in capsys.readouterr().err


@pytest.mark.parametrize("restarted, expected", [(False, True), (True, False)])
def test_restarted_daemon_does_not_replay_notify_current(tmp_path, monkeypatch, restarted, expected):
    if restarted:
        monkeypatch.setenv(daemon.RESTARTED_ENV, "1")
    with patch("isis_monitor.cli.run_daemon", new_callable=AsyncMock, return_value=False) as run_daemon:
        run_cli("daemon", write_ini(tmp_path), "-n")
    assert run_daemon.await_args.args[1].notify_current is expected
    assert daemon.RESTARTED_ENV not in os.environ
