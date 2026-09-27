import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from isis_monitor.config import InstrumentConfig
from isis_monitor.daemon_state import SUBSCRIBER_QUEUE_SIZE, DaemonState
from main import StateLogHandler

INSTRUMENTS = [
    InstrumentConfig("PEARL", 130.0, "TS1"),
    InstrumentConfig("WISH", 50.0, "TS2"),
]

def test_daemon_state_snapshot():
    state = DaemonState()
    state.update_beam_state("TS1", 45.0, "medium")
    state.update_mcr_news("Breaking News")
    state.update_health("daemon", "running")

    snap = state.snapshot()
    assert snap["mcr_news"] == "Breaking News"
    assert snap["beam_states"]["TS1"]["current"] == 45.0
    assert snap["health"]["daemon"] == "running"
    
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

    assert (ts, "TS1", 150.0, "high") in rows
    assert len(rows) == len(state.beam_states)
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


def test_run_name_and_counts_events():
    state = DaemonState(instruments=INSTRUMENTS)
    q = state.subscribe()
    state.update_run_name("WISH", "Run 42")
    state.update_counts("WISH", 12.5)
    assert [(e.event, e.payload) for e in _drain(q)] == [
        ("run", {"instrument": "WISH", "run_name": "Run 42"}),
        ("counts", {"instrument": "WISH", "counts": 12.5}),
    ]
    snap = state.snapshot()
    assert snap["instruments"]["WISH"] == {
        "run_name": "Run 42", "counts": 12.5, "total_runs": 0, "notify_counts": 50.0, "beam_target": "TS2",
        "run_started_at": None, "end_notified": False,
    }
    assert snap["instruments"]["PEARL"]["run_name"] == ""


def test_updates_for_unknown_instrument_are_ignored():
    state = DaemonState(instruments=INSTRUMENTS)
    q = state.subscribe()
    state.update_run_name("MERLIN", "Run 1")
    state.update_counts("MERLIN", 1.0)
    assert state.record_run_completed("MERLIN", datetime.now(timezone.utc)) == 0
    assert _drain(q) == []
    assert set(state.snapshot()["instruments"]) == {"PEARL", "WISH"}


def test_snapshot_is_a_copy():
    state = DaemonState()
    snap = state.snapshot()
    snap["beam_states"]["TS1"]["current"] = 999.0
    snap["health"]["daemon"] = "hacked"
    assert state.beam_states["TS1"]["current"] == 0.0
    assert state.health["daemon"] == "starting"


def test_restore_from_snapshot_ignores_empty_and_saved_health():
    state = DaemonState()
    state.restore_from_snapshot_json(None)
    state.restore_from_snapshot_json("")
    assert state.health["beam"] == "unknown"
    state.restore_from_snapshot_json(json.dumps({"health": {"beam": "connected"}}))
    assert state.health["beam"] == "unknown"  # describes the previous run, not this one


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


def test_record_run_completed_returns_per_instrument_total():
    state = DaemonState(instruments=INSTRUMENTS)
    now = datetime.now(timezone.utc)
    assert state.record_run_completed("PEARL", now) == 1
    assert state.record_run_completed("PEARL", now) == 2
    assert state.record_run_completed("WISH", now) == 1
    assert state.instruments["PEARL"]["total_runs"] == 2


def test_count_runs_completed_since_filters_by_window_and_instrument():
    state = DaemonState(instruments=INSTRUMENTS)
    now = datetime.now(timezone.utc)
    state.record_run_completed("PEARL", now - timedelta(hours=30))  # outside 24h window
    state.record_run_completed("PEARL", now - timedelta(hours=1))
    state.record_run_completed("WISH", now)

    since = now - timedelta(hours=24)
    assert state.count_runs_completed_since(since) == 2
    assert state.count_runs_completed_since(since, ["PEARL"]) == 1
    assert state.count_runs_completed_since(since, []) == 0


def test_instrument_state_persists_through_snapshot():
    state = DaemonState(instruments=INSTRUMENTS)
    state.record_run_completed("WISH", datetime.now(timezone.utc))
    state.update_run_name("WISH", "Run 7")
    state.update_counts("WISH", 3.0)

    snap_json = json.dumps(state.snapshot())

    # MERLIN is new and PEARL was removed from the config since the snapshot;
    # notify_counts comes from the new config, not the snapshot.
    restored = DaemonState(instruments=[
        InstrumentConfig("WISH", 75.0, "TS2"),
        InstrumentConfig("MERLIN", 10.0, "TS1"),
    ])
    restored.restore_from_snapshot_json(snap_json)
    assert restored.instruments["WISH"] == {
        "run_name": "Run 7", "counts": 3.0, "total_runs": 1, "notify_counts": 75.0, "beam_target": "TS2",
        "run_started_at": None, "end_notified": False,
    }
    assert restored.instruments["MERLIN"]["total_runs"] == 0
    assert "PEARL" not in restored.instruments


