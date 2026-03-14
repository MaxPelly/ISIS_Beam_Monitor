#!/usr/bin/env python3
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import argparse
from pathlib import Path

from isis_monitor.config import load_config, ConfigError
from isis_monitor.notifiers import NotificationChannel, TeamsNotifier, DummyNotifier
from isis_monitor.beam import BeamMonitor
from isis_monitor.mcr import MCRNewsMonitor
from isis_monitor.tui import RichTUI

# Anchor the log file to the project directory, not the working directory.
# Cap at 5 MB with 3 rotating backups.
_LOG_PATH = Path(__file__).parent / "monitor.log"

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[RotatingFileHandler(_LOG_PATH, maxBytes=5_000_000, backupCount=3)],
)
logger = logging.getLogger("MAIN")


async def run_all(config, args, stop_event: asyncio.Event):
    # Setup Notification Channels
    beam_channel = NotificationChannel("Beam Updates")
    exp_channel = NotificationChannel("Experiment Updates")
    mcr_channel = NotificationChannel("MCR News")

    # Configure Teams Notifiers
    if config.beam_teams_url:
        beam_channel.add_notifier(TeamsNotifier(config.beam_teams_url))
    if config.experiment_teams_url:
        exp_channel.add_notifier(TeamsNotifier(config.experiment_teams_url))
    if config.news_teams_url:
        mcr_channel.add_notifier(TeamsNotifier(config.news_teams_url))

    if args.dummy:
        logger.info("Initializing Dummy Notifier (logs to console)")
        beam_channel.add_notifier(DummyNotifier())
        exp_channel.add_notifier(DummyNotifier())
        mcr_channel.add_notifier(DummyNotifier())

    # Initialize TUI
    tui = RichTUI()
    tui.start()

    # Initialize Monitors
    beam_monitor = BeamMonitor(config, beam_channel, exp_channel, args.notify_counts, tui=tui)
    mcr_monitor = MCRNewsMonitor(config, mcr_channel, args.notify_current, tui=tui)

    logger.info("Starting monitors concurrently...")
    try:
        await asyncio.gather(
            beam_monitor.run(stop_event),
            mcr_monitor.run(stop_event),
        )
    finally:
        tui.stop()


def main():
    parser = argparse.ArgumentParser(description="ISIS Beam and MCR News Monitor")
    parser.add_argument("config", type=Path, help="Path to .ini configuration file")
    parser.add_argument(
        "-nc", "--notify_counts", type=float, default=130,
        help="Counts threshold for notification",
    )
    parser.add_argument(
        "-n", "--notify_current",
        help="Send a notification for the current news immediately.",
        action=argparse.BooleanOptionalAction,
    )
    parser.add_argument(
        "-d", "--dummy",
        help="Use a dummy notifier that logs to console instead of sending webhooks.",
        action=argparse.BooleanOptionalAction,
    )
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Configuration error: {e}")
        raise SystemExit(1)

    stop_event = asyncio.Event()

    try:
        asyncio.run(run_all(config, args, stop_event))
    except KeyboardInterrupt:
        stop_event.set()
        print("\nStopping monitors...")


if __name__ == "__main__":
    main()
