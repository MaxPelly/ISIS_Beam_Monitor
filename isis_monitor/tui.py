import shutil
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Tuple

from rich import box
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.table import Table

from isis_monitor.config import CHANNEL_LABELS, TARGET_LABELS


_STATE_COLOURS = {
    "off": "red",
    "unknown": "red",
    "low": "orange",
    "medium": "yellow",
    "high": "green"
}

def _get_state_colour(state):
    return _STATE_COLOURS.get(state, "purple")

_PROGRESS_WIDTH = 8
# Beyond this the panel shows "+N more", so the MCR news panel keeps its space.
_MAX_INSTRUMENT_ROWS = 8


def _progress_bar(counts: float, target: float) -> Text:
    """A fixed-width bar of counts collected towards the notify count, then
    e.g. "65/130"."""
    if counts < 0 or target <= 0:
        return Text(f"{'·' * _PROGRESS_WIDTH} —/{target:.0f}", style="dim")
    fraction = min(counts / target, 1.0)
    filled = round(fraction * _PROGRESS_WIDTH)
    bar = Text("█" * filled, style="green" if fraction >= 1.0 else "cyan")
    bar.append("░" * (_PROGRESS_WIDTH - filled), style="dim")
    bar.append(f" {counts:.0f}/{target:.0f}")
    return bar


# Eight Unicode block heights, index 0 = shortest
_BLOCKS = " ▁▂▃▄▅▆▇█"


def sparkline_chars(values: list[float], width: int) -> str:
    """Return a plain-text sparkline of `width` block characters, min-max normalised.

    Shared between the TUI (which colours each block) and the daily summary
    (which shows it as plain text in a notification card).
    """
    if not values:
        return " " * width

    tail = values[-width:]
    min_val = min(tail)
    max_val = max(tail)
    span = max_val - min_val

    pad_len = width - len(tail)
    chars = []
    for v in tail:
        if span == 0:
            idx = 0 if max_val == 0 else len(_BLOCKS) // 2
        else:
            norm = (v - min_val) / span
            # only use empty block for 0
            if min_val == 0:
                idx = round(norm * (len(_BLOCKS) - 1))
            else:
                idx = round(norm * (len(_BLOCKS) - 2)) + 1
        chars.append(_BLOCKS[idx])

    return (" " * pad_len) + "".join(chars)


def _render_sparkline(
    history_data: list[Tuple[float, str]],
    width: int,
) -> Text:
    """Return a Rich Text sparkline, colouring each block by its historical state.

    The bar chart is min-max normalised against the current data in the deque.
    """
    tail = history_data[-width:]
    pad_len = width - len(tail)

    chars = sparkline_chars([v for v, _ in tail], width)
    text = Text(chars[:pad_len])
    for char, (_, power) in zip(chars[pad_len:], tail):
        text.append(char, style=_get_state_colour(power))

    return text


def _fmt_window(seconds: float) -> str:
    """A history window's length, in the unit that suits its size."""
    if seconds < 120:
        return f"{seconds:.0f}s"
    return f"{round(seconds / 60, 1):g} min" if seconds < 3600 else f"{round(seconds / 3600, 1):g} h"


