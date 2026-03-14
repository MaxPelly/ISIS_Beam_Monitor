import asyncio
from collections import deque
from datetime import datetime
from threading import Lock
from typing import Deque, Tuple

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.table import Table

# Eight Unicode block heights, index 0 = shortest
_BLOCKS = " ▁▂▃▄▅▆▇█"
# Minimum scale ceiling so an all-zero history still renders as flat baseline
_MIN_SCALE = 1.0


def _render_sparkline(
    values: list[float],
    power: str,
    width: int,
) -> Text:
    """Return a Rich Text sparkline for *values*, coloured by *power* state.

    The bar chart is normalised against the rolling maximum of the supplied
    values, so the tallest bar always reaches full height.  When all values
    are zero the line is flat at the baseline.
    """
    colour = "green"
    if power == "off":
        colour = "red"
    elif power in ("low", "unknown"):
        colour = "yellow"

    if not values:
        return Text(" " * width, style=colour)

    scale = max(max(values), _MIN_SCALE)
    # Take the most-recent *width* samples
    tail: list[float] = values[-width:]
    chars = ""
    for v in tail:
        idx = round((v / scale) * (len(_BLOCKS) - 1))
        chars += _BLOCKS[idx]
    # Left-pad with spaces if we have fewer samples than width
    chars = chars.rjust(width)
    return Text(chars, style=colour)


class RichTUI:
    def __init__(
        self,
        history_maxlen: int = 60,
        sample_interval: float = 60.0,
    ):
        self.history_maxlen = history_maxlen
        self.sample_interval = sample_interval

        self.beam_states: dict[str, dict] = {
            "TS1":   {"current": 0.0, "power": "unknown"},
            "TS2":   {"current": 0.0, "power": "unknown"},
            "Muons": {"current": 0.0, "power": "unknown"},
        }
        # Per-target rolling history: deque of (datetime, current_μA)
        self._history: dict[str, Deque[Tuple[datetime, float]]] = {
            beam: deque(maxlen=history_maxlen)
            for beam in self.beam_states
        }

        self.mcr_news = "Waiting for initial MCR news..."
        self.last_update = datetime.now()
        self._lock = Lock()

        self.layout = self._make_layout()
        self.live = Live(self.layout, refresh_per_second=4, screen=True)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _make_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
        )
        layout["main"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="mcr", ratio=1),
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

    async def run_sampler(self, stop_event: asyncio.Event) -> None:
        """Coroutine that snapshots the latest beam currents at a fixed interval.

        This is intentionally decoupled from ``beam.py``'s update rate —
        it wakes every ``sample_interval`` seconds and records whatever the
        most-recently-received values are.  If beam.py has been silent the
        last-known values are repeated, producing a flat line on the graph.
        """
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self.sample_interval,
                )
                # stop_event fired → exit cleanly
                break
            except asyncio.TimeoutError:
                pass  # Normal: interval elapsed, take a sample

            now = datetime.now()
            with self._lock:
                for beam, state in self.beam_states.items():
                    self._history[beam].append((now, state["current"]))
                self._update_beam_graph()

    # ------------------------------------------------------------------
    # Public update API  (called by beam.py / mcr.py threads)
    # ------------------------------------------------------------------

    def update_beam_state(self, beam: str, current: float, power: str):
        """Update the *latest* state of a specific beam target.

        Does NOT write to the history deque — that is handled exclusively by
        :meth:`run_sampler` on its own fixed timer.
        """
        with self._lock:
            if beam in self.beam_states:
                self.beam_states[beam] = {"current": current, "power": power}
            self.last_update = datetime.now()
            self._update_beam_panel()

    def update_mcr_news(self, news: str):
        """Update the MCR news panel."""
        with self._lock:
            self.mcr_news = news
            self.last_update = datetime.now()
            self._update_mcr_panel()

    # ------------------------------------------------------------------
    # Internal render helpers  (must be called while _lock is held)
    # ------------------------------------------------------------------

    def _update_all(self):
        """Force-refresh every panel.  Called once on startup (lock not held)."""
        with self._lock:
            self.layout["header"].update(
                Panel(
                    Text("ISIS Facility Monitor", justify="center", style="bold cyan"),
                    style="blue",
                )
            )
            self._update_beam_panel()
            self._update_beam_graph()
            self._update_mcr_panel()

    def _update_beam_panel(self):
        """Render the current-snapshot table into beam_table."""
        table = Table(show_header=True, header_style="bold magenta", expand=True)
        table.add_column("Beam Target")
        table.add_column("Current (μA)", justify="right")
        table.add_column("Power Level")

        for beam, state in self.beam_states.items():
            power_style = "green"
            if state["power"] == "off":
                power_style = "red"
            elif state["power"] in ("low", "unknown"):
                power_style = "yellow"

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
        # Approximate usable width: panel width minus borders/label prefix.
        # Rich doesn't expose a reliable console width here, so we use a
        # sensible default; the sparkline itself clips to actual history length.
        SPARK_WIDTH = 58
        LABEL_W = 7   # "Muons: " is 7 chars

        content = Text()
        for i, (beam, state) in enumerate(self.beam_states.items()):
            values = [v for _, v in self._history[beam]]
            latest = f"{state['current']:6.1f} μA"

            label = Text(f"{beam:<{LABEL_W}}", style="bold")
            spark = _render_sparkline(values, state["power"], SPARK_WIDTH)
            val   = Text(f" {latest}", style="dim")

            content.append_text(label)
            content.append_text(spark)
            content.append_text(val)
            if i < len(self.beam_states) - 1:
                content.append("\n")

        n = len(next(iter(self._history.values())))
        subtitle = f"{n}/{self.history_maxlen} samples · 1 min/bar · {n} min history"
        self.layout["beam_graph"].update(
            Panel(
                content,
                title="Beam Current — rolling 1 h",
                subtitle=subtitle,
                border_style="cyan",
            )
        )

    def _update_mcr_panel(self):
        self.layout["mcr"].update(
            Panel(
                Text(self.mcr_news, style="white"),
                title="Latest MCR News",
                border_style="cyan",
            )
        )
