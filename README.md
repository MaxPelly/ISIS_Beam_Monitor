> **Note:** This branch includes AI-assisted code generation. Treat it with appropriate caution.

# ISIS Beam and MCR News Monitor

This is a Python application that monitors the status of the ISIS beam, experiment updates, and MCR news, sending notifications to designated Microsoft Teams channels. It provides a concurrent monitoring system for real-time facility updates.

## Features

- **Beam Updates**: Monitors the ISIS beam status and sends debounced state-change cards (with severity colour, emoji, current/previous readings and time-in-state) based on configurable thresholds. Correlated multi-target trips are called out in the same card.
- **Experiment Updates**: Tracks run starts/finishes on any number of instruments, each with its own notify threshold, using each instrument's total µA·h collected (`IN:<NAME>:DAE:TOTALUAMPS`): previous-run stats and the new run's target, collection rate and ETA, a warning if data collection stalls while that instrument's beam is on and its DAE is running (and a card when it resumes), and a summary of each experiment (runs, µA·h, duration) when the instrument's RB number changes or goes to "0" (no experiment).
- **MCR News**: Fetches the latest Main Control Room (MCR) news and classifies each update's severity (good/attention/warning) by keyword, with an optional "Open MCR news" link.
- **Daily Summary**: Sends a per-target uptime/trip/sparkline summary card once a day at a configurable time, with the runs and µA·h of each instrument on that target over the last 24h.
- **Microsoft Teams Integration**: Sends rich Adaptive Cards directly to configured Teams webhook URLs.
- **Signed Webhook (optional)**: Can also POST every notification as HMAC-signed JSON, e.g. to the optional `push_site` for mobile push notifications (see `[PUSH]` below).
- **Dummy Notifier**: Includes a logging-based dummy notifier for testing and development without sending actual webhooks.
- **Concurrent Execution**: Uses `asyncio` to run beam and news monitors concurrently for real-time responsiveness.
- **Status**: With `[PUSH] url` set, the push site's Status page shows each beam with its last 24h, each instrument's run and progress towards its notify threshold (µA·h), the MCR news and the daemon's health. `isis-monitor status` prints the same on the host.
- **Config Editor**: `isis-monitor config` edits the instruments and notification settings of the running daemon, which validates, saves and restarts itself to apply them.

## Requirements

Ensure you have Python 3.12+ installed. Install the monitor and its dependencies
from the checkout, which also installs the `isis-monitor` command:

```bash
pip install -e .
```

For development and testing, include the development dependencies:

```bash
pip install -e '.[dev]'
```

Run the tests, optionally with a coverage report (branch coverage, listing uncovered lines):

```bash
pytest
pytest --cov=isis_monitor --cov-branch --cov-report=term-missing
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
# Required: the run's target in total µA·h collected. "Run about to finish" is sent
# [NOTIFICATIONS] finish_warning_minutes before it's expected to be reached (or on
# reaching it, if the rate isn't known yet). "auto, 130" targets each run at the
# median of the experiment's recent runs instead, using 130 until there are enough.
notify_counts = 130
# Optional: TS1, TS2 or Muons — the beam reported on run cards and checked
# before stall warnings. By default it's the instrument's target station from
# ISIS's instrument list (CS:INSTLIST), or [PVS] instrument_target if that
# list doesn't put it on TS1, TS2 or MUON.
# beam_target = TS1
# Optional: the Teams payload `channel` for this instrument's cards —
# experiment (default) sends "Experiment Updates", instrument sends "PEARL".
# channel = experiment
```

The PVs are derived from the name: progress (total µA·h collected this run) from
`IN:<NAME>:DAE:TOTALUAMPS`, the run name from `IN:<NAME>:DAE:WDTITLE`, the run state
from `IN:<NAME>:DAE:RUNSTATE_STR`, the run number from `IN:<NAME>:DAE:RUNNUMBER` and
the experiment's RB number from `IN:<NAME>:DAE:_RBNUMBER`. A new run is spotted by its
title changing or its total collected resetting, so runs with a hidden title are still
reported. Stalls are only checked while the DAE is RUNNING or VETOING, so a paused run,
a run-control wait or a run ended short of its target isn't reported as one. Names may contain letters, digits, `_` and `-`, and are
upper-cased. Downstream flows that route on the payload's `channel` see "Experiment
Updates" unless an instrument sets `channel = instrument`. At least one `[INSTRUMENT:*]`
section is required.

