# ISIS Beam Monitor Codebase Documentation

This document provides a technical overview of the ISIS Beam Monitor codebase, its architecture, components, and guidance for future development.

## Architecture Overview

The ISIS Beam Monitor is a real-time monitoring system designed to track accelerator beam status and MCR (Main Control Room) news updates at the ISIS Neutron and Muon Source. It follows a decoupled, asynchronous architecture using Python's `asyncio` for concurrent operations.

### High-Level Design
The system uses a two-tier architecture (daemon and client) communicating via local UNIX domain sockets:
1.  **Daemon**: A long-lived background process holding the master `DaemonState` (in `daemon_state.py`). It orchestrates monitors, persists state to a local SQLite database (`storage.py`), and serves multiple clients via JSON over IPC (`ipc.py`).
2.  **Monitors**: Asynchronous tasks that fetch and process data from external sources (WebSockets for beam data, HTTP polling for MCR news). They report into the `DaemonState` passed to them as `sink` (a mock in tests).
3.  **Notifications**: `isis_monitor/messages.py` builds channel-agnostic `Notification` objects (title, severity, emoji, facts, optional flavour/URL) for every notification-worthy event; `notifiers.py` renders and delivers them (e.g. as Microsoft Teams Adaptive Cards) via `NotificationChannel`s.
4.  **TUI Client**: A terminal UI built with the `rich` library. It acts as an IPC client, fetching the initial state snapshot from the daemon and then subscribing to a real-time event stream to update its display. It can also edit a subset of the config over IPC; the daemon validates and saves it, then re-execs itself to apply it.

---

## Component Deep Dives

