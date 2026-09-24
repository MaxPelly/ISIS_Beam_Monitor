# ISIS Beam Monitor Codebase Documentation

This document provides a technical overview of the ISIS Beam Monitor codebase, its architecture, components, and guidance for future development.

## Architecture Overview

The ISIS Beam Monitor is a real-time monitoring system designed to track accelerator beam status and MCR (Main Control Room) news updates at the ISIS Neutron and Muon Source. It follows a decoupled, asynchronous architecture using Python's `asyncio` for concurrent operations.

### High-Level Design
The system uses a two-tier architecture (daemon and client) communicating via local UNIX domain sockets:
1.  **Daemon**: A long-lived background process holding the master `DaemonState` (in `daemon_state.py`). It orchestrates monitors, persists state to a local SQLite database (`storage.py`), and serves multiple clients via JSON over IPC (`ipc.py`).
2.  **Monitors**: Asynchronous tasks that fetch and process data from external sources (WebSockets for beam data, HTTP polling for MCR news). They feed data into the `DaemonState` via `MonitorSinkProtocol`.
3.  **Notifications**: `isis_monitor/messages.py` builds channel-agnostic `Notification` objects (title, severity, emoji, facts, optional flavour/URL) for every notification-worthy event; `notifiers.py` renders and delivers them (e.g. as Microsoft Teams Adaptive Cards) via `NotificationChannel`s.
4.  **TUI Client**: A terminal UI built with the `rich` library. It acts as an IPC client, fetching the initial state snapshot from the daemon and then subscribing to a real-time event stream to update its display.

---

## Component Deep Dives

### `isis_monitor/beam.py`
The core logic for accelerator beam monitoring.
-   **`BeamMonitor`**: Manages the WebSocket connection and state. `run()` works until cancelled: it gathers the websocket loop (`_run_loop`, a plain `async for` over messages that reconnects after `beam_reconnect_interval`, or immediately when `request_reconnect()` closes the socket) with `_collection_check_loop`, which checks for stalled data collection roughly every 60s. A malformed PV update is logged and skipped rather than dropping the connection.
-   **`BeamTarget`**: `state_key` ("TS1"/"TS2"/"Muon", also used in messages) and `channel_label` ("TS1"/"TS2"/"Muons", used by the sink, TUI and notification routing).
-   **State Management**: Tracks current beam currents and power levels (off, low, medium, high) to detect transitions, plus per-target `since` timestamps and a 15-minute deque of `(time, counts_collected)` samples used for the run-finishing card's rate/ETA. `counts_pv`'s text is `live_current/total_collected` — only the total is tracked; live current is discarded since beam current is already tracked directly via the TS1/TS2/Muon PVs.
-   **`BeamChangeAggregator`**: Debounces raw beam-state transitions for `debounce_seconds` before turning them into notifications, dropping ones that flap back to their original state, and noting when other targets went off in the same window (the three targets share one accelerator, so correlated trips are the common case, not an edge case).
-   **Run-count milestones**: `DaemonState.record_run_completed()` is called whenever a run completes; `beam.py` checks the returned all-time total against `RUN_MILESTONE_INTERVAL` (25) to fire a milestone card when `fun_mode` is on.

### `isis_monitor/mcr.py`
Handles MCR news polling.
-   **`MCRNewsMonitor`**: Polls the news feed at a configurable interval until cancelled. It uses regex to parse the feed and detect changes in the latest news entry; `request_reconnect()` wakes the poll wait early.
-   **Adaptive Polling**: Implements exponential backoff on fetch failures to reduce load on the source during outages.
-   **Severity classification**: `messages.mcr_news()` classifies each update as GOOD/ATTENTION/WARNING/INFO by keyword (see `messages.py` below) and can attach an "Open MCR news" link via `[DATA] mcr_page_url`.

### `isis_monitor/messages.py`
Builds the structured, channel-agnostic notifications used everywhere else.
-   **`Notification`**: A dataclass (title, text, severity, emoji, facts, flavour, url/url_label, timestamp) with a `to_plain_text()` method used by `DummyNotifier` and log lines.
-   **`Severity`**: `INFO` / `GOOD` / `WARNING` / `ATTENTION`, mapped to Adaptive Card container styles by `notifiers.py`.
-   **Builders**: one pure function per notification-worthy event — `beam_change`, `startup_status`, `run_started`, `run_finishing`, `collection_stalled`, `mcr_news`, `daily_summary`, `run_milestone`. Each takes an optional `rng: random.Random` so callers can opt into a `flavour.py` line (only when `fun_mode` is on) while keeping the builders deterministic and pure for tests.
-   **`fmt_time` / `fmt_duration` / `set_timezone` / `get_timezone`**: shared formatting helpers; the display timezone is process-global, set once from `[NOTIFICATIONS] timezone` at startup.

