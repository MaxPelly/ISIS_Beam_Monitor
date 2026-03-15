> **Note:** This branch includes AI-assisted code generation. Treat it with appropriate caution.

# ISIS Beam and MCR News Monitor

This is a Python application that monitors the status of the ISIS beam, experiment updates, and MCR news, sending notifications to designated Microsoft Teams channels. It provides a concurrent monitoring system for real-time facility updates.

## Features

- **Beam Updates**: Monitors the ISIS beam status and sends alerts based on configurable thresholds.
- **Experiment Updates**: Keeps track of ongoing experiments.
- **MCR News**: Fetches and notifies about the latest Main Control Room (MCR) news.
- **Microsoft Teams Integration**: Sends formatted notifications directly to configured Teams webhook URLs.
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

## Usage

Run the monitor using the main script:

```bash
python main.py path/to/config.ini [OPTIONS]
```

### Options

- `config`: (Required) Path to the `.ini` configuration file.
- `-nc`, `--notify_counts`: Counts threshold at which a "run about to finish" notification is sent (default: 130).
- `-n`, `--notify_current`: Send a notification for the current news immediately on startup. Use `--no-notify_current` to disable (default behaviour: wait for new news before notifying).
- `-d`, `--dummy`, `--no-dummy`: Use a dummy notifier for testing purposes that logs to the console instead of sending actual webhooks.

### Example

To run the monitor with a custom configuration file and enabling dummy notifications for testing:

```bash
python main.py config.ini --dummy
```