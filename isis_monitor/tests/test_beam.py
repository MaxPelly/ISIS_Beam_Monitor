import pytest
import base64
from unittest.mock import AsyncMock, patch
from isis_monitor.config import AppConfig
from isis_monitor.notifiers import NotificationChannel
from isis_monitor.beam import (
    BeamMonitor,
    PV_TS1_BEAM_CURRENT, PV_TS2_BEAM_CURRENT, PV_MUON_BEAM_CURRENT,
    BEAM_TARGETS,
)


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
    return BeamMonitor(mock_config, beam_channel, exp_channel, counts_target=counts_target)


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
    assert m._get_power_label(120, "TS1") == "high"
    assert m._get_power_label(0, "TS2") == "off"
    assert m._get_power_label(10, "TS2") == "low"
    assert m._get_power_label(15, "TS2") == "medium"
    assert m._get_power_label(30, "TS2") == "high"


# ---------------------------------------------------------------------------
# _handle_update — beam-current arm
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_update_beam(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    await m._handle_update({"pv": PV_TS1_BEAM_CURRENT, "value": "10.0"})
    assert m.state.beams["TS1"].current == 10.0
    assert m.state.beams["TS1"].power == "low"
    beam_channel.broadcast.assert_called_once()
    assert "TS1 Beam is now low" in beam_channel.broadcast.call_args[0][0]

    beam_channel.broadcast.reset_mock()

    # No state change → no broadcast
    await m._handle_update({"pv": PV_TS1_BEAM_CURRENT, "value": "45.0"})
    assert m.state.beams["TS1"].current == 45.0
    assert m.state.beams["TS1"].power == "low"
    beam_channel.broadcast.assert_not_called()

    # State change → broadcast
    await m._handle_update({"pv": PV_TS1_BEAM_CURRENT, "value": "60.0"})
    assert m.state.beams["TS1"].current == 60.0
    assert m.state.beams["TS1"].power == "medium"
    beam_channel.broadcast.assert_called_once()
    assert "TS1 Beam is now medium" in beam_channel.broadcast.call_args[0][0]


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
    assert "new run" in exp_channel.broadcast.call_args[0][0].lower()
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
    assert "about to finish" in exp_channel.broadcast.call_args[0][0]


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
