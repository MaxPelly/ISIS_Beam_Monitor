"""Shared formatting helpers and structured notification builders."""
import random
import re
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


def get_timezone() -> ZoneInfo:
    """The timezone currently configured via set_timezone()."""
    return _display_tz


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
    url_label: str = "Open"
    timestamp: Optional[datetime] = None
    channel: str = ""  # e.g. "TS1" or an instrument name; falls back to the NotificationChannel's
                        # name in NotificationChannel.broadcast() when left blank
    topic: str = ""  # what it's about, for subscribers filtering it: a beam target's channel
                     # label, an instrument name, "MCR" or "Summary"

    def _parts(self, with_flavour: bool) -> List[str]:
        parts = [f"{self.emoji} {self.title}".strip(), self.text]
        parts.extend(f"{key}: {value}" for key, value in self.facts)
        parts += [self.flavour if with_flavour else "", fmt_time(self.timestamp) if self.timestamp else ""]
        return [part for part in parts if part]

    def to_plain_text(self) -> str:
        """Render as plain text, e.g. for log lines or the dummy notifier."""
        return "\n".join(self._parts(with_flavour=True))

    def to_summary(self) -> str:
        """Render as a single line for the Teams `summary` field.

        Text-only clients that can't show the Adaptive Card display this
        instead, so it carries the key details: title, text, facts and time.
        Flavour is left out to keep it short.
        """
        return " | ".join(" ".join(part.split()) for part in self._parts(with_flavour=False))


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
    channel: str = "",
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

    pct_of_high = f"{beam_val / high_threshold * 100:.0f}%" if high_threshold > 0 else "n/a"

    return Notification(
        title=f"{display_name} {arrow} {prev_state} → {new_state}",
        text=text,
        severity=severity,
        emoji=emoji,
        facts=[
            ("Current", f"{beam_val:.3f} uA"),
            ("Previous", f"{prev_val:.3f} uA"),
            ("% of high threshold", pct_of_high),
            (f"Was {prev_state} for", fmt_duration(time_in_prev_state)),
        ],
        flavour=flavour.pick(transition, rng) if rng else "",
        timestamp=time_now,
        channel=channel,
        topic=channel,
    )


def startup_status(
    display_name: str,
    state: str,
    beam_val: float,
    time_now: datetime,
    rng: Optional[random.Random] = None,
    channel: str = "",
) -> Notification:
    return Notification(
        title=f"Monitor online: {display_name} is {state}",
        text=f"Current: {beam_val:.3f} uA",
        emoji="🛰️",
        flavour=flavour.pick("startup", rng) if rng else "",
        timestamp=time_now,
        channel=channel,
        topic=channel,
    )


def run_started(
    instrument: str,
    new_run_name: str,
    prev_run_name: str,
    prev_duration: timedelta,
    prev_counts: float,
    time_now: datetime,
    rng: Optional[random.Random] = None,
    channel: str = "",
) -> Notification:
    """Build a card for a new run starting — reports on the run that just ended.

    The run builders' `channel` is blank by default, which broadcast() fills
    with the experiment channel's name ("Experiment Updates").
    """
    return Notification(
        title=f"{instrument}: New run started",
        text=new_run_name,
        emoji=NEW_RUN_EMOJI,
        facts=[
            ("Previous run", prev_run_name),
            ("Duration", fmt_duration(prev_duration)),
            # Negative means no reading ever arrived for that run.
            ("Final total collected", f"{prev_counts:.1f} µA·h" if prev_counts >= 0 else "unknown"),
        ],
        flavour=flavour.pick("new_run", rng) if rng else "",
        timestamp=time_now,
        channel=channel,
        topic=instrument,
    )


def run_finishing(
    instrument: str,
    run_name: str,
    counts_collected: float,
    counts_target: float,
    rate_per_second: float,
    instrument_state: str,
    time_now: datetime,
    rng: Optional[random.Random] = None,
    channel: str = "",
) -> Notification:
    facts = [("Collected", f"{counts_collected:.1f} / {counts_target:g} µA·h")]
    # µA·h collected per hour is simply the average beam current, in µA.
    facts.append(("Rate", f"{rate_per_second * 3600:.1f} µA"))
    if counts_collected >= counts_target:
        # Sent on reaching the target rather than ahead of it (no usable ETA).
        facts.append(("ETA", "target reached"))
    elif rate_per_second > 0:
        eta_seconds = (counts_target - counts_collected) / rate_per_second
        facts.append(("ETA", fmt_duration(timedelta(seconds=eta_seconds))))
    facts.append(("Instrument beam", instrument_state))

    return Notification(
        title=f"{instrument}: Run about to finish",
        text=run_name,
        emoji=FINISHING_EMOJI,
        facts=facts,
        flavour=flavour.pick("finishing", rng) if rng else "",
        timestamp=time_now,
        channel=channel,
        topic=instrument,
    )


