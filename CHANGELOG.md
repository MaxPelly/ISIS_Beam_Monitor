# Changelog

All notable changes to the monitor are recorded here. Versions follow
[Semantic Versioning](https://semver.org/); a release is a `vX.Y.Z` tag, and
its notes are copied from the matching section below. The push site has its
own versions and changelog; each release here notes the push site version it
works with (before 2.1.0, the version its `push_site` submodule pointed at).

## [Unreleased]

- A run's "Previous run" on the next run's card is its own title, not the next run's: a title that comes once a run is ending, before its number moves on (live or re-sent after a reconnect), is the next run's. So is a start time then, which could have been taken as a re-begin, with a spurious restart card and a 0 µA·h completion.
- After a reconnect or an IOC outage, WDTITLE can come back seconds after the rest. A run begun meanwhile now waits up to 10s for its title before its card is sent, rather than being announced with the last run's; a run that ends within that wait is recorded without a card. A run re-begun meanwhile gets its restart card (if it was retitled) once its title comes, if within 10s, and that title is the one later restarts are compared with.
- A run that begins in the hour repeated when the clocks go back gets its real start time, not one an hour earlier.
- Between runs, the status shows the last run's number beside its title and counts, not the next number the DAE is ready with.
- A corrupt database no longer stops the daemon from starting: it's moved aside as `beam_monitor.db.corrupt-<time>`, with an error logged, and the daemon starts afresh. One that can't be opened for another reason (e.g. locked or unreadable) still stops it.
- A push site that hasn't taken the status (timing out, busy, or refusing it as no newer after the clock steps back) for as long as its Status page takes to show the monitor offline (3 of the status's `interval_s`, 90s by default) is warned of once, and its taking it again is logged.
- A blank `[DAEMON] db_path`, `socket_path` or `lock_file` is the default, rather than a temporary database (nothing saved) or a traceback.
- An unwritable `[LOGGING] file` is a configuration error rather than a traceback, and `daemon --dummy` no longer needs the push secret.
- `-n` posts the current MCR news straight away, not after the first poll interval.
- A lone surrogate in a notification or the status (e.g. in a PV's text) is sent as "?" rather than dropping it.
- The docs list `DAE:STARTTIME` among each instrument's PVs.

## [2.1.0] - 2026-10-16

Works with push site 2.0.0, deployed with it (see its README). The daemon's config, database, socket, lock and log move out of the checkout; the database and the instruments' saved state carry over when copied to the new `db_path`.

- The status goes to the push site within `[DAEMON] status_interval` (default 5s) of any change, including each beam's current and each run's µA·h, and at least every 30s, instead of every `sample_interval` (60s) and soon after a significant change. With the push site's 5s polling, its Status page is at most about 10s behind.
- **Breaking (deployment):** the `push_site` submodule is gone: the push site is installed on its own. `deploy/isis-beam-monitor.service` keeps the checkout read-only and the daemon's files outside it: the config in `/etc/isis-beam-monitor` (the daemon's user's, for `isis-monitor config`), the database in `/var/lib/isis-beam-monitor`, the socket and lock in `/run/isis-beam-monitor` and the log file in `/var/log/isis-beam-monitor`, with those paths set in `config.ini` (see the README). Copy the old `beam_monitor.db` to the new `db_path` before starting it, or the daemon starts afresh. With the push site, the unit gives the daemon the site's secret as its `push-secret` credential and the `isis-ingest` group for the site's socket.
- `[PUSH] url` can be `unix:` and a socket's absolute path, e.g. `unix:/run/isis-push-site/ingest.sock`: notifications go to `/ingest` and the status to `/ingest/status` over that socket, as push site 2.0.0 and later require.
- A push site the daemon can't connect to (e.g. its socket missing, or not the daemon's to use) is warned of once, "Can't reach the push site at …", and its taking the status again is logged; a slow reply isn't counted.
- `[PUSH] secret_file` is optional: unset, the daemon reads systemd's `push-secret` credential (`LoadCredential=push-secret:<file>`), so the secret file can stay root's.

## [2.0.0] - 2026-10-15

The Rich TUI goes, and the monitor becomes an installed command, `isis-monitor`. Includes push site v1.2.0. Upgrading from 1.3.0: run `pip install -e .` in the checkout and change the systemd unit's `ExecStart` to `<venv>/bin/isis-monitor daemon <config.ini>`, in one go with the restart. The config and each instrument's saved state carry on unchanged.

