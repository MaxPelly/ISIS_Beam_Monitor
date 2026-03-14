# ISIS Beam Monitor — Code Review

> **Goal:** Assess the codebase against the project's objectives of **robustness**, **modularity**, and **ease of update**. Each finding includes a severity, the affected file, and a concrete solution.

---

## Severity Key

| Label | Meaning |
|-------|---------|
| 🔴 **High** | Could cause crashes, data loss, or silent misbehaviour at runtime |
| 🟡 **Medium** | Hurts maintainability, testability, or resilience |
| 🟢 **Low** | Style / minor improvement |

---

## 1 · Robustness

### 1.1 🔴 `TeamsNotifier` — no response-status checking
[notifiers.py:48](isis_monitor/notifiers.py#L48)

`requests.post()` is fire-and-forget. A 4xx / 5xx response is silently swallowed.

**Solution:** Check `response.status_code` and log failures:
```python
resp = await asyncio.to_thread(requests.post, self.webhook_url, json=payload, timeout=10)
if resp.status_code >= 400:
    logger.error(f"Teams webhook returned {resp.status_code}: {resp.text[:200]}")
```

---

### 1.2 🔴 `BeamMonitor` — no graceful shutdown mechanism
[beam.py:181](isis_monitor/beam.py#L181)

`run()` loops with `while True` and has no cancellation token or event. When `asyncio.gather` is cancelled (e.g. `KeyboardInterrupt`), the `websockets.connect` context manager may not clean up promptly.

**Solution:** Accept an `asyncio.Event` for shutdown:
```python
async def run(self, stop_event: asyncio.Event = None):
    while not (stop_event and stop_event.is_set()):
        ...
```
Signal the event from `main.py`'s interrupt handler.

---

### 1.3 🟡 `MCRNewsMonitor` — single `ClientSession` never refreshed
[mcr.py:40](isis_monitor/mcr.py#L40)

The `aiohttp.ClientSession` is created once and used forever. If DNS changes or the remote server rotates connections, the session may end up with stale connections.

**Solution:** Either recreate the session periodically, or set `aiohttp.TCPConnector(ttl_dns_cache=300)` to auto-refresh DNS.

---

### 1.4 🟡 `MCRNewsMonitor.get_news()` — fragile text parsing
[mcr.py:27](isis_monitor/mcr.py#L27)

```python
re.split(r"\r\n[0-9]{2}", feed)[0]
```

This assumes a very specific format. If the upstream format changes even slightly (e.g. three-digit line numbers), the split silently returns the full raw text instead of clean news.

**Solution:** Add a fallback / validation:
```python
parts = re.split(r"\r\n[0-9]{2,}", feed)
cleaned = re.sub(r"\s+", " ", parts[0].replace("\r\n", "")).strip()
if not cleaned:
    logger.warning("MCR feed parsed to empty string; raw feed may have changed format.")
    return None
```

---

### 1.5 🟡 `BeamMonitor._handle_update()` — counts parsing assumes specific format
[beam.py:149](isis_monitor/beam.py#L149)

```python
counts = float(text_val.split("/")[1])
```

If the text format changes (e.g. no `/`, or more than two parts), this silently drops the update. There's no logging to diagnose why counts stopped working.

**Solution:** Log when parsing fails:
```python
except (IndexError, ValueError) as e:
    logger.warning(f"Failed to parse counts from '{text_val}': {e}")
    return
```

---

### 1.6 🟡 Log file path is hardcoded and relative
[main.py:17](main.py#L17)

```python
handlers=[logging.FileHandler("monitor.log")]
```

`"monitor.log"` is relative to CWD, not to the project directory. If the script is launched from another directory, the log file appears elsewhere. Also, the file will grow unbounded.

**Solution:** Use a `RotatingFileHandler` and anchor to the project root or make the path configurable:
```python
from logging.handlers import RotatingFileHandler
log_path = Path(__file__).parent / "monitor.log"
handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3)
```

---

## 2 · Modularity

### 2.1 🔴 `BeamMonitor._handle_update()` — duplicated per-beam logic
[beam.py:89-125](isis_monitor/beam.py#L89-L125)

The same 7-line block (safe_float → get_power_label → compare state → broadcast → update) is copy-pasted three times for TS1, TS2, and Muon. Adding a fourth beam target means adding another copy.

**Solution:** Introduce a data-driven dispatch:
```python
PV_BEAM_MAP = {
    PV_TS1_BEAM_CURRENT:  ("TS1",  "TS1"),
    PV_TS2_BEAM_CURRENT:  ("TS2",  "TS2"),
    PV_MUON_BEAM_CURRENT: ("Muon", "Muons"),
}
```
Then loop with a single handler that looks up the beam label and broadcast channel from the map.

---

### 2.2 🟡 `MonitorState` — separate fields per beam instead of a data structure
[beam.py:30-44](isis_monitor/beam.py#L30-L44)

Six separate attributes (`TS1_beam_current`, `TS1_beam_power_state`, `TS2_beam_current`, …) make it hard to add new targets.

**Solution:** Use a dict or a per-beam dataclass:
```python
@dataclass
class BeamState:
    current: float = -1.0
    power: str = ""

class MonitorState:
    def __init__(self):
        self.beams = {name: BeamState() for name in BEAM_BOUNDARIES}
        self.run_name: str = ""
        self.current_counts: float = -1.0
        self.end_notified: bool = False
```

---

### 2.3 🟡 No interface / protocol for `tui`
[beam.py:48](isis_monitor/beam.py#L48), [mcr.py:14](isis_monitor/mcr.py#L14)

`tui` is typed as bare `=None` with no protocol. Both `BeamMonitor` and `MCRNewsMonitor` call methods like `tui.update_beam_state(...)` without any contract.

**Solution:** Define a `Protocol`:
```python
class TUIProtocol(Protocol):
    def update_beam_state(self, beam: str, current: float, power: str) -> None: ...
    def update_mcr_news(self, news: str) -> None: ...
```
Then annotate: `tui: Optional[TUIProtocol] = None`. This makes the contract explicit and enables alternative TUI implementations.

---

### 2.4 🟡 Hardcoded PV names make the monitor PEARL-specific
[beam.py:20-21](isis_monitor/beam.py#L20-L21)

```python
PV_COUNTS = "IN:PEARL:CS:DASHBOARD:TAB:2:1:VALUE"
PV_RUN_NAME = "IN:PEARL:DAE:WDTITLE"
```

These are instrument-specific. Reusing the monitor for another instrument requires editing source code.

**Solution:** Move PV names into `config.ini`:
```ini
[PVS]
counts_pv = IN:PEARL:CS:DASHBOARD:TAB:2:1:VALUE
run_name_pv = IN:PEARL:DAE:WDTITLE
```

---

### 2.5 🟡 `NotificationChannel.name` is never used
[notifiers.py:62](isis_monitor/notifiers.py#L62)

The `name` field is stored but never read or logged.

**Solution:** Either use it in log messages / error output, or remove it to reduce confusion.

---

## 3 · Ease of Update / Maintainability

### 3.1 🟡 Mixed sync/async HTTP libraries
[notifiers.py:2](isis_monitor/notifiers.py#L2), [mcr.py:5](isis_monitor/mcr.py#L5)

`notifiers.py` uses `requests` (sync, wrapped in `asyncio.to_thread`), while `mcr.py` uses `aiohttp` (native async). Two HTTP stacks means two sets of timeouts, retry semantics, and dependencies.

**Solution:** Migrate `TeamsNotifier` to use `aiohttp` as well, then drop `requests` from `requirements.txt`.

---

### 3.2 🟡 `requirements.txt` has no version pinning
[requirements.txt](requirements.txt)

```
requests
websockets
aiohttp
rich
```

Any `pip install` could pull breaking major versions (e.g. `websockets` 14 changed its API).

**Solution:** Pin at least major+minor versions:
```
requests>=2.31,<3
websockets>=12,<14
aiohttp>=3.9,<4
rich>=13,<14
```

---

### 3.3 🟡 `requirements-dev.txt` includes `rich` redundantly
[requirements-dev.txt](requirements-dev.txt)

`rich` is already in `requirements.txt`. A `requirements-dev.txt` typically only adds test/dev tools on top of the main requirements.

**Solution:** Change `requirements-dev.txt` to:
```
-r requirements.txt
pytest
pytest-asyncio
pytest-mock
```

---

### 3.4 🟢 Missing `__all__` exports
[isis_monitor/\_\_init\_\_.py](isis_monitor/__init__.py)

The package `__init__.py` is essentially empty. This makes it unclear what the public API is.

**Solution:** Re-export key classes:
```python
from isis_monitor.beam import BeamMonitor
from isis_monitor.mcr import MCRNewsMonitor
from isis_monitor.config import AppConfig, load_config

__all__ = ["BeamMonitor", "MCRNewsMonitor", "AppConfig", "load_config"]
```

---

### 3.5 🟢 `config.py` uses `sys.exit()` on error
[config.py:24,33](isis_monitor/config.py#L24)

Calling `sys.exit()` inside a library module makes it untestable (tests must catch `SystemExit`) and prevents callers from handling the error differently.

**Solution:** Raise a custom exception:
```python
class ConfigError(Exception):
    pass

def load_config(config_path: Path) -> AppConfig:
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")
    ...
```
Catch it in `main.py` and call `sys.exit()` there.

---

### 3.6 🟢 No `pyproject.toml` or `setup.py`
The project lacks a packaging manifest. This means it can't be installed with `pip install -e .`, which complicates both development and deployment.

**Solution:** Add a minimal `pyproject.toml`:
```toml
[project]
name = "isis-monitor"
version = "0.1.0"
dependencies = ["requests", "websockets", "aiohttp", "rich"]

[project.scripts]
isis-monitor = "main:main"
```

---

## 4 · Test Coverage Gaps

| Area | Status | Notes |
|------|--------|-------|
| `config.py` | ✅ Covered | Happy path, missing file, missing MCR URL |
| `beam.py` — `_safe_float`, `_get_power_label` | ✅ Covered | |
| `beam.py` — `_handle_update` | ⚠️ Partial | Only tests beam-current updates; **no tests for run-name (`b64byt`) or counts (`text`) arms** |
| `beam.py` — `run()` | ❌ Not tested | WebSocket reconnection logic untested |
| `mcr.py` | ✅ Covered | Success, failure, timeout |
| `mcr.py` — `run()` | ❌ Not tested | Polling loop, initial fetch, notify_current flag |
| `notifiers.py` | ✅ Covered | DummyNotifier, TeamsNotifier, NotificationChannel |
| `tui.py` | ✅ Covered | Init, start/stop, updates, thread safety |
| `main.py` | ❌ Not tested | Arg parsing, wiring, shutdown |

**Recommendation:** Prioritise tests for the `b64byt`/`text` arms of `_handle_update` and for the `MCRNewsMonitor.run()` polling loop. These are the most complex untested paths.

---

## Summary — Prioritised Action Items

| Priority | Item | Effort |
|----------|------|--------|
| 1 | Deduplicate per-beam logic with a data-driven dispatch (2.1) | Medium |
| 2 | Refactor `MonitorState` to a dict of beam states (2.2) | Small |
| 3 | Check webhook response status (1.1) | Small |
| 4 | Add graceful shutdown via `asyncio.Event` (1.2) | Medium |
| 5 | Move instrument PVs to config (2.4) | Small |
| 6 | Unify on one HTTP library (3.1) | Medium |
| 7 | Pin dependency versions (3.2) | Small |
| 8 | Define a `TUIProtocol` (2.3) | Small |
| 9 | Replace `sys.exit` with exceptions in `config.py` (3.5) | Small |
| 10 | Add missing test coverage (§4) | Medium |
