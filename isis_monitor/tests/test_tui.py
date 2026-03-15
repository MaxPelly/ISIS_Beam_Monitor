import asyncio
import pytest
from collections import deque
from datetime import datetime
from unittest.mock import patch, MagicMock

from isis_monitor.tui import RichTUI, _render_sparkline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_tui(history_maxlen: int = 60, sample_interval: float = 60.0) -> RichTUI:
    """Return a RichTUI instance with Live.start/stop patched out so no real
    terminal is required."""
    with patch("isis_monitor.tui.Live.start"), patch("isis_monitor.tui.Live.stop"):
        tui = RichTUI(history_maxlen=history_maxlen, sample_interval=sample_interval)
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

    def test_history_deques_initialised(self):
        tui = make_tui(history_maxlen=10)
        for target in ("TS1", "TS2", "Muons"):
            assert target in tui._history
            assert isinstance(tui._history[target], deque)
            assert tui._history[target].maxlen == 10
            assert len(tui._history[target]) == 0

    def test_default_params(self):
        tui = make_tui()
        assert tui.history_maxlen == 60
        assert tui.sample_interval == 60.0

    def test_layout_has_beam_graph_panel(self):
        tui = make_tui()
        # Should not raise KeyError
        _ = tui.layout["beam_graph"]

    def test_layout_has_beam_table_panel(self):
        tui = make_tui()
        _ = tui.layout["beam_table"]


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
# update_beam_state() — must NOT write to history
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
        assert tui.beam_states == original_states

    def test_triggers_beam_panel_update(self):
        tui = make_tui()
        with patch.object(tui, "_update_beam_panel") as mock_panel:
            tui.update_beam_state("TS1", 10.0, "low")
        mock_panel.assert_called_once()

    def test_does_not_write_to_history(self):
        """History must only be written by run_sampler, not by update_beam_state."""
        tui = make_tui()
        with patch.object(tui, "_update_beam_panel"):
            tui.update_beam_state("TS1", 99.0, "high")
            tui.update_beam_state("TS1", 88.0, "high")
        assert len(tui._history["TS1"]) == 0

    def test_updates_last_update_timestamp(self):
        tui = make_tui()
        before = tui.last_update
        with patch.object(tui, "_update_beam_panel"):
            tui.update_beam_state("TS2", 5.0, "low")
        assert tui.last_update >= before


# ---------------------------------------------------------------------------
# History buffer
# ---------------------------------------------------------------------------

class TestHistoryBuffer:
    def test_maxlen_eviction(self):
        tui = make_tui(history_maxlen=3)
        # Manually inject samples into the deque (as run_sampler would)
        for v in [1.0, 2.0, 3.0, 4.0]:
            tui._history["TS1"].append((datetime.now(), v, "high"))
        values = [v for _, v, _ in tui._history["TS1"]]
        assert values == [2.0, 3.0, 4.0]   # oldest evicted

    def test_flat_line_when_silent(self):
        """Repeated snapshots of the same value produce a flat history."""
        tui = make_tui(history_maxlen=5)
        tui.beam_states["TS1"]["current"] = 42.0
        now = datetime.now()
        for _ in range(5):
            tui._history["TS1"].append((now, tui.beam_states["TS1"]["current"], "high"))
        values = [v for _, v, _ in tui._history["TS1"]]
        assert all(v == 42.0 for v in values)

    def test_independent_deques_per_target(self):
        tui = make_tui(history_maxlen=5)
        tui._history["TS1"].append((datetime.now(), 10.0, "high"))
        tui._history["TS2"].append((datetime.now(), 20.0, "high"))
        assert len(tui._history["TS1"]) == 1
        assert len(tui._history["TS2"]) == 1
        assert len(tui._history["Muons"]) == 0


# ---------------------------------------------------------------------------
# _render_sparkline() helper
# ---------------------------------------------------------------------------

class TestRenderSparkline:
    def test_empty_values_returns_spaces(self):
        result = _render_sparkline([], 10)
        assert result.plain == " " * 10

    def test_length_matches_width_when_enough_samples(self):
        values = [(float(i), "high") for i in range(20)]
        result = _render_sparkline(values, 10)
        assert len(result.plain) == 10

    def test_left_padded_when_fewer_samples_than_width(self):
        values = [(1.0, "high"), (2.0, "high"), (3.0, "high")]
        result = _render_sparkline(values, 10)
        assert len(result.plain) == 10
        assert result.plain.startswith("       ")   # 7 leading spaces

    def test_all_zero_renders_as_flat_baseline(self):
        values = [(0.0, "high")] * 5
        result = _render_sparkline(values, 5)
        # All zeros → index 0 → space character (baseline)
        assert result.plain.strip() == ""

    def test_max_value_uses_full_block(self):
        from isis_monitor.tui import _BLOCKS
        values = [(0.0, "high"), (100.0, "high")]
        result = _render_sparkline(values, 2)
        assert _BLOCKS[-1] in result.plain   # tallest bar present

    def test_colour_green_for_high_power(self):
        result = _render_sparkline([(1.0, "high")], 5)
        assert result.spans[-1].style == "green"

    def test_colour_red_for_off_power(self):
        result = _render_sparkline([(1.0, "off")], 5)
        assert result.spans[-1].style == "red"

    def test_colour_yellow_for_unknown(self):
        result = _render_sparkline([(1.0, "unknown")], 5)
        assert result.spans[-1].style == "yellow"

    def test_colour_yellow_for_low(self):
        result = _render_sparkline([(1.0, "low")], 5)
        assert result.spans[-1].style == "yellow"


