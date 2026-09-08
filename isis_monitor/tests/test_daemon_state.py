import asyncio
import json
from datetime import datetime, timezone
import pytest

from isis_monitor.daemon_state import DaemonState, DaemonEvent

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

def test_daemon_state_subscriber_drop(caplog):
    state = DaemonState()
    queue = state.subscribe()
    
    # Fill queue past its maxsize (usually 500)
    for i in range(501):
        state.update_log(f"Spam {i}")
        
    assert "Subscriber queue full; dropping subscriber" in caplog.text
    # Should be unsubscribed automatically
    assert queue not in state._subscribers

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
