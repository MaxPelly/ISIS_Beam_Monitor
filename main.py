#!/usr/bin/env python3
import argparse
import asyncio
import fcntl
import json
import logging
import os
import random
import signal
import sqlite3
import sys
import termios
import tty
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

from isis_monitor.beam import BeamMonitor, CHANNEL_LABELS
from isis_monitor.config import ConfigError, load_config
from isis_monitor.daemon_state import DaemonState
from isis_monitor.ipc import IPCClient, IPCServer
from isis_monitor.mcr import MCRNewsMonitor
from isis_monitor.messages import set_timezone
from isis_monitor.notifiers import DummyNotifier, NotificationChannel, TeamsNotifier
from isis_monitor.storage import SQLiteStateStore
from isis_monitor.summary import daily_summary_loop
from isis_monitor.tui import RichTUI

logger = logging.getLogger("MAIN")

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"


class StateLogHandler(logging.Handler):
    """Mirrors log records into DaemonState, and from there to attached TUIs.

    DaemonState is confined to the event loop, so records emitted from other
    threads (e.g. asyncio.to_thread workers) are handed over to `loop`.
    """

    def __init__(self, state: DaemonState, loop: Optional[asyncio.AbstractEventLoop] = None):
        super().__init__()
        self.state = state
        self.loop = loop

    def emit(self, record):
        try:
            msg = self.format(record)
            if self.loop is None or self.loop is _running_loop():
                self.state.update_log(msg)
            else:
                self.loop.call_soon_threadsafe(self.state.update_log, msg)
        except Exception:
            self.handleError(record)


def _running_loop() -> Optional[asyncio.AbstractEventLoop]:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


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
            raise RuntimeError(f"Lock file already held: {self.path}") from exc
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


def configure_logging(log_file: str, log_level: str, max_bytes: int, backup_count: int) -> None:
    log_path = Path(log_file)
    if not log_path.is_absolute():
        log_path = Path(__file__).parent / log_path
    numeric_level = getattr(logging, log_level.upper(), logging.WARNING)
    logging.basicConfig(
        level=numeric_level,
        format=LOG_FORMAT,
        handlers=[RotatingFileHandler(log_path, maxBytes=max_bytes, backupCount=backup_count)],
    )


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


def build_channels(config, dummy: bool):
    def channel(name: str, url: str) -> NotificationChannel:
        ch = NotificationChannel(name)
        if dummy:
            ch.add_notifier(DummyNotifier())
        elif url:
            ch.add_notifier(TeamsNotifier(url, timeout=config.webhook_timeout))
        return ch

    return (
        channel("Beam Updates", config.beam_teams_url),
        channel("Experiment Updates", config.experiment_teams_url),
        channel("MCR News", config.news_teams_url),
    )


async def state_persistence_loop(config, state: DaemonState, store: SQLiteStateStore, stop_event: asyncio.Event):
    """Every sample_interval: sample beam currents into history and persist them."""
    while True:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.sample_interval)
            return
        except asyncio.TimeoutError:
            pass

        ts = datetime.now(timezone.utc)
        rows = state.sample_all_currents(ts)
        cutoff = ts - timedelta(days=config.retention_days)
        state.trim_history_before(cutoff)
        snap = json.dumps(state.snapshot())

        def _persist():
            store.write_samples(rows)
            store.prune_older_than(cutoff)
            store.upsert_snapshot("daemon_state", snap)
            store.commit()

        try:
            await asyncio.to_thread(_persist)
        except sqlite3.Error:
            logger.exception("Failed to persist daemon state; will retry next interval")


