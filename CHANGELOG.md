# Changelog

All notable changes to the monitor are recorded here. Versions follow
[Semantic Versioning](https://semver.org/); a release is a `vX.Y.Z` tag, and
its notes are copied from the matching section below. The push site has its
own versions and changelog; each release here notes the version its
`push_site` submodule points at.

## [Unreleased]

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