### `isis_monitor/beam.py`
The core logic for accelerator beam monitoring.
-   **`BeamMonitor`**: Manages the single WebSocket connection. It subscribes to the three beam-current PVs plus every instrument's counts and run-name PVs, handles beam updates itself, and routes instrument PVs to the owning `InstrumentTracker` (`pv_to_counts` / `pv_to_run_name`; config validation guarantees no PV is shared). `run()` works until cancelled: it gathers the websocket loop (`_run_loop`, a plain `async for` over messages that reconnects after `beam_reconnect_interval`, or immediately when `request_reconnect()` closes the socket; `_classify_ws_error()` sorts failures into transient ones (dropped connections, timeouts, 5xx) and persistent ones (a rejected handshake, bad URL, TLS or DNS failure, or an unexpected error), which set beam health to `"error"` and double the retry wait up to `BEAM_MAX_BACKOFF` (300s, or the interval if that's longer) until the next successful connection; repeats of the same kind of failure are logged at debug, and one line summarises the outage on reconnect) with `_collection_check_loop`, which runs each tracker's stall check roughly every 60s (one failing tracker is logged and doesn't stop the others). A malformed PV update is logged and skipped rather than dropping the connection.
-   **`BeamTarget`**: `state_key` ("TS1"/"TS2"/"Muon", also used in messages and as an instrument's `beam_target`) and `channel_label` ("TS1"/"TS2"/"Muons", used by the sink, TUI and notification routing). Both come from `config.TARGET_LABELS`, the one definition of the three targets (`BEAM_TARGET_KEYS` and `CHANNEL_LABELS` are derived from it).
-   **State Management**: Tracks current beam currents and power levels (off, low, medium, high) to detect transitions, plus per-target `since` timestamps; held in `BeamMonitor.beams` (one `BeamState` per target); `_beam_power(target)` lets trackers look up their instrument's beam. Flavour lines use one rng that `BeamMonitor` passes to the aggregator and trackers only when `fun_mode` is on (otherwise `None`).
-   **`BeamChangeAggregator`**: Debounces raw beam-state transitions for `debounce_seconds` before turning them into notifications, dropping ones that flap back to their original state, and noting when other targets went off in the same window (the three targets share one accelerator, so correlated trips are the common case, not an edge case).

### `isis_monitor/instrument.py`
Run and counts tracking for one instrument (one `InstrumentTracker` per `config.instruments` entry).
-   **`InstrumentTracker`**: Handles its instrument's run-name PV (new-run cards, run-completion recording and, with `fun_mode`, a milestone card every `RUN_MILESTONE_INTERVAL` (25) runs of that instrument) and counts PV (the "run about to finish" card, sent once there are `ETA_MIN_HISTORY` (3 minutes) of samples and the fitted rate puts the run within `finish_warning_minutes` of its `notify_counts`, or else on passing `notify_counts`; it is sent once and re-armed only when counts clearly drop (by more than 1) to more than 25 below the target; before the target the rate fit runs at most every `ETA_CHECK_INTERVAL` (30s)), and `check_collection_progress()` for stall warnings, suppressed while its `beam_target` is off.
-   **`InstrumentState`**: run name, start time, counts, and a 15-minute deque of `(time, counts_collected)` samples used for the finishing card's rate/ETA ("target reached" when it was only sent on reaching the target). Counts come from the instrument's `counts_pv`, derived as `IN:<NAME>:DAE:TOTALUAMPS`: a numeric `value` giving the total µA·h collected this run (0 at the start of a run is a real reading; NaN is ignored).

### `isis_monitor/mcr.py`
Handles MCR news polling.
-   **`MCRNewsMonitor`**: Polls the news feed at a configurable interval until cancelled. It uses regex to parse the feed and detect changes in the latest news entry; `request_reconnect()` wakes the poll wait early.
-   **Adaptive Polling**: Implements exponential backoff on fetch failures to reduce load on the source during outages.
-   **Severity classification**: `messages.mcr_news()` classifies each update as GOOD/ATTENTION/WARNING/INFO by keyword (see `messages.py` below) and can attach an "Open MCR news" link via `[DATA] mcr_page_url`.

### `isis_monitor/messages.py`
Builds the structured, channel-agnostic notifications used everywhere else.
-   **`Notification`**: A dataclass (title, text, severity, emoji, facts, flavour, url/url_label, timestamp, channel, topic) with a `to_plain_text()` method used by `DummyNotifier` and log lines.
-   **`Severity`**: `INFO` / `GOOD` / `WARNING` / `ATTENTION`, mapped to Adaptive Card container styles by `notifiers.py`.
-   **Builders**: one pure function per notification-worthy event — `beam_change`, `startup_status`, `run_started`, `run_finishing`, `collection_stalled`, `mcr_news`, `daily_summary`, `run_milestone`. Each takes an optional `rng: random.Random` so callers can opt into a `flavour.py` line (only when `fun_mode` is on) while keeping the builders deterministic and pure for tests. The run builders take the instrument name as their first argument and prefix it to the title, and an optional `channel`: blank (the default) is filled with "Experiment Updates" on broadcast, which downstream Power Automate flows route on; `InstrumentTracker._card_channel()` passes the instrument's name instead when its config sets `channel = instrument`.
-   **`fmt_time` / `fmt_duration` / `set_timezone` / `get_timezone`**: shared formatting helpers; the display timezone is process-global, set once from `[NOTIFICATIONS] timezone` at startup.

### `isis_monitor/flavour.py`
Optional personality content shown only when `fun_mode = true`.
-   **`pick(key, rng)`**: a random line from the pool for a transition or event (e.g. `"off"`, `"new_run"`), or `""` if there is none.
-   **`fact_of_the_day(rng)`**: a small pool of neutron/ISIS trivia shown once per day on the daily summary card.

### `isis_monitor/summary.py`
Daily per-target uptime summaries and milestone tracking.
-   **`compute_summary(history, since, until=None, sample_interval=60.0)`**: a pure function over `DaemonState.history`'s samples, returning each target's uptime %, trip count, longest continuous on-streak, a sparkline (via `tui.sparkline_chars`) and `coverage_pct`, the share of expected samples actually recorded. Samples aren't recorded while the beam feed is down, so uptime covers only the time the beam could be seen, an on-streak ends at a gap longer than 3 sample intervals (at least 3 minutes, so a daemon restart doesn't end one), and a trip that starts and ends inside a gap isn't counted; the card adds a "Data coverage" fact when coverage is under 99%.
-   **`daily_summary_loop`**: runs inside `run_daemon`, checks the configured local time (`[NOTIFICATIONS] summary_time`) once a minute, and sends one card per target when it's reached (its "Runs in last 24h" counts only instruments whose `beam_target` is that target) (deduped to once per day via the `"summary_last_sent"` snapshot key, so a restart doesn't resend). When `fun_mode` is on, it also tracks each target's longest-ever on-streak in the SQLite snapshot under the key `"records"` and flags a new record.

