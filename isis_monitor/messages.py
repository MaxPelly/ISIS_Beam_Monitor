"""Shared formatting helpers and structured notification builders."""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

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
    url: Optional[str] = None
    url_label: str = "Open"
    timestamp: Optional[datetime] = None
    topic: str = ""  # what it's about, for subscribers filtering it: a beam target,
                     # an instrument name, "MCR" or "Summary"
    beam: str = ""  # the beam target (e.g. "Muons") of beam cards and summaries

    def _parts(self) -> List[str]:
        parts = [f"{self.emoji} {self.title}".strip(), self.text]
        parts.extend(f"{key}: {value}" for key, value in self.facts)
        parts.append(fmt_time(self.timestamp) if self.timestamp else "")
        return [part for part in parts if part]

    def to_plain_text(self) -> str:
        """Render as plain text, e.g. for log lines or the dummy notifier."""
        return "\n".join(self._parts())

    def to_summary(self) -> str:
        """Render as a single line for the Teams `summary` field.

        Text-only clients that can't show the Adaptive Card display this
        instead, so it carries the key details: title, text, facts and time.
        """
        return " | ".join(" ".join(part.split()) for part in self._parts())


# ---------------------------------------------------------------------------
# Builders — one pure function per notification-worthy event.
# ---------------------------------------------------------------------------

def beam_change(
    beam: str,
    *,
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

    restored = (
        prev_state == "off" and new_state != "off"
        and time_in_prev_state >= OUTAGE_RESTORE_THRESHOLD
    )
    emoji = RESTORED_EMOJI if restored else STATE_EMOJI[new_state]

    text = f"{beam} beam moved from {prev_state} to {new_state}."
    if trip_note:
        text = f"{text}\n\n{trip_note}"

    pct_of_high = f"{beam_val / high_threshold * 100:.0f}%" if high_threshold > 0 else "n/a"

    return Notification(
        title=f"{beam} {arrow} {prev_state} → {new_state}",
        text=text,
        severity=severity,
        emoji=emoji,
        facts=[
            ("Current", f"{beam_val:.3f} uA"),
            ("Previous", f"{prev_val:.3f} uA"),
            ("% of high threshold", pct_of_high),
            (f"Was {prev_state} for", fmt_duration(time_in_prev_state)),
        ],
        timestamp=time_now,
        topic=beam,
        beam=beam,
    )


def startup_status(beam: str, *, state: str, beam_val: float, time_now: datetime) -> Notification:
    return Notification(
        title=f"Monitor online: {beam} is {state}",
        text=f"Current: {beam_val:.3f} uA",
        emoji="🛰️",
        timestamp=time_now,
        topic=beam,
        beam=beam,
    )


def run_started(
    instrument: str,
    *,
    run_number: str,
    new_run_name: str,
    time_now: datetime,
    target: float,
    target_note: str = "",  # e.g. "auto, from 5 runs"
    prev_run_name: Optional[str] = None,  # None: the previous run isn't known (e.g. runs were missed)
    prev_duration: Optional[timedelta] = None,
    prev_counts: float = -1.0,  # negative: its final total isn't known
) -> Notification:
    """Build a card for a new run starting: its number and target, and a
    report on the run just before it."""
    facts = [("Run number", run_number)]
    if prev_run_name is not None:
        facts.append(("Previous run", prev_run_name))
        if prev_duration is not None:
            facts.append(("Duration", fmt_duration(prev_duration)))
        facts.append(("Final total collected", f"{prev_counts:.1f} µA·h" if prev_counts >= 0 else "unknown"))
    facts.append(("Target", f"{target:g} µA·h" + (f" ({target_note})" if target_note else "")))
    return Notification(
        title=f"{instrument}: New run started",
        text=new_run_name,
        emoji=NEW_RUN_EMOJI,
        facts=facts,
        timestamp=time_now,
        topic=instrument,
    )


def run_restarted(instrument: str, *, run_number: str, run_name: str, time_now: datetime) -> Notification:
    """A run aborted and begun again under its number with a new title (e.g. a
    new temperature): new in all but number, so not counted as another run."""
    return Notification(
        title=f"{instrument}: Run {run_number} restarted",
        text=run_name,
        emoji=NEW_RUN_EMOJI,
        facts=[("Run number", run_number)],
        timestamp=time_now,
        topic=instrument,
    )


def run_finishing(
    instrument: str,
    *,
    run_name: str,
    counts_collected: float,
    counts_target: float,
    rate_per_second: float,
    instrument_state: str,
    time_now: datetime,
    auto: bool = False,  # counts_target came from recent runs (notify_counts = auto)
    run_number: str = "",
) -> Notification:
    facts = [("Run number", run_number)] if run_number else []
    facts.append(("Collected", f"{counts_collected:.1f} / {counts_target:g} µA·h{' (auto)' if auto else ''}"))
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
        timestamp=time_now,
        topic=instrument,
    )


