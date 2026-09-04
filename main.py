#!/usr/bin/env python3
import argparse
import asyncio
import contextlib
import fcntl
import json
import logging
import os
import signal
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from isis_monitor.beam import BeamMonitor
from isis_monitor.config import ConfigError, load_config
from isis_monitor.daemon_state import DaemonState
from isis_monitor.ipc import IPCClient, IPCServer
from isis_monitor.mcr import MCRNewsMonitor
from isis_monitor.notifiers import DummyNotifier, NotificationChannel, TeamsNotifier
from isis_monitor.storage import SQLiteStateStore
from isis_monitor.tui import RichTUI

logger = logging.getLogger("MAIN")


class StateLogHandler(logging.Handler):
    def __init__(self, state: DaemonState):
        super().__init__()
        self.state = state

    def emit(self, record):
        try:
            msg = self.format(record)
            self.state.update_log(msg)
        except Exception:
            self.handleError(record)


class SingleInstanceLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = None

    def __enter__(self):
        self._fh = self.path.open("w")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Lock file already held: {self.path}") from exc
        self._fh.seek(0)
        self._fh.truncate(0)
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fh:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self._fh.close()
        with contextlib.suppress(OSError):
            self.path.unlink()


def configure_logging(log_file: str, log_level: str, max_bytes: int, backup_count: int) -> None:
    log_path = Path(log_file)
    if not log_path.is_absolute():
        log_path = Path(__file__).parent / log_path
    numeric_level = getattr(logging, log_level.upper(), logging.WARNING)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            RotatingFileHandler(log_path, maxBytes=max_bytes, backupCount=backup_count)
        ],
    )


def install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _on_signal():
        stop_event.set()
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal)


def build_channels(config, dummy: bool):
    beam_channel = NotificationChannel("Beam Updates")
    exp_channel = NotificationChannel("Experiment Updates")
    mcr_channel = NotificationChannel("MCR News")

    if dummy:
        beam_channel.add_notifier(DummyNotifier())
        exp_channel.add_notifier(DummyNotifier())
        mcr_channel.add_notifier(DummyNotifier())
    else:
        if config.beam_teams_url:
            beam_channel.add_notifier(
                TeamsNotifier(config.beam_teams_url, timeout=config.webhook_timeout)
            )
        if config.experiment_teams_url:
            exp_channel.add_notifier(
                TeamsNotifier(config.experiment_teams_url, timeout=config.webhook_timeout)
            )
        if config.news_teams_url:
            mcr_channel.add_notifier(
                TeamsNotifier(config.news_teams_url, timeout=config.webhook_timeout)
            )
    return beam_channel, exp_channel, mcr_channel


async def close_channels(*channels: NotificationChannel) -> None:
    to_close = []
    for channel in channels:
        for notifier in channel.notifiers:
            close_fn = getattr(notifier, "close", None)
            if close_fn is not None:
                to_close.append(close_fn())
    if to_close:
        await asyncio.gather(*to_close, return_exceptions=True)


async def state_persistence_loop(config, state: DaemonState, store: SQLiteStateStore, stop_event: asyncio.Event):
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.sample_interval)
            break
        except asyncio.TimeoutError:
            pass

        ts = datetime.now(timezone.utc)
        state.sample_all_currents(ts)
        store.write_samples(state.get_beam_rows_for_timestamp(ts))

        cutoff = state.cutoff_for_days(config.retention_days)
        store.prune_older_than(cutoff)
        state.trim_history_before(cutoff)

        store.upsert_snapshot("daemon_state", json.dumps(state.snapshot()))
        for component, status in state.get_health().items():
            store.upsert_health(component, status)
        store.commit()


async def daemon_heartbeat_loop(config, state: DaemonState, stop_event: asyncio.Event):
    while not stop_event.is_set():
        state.update_health("daemon", "running")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.heartbeat_interval)
        except asyncio.TimeoutError:
            continue


