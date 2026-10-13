import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from isis_monitor.daemon_state import SUBSCRIBER_QUEUE_SIZE, DaemonState
from main import StateLogHandler

INSTRUMENTS = ["PEARL", "WISH"]

def test_daemon_state_snapshot():
    state = DaemonState()
    state.update_beam_state("TS1", 45.0, "medium")
    state.update_mcr_news("Breaking News")
    state.update_health("daemon", "running")

    snap = state.snapshot()
    assert snap["mcr_news"] == "Breaking News"
    assert snap["beam_states"]["TS1"]["current"] == 45.0
    assert "health" not in snap  # live state, not saved: it describes this run's connections
    
    # History and logs are fetched separately, not in the snapshot
    assert "history" not in snap
    assert "logs" not in snap
    state.update_log("Log entry 1")
    assert state.get_logs_snapshot() == ["Log entry 1"]

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

    assert rows == [(ts, "TS1", 150.0, "high")]  # the others have no reading yet
    assert state.history["TS1"][-1] == (ts, 150.0, "high")
    events = _drain(q)
    assert {e.event for e in events} == {"sample"}
    assert len(events) == len(rows)


def test_append_beam_sample_without_publish_and_unknown_beams():
    state = DaemonState()
    q = state.subscribe()
    state.append_beam_sample("TS1", 1.0, "low", publish=False)
    state.append_beam_sample("Nope", 1.0, "low")
    state.update_beam_state("Nope", 1.0, "low")
    assert len(state.history["TS1"]) == 1
    assert "Nope" not in state.history
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
    assert [row["current"] for row in state.get_history_snapshot()["TS1"]] == [0.0, 1.0, 2.0, 3.0, 4.0]


def test_snapshot_is_a_copy():
    state = DaemonState()
    snap = state.snapshot()
    snap["beam_states"]["TS1"]["current"] = 999.0
    assert state.beam_states["TS1"]["current"] == 0.0


def test_restore_from_snapshot_ignores_empty():
    state = DaemonState()
    state.restore_from_snapshot_json(None)
    state.restore_from_snapshot_json("")
    assert state.snapshot() == DaemonState().snapshot()


def test_restore_from_snapshot():
    state = DaemonState()
    
    valid_json = json.dumps({
        "mcr_news": "Restored News",
        "beam_states": {"Muons": {"current": 2.0, "power": "low"}}
    })
    state.restore_from_snapshot_json(valid_json)
    assert state.mcr_news == "Restored News"
    assert state.beam_states["Muons"] == {"current": 2.0, "power": "unknown"}  # not yet live
    
    # Corrupt JSON shouldn't crash
    state.restore_from_snapshot_json("{bad_json: True")
    # State should remain intact
    assert state.mcr_news == "Restored News"


def test_record_run_completed_ignores_unknown_instruments():
    state = DaemonState(instrument_names=INSTRUMENTS)
    now = datetime.now(timezone.utc)
    state.record_run_completed("PEARL", now, "127920", 10.0)
    state.record_run_completed("MERLIN", now, "1", 10.0)
    assert list(state.run_completions) == [(now, "PEARL", "127920", 10.0)]


def test_runs_completed_since_filters_by_window_and_instrument():
    state = DaemonState(instrument_names=INSTRUMENTS)
    now = datetime.now(timezone.utc)
    state.record_run_completed("PEARL", now - timedelta(hours=30), "1", 10.0)  # outside 24h window
    state.record_run_completed("PEARL", now - timedelta(hours=1), "2", 20.0)
    state.record_run_completed("PEARL", now, "", 5.5)
    state.record_run_completed("WISH", now, "3", 10.0)

    since = now - timedelta(hours=24)
    assert state.runs_completed_since(since, ["PEARL", "MERLIN"]) == {"PEARL": (2, 25.5), "MERLIN": (0, 0.0)}
    assert state.runs_completed_since(since, []) == {}


