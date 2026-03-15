# ISIS Beam Monitor Codebase Documentation

This document provides a technical overview of the ISIS Beam Monitor codebase, its architecture, components, and guidance for future development.

## Architecture Overview

The ISIS Beam Monitor is a real-time monitoring system designed to track accelerator beam status and MCR (Main Control Room) news updates at the ISIS Neutron and Muon Source. It follows a decoupled, asynchronous architecture using Python's `asyncio` for concurrent operations.

### High-Level Design
The system consists of three main parts:
1.  **Monitors**: Asynchronous tasks that fetch and process data from external sources (WebSockets for beam data, HTTP polling for MCR news).
2.  **Notifiers**: Flexible channels for broadcasting alerts to external services like Microsoft Teams or local logs.
3.  **TUI (Terminal User Interface)**: A rich, real-time display built with the `rich` library, providing visual feedback and status summaries.

### Data Flow
-   **Beam Data**: Subscribes to PV (Process Variable) updates via a WebSocket. Updates are dispatched to the TUI and broadcast to notification channels if threshold boundaries are crossed or run states change.
-   **MCR News**: Periodically polls an external URL. If new text is detected, it updates the TUI and broadcasts the news to the MCR notification channel.

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

### `isis_monitor/protocols.py`
Defines the `TUIProtocol`, allowing the monitors to interact with any TUI implementation (or a mock during testing) without being coupled to the `rich` implementation.

---

## Configuration

Configuration is managed via `config.ini` files, loaded through `isis_monitor/config.py`. Key sections include:
-   **`[DATA]`**: WebSocket and HTTP URLs for data sources.
-   **`[WEBHOOKS]`**: URLs for Teams integration.
-   **`[BEAM_BOUNDARIES]`**: Thresholds for power level classification (Off/Low/Medium/High).
-   **`[TUI]`**: Display settings like history length and refresh rates.

---

## Customizing the TUI Layout

The TUI is built using `rich.layout.Layout`. You can adjust the proportions and sizes of the interface by modifying `isis_monitor/tui.py`.

### Adjusting Section Sizes
In `RichTUI._make_layout()`, sections are defined using `split_column` and `split_row`.
- **Fixed Height**: Use the `size` argument (e.g., `Layout(name="header", size=3)`) to set a fixed number of rows.
- **Proportional Width/Height**: Use the `ratio` argument (e.g., `Layout(name="left", ratio=1)`) to make a section take up a proportion of the available space relative to its siblings.

### Column Widths & Internal Padding
- **Table Columns**: The beam status table in `_update_beam_panel()` uses `expand=True`. To adjust individual column behaviors, modify the `table.add_column()` calls.
- **Graph Width**: If you significantly change the width of the "left" column, you may need to update `SPARK_WIDTH` in `_update_beam_graph()` to ensure the sparklines fit correctly or fill the space.

---

## Advice for Future Changes

### Technical Debt & Improvements
-   **Error Handling**: Enhance WebSocket reconnection logic with more granular error classification (e.g., distinguishing network errors from authentication issues).
-   **Testing**: Expand unit tests for `tui.py` and `main.py`. Currently, core logic is well-tested, but UI rendering and orchestration could benefit from more coverage.
-   **Performance**: For very large numbers of beam targets, consider moving TUI rendering to a separate thread to avoid blocking the `asyncio` event loop, though current loads are well within limits.

### Potential Features
-   **Historical Logging**: Persist beam data to a local database (e.g., SQLite) for post-run analysis.
-   **Interactive TUI**: Add keyboard shortcuts to the TUI to toggle specific notification channels or change view modes.
-   **Multiple Notifiers**: Add support for Email, Slack, or SMS notifiers by implementing the `Notifier` interface.

### Best Practices for Extension
1.  **Follow the Protocols**: Always use `isis_monitor.protocols` when adding new UI elements to keep monitors decoupled.
2.  **Async/Await**: Ensure all blocking I/O (like networking) is handled asynchronously to prevent freezing the TUI.
3.  **State Safety**: Always use the `self._lock` when modifying `RichTUI` state to prevent race conditions during rendering.
