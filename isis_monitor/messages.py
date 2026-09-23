"""Shared formatting helpers and structured notification builders."""
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from isis_monitor import flavour

UK_TZ = ZoneInfo("Europe/London")
_display_tz = UK_TZ


def set_timezone(tz_name: str) -> None:
    """Set the timezone used by fmt_time(), e.g. from `[NOTIFICATIONS] timezone`."""
    global _display_tz
    _display_tz = ZoneInfo(tz_name)


def fmt_time(dt: datetime) -> str:
    """Format a datetime in the configured local timezone, e.g. 'Wed 23 Sep 14:05'."""
    return dt.astimezone(_display_tz).strftime("%a %d %b %H:%M")


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

# Always-on emoji — these carry information, so they show regardless of fun_mode.
STATE_EMOJI = {"high": "🟢", "medium": "🟡", "low": "🟠", "off": "🔴"}
RESTORED_EMOJI = "🎉"  # beam recovering from an outage of an hour or more
NEW_RUN_EMOJI = "🚀"
FINISHING_EMOJI = "🏁"
MCR_NEWS_EMOJI = "📰"
OUTAGE_RESTORE_THRESHOLD = timedelta(hours=1)


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
    rng: Optional[random.Random] = None,
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

    restored = (
        prev_state == "off" and new_state != "off"
        and time_in_prev_state >= OUTAGE_RESTORE_THRESHOLD
    )
    emoji = RESTORED_EMOJI if restored else STATE_EMOJI[new_state]
    transition = "restored" if restored else new_state

    text = f"{display_name} beam moved from {prev_state} to {new_state}."
    if trip_note:
        text = f"{text}\n\n{trip_note}"

    return Notification(
        title=f"{display_name} {arrow} {prev_state} → {new_state}",
        text=text,
        severity=severity,
        emoji=emoji,
        facts=[
            ("Current", f"{beam_val:.3f} uA"),
            ("Previous", f"{prev_val:.3f} uA"),
            ("% of high threshold", f"{beam_val / high_threshold * 100:.0f}%"),
            (f"Was {prev_state}", f"for {fmt_duration(time_in_prev_state)}"),
        ],
        flavour=flavour.pick((display_name, transition), rng) if rng else "",
        timestamp=time_now,
    )


def startup_status(
    display_name: str,
    state: str,
    beam_val: float,
    time_now: datetime,
    rng: Optional[random.Random] = None,
) -> Notification:
    return Notification(
        title=f"Monitor online: {display_name} is {state}",
        text=f"Current: {beam_val:.3f} uA",
        emoji="🛰️",
        flavour=flavour.pick((display_name, "startup"), rng) if rng else "",
        timestamp=time_now,
    )


def run_started(
    new_run_name: str,
    prev_run_name: str,
    prev_duration: timedelta,
    prev_good_frames: float,
    prev_raw_frames: float,
    time_now: datetime,
    rng: Optional[random.Random] = None,
) -> Notification:
    """Build a card for a new run starting — reports on the run that just ended."""
    return Notification(
        title="New run started",
        text=new_run_name,
        emoji=NEW_RUN_EMOJI,
        facts=[
            ("Previous run", prev_run_name),
            ("Duration", fmt_duration(prev_duration)),
            ("Final good frames", f"{prev_good_frames:.0f}"),
            ("Final raw frames", f"{prev_raw_frames:.0f}"),
        ],
        flavour=flavour.pick(("*", "new_run"), rng) if rng else "",
        timestamp=time_now,
    )


def run_finishing(
    run_name: str,
    tracked_frames: float,
    counts_target: float,
    good_frames: float,
    raw_frames: float,
    rate_per_second: float,
    instrument_state: str,
    time_now: datetime,
    rng: Optional[random.Random] = None,
) -> Notification:
    facts = [("Frames", f"{tracked_frames:.0f} / {counts_target:.0f}")]
    facts.append(("Rate", f"{rate_per_second * 60:.1f} frames/min"))
    if rate_per_second > 0:
        eta_seconds = max(counts_target - tracked_frames, 0) / rate_per_second
        facts.append(("ETA", fmt_duration(timedelta(seconds=eta_seconds))))
    efficiency = (good_frames / raw_frames * 100) if raw_frames > 0 else 0.0
    facts.append(("Good-frame efficiency", f"{efficiency:.0f}%"))
    facts.append(("Instrument beam", instrument_state))

    return Notification(
        title="Run about to finish",
        text=run_name,
        emoji=FINISHING_EMOJI,
        facts=facts,
        flavour=flavour.pick(("*", "finishing"), rng) if rng else "",
        timestamp=time_now,
    )


def frames_vetoed(time_now: datetime) -> Notification:
    return Notification(
        title="Frames being vetoed",
        text="Good frames are flat while raw frames keep rising.",
        severity=Severity.WARNING,
        emoji="⚠️",
        timestamp=time_now,
    )


def frames_stalled(instrument_target: str, stalled_for: timedelta, time_now: datetime) -> Notification:
    return Notification(
        title="Frames stalled",
        text=f"No new frames for {fmt_duration(stalled_for)} while {instrument_target} beam is on.",
        severity=Severity.WARNING,
        emoji="⚠️",
        timestamp=time_now,
    )


def mcr_news(
    news_text: str, time_now: datetime, rng: Optional[random.Random] = None
) -> Notification:
    return Notification(
        title="MCR News",
        text=news_text,
        emoji=MCR_NEWS_EMOJI,
        flavour=flavour.pick(("*", "mcr_news"), rng) if rng else "",
        timestamp=time_now,
    )
