# Note this is me playing with ai code generation. Treat this branch with extreme caution!

# ISIS Beam and MCR News Monitor

This is a Python application that monitors the status of the ISIS beam, experiment updates, and MCR news, sending notifications to designated Microsoft Teams channels. It provides a concurrent monitoring system for real-time facility updates.

## Features

- **Beam Updates**: Monitors the ISIS beam status and sends alerts based on configurable thresholds.
- **Experiment Updates**: Keeps track of ongoing experiments.
- **MCR News**: Fetches and notifies about the latest Main Control Room (MCR) news.
- **Microsoft Teams Integration**: Sends formatted notifications directly to configured Teams Webhook URLs.
- **Dummy Notifier**: Includes a logging-based dummy notifier for testing and development without sending actual webhooks.
- **Concurrent Execution**: Uses `asyncio` to run beam and news monitors concurrently for real-time responsiveness.

## Requirements

Ensure you have Python 3.7+ installed. 
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

## Usage

Run the monitor using the main script:

```bash
python main.py path/to/config.ini [OPTIONS]
```

### Options

- `config`: (Required) Path to the `.ini` configuration file.
- `-nc`, `--notify_counts`: Counts threshold for beam notification (Default: 130).
- `-n`, `--notify_current`, `--no-notify_current`: Send a notification for the current news on startup, rather than waiting for new news to be posted.
- `-d`, `--dummy`, `--no-dummy`: Use a dummy notifier for testing purposes that logs to the console instead of sending actual webhooks.

### Example

To run the monitor with a custom configuration file and enabling dummy notifications for testing:

```bash
python main.py config.ini --dummy
```
