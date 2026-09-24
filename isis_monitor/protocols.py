from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class MonitorSinkProtocol(Protocol):
    """Receives monitor updates; implemented by DaemonState."""

    def update_beam_state(self, beam: str, current: float, power: str) -> None:
        ...

    def update_mcr_news(self, news: str) -> None:
        ...

    def update_run_name(self, run_name: str) -> None:
        ...

    def update_counts(self, counts: float) -> None:
        ...

    def update_health(self, component: str, status: str) -> None:
        ...

    def record_run_completed(self, ts: datetime) -> int:
        """Record a completed run and return the new all-time total."""
        ...
