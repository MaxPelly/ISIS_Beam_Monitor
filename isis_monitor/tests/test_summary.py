import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from isis_monitor.config import AppConfig
from isis_monitor.daemon_state import DaemonState
from isis_monitor.messages import get_timezone
from isis_monitor.storage import SQLiteStateStore
from isis_monitor.summary import (
    LAST_SENT_KEY,
    TargetSummary,
    compute_summary,
    daily_summary_loop,
)
from isis_monitor.tests.helpers import fake_channel

# ---------------------------------------------------------------------------
# compute_summary
# ---------------------------------------------------------------------------

def test_compute_summary_uptime_counts_on_samples_in_the_window():
    t0 = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
    assert compute_summary({"TS1": []}, since=t0)["TS1"] == TargetSummary(0.0, 0, timedelta(0), "", coverage_pct=0.0)
    in_and_out = [(t0, 10.0, "off"), (t0 + timedelta(hours=25), 10.0, "high")]
    assert compute_summary({"TS1": in_and_out}, since=t0 + timedelta(hours=24))["TS1"].uptime_pct == 100.0
    unknown = [(t0, 10.0, "unknown"), (t0 + timedelta(minutes=1), 10.0, "unknown")]
    assert compute_summary({"TS1": unknown}, since=t0)["TS1"].uptime_pct == 0.0  # unknown counts as off


def test_compute_summary_counts_trips_and_longest_streak():
    t0 = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
    powers = ["high", "high", "off", "high", "high", "high", "off"]
    samples = [(t0 + timedelta(minutes=i), 10.0, p) for i, p in enumerate(powers)]
    summary = compute_summary({"TS1": samples}, since=t0)["TS1"]
    assert summary.trips == 2
    assert summary.longest_on_streak == timedelta(minutes=3)
    assert summary.uptime_pct == pytest.approx(5 / 7 * 100)
    assert len(summary.sparkline) == 60


def test_compute_summary_sparkline_spans_the_whole_day():
    """A day of 1-minute samples: off for the first 12 hours, then on. The
    sparkline shows both halves, not just the last 60 minutes."""
    t0 = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
    samples = [(t0 + timedelta(minutes=i), 0.0 if i < 720 else 100.0, "off" if i < 720 else "high")
               for i in range(1440)]
    sparkline = compute_summary({"TS1": samples}, since=t0)["TS1"].sparkline
    assert sparkline == " " * 30 + "█" * 30


def test_compute_summary_coverage_counts_missing_samples():
    t0 = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
    # 6 samples at 1-minute intervals over a 10-minute window: the feed was
    # down for the rest, which counts against coverage but not uptime.
    samples = [(t0 + timedelta(minutes=i), 150.0, "high") for i in range(6)]
    summary = compute_summary({"TS1": samples}, since=t0, until=t0 + timedelta(minutes=10))["TS1"]
    assert summary.uptime_pct == 100.0
    assert summary.coverage_pct == 60.0

    full = compute_summary({"TS1": samples}, since=t0, until=t0 + timedelta(minutes=5))["TS1"]
    assert full.coverage_pct == 100.0  # capped
    fast = compute_summary(
        {"TS1": samples}, since=t0, until=t0 + timedelta(minutes=10), sample_interval=30.0
    )["TS1"]
    assert fast.coverage_pct == 30.0


def test_compute_summary_on_streak_does_not_span_a_feed_gap():
    t0 = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
    hour_on = [(t0 + timedelta(minutes=i), 150.0, "high") for i in range(61)]
    after_gap = [(ts + timedelta(hours=7), cur, power) for ts, cur, power in hour_on]
    summary = compute_summary({"TS1": hour_on + after_gap}, since=t0)["TS1"]
    assert summary.longest_on_streak == timedelta(hours=1)
    assert summary.trips == 0
    # A restart's short gap (a couple of missed samples) doesn't end it.
    restart = [(ts + timedelta(minutes=63), cur, power) for ts, cur, power in hour_on]
    assert compute_summary({"TS1": hour_on + restart}, since=t0)["TS1"].longest_on_streak == timedelta(hours=2, minutes=3)


# ---------------------------------------------------------------------------
# daily_summary_loop
# ---------------------------------------------------------------------------

TICKS = 5  # clock reads before run_summary stops the loop


