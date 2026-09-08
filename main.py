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
import sys
import tty
import termios

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
        self._fh = self.path.open("a+")
        self._fh.seek(0)
        pid_str = self._fh.read().strip()
        if pid_str.isdigit():
            try:
                os.kill(int(pid_str), 0)
            except ProcessLookupError:
                pass
            except PermissionError:
                raise RuntimeError(f"Lock held by another user's process: {pid_str}")
            else:
                # If flock is supported, let flock do its job. But if not, we rely on this check.
                pass
        
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Lock file already held: {self.path}") from exc
        except OSError:
            # Fallback for systems without flock: rely on the PID check
            if pid_str.isdigit():
                try:
                    os.kill(int(pid_str), 0)
                    raise RuntimeError(f"Lock file already held by PID {pid_str}")
                except ProcessLookupError:
                    pass

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
        
        beam_rows = state.get_beam_rows_for_timestamp(ts)
        cutoff = state.cutoff_for_days(config.retention_days)
        state.trim_history_before(cutoff)
        snap = json.dumps(state.snapshot())
        health = state.get_health()

        def _persist():
            store.write_samples(beam_rows)
            store.prune_older_than(cutoff)
            store.upsert_snapshot("daemon_state", snap)
            for component, status in health.items():
                store.upsert_health(component, status)
            store.commit()

        await asyncio.to_thread(_persist)
    logger.warning("State Persistance quit")
    return


async def daemon_heartbeat_loop(config, state: DaemonState, stop_event: asyncio.Event):
    while not stop_event.is_set():
        state.update_health("daemon", "running")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.heartbeat_interval)
        except asyncio.TimeoutError:
            continue
    logger.warning("Heartbeat quit")
    return


async def run_daemon(config, args, stop_event: asyncio.Event):
    install_signal_handlers(stop_event)

    state = DaemonState(history_maxlen=max(config.history_maxlen, int((86400 * config.retention_days) / max(config.sample_interval, 1.0))))
    state.update_health("daemon", "starting")

    def _init_db():
        store = SQLiteStateStore(Path(config.daemon_db_path))
        raw_snap = store.load_snapshot("daemon_state")
        cutoff = state.cutoff_for_days(config.retention_days)
        recent = store.load_recent_samples(cutoff)
        return store, raw_snap, recent

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
        if name == "shutdown":
            stop_event.set()
            return {"shutdown": "ok"}
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
        logger.warning("Shutting down daemon")
        state.update_health("daemon", "stopping")
        snap = json.dumps(state.snapshot())
        
        def _close_db():
            store.upsert_snapshot("daemon_state", snap)
            store.commit()
            store.close()
            
        await asyncio.to_thread(_close_db)
        await ipc_server.stop()
        await close_channels(beam_channel, exp_channel, mcr_channel)


def _apply_snapshot_to_tui(tui: RichTUI, snapshot: dict) -> None:
    beam_states = snapshot.get("beam_states", {})
    for beam in ("TS1", "TS2", "Muons"):
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

def tui_command_handler(client: IPCClient, stop_event: asyncio.Event, tui: RichTUI):
    # Read a single character immediately without waiting for enter
    ch = sys.stdin.read(1)
    if ch.lower() == 'q':
        stop_event.set()
    elif ch.lower() == 'r':
        async def _send_reconnect():
            try:
                response = await client.request({"method": "command", "name": "force_reconnect_all"})
                tui.update_log(f"Reconnect request result: {response.get('result')}")
            except Exception as e:
                tui.update_log(f"Reconnect request failed: {e}")
        asyncio.create_task(_send_reconnect())



async def run_tui(config, stop_event: asyncio.Event):
    install_signal_handlers(stop_event)

    # Configure terminal to read keystrokes immediately
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
    loop = asyncio.get_running_loop()

    backoff = config.tui_reconnect_initial
    try:
        while not stop_event.is_set():
            client = IPCClient(Path(config.tui_socket_path))
            has_reader = False
            try:
                tui.update_connection_state("connecting")
                await client.connect()
                tui.update_connection_state("connected")

                snapshot_resp = await client.request({"method": "get_snapshot"})
                if snapshot_resp.get("ok"):
                    _apply_snapshot_to_tui(tui, snapshot_resp.get("snapshot", {}))
                    
                history_resp = await client.request({"method": "get_history"})
                if history_resp.get("ok"):
                    tui.set_history_snapshot(history_resp.get("history", {}))
                    
                logs_resp = await client.request({"method": "get_logs"})
                if logs_resp.get("ok"):
                    for line in logs_resp.get("logs", [])[-20:]:
                        tui.update_log(str(line))

                sub_resp = await client.request({"method": "subscribe_updates"})
                if sub_resp.get("ok"):
                    tui.update_log("Subscribed to daemon updates.")

                # Reset backoff only after successful sync
                backoff = config.tui_reconnect_initial

                loop.add_reader(sys.stdin.fileno(), tui_command_handler, client, stop_event, tui)
                has_reader = True

                # --- FIX: Race network events against stop_event ---
                event_iterator = client.iter_events().__aiter__()
                while not stop_event.is_set():
                    get_next_event = asyncio.create_task(event_iterator.__anext__())
                    wait_stop = asyncio.create_task(stop_event.wait())
                    
                    done, pending = await asyncio.wait(
                        [get_next_event, wait_stop],
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    
                    for task in pending:
                        task.cancel()
                        
                    if wait_stop in done:
                        break
                        
                    try:
                        message = get_next_event.result()
                        _apply_event_to_tui(tui, message)
                    except StopAsyncIteration:
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
                if has_reader:
                    loop.remove_reader(sys.stdin.fileno())
                await client.close()
    finally:
        tui.stop()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


async def run_stop(config) -> None:
    """Connect to a running daemon via IPC and request a clean shutdown."""
    client = IPCClient(Path(config.daemon_socket_path))
    try:
        await client.connect()
    except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
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

    stop_parser = subparsers.add_parser("stop", help="Gracefully shut down a running daemon")
    stop_parser.add_argument("config", type=Path, help="Path to .ini configuration file")

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
        elif args.mode == "stop":
            asyncio.run(run_stop(config))
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nStopping monitors...")


if __name__ == "__main__":
    main()