def test_instrument_state_is_the_trackers_and_restores_only_configured_instruments():
    state = DaemonState(instrument_names=INSTRUMENTS)
    views = {"WISH": {"format": 2, "run_name": "Run 7"}, "PEARL": {"format": 2, "run_name": ""}}
    state.instruments = lambda: views
    snap_json = json.dumps(state.snapshot())
    assert json.loads(snap_json)["instruments"] == views

    # MERLIN is new and PEARL was removed from the config since the snapshot;
    # the entries are the trackers' to check (see InstrumentTracker.from_snapshot).
    restored = DaemonState(instrument_names=["WISH", "MERLIN"])
    restored.restore_from_snapshot_json(snap_json)
    assert restored.saved_instruments == {"WISH": views["WISH"]}
    restored.restore_from_snapshot_json(json.dumps({"instruments": ["junk"]}))
    assert restored.saved_instruments == {}


def test_recent_run_completions_survive_a_snapshot():
    now = datetime.now(timezone.utc)
    state = DaemonState(instrument_names=INSTRUMENTS)
    state.record_run_completed("PEARL", now - timedelta(hours=25), "1", 10.0)  # too old to keep
    state.record_run_completed("PEARL", now - timedelta(hours=2), "2", 10.0)
    state.record_run_completed("WISH", now - timedelta(hours=1), "3", 10.0)

    snap = state.snapshot()
    assert len(snap["run_completions"]) == 2
    # WISH has since been removed from the config.
    restored = DaemonState(instrument_names=INSTRUMENTS[:1])
    restored.restore_from_snapshot_json(json.dumps(snap))
    assert list(restored.run_completions) == [(now - timedelta(hours=2), "PEARL", "2", 10.0)]


def test_old_and_malformed_run_completions_are_not_restored(caplog):
    now = datetime.now(timezone.utc)
    state = DaemonState(instrument_names=INSTRUMENTS)
    state.restore_from_snapshot_json(json.dumps({"mcr_news": "x"}))  # snapshot from before this was saved
    assert not state.run_completions

    with caplog.at_level(logging.WARNING):
        state.restore_from_snapshot_json(json.dumps({"run_completions": [
            [(now - timedelta(hours=30)).isoformat(), "PEARL", "1", 1.0],
            ["not a time", "PEARL", "1", 1.0],
            [now.isoformat(), ["unhashable"], "1", 1.0],
            [now.isoformat(), "PEARL", "1", "NaN"],
            [now.isoformat(), "PEARL", "1"],  # no µA·h
            [now.isoformat(), "PEARL", 1.0],  # an older entry, without a run number
            "junk",
            [(now - timedelta(minutes=5)).replace(tzinfo=None).isoformat(), "WISH", "2", 1.0],  # naive means UTC
        ]}))
    assert [(name, number) for _, name, number, _ in state.run_completions] == [("WISH", "2")]
    assert "Skipped 6 malformed run completion(s)" in caplog.text
    state.restore_from_snapshot_json(json.dumps({"run_completions": {"not": "a list"}}))
    assert len(state.run_completions) == 1


def test_beams_are_unknown_while_the_beam_feed_is_down():
    state = DaemonState()
    state.update_health("beam", "connected")
    state.update_beam_state("TS1", 150.0, "high")
    q = state.subscribe()

    state.update_health("beam", "disconnected")
    assert state.beam_states["TS1"] == {"current": 150.0, "power": "unknown"}
    events = [q.get_nowait() for _ in range(q.qsize())]
    assert {e.payload["beam"] for e in events if e.event == "beam"} == {"TS1", "TS2", "Muons"}

    state.update_health("beam", "reconnecting")
    state.update_health("beam", "connected")
    assert state.beam_states["TS1"]["power"] == "unknown"  # until PVWS re-sends it
    state.update_beam_state("TS1", 150.0, "high")
    assert state.beam_states["TS1"]["power"] == "high"
    state.update_health("mcr", "disconnected")  # other components leave beams alone
    assert state.beam_states["TS1"]["power"] == "high"
