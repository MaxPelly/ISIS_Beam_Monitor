"""The isis-monitor command: runs the daemon, or talks to a running one."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from isis_monitor import __version__
from isis_monitor.config import ConfigError, load_config, read_push_secret
from isis_monitor.config_editor import run_config_editor
from isis_monitor.daemon import RESTARTED_ENV, LockHeldError, SingleInstanceLock, restart_process, run_daemon
from isis_monitor.ipc import ipc_request
from isis_monitor.messages import set_timezone
from isis_monitor.status import render_text as render_status

logger = logging.getLogger("MAIN")

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
LOG_MAX_BYTES, LOG_BACKUP_COUNT = 5_000_000, 3


def configure_logging(mode: str, config) -> None:
    """Only the daemon writes the log: to stderr, and to `[LOGGING] file` if
    set. The other commands only show warnings, on stderr."""
    if mode != "daemon":
        logging.basicConfig(level=logging.WARNING, format=LOG_FORMAT)
        return
    handlers = [logging.StreamHandler()]
    if config.log_file:
        handlers.append(RotatingFileHandler(config.log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT))
    logging.basicConfig(level=config.log_level, format=LOG_FORMAT, handlers=handlers)


def fail(message: str):
    print(message, file=sys.stderr)
    raise SystemExit(1)


async def request_or_exit(config, payload: dict) -> dict:
    """The daemon's reply to `payload`; exits with 1 (and says why) if there's none."""
    try:
        return await ipc_request(Path(config.daemon_socket_path), payload)
    except (OSError, ValueError) as exc:  # including TimeoutError
        fail(f"No answer from the daemon at {config.daemon_socket_path}: {exc}")


async def run_stop(config) -> None:
    """Ask a running daemon to shut down cleanly."""
    response = await request_or_exit(config, {"method": "command", "name": "shutdown"})
    if not response.get("ok"):
        fail(f"Daemon returned an error: {response.get('error')}")
    result = response.get("result", {})
    if result.get("shutdown") == "ok":
        print("Shutdown signal sent — daemon is stopping cleanly.")
    else:
        print(f"Daemon responded: {result}")


async def run_status(config, as_json: bool = False) -> None:
    """Print the daemon's status: as the status page shows it, or its JSON."""
    response = await request_or_exit(config, {"method": "get_status"})
    if not response.get("ok"):
        fail(f"Daemon returned an error: {response.get('error')}")
    print(json.dumps(response["status"], indent=2, ensure_ascii=False) if as_json else render_status(response["status"]))


async def run_reconnect(config) -> None:
    """Make the daemon reconnect to PVWS and re-read the MCR news."""
    response = await request_or_exit(config, {"method": "command", "name": "force_reconnect_all"})
    result = response.get("result", {})
    if not response.get("ok") or set(result) != {"beam", "mcr"}:
        fail(f"Daemon returned an error: {response.get('error') or result}")
    for name, label in (("beam", "PVWS"), ("mcr", "MCR news")):
        print(f"{label}: {'reconnecting' if result[name] else 'already reconnecting'}")


async def run_config(config) -> None:
    """Edit the running daemon's config on the terminal; the daemon saves it and restarts."""
    # Let Ctrl-C interrupt input() at once; asyncio.run's handler would only
    # cancel the next request, e.g. the save the user just confirmed.
    signal.signal(signal.SIGINT, signal.default_int_handler)

    async def read_line(prompt: str) -> Optional[str]:
        # Blocking is fine: nothing else runs, and each request has its own connection.
        try:
            return input(prompt)
        except EOFError:
            return None

    if not await run_config_editor(lambda payload: ipc_request(Path(config.daemon_socket_path), payload), read_line, print):
        raise SystemExit(1)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ISIS Beam and MCR News Monitor")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    for mode, help_text in (
        ("daemon", "Run the long-lived daemon process"),
        ("config", "Edit the running daemon's instruments and notification settings"),
        ("status", "Print the running daemon's status"),
        ("reconnect", "Make the running daemon reconnect to PVWS and the MCR news"),
        ("stop", "Gracefully shut down a running daemon"),
    ):
        subparsers.add_parser(mode, help=help_text).add_argument(
            "config", type=Path, help="Path to .ini configuration file")
    daemon_parser = subparsers.choices["daemon"]
    daemon_parser.add_argument("-n", "--notify_current", action=argparse.BooleanOptionalAction,
                               help="Send a notification for the current news immediately.")
    daemon_parser.add_argument("-d", "--dummy", action=argparse.BooleanOptionalAction,
                               help="Use a dummy notifier that logs to console instead of sending webhooks.")

    subparsers.choices["status"].add_argument("--json", action="store_true", help="Print the status as JSON.")

    return parser.parse_args(argv)


def main():
    args = parse_args()
    if os.environ.pop(RESTARTED_ENV, None) and args.mode == "daemon":
        # -n means "post the current news now", not on every config apply.
        args.notify_current = False

    try:
        config = load_config(args.config)
        if args.mode == "daemon":
            config.push_secret = read_push_secret(config)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        raise SystemExit(1)

    set_timezone(config.notifications_timezone)

    configure_logging(args.mode, config)
    for warning in config.warnings:  # once, now they can reach the log
        logger.warning(warning)

    stop_event = asyncio.Event()

    try:
        if args.mode == "daemon":
            with SingleInstanceLock(Path(config.daemon_lock_file)):
                restart = asyncio.run(run_daemon(config, args, stop_event))
            if restart:  # after the lock and database are released
                restart_process()
        elif args.mode == "stop":
            asyncio.run(run_stop(config))
        elif args.mode == "config":
            asyncio.run(run_config(config))
        elif args.mode == "status":
            asyncio.run(run_status(config, args.json))
        elif args.mode == "reconnect":
            asyncio.run(run_reconnect(config))
    except LockHeldError as exc:  # anything else keeps its traceback
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        if args.mode != "daemon":
            raise SystemExit(130)  # interrupted, like any command
        print("\nStopping monitors...")


if __name__ == "__main__":
    main()
