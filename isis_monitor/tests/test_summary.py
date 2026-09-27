import asyncio
import json
import random
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from isis_monitor.config import AppConfig, InstrumentConfig
from isis_monitor.daemon_state import DaemonState
from isis_monitor.messages import get_timezone
from isis_monitor.notifiers import NotificationChannel
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
    assert result["TS1"] == TargetSummary(0.0, 0, timedelta(0), "")


def test_compute_summary_counts_trips_and_longest_streak():
    t0 = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
    powers = ["high", "high", "off", "high", "high", "high", "off"]
    samples = [(t0 + timedelta(minutes=i), 10.0, p) for i, p in enumerate(powers)]
    summary = compute_summary({"TS1": samples}, since=t0)["TS1"]
    assert summary.trips == 2
    assert summary.longest_on_streak == timedelta(minutes=3)
    assert summary.uptime_pct == pytest.approx(5 / 7 * 100)
    assert len(summary.sparkline) == 60


def test_compute_summary_filters_by_since():
    t0 = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
    samples = [
        (t0, 10.0, "off"),
        (t0 + timedelta(hours=25), 10.0, "high"),
    ]
    summary = compute_summary({"TS1": samples}, since=t0 + timedelta(hours=24))["TS1"]
    assert summary.uptime_pct == 100.0  # only the second sample is within the window


def test_compute_summary_treats_unknown_as_off():
    t0 = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
    samples = [(t0, 10.0, "unknown"), (t0 + timedelta(minutes=1), 10.0, "unknown")]
    summary = compute_summary({"TS1": samples}, since=t0)["TS1"]
    assert summary.uptime_pct == 0.0


# ---------------------------------------------------------------------------
# daily_summary_loop
# ---------------------------------------------------------------------------

async def _run_summary_loop_briefly(config, state, store, channel, **kw):
    stop_event = asyncio.Event()

    async def stop_soon():
        await asyncio.sleep(0.05)
        stop_event.set()

    with patch("isis_monitor.summary.SUMMARY_CHECK_INTERVAL", 0.01):
        asyncio.create_task(stop_soon())
        await daily_summary_loop(config, state, store, channel, stop_event, **kw)


@pytest.mark.asyncio
async def test_daily_summary_loop_sends_one_card_per_target_at_summary_time(tmp_path):
    now_local = datetime.now(get_timezone())
    config = make_config(summary_time=now_local.strftime("%H:%M"))

    state = DaemonState()
    t0 = datetime.now(timezone.utc) - timedelta(hours=1)
    for beam in state.history:
        state.history[beam].append((t0, 10.0, "high"))

    store = SQLiteStateStore(tmp_path / "summary_test.db")
    beam_channel = NotificationChannel("Beam")
    beam_channel.broadcast = AsyncMock()
    await _run_summary_loop_briefly(config, state, store, beam_channel)

    # One card per target (TS1, TS2, Muons), even though multiple ticks
    # elapsed before stop_event fired — last_sent_date dedupes to once/day.
    assert beam_channel.broadcast.call_count == 3
    sent_channels = {call.args[0].channel for call in beam_channel.broadcast.call_args_list}
    assert sent_channels == {"TS1", "TS2", "Muons"}
    store.close()


@pytest.mark.asyncio
async def test_daily_summary_loop_fires_even_if_the_exact_minute_was_missed(tmp_path):
    """A slow tick that steps past the target minute must still send today's
    summary rather than silently waiting for tomorrow (regression guard)."""
    now_local = datetime.now(get_timezone())
    just_passed = now_local.replace(minute=max(now_local.minute - 1, 0)).strftime("%H:%M")
    config = make_config(summary_time=just_passed)

    state = DaemonState()
    store = SQLiteStateStore(tmp_path / "summary_test_missed_minute.db")
    beam_channel = NotificationChannel("Beam")
    beam_channel.broadcast = AsyncMock()
    await _run_summary_loop_briefly(config, state, store, beam_channel)

    assert beam_channel.broadcast.call_count == 3
    store.close()


@pytest.mark.asyncio
async def test_daily_summary_loop_does_not_fire_outside_summary_time(tmp_path):
    # 23:59 is guaranteed later today without the hour-wraparound that
    # `now + timedelta(hours=6)` could hit (e.g. run at 22:00 -> 04:00,
    # which is numerically "earlier" and would wrongly look already-past).
    now_local = datetime.now(get_timezone())
    off_time = "23:58" if now_local.strftime("%H:%M") == "23:59" else "23:59"
    config = make_config(summary_time=off_time)

    state = DaemonState()
    store = SQLiteStateStore(tmp_path / "summary_test2.db")
    beam_channel = NotificationChannel("Beam")
    beam_channel.broadcast = AsyncMock()
    await _run_summary_loop_briefly(config, state, store, beam_channel)

    beam_channel.broadcast.assert_not_called()
    store.close()