### `isis_monitor/notifiers.py`
A decoupled notification system.
-   **`Notifier` (Abstract)**: Base class for notification implementations; `send()` takes a `Notification`.
-   **`HTTPNotifier`**: Base for notifiers that POST JSON: subclasses provide `_create_payload()` (and may override `_request_kwargs()`), and it handles the session, retries and `Retry-After` described under `TeamsNotifier`.
-   **`TeamsNotifier`**: Renders a `Notification` as a Microsoft Teams Adaptive Card (severity-coloured header, body text, italic flavour line, `FactSet`, optional `Action.OpenUrl` button) and posts it via webhook. A 429, a 5xx or a network error/timeout is retried after `RETRY_DELAYS` (2s, then 4s: three attempts in all), or after the server's `Retry-After` (seconds or HTTP date) capped at `MAX_RETRY_AFTER` (60s); other 4xx responses aren't retried. The channel's worker waits during retries, so later notifications on that channel are delayed (bounded by the attempts, `webhook_timeout` and the delays), but other channels and callers aren't.
-   **`WebhookNotifier`**: POSTs the `Notification` as versioned JSON (`"v": 1`, a fresh `id`, its fields including `topic`, and `summary`) signed with `X-Timestamp` and `X-Signature` (hex HMAC-SHA256 of `<timestamp>.<body>`). Each attempt is re-signed but keeps the `id`, so a receiver can drop repeats. `[PUSH] url` adds one to every channel, with a short `timeout` (2s default), retries after 1s and 2s and `Retry-After` capped at 5s, because the notifiers in a channel are sent together: a slow or failing receiver delays that channel's next Teams card by at most about 16s (3 × 2s timeout + 5s + 5s).
-   **`NotificationChannel`**: Groups multiple notifiers for a specific category of updates (e.g., "Beam Updates"). `broadcast()` only queues the notification (bounded at 100; the oldest is dropped with a warning when full) and returns; one worker task per channel sends them in order, so a slow or hung webhook never holds up the caller — in particular the beam WebSocket loop, which would otherwise stop reading and miss keepalive pongs. A failing notifier is logged without affecting the others. `close()` gives queued notifications up to `CLOSE_TIMEOUT` (5s) to go out, then stops the worker and releases every notifier's resources; later broadcasts are logged and dropped.

### `isis_monitor/tui.py`
The live terminal interface.
-   **`RichTUI`**: Coordinates the layout and rendering. All updates arrive on the event loop from the IPC event stream (`main.tui_connection_loop`); history comes from the daemon's `sample` events.
-   **Compact layout**: below `COMPACT_WIDTH` (80) columns `_make_layout()` builds a phone-sized layout (header, beams on one line, two lines per instrument, MCR news) with no graph or logs panel; their updates return early but the data is still kept. `set_compact()` rebuilds the layout and swaps it into Live; `main.run_tui` calls `fit_to_width()` at start and on `SIGWINCH`, and the `v` key calls `toggle_compact()`, after which resizes no longer change the layout.
-   **`sparkline_chars(values, width)`**: a pure, uncoloured sparkline renderer shared with `summary.py`; `_render_sparkline()` wraps it to add per-block colour for the TUI.
-   **Instruments panel**: `set_instruments()` (from the snapshot) and `update_instrument()` (from `run`/`counts` events) feed a table of run name and a progress bar towards `notify_counts`, capped at 8 rows plus "+N more".
-   **Keys** (`main.run_tui`): stdin is in cbreak mode and read with `os.read` (not `sys.stdin`, whose buffer can swallow keys or block the loop). `c` hands the terminal to the config editor: the key reader is removed, Live is stopped and canonical mode restored, then everything is put back when the editor exits (or skipped if the TUI is quitting). Keys read in the same chunk as `c` are passed to the editor as `typed_ahead`.

