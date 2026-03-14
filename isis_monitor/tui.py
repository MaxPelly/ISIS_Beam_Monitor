from datetime import datetime
from threading import Lock

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.table import Table

class RichTUI:
    def __init__(self):
        self.layout = self._make_layout()
        self.mcr_news = "Waiting for initial MCR news..."
        self.beam_states = {
            "TS1": {"current": 0.0, "power": "unknown"},
            "TS2": {"current": 0.0, "power": "unknown"},
            "Muons": {"current": 0.0, "power": "unknown"}
        }
        self.last_update = datetime.now()
        self._lock = Lock()
        self.live = Live(self.layout, refresh_per_second=4, screen=True)

    def _make_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main")
        )
        layout["main"].split_row(
            Layout(name="beam", ratio=1),
            Layout(name="mcr", ratio=1)
        )
        return layout

    def start(self):
        """Start the live TUI display."""
        self.live.start()
        self._update_all()

    def stop(self):
        """Stop the live TUI display."""
        self.live.stop()

    def update_beam_state(self, beam: str, current: float, power: str):
        """Update the state of a specific beam target."""
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

    def _update_all(self):
        """Force update all panels."""
        with self._lock:
            self.layout["header"].update(
                Panel(
                    Text("ISIS Facility Monitor", justify="center", style="bold cyan"),
                    style="blue"
                )
            )
            self._update_beam_panel()
            self._update_mcr_panel()

    def _update_beam_panel(self):
        table = Table(show_header=True, header_style="bold magenta", expand=True)
        table.add_column("Beam Target")
        table.add_column("Current (μA)", justify="right")
        table.add_column("Power Level")

        for beam, state in self.beam_states.items():
            power_style = "green"
            if state["power"] == "off":
                power_style = "red"
            elif state["power"] in ["low", "unknown"]:
                power_style = "yellow"

            power_str = str(state["power"])
            table.add_row(
                beam,
                f"{state['current']:.3f}",
                f"[{power_style}]{power_str.upper()}[/]"
            )

        time_str = self.last_update.strftime("%H:%M:%S")
        self.layout["beam"].update(
            Panel(
                table,
                title=f"Beam Status (Last Update: {time_str})",
                border_style="cyan"
            )
        )

    def _update_mcr_panel(self):
        self.layout["mcr"].update(
            Panel(
                Text(self.mcr_news, style="white"),
                title="Latest MCR News",
                border_style="cyan"
            )
        )