@pytest.mark.asyncio
async def test_daily_summary_loop_flags_and_persists_new_record(tmp_path):
    now_local = datetime.now(get_timezone())
    config = make_config(summary_time=now_local.strftime("%H:%M"), fun_mode=True)

    state = DaemonState()
    t0 = datetime.now(timezone.utc) - timedelta(hours=2)
    state.history["TS1"].append((t0, 10.0, "high"))
    state.history["TS1"].append((t0 + timedelta(hours=2), 10.0, "high"))

    store = SQLiteStateStore(tmp_path / "summary_test3.db")
    beam_channel = NotificationChannel("Beam")
    beam_channel.broadcast = AsyncMock()
    await _run_summary_loop_briefly(config, state, store, beam_channel, rng=random.Random(1))

    ts1_notification = next(
        call.args[0] for call in beam_channel.broadcast.call_args_list
        if call.args[0].title.startswith("TS1")
    )
    assert "New record" in ts1_notification.text

    saved = json.loads(store.load_snapshot("records"))
    assert saved["TS1"] > 0
    store.close()


@pytest.mark.asyncio
async def test_daily_summary_loop_no_record_tracking_without_fun_mode(tmp_path):
    now_local = datetime.now(get_timezone())
    config = make_config(summary_time=now_local.strftime("%H:%M"), fun_mode=False)

    state = DaemonState()
    t0 = datetime.now(timezone.utc) - timedelta(hours=2)
    state.history["TS1"].append((t0, 10.0, "high"))
    state.history["TS1"].append((t0 + timedelta(hours=2), 10.0, "high"))

    store = SQLiteStateStore(tmp_path / "summary_test4.db")
    beam_channel = NotificationChannel("Beam")
    beam_channel.broadcast = AsyncMock()
    await _run_summary_loop_briefly(config, state, store, beam_channel)

    ts1_notification = next(
        call.args[0] for call in beam_channel.broadcast.call_args_list
        if call.args[0].title.startswith("TS1")
    )
    assert "New record" not in ts1_notification.text
    assert store.load_snapshot("records") is None
    store.close()


@pytest.mark.asyncio
async def test_daily_summary_not_resent_after_restart_same_day(tmp_path):
    """The last-sent date is persisted, so restarting the daemon after
    summary_time doesn't send the day's cards a second time."""
    config = make_config(summary_time=datetime.now(get_timezone()).strftime("%H:%M"))
    db = tmp_path / "summary_restart.db"

    store = SQLiteStateStore(db)
    first = NotificationChannel("Beam")
    first.broadcast = AsyncMock()
    await _run_summary_loop_briefly(config, DaemonState(), store, first)
    store.close()
    assert first.broadcast.call_count == 3

    store = SQLiteStateStore(db)
    after_restart = NotificationChannel("Beam")
    after_restart.broadcast = AsyncMock()
    await _run_summary_loop_briefly(config, DaemonState(), store, after_restart)
    store.close()
    after_restart.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_daily_summary_tolerates_corrupt_persisted_values(tmp_path, caplog):
    config = make_config(summary_time=datetime.now(get_timezone()).strftime("%H:%M"), fun_mode=True)
    store = SQLiteStateStore(tmp_path / "summary_corrupt.db")
    store.upsert_snapshot("records", "{not json")
    store.upsert_snapshot(LAST_SENT_KEY, "yesterday-ish")
    store.commit()
    channel = NotificationChannel("Beam")
    channel.broadcast = AsyncMock()

    await _run_summary_loop_briefly(config, DaemonState(), store, channel, rng=random.Random(1))

    assert "Corrupt records snapshot" in caplog.text
    assert channel.broadcast.call_count == 3
    assert store.load_snapshot(LAST_SENT_KEY) == datetime.now(get_timezone()).date().isoformat()
    store.close()


@pytest.mark.asyncio
async def test_daily_summary_counts_runs_only_on_instruments_using_that_target(tmp_path):
    config = make_config(summary_time=datetime.now(get_timezone()).strftime("%H:%M"))
    state = DaemonState(instruments=[
        InstrumentConfig("PEARL", 130.0, "TS1"),
        InstrumentConfig("EMU", 10.0, "Muon"),
    ])
    now = datetime.now(timezone.utc)
    state.record_run_completed("PEARL", now)
    state.record_run_completed("PEARL", now)
    state.record_run_completed("EMU", now)
    state.record_run_completed("EMU", now - timedelta(hours=30))  # outside the window

    store = SQLiteStateStore(tmp_path / "summary_runs.db")
    channel = NotificationChannel("Beam")
    channel.broadcast = AsyncMock()
    await _run_summary_loop_briefly(config, state, store, channel)

    runs = {
        call.args[0].channel: dict(call.args[0].facts)["Runs in last 24h"]
        for call in channel.broadcast.call_args_list
    }
    assert runs == {"TS1": "2", "TS2": "0", "Muons": "1"}
    store.close()


@pytest.mark.asyncio
async def test_daily_summary_loop_survives_database_errors(tmp_path, caplog):
    """A transient SQLite error at load or save time is logged, not fatal."""
    import sqlite3
    config = make_config(summary_time=datetime.now(get_timezone()).strftime("%H:%M"))
    store = SQLiteStateStore(tmp_path / "summary_errors.db")
    real_run = store.run

    async def failing_run(fn, *args):
        if fn.__name__ in ("_load_records", "_save_progress"):
            raise sqlite3.OperationalError("database is locked")
        return await real_run(fn, *args)

    store.run = failing_run
    channel = NotificationChannel("Beam")
    channel.broadcast = AsyncMock()
    await _run_summary_loop_briefly(config, DaemonState(), store, channel)

    assert channel.broadcast.call_count == 3  # still sent, and only once
    assert "Failed to load daily summary state" in caplog.text
    assert "Failed to save daily summary state" in caplog.text
    store.close()
