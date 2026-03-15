import asyncio
from typing import Protocol, runtime_checkable


@runtime_checkable
class TUIProtocol(Protocol):
    """Interface contract for TUI implementations consumed by the monitors.

    Any class implementing these methods can be passed as the ``tui``
    argument to :class:`~isis_monitor.beam.BeamMonitor` or
    :class:`~isis_monitor.mcr.MCRNewsMonitor`, or used directly by ``main.py``.
    """

    # ------------------------------------------------------------------
    # Data update API  (called by beam.py / mcr.py)
    # ------------------------------------------------------------------

    def update_beam_state(self, beam: str, current: float, power: str) -> None:
        """Update the displayed state of one beam target."""
        ...

    def update_mcr_news(self, news: str) -> None:
        """Update the displayed MCR news text."""
        ...

    def update_log(self, message: str) -> None:
        """Add a log message to the log display."""
        ...

    # ------------------------------------------------------------------
    # Lifecycle API  (called by main.py)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the live TUI display."""
        ...

    def stop(self) -> None:
        """Stop the live TUI display."""
        ...

    async def run_sampler(self, stop_event: asyncio.Event) -> None:
        """Coroutine that periodically snapshots beam state into history."""
        ...
