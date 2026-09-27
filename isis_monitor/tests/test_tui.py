from collections import deque
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from isis_monitor.tui import RichTUI, _progress_bar, _render_sparkline, sparkline_chars


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
        """History comes only from the daemon's sample events, not update_beam_state."""
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
        # Manually inject samples into the deque
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

class TestSparklineChars:
    def test_empty_values_returns_spaces(self):
        assert sparkline_chars([], 10) == " " * 10

    def test_length_matches_width_when_enough_samples(self):
        result = sparkline_chars(list(range(20)), 10)
        assert len(result) == 10

    def test_left_padded_when_fewer_samples_than_width(self):
        result = sparkline_chars([1.0, 2.0, 3.0], 10)
        assert len(result) == 10
        assert result.startswith("       ")   # 7 leading spaces

    def test_all_zero_renders_as_flat_baseline(self):
        assert sparkline_chars([0.0] * 5, 5).strip() == ""

    def test_max_value_uses_full_block(self):
        from isis_monitor.tui import _BLOCKS
        result = sparkline_chars([0.0, 100.0], 2)
        assert _BLOCKS[-1] in result

    def test_matches_render_sparkline_block_characters(self):
        """sparkline_chars and _render_sparkline must select identical characters."""
        values = [1.0, 5.0, 3.0, 8.0, 2.0]
        history_data = [(v, "high") for v in values]
        assert _render_sparkline(history_data, 5).plain == sparkline_chars(values, 5)


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
        assert result.spans[-1].style == "red"

    def test_colour_yellow_for_low(self):
        result = _render_sparkline([(1.0, "low")], 5)
        assert result.spans[-1].style == "orange"


# ---------------------------------------------------------------------------
# History fed from the daemon (add_history_sample / set_history_snapshot)
# ---------------------------------------------------------------------------

class TestDaemonHistory:
    def test_add_history_sample_appends_and_redraws(self):
        tui = make_tui()
        ts = datetime.now(timezone.utc)
        with patch.object(tui, "_update_beam_graph") as graph:
            tui.add_history_sample("TS1", ts, 12.5, "low")
        assert list(tui._history["TS1"]) == [(ts, 12.5, "low")]
        graph.assert_called_once()

    def test_add_history_sample_ignores_unknown_beam(self):
        tui = make_tui()
        tui.add_history_sample("Nope", datetime.now(timezone.utc), 1.0, "low")
        assert all(len(h) == 0 for h in tui._history.values())

    def test_set_history_snapshot_replaces_existing_history(self):
        tui = make_tui(history_maxlen=2)
        tui._history["TS2"].append((datetime.now(timezone.utc), 99.0, "high"))
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [
            {"timestamp": (ts.replace(minute=m)).isoformat(), "current": float(m), "power": "low"}
            for m in range(3)
        ]
        tui.set_history_snapshot({"TS1": rows, "Unknown": rows})
        assert [v for _, v, _ in tui._history["TS1"]] == [1.0, 2.0]  # maxlen keeps the newest
        assert len(tui._history["TS2"]) == 0
        assert tui._history["TS1"][0][0] == ts.replace(minute=1)

    def test_update_connection_state_uppercases_and_redraws_header(self):
        tui = make_tui()
        tui.update_connection_state("connected")
        assert tui.connection_state == "CONNECTED"
        header = tui.layout["header"].renderable
        assert "CONNECTED" in header.renderable.plain

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


# ---------------------------------------------------------------------------
# Instruments panel
# ---------------------------------------------------------------------------

def _panel_text(tui, name) -> str:
    from rich.console import Console
    console = Console(width=120, record=True)
    console.print(tui.layout[name].renderable)
    return console.export_text()


INSTRUMENTS = {
    "PEARL": {"run_name": "Pearl run", "counts": 65.0, "total_runs": 3, "notify_counts": 130.0, "beam_target": "TS1"},
    "EMU": {"run_name": "", "counts": -1.0, "total_runs": 0, "notify_counts": 10.0, "beam_target": "Muon"},
}


class TestInstrumentsPanel:
    def test_set_instruments_renders_a_row_each_and_resizes(self):
        tui = make_tui()
        tui.set_instruments(INSTRUMENTS)
        text = _panel_text(tui, "instruments")
        assert "Pearl run" in text and "████░░░░ 65/130" in text
        assert "EMU" in text and "—/10" in text
        assert tui.layout["instruments"].size == 6

    def test_many_instruments_are_capped_with_a_more_row(self):
        tui = make_tui()
        tui.set_instruments({f"I{n}": dict(INSTRUMENTS["PEARL"]) for n in range(11)})
        text = _panel_text(tui, "instruments")
        assert "I7" in text and "I8" not in text and "+3 more" in text
        assert tui.layout["instruments"].size == 4 + 8 + 1

    def test_set_instruments_copies_input(self):
        tui = make_tui()
        source = {"PEARL": dict(INSTRUMENTS["PEARL"])}
        tui.set_instruments(source)
        source["PEARL"]["counts"] = 0.0
        assert tui.instruments["PEARL"]["counts"] == 65.0

    def test_update_instrument_changes_known_and_ignores_unknown(self):
        tui = make_tui()
        tui.set_instruments(INSTRUMENTS)
        tui.update_instrument("PEARL", run_name="Next run", counts=0.0)
        tui.update_instrument("MERLIN", counts=5.0)
        assert tui.instruments["PEARL"]["run_name"] == "Next run"
        assert "MERLIN" not in tui.instruments
        assert "Next run" in _panel_text(tui, "instruments")

    def test_beam_column_uses_the_targets_power_colour(self):
        tui = make_tui()
        tui.set_instruments(INSTRUMENTS)
        tui.update_beam_state("Muons", 1.0, "off")
        table = tui.layout["instruments"].renderable.renderable
        emu_beam = table.columns[1]._cells[1]
        assert (emu_beam.plain, emu_beam.style) == ("Muon", "red")


class TestProgressBar:
    def test_no_counts_yet_is_a_dash(self):
        assert _progress_bar(-1.0, 100.0).plain == "········ —/100"
        assert _progress_bar(5.0, 0.0).plain == "········ —/0"

    def test_partial_and_capped_at_full(self):
        assert _progress_bar(25.0, 100.0).plain == "██░░░░░░ 25/100"
        assert _progress_bar(150.0, 100.0).plain == "████████ 150/100"


def test_add_history_sample_skips_samples_it_already_has():
    """The TUI subscribes before fetching history, so a sample can arrive twice."""
    tui = make_tui()
    t0 = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)
    tui.set_history_snapshot({"TS1": [{"timestamp": t0.isoformat(), "current": 1.0, "power": "low"}]})
    tui.add_history_sample("TS1", t0, 1.0, "low")
    tui.add_history_sample("TS1", t0.replace(minute=1), 2.0, "low")
    assert [v for _, v, _ in tui._history["TS1"]] == [1.0, 2.0]