### `isis_monitor/config_editor.py`
The line-based editor behind the TUI's `c` key. `edit_settings()` works on the `editable_settings()` dict (numbered menu, add/edit/delete instruments, diff via `describe_changes()`, confirm before save); `run_config_editor()` fetches it with `get_config` and saves with `update_config`, returning to the menu with edits intact on `invalid_config` or a lost connection. Terminal I/O (`read_line`, `write`) and the request function are injected, so it is tested with scripted input; `main.run_tui` supplies a request function that always uses the currently connected client.

### `isis_monitor/daemon_state.py` & `storage.py`
The core state management and persistence layer.
-   **`DaemonState`**: An event-loop-confined object (not thread-safe; `main.StateLogHandler` hands off log records from other threads via `call_soon_threadsafe`) holding current beam statuses, historical data buffers, MCR news, health checks, and per-instrument state in `instruments` (run name, counts, all-time `total_runs`, plus `notify_counts`/`beam_target` from the config for display). Updates for instruments not in the config are ignored. `record_run_completed(instrument, ts)` and `count_runs_completed_since(since, instruments=None)` track completions; `total_runs` persists across restarts via the snapshot (instruments removed from the config are dropped), as do the last `RUN_COMPLETIONS_WINDOW` (24h) of the `run_completions` deque, so the daily summary's run count survives a restart. It manages a pub/sub queue system for IPC clients: a subscriber whose queue fills is dropped and sent a `None` sentinel, so its connection closes and the client resyncs. `update_health()` only publishes actual status changes; any beam health other than `"connected"` also sets every beam's power to `"unknown"` (keeping its last current), and restored beam states start as `"unknown"` too, so stale values never look live. PVWS re-sends every value on reconnect.
-   **`SQLiteStateStore`**: Persists 1-minute beam samples and JSON snapshots (`"daemon_state"`, `"records"`, `"summary_last_sent"`), enabling crash recovery and historical lookups. `main.state_persistence_loop` writes to it every `sample_interval` (recording beam samples only while beam health is `"connected"` and skipping beams still `"unknown"` just after a reconnect, leaving a gap in the history otherwise), logging and retrying on `sqlite3.Error` rather than crashing the daemon (the daily summary loop handles errors the same way). All async access goes through `store.run(fn, ...)`, which uses a single worker thread: the connection is shared, and concurrent use from several threads corrupts it. The snapshot also carries each instrument's `run_started_at` and `end_notified` (kept on restore only if the instrument's `notify_counts` is unchanged, since a card for another target doesn't cover the new one), which `run_daemon` restores into the trackers (via `BeamMonitor.restore_instruments`) so restarts don't reset a run's clock or re-send its finishing card; connection health is deliberately not restored.

