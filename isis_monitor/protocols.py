from typing import Protocol, runtime_checkable


@runtime_checkable
class TUIProtocol(Protocol):
    """Interface contract for TUI implementations consumed by the monitors.

    Any class implementing these two methods can be passed as the ``tui``
    argument to :class:`~isis_monitor.beam.BeamMonitor` or
    :class:`~isis_monitor.mcr.MCRNewsMonitor`.
    """

    def update_beam_state(self, beam: str, current: float, power: str) -> None:
        """Update the displayed state of one beam target."""
        ...

    def update_mcr_news(self, news: str) -> None:
        """Update the displayed MCR news text."""
        ...
