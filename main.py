#!/usr/bin/env python3
import asyncio
import logging
import argparse
import sys
from pathlib import Path

from isis_monitor.config import load_config
from isis_monitor.notifiers import NotificationChannel, TeamsNotifier, DummyNotifier
from isis_monitor.beam import BeamMonitor
from isis_monitor.mcr import MCRNewsMonitor

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("MAIN")

async def run_all(config, args):
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

    # Initialize Monitors
    beam_monitor = BeamMonitor(config, beam_channel, exp_channel, args.notify_counts)
    mcr_monitor = MCRNewsMonitor(config, mcr_channel, args.notify_current)

    logger.info("Starting monitors concurrently...")
    
    # Run them concurrently
    await asyncio.gather(
        beam_monitor.run(),
        mcr_monitor.run()
    )

def main():
    parser = argparse.ArgumentParser(description="ISIS Beam and MCR News Monitor")
    parser.add_argument("config", type=Path, help="Path to .ini configuration file")
    parser.add_argument("-nc", "--notify_counts", type=float, default=130, help="Counts threshold for notification")
    parser.add_argument("-n", "--notify_current", help="Send a notification for the current news. Otherwise waits until new news is posted.", action=argparse.BooleanOptionalAction)
    parser.add_argument("-d", "--dummy", help="Use a dummy notifier for testing purposes that logs to console.", action=argparse.BooleanOptionalAction)
    args = parser.parse_args()

    config = load_config(args.config)

    try:
        asyncio.run(run_all(config, args))
    except KeyboardInterrupt:
        print("\nStopping monitors...")

if __name__ == "__main__":
    main()
