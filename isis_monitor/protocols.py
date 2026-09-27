from datetime import datetime
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class MonitorSinkProtocol(Protocol):
    """Receives monitor updates; implemented by DaemonState."""

    def update_beam_state(self, beam: str, current: float, power: str) -> None:
        ...

    def update_mcr_news(self, news: str) -> None:
        ...

    def update_run_name(self, instrument: str, run_name: str) -> None:
        ...

    def update_counts(self, instrument: str, counts: float) -> None:
        ...

    def update_run_progress(
        self, instrument: str, run_started_at: Optional[datetime], end_notified: bool
    ) -> None:
        """Record tracker state that must survive a restart (not published)."""
        ...

    def update_health(self, component: str, status: str) -> None:
        ...

    def record_run_completed(self, instrument: str, ts: datetime) -> int:
        """Record a completed run and return the instrument's new all-time total."""
        ...
