import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from isis_monitor.daemon_state import SUBSCRIBER_QUEUE_SIZE, DaemonState
from main import StateLogHandler

def test_daemon_state_snapshot():
    state = DaemonState()
    state.update_beam_state("TS1", 45.0, "medium")
    state.update_mcr_news("Breaking News")
    state.update_health("daemon", "running")

    snap = state.snapshot()
    assert snap["mcr_news"] == "Breaking News"
    assert snap["beam_states"]["TS1"]["current"] == 45.0
    assert snap["health"]["daemon"] == "running"
    
    # Check that history and logs are NOT in snapshot
    assert "history" not in snap
    assert "logs" not in snap

def test_daemon_state_get_history_and_logs():
    state = DaemonState()
    ts = datetime.now(timezone.utc)
    state.append_beam_sample("TS1", 10.0, "low", ts=ts)
    state.update_log("Log entry 1")

    history = state.get_history_snapshot()
    assert len(history["TS1"]) == 1
    assert history["TS1"][0]["current"] == 10.0

    logs = state.get_logs_snapshot()
    assert len(logs) == 1
    assert logs[0] == "Log entry 1"

def test_daemon_state_pubsub():
    state = DaemonState()
    queue = state.subscribe()

    state.update_beam_state("TS2", 100.0, "high")
    
    event = queue.get_nowait()
    assert event.event == "beam"
    assert event.payload["beam"] == "TS2"

    state.unsubscribe(queue)
    state.update_log("Log entry")
    
    assert queue.empty()

def _drain(q):
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    return events


def test_full_subscriber_is_dropped_with_sentinel(caplog):
    state = DaemonState()
    slow = state.subscribe()
    for i in range(SUBSCRIBER_QUEUE_SIZE + 1):
        state.update_log(f"Spam {i}")

    assert "Dropped 1 IPC subscriber(s)" in caplog.text
    assert slow not in state._subscribers
    # The backlog is discarded and replaced by a single "resync" sentinel.
    assert _drain(slow) == [None]


def test_dropping_subscriber_does_not_recurse_through_state_log_handler():
    """The drop warning is itself published via update_log(); it must not
    re-enter the still-full queue (previously: RecursionError)."""
    state = DaemonState()
    handler = StateLogHandler(state)
    handler.handleError = MagicMock()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        state.subscribe()
        healthy = state.subscribe()
        for i in range(SUBSCRIBER_QUEUE_SIZE + 1):
            state.update_log(f"Spam {i}")
            _drain(healthy)  # keep this one keeping up
    finally:
        root.removeHandler(handler)

    handler.handleError.assert_not_called()
    assert sum("Dropped" in line for line in state.logs) == 1
    assert healthy in state._subscribers


def test_update_health_publishes_only_on_change():
    state = DaemonState()
    q = state.subscribe()
    state.update_health("mcr", "connected")
    state.update_health("mcr", "connected")
    state.update_health("mcr", "error")
    events = _drain(q)
    assert [e.payload["status"] for e in events] == ["connected", "error"]
    assert state.health["mcr"] == "error"


def test_sample_all_currents_appends_publishes_and_returns_rows():
    state = DaemonState()
    state.update_beam_state("TS1", 150.0, "high")
    q = state.subscribe()
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)

    rows = state.sample_all_currents(ts)

    assert (ts, "TS1", 150.0, "high") in rows
    assert len(rows) == len(state.beam_states)
    assert state.history["TS1"][-1] == (ts, 150.0, "high")
    events = _drain(q)
    assert {e.event for e in events} == {"sample"}
    assert len(events) == len(rows)


def test_append_beam_sample_without_publish_and_unknown_beam():
    state = DaemonState()
    q = state.subscribe()
    state.append_beam_sample("TS1", 1.0, "low", publish=False)
    state.append_beam_sample("Nope", 1.0, "low")
    assert len(state.history["TS1"]) == 1
    assert "Nope" not in state.history
    assert q.empty()


