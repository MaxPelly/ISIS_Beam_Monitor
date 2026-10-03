import asyncio
import json
import random
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from isis_monitor.config import AppConfig, InstrumentConfig
from isis_monitor.daemon_state import DaemonState
from isis_monitor.messages import get_timezone
from isis_monitor.tests.helpers import fake_channel
from isis_monitor.storage import SQLiteStateStore
from isis_monitor.summary import LAST_SENT_KEY, TargetSummary, compute_summary, daily_summary_loop


def make_config(**overrides):
    base = dict(
        mcr_news_url="http://test.url/mcr",
        isis_websocket_url="",
        news_teams_url="",
        beam_teams_url="",
        experiment_teams_url="",
    )
    base.update(overrides)
    return AppConfig(**base)


# ---------------------------------------------------------------------------
# compute_summary
# ---------------------------------------------------------------------------

def test_compute_summary_empty_history_returns_zeroed_summary():
    result = compute_summary({"TS1": []}, since=datetime.now(timezone.utc))
    assert result["TS1"] == TargetSummary(0.0, 0, timedelta(0), "", coverage_pct=0.0)


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


def test_compute_summary_filters_by_since():
    t0 = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
    samples = [
        (t0, 10.0, "off"),
        (t0 + timedelta(hours=25), 10.0, "high"),
    ]
    summary = compute_summary({"TS1": samples}, since=t0 + timedelta(hours=24))["TS1"]
    assert summary.uptime_pct == 100.0  # only the second sample is within the window


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


def test_compute_summary_treats_unknown_as_off():
    t0 = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
    samples = [(t0, 10.0, "unknown"), (t0 + timedelta(minutes=1), 10.0, "unknown")]
    summary = compute_summary({"TS1": samples}, since=t0)["TS1"]
    assert summary.uptime_pct == 0.0


# ---------------------------------------------------------------------------
# daily_summary_loop
# ---------------------------------------------------------------------------

async def run_summary(store, state=None, rng=None, now=None, **overrides):
    """Run daily_summary_loop briefly with its clock stopped at `now` (default: the
    real time), so a run can't straddle midnight; summary_time defaults to that minute.
    Returns the channel."""
    now = now or datetime.now(timezone.utc)
    overrides.setdefault("summary_time", now.astimezone(get_timezone()).strftime("%H:%M"))
    clock = MagicMock(now=lambda tz: now.astimezone(tz))
    channel = fake_channel("Beam")
    stop_event = asyncio.Event()

    async def stop_soon():
        await asyncio.sleep(0.05)
        stop_event.set()

    with patch("isis_monitor.summary.SUMMARY_CHECK_INTERVAL", 0.01), patch("isis_monitor.summary.datetime", clock):
        asyncio.create_task(stop_soon())
        await daily_summary_loop(make_config(**overrides), state or DaemonState(), store, channel, stop_event, rng=rng)
    return channel


@pytest.fixture
def store(tmp_path):
    store = SQLiteStateStore(tmp_path / "summary.db")
    yield store
    store.close()


def noon():
    """Midday local time, in whatever display timezone is current."""
    return datetime(2026, 6, 1, 12, 0, tzinfo=get_timezone())


def ts1_card(channel):
    return next(c.args[0] for c in channel.broadcast.call_args_list if c.args[0].title.startswith("TS1"))


async def test_daily_summary_loop_sends_one_card_per_target_at_summary_time(store):
    state = DaemonState()
    t0 = datetime.now(timezone.utc) - timedelta(hours=1)
    for beam in state.history:
        state.history[beam].append((t0, 10.0, "high"))
    channel = await run_summary(store, state)
    # One card per target, even though multiple ticks elapsed — last_sent_date dedupes to once/day.
    assert {c.args[0].channel for c in channel.broadcast.call_args_list} == {"TS1", "TS2", "Muons"}
    assert channel.broadcast.call_count == 3


async def test_daily_summary_loop_fires_even_if_the_exact_minute_was_missed(store):
    """A slow tick that steps past the target minute must still send today's
    summary rather than silently waiting for tomorrow (regression guard)."""
    channel = await run_summary(store, now=noon(), summary_time="11:59")
    assert channel.broadcast.call_count == 3


async def test_daily_summary_loop_does_not_fire_before_summary_time(store):
    (await run_summary(store, now=noon(), summary_time="12:01")).broadcast.assert_not_called()


@pytest.mark.parametrize("fun_mode", [True, False])
async def test_daily_summary_loop_tracks_records_only_in_fun_mode(store, fun_mode):
    state = DaemonState()
    t0 = datetime.now(timezone.utc) - timedelta(hours=2)
    for minute in range(121):
        state.history["TS1"].append((t0 + timedelta(minutes=minute), 10.0, "high"))
    channel = await run_summary(store, state, rng=random.Random(1), fun_mode=fun_mode)
    assert ("New record" in ts1_card(channel).text) is fun_mode
    saved = store.load_snapshot("records")
    assert (json.loads(saved)["TS1"] > 0) if fun_mode else saved is None


async def test_daily_summary_not_resent_after_restart_same_day(tmp_path):
    """The last-sent date is persisted, so restarting the daemon after
    summary_time doesn't send the day's cards a second time."""
    now = datetime.now(timezone.utc)
    for expected in (3, 0):
        store = SQLiteStateStore(tmp_path / "summary.db")
        assert (await run_summary(store, now=now)).broadcast.call_count == expected
        store.close()


@pytest.mark.parametrize("records", ["{not json", "[1, 2]", '{"TS1": "x"}'])
async def test_daily_summary_tolerates_corrupt_persisted_values(store, caplog, records):
    store.upsert_snapshot("records", records)
    store.upsert_snapshot(LAST_SENT_KEY, "yesterday-ish")
    store.commit()
    now = datetime.now(timezone.utc)
    channel = await run_summary(store, rng=random.Random(1), now=now, fun_mode=True)
    assert "Corrupt records snapshot" in caplog.text
    assert channel.broadcast.call_count == 3
    assert store.load_snapshot(LAST_SENT_KEY) == now.astimezone(get_timezone()).date().isoformat()


async def test_daily_summary_counts_runs_only_on_instruments_using_that_target(store):
    state = DaemonState(instruments=[
        InstrumentConfig("PEARL", 130.0, "TS1"),
        InstrumentConfig("EMU", 10.0, "Muon"),
    ])
    now = datetime.now(timezone.utc)
    state.record_run_completed("PEARL", now)
    state.record_run_completed("PEARL", now)
    state.record_run_completed("EMU", now)
    state.record_run_completed("EMU", now - timedelta(hours=30))  # outside the window
    channel = await run_summary(store, state)
    runs = {c.args[0].channel: dict(c.args[0].facts)["Runs in last 24h"] for c in channel.broadcast.call_args_list}
    assert runs == {"TS1": "2", "TS2": "0", "Muons": "1"}


async def test_daily_summary_loop_survives_database_errors(store, caplog):
    """A transient SQLite error at load or save time is logged, not fatal."""
    import sqlite3
    real_run = store.run

    async def failing_run(fn, *args):
        if fn.__name__ in ("_load_records", "_save_progress"):
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