def test_legacy_snapshot_restores_into_first_instrument():
    """Snapshots from before multi-instrument support had one global run."""
    legacy = json.dumps({"run_name": "Old run", "current_counts": 42.0, "total_runs_completed": 30})
    state = DaemonState(instruments=INSTRUMENTS)
    state.restore_from_snapshot_json(legacy)
    assert state.instruments["PEARL"]["run_name"] == "Old run"
    assert state.instruments["PEARL"]["counts"] == 42.0
    assert state.instruments["PEARL"]["total_runs"] == 30
    assert state.instruments["WISH"]["total_runs"] == 0


def test_legacy_snapshot_without_run_fields_or_instruments_is_a_noop():
    state = DaemonState(instruments=INSTRUMENTS)
    state.restore_from_snapshot_json(json.dumps({"mcr_news": "x"}))
    assert state.instruments["PEARL"]["total_runs"] == 0

    empty = DaemonState()
    empty.restore_from_snapshot_json(json.dumps({"total_runs_completed": 3}))
    assert empty.instruments == {}


def test_malformed_instrument_entries_in_snapshot_are_skipped():
    state = DaemonState(instruments=INSTRUMENTS)
    state.restore_from_snapshot_json(json.dumps({"instruments": {
        "PEARL": "junk",
        "WISH": {"total_runs": 4},
        "MERLIN": {},
    }}))
    assert state.instruments["PEARL"]["total_runs"] == 0
    assert state.instruments["WISH"]["total_runs"] == 4

    # A bad value skips that instrument without partially applying it.
    state.restore_from_snapshot_json(json.dumps({"instruments": {"WISH": {"run_name": "R", "counts": None}}}))
    assert state.instruments["WISH"]["run_name"] == ""
    assert state.instruments["WISH"]["total_runs"] == 4



def test_run_progress_is_saved_but_not_published_and_survives_a_snapshot():
    state = DaemonState(instruments=INSTRUMENTS)
    q = state.subscribe()
    started = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)
    state.update_run_progress("WISH", started, True)
    state.update_run_progress("MERLIN", started, True)  # unknown: ignored
    assert _drain(q) == []

    restored = DaemonState(instruments=INSTRUMENTS)
    restored.restore_from_snapshot_json(json.dumps(state.snapshot()))
    assert restored.instruments["WISH"]["run_started_at"] == started.isoformat()
    assert restored.instruments["WISH"]["end_notified"] is True
    assert restored.instruments["PEARL"]["run_started_at"] is None


def test_malformed_run_start_in_snapshot_skips_that_instrument():
    state = DaemonState(instruments=INSTRUMENTS)
    state.restore_from_snapshot_json(json.dumps({"instruments": {
        "WISH": {"total_runs": 4, "run_started_at": "yesterday"},
    }}))
    assert state.instruments["WISH"]["total_runs"] == 0



def test_run_start_without_a_timezone_is_restored_as_utc():
    state = DaemonState(instruments=INSTRUMENTS)
    state.restore_from_snapshot_json(json.dumps({"instruments": {"WISH": {"run_started_at": "2026-09-27T08:00:00"}}}))
    assert state.instruments["WISH"]["run_started_at"] == "2026-09-27T08:00:00+00:00"


def test_recent_run_completions_survive_a_snapshot():
    now = datetime.now(timezone.utc)
    state = DaemonState(instruments=INSTRUMENTS)
    state.record_run_completed("PEARL", now - timedelta(hours=25))  # too old to keep
    state.record_run_completed("PEARL", now - timedelta(hours=2))
    state.record_run_completed("WISH", now - timedelta(hours=1))

    snap = state.snapshot()
    assert len(snap["run_completions"]) == 2
    # WISH has since been removed from the config.
    restored = DaemonState(instruments=INSTRUMENTS[:1])
    restored.restore_from_snapshot_json(json.dumps(snap))
    assert restored.count_runs_completed_since(now - timedelta(hours=24)) == 1
    assert list(restored.run_completions) == [(now - timedelta(hours=2), "PEARL")]


def test_old_and_malformed_run_completions_are_not_restored(caplog):
    now = datetime.now(timezone.utc)
    state = DaemonState(instruments=INSTRUMENTS)
    state.restore_from_snapshot_json(json.dumps({"mcr_news": "x"}))  # snapshot from before this was saved
    assert not state.run_completions

    with caplog.at_level(logging.WARNING):
        state.restore_from_snapshot_json(json.dumps({"run_completions": [
            [(now - timedelta(hours=30)).isoformat(), "PEARL"],
            ["not a time", "PEARL"],
            [now.isoformat(), ["unhashable"]],
            "junk",
            [(now - timedelta(minutes=5)).replace(tzinfo=None).isoformat(), "WISH"],  # naive means UTC
        ]}))
    assert [name for _, name in state.run_completions] == ["WISH"]
    assert "Skipped 3 malformed run completion(s)" in caplog.text
    state.restore_from_snapshot_json(json.dumps({"run_completions": {"not": "a list"}}))
    assert len(state.run_completions) == 1
