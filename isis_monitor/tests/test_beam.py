import asyncio
import pytest
import base64
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from isis_monitor.config import AppConfig
from isis_monitor.notifiers import NotificationChannel
from isis_monitor.beam import (
    BeamMonitor,
    BEAM_TARGETS,
    _fit_rate,
)

DEBOUNCE_SECONDS = 0.05
SETTLE = DEBOUNCE_SECONDS * 3  # wait comfortably past the debounce window in tests


@pytest.fixture
def mock_config():
    return AppConfig(
        mcr_news_url="",
        isis_websocket_url="wss://test",
        news_teams_url="",
        beam_teams_url="",
        experiment_teams_url="",
    )


@pytest.fixture
def mock_channels():
    beam_channel = NotificationChannel("Beam")
    beam_channel.broadcast = AsyncMock()
    exp_channel = NotificationChannel("Exp")
    exp_channel.broadcast = AsyncMock()
    return beam_channel, exp_channel


def make_monitor(mock_config, mock_channels, counts_target=100, rng=None, sink=None):
    beam_channel, exp_channel = mock_channels
    return BeamMonitor(
        mock_config, beam_channel, exp_channel, counts_target=counts_target,
        debounce_seconds=DEBOUNCE_SECONDS, rng=rng, sink=sink,
    )


# ---------------------------------------------------------------------------
# _safe_float
# ---------------------------------------------------------------------------

def test_safe_float(mock_config, mock_channels):
    m = make_monitor(mock_config, mock_channels)
    assert m._safe_float("123.4") == 123.4
    assert m._safe_float(123.4) == 123.4
    assert m._safe_float("NaN") == 0.0
    assert m._safe_float("nan") == 0.0
    assert m._safe_float("bad_string") == 0.0
    assert m._safe_float(None) == 0.0


# ---------------------------------------------------------------------------
# _get_power_label
# ---------------------------------------------------------------------------

def test_get_power_label(mock_config, mock_channels):
    m = make_monitor(mock_config, mock_channels)
    assert m._get_power_label(-5, "TS1") == "off"
    assert m._get_power_label(0, "TS1") == "off"
    assert m._get_power_label(20, "TS1") == "low"
    assert m._get_power_label(75, "TS1") == "medium"
    assert m._get_power_label(150, "TS1") == "high"
    assert m._get_power_label(0, "TS2") == "off"
    assert m._get_power_label(5, "TS2") == "low"
    assert m._get_power_label(15, "TS2") == "medium"
    assert m._get_power_label(30, "TS2") == "high"


# ---------------------------------------------------------------------------
# _handle_update — beam-current arm
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_update_beam_startup_sends_immediately(mock_config, mock_channels):
    """The first reading for a target is a startup card, sent with no debounce."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "10.0"})

    assert m.state.beams["TS1"].current == 10.0
    assert m.state.beams["TS1"].power == "low"
    beam_channel.broadcast.assert_called_once()
    notification = beam_channel.broadcast.call_args[0][0]
    assert notification.title == "Monitor online: TS1 is low"


@pytest.mark.asyncio
async def test_handle_update_beam_no_change_no_broadcast(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "10.0"})
    beam_channel.broadcast.reset_mock()

    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "45.0"})
    await asyncio.sleep(SETTLE)

    assert m.state.beams["TS1"].current == 45.0
    assert m.state.beams["TS1"].power == "low"
    beam_channel.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_handle_update_beam_change_is_debounced(mock_config, mock_channels):
    """A confirmed state change is not sent immediately — only after the debounce window."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "10.0"})  # startup
    beam_channel.broadcast.reset_mock()

    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "60.0"})
    assert m.state.beams["TS1"].power == "medium"  # state updates immediately
    beam_channel.broadcast.assert_not_called()  # but no card yet — still pending

    await asyncio.sleep(SETTLE)

    beam_channel.broadcast.assert_called_once()
    notification = beam_channel.broadcast.call_args[0][0]
    assert notification.title == "TS1 ⬆️ low → medium"