- The `push_site` submodule points at push site v1.2.0 (dependencies only in its `pyproject.toml`, and HTTP 503 rather than 500 while its database is locked).
- A 1.3.0rc release's saved instrument state is no longer read (1.3.0 converted it on its first save), so upgrade from 1.3.0, not straight from an rc.
- `[DAEMON] sample_interval` must be at least 10s (was 1s): each status sent to the push site bins the last 24h of samples, which at 1s took most of a second.
- A saved state that's corrupt (e.g. a number too large for a float) is logged and skipped, rather than stopping the daemon from starting.
- **Breaking:** the monitor is an installed command, `isis-monitor daemon|config|status|reconnect|stop <config.ini>`, and `main.py` is gone. Install it with `pip install -e .` in the checkout (`pip install -e '.[dev]'` for the tests); `requirements.txt` and `requirements-dev.txt` are gone, the dependencies being only in `pyproject.toml`. A systemd unit's `ExecStart` becomes `<venv>/bin/isis-monitor daemon <config.ini>` (see `deploy/isis-beam-monitor.service`).
- **Breaking:** the Rich TUI (`main.py tui`) is gone, and with it the `rich` dependency. The push site's Status page is the live display; on the host, `isis-monitor status` prints the same, `isis-monitor config` edits the config and `isis-monitor reconnect` replaces the `r` key. The `[TUI]` and `[TUI_CLIENT]` sections are no longer used (a leftover one is listed in the unknown-settings warning); delete them.
- The daemon keeps only the last 24h of beam history in memory, which is all the status and the daily summary read, rather than `[DAEMON] retention_days` of it; `retention_days` is how long the database keeps it, and no longer has to fit 100,000 samples per beam.
- The daemon's IPC socket is now request and reply only, one connection per command: `get_snapshot`, `get_history`, `get_logs` and `subscribe_updates` are gone, and `get_status` returns the status page's payload. On Linux it refuses a connection from any other user (even root), as well as being owner-only.
- `isis-monitor status <config.ini>` prints the daemon's status as plain text: each beam with its last 24h as a sparkline, each instrument's run and progress, the MCR news and health; `--json` prints the status page's JSON instead. `isis-monitor reconnect <config.ini>` makes the daemon reconnect to PVWS and re-read the MCR news, like the TUI's `r` key.
- `isis-monitor config <config.ini>` opens the config editor on its own, without the TUI. It talks to the running daemon like the TUI's `c` key did, with a fresh connection for each request.
- The commands exit 1 on failure (with the error on stderr) and 130 on Ctrl-C. A command gives up on a daemon that hasn't replied within 10s, and the daemon closes a connection left idle for 30s.
- A push site busy with its database (HTTP 5xx, e.g. push site 1.2.0's 503 while an `isis-push` command holds it) is logged at debug, not warned of as refusing the status: the next send replaces it.
- A `[DAEMON] socket_path` longer than a Unix socket path can be (107 bytes) is refused with a config error, instead of the daemon failing to start with a traceback.

## [1.3.0] - 2026-10-15

Run detection by run number goes live, replacing the title and counts heuristics, after the 1.3.0rc releases ran it in shadow alongside them (over a day of live data it matched every real run, and found every live-only card to be a retitle or an abort). Includes push site v1.1.4. No config change; from 1.3.0rc each instrument's saved state carries on. A run the old logic had already counted after a retitle is counted again when it really ends, so the next daily summary may count one run twice.

- Runs are detected by run number (`DAE:RUNNUMBER`), as the 1.3.0rc releases did in shadow, instead of by their title changing or their counts resetting. So retitling a run, or setting the next run's title before it begins, no longer sends a new-run card or counts as a run (SETUP retitles sent cards before), and an abort and re-begin under the same number isn't another run. The new-run card shows the run number, and a run begun again with a new title (e.g. a furnace's next temperature) gets a "Run … restarted" card. A run the daemon only saw end (after a reconnect, say) is recorded without a card.
- Stalls are only checked while a run is under way in RUNNING or VETOING and has collected something (a muon instrument's µA·h stay 0); the guess that a run at its target had probably ended is gone.
- Upgrading from 1.3.0rc: each instrument's saved state is carried over (the shadow's becomes the live one), so nothing resets. The `[shadow]` log lines are gone; run decisions are logged without the prefix.
- The `push_site` submodule points at push site v1.1.4, which pushes MCR news and a beam coming back on with high urgency, so an idle phone shows them straight away.
- Since rc3, the run detection makes a run the new experiment's when the RB number changes within 30s of its begin, by its STARTTIME, while it's under way (it checked for no counts yet, which with beam on there almost always are), and records a run first seen ending even when the DAE sends SETUP before the next run number.

## [1.3.0rc3] - 2026-10-14

1.3.0rc2 with the fixes from the 14 Oct review, so the shadow week runs on the run detection 1.3.0 will ship. Includes push site v1.1.3 (deploy it first). No config change and no reset of saved state: from rc2 it carries on where it left off.

- The `push_site` submodule points at push site v1.1.3 (1.1.2's Status page, which shows when it was updated and whose script and styles can't be served stale from a cache, plus 1.1.3's unknown and offline states).
- The shadow run detection also records a run first seen as it ends (e.g. after a reconnect), takes a run's final total the same way whatever order PVWS replays it in, makes a run that's only just begun the new experiment's when the RB number changes, ignores a run number that goes back to one already completed, and after a reconnect waits for the run number however long it takes.
- Config warnings (unknown keys, an `http://` push URL, a readable secret file) now reach the log file: they're logged once the daemon's logging is set up, rather than lost before it.
- An unknown `[LOGGING] level` now stops the daemon from starting, naming the allowed levels, instead of quietly logging at WARNING.
- A push site that refuses the status (e.g. HTTP 401 or 413) is warned of once, and its taking the status again is logged.
- On shutdown the notifiers are closed even if stopping the IPC server or saving the state fails.
- An instrument left out of a non-empty `CS:INSTLIST` falls back to its `beam_target`, else `[PVS] instrument_target`, rather than keeping the station from an older list; a list over 1 MB uncompressed is refused as unreadable.
- The status sends at most 64 instruments, the push site's limit.
- fun_mode's saved uptime records are no longer deleted on start: 1.2.0 and later already did so. Going from an earlier release straight to this one leaves them in the database, unused.
- Tags ending in `aN`, `bN` or `rcN` are published as GitHub pre-releases, so they don't become "Latest".

## [1.3.0rc2] - 2026-10-13

1.3.0rc1 plus the status publisher, for the push site's new Status page. Includes push site v1.1.0, which must be running first (an older push site refuses the status, which is only logged at debug). No config change, and no reset of saved state beyond rc1's: from rc1 it carries on where it left off, while from 1.2.0 the rc1 notes apply.

- With `[PUSH] url` set, the daemon also sends its status (beams with their last 24h, each instrument's run, MCR news, health) to the push site's `/ingest/status` every `sample_interval` and soon after a significant change, for the site's Status page (push site 1.1.0 or later; an older site just refuses it). While an instrument's IOC is down, the page shows its run state and counts as unknown.

## [1.3.0rc1] - 2026-10-13

A release candidate for comparing the new run detection with the current one live. Includes push site v1.0.3 (unchanged).

Upgrading: this release resets each instrument's saved state once (see below), so deploy it between experiments if you can. To compare the two run detections with `compare_shadow.py`, keep `[LOGGING] file = monitor.log`.

- Run detection by run number (`IN:<NAME>:DAE:RUNNUMBER`) runs in shadow alongside the current title and counts heuristics: it sends nothing, and logs what it would do with a `[shadow]` prefix. A run starts once it's under way past BEGINNING under a new number, completes when its number moves on, and an abort begins the same run again (seen by its `DAE:STARTTIME`, which is now subscribed to as well). Its counts are only credited once PVWS's replay after a reconnect, restart or outage has delivered both the run number and state.
- Each instrument's saved state has a new layout, kept by its tracker alone, and each run completion now records its run number. The first start after upgrading ignores the old instrument entries and run completions (one warning each), so the current experiment's progress, the auto-target history and the current run's start are forgotten, and the next daily summary under-reports runs.
- The TUI refreshes its instruments panel every 2s.

## [1.2.0] - 2026-10-13

Includes push site v1.0.3 (unchanged).

Upgrading: a stale `config.ini` still starts, and one warning lists the settings it no longer uses. To keep each instrument's beam target from ISIS's instrument list, delete any `beam_target` lines the config editor wrote, unless one is on purpose. Logs now go to stderr (journald under systemd); set `[LOGGING] file = monitor.log` to keep writing a file.

- `fun_mode` is gone, with its personality lines, uptime records, run-count milestones and daily fact. A leftover `[NOTIFICATIONS] fun_mode` is ignored, and the saved uptime records are deleted on start. Push payloads no longer carry `flavour`.
- Each beam target is configured in its own optional section, `[BEAM:TS1]`, `[BEAM:TS2]` or `[BEAM:Muons]`, with `pv` and `boundaries`; boundaries must be finite and in increasing order. These replace `[PVS] ts1_beam_current_pv`, `ts2_beam_current_pv` and `muon_beam_current_pv` and the `[BEAM_BOUNDARIES]` section, which are now ignored. Beam target names are case-insensitive and "Muon" still works, but the muon target is called "Muons" everywhere, so its beam cards are now titled "Muons …" rather than "Muon …", and the config editor writes `beam_target = Muons`.
- Each instrument's PV prefix and default beam target come from ISIS's instrument list, `CS:INSTLIST`, so instruments whose PVs aren't under `IN:<NAME>:` work. `beam_target` is now optional and best left unset; set it only for an instrument the list doesn't put on TS1, TS2 or MUON, or to override the list. `[PVS] instrument_target` changes meaning: it's now only the fallback for such an instrument (or one the list doesn't name). The config editor offers `auto` for the beam target and no longer writes an explicit one.
- `sample_interval` moves from `[TUI]` to `[DAEMON]`, since it sets how often the daemon samples beam history and saves its state.
- Only the daemon logs, to stderr (so journald under systemd) and, if `[LOGGING] file` is set, to a rotating file (5 MB × 3). `monitor.log` is no longer written by default. The TUI logs nothing and `stop` only prints warnings, so neither touches the daemon's log. `[LOGGING] log_file` and `log_level` are now `file` and `level`; `log_max_bytes` and `log_backup_count` are gone.
- On start, one warning lists every section and key in `config.ini` that isn't used, such as a typo or a setting retired by an upgrade. Before, only instrument sections were checked.
- `[WEBHOOKS]` URLs must start with `https://`, since whoever has one can post to the channel. A plain `http://` `[PUSH] url` to a host other than this one logs a warning.
- Each notifier (each Teams webhook, and the push site) has its own queue, so a slow or failing one no longer delays the others. One push notifier serves all three channels, so the push feed keeps the order notifications were sent in, and push retries match Teams'.

## [1.1.1] - 2026-10-13

Includes push site v1.0.3, whose feed keeps the newest 75 notifications of each kind (see its changelog for a `push_site.ini` change).

- MCR news posted while the daemon was stopped is announced when it starts again, instead of being taken as the baseline.
- A failure to send a beam change card is logged instead of being lost.
- Each notification is logged on one line, and routine shutdown is logged at INFO rather than WARNING.
- `pip install -e .` works from a checkout.
- `.gitignore` covers `config.ini.bak` and the config editor's temporary files, which can hold webhook URLs.

## [1.1.0] - 2026-10-11

Includes push site v1.0.2, whose feed has a tab for each kind of notification.

- `notify_counts = auto, <number>` sets each run's target from the median of the experiment's recent runs, ignoring very short ones; `<number>` is used until there are 3 runs that aren't very short, and the history restarts when the instrument's RB number changes. The TUI shows each run's target, marked ≈ when it's an auto one.
- Stalls are only checked while the instrument's DAE is RUNNING or VETOING (`IN:<NAME>:DAE:RUNSTATE_STR`), so a paused run, a run-control wait or a run ended short of its target is no longer reported as stalled.
- A "data collection resumed" card follows a stall warning once µA·h are collected again.
- Finishing and stall cards name the run number (`IN:<NAME>:DAE:RUNNUMBER`), and the new-run card shows the new run's target.
- The daily summary lists each instrument on that target with its runs and µA·h collected over the last 24h.
- An "experiment finished" card summarises each experiment (runs, µA·h collected, average run, duration) when the instrument's RB number (`IN:<NAME>:DAE:_RBNUMBER`) changes or goes to "0" (no experiment).

## [1.0.1] - 2026-10-09

Includes push site v1.0.1.

- The TUI header shows the daemon's version, and the TUI's own if it differs (e.g. after an update without a restart).
- Push notifications carry the daemon's version as `monitor_version`, which the push site shows in its footer.

## [1.0.0] - 2026-10-09

First versioned release. Includes push site v1.0.0.

- Beam state cards for TS1, TS2 and Muons, run tracking for any number of instruments, MCR news and a daily summary, sent as Teams Adaptive Cards and optionally to the push site.
- TUI client with live graphs, an instruments panel and a config editor.
- The daemon's version is shown at the foot of every Teams card, logged at startup and printed by `python main.py --version`.
