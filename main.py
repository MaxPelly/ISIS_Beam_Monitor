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
from typing import Awaitable, Callable, Optional

from isis_monitor.beam import BeamMonitor
from isis_monitor.config_editor import run_config_editor
from isis_monitor.config import (
    BEAM_TARGET_KEYS,
    CHANNEL_LABELS,
    CHANNEL_MODES,
    ConfigChangedError,
    ConfigError,
    config_revision,
    editable_settings,
    load_config,
    update_config_file,
)
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
# Set in the environment of a daemon that re-exec'd itself (see restart_process).
RESTARTED_ENV = "ISIS_MONITOR_RESTARTED"
LOOP_STOP_TIMEOUT = 5.0  # seconds run_daemon's loops get to exit after stop_event
IPC_REQUEST_TIMEOUT = 10.0  # for the TUI's sync and `stop`; a stuck daemon mustn't hang them


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
    """Every sample_interval: sample beam currents into history (while the
    beam feed is connected) and persist them."""
    while True:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.sample_interval)
            return
        except asyncio.TimeoutError:
            pass

        ts = datetime.now(timezone.utc)
        # While the beam feed is down the values are stale, and sampling them
        # would count as beam time in the daily summary; leave a gap instead.
        rows = state.sample_all_currents(ts) if state.health["beam"] == "connected" else []
        cutoff = ts - timedelta(days=config.retention_days)
        state.trim_history_before(cutoff)
        snap = json.dumps(state.snapshot())

        def _persist():
            store.write_samples(rows)
            store.prune_older_than(cutoff)
            store.upsert_snapshot("daemon_state", snap)
            store.commit()

        try:
            await store.run(_persist)
        except sqlite3.Error:
            logger.exception("Failed to persist daemon state; will retry next interval")


