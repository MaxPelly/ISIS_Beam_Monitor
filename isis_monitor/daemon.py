"""The daemon: runs the monitors, keeps and saves the state, and answers the CLI over IPC."""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiohttp

from isis_monitor import __version__
from isis_monitor.beam import BeamMonitor
from isis_monitor.config import (
    BEAM_NAMES,
    CHANNEL_MODES,
    ConfigChangedError,
    ConfigError,
    editable_settings,
    read_config,
    update_config_file,
)
from isis_monitor.daemon_state import HISTORY_WINDOW, MCR_NEWS_PLACEHOLDER, DaemonState
from isis_monitor.ipc import IPCServer
from isis_monitor.mcr import MCRNewsMonitor
from isis_monitor.messages import Notification
from isis_monitor.notifiers import (
    DummyNotifier,
    NotificationChannel,
    Notifier,
    TeamsNotifier,
    WebhookNotifier,
)
from isis_monitor.status import StatusPublisher
from isis_monitor.status import build as build_status
from isis_monitor.storage import SQLiteStateStore
from isis_monitor.summary import daily_summary_loop

logger = logging.getLogger(__name__)

# Set in the environment of a daemon that re-exec'd itself (see restart_process).
RESTARTED_ENV = "ISIS_MONITOR_RESTARTED"
LOOP_STOP_TIMEOUT = 5.0  # seconds run_daemon's loops get to exit after stop_event
CLOSE_TIMEOUT = 5.0  # seconds the notifiers get to send what's queued, on shutdown


class LockHeldError(RuntimeError):
    """Another daemon already holds the lock file."""