### `isis_monitor/ipc.py`
Manages local communication between the daemon and clients.
-   **`IPCServer`**: A UNIX domain socket server speaking newline-delimited JSON, created owner-only (0600). Methods: `get_snapshot`, `get_history` (optional `limit` = newest N samples per beam), `get_logs`, `subscribe_updates`, `command` (`force_reconnect_all`, `force_reconnect_beam`, `force_reconnect_mcr`, `shutdown`, `restart`), and — via the optional `config_handler` — `get_config` (editable settings read fresh from the file, a `revision` hash, and the allowed `beam_targets` and `channel_modes`) and `update_config` (`settings` plus that `revision`; errors `invalid_config`, `config_changed`, `restart_pending`, `invalid_request`, `config_write_failed`). Malformed or failing requests get an `ok: false` reply without dropping the connection. `stop()` closes attached clients itself, since Python 3.12's `Server.wait_closed()` otherwise waits for them forever, gives them `STOP_FLUSH_TIMEOUT` (1s) to take already-written replies (e.g. to the `update_config` that triggered a restart), then aborts any that aren't reading. A subscriber whose event queue overflows, or which takes nothing for `SUBSCRIBER_DRAIN_TIMEOUT` (30s), is aborted; the client reconnects and resyncs.
-   **`IPCClient`**: One background reader task routes replies and pushed events to separate queues, so `request()` and `iter_events()` can run concurrently. Once the connection ends, every later call raises instead of hanging. `request(..., timeout=)` closes the client on timeout, since a late reply would otherwise be taken as the next request's answer; the TUI's sync (which subscribes before fetching state, so no event is missed) and `main.py stop` use `IPC_REQUEST_TIMEOUT` (10s). Reconnection backoff lives in `main.tui_connection_loop`.

### `main.py` — restarting the daemon
`run_daemon` returns `True` when a restart was requested (a successful `update_config` or the `restart` command; config edits are serialised with a lock and refused once a restart is pending). `main()` then, after the lock, database and socket are released, calls `restart_process()`: it flushes output and `os.execve`s the same interpreter and `sys.orig_argv`, keeping the PID, with `ISIS_MONITOR_RESTARTED=1` so the new process ignores `-n/--notify_current`. If exec fails it prints the error and exits 1. Tests make `os.execve` raise via an autouse fixture, since a real exec would replace pytest.

---

## Configuration