### `isis_monitor/flavour.py`
Optional personality content shown only when `fun_mode = true`.
-   **`pick(key, rng)`**: line pools keyed by `(beam target or "*", transition)`, falling back to the `"*"` pool when there's no target-specific one.
-   **`fact_of_the_day(rng)`**: a small pool of neutron/ISIS trivia shown once per day on the daily summary card.

### `isis_monitor/summary.py`
Daily per-target uptime summaries and milestone tracking.
-   **`compute_summary(history, since)`**: a pure function over `DaemonState.history`'s 1-minute samples, returning each target's uptime %, trip count, longest continuous on-streak and a sparkline (via `tui.sparkline_chars`).
-   **`daily_summary_loop`**: runs inside `run_daemon`, checks the configured local time (`[NOTIFICATIONS] summary_time`) once a minute, and sends one card per target when it's reached (deduped to once per day via the `"summary_last_sent"` snapshot key, so a restart doesn't resend). When `fun_mode` is on, it also tracks each target's longest-ever on-streak in the SQLite snapshot under the key `"records"` and flags a new record.

### `isis_monitor/notifiers.py`
A decoupled notification system.
-   **`Notifier` (Abstract)**: Base class for notification implementations; `send()` takes a `Notification`.
-   **`TeamsNotifier`**: Renders a `Notification` as a Microsoft Teams Adaptive Card (severity-coloured header, body text, italic flavour line, `FactSet`, optional `Action.OpenUrl` button) and posts it via webhook.
-   **`NotificationChannel`**: Groups multiple notifiers for a specific category of updates (e.g., "Beam Updates"). A failing notifier is logged without affecting the others; `close()` releases every notifier's resources.

### `isis_monitor/tui.py`
The live terminal interface.
-   **`RichTUI`**: Coordinates the layout and rendering. All updates arrive on the event loop from the IPC event stream (`main.tui_connection_loop`); history comes from the daemon's `sample` events.
-   **`sparkline_chars(values, width)`**: a pure, uncoloured sparkline renderer shared with `summary.py`; `_render_sparkline()` wraps it to add per-block colour for the TUI.

### `isis_monitor/daemon_state.py` & `storage.py`
The core state management and persistence layer.
-   **`DaemonState`**: An event-loop-confined object (not thread-safe; `main.StateLogHandler` hands off log records from other threads via `call_soon_threadsafe`) holding current beam statuses, historical data buffers, MCR news, health checks, and run-completion tracking (`record_run_completed()`, `count_runs_completed_since()`; `total_runs_completed` persists across restarts via the snapshot, the rolling 24h `run_completions` deque does not). It manages a pub/sub queue system for IPC clients: a subscriber whose queue fills is dropped and sent a `None` sentinel, so its connection closes and the client resyncs. `update_health()` only publishes actual status changes.
-   **`SQLiteStateStore`**: Persists 1-minute beam samples and JSON snapshots (`"daemon_state"`, `"records"`, `"summary_last_sent"`), enabling crash recovery and historical lookups. `main.state_persistence_loop` writes to it every `sample_interval`, logging and retrying on `sqlite3.Error` rather than crashing the daemon.

### `isis_monitor/ipc.py`
Manages local communication between the daemon and clients.
-   **`IPCServer`**: A UNIX domain socket server speaking newline-delimited JSON. Methods: `get_snapshot`, `get_history` (optional `limit` = newest N samples per beam), `get_logs`, `subscribe_updates` and `command`. Malformed or failing requests get an `ok: false` reply without dropping the connection. `stop()` closes attached clients itself, since Python 3.12's `Server.wait_closed()` otherwise waits for them forever.
-   **`IPCClient`**: One background reader task routes replies and pushed events to separate queues, so `request()` and `iter_events()` can run concurrently. Once the connection ends, every later call raises instead of hanging. Reconnection backoff lives in `main.tui_connection_loop`.

### `isis_monitor/protocols.py`
Defines `MonitorSinkProtocol`, the interface monitors use to report into `DaemonState` (or a mock in tests).

---