With `notify_counts = auto, <number>`, each run's target is set when it starts, from the
median of the last 5 completed runs of the same experiment. Very short runs (under 1 µA·h
or a quarter of the typical run, e.g. a mistaken setup) are left out, and `<number>` is
used until 3 runs remain. The experiment is told apart by its RB number
(`IN:<NAME>:DAE:_RBNUMBER`): when it changes, including to "0" (no experiment), the
history starts again. The finishing
card shows an auto target as e.g. "60.5 / 60 µA·h (auto)", and `status` as "≈60".

Known issue: retitling a run more than 2 minutes after it began is taken as a new run, as
a new title usually comes just before BEGIN. That sends a new-run card and records the
µA·h so far as a completed run, so the run is counted twice in the experiment card, the
daily summary and the auto target history.

### Optional `[NOTIFICATIONS]` section

```ini
[NOTIFICATIONS]
# Timezone for card timestamps and for summary_time (default = Europe/London).
# timezone = Europe/London
# How long a beam state change must persist, in seconds (0-3600), before a
# card is sent — filters out brief flickers (default = 20).
# debounce_seconds = 20
# How many minutes the µA·h collected can stay flat, while the instrument's
# beam is on, before a stall warning is sent (at most 7 days; default = 15).
# stall_minutes = 15
# How many minutes before a run is expected to reach its notify_counts the
# "run about to finish" card is sent (0 = only once it's passed; default = 15).
# finish_warning_minutes = 15
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
# The beam_target for instruments that don't set one and that CS:INSTLIST
# doesn't put on TS1, TS2 or MUON (or doesn't list at all) (default = TS1).
# instrument_target = TS1
```

The daemon also reads each instrument's PV prefix from `CS:INSTLIST`, as it isn't
always `IN:<NAME>:` (e.g. `PEARL_SETUP` is `IN:PEARL_5B:`). It caches the list, so a
restart starts with the right PVs; an instrument the list doesn't name (probably a
typo) is logged and keeps `IN:<NAME>:`.

### Optional `[BEAM:<NAME>]` sections

Each beam target (`TS1`, `TS2`, `Muons`) can override its current PV and power boundaries:

```ini
[BEAM:Muons]
# pv = AC:MUON:BEAM:CURR
# Off at or below the first value, low below the second, medium below the third, else high (µA).
# boundaries = 0, 2, 5
```

### Optional `[PUSH]` signed webhook

Sends every notification, on all three channels, as HMAC-signed JSON to one URL as
well as to Teams. It's meant for the optional `push_site` submodule (mobile push
notifications), but any receiver that checks the signature will work. It also sends the
daemon's status (beams, instruments, MCR news, health), signed the same way, to the URL
plus `/status` (e.g. `/ingest/status`) every `[DAEMON] sample_interval` and soon after a
change, for the push site's Status page (push site 1.1.0 or later).

The push site is a separate repository, included here as the `push_site` git submodule.
A plain clone leaves it empty, and the monitor works without it. To use it:

```bash
git submodule update --init push_site   # or clone with --recurse-submodules
```

`push_site/README.md` then covers setting it up, sharing this secret with it, and
inviting people's phones.

```ini
[PUSH]
url = http://127.0.0.1:8765/ingest
# 32 to 4096 characters, created owner-only, e.g. `(umask 077; openssl rand -hex 32 > push.secret)`.
# Relative to this config file's directory; only the daemon reads it. With the
# push_site submodule, use the site's own copy: push_site/push.secret.
secret_file = push_site/push.secret
# timeout = 2
```

Each request has an `X-Timestamp` header (Unix seconds) and an `X-Signature` header:
the hex HMAC-SHA256 of `<timestamp>.<body>` under the secret (the file's contents with
surrounding whitespace, such as the trailing newline, removed). The body is a JSON
object (`"v": 1`) holding the notification's `id`, `title`, `text`,
`severity`, `emoji`, `facts`, `url`, `url_label`, `timestamp`
(ISO 8601, UTC), `topic` (`TS1`/`TS2`/`Muons`, the instrument name, `MCR`
or `Summary`) and `monitor_version`. A retry keeps the same `id`.

## Usage

Run the daemon (long-running monitor, notifications, persistence, IPC server):

```bash
isis-monitor daemon path/to/config.ini [OPTIONS]
```

Print the daemon's status (beams with their last 24h, instruments, MCR news and health),
or its JSON with `--json`; make it reconnect to PVWS and the MCR news; or stop it:

```bash
isis-monitor status path/to/config.ini [--json]
isis-monitor reconnect path/to/config.ini
isis-monitor stop path/to/config.ini
```