async def run_daemon(config, args, stop_event: asyncio.Event):
    install_signal_handlers(stop_event)

    samples_for_retention = int(86400 * config.retention_days / max(config.sample_interval, 1.0))
    state = DaemonState(history_maxlen=max(config.history_maxlen, samples_for_retention))
    state.update_health("daemon", "starting")

    def _init_db():
        store = SQLiteStateStore(Path(config.daemon_db_path))
        cutoff = datetime.now(timezone.utc) - timedelta(days=config.retention_days)
        return store, store.load_snapshot("daemon_state"), store.load_recent_samples(cutoff)

    store, raw_snap, recent_samples = await asyncio.to_thread(_init_db)
    state.restore_from_snapshot_json(raw_snap)
    for row in recent_samples:
        state.append_beam_sample(
            beam=str(row["target"]),
            current=float(row["current"]),
            power=str(row["power"]),
            ts=datetime.fromisoformat(str(row["timestamp"])),
            publish=False,
        )

    state_log_handler = StateLogHandler(state, asyncio.get_running_loop())
    state_log_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.getLogger().addHandler(state_log_handler)

    channels = build_channels(config, args.dummy)
    beam_channel, exp_channel, mcr_channel = channels
    beam_monitor = BeamMonitor(
        config,
        beam_channel,
        exp_channel,
        config.instruments[0].notify_counts if config.instruments else config.notify_counts,
        sink=state,
        debounce_seconds=config.debounce_seconds,
    )
    mcr_monitor = MCRNewsMonitor(config, mcr_channel, args.notify_current, sink=state)

    async def command_handler(name: str) -> dict:
        if name in {"force_reconnect", "force_reconnect_all"}:
            return {"beam": beam_monitor.request_reconnect(), "mcr": mcr_monitor.request_reconnect()}
        if name == "force_reconnect_beam":
            return {"beam": beam_monitor.request_reconnect()}
        if name == "force_reconnect_mcr":
            return {"mcr": mcr_monitor.request_reconnect()}
        if name == "shutdown":
            stop_event.set()
            return {"shutdown": "ok"}
        return {"error": "unknown_command", "name": name}

    ipc_server = IPCServer(Path(config.daemon_socket_path), state, command_handler)
    try:
        await ipc_server.start()
        state.update_health("daemon", "running")
        await asyncio.gather(
            run_until_stopped(beam_monitor.run(), stop_event),
            run_until_stopped(mcr_monitor.run(), stop_event),
            state_persistence_loop(config, state, store, stop_event),
            daily_summary_loop(config, state, store, beam_channel, stop_event, rng=random.Random()),
        )
    finally:
        logger.warning("Shutting down daemon")
        state.update_health("daemon", "stopping")
        await ipc_server.stop()
        snap = json.dumps(state.snapshot())

        def _close_db():
            store.upsert_snapshot("daemon_state", snap)
            store.commit()
            store.close()

        await asyncio.to_thread(_close_db)
        await asyncio.gather(*(ch.close() for ch in channels))
        logging.getLogger().removeHandler(state_log_handler)


def _apply_snapshot_to_tui(tui: RichTUI, snapshot: dict) -> None:
    beam_states = snapshot.get("beam_states", {})
    for beam in CHANNEL_LABELS:
        state = beam_states.get(beam)
        if state:
            tui.update_beam_state(beam, float(state.get("current", 0.0)), str(state.get("power", "unknown")))
    if snapshot.get("mcr_news"):
        tui.update_mcr_news(str(snapshot["mcr_news"]))


def _apply_event_to_tui(tui: RichTUI, message: dict) -> None:
    ev = message.get("event")
    payload = message.get("payload", {})
    if ev == "beam":
        tui.update_beam_state(str(payload.get("beam", "")), float(payload.get("current", 0.0)), str(payload.get("power", "unknown")))
    elif ev == "mcr":
        tui.update_mcr_news(str(payload.get("news", "")))
    elif ev == "log":
        tui.update_log(str(payload.get("message", "")))
    elif ev == "sample":
        ts_raw = payload.get("timestamp")
        if not ts_raw:
            return
        tui.add_history_sample(
            str(payload.get("beam", "")),
            datetime.fromisoformat(str(ts_raw)),
            float(payload.get("current", 0.0)),
            str(payload.get("power", "unknown")),
        )
    elif ev == "health":
        tui.update_log(f"Health: {payload.get('component', '')} -> {payload.get('status', '')}")


async def _sync_tui(client: IPCClient, tui: RichTUI, history_limit: int) -> None:
    snapshot_resp = await client.request({"method": "get_snapshot"})
    if snapshot_resp.get("ok"):
        _apply_snapshot_to_tui(tui, snapshot_resp.get("snapshot", {}))

    history_resp = await client.request({"method": "get_history", "limit": history_limit})
    if history_resp.get("ok"):
        tui.set_history_snapshot(history_resp.get("history", {}))

    logs_resp = await client.request({"method": "get_logs"})
    if logs_resp.get("ok"):
        for line in logs_resp.get("logs", [])[-20:]:
            tui.update_log(str(line))

    sub_resp = await client.request({"method": "subscribe_updates"})
    if sub_resp.get("ok"):
        tui.update_log("Subscribed to daemon updates.")


async def tui_connection_loop(config, tui: RichTUI, on_client: Callable[[Optional[IPCClient]], None]) -> None:
    """Keep `tui` attached to the daemon, reconnecting with backoff, until cancelled.

    `on_client` is told the live client (or None) so key presses can send commands.
    """
    backoff = config.tui_reconnect_initial
    while True:
        client = IPCClient(Path(config.tui_socket_path))
        try:
            tui.update_connection_state("connecting")
            await client.connect()
            tui.update_connection_state("connected")
            await _sync_tui(client, tui, config.history_maxlen)
            backoff = config.tui_reconnect_initial
            on_client(client)
            async for message in client.iter_events():
                _apply_event_to_tui(tui, message)
        except (OSError, ValueError) as exc:
            tui.update_connection_state("disconnected")
            tui.update_log(f"Daemon connection lost: {exc}")
        finally:
            on_client(None)
            await client.close()
        await asyncio.sleep(backoff)
        backoff = min(config.tui_reconnect_max, backoff * 2)


