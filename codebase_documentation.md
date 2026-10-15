# ISIS Beam Monitor: codebase map

A map of the code and the rules it relies on. The detail is in the docstrings; `config.ini.example` documents every setting.

```
PVWS (WebSocket) ──┐                    ┌─> Teams webhooks (Power Automate flows)
                   ├─> isis-monitor ────┤
MCR news (HTTP) ───┘     daemon         └─> push site: /ingest (cards), /ingest/status ─> phones
                           ^
                           │ owner-only Unix socket, newline-delimited JSON, one request per connection
                  isis-monitor config | status | reconnect | stop
```

## Modules (`isis_monitor/`)

- `cli.py`: the `isis-monitor` command. Runs the daemon, or sends the daemon one request per command. After a config save it calls `daemon.restart_process()`.
- `daemon.py`: `run_daemon` starts the monitors, the persistence and summary loops, the `IPCServer` and the channels. `build_channels` picks the Teams channel for each card. `SingleInstanceLock` stops a second daemon from starting.
- `ipc.py`: `IPCServer` (`get_status`, `command`, `get_config`, `update_config`) and `ipc_request`.
- `daemon_state.py`: `DaemonState` holds beam states and 24h history, MCR news, health and completed runs. It saves and restores them as a JSON snapshot. Instrument state belongs to each tracker.
- `beam.py`: `BeamMonitor` keeps the one PVWS connection and routes each PV to its handler. `BeamChangeAggregator` debounces beam power changes.
- `instrument.py`: `InstrumentTracker` is `RunTracker`'s listener: it sends one instrument's run, restarted, experiment, finishing and stall cards.
- `runs.py`: `RunTracker` detects runs and experiments from the DAE's PVs and is deterministic, so recorded PV sequences replay exactly. It keeps each run (`RunInfo`), the targets (`auto_target()`) and the saved state.
- `instlist.py`: `parse_instlist()` decodes `CS:INSTLIST`, which gives each instrument's PV prefix and target station.
- `mcr.py`: `MCRNewsMonitor` polls the MCR news page and backs off when fetches fail.
- `messages.py`: `Notification`, one pure builder for each event, the shared formatting helpers and `strip_controls`.
- `notifiers.py`: `TeamsNotifier` sends Adaptive Cards. `WebhookNotifier` sends signed JSON to the push site. `NotificationChannel` passes each notification to its notifiers.
- `status.py`: `build()` makes the status page's payload from `DaemonState`. `StatusPublisher` posts it and `render_text()` formats it for `isis-monitor status`.
- `summary.py`: `compute_summary()` and `daily_summary_loop` send the daily uptime card for each target.
- `storage.py`: `SQLiteStateStore` stores beam samples and snapshots.
- `config.py`: `AppConfig` and `parse_config()`, which checks all settings before the daemon starts. `editable_settings()` and `update_config_file()` let the daemon read and save the config.
- `config_editor.py`: the line-based editor behind `isis-monitor config`. Its terminal I/O is injected so tests can script it.

## Rules the code relies on

- **One thread for state.** `DaemonState` and the trackers are changed only on the event loop. Code in a worker thread passes its results back to the loop.
- **One thread for SQLite.** All database access from async code goes through `store.run()`, which has a single worker thread. Using the connection from several threads corrupts it.
- **Clean remote text.** Text from PVWS, the MCR feed or webhook replies goes through `messages.strip_controls` before it's shown or logged.
- **One notification per beam target.** Readers follow TS1, TS2 and Muons separately, so beam cards and daily summaries are never merged.
- **Startup cards are intentional.** A target's first reading after the daemon starts always sends a card (`startup_status`).
- **Teams channel.** Run and experiment cards go to "Experiment Updates" by default. An instrument with `channel = instrument` uses its own name instead. Beam cards use the target's name and MCR news uses "MCR News".
- **A run is identified by its run number (`DAE:RUNNUMBER`).** `RunTracker` decides when runs start, end and begin again (an abort keeps the number); `InstrumentTracker`, its listener, sends their cards. A run picked up mid-way or only seen ending gets no new-run card but is still recorded.
- **A config save restarts the daemon in place.** After a successful `update_config`, `run_daemon` returns `True` and `restart_process()` re-execs the same command (same PID) with `ISIS_MONITOR_RESTARTED=1`. The config `revision` makes a retried save safe.
- **Failures stay contained.** Each loop catches and logs its own errors and keeps going: reconnects, backoff, retried writes, and the stall check runs separately for each tracker. Each notifier has its own queue and worker, so a slow or failing webhook holds up nothing else. If an exception does escape a loop, the daemon shuts down cleanly rather than continuing half-running.

## Checklists

**A new notification type**
1. Add a builder to `messages.py` that sets `topic` (and `beam` for beam cards). Test it in `test_messages.py`.
2. Call it from the monitor or tracker and `broadcast()` it on the right channel. If the routing changes, update `build_channels`.
3. If it uses a new `topic`, add it to the push site's topics (`BASE_TOPICS` in its `push_site/prefs.py`).

**A new instrument field**
1. If it comes from a PV, add a property on `InstrumentConfig` and a handler in `runs.pv_handlers()`.
2. Keep it in `RunTracker` (or `RunInfo`) with its `to_snapshot()`/`from_snapshot()`, and show it in `InstrumentTracker.view()`. If the saved layout changes, bump `SNAPSHOT_FORMAT`.
3. If the status page shows it, add it to `status._instrument()` and the push site's `push_site/status.py` (which checks the payload) and `static/app.js`.
4. Test it in `test_runs.py`, `test_instrument.py` and `test_status.py`.

**A new config key**
1. Add an `AppConfig` field with `_ini(section, default)`. If it's numeric, add a range to `_BOUNDS`; checks go in `_validate()`.
2. To make a `[NOTIFICATIONS]` key editable, add it to `EDITABLE_NOTIFICATION_KEYS`; the editor's menu then lists it automatically. A per-instrument key goes in `INSTRUMENT_KEYS`, `_read_instruments()` and the prompts in `config_editor._edit_instrument()`. Never make secrets editable.
3. Add it to `config.ini.example` and the README's configuration table. Test it in `test_config.py` (and `test_config_editor.py` if it's editable).

Tests: `pytest` in `isis_monitor/tests` (pytest-asyncio auto mode). Shared fakes such as `FakePVWS` and `wait_until` are in `tests/helpers.py`.
