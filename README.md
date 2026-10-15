> **Note:** This branch includes AI-assisted code generation. Treat it with appropriate caution.

# ISIS Beam and MCR News Monitor

This is a Python application that monitors the status of the ISIS beam, experiment updates, and MCR news, sending notifications to designated Microsoft Teams channels. It provides a concurrent monitoring system for real-time facility updates.

## Features

- **Beam Updates**: Monitors the ISIS beam status and sends debounced state-change cards (with severity colour, emoji, current/previous readings and time-in-state) based on configurable thresholds. Correlated multi-target trips are called out in the same card.
- **Experiment Updates**: Tracks run starts/finishes on any number of instruments, each with its own notify threshold, using each instrument's total µA·h collected (`IN:<NAME>:DAE:TOTALUAMPS`): previous-run stats and the new run's target, collection rate and ETA, a warning if data collection stalls while that instrument's beam is on and its DAE is running (and a card when it resumes), and a summary of each experiment (runs, µA·h, duration) when the instrument's RB number changes or goes to "0" (no experiment).
- **MCR News**: Fetches the latest Main Control Room (MCR) news and classifies each update's severity (good/attention/warning) by keyword, with an optional "Open MCR news" link.
- **Daily Summary**: Sends a per-target uptime/trip/sparkline summary card once a day at a configurable time, with the runs and µA·h of each instrument on that target over the last 24h.
- **Microsoft Teams Integration**: Sends rich Adaptive Cards directly to configured Teams webhook URLs.
- **Signed Webhook (optional)**: Can also POST every notification as HMAC-signed JSON, e.g. to the optional ISIS Push Site for mobile push notifications (see [Signed webhook](#signed-webhook-push) below).
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

The application reads an INI file. `config.ini.example` is the annotated example,
with every setting below; start from a copy of it:

1. `cp config.ini.example config.ini`
2. Add your Teams webhook URLs in `[WEBHOOKS]`.
3. Add an `[INSTRUMENT:<NAME>]` section for each instrument to monitor (see [Instruments](#instruments)).

The whole file is checked when any command starts, and an invalid value stops it with
a `Configuration error` naming the setting. A blank number (e.g. `retention_days =`)
means the default, and unknown sections and keys are logged as a warning. Relative
paths are relative to the config file's directory.

### Configuration reference

Only `[DATA] mcr_news_url` and at least one `[INSTRUMENT:<NAME>]` section with a
`notify_counts` are required; everything else, including whole sections, may be left out.
Times are in seconds unless the key says minutes.

| Section | Key | Default | Allowed values | Meaning |
|---|---|---|---|---|
| `[DATA]` | `mcr_news_url` | none (**required**) | URL | The MCR news text feed. |
| | `isis_websocket_url` | blank | PVWS WebSocket URL | The PV feed for beams and instruments; blank (logged as a warning) watches neither. |
| | `mcr_page_url` | blank | URL | Link for an "Open MCR news" button on MCR cards; blank for none. |
| `[WEBHOOKS]` | `beam_teams_url` | blank | `https://` URL | Teams webhook for beam cards and the daily summary; blank disables it. |
| | `experiment_teams_url` | blank | `https://` URL | Teams webhook for every instrument's run and experiment cards; blank disables it. |
| | `news_teams_url` | blank | `https://` URL | Teams webhook for MCR news cards; blank disables it. |
| `[PUSH]` | `url` | blank | `http://` or `https://` URL, or `unix:` and a socket's absolute path | Also POST every notification, signed, here, and the status to this URL plus `/status`; blank disables it. `unix:/run/isis-push-site/ingest.sock` posts to `/ingest` over the push site's socket. Plain `http://` to a non-local host is warned about. |
| | `secret_file` | blank | file of 32 to 4096 characters | The shared signing secret; only the daemon reads it, and a world-readable file is warned about. Unset, the daemon reads systemd's `push-secret` credential (`LoadCredential=` in its unit), which it then needs. |
| | `timeout` | 2 | 0.5 to 30 | Seconds before each push attempt is abandoned. |
| `[PVS]` | `instrument_target` | `TS1` | `TS1`, `TS2`, `Muons` (any case; `Muon` too) | The `beam_target` of instruments that don't set one and that `CS:INSTLIST` doesn't put on TS1, TS2 or MUON. |
| `[TIMEOUTS_INTERVALS]` | `mcr_poll_interval` | 60 | 5 to 86400 | Seconds between MCR news polls. |
| | `beam_reconnect_interval` | 5 | 0.5 to 3600 | Seconds before retrying a lost PVWS connection. |
| | `webhook_timeout` | 10 | 1 to 300 | Seconds before a Teams send is abandoned. |
| `[DAEMON]` | `db_path` | `beam_monitor.db` | path | SQLite database of history and state. |
| | `socket_path` | `isis_beam_monitor.sock` | path of at most 107 bytes once made absolute | Unix socket the other commands use to reach the daemon. Keep it out of shared directories such as `/tmp`. |
| | `lock_file` | `isis_beam_monitor.lock` | path | Lock that keeps to one daemon per config. |
| | `sample_interval` | 60 | 10 to 3600 | Seconds between beam history samples, which is also how often state is saved. |
| | `status_interval` | 5 | 1 to 3600 | Seconds between checks for a changed status to send to `[PUSH] url`; an unchanged one is sent every 30s. |
| | `retention_days` | 7 | whole number, 1 to 365 | Days of beam history kept in the database. |
| `[LOGGING]` | `level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` (any case) | The daemon's log level. It always logs to stderr (journald under systemd). |
| | `file` | blank | path | Also log to this rotating file (5 MB × 3); blank for none. |
| `[NOTIFICATIONS]` | `timezone` | `Europe/London` | IANA timezone | For card timestamps and `summary_time`. |
| | `debounce_seconds` | 20 | 0 to 3600 | How long a beam state change must last before its card is sent, filtering out flickers. |
| | `stall_minutes` | 15 | above 0, at most 10080 (7 days) | How long the µA·h collected can stay flat, while the instrument's beam is on and its DAE running, before a stall warning. |
| | `finish_warning_minutes` | 15 | 0 to 10080 | How long before a run is expected to reach its `notify_counts` the "run about to finish" card is sent (0 = once it's reached). |
| | `summary_time` | `08:00` | `HH:MM`, 00:00 to 23:59 | When the daily beam-uptime summary is sent, in `timezone`. |
| `[INSTRUMENT:<NAME>]` | `notify_counts` | none (**required**) | positive number, or `auto, <positive number>` | The run's target in total µA·h collected; `auto` sets it per run (see [Instruments](#instruments)). |
| | `beam_target` | from `CS:INSTLIST` | `TS1`, `TS2`, `Muons` (any case; `Muon` too) | The beam reported on run cards and checked before stall warnings. Unset, it's the instrument's target station from `CS:INSTLIST`, else `[PVS] instrument_target`. |
| | `channel` | `experiment` | `experiment`, `instrument` (any case) | The Teams payload `channel` of its cards: "Experiment Updates", or the instrument's name. |
| `[BEAM:<NAME>]` | `pv` | `AC:TS1:BEAM:CURR`, `AC:TS2:BEAM:CURR`, `AC:MUON:BEAM:CURR` | PV name, different for each beam | The beam's current, in µA. |
| | `boundaries` | TS1 `0, 50, 140`; TS2 `0, 10, 30`; Muons `0, 2, 5` | 3 finite numbers, in increasing order (equal allowed) | Power cutoffs in µA: off at or below the first, low below the second, medium below the third, else high. |

`<NAME>` in `[INSTRUMENT:<NAME>]` may contain letters, digits, `_` and `-` and is
upper-cased; each instrument appears once. `[BEAM:<NAME>]` is `TS1`, `TS2` or `Muons`
(any case; `Muon` too), once each.

### Instruments

Run notifications for every instrument go to `experiment_teams_url`, with the
instrument name in the card title (e.g. "PEARL: New run started").

The PVs are derived from the name: progress (total µA·h collected this run) from
`IN:<NAME>:DAE:TOTALUAMPS`, the run name from `IN:<NAME>:DAE:WDTITLE`, the run state
from `IN:<NAME>:DAE:RUNSTATE_STR`, the run number from `IN:<NAME>:DAE:RUNNUMBER` and
the experiment's RB number from `IN:<NAME>:DAE:_RBNUMBER`. A new run is spotted by its
title changing or its total collected resetting, so runs with a hidden title are still
reported. Stalls are only checked while the DAE is RUNNING or VETOING, so a paused run,
a run-control wait or a run ended short of its target isn't reported as one. Downstream
flows that route on the payload's `channel` see "Experiment Updates" unless an
instrument sets `channel = instrument`.

The daemon also reads each instrument's PV prefix from `CS:INSTLIST`, as it isn't
always `IN:<NAME>:` (e.g. `PEARL_SETUP` is `IN:PEARL_5B:`). It caches the list, so a
restart starts with the right PVs; an instrument the list doesn't name (probably a
typo) is logged and keeps `IN:<NAME>:`.

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

### Signed webhook (`[PUSH]`)

Sends every notification, on all three channels, as HMAC-signed JSON to one URL as
well as to Teams. It's meant for the optional companion
[ISIS Push Site](https://github.com/MaxPelly/ISIS_Push_Site) (mobile push notifications,
v2.0.0 or later), but any receiver that checks the signature will work. It also sends
the daemon's status (beams, instruments, MCR news, health), signed the same way, to the
URL plus `/status` (e.g. `/ingest/status`), for the push site's Status page: within
`[DAEMON] status_interval` (5s) of any change, and at least every 30s.

The push site is installed on its own; its README covers setting it up, sharing the
secret with the monitor, and inviting people's phones. With its standard layout, set
`url = unix:/run/isis-push-site/ingest.sock` and leave `secret_file` unset: the unit
in `deploy/isis-beam-monitor.service` gives the daemon the site's secret as its
`push-secret` credential and puts it in the `isis-ingest` group, which can reach the
socket. Another receiver needs a secret file of its own, created owner-only, e.g.
`(umask 077; openssl rand -hex 32 > push.secret)`.

Each request has an `X-Timestamp` header (Unix seconds) and an `X-Signature` header:
the hex HMAC-SHA256 of `<timestamp>.<body>` under the secret (the file's contents with
surrounding whitespace, such as the trailing newline, removed). The body is a JSON
object (`"v": 1`) holding the notification's `id`, `title`, `text`,
`severity`, `emoji`, `facts`, `url`, `url_label`, `timestamp`
(ISO 8601, UTC), `topic` (`TS1`/`TS2`/`Muons`, the instrument name, `MCR`
or `Summary`) and `monitor_version`. A retry keeps the same `id`.

## Status page

The live display is the push site's Status page: each beam with its last 24h, each
instrument's run and progress, the MCR news and the daemon's health, from the status
the daemon pushes to `[PUSH] url` plus `/status`. See the push site's README for
setting it up. Without the push site, `isis-monitor status` shows the same on the host.

## Usage

```bash
isis-monitor daemon path/to/config.ini [-n] [-d]   # run the monitor
isis-monitor status path/to/config.ini [--json]    # print its status, or its JSON
isis-monitor config path/to/config.ini             # edit instruments and notification settings
isis-monitor reconnect path/to/config.ini          # reconnect to PVWS and re-read the MCR news
isis-monitor stop path/to/config.ini               # shut it down cleanly
isis-monitor --version
```

The daemon runs the monitors, sends the notifications and keeps the history; the other
commands talk to it over its `[DAEMON] socket_path`, so they must run as the daemon's
user with the same config file.

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
  daemon afterwards (`sudo systemctl restart isis-beam-monitor` under systemd, or
  `isis-monitor stop path/to/config.ini` and start it again). A later save from the editor
  also applies them, since it re-reads the file first.

### Daemon options

- `-n`, `--notify_current`: Send a notification for the current news immediately on a first start, when there's no saved news yet. Use `--no-notify_current` to disable (default behaviour: wait for new news before notifying). Otherwise the daemon compares against the news it last saw, so news posted while it was stopped is still announced.
- `-d`, `--dummy`, `--no-dummy`: Use a dummy notifier for testing purposes that logs to the console instead of sending actual webhooks.

For example, to try a configuration without sending anything:

```bash
isis-monitor daemon config.ini --dummy
```

### Running as a service (systemd)

`deploy/isis-beam-monitor.service` runs the daemon in a systemd sandbox where its
checkout is read-only. It keeps its config in `/etc/isis-beam-monitor` (owned by the
daemon's user, so `isis-monitor config` can rewrite it), its database in
`/var/lib/isis-beam-monitor`, its socket and lock in `/run/isis-beam-monitor` and its
log file, if any, in `/var/log/isis-beam-monitor`; systemd creates the last three. It
restarts after a crash with backoff, but stays stopped after
`isis-monitor stop /etc/isis-beam-monitor/config.ini` (run as the daemon's user). Its
placeholders are `/path/to/ISIS_Beam_Monitor` (the checkout, with the dependencies in a
venv at `.venv`) and `MONITOR_USER`. Without the push site, delete its
`LoadCredential=` and `SupplementaryGroups=` lines. To install it, run from the
checkout as the user the daemon should run as:

1. Install the code and the config:
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -e .
   sudo install -d -m 700 -o "$USER" -g "$USER" /etc/isis-beam-monitor
   install -m 600 config.ini.example /etc/isis-beam-monitor/config.ini
   ```
2. Fill in `/etc/isis-beam-monitor/config.ini`, putting the daemon's files where the
   unit lets it write:
   ```ini
   [DAEMON]
   db_path = /var/lib/isis-beam-monitor/beam_monitor.db
   socket_path = /run/isis-beam-monitor/isis_beam_monitor.sock
   lock_file = /run/isis-beam-monitor/isis_beam_monitor.lock

   [LOGGING]
   # Optional: journald has the log too
   file = /var/log/isis-beam-monitor/monitor.log
   ```
   Moving an existing install? Stop it first and copy its database across, or the
   daemon starts afresh, without its history or the runs and experiments under way
   (systemd adopts the directory):
   ```bash
   sudo install -d -m 700 -o "$USER" -g "$USER" /var/lib/isis-beam-monitor
   cp -p /old/checkout/beam_monitor.db /var/lib/isis-beam-monitor/
   ```
3. Install the unit and start it:
   ```bash
   sed -e "/^#/!s|/path/to/ISIS_Beam_Monitor|$PWD|g" -e "/^#/!s|MONITOR_USER|$USER|g" \
     deploy/isis-beam-monitor.service | sudo tee /etc/systemd/system/isis-beam-monitor.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now isis-beam-monitor.service
   sudo systemctl status isis-beam-monitor.service
   ```

The commands (`status`, `config`, `stop`) find the daemon through the paths in that
config, so they work only while it runs under systemd, which creates
`/run/isis-beam-monitor`.

### Troubleshooting

- **Logs**: the daemon logs to stderr, so under systemd read them with `journalctl -u isis-beam-monitor` (add `-f` to follow); run by hand, they're on its terminal. `[LOGGING] file` adds a log file.
- **`Configuration error: …`**: the named setting is missing or out of range; see the [configuration reference](#configuration-reference).
- **`[DAEMON] socket_path … is longer than a Unix socket path can be`**: Unix socket paths are limited to 107 bytes, and a relative `socket_path` is made absolute from the config file's directory, so a config in a deep directory can exceed it. Set a shorter absolute `socket_path`.
- **`Lock file already held`**: another daemon is running with this config (or another config shares its `lock_file`).
- **`No answer from the daemon`**: check the daemon is running, and that the command runs as the daemon's user (it refuses any other, even root, e.g. `sudo -u MONITOR_USER /path/to/ISIS_Beam_Monitor/.venv/bin/isis-monitor status /etc/isis-beam-monitor/config.ini`) with the same config file.
- **No live updates**: check the log for PVWS or MCR news errors; `isis-monitor reconnect path/to/config.ini` forces a reconnect.

## Releases

Versions follow [Semantic Versioning](https://semver.org/) and are set in `isis_monitor/__init__.py` (`__version__`), which is shown at the foot of every Teams card and by `isis-monitor --version`. The push site is versioned separately in its own repo. To release:

1. Move the `[Unreleased]` entries in `CHANGELOG.md` into a new `## [X.Y.Z] - YYYY-MM-DD` section, noting the push site version it works with, and set `__version__` to `X.Y.Z`.
2. Commit on the release branch, then tag and push both: `git tag -a vX.Y.Z -m vX.Y.Z && git push origin HEAD vX.Y.Z`.

The release workflow checks the tag matches `__version__`, runs the tests and publishes a GitHub release with that section's notes.