async def run_daemon(config, args, stop_event: asyncio.Event):
    install_signal_handlers(stop_event)

    state = DaemonState(history_maxlen=max(config.history_maxlen, int((86400 * config.retention_days) / max(config.sample_interval, 1.0))))
    state.update_health("daemon", "starting")

    store = SQLiteStateStore(Path(config.daemon_db_path))
    state.restore_from_snapshot_json(store.load_snapshot("daemon_state"))
    cutoff = state.cutoff_for_days(config.retention_days)
    for row in store.load_recent_samples(cutoff):
        state.append_beam_sample(
            beam=str(row["target"]),
            current=float(row["current"]),
            power=str(row["power"]),
            ts=datetime.fromisoformat(str(row["timestamp"])),
            publish=False,
        )

    state_log_handler = StateLogHandler(state)
    state_log_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logging.getLogger().addHandler(state_log_handler)

    beam_channel, exp_channel, mcr_channel = build_channels(config, args.dummy)
    beam_monitor = BeamMonitor(
        config,
        beam_channel,
        exp_channel,
        args.notify_counts,
        sink=state,
    )
    mcr_monitor = MCRNewsMonitor(
        config,
        mcr_channel,
        args.notify_current,
        sink=state,
    )

    async def command_handler(name: str) -> dict:
        if name in {"force_reconnect", "force_reconnect_all"}:
            return {
                "beam": beam_monitor.request_reconnect(),
                "mcr": mcr_monitor.request_reconnect(),
            }
        if name == "force_reconnect_beam":
            return {"beam": beam_monitor.request_reconnect()}
        if name == "force_reconnect_mcr":
            return {"mcr": mcr_monitor.request_reconnect()}
        return {"error": "unknown_command", "name": name}

    ipc_server = IPCServer(Path(config.daemon_socket_path), state, command_handler)
    await ipc_server.start()
    state.update_health("daemon", "running")

    try:
        await asyncio.gather(
            beam_monitor.run(stop_event),
            mcr_monitor.run(stop_event),
            state_persistence_loop(config, state, store, stop_event),
            daemon_heartbeat_loop(config, state, stop_event),
        )
    finally:
        state.update_health("daemon", "stopping")
        store.upsert_snapshot("daemon_state", json.dumps(state.snapshot()))
        store.commit()
        store.close()
        await ipc_server.stop()
        await close_channels(beam_channel, exp_channel, mcr_channel)


def _apply_snapshot_to_tui(tui: RichTUI, snapshot: dict) -> None:
    beam_states = snapshot.get("beam_states", {})
    for beam in ("TS1", "TS2", "Muons"):
        state = beam_states.get(beam)
        if state:
            tui.update_beam_state(beam, float(state.get("current", 0.0)), str(state.get("power", "unknown")))
    tui.set_history_snapshot(snapshot.get("history", {}))
    if snapshot.get("mcr_news"):
        tui.update_mcr_news(str(snapshot["mcr_news"]))
    for line in snapshot.get("logs", [])[-20:]:
        tui.update_log(str(line))


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
        ts = datetime.fromisoformat(str(ts_raw))
        tui.add_history_sample(
            str(payload.get("beam", "")),
            ts,
            float(payload.get("current", 0.0)),
            str(payload.get("power", "unknown")),
        )
    elif ev == "health":
        comp = str(payload.get("component", ""))
        status = str(payload.get("status", ""))
        tui.update_log(f"Health: {comp} -> {status}")


async def tui_command_loop(client: IPCClient, stop_event: asyncio.Event, tui: RichTUI):
    while not stop_event.is_set():
        cmd = (await asyncio.to_thread(input, "Command [r=reconnect,q=quit]: ")).strip().lower()
        if cmd == "q":
            stop_event.set()
            return
        if cmd == "r":
            response = await client.request({"method": "command", "name": "force_reconnect_all"})
            tui.update_log(f"Reconnect request result: {response.get('result')}")


async def run_tui(config, stop_event: asyncio.Event):
    install_signal_handlers(stop_event)

    tui = RichTUI(
        history_maxlen=config.history_maxlen,
        sample_interval=config.sample_interval,
        refresh_per_second=config.refresh_per_second,
        logs_maxlen=config.logs_maxlen,
    )
    tui.start()

    backoff = config.tui_reconnect_initial
    try:
        while not stop_event.is_set():
            client = IPCClient(Path(config.tui_socket_path))
            cmd_task: Optional[asyncio.Task] = None
            try:
                tui.update_connection_state("connecting")
                await client.connect()
                tui.update_connection_state("connected")

                snapshot_resp = await client.request({"method": "get_snapshot"})
                if snapshot_resp.get("ok"):
                    _apply_snapshot_to_tui(tui, snapshot_resp.get("snapshot", {}))

                sub_resp = await client.request({"method": "subscribe_updates"})
                if sub_resp.get("ok"):
                    tui.update_log("Subscribed to daemon updates.")

                cmd_task = asyncio.create_task(tui_command_loop(client, stop_event, tui))
                backoff = config.tui_reconnect_initial

                async for message in client.iter_events():
                    _apply_event_to_tui(tui, message)
                    if stop_event.is_set():
                        break
            except (FileNotFoundError, ConnectionError, OSError) as exc:
                tui.update_connection_state("disconnected")
                tui.update_log(f"Daemon connection lost: {exc}")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(config.tui_reconnect_max, max(config.tui_reconnect_initial, backoff * 2))
            finally:
                if cmd_task:
                    cmd_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await cmd_task
                await client.close()
    finally:
        tui.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ISIS Beam and MCR News Monitor")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    daemon_parser = subparsers.add_parser("daemon", help="Run the long-lived daemon process")
    daemon_parser.add_argument("config", type=Path, help="Path to .ini configuration file")
    daemon_parser.add_argument(
        "-nc", "--notify_counts", type=float, default=130, help="Counts threshold for notification"
    )
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

    return parser.parse_args()


def main():
    args = parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Configuration error: {e}")
        raise SystemExit(1)

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
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nStopping monitors...")


if __name__ == "__main__":
    main()
