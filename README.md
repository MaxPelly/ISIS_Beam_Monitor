> **Note:** This branch includes AI-assisted code generation. Treat it with appropriate caution.

# ISIS Beam and MCR News Monitor

This is a Python application that monitors the status of the ISIS beam, experiment updates, and MCR news, sending notifications to designated Microsoft Teams channels. It provides a concurrent monitoring system for real-time facility updates.

## Features

- **Beam Updates**: Monitors the ISIS beam status and sends debounced state-change cards (with severity colour, emoji, current/previous readings and time-in-state) based on configurable thresholds. Correlated multi-target trips are called out in the same card.
- **Experiment Updates**: Tracks run starts/finishes, including previous-run stats, counts collected, a collection-rate ETA, and a warning if data collection stalls while the instrument beam is on.
- **MCR News**: Fetches the latest Main Control Room (MCR) news and classifies each update's severity (good/attention/warning) by keyword, with an optional "Open MCR news" link.
- **Daily Summary**: Sends a per-target uptime/trip/sparkline summary card once a day at a configurable time.
- **`fun_mode`**: Optional personality lines, longest-uptime records, run-count milestones and a daily fact, on top of the always-on severity/emoji information.
- **Microsoft Teams Integration**: Sends rich Adaptive Cards directly to configured Teams webhook URLs.
- **Dummy Notifier**: Includes a logging-based dummy notifier for testing and development without sending actual webhooks.
- **Concurrent Execution**: Uses `asyncio` to run beam and news monitors concurrently for real-time responsiveness.
- **Live TUI Graph View**: Displays a rolling 1-hour sparkline graph of beam current (μA) for TS1, TS2, and Muons directly in the terminal. The graph is sampled on its own fixed 1-minute timer, fully decoupled from the beam WebSocket update rate — a silent beam produces a flat line at the last-known value.

## Requirements

Ensure you have Python 3.10+ installed.
Install the required dependencies using pip:

```bash
pip install -r requirements.txt
```

For development and testing, install the development dependencies:

```bash
pip install -r requirements-dev.txt
```

## Configuration

The application requires an INI configuration file to set up the Teams webhook URLs and other settings.

1. Copy the example configuration file:
   ```bash
   cp config.ini.example config.ini
   ```
2. Edit `config.ini` and add your specific Teams webhook URLs for beam, experiment, and news updates.

### Optional `[TUI]` section

```ini
[TUI]
# Number of 1-minute samples to retain per beam target (default = 60 → 1-hour rolling window).
# history_maxlen = 60
# Interval in seconds between graph samples (default = 60).
# sample_interval = 60
```

### Optional `[NOTIFICATIONS]` section

```ini
[NOTIFICATIONS]
# Adds optional personality lines to notification cards (default = false).
# Emoji and severity colours always show, regardless of this setting.
# fun_mode = false
# Timezone used for the timestamps shown on notification cards (default = Europe/London).
# timezone = Europe/London
# How long a beam state change must persist, in seconds, before a card
# is sent — filters out brief flickers (default = 20).
# debounce_seconds = 20
# How many minutes counts collected can stay flat, while the instrument
# beam is on, before a stall warning is sent (default = 15).
# stall_minutes = 15
# UK-local time (HH:MM) the daily beam-uptime summary is sent at (default = 08:00).
# summary_time = 08:00
```

### Optional `mcr_page_url` setting in `[DATA]`

```ini
[DATA]
# Optional link shown as an "Open MCR news" button on MCR notification cards.
# mcr_page_url = https://www.isis.stfc.ac.uk/gallery/beam-status/
```

### Optional instrument setting in `[PVS]`

```ini
[PVS]
# Which beam target's state is reported as "the instrument's beam" on run
# cards (default = TS1).
# instrument_target = TS1
```

## Usage

Run the daemon (long-running monitor, notifications, persistence, IPC server):

```bash
python main.py daemon path/to/config.ini [OPTIONS]
```

Run the TUI client (attach/detach as needed, same host via SSH):

```bash
python main.py tui path/to/config.ini
```

In TUI mode, operator commands are available from stdin:
- `r` + Enter: force reconnect (beam + MCR) on daemon
- `q` + Enter: quit TUI client

### Daemon options

- `config`: (Required) Path to the `.ini` configuration file.
- `-n`, `--notify_current`: Send a notification for the current news immediately on startup. Use `--no-notify_current` to disable (default behaviour: wait for new news before notifying).
- `-d`, `--dummy`, `--no-dummy`: Use a dummy notifier for testing purposes that logs to the console instead of sending actual webhooks.

### Example

To run the daemon with a custom configuration file and dummy notifications for testing:

```bash
python main.py daemon config.ini --dummy
```

To run TUI from an SSH session on the same host:

```bash
python main.py tui config.ini
```

### Linux service example (systemd)

Create `/etc/systemd/system/isis-beam-monitor.service`:

```ini
[Unit]
Description=ISIS Beam Monitor Daemon
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/ISIS_Beam_Monitor
ExecStart=/usr/bin/python /path/to/ISIS_Beam_Monitor/main.py daemon /path/to/ISIS_Beam_Monitor/config.ini
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now isis-beam-monitor.service
sudo systemctl status isis-beam-monitor.service
```

### Troubleshooting

- **`Lock file already held`**: another daemon instance is running (or stale lock path configured).
- **TUI cannot connect**: ensure daemon is running and `[DAEMON].socket_path` matches `[TUI_CLIENT].socket_path`.
- **No live updates**: check `monitor.log` for websocket/news source errors; use `r` in TUI to force reconnect.