# ---------------------------------------------------------------------------
# run_sampler()
# ---------------------------------------------------------------------------

class TestRunSampler:
    @pytest.mark.asyncio
    async def test_sampler_appends_to_history(self):
        """After one sample interval the deque gains one entry per target."""
        tui = make_tui(sample_interval=0.05)   # 50 ms for fast test
        tui.beam_states["TS1"]["current"] = 55.0

        stop_event = asyncio.Event()
        task = asyncio.create_task(tui.run_sampler(stop_event))

        await asyncio.sleep(0.12)   # allow ~2 intervals
        stop_event.set()
        await task

        assert len(tui._history["TS1"]) >= 1
        _, value, _ = tui._history["TS1"][-1]
        assert value == 55.0

    @pytest.mark.asyncio
    async def test_sampler_flat_line_when_no_beam_update(self):
        """Values are repeated when beam.py sends no updates (flat line)."""
        tui = make_tui(sample_interval=0.05)
        tui.beam_states["TS2"]["current"] = 77.5

        stop_event = asyncio.Event()
        task = asyncio.create_task(tui.run_sampler(stop_event))
        await asyncio.sleep(0.18)
        stop_event.set()
        await task

        values = [v for _, v, _ in tui._history["TS2"]]
        assert len(values) >= 2
        assert all(v == 77.5 for v in values)

    @pytest.mark.asyncio
    async def test_sampler_stops_on_event(self):
        """run_sampler returns promptly when stop_event is set."""
        tui = make_tui(sample_interval=10.0)   # long interval
        stop_event = asyncio.Event()
        task = asyncio.create_task(tui.run_sampler(stop_event))
        await asyncio.sleep(0.05)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1.0)   # must finish quickly

    @pytest.mark.asyncio
    async def test_sampler_respects_maxlen(self):
        tui = make_tui(history_maxlen=3, sample_interval=0.05)
        stop_event = asyncio.Event()
        task = asyncio.create_task(tui.run_sampler(stop_event))
        await asyncio.sleep(0.30)   # allow > 3 intervals
        stop_event.set()
        await task
        assert len(tui._history["TS1"]) <= 3


# ---------------------------------------------------------------------------
# _update_beam_panel() — structural checks via Rich renderable
# ---------------------------------------------------------------------------

class TestUpdateBeamPanel:
    def test_panel_is_set_on_layout(self):
        from rich.panel import Panel
        tui = make_tui()
        tui.beam_states["TS1"] = {"current": 75.0, "power": "medium"}
        mock_update = MagicMock()
        tui.layout["beam_table"].update = mock_update
        tui._update_beam_panel()
        mock_update.assert_called_once()
        arg = mock_update.call_args[0][0]
        assert isinstance(arg, Panel)

    def test_beam_panel_title_contains_last_update(self):
        from rich.panel import Panel
        tui = make_tui()
        mock_update = MagicMock()
        tui.layout["beam_table"].update = mock_update
        tui._update_beam_panel()
        panel: Panel = mock_update.call_args[0][0]
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
        tui = make_tui()
        before = tui.last_update
        with patch.object(tui, "_update_mcr_panel"):
            tui.update_mcr_news("News update")
        assert tui.last_update >= before


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
        assert isinstance(tui.beam_states["TS1"]["current"], float)
        assert isinstance(tui.mcr_news, str)
        # History must still be empty — only run_sampler writes to it
        assert len(tui._history["TS1"]) == 0

# ---------------------------------------------------------------------------
# update_log() and _update_logs_panel()
# ---------------------------------------------------------------------------

class TestUpdateLog:
    def test_appends_to_deque(self):
        tui = make_tui()
        tui.update_log("Test log massage 1")
        tui.update_log("Test log massage 2")
        assert len(tui._logs) == 2
        assert tui._logs[0] == "Test log massage 1"
        assert tui._logs[1] == "Test log massage 2"

    def test_respects_maxlen(self):
        tui = make_tui()
        # default maxlen is 50
        for i in range(60):
            tui.update_log(f"Msg {i}")
        assert len(tui._logs) == 50
        assert tui._logs[0] == "Msg 10"  # 0-9 were evicted
        assert tui._logs[-1] == "Msg 59"

    def test_triggers_logs_panel_update(self):
        tui = make_tui()
        with patch.object(tui, "_update_logs_panel") as mock_panel:
            tui.update_log("New log")
        mock_panel.assert_called_once()

    def test_updates_last_update_timestamp(self):
        tui = make_tui()
        before = tui.last_update
        with patch.object(tui, "_update_logs_panel"):
            tui.update_log("Another log")
        assert tui.last_update >= before

class TestUpdateLogsPanel:
    def test_panel_is_set_on_layout(self):
        from rich.panel import Panel
        tui = make_tui()
        mock_update = MagicMock()
        tui.layout["logs"].update = mock_update
        tui._update_logs_panel()
        mock_update.assert_called_once()
        arg = mock_update.call_args[0][0]
        assert isinstance(arg, Panel)

    def test_logs_panel_contains_joined_text(self):
        from rich.panel import Panel
        tui = make_tui()
        tui._logs.extend(["Log 1", "Log 2"])
        mock_update = MagicMock()
        tui.layout["logs"].update = mock_update
        tui._update_logs_panel()
        panel: Panel = mock_update.call_args[0][0]
        # Text block should contain the joined strings
        assert "Log 1\nLog 2" in panel.renderable.plain
