# Code Review — ISIS Beam Monitor

**Date:** 2026-03-15  
**Scope:** Full codebase review for accuracy and reliability  
**Test Status:** ✅ 72/72 tests passing

---

## Summary

The codebase is well-structured and cleanly written. The modular architecture (`beam.py`, `mcr.py`, `notifiers.py`, `tui.py`, `config.py`, `protocols.py`) provides good separation of concerns. Tests are comprehensive with solid coverage. The issues below range from genuine bugs to defence-in-depth suggestions.

---

## 🔴 Bugs

### 1. `KeyboardInterrupt` does not propagate to running tasks

**File:** [main.py](file:///home/max/ISIS_Beam_Monitor/main.py#L121-L127)

```python
stop_event = asyncio.Event()
try:
    asyncio.run(run_all(config, args, stop_event))
except KeyboardInterrupt:
    stop_event.set()            # ← too late: asyncio.run() has already torn down the loop
    print("\nStopping monitors...")
```

`asyncio.run()` catches `KeyboardInterrupt` internally, cancels all tasks, and then raises it. By the time the `except` block sets `stop_event`, the event loop is already gone and the tasks are already cancelled. The `stop_event.set()` call here has no effect.

This works today only because `beam.py` and `mcr.py` both catch `asyncio.CancelledError` and return. But the graceful shutdown path (e.g. the TUI's `finally: tui.stop()`) relies on `asyncio.gather` completing, which does happen via task cancellation. The `stop_event.set()` is dead code — not harmful, but misleading.

**Recommendation:** Remove the dead `stop_event.set()` or install a signal handler *inside* the async context so the stop event is set before cancellation occurs:

```python
async def run_all(config, args, stop_event):
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
    # ... rest of function
```

---

### 2. `_update_logs_panel` double-acquires `RLock`

**File:** [tui.py](file:///home/max/ISIS_Beam_Monitor/isis_monitor/tui.py#L280-L292)

`_update_logs_panel` acquires `self._lock` internally:

```python
def _update_logs_panel(self):
    with self._lock:              # ← acquires lock
        logs_to_show = list(self._logs)[-7:]
        ...
```

But it is always called from `update_log`, which *already* holds the lock:

```python
def update_log(self, message: str):
    with self._lock:              # ← already holding lock
        self._logs.append(message)
        self._update_logs_panel()  # ← re-enters lock
```

This only works because `RLock` is re-entrant. However, it is the only `_update_*` helper that acquires the lock internally — all the others (`_update_beam_panel`, `_update_mcr_panel`, `_update_beam_graph`) assume the caller holds the lock as the docstring on line 192 states: *"must be called while _lock is held"*.

**Recommendation:** Remove the `with self._lock:` from `_update_logs_panel` to be consistent with the other helpers and the documented contract.

---

### 3. `pyproject.toml` entry point is wrong

**File:** [pyproject.toml](file:///home/max/ISIS_Beam_Monitor/pyproject.toml#L16-L17)

```toml
[project.scripts]
isis-monitor = "main:main"
```

This refers to `main:main`, which would only work if `main.py` were on `sys.path` as a top-level module. Since `main.py` is not inside the `isis_monitor` package, a `pip install` would fail to find the entry point. If the intent is for the script to be pip-installable, this needs to be corrected — or the entry point section should be removed.

**Recommendation:** Either move `main.py` into the package and adjust the path, or update to a relative reference that setuptools can resolve (e.g. wrapping in a package).

---

## 🟡 Reliability Concerns

### 4. `TeamsNotifier.send` creates a new `ClientSession` per call

**File:** [notifiers.py](file:///home/max/ISIS_Beam_Monitor/isis_monitor/notifiers.py#L45-L63)

Each call to `send()` creates a fresh `aiohttp.ClientSession`, establishes a new TCP/TLS connection, sends one request, and tears it all down. Under high-frequency beam state changes, this is wasteful and risks hitting OS file descriptor limits.

By contrast, `MCRNewsMonitor.run` correctly creates a single long-lived session.

**Recommendation:** Accept a shared session or create one in `__init__` with proper lifecycle management.

---

### 5. No validation that boundary tuples have exactly 3 elements

**File:** [config.py](file:///home/max/ISIS_Beam_Monitor/isis_monitor/config.py#L84-L95)

`_parse_tuple` will happily return tuples of any length. The `_get_power_label` method in `beam.py` unconditionally indexes `boundaries[0]`, `[1]`, `[2]` — a tuple with fewer than 3 elements will raise an `IndexError` at runtime.

**Recommendation:** Add a length check in `_parse_tuple`:
```python
if len(result) != 3:
    raise ConfigError(f"{key} must have exactly 3 comma-separated values, got {len(result)}")
```

---

### 6. `isis_websocket_url` is not validated at config load time

**File:** [config.py](file:///home/max/ISIS_Beam_Monitor/isis_monitor/config.py#L69)

`mcr_news_url` is validated as required (raises `ConfigError` if empty), but `isis_websocket_url` is silently allowed to be empty. `BeamMonitor.run` does handle this case with an early return and a warning log, but this means the application will silently start without its primary function if the URL is accidentally omitted.

**Recommendation:** Either validate it as required alongside `mcr_news_url`, or add a louder warning at startup (e.g. log at `WARNING` level from `main.py`).

---

### 7. Naive `datetime.now()` used throughout — no timezone awareness

**Files:** [beam.py](file:///home/max/ISIS_Beam_Monitor/isis_monitor/beam.py#L124), [tui.py](file:///home/max/ISIS_Beam_Monitor/isis_monitor/tui.py#L96)

All calls use `datetime.now()` without a timezone. This is fine for a single-host monitor, but the timestamps embedded in notification messages (e.g. `f"{time_now}: TS1 Beam is now low"`) will not be unambiguous to recipients in other timezones.

**Recommendation:** Use `datetime.now(timezone.utc)` or `datetime.now(tz=ZoneInfo("Europe/London"))` for ISIS-specific context.

---

### 8. `mcr.py` polling loop has no back-off on repeated failures

**File:** [mcr.py](file:///home/max/ISIS_Beam_Monitor/isis_monitor/mcr.py#L78-L96)

If `get_news` returns `None` (HTTP error, timeout, parsing failure), the loop simply waits `mcr_poll_interval` and retries. During a sustained outage, this produces a log warning every 60 seconds forever with no exponential back-off.

**Recommendation:** Add an incrementing delay (capped at a max) on consecutive failures, resetting on success.

---

## 🟢 Minor / Style

### 9. `\n` prefix in some log messages

**File:** [beam.py](file:///home/max/ISIS_Beam_Monitor/isis_monitor/beam.py#L116)

```python
logger.info(f"\nState Change: {msg}")
```

Leading `\n` in log messages is a holdover from pre-TUI console output. With the `RotatingFileHandler` and TUI log panel, this inserts blank lines into the log file and the TUI panel.

**Recommendation:** Remove the `\n` prefixes from log messages.

---

### 10. Graph subtitle hardcodes "1 min/bar"

**File:** [tui.py](file:///home/max/ISIS_Beam_Monitor/isis_monitor/tui.py#L261)

```python
subtitle = f"{n}/{self.history_maxlen} samples · 1 min/bar · {n} min history"
```

The sample interval is configurable (`sample_interval`), but the subtitle always says "1 min/bar". If someone sets `sample_interval = 30`, the label will be wrong.

**Recommendation:** Use `self.sample_interval` to compute the label dynamically.

---

### 11. `TUIProtocol` does not include `run_sampler`

**File:** [protocols.py](file:///home/max/ISIS_Beam_Monitor/isis_monitor/protocols.py)

The protocol defines `update_beam_state`, `update_mcr_news`, and `update_log`, but `main.py` also calls `tui.start()`, `tui.stop()`, and `await tui.run_sampler(stop_event)`. These are not part of the protocol, making `TUIProtocol` an incomplete contract. A mock TUI for testing `main.py` would need to know about these undocumented methods.

**Recommendation:** Either expand `TUIProtocol` to include lifecycle methods, or create a separate `TUILifecycle` protocol.

---

### 12. Test coverage gap — `main.py` has no tests

`main.py` contains the argument parser, logging configuration, signal handling, and the `TUILogHandler`. None of these are tested. The `TUILogHandler` class is a good candidate for a unit test.

---

### 13. Test coverage gap — no test for missing `[DATA]` section entirely

**File:** [test_config.py](file:///home/max/ISIS_Beam_Monitor/isis_monitor/tests/test_config.py)

There is a test for a missing `mcr_news_url` key, but no test for a config file missing the entire `[DATA]` section. `configparser.get` with `fallback=""` would silently return an empty string, which the validation *does* catch — but it's worth an explicit test.

---

## Test Results

```
72 passed in 0.87s
```

All tests pass. Test suite covers `beam.py`, `config.py`, `mcr.py`, `notifiers.py`, and `tui.py` well with 72 test cases including async, thread-safety, and edge-case tests.
