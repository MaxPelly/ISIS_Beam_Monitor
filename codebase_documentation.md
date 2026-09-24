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
-   **`BeamMonitor`**: Manages the WebSocket connection and state. It dispatches updates based on PV names, and runs a second periodic loop (`_collection_check_loop`, gathered alongside the websocket loop in `run()`) that checks for stalled data collection roughly every 60s.
-   **`BeamTarget`**: Configuration for specific beam targets (TS1, TS2, Muons).
-   **State Management**: Tracks current beam currents and power levels (off, low, medium, high) to detect transitions, plus per-target `since` timestamps and a 15-minute deque of `(time, counts_collected)` samples used for the run-finishing card's rate/ETA. `counts_pv`'s text is `live_current/total_collected` — only the total is tracked; live current is discarded since beam current is already tracked directly via the TS1/TS2/Muon PVs.
-   **`BeamChangeAggregator`**: Debounces raw beam-state transitions for `debounce_seconds` before turning them into notifications, dropping ones that flap back to their original state, and noting when other targets went off in the same window (the three targets share one accelerator, so correlated trips are the common case, not an edge case).
-   **Run-count milestones**: `DaemonState.record_run_completed()` is called whenever a run completes; `beam.py` checks the returned all-time total against `RUN_MILESTONE_INTERVAL` (25) to fire a milestone card when `fun_mode` is on.

### `isis_monitor/mcr.py`
Handles MCR news polling.
-   **`MCRNewsMonitor`**: Polls the news feed at a configurable interval. It uses regex to parse the feed and detect changes in the latest news entry.
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
-   **`daily_summary_loop`**: runs inside `run_daemon`, checks the configured local time (`[NOTIFICATIONS] summary_time`) once a minute, and sends one card per target when it's reached (deduped to once per day). When `fun_mode` is on, it also tracks each target's longest-ever on-streak in the SQLite snapshot under the key `"records"` and flags a new record.

### `isis_monitor/notifiers.py`
A decoupled notification system.
-   **`Notifier` (Abstract)**: Base class for notification implementations; `send()` takes a `Notification`.
-   **`TeamsNotifier`**: Renders a `Notification` as a Microsoft Teams Adaptive Card (severity-coloured header, body text, italic flavour line, `FactSet`, optional `Action.OpenUrl` button) and posts it via webhook.
-   **`NotificationChannel`**: Groups multiple notifiers for a specific category of updates (e.g., "Beam Updates").

### `isis_monitor/tui.py`
The live terminal interface.
-   **`RichTUI`**: Coordinates the layout and rendering. It uses a `threading.RLock` to safely handle updates from multiple async tasks.
-   **`sparkline_chars(values, width)`**: a pure, uncoloured sparkline renderer shared with `summary.py`; `_render_sparkline()` wraps it to add per-block colour for the TUI.
-   **Sampler**: An independent coroutine that snapshots state at fixed intervals to ensure consistent graph pacing.

### `isis_monitor/daemon_state.py` & `storage.py`
The core state management and persistence layer.
-   **`DaemonState`**: A thread-safe, lock-protected singleton holding current beam statuses, historical data buffers, MCR news, health checks, and run-completion tracking (`record_run_completed()`, `count_runs_completed_since()`; `total_runs_completed` persists across restarts via the snapshot, the rolling 24h `run_completions` deque does not). It manages a pub/sub queue system for IPC clients.
-   **`SQLiteStateStore`**: Handles synchronizing the daemon's state to disk (including the `"records"` snapshot key used for uptime milestones), enabling crash recovery and historical lookups.

### `isis_monitor/ipc.py`
Manages local communication between the daemon and clients.
-   **`IPCServer`**: A UNIX domain socket server that handles requests (like fetching a state snapshot or history) and multiplexes event streams to subscribed clients using a newline-delimited JSON protocol.
-   **`IPCClient`**: A resilient async client that manages connection state and reconnection backoff.

### `isis_monitor/protocols.py`
Defines runtime-checkable protocols (e.g., `MonitorSinkProtocol`, `TUIProtocol`) allowing monitors to interact with the daemon or the TUI interchangeably during testing.

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
-   **Testing**: Expand unit tests for `tui.py` and `main.py`. Currently, core logic is well-tested, but UI rendering and orchestration could benefit from more coverage.
-   **Performance**: If the SQLite persistence overhead grows, consider migrating `storage.py` to use `aiosqlite` for native async database access instead of `asyncio.to_thread`.
-   **Finishing-card ETA**: `beam.py` only builds the "run about to finish" card once counts have *already* crossed `counts_target`, so the ETA fact is always ~0s at that point — accurate, but not predictive. Making it fire in advance (with a real ETA) would mean changing that trigger condition; see `NOTIFICATIONS_PLAN.md` Phase 5 for the full note.
-   **24h run count after a restart**: `DaemonState.run_completions` (used for the daily summary's "runs in last 24h" count) is in-memory only, so a daemon restart loses that rolling window until it refills naturally. `total_runs_completed` (used for the 25-run milestone) does persist. See `NOTIFICATIONS_PLAN.md` Phase 6.

### Potential Features
-   **Prometheus Exporter**: Add a lightweight HTTP endpoint to export beam metrics and health status for ingestion by Prometheus/Grafana.
-   **Interactive TUI**: Add keyboard shortcuts to the TUI to toggle specific notification channels or change view modes.
-   **Multiple Notifiers**: Add support for Email, Slack, or SMS notifiers by implementing the `Notifier` interface.

### Best Practices for Extension
1.  **Follow the Protocols**: Always use `isis_monitor.protocols` when adding new sinks to keep monitors decoupled.
2.  **Async/Await**: Ensure all blocking I/O (like networking or DB access) is handled asynchronously (or wrapped in `to_thread`) to prevent freezing the TUI or Daemon.
3.  **State Safety**: Always use `self._lock` when modifying `DaemonState` or `RichTUI` state to prevent race conditions.
