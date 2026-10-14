import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from isis_monitor.daemon_state import DaemonState

INSTRUMENTS = ["PEARL", "WISH"]


def test_snapshot_is_a_copy_of_what_is_saved():
    state = DaemonState()
    state.update_beam_state("TS1", 45.0, "medium")
    state.update_mcr_news("Breaking News")
    state.update_health("daemon", "running")

    snap = state.snapshot()
    assert snap["mcr_news"] == "Breaking News"
    assert snap["beam_states"]["TS1"]["current"] == 45.0
    assert "health" not in snap  # live state, not saved: it describes this run's connections
    assert "history" not in snap  # saved as samples, separately
    snap["beam_states"]["TS1"]["current"] = 999.0
    assert state.beam_states["TS1"]["current"] == 45.0


def test_history_sampling_and_trimming():
    state = DaemonState()
    state.update_beam_state("TS1", 150.0, "high")
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert state.sample_all_currents(ts) == [(ts, "TS1", 150.0, "high")]  # the others have no reading yet
    assert state.history["TS1"][-1] == (ts, 150.0, "high")

    # Unknown beams are ignored.
    state.append_beam_sample("Nope", 1.0, "low")
    state.update_beam_state("Nope", 1.0, "low")
    assert "Nope" not in state.history and "Nope" not in state.beam_states

    now = datetime.now(timezone.utc)
    for minutes in (30, 20, 10, 0):
        state.append_beam_sample("TS2", float(minutes), "low", ts=now - timedelta(minutes=minutes))
    state.trim_history_before(now - timedelta(minutes=15))
    assert [v for _, v, _ in state.history["TS2"]] == [10.0, 0.0]
    assert not state.history["TS1"]  # 2026-01-01 is before the cutoff too


def test_restore_from_snapshot():
    state = DaemonState(instrument_names=INSTRUMENTS)
    views = {"WISH": {"format": 2, "run_name": "Run 7"}, "PEARL": {"format": 2, "run_name": ""}}
    state.instruments = lambda: views  # the trackers' state
    state.update_mcr_news("Restored News")
    state.update_beam_state("Muons", 2.0, "low")
    snap_json = json.dumps(state.snapshot())
    assert json.loads(snap_json)["instruments"] == views

    # MERLIN is new and PEARL was removed from the config since the snapshot;
    # the entries are the trackers' to check (see InstrumentTracker.from_snapshot).
    restored = DaemonState(instrument_names=["WISH", "MERLIN"])
    restored.restore_from_snapshot_json(snap_json)
    assert restored.mcr_news == "Restored News"
    assert restored.beam_states["Muons"] == {"current": 2.0, "power": "unknown"}  # not yet live
    assert restored.saved_instruments == {"WISH": views["WISH"]}
    restored.restore_from_snapshot_json(json.dumps({"instruments": ["junk"]}))
    assert restored.saved_instruments == {}


@pytest.mark.parametrize("raw", [
    None,
    "",
    "{bad_json: True",
    "[]",  # JSON, but not a snapshot
    json.dumps({"mcr_news": "Old News"}),  # from before run completions were saved
    json.dumps({"run_completions": {"not": "a list"}}),
])
def test_unusable_snapshots_leave_the_state_as_it_was(raw):
    """Nothing in a corrupt or partial snapshot should crash or wipe what's there."""
    state = DaemonState(instrument_names=INSTRUMENTS)
    state.update_mcr_news("Old News")
    state.update_beam_state("TS1", 150.0, "high")
    state.record_run_completed("PEARL", datetime.now(timezone.utc), "1", 10.0)
    before = state.snapshot()
    state.restore_from_snapshot_json(raw)
    assert state.snapshot() == before


def test_run_completions_are_recorded_and_totalled_for_configured_instruments():
    state = DaemonState(instrument_names=INSTRUMENTS)
    now = datetime.now(timezone.utc)
    state.record_run_completed("PEARL", now - timedelta(hours=30), "1", 10.0)  # outside 24h window
    state.record_run_completed("PEARL", now - timedelta(hours=1), "2", 20.0)
    state.record_run_completed("PEARL", now, "", 5.5)
    state.record_run_completed("WISH", now, "3", 10.0)
    state.record_run_completed("MERLIN", now, "1", 10.0)  # not configured
    assert [name for _, name, _, _ in state.run_completions] == ["PEARL", "PEARL", "PEARL", "WISH"]

    since = now - timedelta(hours=24)
    assert state.runs_completed_since(since, ["PEARL", "MERLIN"]) == {"PEARL": (2, 25.5), "MERLIN": (0, 0.0)}
    assert state.runs_completed_since(since, []) == {}


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
    with caplog.at_level(logging.WARNING):
        state.restore_from_snapshot_json(json.dumps({"run_completions": [
            [(now - timedelta(hours=30)).isoformat(), "PEARL", "1", 1.0],
            ["not a time", "PEARL", "1", 1.0],
            [now.isoformat(), ["unhashable"], "1", 1.0],
            [now.isoformat(), "PEARL", "1", "NaN"],
            [now.isoformat(), "PEARL", "1"],  # no µA·h
            [now.isoformat(), "PEARL", 1.0],  # an older entry, without a run number
            [now.isoformat(), "PEARL", 1, 1.0],  # a run number that isn't text
            "junk",
            [(now - timedelta(minutes=5)).replace(tzinfo=None).isoformat(), "WISH", "2", 1.0],  # naive means UTC
        ]}))
    assert [(name, number) for _, name, number, _ in state.run_completions] == [("WISH", "2")]
    assert "Skipped 7 malformed run completion(s)" in caplog.text


def test_beams_are_unknown_while_the_beam_feed_is_down():
    state = DaemonState()
    state.update_health("beam", "connected")
    state.update_beam_state("TS1", 150.0, "high")

    state.update_health("beam", "disconnected")
    assert state.beam_states["TS1"] == {"current": 150.0, "power": "unknown"}
    assert state.health["beam"] == "disconnected"

    state.update_health("beam", "reconnecting")
    state.update_health("beam", "connected")
    assert state.beam_states["TS1"]["power"] == "unknown"  # until PVWS re-sends it
    state.update_beam_state("TS1", 150.0, "high")
    assert state.beam_states["TS1"]["power"] == "high"
    state.update_health("mcr", "disconnected")  # other components leave beams alone
    assert state.beam_states["TS1"]["power"] == "high"
