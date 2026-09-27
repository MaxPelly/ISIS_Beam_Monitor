> **Note:** This branch includes AI-assisted code generation. Treat it with appropriate caution.

# ISIS Beam and MCR News Monitor

This is a Python application that monitors the status of the ISIS beam, experiment updates, and MCR news, sending notifications to designated Microsoft Teams channels. It provides a concurrent monitoring system for real-time facility updates.

## Features

- **Beam Updates**: Monitors the ISIS beam status and sends debounced state-change cards (with severity colour, emoji, current/previous readings and time-in-state) based on configurable thresholds. Correlated multi-target trips are called out in the same card.
- **Experiment Updates**: Tracks run starts/finishes on any number of instruments, each with its own notify threshold, using each instrument's total µA·h collected (`IN:<NAME>:DAE:TOTALUAMPS`): previous-run stats, collection rate and ETA, and a warning if data collection stalls while that instrument's beam is on.
- **MCR News**: Fetches the latest Main Control Room (MCR) news and classifies each update's severity (good/attention/warning) by keyword, with an optional "Open MCR news" link.
- **Daily Summary**: Sends a per-target uptime/trip/sparkline summary card once a day at a configurable time.
- **`fun_mode`**: Optional personality lines, longest-uptime records, run-count milestones and a daily fact, on top of the always-on severity/emoji information.
- **Microsoft Teams Integration**: Sends rich Adaptive Cards directly to configured Teams webhook URLs.
- **Dummy Notifier**: Includes a logging-based dummy notifier for testing and development without sending actual webhooks.
- **Concurrent Execution**: Uses `asyncio` to run beam and news monitors concurrently for real-time responsiveness.
- **Live TUI Graph View**: Displays a rolling 1-hour sparkline graph of beam current (μA) for TS1, TS2, and Muons directly in the terminal. The graph is sampled on its own fixed 1-minute timer, fully decoupled from the beam WebSocket update rate — a silent beam produces a flat line at the last-known value.
- **TUI Instruments Panel and Config Editor**: Shows each instrument's run and progress towards its notify threshold (µA·h), and lets you edit instruments and notification settings from the TUI; the daemon validates, saves and restarts itself to apply them.

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
3. Add an `[INSTRUMENT:<NAME>]` section for each instrument to monitor (see below).

### Instruments

Each instrument gets its own section. Run notifications for every instrument go to
`experiment_teams_url`, with the instrument name in the card title (e.g. "PEARL: New run started").

```ini
[INSTRUMENT:PEARL]
# Required: total µA·h collected this run at which "run about to finish" is sent.
notify_counts = 130
# Optional: TS1, TS2 or Muon — the beam reported on run cards and checked
# before stall warnings (default = [PVS] instrument_target, itself TS1 by default).
# beam_target = TS1
# Optional: the Teams payload `channel` for this instrument's cards —
# experiment (default) sends "Experiment Updates", instrument sends "PEARL".
# channel = experiment
```

Both PVs are derived from the name: progress (total µA·h collected this run) from
`IN:<NAME>:DAE:TOTALUAMPS`, and the run name from `IN:<NAME>:DAE:WDTITLE`. Names may
contain letters, digits, `_` and `-`, and are upper-cased. Downstream flows that route on
the payload's `channel` see "Experiment Updates" unless an instrument sets
`channel = instrument`.

A config with no `[INSTRUMENT:*]` sections still works: one instrument is built from the
legacy `[PVS]` keys (`run_name_pv`, and `notify_counts`, default 130), named from `counts_pv`
(e.g. PEARL); its progress is also read from `IN:<NAME>:DAE:TOTALUAMPS`.

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
# Timezone for card timestamps and for summary_time (default = Europe/London).
# timezone = Europe/London
# How long a beam state change must persist, in seconds (0-3600), before a
# card is sent — filters out brief flickers (default = 20).
# debounce_seconds = 20
# How many minutes the µA·h collected can stay flat, while the instrument's
# beam is on, before a stall warning is sent (at most 7 days; default = 15).
# stall_minutes = 15
# Time (HH:MM, in the timezone above) the daily beam-uptime summary is sent at (default = 08:00).
# summary_time = 08:00
```

### Optional `mcr_page_url` setting in `[DATA]`

```ini
[DATA]
# Optional link shown as an "Open MCR news" button on MCR notification cards.
# mcr_page_url = https://www.isis.stfc.ac.uk/gallery/beam-status/
```

### Optional `[PVS]` default beam target

```ini
[PVS]
# The beam_target for instrument sections that don't set one (default = TS1).
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

