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

# Logger is configured dynamically in main() based on config
logger = logging.getLogger("MAIN")

class TUILogHandler(logging.Handler):
    def __init__(self, tui):
        super().__init__()
        self.tui = tui

    def emit(self, record):
        try:
            msg = self.format(record)
            self.tui.update_log(msg)
        except Exception:
            self.handleError(record)


async def run_all(config, args, stop_event: asyncio.Event):
    # Initialize TUI
    tui = RichTUI(
        history_maxlen=config.history_maxlen,
        sample_interval=config.sample_interval,
        refresh_per_second=config.refresh_per_second,
        logs_maxlen=config.logs_maxlen,
    )
    tui.start()

    # Route logs to TUI
    tui_handler = TUILogHandler(tui)
    tui_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logging.getLogger().addHandler(tui_handler)

    # Setup Notification Channels
    beam_channel = NotificationChannel("Beam Updates")
    exp_channel = NotificationChannel("Experiment Updates")
    mcr_channel = NotificationChannel("MCR News")

    # Configure Teams Notifiers
    if config.beam_teams_url:
        beam_channel.add_notifier(TeamsNotifier(config.beam_teams_url, timeout=config.webhook_timeout))
    if config.experiment_teams_url:
        exp_channel.add_notifier(TeamsNotifier(config.experiment_teams_url, timeout=config.webhook_timeout))
    if config.news_teams_url:
        mcr_channel.add_notifier(TeamsNotifier(config.news_teams_url, timeout=config.webhook_timeout))

    if args.dummy:
        logger.info("Initializing Dummy Notifier (logs to console)")
        beam_channel.add_notifier(DummyNotifier())
        exp_channel.add_notifier(DummyNotifier())
        mcr_channel.add_notifier(DummyNotifier())

    # Initialize Monitors
    beam_monitor = BeamMonitor(config, beam_channel, exp_channel, args.notify_counts, tui=tui)
    mcr_monitor = MCRNewsMonitor(config, mcr_channel, args.notify_current, tui=tui)

    logger.info("Starting monitors concurrently...")
    try:
        await asyncio.gather(
            beam_monitor.run(stop_event),
            mcr_monitor.run(stop_event),
            tui.run_sampler(stop_event),
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

    # Configure logging based on config
    log_path = Path(config.log_file)
    if not log_path.is_absolute():
        log_path = Path(__file__).parent / log_path

    numeric_level = getattr(logging, config.log_level.upper(), logging.WARNING)

    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[RotatingFileHandler(
            log_path, 
            maxBytes=config.log_max_bytes, 
            backupCount=config.log_backup_count
        )],
    )

    stop_event = asyncio.Event()

    try:
        asyncio.run(run_all(config, args, stop_event))
    except KeyboardInterrupt:
        stop_event.set()
        print("\nStopping monitors...")

if __name__ == "__main__":
    main()
