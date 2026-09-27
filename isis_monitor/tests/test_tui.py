from datetime import datetime, timezone
from unittest.mock import patch

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


def _panel_text(tui, name) -> str:
    from rich.console import Console
    console = Console(width=120, record=True)
    console.print(tui.layout[name].renderable)
    return console.export_text()


# ---------------------------------------------------------------------------
# Initialisation / start() / stop()
# ---------------------------------------------------------------------------

def test_initial_state():
    tui = make_tui(history_maxlen=10)
    assert tui.mcr_news == "Waiting for initial MCR news..."
    for target in ("TS1", "TS2", "Muons"):
        assert tui.beam_states[target] == {"current": 0.0, "power": "unknown"}
        assert tui._history[target].maxlen == 10
        assert len(tui._history[target]) == 0


def test_start_draws_every_panel_and_stop_stops_live():
    tui = make_tui()
    with patch.object(tui.live, "start") as mock_start:
        tui.start()
    mock_start.assert_called_once()
    assert "DISCONNECTED" in _panel_text(tui, "header")
    assert "Waiting for initial MCR news..." in _panel_text(tui, "mcr")
    assert "TS1" in _panel_text(tui, "beam_table")
    assert "0/60 samples" in _panel_text(tui, "beam_graph")
    assert "rolling 60 min" in _panel_text(tui, "beam_graph")
    short = make_tui(history_maxlen=30, sample_interval=3)
    short._update_beam_graph()
    assert "rolling 90s" in _panel_text(short, "beam_graph")
    with patch.object(tui.live, "stop") as mock_stop:
        tui.stop()
    mock_stop.assert_called_once()


# ---------------------------------------------------------------------------
# update_beam_state() — must NOT write to history
# ---------------------------------------------------------------------------

class TestUpdateBeamState:
    def test_updates_known_beam_and_its_row(self):
        tui = make_tui()
        tui.update_beam_state("TS1", 123.456, "high")
        assert tui.beam_states["TS1"] == {"current": 123.456, "power": "high"}
        text = _panel_text(tui, "beam_table")
        assert "123.456" in text and "HIGH" in text
        assert tui.last_update.strftime("%H:%M:%S") in text  # "Last Update" in the title

    def test_ignores_unknown_beam_target(self):
        tui = make_tui()
        original_states = dict(tui.beam_states)
        tui.update_beam_state("UnknownBeam", 50.0, "high")
        assert tui.beam_states == original_states

    def test_does_not_write_to_history(self):
        """History comes only from the daemon's sample events, not update_beam_state."""
        tui = make_tui()
        tui.update_beam_state("TS1", 99.0, "high")
        tui.update_beam_state("TS1", 88.0, "high")
        assert len(tui._history["TS1"]) == 0


# ---------------------------------------------------------------------------
# Sparklines
# ---------------------------------------------------------------------------

class TestSparklineChars:
    def test_empty_values_returns_spaces(self):
        assert sparkline_chars([], 10) == " " * 10

    def test_left_padded_or_truncated_to_width(self):
        result = sparkline_chars([1.0, 2.0, 3.0], 10)
        assert len(result) == 10
        assert result.startswith("       ")   # 7 leading spaces
        assert len(sparkline_chars(list(range(20)), 10)) == 10

    def test_all_zero_renders_as_flat_baseline(self):
        assert sparkline_chars([0.0] * 5, 5).strip() == ""

    def test_max_value_uses_full_block(self):
        from isis_monitor.tui import _BLOCKS
        result = sparkline_chars([0.0, 100.0], 2)
        assert _BLOCKS[-1] in result


class TestRenderSparkline:
    def test_empty_values_returns_spaces(self):
        assert _render_sparkline([], 10).plain == " " * 10

    def test_matches_sparkline_chars_including_padding(self):
        """sparkline_chars and _render_sparkline must select identical characters."""
        values = [1.0, 5.0, 3.0, 8.0, 2.0]
        history_data = [(v, "high") for v in values]
        assert _render_sparkline(history_data, 8).plain == sparkline_chars(values, 8)

    def test_each_block_coloured_by_its_power_state(self):
        result = _render_sparkline(
            [(1.0, "high"), (2.0, "low"), (3.0, "off"), (4.0, "unknown")], 4)
        assert [span.style for span in result.spans] == ["green", "orange", "red", "red"]


# ---------------------------------------------------------------------------
# History fed from the daemon (add_history_sample / set_history_snapshot)
# ---------------------------------------------------------------------------

class TestDaemonHistory:
    def test_add_history_sample_appends_and_redraws(self):
        tui = make_tui()
        ts = datetime.now(timezone.utc)
        tui.add_history_sample("TS1", ts, 12.5, "low")
        assert list(tui._history["TS1"]) == [(ts, 12.5, "low")]
        assert "1/60 samples" in _panel_text(tui, "beam_graph")

    def test_add_history_sample_ignores_unknown_beam(self):
        tui = make_tui()
        tui.add_history_sample("Nope", datetime.now(timezone.utc), 1.0, "low")
        assert all(len(h) == 0 for h in tui._history.values())

    def test_add_history_sample_skips_samples_it_already_has(self):
        """The TUI subscribes before fetching history, so a sample can arrive twice."""
        tui = make_tui()
        t0 = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)
        tui.set_history_snapshot({"TS1": [{"timestamp": t0.isoformat(), "current": 1.0, "power": "low"}]})
        tui.add_history_sample("TS1", t0, 1.0, "low")
        tui.add_history_sample("TS1", t0.replace(minute=1), 2.0, "low")
        assert [v for _, v, _ in tui._history["TS1"]] == [1.0, 2.0]

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
        assert "[CONNECTED]" in _panel_text(tui, "header")


# ---------------------------------------------------------------------------
# MCR news and logs
# ---------------------------------------------------------------------------

def test_update_mcr_news_shows_the_news():
    tui = make_tui()
    tui.update_mcr_news("Reactor at full power")
    assert tui.mcr_news == "Reactor at full power"
    assert "Reactor at full power" in _panel_text(tui, "mcr")


class TestUpdateLog:
    def test_respects_maxlen(self):
        tui = make_tui()
        # default maxlen is 50
        for i in range(60):
            tui.update_log(f"Msg {i}")
        assert len(tui._logs) == 50
        assert tui._logs[0] == "Msg 10"  # 0-9 were evicted
        assert tui._logs[-1] == "Msg 59"

    def test_panel_shows_only_the_latest_logs(self):
        tui = make_tui()
        for i in range(20):
            tui.update_log(f"Log {i}")
        text = _panel_text(tui, "logs")
        assert "Log 19" in text and "Log 6" in text and "Log 5" not in text  # 14 rows fit
        assert text.index("Log 6") < text.index("Log 19")  # oldest first


# ---------------------------------------------------------------------------
# Instruments panel
# ---------------------------------------------------------------------------

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