async def _send_reconnect(client: IPCClient, tui: RichTUI) -> None:
    try:
        response = await client.request({"method": "command", "name": "force_reconnect_all"})
        tui.update_log(f"Reconnect request result: {response.get('result')}")
    except Exception as e:
        tui.update_log(f"Reconnect request failed: {e}")


def handle_tui_key(
    ch: str, client: Optional[IPCClient], stop_event: asyncio.Event, tui: RichTUI, tasks: set
) -> None:
    ch = ch.lower()
    if ch == "q":
        stop_event.set()
    elif ch == "r":
        if client is None:
            tui.update_log("Not connected to the daemon.")
            return
        task = asyncio.create_task(_send_reconnect(client, tui))
        tasks.add(task)  # keep a reference so the task isn't garbage-collected
        task.add_done_callback(tasks.discard)


async def run_tui(config, stop_event: asyncio.Event):
    install_signal_handlers(stop_event)

    # Read keystrokes immediately, without waiting for Enter
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    tty.setcbreak(fd)

    tui = RichTUI(
        history_maxlen=config.history_maxlen,
        sample_interval=config.sample_interval,
        refresh_per_second=config.refresh_per_second,
        logs_maxlen=config.logs_maxlen,
    )
    tui.start()

    client: Optional[IPCClient] = None
    key_tasks: set = set()

    def set_client(c: Optional[IPCClient]) -> None:
        nonlocal client
        client = c

    loop = asyncio.get_running_loop()
    loop.add_reader(fd, lambda: handle_tui_key(sys.stdin.read(1), client, stop_event, tui, key_tasks))
    try:
        await run_until_stopped(tui_connection_loop(config, tui, set_client), stop_event)
    finally:
        loop.remove_reader(fd)
        tui.stop()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


async def run_stop(config) -> None:
    """Connect to a running daemon via IPC and request a clean shutdown."""
    client = IPCClient(Path(config.daemon_socket_path))
    try:
        await client.connect()
    except OSError as exc:
        print(f"Could not connect to daemon at {config.daemon_socket_path}: {exc}")
        raise SystemExit(1)

    try:
        response = await client.request({"method": "command", "name": "shutdown"})
        if response.get("ok"):
            result = response.get("result", {})
            if result.get("shutdown") == "ok":
                print("Shutdown signal sent — daemon is stopping cleanly.")
            else:
                print(f"Daemon responded: {result}")
        else:
            print(f"Daemon returned an error: {response.get('error')}")
            raise SystemExit(1)
    finally:
        await client.close()


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ISIS Beam and MCR News Monitor")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    daemon_parser = subparsers.add_parser("daemon", help="Run the long-lived daemon process")
    daemon_parser.add_argument("config", type=Path, help="Path to .ini configuration file")
    daemon_parser.add_argument(
        "-n",
        "--notify_current",
        help="Send a notification for the current news immediately.",
        action=argparse.BooleanOptionalAction,
    )
    daemon_parser.add_argument(
        "-d",
        "--dummy",
        help="Use a dummy notifier that logs to console instead of sending webhooks.",
        action=argparse.BooleanOptionalAction,
    )

    tui_parser = subparsers.add_parser("tui", help="Run the TUI client attached to daemon")
    tui_parser.add_argument("config", type=Path, help="Path to .ini configuration file")

    stop_parser = subparsers.add_parser("stop", help="Gracefully shut down a running daemon")
    stop_parser.add_argument("config", type=Path, help="Path to .ini configuration file")

    return parser.parse_args(argv)


def main():
    args = parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Configuration error: {e}")
        raise SystemExit(1)

    set_timezone(config.notifications_timezone)

    configure_logging(
        config.log_file,
        config.log_level,
        config.log_max_bytes,
        config.log_backup_count,
    )

    stop_event = asyncio.Event()

    try:
        if args.mode == "daemon":
            with SingleInstanceLock(Path(config.daemon_lock_file)):
                asyncio.run(run_daemon(config, args, stop_event))
        elif args.mode == "tui":
            asyncio.run(run_tui(config, stop_event))
        elif args.mode == "stop":
            asyncio.run(run_stop(config))
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nStopping monitors...")


if __name__ == "__main__":
    main()
