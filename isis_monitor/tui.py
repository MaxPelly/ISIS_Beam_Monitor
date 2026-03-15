import asyncio
from collections import deque
from datetime import datetime, timezone
from threading import RLock
from typing import Deque, Tuple

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.table import Table

# Eight Unicode block heights, index 0 = shortest
_BLOCKS = " ▁▂▃▄▅▆▇█"


def _render_sparkline(
    history_data: list[Tuple[float, str]],
    width: int,
) -> Text:
    """Return a Rich Text sparkline, colouring each block by its historical state.

    The bar chart is min-max normalised against the current data in the deque.
    """
    text = Text()
    if not history_data:
        text.append(" " * width)
        return text

    # Extract just the values to calculate our dynamic range
    values = [v for v, _ in history_data]
    tail = history_data[-width:]
    
    min_val = min(values)
    max_val = max(values)
    span = max_val - min_val

    # Left-pad with spaces if we have fewer samples than width
    pad_len = width - len(tail)
    if pad_len > 0:
        text.append(" " * pad_len)

    # Build the sparkline character by character
    for v, power in tail:
        # Determine historical colour
        colour = "green"
        if power == "off":
            colour = "red"
        elif power in ("low", "unknown"):
            colour = "yellow"

        # Determine block height
        if span == 0:
            block_idx = 0 if max_val == 0 else len(_BLOCKS) // 2
            char = _BLOCKS[block_idx]
        else:
            norm = (v - min_val) / span
            # only use empty block for 0
            if min_val == 0:
                idx = round(norm * (len(_BLOCKS) - 1))
            else:
                idx = round(norm * (len(_BLOCKS) - 2)) + 1 
            char = _BLOCKS[idx]
            
        text.append(char, style=colour)

    return text


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
        self.refresh_per_second = refresh_per_second
        self.logs_maxlen = logs_maxlen

        self.beam_states: dict[str, dict] = {
            "TS1":   {"current": 0.0, "power": "unknown"},
            "TS2":   {"current": 0.0, "power": "unknown"},
            "Muons": {"current": 0.0, "power": "unknown"},
        }
        # Per-target rolling history: deque of (datetime, current_μA, power_state)
        self._history: dict[str, Deque[Tuple[datetime, float, str]]] = {
            beam: deque(maxlen=history_maxlen)
            for beam in self.beam_states
        }

        self.mcr_news = "Waiting for initial MCR news..."
        self._logs: Deque[str] = deque(maxlen=self.logs_maxlen)
        self.last_update = datetime.now(timezone.utc)
        self._lock = RLock()

        self.layout = self._make_layout()
        self.live = Live(self.layout, refresh_per_second=self.refresh_per_second, screen=True)

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

        This is intentionally decoupled from ``beam.py``'s update rate --
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

            now = datetime.now(timezone.utc)
            with self._lock:
                for beam, state in self.beam_states.items():
                    self._history[beam].append((now, state["current"], state["power"]))
                self._update_beam_graph()

    # ------------------------------------------------------------------
    # Public update API  (called by beam.py / mcr.py threads)
    # ------------------------------------------------------------------

    def update_beam_state(self, beam: str, current: float, power: str):
        """Update the *latest* state of a specific beam target.

        Does NOT write to the history deque -- that is handled exclusively by
        :meth:`run_sampler` on its own fixed timer.
        """
        with self._lock:
            if beam in self.beam_states:
                self.beam_states[beam] = {"current": current, "power": power}
            self.last_update = datetime.now(timezone.utc)
            self._update_beam_panel()

    def update_mcr_news(self, news: str):
        """Update the MCR news panel."""
        with self._lock:
            self.mcr_news = news
            self.last_update = datetime.now(timezone.utc)
            self._update_mcr_panel()

    def update_log(self, message: str):
        """Append a log message to the log history."""
        with self._lock:
            self._logs.append(message)
            self.last_update = datetime.now(timezone.utc)
            self._update_logs_panel()

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
            self._update_logs_panel()

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
        SPARK_WIDTH = 58
        LABEL_W = 7   # "Muons: " is 7 chars

        content = Text()
        for i, (beam, state) in enumerate(self.beam_states.items()):
            # Extract both current and historical power state
            history_data = [(v, p) for _, v, p in self._history[beam]]
            latest = f"{state['current']:6.1f} μA"

            label = Text(f"{beam:<{LABEL_W}}", style="bold")
            spark = _render_sparkline(history_data, SPARK_WIDTH)
            val   = Text(f" {latest}", style="dim")

            content.append_text(label)
            content.append_text(spark)
            content.append_text(val)
            if i < len(self.beam_states) - 1:
                content.append("\n")

        n = len(next(iter(self._history.values())))
        interval_s = self.sample_interval
        bar_label = (
            f"{interval_s:.0f}s/bar" if interval_s < 60
            else f"{interval_s / 60:.0f} min/bar"
        )
        history_label = (
            f"{n * interval_s:.0f}s history" if interval_s < 60
            else f"{n * interval_s / 60:.0f} min history"
        )
        subtitle = f"{n}/{self.history_maxlen} samples · {bar_label} · {history_label}"
        self.layout["beam_graph"].update(
            Panel(
                content,
                title="Beam Current -- rolling 1 h",
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

    def _update_logs_panel(self):
        # Only show the latest few logs that fit in the panel height (split size 8)
        # NOTE: caller must hold self._lock (consistent with all other _update_* helpers)
        logs_to_show = list(self._logs)[-7:]
        log_text = "\n".join(logs_to_show)
        self.layout["logs"].update(
            Panel(
                Text(log_text, style="dim", no_wrap=False),
                title="System Logs",
                border_style="cyan",
            )
        )