class SingleInstanceLock:
    """Holds an exclusive flock on `path` for the lifetime of the daemon.

    The file is deliberately never deleted: unlinking a lock file lets a
    second process lock the orphaned inode while a third creates a new one.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            fh.close()
            raise LockHeldError(f"Lock file already held: {self.path}") from exc
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fh:
            self._fh.close()  # releases the flock
            self._fh = None


def install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)


async def run_until_stopped(coro, stop_event: asyncio.Event) -> None:
    """Run `coro` until it returns or `stop_event` is set, then cancel it.

    An exception raised by `coro` itself propagates to the caller.
    """
    task = asyncio.ensure_future(coro)
    stopper = asyncio.ensure_future(stop_event.wait())
    try:
        await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopper.cancel()
        task.cancel()
    await asyncio.wait({task})
    if not task.cancelled():
        task.result()


def build_channels(config, dummy: bool, session: Optional[aiohttp.ClientSession] = None):
    """The beam, experiment and MCR channels. Each picks the Teams channel
    name that downstream flows route on: the beam target's, the instrument's
    (with `channel = instrument`) or else the NotificationChannel's own.

    Each Teams webhook gets its own notifier; one dummy or push notifier is
    shared by all three, so the push feed keeps its order. The HTTP
    notifiers share `session`, which the caller closes."""
    own_channel = {i.name for i in config.instruments if i.channel == "instrument"}

    def experiment_route(n: Notification) -> str:
        return n.topic if n.topic in own_channel else "Experiment Updates"

    shared: Optional[Notifier] = DummyNotifier() if dummy else None
    if not dummy and config.push_url:
        shared = WebhookNotifier(config.push_url, config.push_secret, timeout=config.push_timeout, session=session)

    def channel(name: str, url: str, route=None) -> NotificationChannel:
        ch = NotificationChannel(name, route)
        if url and not dummy:
            ch.add_notifier(TeamsNotifier(url, timeout=config.webhook_timeout, session=session))
        if shared:
            ch.add_notifier(shared)
        return ch

    return (
        channel("Beam Updates", config.beam_teams_url, lambda n: n.beam),
        channel("Experiment Updates", config.experiment_teams_url, experiment_route),
        channel("MCR News", config.news_teams_url),
    )


async def state_persistence_loop(config, state: DaemonState, store: SQLiteStateStore, stop_event: asyncio.Event):
    """Every sample_interval: sample beam currents into history (while the
    beam feed is connected) and persist them."""
    while True:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.sample_interval)
            return
        except TimeoutError:
            pass

        try:
            ts = datetime.now(timezone.utc)
            # While the beam feed is down the values are stale, and sampling them
            # would count as beam time in the daily summary; leave a gap instead.
            rows = state.sample_all_currents(ts) if state.health["beam"] == "connected" else []
            cutoff = ts - timedelta(days=config.retention_days)
            state.trim_history_before(ts - HISTORY_WINDOW)
            snap = json.dumps(state.snapshot())

            def _persist(rows, cutoff, snap):
                store.write_samples(rows)
                store.prune_older_than(cutoff)
                store.upsert_snapshot("daemon_state", snap)
                store.commit()

            await store.run(_persist, rows, cutoff, snap)
        except Exception:  # not just sqlite3.Error: no failure here may stop the daemon
            logger.exception("Failed to persist daemon state; will retry next interval")


async def run_daemon(config, args, stop_event: asyncio.Event) -> bool:
    """Run until stopped; returns True if the daemon should restart itself."""
    install_signal_handlers(stop_event)

    state = DaemonState(instrument_names=[inst.name for inst in config.instruments])  # health starts as "starting"

    def _init_db():
        store = SQLiteStateStore(Path(config.daemon_db_path))
        cutoff = datetime.now(timezone.utc) - HISTORY_WINDOW
        return store, store.load_snapshot("daemon_state"), store.load_recent_samples(cutoff)

    store, raw_snap, recent_samples = await asyncio.to_thread(_init_db)
    state.restore_from_snapshot_json(raw_snap)
    for row in recent_samples:
        state.append_beam_sample(
            beam=str(row["target"]),
            current=float(row["current"]),
            power=str(row["power"]),
            ts=datetime.fromisoformat(str(row["timestamp"])),
        )
    logger.info(f"ISIS Beam Monitor v{__version__} starting")

    session = aiohttp.ClientSession()  # for every HTTP notifier
    channels = build_channels(config, args.dummy, session)
    beam_channel, exp_channel, mcr_channel = channels
    beam_monitor = BeamMonitor(
        config,
        beam_channel,
        exp_channel,
        sink=state,
    )
    beam_monitor.restore_instruments(state.saved_instruments)
    state.instruments = beam_monitor.instrument_views
    mcr_monitor = MCRNewsMonitor(
        config, mcr_channel, args.notify_current, sink=state,
        old_news=None if state.mcr_news == MCR_NEWS_PLACEHOLDER else state.mcr_news,
    )
    restart_requested = False

    def request_restart(reason: str) -> None:
        nonlocal restart_requested
        logger.warning(f"{reason}; restarting daemon")
        restart_requested = True
        stop_event.set()

    async def command_handler(name: str) -> dict:
        if name == "force_reconnect_all":
            return {"beam": beam_monitor.request_reconnect(), "mcr": mcr_monitor.request_reconnect()}
        if name == "shutdown":
            stop_event.set()
            return {"shutdown": "ok"}
        return {"error": "unknown_command", "name": name}

    def current_config() -> dict:
        # Read from the file rather than the running config, so edits build
        # on anything changed by hand since the daemon started.
        file_config, revision = read_config(args.config)
        return {
            "config": editable_settings(file_config),
            "revision": revision,
            "beam_targets": list(BEAM_NAMES),
            "channel_modes": list(CHANNEL_MODES),
        }

    config_lock = asyncio.Lock()  # one read or edit of the file at a time

    async def config_handler(method: str, req: dict) -> dict:
        async with config_lock:
            try:
                if method == "get_config":
                    return await asyncio.to_thread(current_config)
                if restart_requested or stop_event.is_set():
                    # A save accepted during a shutdown would turn it into a restart.
                    return {"ok": False, "error": "restart_pending",
                            "detail": "The daemon is already stopping or restarting"}
                if not isinstance(req.get("revision"), str):
                    return {"ok": False, "error": "invalid_request", "detail": "revision from get_config is required"}
                await asyncio.to_thread(update_config_file, args.config, req.get("settings"), req["revision"])
            except ConfigChangedError as exc:
                return {"ok": False, "error": "config_changed", "detail": str(exc)}
            except ConfigError as exc:
                return {"ok": False, "error": "invalid_config", "detail": str(exc)}
            except OSError as exc:
                return {"ok": False, "error": "config_write_failed", "detail": str(exc)}
            request_restart(f"Config file {args.config} updated over IPC")
            return {"restarting": True}

    def current_status() -> dict:
        # interval_s is the interval it's really sent at, which the site judges staleness by.
        return build_status(state, {name: b.since for name, b in beam_monitor.beams.items()},
                            max(config.sample_interval, StatusPublisher.DEBOUNCE))

    loops = [beam_monitor.run, mcr_monitor.run]  # each run until stop_event
    if config.push_url and not args.dummy:
        publisher = StatusPublisher(
            config.push_url, config.push_secret, current_status, config.sample_interval, session, config.push_timeout,
        )
        state.on_change = publisher.poke
        loops.append(publisher.run)

    ipc_server = IPCServer(Path(config.daemon_socket_path), command_handler, config_handler, current_status)
    tasks: list = []
    try:
        await ipc_server.start()
        state.update_health("daemon", "running")
        tasks = [asyncio.ensure_future(coro) for coro in (
            *(run_until_stopped(run(), stop_event) for run in loops),
            state_persistence_loop(config, state, store, stop_event),
            daily_summary_loop(config, state, store, beam_channel, stop_event),
        )]
        await asyncio.gather(*tasks)
    finally:
        # If one loop crashed, gather() raised while the others kept running;
        # stop them before the store and channels they use are closed. They
        # exit on stop_event by themselves (letting monitors close cleanly);
        # cancel any that don't within LOOP_STOP_TIMEOUT.
        stop_event.set()
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=LOOP_STOP_TIMEOUT)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Shutting down daemon")
        state.update_health("daemon", "stopping")
        try:
            snap = json.dumps(state.snapshot())

            def _close_db():
                store.upsert_snapshot("daemon_state", snap)
                store.commit()
                store.close()

            await store.run(_close_db)
            await ipc_server.stop()
        except Exception:  # still send what's queued and close the rest
            logger.exception("Failed to save the state or stop the IPC server")
        # Each notifier once (the push notifier is in every channel), all
        # within one CLOSE_TIMEOUT.
        notifiers = dict.fromkeys(n for ch in channels for n in ch.notifiers)
        results = await asyncio.gather(*(n.close(CLOSE_TIMEOUT) for n in notifiers), return_exceptions=True)
        for notifier, result in zip(notifiers, results, strict=True):
            if isinstance(result, Exception):
                logger.error(f"Closing {type(notifier).__name__} failed: {result!r}")
        await session.close()
    return restart_requested


def restart_process() -> None:
    """Replace this process with a fresh copy of itself (same PID, same
    arguments), e.g. to pick up an edited config file."""
    logging.shutdown()
    sys.stdout.flush()  # exec discards anything still buffered
    sys.stderr.flush()
    try:
        os.execve(sys.executable, [sys.executable, *sys.orig_argv[1:]], {**os.environ, RESTARTED_ENV: "1"})
    except OSError as exc:  # e.g. the interpreter was removed by a venv rebuild
        print(f"Failed to restart daemon: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
