"""Shared formatting helpers and structured notification builders."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

UK_TZ = ZoneInfo("Europe/London")


def fmt_time(dt: datetime) -> str:
    """Format a datetime in local UK time, e.g. 'Wed 23 Sep 14:05'."""
    return dt.astimezone(UK_TZ).strftime("%a %d %b %H:%M")


def fmt_duration(td: timedelta) -> str:
    """Format a timedelta as e.g. '3h 12m', '45m' or '20s'."""
    total_seconds = max(int(td.total_seconds()), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


_STATE_ORDER = {"off": 0, "low": 1, "medium": 2, "high": 3}


class Severity(Enum):
    """How a notification should be visually emphasised."""
    INFO = "info"
    GOOD = "good"
    WARNING = "warning"
    ATTENTION = "attention"


@dataclass
class Notification:
    """A structured notification, independent of any particular delivery channel."""
    title: str
    text: str
    severity: Severity = Severity.INFO
    emoji: str = ""
    facts: List[Tuple[str, str]] = field(default_factory=list)
    flavour: str = ""
    url: Optional[str] = None
    timestamp: Optional[datetime] = None

    def to_plain_text(self) -> str:
        """Render as plain text, e.g. for log lines or the dummy notifier."""
        header = f"{self.emoji} {self.title}".strip()
        lines = [header, self.text]
        lines.extend(f"{key}: {value}" for key, value in self.facts)
        if self.flavour:
            lines.append(self.flavour)
        if self.timestamp:
            lines.append(fmt_time(self.timestamp))
        return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------------------
# Builders — one pure function per notification-worthy event.
# ---------------------------------------------------------------------------

def beam_change(
    display_name: str,
    prev_state: str,
    new_state: str,
    beam_val: float,
    prev_val: float,
    high_threshold: float,
    time_in_prev_state: timedelta,
    time_now: datetime,
    trip_note: str = "",
) -> Notification:
    """Build a card for a confirmed (debounced) beam state transition."""
    going_up = _STATE_ORDER[new_state] > _STATE_ORDER[prev_state]
    arrow = "⬆️" if going_up else "⬇️"
    if new_state == "off":
        severity = Severity.ATTENTION
    elif going_up:
        severity = Severity.GOOD
    else:
        severity = Severity.WARNING

    text = f"{display_name} beam moved from {prev_state} to {new_state}."
    if trip_note:
        text = f"{text}\n\n{trip_note}"

    return Notification(
        title=f"{display_name} {arrow} {prev_state} → {new_state}",
        text=text,
        severity=severity,
        facts=[
            ("Current", f"{beam_val:.3f} uA"),
            ("Previous", f"{prev_val:.3f} uA"),
            ("% of high threshold", f"{beam_val / high_threshold * 100:.0f}%"),
            (f"Was {prev_state}", f"for {fmt_duration(time_in_prev_state)}"),
        ],
        timestamp=time_now,
    )


def startup_status(
    display_name: str, state: str, beam_val: float, time_now: datetime
) -> Notification:
    return Notification(
        title=f"Monitor online: {display_name} is {state}",
        text=f"Current: {beam_val:.3f} uA",
        emoji="🛰️",
        timestamp=time_now,
    )


def run_started(run_name: str, time_now: datetime) -> Notification:
    return Notification(
        title="New run started",
        text=run_name,
        timestamp=time_now,
    )


def run_finishing(run_name: str, time_now: datetime) -> Notification:
    return Notification(
        title="Run about to finish",
        text=run_name,
        timestamp=time_now,
    )


def mcr_news(news_text: str, time_now: datetime) -> Notification:
    return Notification(
        title="MCR News",
        text=news_text,
        timestamp=time_now,
    )