class RichTUI:
    def __init__(
        self,
        history_maxlen: int = 60,
        sample_interval: float = 60.0,
        refresh_per_second: int = 4,
        logs_maxlen: int = 50,
    ):
        self.history_maxlen = history_maxlen
        self.sample_interval = sample_interval

        self.beam_states: dict[str, dict] = {
            beam: {"current": 0.0, "power": "unknown"} for beam in CHANNEL_LABELS
        }
        # Per-target rolling history: deque of (datetime, current_μA, power_state)
        self._history: dict[str, Deque[Tuple[datetime, float, str]]] = {
            beam: deque(maxlen=history_maxlen)
            for beam in self.beam_states
        }

        # name -> {"run_name", "counts", "notify_counts", "beam_target", ...}
        self.instruments: dict[str, dict] = {}

        self.mcr_news = "Waiting for initial MCR news..."
        self._logs: Deque[str] = deque(maxlen=logs_maxlen)
        self.last_update = datetime.now(timezone.utc)
        self.connection_state = "DISCONNECTED"

        self.layout = self._make_layout()
        self.live = Live(self.layout, refresh_per_second=refresh_per_second, screen=True)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _make_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="logs", size=16),
        )
        layout["main"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="right", ratio=1),
        )
        layout["right"].split_column(
            Layout(name="instruments", size=self._instruments_panel_height()),
            Layout(name="mcr"),
        )
        layout["left"].split_column(
            Layout(name="beam_table", size=10),
            Layout(name="beam_graph"),
        )
        return layout

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start the live TUI display."""
        self.live.start()
        self._update_all()

    def stop(self):
        """Stop the live TUI display."""
        self.live.stop()

    # ------------------------------------------------------------------
    # Public update API  (called from main.py's IPC event handler)
    # ------------------------------------------------------------------

    def update_beam_state(self, beam: str, current: float, power: str):
        """Update the latest state of a beam target (history comes from the
        daemon's sample events, not from here)."""
        if beam in self.beam_states:
            self.beam_states[beam] = {"current": current, "power": power}
        self.last_update = datetime.now(timezone.utc)
        self._update_beam_panel()
        self._update_instruments_panel()  # the Beam column is coloured by power

    def set_instruments(self, instruments: dict[str, dict]) -> None:
        """Replace every instrument's state, e.g. from a daemon snapshot."""
        self.instruments = {name: dict(info) for name, info in instruments.items()}
        self.layout["instruments"].size = self._instruments_panel_height()
        self._update_instruments_panel()

    def update_instrument(self, name: str, **fields) -> None:
        """Update some of one instrument's state, e.g. run_name or counts."""
        if name not in self.instruments:
            return
        self.instruments[name].update(fields)
        self.last_update = datetime.now(timezone.utc)
        self._update_instruments_panel()

    def update_mcr_news(self, news: str):
        """Update the MCR news panel."""
        self.mcr_news = news
        self.last_update = datetime.now(timezone.utc)
        self._update_mcr_panel()

    def update_log(self, message: str):
        """Append a log message to the log history."""
        self._logs.append(message)
        self.last_update = datetime.now(timezone.utc)
        self._update_logs_panel()

    # ------------------------------------------------------------------
    # Internal render helpers
    # ------------------------------------------------------------------

    def _update_all(self):
        """Force-refresh every panel."""
        self.layout["header"].update(
            Panel(
                Text.assemble(
                    (f"ISIS Facility Monitor  [{self.connection_state}]", "bold cyan"),
                    ("   r reconnect · c config · q quit", "dim"),
                    justify="center",
                ),
                style="blue",
            )
        )
        self._update_beam_panel()
        self._update_beam_graph()
        self._update_instruments_panel()
        self._update_mcr_panel()
        self._update_logs_panel()

    def _update_beam_panel(self):
        """Render the current-snapshot table into beam_table."""
        table = Table(show_header=True, header_style="bold magenta", expand=True)
        table.add_column("Beam Target")
        table.add_column("Current (μA)", justify="right")
        table.add_column("Power Level")

        for beam, state in self.beam_states.items():
            power_style = _get_state_colour(state["power"])

            table.add_row(
                beam,
                f"{state['current']:.3f}",
                f"[{power_style}]{str(state['power']).upper()}[/]",
            )

        time_str = self.last_update.strftime("%H:%M:%S")
        self.layout["beam_table"].update(
            Panel(
                table,
                title=f"Beam Status (Last Update: {time_str})",
                border_style="cyan",
            )
        )

    def _update_beam_graph(self):
        """Render the rolling sparkline graph into beam_graph."""
        # Approximate usable width: terminal width minus half for layout split, minus borders/padding and label.
        term_width = shutil.get_terminal_size((120, 24)).columns
        SPARK_WIDTH = max(10, (term_width // 2) - 30)
        LABEL_W = 7   # "Muons: " is 7 chars

        content = Text()
        for i, (beam, state) in enumerate(self.beam_states.items()):
            if i:
                content.append("\n")
            content.append(f"{beam:<{LABEL_W}}", style="bold")
            content.append_text(_render_sparkline([(v, p) for _, v, p in self._history[beam]], SPARK_WIDTH))
            content.append(f" {state['current']:6.1f} μA", style="dim")

        n = len(next(iter(self._history.values())))
        interval_s = self.sample_interval
        subtitle = f"{n}/{self.history_maxlen} samples · {_fmt_window(interval_s)}/bar · {_fmt_window(n * interval_s)} history"
        self.layout["beam_graph"].update(
            Panel(
                content,
                title=f"Beam Current -- rolling {_fmt_window(self.history_maxlen * interval_s)}",
                subtitle=subtitle,
                border_style="cyan",
            )
        )

    def _instruments_panel_height(self) -> int:
        # panel borders (2) + table header and its rule (2) + one line per
        # shown instrument, plus one for "+N more"
        n = len(self.instruments)
        if n > _MAX_INSTRUMENT_ROWS:
            return 4 + _MAX_INSTRUMENT_ROWS + 1
        return 4 + max(n, 1)

    def _update_instruments_panel(self):
        # SIMPLE_HEAD (no outer border) leaves the run name as much room as possible.
        table = Table(show_header=True, header_style="bold magenta", expand=True, box=box.SIMPLE_HEAD,
                      pad_edge=False, show_edge=False, collapse_padding=True)
        table.add_column("Name", no_wrap=True)
        table.add_column("Beam", no_wrap=True)
        table.add_column("Run", overflow="ellipsis", no_wrap=True, ratio=1)
        table.add_column("µA·h", no_wrap=True)

        shown = list(self.instruments.items())[:_MAX_INSTRUMENT_ROWS]
        for name, info in shown:
            target = str(info.get("beam_target", ""))
            beam = self.beam_states.get(TARGET_LABELS.get(target, target), {})
            table.add_row(
                name,
                Text(target, style=_get_state_colour(beam.get("power", "unknown"))),
                str(info.get("run_name", "")) or "—",
                _progress_bar(float(info.get("counts", -1.0)), float(info.get("notify_counts", 0.0))),
            )
        hidden = len(self.instruments) - len(shown)
        if hidden:
            table.add_row(Text(f"+{hidden} more", style="dim"), "", "", "")

        self.layout["instruments"].update(
            Panel(table, title="Instruments", border_style="cyan")
        )

    def _update_mcr_panel(self):
        self.layout["mcr"].update(
            Panel(
                Text(self.mcr_news, style="white"),
                title="Latest MCR News",
                border_style="cyan",
            )
        )

    def add_history_sample(self, beam: str, timestamp: datetime, current: float, power: str) -> None:
        history = self._history.get(beam)
        # Skip samples already in the history (the TUI subscribes before
        # fetching the history snapshot, so one can arrive both ways).
        if history is not None and not (history and timestamp <= history[-1][0]):
            history.append((timestamp, current, power))
        self.last_update = datetime.now(timezone.utc)
        self._update_beam_graph()

    def set_history_snapshot(self, history: dict[str, list[dict]]) -> None:
        for samples in self._history.values():
            samples.clear()
        for beam, rows in history.items():
            if (samples := self._history.get(beam)) is not None:
                samples.extend(
                    (datetime.fromisoformat(str(row["timestamp"])), float(row["current"]), str(row["power"]))
                    for row in rows
                )
        self._update_beam_graph()

    def update_connection_state(self, state: str) -> None:
        self.connection_state = state.upper()
        self._update_all()

    def _update_logs_panel(self):
        # The panel has 14 rows (layout size 16, less borders), and a log entry
        # can span several lines; long lines are cut rather than wrapped, so
        # the newest lines always fit.
        log_text = "\n".join("\n".join(self._logs).splitlines()[-14:])
        self.layout["logs"].update(
            Panel(
                Text(log_text, style="dim", no_wrap=True, overflow="ellipsis"),
                title="System Logs",
                border_style="cyan",
            )
        )
