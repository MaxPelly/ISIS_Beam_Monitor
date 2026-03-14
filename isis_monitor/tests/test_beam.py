import pytest
from unittest.mock import AsyncMock, patch
from isis_monitor.config import AppConfig
from isis_monitor.notifiers import NotificationChannel
from isis_monitor.beam import BeamMonitor, PV_TS1_BEAM_CURRENT, PV_TS2_BEAM_CURRENT, PV_MUON_BEAM_CURRENT

@pytest.fixture
def mock_config():
    return AppConfig(
        mcr_news_url="",
        isis_websocket_url="wss://test",
        news_teams_url="",
        beam_teams_url="",
        experiment_teams_url=""
    )

@pytest.fixture
def mock_channels():
    beam_channel = NotificationChannel("Beam")
    beam_channel.broadcast = AsyncMock()
    
    exp_channel = NotificationChannel("Exp")
    exp_channel.broadcast = AsyncMock()
    return beam_channel, exp_channel

def test_safe_float(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    monitor = BeamMonitor(mock_config, beam_channel, exp_channel, counts_target=100)
    
    assert monitor._safe_float("123.4") == 123.4
    assert monitor._safe_float(123.4) == 123.4
    assert monitor._safe_float("NaN") == 0.0
    assert monitor._safe_float("nan") == 0.0
    assert monitor._safe_float("bad_string") == 0.0
    assert monitor._safe_float(None) == 0.0

def test_get_power_label(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    monitor = BeamMonitor(mock_config, beam_channel, exp_channel, counts_target=100)
    
    assert monitor._get_power_label(-5, "TS1") == "off"
    assert monitor._get_power_label(0, "TS1") == "off"
    assert monitor._get_power_label(20, "TS1") == "low"
    assert monitor._get_power_label(75, "TS1") == "medium"
    assert monitor._get_power_label(120, "TS1") == "high"
    
    assert monitor._get_power_label(0, "TS2") == "off"
    assert monitor._get_power_label(10, "TS2") == "low"
    assert monitor._get_power_label(15, "TS2") == "medium"
    assert monitor._get_power_label(30, "TS2") == "high"

@pytest.mark.asyncio
async def test_handle_update_beam(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    monitor = BeamMonitor(mock_config, beam_channel, exp_channel, counts_target=100)
    
    # Send TS1 low power message
    await monitor._handle_update({"pv": PV_TS1_BEAM_CURRENT, "value": "10.0"})
    
    assert monitor.state.TS1_beam_current == 10.0
    assert monitor.state.TS1_beam_power_state == "low"
    beam_channel.broadcast.assert_called_once()
    assert "TS1 Beam is now low" in beam_channel.broadcast.call_args[0][0]
    
    beam_channel.broadcast.reset_mock()
    
    # Send TS1 still low power message (should NOT broadcast)
    await monitor._handle_update({"pv": PV_TS1_BEAM_CURRENT, "value": "45.0"})
    assert monitor.state.TS1_beam_current == 45.0
    assert monitor.state.TS1_beam_power_state == "low"
    beam_channel.broadcast.assert_not_called()
    
    # Send TS1 medium power message (SHOULD broadcast)
    await monitor._handle_update({"pv": PV_TS1_BEAM_CURRENT, "value": "60.0"})
    assert monitor.state.TS1_beam_current == 60.0
    assert monitor.state.TS1_beam_power_state == "medium"
    beam_channel.broadcast.assert_called_once()
    assert "TS1 Beam is now medium" in beam_channel.broadcast.call_args[0][0]