### Editing the configuration

Run `isis-monitor config path/to/config.ini` to open a numbered menu of the notification settings
(`timezone`, `debounce_seconds`, `stall_minutes`,
`finish_warning_minutes`, `summary_time`) and the
instruments (name, notify threshold, beam target, Teams channel). Type a number to edit
that entry (Enter keeps the current value), `a` to add an instrument, `d <number>` to
delete one, `s` to review the changes and save, or `q` to leave without saving.

On save the daemon validates the new settings (nothing is written if they're invalid, and
you return to the menu to fix them), writes `config.ini`, and restarts itself in place (same
PID, so it works under systemd or when run by hand).
Things to know:

- The file is rewritten by Python's `configparser`, so **comments are lost**. The previous
  version is kept as `config.ini.bak`, and `config.ini.example` documents every setting.
- If the file changed since the editor opened it (a hand edit, or another editor saving
  first), the save is refused rather than overwriting those changes; reopen the editor.
- Saving drops unknown keys from instrument sections and writes an explicit `channel`
  for every instrument. `beam_target` is only written when it's set; choose `auto` to
  leave it to `CS:INSTLIST`.
- The daemon's user needs write access to `config.ini` and its directory (for the temporary
  file and `config.ini.bak`); otherwise saving fails with a write error.
- Each restart re-sends the beam "Monitor online" cards, as any daemon start does.
  `-n/--notify_current` is not re-applied on these restarts.
- Other settings (webhooks, paths, boundaries, …) are still edited by hand; restart the
  daemon afterwards (e.g. `systemctl restart isis-beam-monitor`, or `isis-monitor stop`
  and start it again). A later save from the editor also applies them, since it re-reads the file first.

### Daemon options

- `config`: (Required) Path to the `.ini` configuration file.
- `-n`, `--notify_current`: Send a notification for the current news immediately on a first start, when there's no saved news yet. Use `--no-notify_current` to disable (default behaviour: wait for new news before notifying). Otherwise the daemon compares against the news it last saw, so news posted while it was stopped is still announced.
- `-d`, `--dummy`, `--no-dummy`: Use a dummy notifier for testing purposes that logs to the console instead of sending actual webhooks.

### Example

To run the daemon with a custom configuration file and dummy notifications for testing:

```bash
isis-monitor daemon config.ini --dummy
```

### Running as a service (systemd)

`deploy/isis-beam-monitor.service` runs the daemon in a systemd sandbox that
can only write to the checkout. It restarts after a crash with backoff, but
stays stopped after `isis-monitor stop`. Its paths and user are placeholders:
`/path/to/ISIS_Beam_Monitor` (the checkout, with the dependencies installed
in a venv at `.venv`) and `MONITOR_USER`. To fill them in and install it,
run from the checkout as the user the daemon should run as:

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
sed -e "/^#/!s|/path/to/ISIS_Beam_Monitor|$PWD|g" -e "/^#/!s|MONITOR_USER|$USER|g" \
  deploy/isis-beam-monitor.service | sudo tee /etc/systemd/system/isis-beam-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now isis-beam-monitor.service
sudo systemctl status isis-beam-monitor.service
```

### Troubleshooting

- **`Lock file already held`**: another daemon instance is running (or stale lock path configured).
- **`No answer from the daemon`**: ensure the daemon is running and the command uses the same config file (both use `[DAEMON] socket_path`).
- **No live updates**: check the daemon's log (`journalctl -u isis-beam-monitor`, its terminal, or `[LOGGING] file` if set) for websocket/news source errors; `isis-monitor reconnect config.ini` forces a reconnect.

## Releases

Versions follow [Semantic Versioning](https://semver.org/) and are set in `isis_monitor/__init__.py` (`__version__`), which is shown at the foot of every Teams card and by `isis-monitor --version`. The push site is versioned separately in its own repo. To release:

1. If the push site has changed, release it first, then point the submodule at its tag: `git -C push_site fetch --tags && git -C push_site checkout vX.Y.Z && git add push_site`.
2. Move the `[Unreleased]` entries in `CHANGELOG.md` into a new `## [X.Y.Z] - YYYY-MM-DD` section, noting the push site version, and set `__version__` to `X.Y.Z`.
3. Commit, then tag and push: `git tag -a vX.Y.Z -m vX.Y.Z && git push origin AI_assisted_main vX.Y.Z`.

The release workflow checks the tag matches `__version__`, runs the tests and publishes a GitHub release with that section's notes.