def test_update_beam_state_ignores_unknown_beam():
    state = DaemonState()
    q = state.subscribe()
    state.update_beam_state("Nope", 1.0, "low")
    assert "Nope" not in state.beam_states
    assert q.empty()


def test_trim_history_before_drops_only_old_samples():
    state = DaemonState()
    now = datetime.now(timezone.utc)
    for minutes in (30, 20, 10, 0):
        state.append_beam_sample("TS2", float(minutes), "low", ts=now - timedelta(minutes=minutes))
    state.trim_history_before(now - timedelta(minutes=15))
    assert [v for _, v, _ in state.history["TS2"]] == [10.0, 0.0]


def test_get_history_snapshot_limit_returns_newest():
    state = DaemonState()
    now = datetime.now(timezone.utc)
    for i in range(5):
        state.append_beam_sample("TS1", float(i), "low", ts=now + timedelta(minutes=i))
    limited = state.get_history_snapshot(limit=2)
    assert [row["current"] for row in limited["TS1"]] == [3.0, 4.0]
    assert limited["TS2"] == []
    assert len(state.get_history_snapshot()["TS1"]) == 5


def test_run_name_and_counts_events():
    state = DaemonState()
    q = state.subscribe()
    state.update_run_name("Run 42")
    state.update_counts(12.5)
    assert [(e.event, e.payload) for e in _drain(q)] == [
        ("run", {"run_name": "Run 42"}),
        ("counts", {"counts": 12.5}),
    ]
    snap = state.snapshot()
    assert snap["run_name"] == "Run 42"
    assert snap["current_counts"] == 12.5


def test_snapshot_is_a_copy():
    state = DaemonState()
    snap = state.snapshot()
    snap["beam_states"]["TS1"]["current"] = 999.0
    snap["health"]["daemon"] = "hacked"
    assert state.beam_states["TS1"]["current"] == 0.0
    assert state.health["daemon"] == "starting"


def test_restore_from_snapshot_restores_health_and_ignores_empty():
    state = DaemonState()
    state.restore_from_snapshot_json(None)
    state.restore_from_snapshot_json("")
    assert state.health["beam"] == "unknown"
    state.restore_from_snapshot_json(json.dumps({"health": {"beam": "connected"}, "run_name": "R1"}))
    assert state.health["beam"] == "connected"
    assert state.run_name == "R1"


def test_restore_from_snapshot():
    state = DaemonState()
    
    valid_json = json.dumps({
        "mcr_news": "Restored News",
        "beam_states": {"Muons": {"current": 2.0, "power": "low"}}
    })
    state.restore_from_snapshot_json(valid_json)
    assert state.mcr_news == "Restored News"
    assert state.beam_states["Muons"]["current"] == 2.0
    
    # Corrupt JSON shouldn't crash
    state.restore_from_snapshot_json("{bad_json: True")
    # State should remain intact
    assert state.mcr_news == "Restored News"


def test_record_run_completed_returns_running_total():
    state = DaemonState()
    now = datetime.now(timezone.utc)
    assert state.record_run_completed(now) == 1
    assert state.record_run_completed(now) == 2
    assert state.total_runs_completed == 2


def test_count_runs_completed_since_filters_by_window():
    state = DaemonState()
    now = datetime.now(timezone.utc)
    state.record_run_completed(now - timedelta(hours=30))  # outside 24h window
    state.record_run_completed(now - timedelta(hours=1))
    state.record_run_completed(now)

    assert state.count_runs_completed_since(now - timedelta(hours=24)) == 2


def test_total_runs_completed_persists_through_snapshot():
    state = DaemonState()
    state.record_run_completed(datetime.now(timezone.utc))
    state.record_run_completed(datetime.now(timezone.utc))

    snap_json = json.dumps(state.snapshot())

    restored = DaemonState()
    restored.restore_from_snapshot_json(snap_json)
    assert restored.total_runs_completed == 2