async def run_daemon(config, args, stop_event: asyncio.Event) -> bool:
    """Run until stopped; returns True if the daemon should restart itself."""
    install_signal_handlers(stop_event)

    samples_for_retention = int(86400 * config.retention_days / config.sample_interval)
    state = DaemonState(
        history_maxlen=max(config.history_maxlen, samples_for_retention),
        instruments=config.instruments,
    )  # health starts as "starting"

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
        sink=state,
    )
    beam_monitor.restore_instruments(state.instruments)
    mcr_monitor = MCRNewsMonitor(config, mcr_channel, args.notify_current, sink=state)
    restart_requested = False

    def request_restart(reason: str) -> None:
        nonlocal restart_requested
        logger.warning(f"{reason}; restarting daemon")
        restart_requested = True
        stop_event.set()

    async def command_handler(name: str) -> dict:
        if name == "force_reconnect_all":
            return {"beam": beam_monitor.request_reconnect(), "mcr": mcr_monitor.request_reconnect()}
        if name == "force_reconnect_beam":
            return {"beam": beam_monitor.request_reconnect()}
        if name == "force_reconnect_mcr":
            return {"mcr": mcr_monitor.request_reconnect()}
        if name == "shutdown":
            stop_event.set()
            return {"shutdown": "ok"}
        if name == "restart":
            request_restart("Restart requested over IPC")
            return {"restart": "ok"}
        return {"error": "unknown_command", "name": name}

    def read_config() -> dict:
        # Read from the file rather than the running config, so edits build
        # on anything changed by hand since the daemon started.
        return {
            "config": editable_settings(load_config(args.config)),
            "revision": config_revision(args.config),
            "beam_targets": list(BEAM_TARGET_KEYS),
            "channel_modes": list(CHANNEL_MODES),
        }

    config_lock = asyncio.Lock()  # one read or edit of the file at a time

    async def config_handler(method: str, req: dict) -> dict:
        async with config_lock:
            try:
                if method == "get_config":
                    return await asyncio.to_thread(read_config)
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

    ipc_server = IPCServer(Path(config.daemon_socket_path), state, command_handler, config_handler)
    tasks: list = []
    try:
        await ipc_server.start()
        state.update_health("daemon", "running")
        tasks = [asyncio.ensure_future(coro) for coro in (
            run_until_stopped(beam_monitor.run(), stop_event),
            run_until_stopped(mcr_monitor.run(), stop_event),
            state_persistence_loop(config, state, store, stop_event),
            daily_summary_loop(config, state, store, beam_channel, stop_event, rng=random.Random()),
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
        logger.warning("Shutting down daemon")
        state.update_health("daemon", "stopping")
        await ipc_server.stop()
        snap = json.dumps(state.snapshot())

        def _close_db():
            store.upsert_snapshot("daemon_state", snap)
            store.commit()
            store.close()

        await store.run(_close_db)
        await asyncio.gather(*(ch.close() for ch in channels))
        logging.getLogger().removeHandler(state_log_handler)
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
        raise SystemExit(1)


def _apply_snapshot_to_tui(tui: RichTUI, snapshot: dict) -> None:
    beam_states = snapshot.get("beam_states", {})
    for beam in CHANNEL_LABELS:
        state = beam_states.get(beam)
        if state:
            tui.update_beam_state(beam, float(state.get("current", 0.0)), str(state.get("power", "unknown")))
    if snapshot.get("mcr_news"):
        tui.update_mcr_news(str(snapshot["mcr_news"]))
    if isinstance(snapshot.get("instruments"), dict):
        tui.set_instruments(snapshot["instruments"])


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
    elif ev == "run":
        tui.update_instrument(str(payload.get("instrument", "")), run_name=str(payload.get("run_name", "")))
    elif ev == "counts":
        tui.update_instrument(str(payload.get("instrument", "")), counts=float(payload.get("counts", -1.0)))
    elif ev == "health":
        tui.update_log(f"Health: {payload.get('component', '')} -> {payload.get('status', '')}")


async def _sync_tui(client: IPCClient, tui: RichTUI, history_limit: int) -> None:
    # Subscribe first, so nothing that happens during the sync is missed;
    # events carry absolute values, so replaying one after the snapshot is harmless.
    sub_resp = await client.request({"method": "subscribe_updates"}, timeout=IPC_REQUEST_TIMEOUT)

    snapshot_resp = await client.request({"method": "get_snapshot"}, timeout=IPC_REQUEST_TIMEOUT)
    if snapshot_resp.get("ok"):
        _apply_snapshot_to_tui(tui, snapshot_resp.get("snapshot", {}))

    history_resp = await client.request(
        {"method": "get_history", "limit": history_limit}, timeout=IPC_REQUEST_TIMEOUT
    )
    if history_resp.get("ok"):
        tui.set_history_snapshot(history_resp.get("history", {}))

    logs_resp = await client.request({"method": "get_logs"}, timeout=IPC_REQUEST_TIMEOUT)
    if logs_resp.get("ok"):
        for line in logs_resp.get("logs", [])[-20:]:
            tui.update_log(str(line))

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


def _track(task: asyncio.Task, tasks: set) -> None:
    tasks.add(task)  # keep a reference so the task isn't garbage-collected
    task.add_done_callback(tasks.discard)
    task.add_done_callback(_log_task_failure)


def _log_task_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("TUI action failed", exc_info=task.exception())


def handle_tui_key(
    ch: str,
    client: Optional[IPCClient],
    stop_event: asyncio.Event,
    tui: RichTUI,
    tasks: set,
    edit_config: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    ch = ch.lower()
    if ch == "q":
        stop_event.set()
    elif ch in ("r", "c"):
        if client is None:
            tui.update_log("Not connected to the daemon.")
        elif ch == "r":
            _track(asyncio.create_task(_send_reconnect(client, tui)), tasks)
        elif edit_config is not None:
            _track(asyncio.create_task(edit_config()), tasks)


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
    typed_ahead = ""  # keys read in the same chunk as "c", owed to the editor

    # stdin is read with os.read rather than through sys.stdin, whose buffer
    # could swallow typed-ahead keys (so the reader never fires for them) or
    # block the event loop waiting for a newline.
    def on_key() -> None:
        nonlocal typed_ahead
        data = os.read(fd, 64)
        if not data:  # stdin closed
            stop_event.set()
            return
        text = data.decode(errors="ignore")
        for i, ch in enumerate(text):
            handle_tui_key(ch, client, stop_event, tui, key_tasks, edit_config)
            if ch.lower() == "c" and client is not None:
                typed_ahead = text[i + 1:]  # the editor now owns the terminal
                return

    async def read_line(prompt: str) -> Optional[str]:
        """Read one line without blocking the event loop (so the daemon
        connection stays alive and quitting can cancel it); None on EOF."""
        nonlocal typed_ahead
        sys.stdout.write(prompt)
        if "\n" in typed_ahead:
            text, typed_ahead = typed_ahead.split("\n", 1)
            print(text)
            return text
        typed_ahead = ""  # a partial line can't be shown or edited, so drop it
        sys.stdout.flush()
        line: asyncio.Future = loop.create_future()

        def ready() -> None:
            if not line.done():
                # In canonical mode one read returns at most one line.
                nonlocal typed_ahead
                data = os.read(fd, 4096)
                if not data:
                    line.set_result(None)
                    return
                # Usually one line, but keys queued while switching terminal
                # modes arrive together; keep the rest for the next prompts.
                text, _, typed_ahead = data.decode(errors="replace").partition("\n")
                line.set_result(text)

        loop.add_reader(fd, ready)
        try:
            return await line
        finally:
            loop.remove_reader(fd)

    async def request(payload: dict) -> dict:
        # Whichever client is live now: the connection may have been
        # replaced (e.g. by a daemon restart) while the editor was open.
        if client is None:
            raise RuntimeError("Not connected to the daemon")
        return await client.request(payload)

    async def edit_config() -> None:
        # Hand the terminal over to the line-based editor, then take it back.
        nonlocal typed_ahead
        loop.remove_reader(fd)
        tui.stop()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        try:
            await run_config_editor(request, read_line, print)
        finally:
            typed_ahead = ""  # not owed to the next editor session
            tty.setcbreak(fd)
            if not stop_event.is_set():  # otherwise run_tui is tearing down anyway
                tui.start()
                loop.add_reader(fd, on_key)

    loop.add_reader(fd, on_key)
    try:
        await run_until_stopped(tui_connection_loop(config, tui, set_client), stop_event)
    finally:
        for task in list(key_tasks):  # e.g. an open config editor
            task.cancel()
        await asyncio.gather(*key_tasks, return_exceptions=True)
        loop.remove_reader(fd)
        tui.stop()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


async def run_stop(config) -> None:
    """Connect to a running daemon via IPC and request a clean shutdown."""
    client = IPCClient(Path(config.daemon_socket_path))
    try:
        await asyncio.wait_for(client.connect(), IPC_REQUEST_TIMEOUT)
    except (OSError, asyncio.TimeoutError) as exc:  # distinct classes before Python 3.11
        print(f"Could not connect to daemon at {config.daemon_socket_path}: {exc!r}")
        raise SystemExit(1)

    try:
        response = await client.request({"method": "command", "name": "shutdown"}, timeout=IPC_REQUEST_TIMEOUT)
    except TimeoutError:
        print(f"The daemon didn't answer within {IPC_REQUEST_TIMEOUT:.0f}s; it may be stuck.")
        raise SystemExit(1)
    finally:
        await client.close()
    if not response.get("ok"):
        print(f"Daemon returned an error: {response.get('error')}")
        raise SystemExit(1)
    result = response.get("result", {})
    if result.get("shutdown") == "ok":
        print("Shutdown signal sent — daemon is stopping cleanly.")
    else:
        print(f"Daemon responded: {result}")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ISIS Beam and MCR News Monitor")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    for mode, help_text in (
        ("daemon", "Run the long-lived daemon process"),
        ("tui", "Run the TUI client attached to daemon"),
        ("stop", "Gracefully shut down a running daemon"),
    ):
        subparsers.add_parser(mode, help=help_text).add_argument(
            "config", type=Path, help="Path to .ini configuration file")
    daemon_parser = subparsers.choices["daemon"]
    daemon_parser.add_argument("-n", "--notify_current", action=argparse.BooleanOptionalAction,
                               help="Send a notification for the current news immediately.")
    daemon_parser.add_argument("-d", "--dummy", action=argparse.BooleanOptionalAction,
                               help="Use a dummy notifier that logs to console instead of sending webhooks.")

    return parser.parse_args(argv)


def main():
    args = parse_args()
    if os.environ.pop(RESTARTED_ENV, None) and args.mode == "daemon":
        # -n means "post the current news now", not on every config apply.
        args.notify_current = False

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Configuration error: {e}")
        raise SystemExit(1)

    set_timezone(config.notifications_timezone)

    configure_logging(config.log_file, config.log_level, config.log_max_bytes, config.log_backup_count)

    stop_event = asyncio.Event()

    try:
        if args.mode == "daemon":
            with SingleInstanceLock(Path(config.daemon_lock_file)):
                restart = asyncio.run(run_daemon(config, args, stop_event))
            if restart:  # after the lock and database are released
                restart_process()
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