@pytest.mark.asyncio
async def test_handle_update_beam_flapping_sends_nothing(mock_config, mock_channels):
    """A change that reverts before the debounce window elapses is dropped entirely."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "10.0"})  # startup
    beam_channel.broadcast.reset_mock()

    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "60.0"})  # -> medium
    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "10.0"})  # -> low again
    await asyncio.sleep(SETTLE)

    beam_channel.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_handle_update_beam_trip_notes_other_targets(mock_config, mock_channels):
    """When multiple targets go off in the same debounce window, each card names the others."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    # Startup — get all three targets to a non-zero state first.
    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "60.0"})
    await m._handle_update({"pv": mock_config.ts2_beam_current_pv, "value": "20.0"})
    await m._handle_update({"pv": mock_config.muon_beam_current_pv, "value": "3.0"})
    beam_channel.broadcast.reset_mock()

    # All three trip off within the same window.
    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "0.0"})
    await m._handle_update({"pv": mock_config.ts2_beam_current_pv, "value": "0.0"})
    await m._handle_update({"pv": mock_config.muon_beam_current_pv, "value": "0.0"})
    await asyncio.sleep(SETTLE)

    assert beam_channel.broadcast.call_count == 3
    for call in beam_channel.broadcast.call_args_list:
        notification = call.args[0]
        assert "also went off, likely a facility-wide trip" in notification.text


@pytest.mark.asyncio
async def test_change_aggregator_cancel_all_stops_pending_flush(mock_config, mock_channels):
    """cancel_all() (called on shutdown) must stop pending timers from firing."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "10.0"})  # startup
    beam_channel.broadcast.reset_mock()

    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "60.0"})  # pending
    m.change_aggregator.cancel_all()
    await asyncio.sleep(SETTLE)

    beam_channel.broadcast.assert_not_called()
    assert m.change_aggregator._pending == {}


@pytest.mark.asyncio
async def test_handle_update_beam_fun_mode_off_no_flavour(mock_config, mock_channels):
    """fun_mode defaults to False — no flavour line is added, even with an rng available."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels, rng=random.Random(1))

    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "10.0"})  # startup
    assert beam_channel.broadcast.call_args[0][0].flavour == ""
    beam_channel.broadcast.reset_mock()

    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "60.0"})
    await asyncio.sleep(SETTLE)
    assert beam_channel.broadcast.call_args[0][0].flavour == ""


@pytest.mark.asyncio
async def test_handle_update_beam_fun_mode_on_adds_flavour(mock_config, mock_channels):
    fun_config = replace(mock_config, fun_mode=True)
    beam_channel, exp_channel = mock_channels
    m = make_monitor(fun_config, mock_channels, rng=random.Random(1))

    await m._handle_update({"pv": fun_config.ts1_beam_current_pv, "value": "10.0"})  # startup
    assert beam_channel.broadcast.call_args[0][0].flavour != ""
    beam_channel.broadcast.reset_mock()

    await m._handle_update({"pv": fun_config.ts1_beam_current_pv, "value": "60.0"})
    await asyncio.sleep(SETTLE)
    assert beam_channel.broadcast.call_args[0][0].flavour != ""


