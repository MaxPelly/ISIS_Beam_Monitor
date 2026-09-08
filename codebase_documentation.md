# ISIS Beam Monitor Codebase Documentation

This document provides a technical overview of the ISIS Beam Monitor codebase, its architecture, components, and guidance for future development.

## Architecture Overview

The ISIS Beam Monitor is a real-time monitoring system designed to track accelerator beam status and MCR (Main Control Room) news updates at the ISIS Neutron and Muon Source. It follows a decoupled, asynchronous architecture using Python's `asyncio` for concurrent operations.

### High-Level Design
The system uses a two-tier architecture (daemon and client) communicating via local UNIX domain sockets:
1.  **Daemon**: A long-lived background process holding the master `DaemonState` (in `daemon_state.py`). It orchestrates monitors, persists state to a local SQLite database (`storage.py`), and serves multiple clients via JSON over IPC (`ipc.py`).
2.  **Monitors**: Asynchronous tasks that fetch and process data from external sources (WebSockets for beam data, HTTP polling for MCR news). They feed data into the `DaemonState` via `MonitorSinkProtocol`.
3.  **Notifiers**: Flexible channels for broadcasting alerts to external services like Microsoft Teams or local logs.
4.  **TUI Client**: A terminal UI built with the `rich` library. It acts as an IPC client, fetching the initial state snapshot from the daemon and then subscribing to a real-time event stream to update its display.

---

## Component Deep Dives

### `isis_monitor/beam.py`
The core logic for accelerator beam monitoring.
-   **`BeamMonitor`**: Manages the WebSocket connection and state. It dispatches updates based on PV names.
-   **`BeamTarget`**: Configuration for specific beam targets (TS1, TS2, Muons).
-   **State Management**: Tracks current beam currents and power levels (off, low, medium, high) to detect transitions.

### `isis_monitor/mcr.py`
Handles MCR news polling.
-   **`MCRNewsMonitor`**: Polls the news feed at a configurable interval. It uses regex to parse the feed and detect changes in the latest news entry.
-   **Adaptive Polling**: Implements exponential backoff on fetch failures to reduce load on the source during outages.

### `isis_monitor/notifiers.py`
A decoupled notification system.
-   **`Notifier` (Abstract)**: Base class for notification implementations.
-   **`TeamsNotifier`**: Sends Adaptive Cards to Microsoft Teams via webhooks.
-   **`NotificationChannel`**: Groups multiple notifiers for a specific category of updates (e.g., "Beam Updates").

### `isis_monitor/tui.py`
The live terminal interface.
-   **`RichTUI`**: Coordinates the layout and rendering. It uses a `threading.RLock` to safely handle updates from multiple async tasks.
-   **Sparklines**: Visualizes historical beam current data using Unicode block characters, normalized against the rolling buffer's range.
-   **Sampler**: An independent coroutine that snapshots state at fixed intervals to ensure consistent graph pacing.

### `isis_monitor/daemon_state.py` & `storage.py`
The core state management and persistence layer.
-   **`DaemonState`**: A thread-safe, lock-protected singleton holding current beam statuses, historical data buffers, MCR news, and health checks. It manages a pub/sub queue system for IPC clients.
-   **`SQLiteStateStore`**: Handles synchronizing the daemon's state to disk, enabling crash recovery and historical lookups.

### `isis_monitor/ipc.py`
Manages local communication between the daemon and clients.
-   **`IPCServer`**: A UNIX domain socket server that handles requests (like fetching a state snapshot or history) and multiplexes event streams to subscribed clients using a newline-delimited JSON protocol.
-   **`IPCClient`**: A resilient async client that manages connection state and reconnection backoff.

### `isis_monitor/protocols.py`
Defines runtime-checkable protocols (e.g., `MonitorSinkProtocol`, `TUIProtocol`) allowing monitors to interact with the daemon or the TUI interchangeably during testing.

---

## Configuration

Configuration is managed via `config.ini` files, loaded through `isis_monitor/config.py`. Key sections include:
-   **`[DATA]`**: WebSocket and HTTP URLs for data sources.
-   **`[WEBHOOKS]`**: URLs for Teams integration (should be kept secure).
-   **`[DAEMON]`** / **`[TUI_CLIENT]`**: Paths for UNIX sockets, SQLite database, and retention settings.
-   **`[BEAM_BOUNDARIES]`**: Thresholds for power level classification.
-   **`[TUI]`**: Display settings like history length and refresh rates.

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

### Potential Features
-   **Prometheus Exporter**: Add a lightweight HTTP endpoint to export beam metrics and health status for ingestion by Prometheus/Grafana.
-   **Interactive TUI**: Add keyboard shortcuts to the TUI to toggle specific notification channels or change view modes.
-   **Multiple Notifiers**: Add support for Email, Slack, or SMS notifiers by implementing the `Notifier` interface.

### Best Practices for Extension
1.  **Follow the Protocols**: Always use `isis_monitor.protocols` when adding new sinks to keep monitors decoupled.
2.  **Async/Await**: Ensure all blocking I/O (like networking or DB access) is handled asynchronously (or wrapped in `to_thread`) to prevent freezing the TUI or Daemon.
3.  **State Safety**: Always use `self._lock` when modifying `DaemonState` or `RichTUI` state to prevent race conditions.