def collection_stalled(
    instrument: str, *, beam_target: str, stalled_for: timedelta, time_now: datetime, run_number: str = "",
) -> Notification:
    return Notification(
        title=f"{instrument}: Data collection stalled",
        text=f"No µA·h collected for {fmt_duration(stalled_for)} while {beam_target} beam is on.",
        facts=[("Run number", run_number)] if run_number else [],
        severity=Severity.WARNING,
        emoji="⚠️",
        timestamp=time_now,
        topic=instrument,
    )


def collection_resumed(instrument: str, *, stalled_for: timedelta, time_now: datetime) -> Notification:
    """After a collection_stalled warning, once the µA·h collected rises again."""
    return Notification(
        title=f"{instrument}: Data collection resumed",
        text=f"µA·h are being collected again, after {fmt_duration(stalled_for)} without.",
        severity=Severity.GOOD,
        emoji="✅",
        timestamp=time_now,
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


def mcr_news(news_text: str, *, time_now: datetime, url: Optional[str] = None) -> Notification:
    severity, emoji = _mcr_severity_and_emoji(news_text)
    return Notification(
        title="MCR News",
        text=news_text,
        severity=severity,
        emoji=emoji,
        url=url,
        url_label="Open MCR news",
        timestamp=time_now,
        topic="MCR",
    )


def daily_summary(
    beam: str,
    *,
    uptime_pct: float,
    trips: int,
    longest_on_streak: timedelta,
    sparkline: str,
    instrument_runs: Dict[str, Tuple[int, float]],
    time_now: datetime,
    coverage_pct: float = 100.0,
) -> Notification:
    text = f"{beam}: {uptime_pct:.0f}% uptime over the last 24h."

    facts = [("Uptime", f"{uptime_pct:.0f}%")]
    # Uptime only covers the time the beam feed was connected; say so when
    # that wasn't (nearly) all of it. The slack allows for timer jitter.
    if coverage_pct < 99:
        facts.append(("Data coverage", f"{coverage_pct:.0f}%"))
    facts += [
        ("Trips", str(trips)),
        ("Longest continuous on", fmt_duration(longest_on_streak)),
        ("Sparkline", sparkline),
        ("Runs in last 24h", str(sum(runs for runs, _ in instrument_runs.values()))),
    ]
    facts += [
        (name, f"{runs} run{'s' * (runs != 1)}, {uah:.1f} µA·h" if runs else "no runs")
        for name, (runs, uah) in instrument_runs.items()
    ]

    return Notification(
        title=f"{beam} daily summary",
        text=text,
        severity=Severity.GOOD if uptime_pct >= 90 else Severity.INFO,
        emoji="📊",
        facts=facts,
        timestamp=time_now,
        topic="Summary",
        beam=beam,
    )


def experiment_finished(
    instrument: str, *, rb_number: str, runs: int, uah: float, duration: timedelta, partial: bool,
    time_now: datetime,
) -> Notification:
    """On an instrument's RB number changing: the experiment it ended. `partial`
    means it had started before the daemon saw it, so its totals are only the runs since."""
    at_least = "at least " if partial else ""
    return Notification(
        title=f"{instrument}: Experiment finished",
        text=f"RB {rb_number}",
        emoji="📋",
        facts=[
            ("Runs", f"{at_least}{runs}"),
            ("Total collected", f"{at_least}{uah:.1f} µA·h"),
            ("Average run", f"{uah / runs:.1f} µA·h"),
            ("Duration", f"{at_least}{fmt_duration(duration)}"),
        ],
        timestamp=time_now,
        topic=instrument,
    )


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def strip_controls(text: str) -> str:
    """Remote text (run titles, MCR news) with each control character made a
    space, so e.g. ESC sequences can't drive a terminal showing `status` or the log."""
    return _CONTROL_RE.sub(" ", text).strip()


# Eight Unicode block heights, index 0 = shortest
_BLOCKS = " ▁▂▃▄▅▆▇█"


def sparkline_chars(values: list[float], width: int) -> str:
    """Return a plain-text sparkline of `width` block characters, min-max normalised.

    Shared by the daily summary (plain text in a notification card) and
    `isis-monitor status`.
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