def collection_stalled(
    instrument: str, beam_target: str, stalled_for: timedelta, time_now: datetime, channel: str = ""
) -> Notification:
    return Notification(
        title=f"{instrument}: Data collection stalled",
        text=f"No µA·h collected for {fmt_duration(stalled_for)} while {beam_target} beam is on.",
        severity=Severity.WARNING,
        emoji="⚠️",
        timestamp=time_now,
        channel=channel,
        topic=instrument,
    )


# Checked in this order — GOOD first, since a resolution message routinely
# names the fault it just cleared in the same sentence (e.g. "The faulty
# power supply ... has been repaired").
# Whole words (with common endings), so e.g. "tissue" isn't an issue.
_MCR_GOOD_RE = re.compile(r"\b(restored|(back|beam) on(line)?|resolved|rectified|repaired|fixed)\b")
_MCR_ATTENTION_RE = re.compile(
    r"\b(fault(s|y|ed|ing)?|(issue|problem)s?|trip(s|ped|ping)?|investigating)\b"
)
_MCR_WARNING_RE = re.compile(r"\b(maintenance|shutdowns?)\b")


def _mcr_severity_and_emoji(news_text: str) -> Tuple[Severity, str]:
    lowered = news_text.lower()
    if _MCR_GOOD_RE.search(lowered):
        return Severity.GOOD, "🎉"
    if _MCR_ATTENTION_RE.search(lowered):
        return Severity.ATTENTION, "🚨"
    if _MCR_WARNING_RE.search(lowered):
        return Severity.WARNING, "🔧"
    return Severity.INFO, MCR_NEWS_EMOJI


def mcr_news(
    news_text: str,
    time_now: datetime,
    url: Optional[str] = None,
    rng: Optional[random.Random] = None,
) -> Notification:
    severity, emoji = _mcr_severity_and_emoji(news_text)
    return Notification(
        title="MCR News",
        text=news_text,
        severity=severity,
        emoji=emoji,
        flavour=flavour.pick("mcr_news", rng) if rng else "",
        url=url,
        url_label="Open MCR news",
        timestamp=time_now,
        topic="MCR",
    )


def daily_summary(
    display_name: str,
    uptime_pct: float,
    trips: int,
    longest_on_streak: timedelta,
    sparkline: str,
    runs_last_24h: int,
    time_now: datetime,
    is_new_record: bool = False,
    fact_of_the_day: str = "",
    coverage_pct: float = 100.0,
) -> Notification:
    text = f"{display_name}: {uptime_pct:.0f}% uptime over the last 24h."
    if is_new_record:
        text = f"{text} 🏆 New record!"

    facts = [("Uptime", f"{uptime_pct:.0f}%")]
    # Uptime only covers the time the beam feed was connected; say so when
    # that wasn't (nearly) all of it. The slack allows for timer jitter.
    if coverage_pct < 99:
        facts.append(("Data coverage", f"{coverage_pct:.0f}%"))
    facts += [
        ("Trips", str(trips)),
        ("Longest continuous on", fmt_duration(longest_on_streak)),
        ("Sparkline", sparkline),
        ("Runs in last 24h", str(runs_last_24h)),
    ]

    return Notification(
        title=f"{display_name} daily summary",
        text=text,
        severity=Severity.GOOD if uptime_pct >= 90 else Severity.INFO,
        emoji="📊",
        facts=facts,
        flavour=fact_of_the_day,
        timestamp=time_now,
        channel=display_name,  # already the beam target's channel label (e.g. "TS1")
        topic="Summary",
    )


def run_milestone(
    instrument: str,
    run_count: int,
    time_now: datetime,
    rng: Optional[random.Random] = None,
    channel: str = "",
) -> Notification:
    return Notification(
        title=f"{instrument}: {run_count} runs completed",
        text=f"That's {run_count} runs since records began.",
        severity=Severity.GOOD,
        emoji="🏆",
        flavour=flavour.pick("milestone", rng) if rng else "",
        timestamp=time_now,
        channel=channel,
        topic=instrument,
    )


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def strip_controls(text: str) -> str:
    """Remote text (run titles, MCR news) with each control character made a
    space, so e.g. ESC sequences can't drive a terminal showing the TUI or log."""
    return _CONTROL_RE.sub(" ", text).strip()


# Eight Unicode block heights, index 0 = shortest
_BLOCKS = " ▁▂▃▄▅▆▇█"


def sparkline_chars(values: list[float], width: int) -> str:
    """Return a plain-text sparkline of `width` block characters, min-max normalised.

    Shared between the daily summary (plain text in a notification card) and
    the TUI (which colours each block); kept here so the daemon needn't
    import the TUI and rich.
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
