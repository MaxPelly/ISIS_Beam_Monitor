import asyncio
import pytest
import base64
from unittest.mock import AsyncMock, patch
from isis_monitor.config import AppConfig
from isis_monitor.notifiers import NotificationChannel
from isis_monitor.beam import (
    BeamMonitor,
    BEAM_TARGETS,
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


def make_monitor(mock_config, mock_channels, counts_target=100):
    beam_channel, exp_channel = mock_channels
    return BeamMonitor(
        mock_config, beam_channel, exp_channel, counts_target=counts_target,
        debounce_seconds=DEBOUNCE_SECONDS,
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

    new_run = "Run 12346"
    b64 = base64.b64encode(new_run.encode()).decode()
    await m._handle_update({"pv": mock_config.run_name_pv, "b64byt": b64})

    assert m.state.run_name == new_run
    exp_channel.broadcast.assert_called_once()
    notification = exp_channel.broadcast.call_args[0][0]
    assert "new run" in notification.title.lower()
    assert notification.text == new_run
    assert m.state.current_counts == 0


@pytest.mark.asyncio
async def test_handle_update_run_name_nan_ignored(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    await m._handle_update({"pv": mock_config.run_name_pv, "b64byt": "nan"})
    assert m.state.run_name == ""
    exp_channel.broadcast.assert_not_called()


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
    assert m.state.current_counts == 110.0
    assert m.state.end_notified is True
    exp_channel.broadcast.assert_called_once()
    notification = exp_channel.broadcast.call_args[0][0]
    assert "about to finish" in notification.title
    assert notification.text == "Run 1"


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