async def run_summary(store, state=None, now=None, **overrides):
    """Run daily_summary_loop briefly with its clock stopped at `now` (default: the
    real time), so a run can't straddle midnight; summary_time defaults to that minute.
    Returns the channel."""
    now = now or datetime.now(timezone.utc)
    overrides.setdefault("summary_time", now.astimezone(get_timezone()).strftime("%H:%M"))
    channel = fake_channel("Beam")
    stop_event = asyncio.Event()
    reads = 0

    def read_clock(tz):  # stops the loop after a few ticks
        nonlocal reads
        reads += 1
        if reads >= TICKS:
            stop_event.set()
        return now.astimezone(tz)

    with patch("isis_monitor.summary.SUMMARY_CHECK_INTERVAL", 0.001), \
            patch("isis_monitor.summary.datetime", MagicMock(now=read_clock)):
        await daily_summary_loop(AppConfig(**overrides), state or DaemonState(), store, channel, stop_event)
    return channel


@pytest.fixture
def store(tmp_path):
    store = SQLiteStateStore(tmp_path / "summary.db")
    yield store
    store.close()


def noon():
    """Midday local time, in whatever display timezone is current."""
    return datetime(2026, 6, 1, 12, 0, tzinfo=get_timezone())


@pytest.mark.parametrize("summary_time, cards", [
    ("12:00", 3),
    # A slow tick that steps past the target minute must still send today's
    # summary rather than silently waiting for tomorrow (regression guard).
    ("11:59", 3),
    ("12:01", 0),
])
async def test_daily_summary_loop_sends_one_card_per_target_once_summary_time_is_reached(store, summary_time, cards):
    state = DaemonState()
    for beam in state.history:
        state.history[beam].append((noon() - timedelta(hours=1), 10.0, "high"))
    channel = await run_summary(store, state, now=noon(), summary_time=summary_time)
    # Once per target, though several ticks elapsed: the last-sent date dedupes to once a day.
    assert channel.broadcast.call_count == cards
    assert {c.args[0].beam for c in channel.broadcast.call_args_list} == ({"TS1", "TS2", "Muons"} if cards else set())


async def test_daily_summary_last_sent_date_is_kept_across_restarts(tmp_path):
    """Restarting the daemon after summary_time doesn't send the day's cards a
    second time; a corrupt saved date is taken as not sent today."""
    now = datetime.now(timezone.utc)
    store = SQLiteStateStore(tmp_path / "summary.db")
    store.upsert_snapshot(LAST_SENT_KEY, "yesterday-ish")
    store.commit()
    for expected in (3, 0):
        assert (await run_summary(store, now=now)).broadcast.call_count == expected
        assert store.load_snapshot(LAST_SENT_KEY) == now.astimezone(get_timezone()).date().isoformat()
        store.close()
        store = SQLiteStateStore(tmp_path / "summary.db")
    store.close()


async def test_daily_summary_counts_runs_only_on_instruments_using_that_target(store):
    state = DaemonState(instrument_names=["PEARL", "EMU"])
    state.instruments = lambda: {"PEARL": {"beam_target": "TS1"}, "EMU": {"beam_target": "Muons"}}  # trackers' views
    now = datetime.now(timezone.utc)
    state.record_run_completed("PEARL", now, "1", 60.0)
    state.record_run_completed("PEARL", now, "2", 70.0)
    state.record_run_completed("EMU", now, "3", 5.0)
    state.record_run_completed("EMU", now - timedelta(hours=30), "4", 5.0)  # outside the window
    channel = await run_summary(store, state)
    cards = {c.args[0].beam: dict(c.args[0].facts) for c in channel.broadcast.call_args_list}
    assert {beam: facts["Runs in last 24h"] for beam, facts in cards.items()} == {"TS1": "2", "TS2": "0", "Muons": "1"}
    assert (cards["TS1"]["PEARL"], cards["Muons"]["EMU"]) == ("2 runs, 130.0 µA·h", "1 run, 5.0 µA·h")
    assert "EMU" not in cards["TS1"]


async def test_daily_summary_loop_survives_database_errors(store, caplog):
    """A transient SQLite error at load or save time is logged, not fatal."""
    import sqlite3
    real_run = store.run

    async def failing_run(fn, *args):
        if fn.__name__ in ("_load_last_sent", "_save_progress"):
            raise sqlite3.OperationalError("database is locked")
        return await real_run(fn, *args)

    store.run = failing_run
    channel = await run_summary(store)
    assert channel.broadcast.call_count == 3  # still sent, and only once
    assert "Failed to load daily summary state" in caplog.text
    assert "Failed to save daily summary state" in caplog.text


async def test_daily_summary_loop_survives_a_failing_summary(store, caplog):
    """An unexpected error skips that day's cards, logged, without ending the loop."""
    with patch("isis_monitor.summary.daily_summary", side_effect=RuntimeError("boom")):
        channel = await run_summary(store)
    channel.broadcast.assert_not_called()
    assert caplog.text.count("Failed to send the daily summary") == 1  # not retried every tick
