import json
from datetime import datetime, timedelta, timezone

from isis_monitor.config import InstrumentConfig
from isis_monitor.daemon_state import DaemonState
from isis_monitor.instrument import InstrumentTracker
from isis_monitor.status import HISTORY_POINTS, MAX_TITLE, build
from isis_monitor.tests.helpers import fake_channel

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def state_with(*instruments):
    state = DaemonState(instrument_names=[i.instrument.name for i in instruments])
    state.instruments = lambda: {i.instrument.name: i.view() for i in instruments}
    return state


def tracker(name="PEARL", **config):
    return InstrumentTracker(InstrumentConfig(name, 130.0, "TS1", **config), fake_channel(), lambda b: "high", 15)


def test_beams_report_live_values_and_unknown_ones_as_unknown():
    state = state_with()
    state.update_beam_state("TS1", 152.34, "high")
    status = build(state, {"TS1": NOW - timedelta(hours=1)}, 60, NOW)
    ts1, ts2 = status["beams"][:2]
    assert (ts1["current"], ts1["power"], ts1["since"]) == (152.34, "high", "2026-10-13T11:00:00+00:00")
    assert (ts2["current"], ts2["power"], ts2["since"]) == (None, "unknown", None)  # not 0: no reading yet
    assert [b["name"] for b in status["beams"]] == ["TS1", "TS2", "Muons"]


def test_history_is_24h_in_10_minute_bins():
    state = state_with()
    for minutes, current, power in ((5, 100.0, "high"), (8, 50.0, "high"), (9, 0.0, "off"),
                                    (24 * 60 + 5, 1.0, "low")):  # the last is too old
        state.history["TS1"].append((NOW - timedelta(minutes=minutes), current, power))
    values = build(state, {}, 60, NOW)["beams"][0]["history"]["values"]
    assert len(values) == HISTORY_POINTS
    assert values[-1] == [50.0, "high"]  # mean, and the most common power
    assert values[-2] == [None, "unknown"] and values[0] == [None, "unknown"]


def test_instruments_come_from_the_trackers():
    pearl = tracker(auto_counts=True)
    pearl.state.run_name, pearl.state.current_counts, pearl.state.run_number = "x" * 300, 65.256, "127921"
    pearl.state.run_state, pearl.state.rb_number = "RUNNING", "2610403"
    wish = tracker("WISH")
    (p, w) = build(state_with(pearl, wish), {}, 60, NOW)["instruments"]
    assert p == {"name": "PEARL", "beam": "TS1", "run_number": "127921", "run_state": "RUNNING",
                 "title": "x" * MAX_TITLE, "started_at": None, "counts": 65.26, "target": 130.0, "auto": True,
                 "rb": "2610403"}
    assert w["counts"] is None  # nothing read yet


def test_mcr_news_and_health():
    state = state_with()
    assert build(state, {}, 60, NOW)["mcr"] == {"news": "", "seen_at": None}  # not the placeholder
    state.update_mcr_news("Beam on.")
    state.update_health("beam", "connected")
    status = build(state, {}, 60, NOW)
    assert status["mcr"]["news"] == "Beam on." and status["mcr"]["seen_at"]
    assert status["health"]["beam"] == "connected"
    assert (status["v"], status["kind"], status["interval_s"]) == (1, "status", 60)


def test_a_long_status_stays_well_under_the_push_sites_limit():
    """20 instruments with the longest titles and news; the site takes up to 64 KB."""
    trackers = [tracker(f"INSTRUMENT{n:02d}") for n in range(20)]
    for t in trackers:
        t.state.run_name = "é" * 300
    state = state_with(*trackers)
    state.update_mcr_news("é" * 1000)
    for name in state.history:
        for minutes in range(0, 24 * 60, 1):
            state.history[name].append((NOW - timedelta(minutes=minutes), 123.456, "medium"))
    assert len(json.dumps(build(state, {}, 60, NOW), ensure_ascii=False).encode()) < 48 * 1024