In TUI mode, single keys (no Enter needed) control the client:
- `r`: force reconnect (beam + MCR) on daemon
- `c`: edit the configuration (see below)
- `q`: quit TUI client

### Editing the configuration from the TUI

Press `c` to pause the display and open a numbered menu of the notification settings
(`fun_mode`, `timezone`, `debounce_seconds`, `stall_minutes`, `summary_time`) and the
instruments (name, notify threshold, beam target, Teams channel). Type a number to edit
that entry (Enter keeps the current value), `a` to add an instrument, `d <number>` to
delete one, `s` to review the changes and save, or `q` to leave without saving.

On save the daemon validates the new settings (nothing is written if they're invalid, and
you return to the menu to fix them), writes `config.ini`, and restarts itself in place (same
PID, so it works under systemd or when run by hand); the TUI reconnects automatically.
Things to know:

- The file is rewritten by Python's `configparser`, so **comments are lost**. The previous
  version is kept as `config.ini.bak`, and `config.ini.example` documents every setting.
- If the file changed since the editor opened it (a hand edit, or another TUI saving
  first), the save is refused rather than overwriting those changes; reopen the editor.
- A legacy `[PVS]`-only config becomes explicit `[INSTRUMENT:*]` sections on the first
  save; a non-standard `[PVS] run_name_pv` is not kept (the derived
  `IN:<NAME>:DAE:WDTITLE` is used).
- Saving drops any old `counts_pv` lines from instrument sections (see Upgrading) and
  writes an explicit `channel` and `beam_target` for every instrument, so
  `[PVS] instrument_target` then only applies to sections added by hand without a
  `beam_target`.
- Renaming an instrument starts its run count (for milestones) from zero.
- The daemon's user needs write access to `config.ini` and its directory (for the temporary
  file and `config.ini.bak`); otherwise saving fails with a write error.
- Each restart re-sends the beam "Monitor online" cards, as any daemon start does.
  `-n/--notify_current` is not re-applied on these restarts.
- Other settings (webhooks, paths, boundaries, …) are still edited by hand; restart the
  daemon afterwards (e.g. `systemctl restart isis-beam-monitor`, or `python main.py stop`
  and start it again). A later TUI save also applies them, since it re-reads the file first.

### Daemon options

- `config`: (Required) Path to the `.ini` configuration file.
- `-n`, `--notify_current`: Send a notification for the current news immediately on startup (not on restarts triggered from the TUI). Use `--no-notify_current` to disable (default behaviour: wait for new news before notifying).
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

### Upgrading from a single-instrument version

- The `-nc/--notify_counts` option has been removed; remove it from any launch script or
  systemd unit (the daemon won't start with it). Set `notify_counts` in each
  `[INSTRUMENT:*]` section, or in `[PVS]` for a legacy config.
- Existing configs keep working unchanged as a single instrument.
- The saved run count from before the upgrade is attributed to the first configured
  instrument.

### Upgrading to TOTALUAMPS progress tracking

- Run progress now comes from `IN:<NAME>:DAE:TOTALUAMPS` (total µA·h this run) instead of
  a per-instrument dashboard `counts_pv`. Existing `counts_pv` lines in `[INSTRUMENT:*]`
  sections are ignored with a warning in the log; the next TUI save removes them, or
  delete them by hand to silence the warning.
- `notify_counts` keeps its meaning and units (µA·h), so existing thresholds still apply.
- On the first restart after upgrading, an instrument already past its threshold gets a
  "run about to finish" card straight away, since older snapshots don't record that it was
  sent. Later restarts (including TUI config saves) remember it.
- Run cards keep the "Experiment Updates" channel unless an instrument sets
  `channel = instrument`.
- A config without `counts_pv` lines won't load on an older version (which requires
  them), so keep a copy (e.g. `config.ini.bak`) if you might roll back.

### Troubleshooting

- **`Lock file already held`**: another daemon instance is running (or stale lock path configured).
- **TUI cannot connect**: ensure daemon is running and `[DAEMON].socket_path` matches `[TUI_CLIENT].socket_path`.
- **No live updates**: check `monitor.log` for websocket/news source errors; use `r` in TUI to force reconnect.