# ---------------------------------------------------------------------------
# _handle_update — run-name (b64byt) arm
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_update_run_name_first_set(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    run_name = "Run 12345"
    b64 = base64.b64encode(run_name.encode()).decode()
    await m._handle_update({"pv": mock_config.run_name_pv, "b64byt": b64})

    assert m.state.run_name == run_name
    exp_channel.broadcast.assert_not_called()  # No previous run → no notification


@pytest.mark.asyncio
async def test_handle_update_run_name_change(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    # Seed first run
    m.state.run_name = "Run 12345"
    m.state.run_started_at = datetime.now(timezone.utc) - timedelta(hours=2)
    m.state.current_good_frames = 900.0
    m.state.current_raw_frames = 1000.0

    new_run = "Run 12346"
    b64 = base64.b64encode(new_run.encode()).decode()
    await m._handle_update({"pv": mock_config.run_name_pv, "b64byt": b64})

    assert m.state.run_name == new_run
    exp_channel.broadcast.assert_called_once()
    notification = exp_channel.broadcast.call_args[0][0]
    assert "new run" in notification.title.lower()
    assert notification.text == new_run
    assert notification.facts == [
        ("Previous run", "Run 12345"),
        ("Duration", "2h 0m"),
        ("Final good frames", "900"),
        ("Final raw frames", "1000"),
    ]
    assert m.state.current_counts == 0
    assert m.state.current_good_frames == 0.0
    assert m.state.current_raw_frames == 0.0


@pytest.mark.asyncio
async def test_handle_update_run_name_change_resets_end_notified(mock_config, mock_channels):
    """A new run must re-arm the 'about to finish' notification for itself,
    even if the previous run ended with end_notified already set."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels, counts_target=10)
    m.state.run_name = "Run 1"
    m.state.run_started_at = datetime.now(timezone.utc)
    m.state.end_notified = True

    b64 = base64.b64encode(b"Run 2").decode()
    await m._handle_update({"pv": mock_config.run_name_pv, "b64byt": b64})
    assert m.state.end_notified is False

    exp_channel.broadcast.reset_mock()
    # counts_target=10 is small enough that the old "< target - 25" reset
    # path would never trip; the run-start reset must do it instead.
    await m._handle_update({"pv": mock_config.counts_pv, "text": "5/12"})
    exp_channel.broadcast.assert_called_once()
    assert "about to finish" in exp_channel.broadcast.call_args[0][0].title


@pytest.mark.asyncio
async def test_handle_update_run_name_nan_ignored(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    await m._handle_update({"pv": mock_config.run_name_pv, "b64byt": "nan"})
    assert m.state.run_name == ""
    exp_channel.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_handle_update_run_name_change_records_completion_on_sink(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    sink = MagicMock()
    sink.record_run_completed.return_value = 5  # not a multiple of 25
    m = make_monitor(mock_config, mock_channels, sink=sink)
    m.state.run_name = "Run 1"
    m.state.run_started_at = datetime.now(timezone.utc)

    b64 = base64.b64encode(b"Run 2").decode()
    await m._handle_update({"pv": mock_config.run_name_pv, "b64byt": b64})

    sink.record_run_completed.assert_called_once()
    exp_channel.broadcast.assert_called_once()  # only the "new run" card, no milestone


@pytest.mark.asyncio
async def test_handle_update_run_name_change_no_completion_on_first_ever_run(mock_config, mock_channels):
    """The very first run seen isn't a completion — nothing to count yet."""
    beam_channel, exp_channel = mock_channels
    sink = MagicMock()
    m = make_monitor(mock_config, mock_channels, sink=sink)

    b64 = base64.b64encode(b"Run 1").decode()
    await m._handle_update({"pv": mock_config.run_name_pv, "b64byt": b64})

    sink.record_run_completed.assert_not_called()


@pytest.mark.asyncio
async def test_handle_update_run_name_change_milestone_every_25_runs(mock_config, mock_channels):
    fun_config = replace(mock_config, fun_mode=True)
    beam_channel, exp_channel = mock_channels
    sink = MagicMock()
    sink.record_run_completed.return_value = 25
    m = make_monitor(fun_config, mock_channels, sink=sink)
    m.state.run_name = "Run 24"
    m.state.run_started_at = datetime.now(timezone.utc)

    b64 = base64.b64encode(b"Run 25").decode()
    await m._handle_update({"pv": fun_config.run_name_pv, "b64byt": b64})

    assert exp_channel.broadcast.call_count == 2  # "new run" card + milestone card
    milestone = exp_channel.broadcast.call_args_list[1].args[0]
    assert "25" in milestone.title


@pytest.mark.asyncio
async def test_handle_update_run_name_change_no_milestone_without_fun_mode(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    sink = MagicMock()
    sink.record_run_completed.return_value = 25
    m = make_monitor(mock_config, mock_channels, sink=sink)  # fun_mode defaults to False
    m.state.run_name = "Run 24"
    m.state.run_started_at = datetime.now(timezone.utc)

    b64 = base64.b64encode(b"Run 25").decode()
    await m._handle_update({"pv": mock_config.run_name_pv, "b64byt": b64})

    exp_channel.broadcast.assert_called_once()  # only the "new run" card


# ---------------------------------------------------------------------------
# _handle_update — counts (text) arm
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_update_counts_below_threshold(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels, counts_target=100)
    m.state.run_name = "Run 1"

    await m._handle_update({"pv": mock_config.counts_pv, "text": "50/90"})
    assert m.state.current_counts == 90.0
    exp_channel.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_handle_update_counts_triggers_notification(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels, counts_target=100)
    m.state.run_name = "Run 1"

    await m._handle_update({"pv": mock_config.counts_pv, "text": "50/110"})
    assert m.state.current_counts == 110.0  # counts_type defaults to "raw"
    assert m.state.current_good_frames == 50.0
    assert m.state.current_raw_frames == 110.0
    assert m.state.end_notified is True
    exp_channel.broadcast.assert_called_once()
    notification = exp_channel.broadcast.call_args[0][0]
    assert "about to finish" in notification.title
    assert notification.text == "Run 1"
    fact_keys = [key for key, _ in notification.facts]
    assert fact_keys == ["Frames", "Rate", "Good-frame efficiency", "Instrument beam"]
    assert ("Frames", "110 / 100") in notification.facts
    assert ("Instrument beam", "") in notification.facts  # TS1 never seen a beam-current update


@pytest.mark.asyncio
async def test_handle_update_counts_tracks_good_frames_when_configured(mock_config, mock_channels):
    good_config = replace(mock_config, counts_type="good")
    beam_channel, exp_channel = mock_channels
    m = make_monitor(good_config, mock_channels, counts_target=100)
    m.state.run_name = "Run 1"

    await m._handle_update({"pv": good_config.counts_pv, "text": "110/200"})
    assert m.state.current_counts == 110.0  # tracks good frames, not raw
    exp_channel.broadcast.assert_called_once()


@pytest.mark.asyncio
async def test_handle_update_counts_resets_end_notified(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels, counts_target=100)
    m.state.run_name = "Run 1"
    m.state.end_notified = True

    # Drops below target - 25 = 75 → resets flag
    await m._handle_update({"pv": mock_config.counts_pv, "text": "50/50"})
    assert m.state.end_notified is False


@pytest.mark.asyncio
async def test_handle_update_counts_malformed(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels, counts_target=100)

    await m._handle_update({"pv": mock_config.counts_pv, "text": "bad_format"})
    assert m.state.current_counts == -1.0  # unchanged, no crash


# ---------------------------------------------------------------------------
# _fit_rate
# ---------------------------------------------------------------------------

def test_fit_rate_returns_zero_for_fewer_than_two_samples():
    assert _fit_rate([]) == 0.0
    assert _fit_rate([(datetime.now(timezone.utc), 10.0)]) == 0.0


def test_fit_rate_computes_slope_per_second():
    t0 = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    samples = [(t0, 0.0), (t0 + timedelta(seconds=10), 100.0)]
    assert _fit_rate(samples) == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# _check_frame_progress — veto / stall detection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_frame_progress_detects_veto(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)
    m.state.run_name = "Run 1"
    now = datetime.now(timezone.utc)

    m.state.current_good_frames = 100.0
    m.state.current_raw_frames = 200.0
    m.state.last_check_good = 100.0
    m.state.last_check_raw = 150.0  # raw rose, good didn't

    await m._check_frame_progress(now)

    exp_channel.broadcast.assert_called_once()
    notification = exp_channel.broadcast.call_args[0][0]
    assert notification.title == "Frames being vetoed"
    assert m.state.veto_warned is True


@pytest.mark.asyncio
async def test_check_frame_progress_veto_warns_once_then_resets_on_movement(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)
    m.state.run_name = "Run 1"
    now = datetime.now(timezone.utc)

    m.state.current_good_frames = 100.0
    m.state.current_raw_frames = 200.0
    m.state.last_check_good = 100.0
    m.state.last_check_raw = 150.0
    await m._check_frame_progress(now)
    exp_channel.broadcast.assert_called_once()

    # Still vetoed on the next tick — no repeat warning.
    exp_channel.broadcast.reset_mock()
    m.state.current_raw_frames = 300.0
    await m._check_frame_progress(now + timedelta(seconds=60))
    exp_channel.broadcast.assert_not_called()

    # Good frames finally move — resets the warning flag.
    m.state.current_good_frames = 250.0
    await m._check_frame_progress(now + timedelta(seconds=120))
    assert m.state.veto_warned is False


@pytest.mark.asyncio
async def test_check_frame_progress_detects_stall_when_instrument_beam_on(mock_config, mock_channels):
    beam_config = replace(mock_config, stall_minutes=0.01)  # ~0.6s, fast for tests
    beam_channel, exp_channel = mock_channels
    m = make_monitor(beam_config, mock_channels)
    m.state.run_name = "Run 1"
    m.state.beams["TS1"].power = "high"  # instrument beam is on

    now = datetime.now(timezone.utc)
    m.state.current_good_frames = 100.0
    m.state.current_raw_frames = 100.0
    m.state.last_check_good = 100.0
    m.state.last_check_raw = 100.0

    await m._check_frame_progress(now)  # starts the stall clock
    exp_channel.broadcast.assert_not_called()

    await m._check_frame_progress(now + timedelta(seconds=1))  # past stall_minutes
    exp_channel.broadcast.assert_called_once()
    notification = exp_channel.broadcast.call_args[0][0]
    assert notification.title == "Frames stalled"


@pytest.mark.asyncio
async def test_check_frame_progress_no_stall_warning_when_instrument_beam_off(mock_config, mock_channels):
    beam_config = replace(mock_config, stall_minutes=0.01)
    beam_channel, exp_channel = mock_channels
    m = make_monitor(beam_config, mock_channels)
    m.state.run_name = "Run 1"
    m.state.beams["TS1"].power = "off"  # instrument beam is off — no warning expected

    now = datetime.now(timezone.utc)
    m.state.current_good_frames = 100.0
    m.state.current_raw_frames = 100.0
    m.state.last_check_good = 100.0
    m.state.last_check_raw = 100.0

    await m._check_frame_progress(now)
    await m._check_frame_progress(now + timedelta(seconds=1))
    exp_channel.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_check_frame_progress_movement_resets_stall_clock(mock_config, mock_channels):
    beam_config = replace(mock_config, stall_minutes=0.01)
    beam_channel, exp_channel = mock_channels
    m = make_monitor(beam_config, mock_channels)
    m.state.run_name = "Run 1"
    m.state.beams["TS1"].power = "high"

    now = datetime.now(timezone.utc)
    m.state.current_good_frames = 100.0
    m.state.current_raw_frames = 100.0
    m.state.last_check_good = 100.0
    m.state.last_check_raw = 100.0
    await m._check_frame_progress(now)
    assert m.state.frames_stalled_since is not None

    # Frames move again — stall clock resets.
    m.state.current_good_frames = 110.0
    m.state.current_raw_frames = 110.0
    await m._check_frame_progress(now + timedelta(seconds=0.5))
    assert m.state.frames_stalled_since is None

    exp_channel.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_check_frame_progress_no_active_run_never_warns(mock_config, mock_channels):
    """Between runs, frame counts are naturally static — that must not look
    like a stall or a veto just because no run is currently in progress."""
    beam_config = replace(mock_config, stall_minutes=0.01)
    beam_channel, exp_channel = mock_channels
    m = make_monitor(beam_config, mock_channels)
    assert m.state.run_name == ""  # no run active
    m.state.beams["TS1"].power = "high"

    now = datetime.now(timezone.utc)
    m.state.current_good_frames = 100.0
    m.state.current_raw_frames = 100.0
    m.state.last_check_good = 100.0
    m.state.last_check_raw = 100.0

    await m._check_frame_progress(now)
    await m._check_frame_progress(now + timedelta(seconds=1))

    exp_channel.broadcast.assert_not_called()
    assert m.state.frames_stalled_since is None
