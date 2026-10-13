# Changelog

All notable changes to the monitor are recorded here. Versions follow
[Semantic Versioning](https://semver.org/); a release is a `vX.Y.Z` tag, and
its notes are copied from the matching section below. The push site has its
own versions and changelog; each release here notes the version its
`push_site` submodule points at.

## [Unreleased]

- `sample_interval` moves from `[TUI]` to `[DAEMON]`, since it sets how often the daemon samples beam history and saves its state.

- `[WEBHOOKS]` URLs must start with `https://`, since whoever has one can post to the channel. A plain `http://` `[PUSH] url` to a host other than this one logs a warning.

- On start, one warning lists every section and key in `config.ini` that isn't used, such as a typo or a setting retired by an upgrade. Before, only instrument sections were checked.

- Each beam target is configured in its own optional section, `[BEAM:TS1]`, `[BEAM:TS2]` or `[BEAM:Muons]`, with `pv` and `boundaries`; boundaries must be finite and in increasing order. These replace `[PVS] ts1_beam_current_pv`, `ts2_beam_current_pv` and `muon_beam_current_pv` and the `[BEAM_BOUNDARIES]` section, which are now ignored. Beam target names are case-insensitive and "Muon" still works, but the muon target is called "Muons" everywhere, so its beam cards are now titled "Muons …" rather than "Muon …", and the config editor writes `beam_target = Muons`.

- Each notifier (each Teams webhook, and the push site) has its own queue, so a slow or failing one no longer delays the others. One push notifier serves all three channels, so the push feed keeps the order notifications were sent in, and push retries match Teams'.

- Only the daemon logs, to stderr (so journald under systemd) and, if `[LOGGING] file` is set, to a rotating file (5 MB × 3). `monitor.log` is no longer written by default. The TUI logs nothing and `stop` only prints warnings, so neither touches the daemon's log. `[LOGGING] log_file` and `log_level` are now `file` and `level`; `log_max_bytes` and `log_backup_count` are gone.

- `fun_mode` is gone, with its personality lines, uptime records, run-count milestones and daily fact. A leftover `[NOTIFICATIONS] fun_mode` is ignored, and the saved uptime records are deleted on start. Push payloads no longer carry `flavour`.

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