## Configuration

Configuration is managed via `config.ini` files, loaded through `isis_monitor/config.py`. Key sections include:
-   **`[DATA]`**: WebSocket and HTTP URLs for data sources, plus the optional `mcr_page_url` link button.
-   **`[WEBHOOKS]`**: URLs for Teams integration (should be kept secure). A blank URL disables that channel's Teams notifier.
-   **`[PVS]`**: PV names for a non-PEARL instrument, and `instrument_target` (which beam target's state is reported on run cards).
-   **`[DAEMON]`** / **`[TUI_CLIENT]`**: Paths for UNIX sockets, SQLite database, and retention settings.
-   **`[BEAM_BOUNDARIES]`**: Thresholds for power level classification.
-   **`[TUI]`**: Display settings like history length and refresh rates.
-   **`[NOTIFICATIONS]`**: `fun_mode` (personality lines/milestones), `timezone` (for card timestamps and `summary_time`), `debounce_seconds` (beam-change confirmation window), `stall_minutes` (collection-stall warning threshold), and `summary_time` (HH:MM local time the daily summary is sent).

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

### Technical Debt & Improvements
-   **Error Handling**: Enhance WebSocket reconnection logic with more granular error classification (e.g., distinguishing network errors from authentication issues).
-   **Performance**: If the SQLite persistence overhead grows, consider migrating `storage.py` to use `aiosqlite` for native async database access instead of `asyncio.to_thread`.
-   **Finishing-card ETA**: `beam.py` only builds the "run about to finish" card once counts have *already* crossed `counts_target`, so the ETA fact is always ~0s at that point — accurate, but not predictive. Making it fire in advance (with a real ETA) would mean changing that trigger condition; see `NOTIFICATIONS_PLAN.md` Phase 5 for the full note.
-   **No webhook retry**: `TeamsNotifier.send()` makes a single attempt and only logs failures, so a Teams rate-limit (HTTP 429), a 5xx response or a network blip drops that notification for good. A small retry with backoff for 429/5xx/connection errors (honouring `Retry-After`) would fix this. Keep it bounded so a Teams outage can't pile up sends.
-   **Notifications block beam processing**: `BeamMonitor._handle_update()` awaits `broadcast()` for run-started/finishing/milestone/stall and startup cards inside the WebSocket message loop, so a slow webhook (up to `webhook_timeout`, 10s by default) holds up every PV update behind it. Debounced beam-change cards already send from their own tasks. A fix would move sending off the loop, e.g. one queue plus a worker task per `NotificationChannel`, which also keeps notifications in order.
-   **Stale beam state recorded after a restart**: `run_daemon` restores `beam_states` from the last snapshot, and `state_persistence_loop` samples them every minute. If the WebSocket can't connect at startup (or drops later), the last known values keep being written as fresh samples, which inflates the daily summary's uptime figures. One option is to set beams to `"unknown"` when the beam connection is lost or not yet established (the summary already treats `"unknown"` as off), or to skip sampling while beam health isn't `"connected"`.
-   **24h run count after a restart**: `DaemonState.run_completions` (used for the daily summary's "runs in last 24h" count) is in-memory only, so a daemon restart loses that rolling window until it refills naturally. `total_runs_completed` (used for the 25-run milestone) does persist. See `NOTIFICATIONS_PLAN.md` Phase 6.

### Potential Features
-   **Prometheus Exporter**: Add a lightweight HTTP endpoint to export beam metrics and health status for ingestion by Prometheus/Grafana.
-   **Interactive TUI**: Add keyboard shortcuts to the TUI to toggle specific notification channels or change view modes.
-   **Multiple Notifiers**: Add support for Email, Slack, or SMS notifiers by implementing the `Notifier` interface.

### Best Practices for Extension
1.  **Follow the Protocols**: Always use `isis_monitor.protocols` when adding new sinks to keep monitors decoupled.
2.  **Async/Await**: Ensure all blocking I/O (like networking or DB access) is handled asynchronously (or wrapped in `to_thread`) to prevent freezing the TUI or Daemon.
3.  **State Safety**: `DaemonState` and `RichTUI` are only touched from the event-loop thread. Code running in a worker thread must hand results back to the loop rather than mutating them directly.
4.  **Shutdown**: Monitors run until cancelled (`main.run_until_stopped`), so they need no stop-event plumbing; loops that write to SQLite instead watch `stop_event` so an in-flight write finishes before the store is closed.