Configuration is managed via `config.ini` files, loaded through `isis_monitor/config.py`. Key sections include:
-   **`[DATA]`**: WebSocket and HTTP URLs for data sources, plus the optional `mcr_page_url` link button.
-   **`[WEBHOOKS]`**: URLs for Teams integration (should be kept secure). A blank URL disables that channel's Teams notifier.
-   **`[PUSH]`**: meant for the optional `push_site` git submodule (a separate repo and a PWA that sends mobile Web Push; it never imports this package, and the signed payload is the only contract). Settings are optional `url`, `secret_file` and `timeout` for the `WebhookNotifier`. `parse_config()` only checks `url` and that `secret_file` is set; `main()` reads the secret with `read_push_secret()` in daemon mode only (the TUI and `stop` never need it), into `AppConfig.push_secret` (left out of `repr`): at most 4 KB, surrounding whitespace dropped, at least 32 characters, warning if the file is world-readable. These keys aren't TUI-editable.
-   **`[INSTRUMENT:<NAME>]`**: one per instrument — `notify_counts` (required, µA·h), `beam_target` and `channel` (`experiment`/`instrument`, both optional); at least one is required; parsed into `AppConfig.instruments` (`InstrumentConfig`, with `counts_pv` derived as `IN:<NAME>:DAE:TOTALUAMPS` and `run_name_pv` as `IN:<NAME>:DAE:WDTITLE`).
-   **`[PVS]`**: `instrument_target` (the default `beam_target` for instrument sections that don't set one; TS1) and the beam-current PVs.
-   **`[DAEMON]`** / **`[TUI_CLIENT]`**: Paths for UNIX sockets, SQLite database, and retention settings.
-   **`[BEAM_BOUNDARIES]`**: Thresholds for power level classification.
-   **`[TUI]`**: Display settings like history length and refresh rates.
-   **`[NOTIFICATIONS]`**: `fun_mode` (personality lines/milestones), `timezone` (for card timestamps and `summary_time`), `debounce_seconds` (beam-change confirmation window, 0–3600), `stall_minutes` (collection-stall warning threshold, up to 7 days), `finish_warning_minutes` (how far ahead of `notify_counts` the finishing card is sent; 0 = only on reaching it), and `summary_time` (HH:MM local time the daily summary is sent).

`load_config()` is a thin wrapper over `parse_config(parser, path)`, which also validates everything that could otherwise fail only after a restart (timezone, finite bounds, instrument names and PV clashes). `editable_settings()` exposes the TUI-editable subset (the `[NOTIFICATIONS]` keys above and each instrument's name, `notify_counts`, `beam_target` and `channel`) as INI strings; the daemon's `get_config` adds the allowed `beam_targets` and `channel_modes`; `update_config_file()` applies such a dict, validates the result, checks the file still matches the given `config_revision()`, then writes it atomically (temp file, `fsync`, `os.replace`, directory `fsync`), keeping the file mode and a `.bak` copy. `configparser` drops comments on rewrite.

---

## Customizing the TUI Layout

The TUI is built using `rich.layout.Layout`. You can adjust the proportions and sizes of the interface by modifying `isis_monitor/tui.py`.

### Adjusting Section Sizes
In `RichTUI._make_layout()`, sections are defined using `split_column` and `split_row`.
- **Fixed Height**: Use the `size` argument (e.g., `Layout(name="header", size=3)`) to set a fixed number of rows.
- **Proportional Width/Height**: Use the `ratio` argument (e.g., `Layout(name="left", ratio=1)`) to make a section take up a proportion of the available space.

### Column Widths & Internal Padding
- **Table Columns**: The beam status table in `_update_beam_panel()` uses `expand=True`. To adjust individual column behaviors, modify the `table.add_column()` calls.
- **Graph Width**: The TUI automatically scales sparklines using `shutil.get_terminal_size()`, but you can override `SPARK_WIDTH` in `_update_beam_graph()` if you want a fixed size.

---

## Advice for Future Changes

### Future Considerations
-   **aiosqlite (deliberately deferred)**: `storage.py` runs every SQLite call on one worker thread via `store.run()`. The load is tiny (three beam rows and one snapshot per `sample_interval`, plus the daily summary), and the single thread already solves the connection's thread-safety problem, so moving to `aiosqlite` would add a dependency and a rewrite of `storage.py`, both loops and their tests for no measurable gain. Revisit it only if persistence becomes measurably slow, e.g. `state_persistence_loop` ticks overrunning `sample_interval`.

### Potential Features
-   **Prometheus Exporter**: Add a lightweight HTTP endpoint to export beam metrics and health status for ingestion by Prometheus/Grafana.
-   **Interactive TUI**: The TUI already has `r` (reconnect) and `c` (config editor) and `v` (compact/full layout); further shortcuts could toggle specific notification channels. The editor only covers `[NOTIFICATIONS]` and instruments; extending `EDITABLE_NOTIFICATION_KEYS`/`editable_settings()` would expose more (keep secrets like webhook URLs out of it).
-   **Multiple Notifiers**: Add support for Email, Slack, or SMS notifiers by implementing the `Notifier` interface.

### Best Practices for Extension
1.  **Async/Await**: Ensure all blocking I/O (like networking or DB access) is handled asynchronously (or wrapped in `to_thread`, or `store.run` for SQLite) to prevent freezing the TUI or Daemon.
2.  **State Safety**: `DaemonState` and `RichTUI` are only touched from the event-loop thread. Code running in a worker thread must hand results back to the loop rather than mutating them directly.
3.  **Shutdown**: Monitors run until cancelled (`main.run_until_stopped`), so they need no stop-event plumbing; loops that write to SQLite instead watch `stop_event` so an in-flight write finishes before the store is closed.
4.  **Tests**: `isis_monitor/tests` runs with `pytest` (pytest-asyncio in auto mode, so async tests need no decorator). Shared helpers live in `isis_monitor/tests/helpers.py`: `wait_until`, `FakePVWS` (a local PVWS stand-in, optionally on a fixed port), `running`, `serving`/`connected`/`raw_request` for IPC, `never_answers` and `fake_channel`. Prefer waiting on a condition over fixed sleeps.
