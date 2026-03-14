import pytest
from unittest.mock import patch, MagicMock, call
from isis_monitor.tui import RichTUI


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_tui() -> RichTUI:
    """Return a RichTUI instance with Live.start/stop patched out so no real
    terminal is required."""
    with patch("isis_monitor.tui.Live.start"), patch("isis_monitor.tui.Live.stop"):
        tui = RichTUI()
    return tui


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

class TestInit:
    def test_initial_mcr_news(self):
        tui = make_tui()
        assert tui.mcr_news == "Waiting for initial MCR news..."

    def test_initial_beam_states(self):
        tui = make_tui()
        for target in ("TS1", "TS2", "Muons"):
            assert tui.beam_states[target]["current"] == 0.0
            assert tui.beam_states[target]["power"] == "unknown"

    def test_lock_exists(self):
        tui = make_tui()
        assert tui._lock is not None


# ---------------------------------------------------------------------------
# start() / stop()
# ---------------------------------------------------------------------------

class TestStartStop:
    def test_start_calls_live_start(self):
        tui = make_tui()
        with patch.object(tui.live, "start") as mock_start, \
             patch.object(tui, "_update_all"):
            tui.start()
            mock_start.assert_called_once()

    def test_start_calls_update_all(self):
        tui = make_tui()
        with patch.object(tui.live, "start"), \
             patch.object(tui, "_update_all") as mock_update:
            tui.start()
            mock_update.assert_called_once()

    def test_stop_calls_live_stop(self):
        tui = make_tui()
        with patch.object(tui.live, "stop") as mock_stop:
            tui.stop()
            mock_stop.assert_called_once()


# ---------------------------------------------------------------------------
# update_beam_state()
# ---------------------------------------------------------------------------

class TestUpdateBeamState:
    def test_updates_known_beam(self):
        tui = make_tui()
        with patch.object(tui, "_update_beam_panel"):
            tui.update_beam_state("TS1", 123.456, "high")
        assert tui.beam_states["TS1"] == {"current": 123.456, "power": "high"}

    def test_updates_all_three_targets(self):
        tui = make_tui()
        with patch.object(tui, "_update_beam_panel"):
            tui.update_beam_state("TS1", 100.0, "high")
            tui.update_beam_state("TS2", 20.0, "medium")
            tui.update_beam_state("Muons", 0.0, "off")
        assert tui.beam_states["TS1"]["power"] == "high"
        assert tui.beam_states["TS2"]["power"] == "medium"
        assert tui.beam_states["Muons"]["power"] == "off"

    def test_ignores_unknown_beam_target(self):
        tui = make_tui()
        original_states = dict(tui.beam_states)
        with patch.object(tui, "_update_beam_panel"):
            tui.update_beam_state("UnknownBeam", 50.0, "high")
        # beam_states dict should be unchanged
        assert tui.beam_states == original_states

    def test_triggers_beam_panel_update(self):
        tui = make_tui()
        with patch.object(tui, "_update_beam_panel") as mock_panel:
            tui.update_beam_state("TS1", 10.0, "low")
        mock_panel.assert_called_once()

    def test_updates_last_update_timestamp(self):
        from datetime import datetime
        tui = make_tui()
        before = tui.last_update
        with patch.object(tui, "_update_beam_panel"):
            tui.update_beam_state("TS2", 5.0, "low")
        assert tui.last_update >= before


# ---------------------------------------------------------------------------
# update_mcr_news()
# ---------------------------------------------------------------------------

class TestUpdateMcrNews:
    def test_updates_news_text(self):
        tui = make_tui()
        with patch.object(tui, "_update_mcr_panel"):
            tui.update_mcr_news("Reactor at full power")
        assert tui.mcr_news == "Reactor at full power"

    def test_triggers_mcr_panel_update(self):
        tui = make_tui()
        with patch.object(tui, "_update_mcr_panel") as mock_panel:
            tui.update_mcr_news("Some news")
        mock_panel.assert_called_once()

    def test_updates_last_update_timestamp(self):
        from datetime import datetime
        tui = make_tui()
        before = tui.last_update
        with patch.object(tui, "_update_mcr_panel"):
            tui.update_mcr_news("News update")
        assert tui.last_update >= before


# ---------------------------------------------------------------------------
# _update_beam_panel() — structural checks via Rich renderable
# ---------------------------------------------------------------------------

class TestUpdateBeamPanel:
    def test_panel_is_set_on_layout(self):
        from rich.panel import Panel
        tui = make_tui()
        tui.beam_states["TS1"] = {"current": 75.0, "power": "medium"}
        mock_update = MagicMock()
        tui.layout["beam"].update = mock_update
        tui._update_beam_panel()
        mock_update.assert_called_once()
        arg = mock_update.call_args[0][0]
        assert isinstance(arg, Panel)

    def test_beam_panel_title_contains_last_update(self):
        from rich.panel import Panel
        tui = make_tui()
        mock_update = MagicMock()
        tui.layout["beam"].update = mock_update
        tui._update_beam_panel()
        panel: Panel = mock_update.call_args[0][0]
        # The panel title includes the timestamp from last_update
        expected_time = tui.last_update.strftime("%H:%M:%S")
        assert expected_time in panel.title


# ---------------------------------------------------------------------------
# _update_mcr_panel() — structural checks
# ---------------------------------------------------------------------------

class TestUpdateMcrPanel:
    def test_panel_is_set_on_layout(self):
        from rich.panel import Panel
        tui = make_tui()
        mock_update = MagicMock()
        tui.layout["mcr"].update = mock_update
        tui._update_mcr_panel()
        mock_update.assert_called_once()
        arg = mock_update.call_args[0][0]
        assert isinstance(arg, Panel)

    def test_mcr_panel_title(self):
        from rich.panel import Panel
        tui = make_tui()
        mock_update = MagicMock()
        tui.layout["mcr"].update = mock_update
        tui._update_mcr_panel()
        panel: Panel = mock_update.call_args[0][0]
        assert panel.title == "Latest MCR News"


# ---------------------------------------------------------------------------
# Thread-safety (smoke test)
# ---------------------------------------------------------------------------

class TestThreadSafety:
    def test_concurrent_updates_do_not_raise(self):
        """Fire beam and MCR updates from multiple threads and verify no
        exceptions are raised and the final state is self-consistent."""
        import threading
        tui = make_tui()
        errors = []

        def do_beam():
            try:
                for i in range(50):
                    with patch.object(tui, "_update_beam_panel"):
                        tui.update_beam_state("TS1", float(i), "high")
            except Exception as e:
                errors.append(e)

        def do_mcr():
            try:
                for i in range(50):
                    with patch.object(tui, "_update_mcr_panel"):
                        tui.update_mcr_news(f"News {i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=do_beam),
                   threading.Thread(target=do_mcr)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Exceptions in threads: {errors}"
        # Final state must be internally consistent
        assert isinstance(tui.beam_states["TS1"]["current"], float)
        assert isinstance(tui.mcr_news, str)
