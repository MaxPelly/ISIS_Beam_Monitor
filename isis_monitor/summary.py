"""Daily beam-uptime summary cards, and fun_mode-only milestones."""
import asyncio
import json
import logging
import random
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Deque, Dict, List, Optional, Tuple

from isis_monitor.config import TARGET_LABELS, AppConfig
from isis_monitor.daemon_state import DaemonState
from isis_monitor.flavour import fact_of_the_day
from isis_monitor.messages import daily_summary, get_timezone
from isis_monitor.notifiers import NotificationChannel
from isis_monitor.storage import SQLiteStateStore
from isis_monitor.tui import sparkline_chars

logger = logging.getLogger(__name__)

SUMMARY_CHECK_INTERVAL = 60.0
SUMMARY_WINDOW = timedelta(hours=24)
SPARKLINE_WIDTH = 60
_OFF_LIKE = ("off", "unknown")
# Persisted so a daemon restart later the same day doesn't resend the summary.
LAST_SENT_KEY = "summary_last_sent"


@dataclass
class TargetSummary:
    uptime_pct: float
    trips: int
    longest_on_streak: timedelta
    sparkline: str
    coverage_pct: float = 100.0  # share of the expected samples actually recorded


def compute_summary(
    history: Dict[str, Deque[Tuple[datetime, float, str]]],
    since: datetime,
    until: Optional[datetime] = None,
    sample_interval: float = 60.0,
) -> Dict[str, TargetSummary]:
    """Summarise each target's samples at or after `since`.

    Samples aren't recorded while the beam feed is down, so uptime covers
    only the time the beam could be seen; coverage_pct says how much that was.
    """
    until = until or datetime.now(timezone.utc)
    expected_samples = max((until - since).total_seconds() / sample_interval, 1.0)
    # Loose enough that a daemon restart (e.g. after a TUI config save), which
    # misses a sample or two, doesn't end a streak.
    max_gap = timedelta(seconds=max(3 * sample_interval, 180.0))
    summaries: Dict[str, TargetSummary] = {}
    for beam, samples in history.items():
        recent = [s for s in samples if s[0] >= since]
        if not recent:
            summaries[beam] = TargetSummary(0.0, 0, timedelta(0), "", coverage_pct=0.0)
            continue

        on_count = sum(1 for _, _, power in recent if power not in _OFF_LIKE)
        uptime_pct = on_count / len(recent) * 100

        trips = 0
        longest_on_streak = timedelta(0)
        streak_start = recent[0][0] if recent[0][2] not in _OFF_LIKE else None
        prev_ts, prev_power = recent[0][0], recent[0][2]
        for ts, _, power in recent[1:]:
            is_on = power not in _OFF_LIKE
            if prev_power not in _OFF_LIKE and not is_on:
                trips += 1
            # A gap means the feed was down, so the beam wasn't seen to stay
            # on through it: a streak ends at the last sample before one.
            gap = ts - prev_ts > max_gap
            if streak_start is not None and (gap or not is_on):
                longest_on_streak = max(longest_on_streak, (prev_ts if gap else ts) - streak_start)
                streak_start = None
            if is_on and streak_start is None:
                streak_start = ts
            prev_ts, prev_power = ts, power
        if streak_start is not None:
            longest_on_streak = max(longest_on_streak, recent[-1][0] - streak_start)

        values = [cur for _, cur, _ in recent]
        summaries[beam] = TargetSummary(
            uptime_pct=uptime_pct,
            trips=trips,
            longest_on_streak=longest_on_streak,
            sparkline=sparkline_chars(values, SPARKLINE_WIDTH),
            coverage_pct=min(len(recent) / expected_samples * 100, 100.0),
        )
    return summaries


def _instruments_by_channel(state: DaemonState) -> Dict[str, List[str]]:
    """Instrument names grouped by the channel label (e.g. "Muons") of their
    beam target (e.g. "Muon")."""
    grouped: Dict[str, List[str]] = {}
    for name, info in state.instruments.items():
        grouped.setdefault(TARGET_LABELS.get(str(info["beam_target"]), ""), []).append(name)
    return grouped


def _load_records(store: SQLiteStateStore) -> Dict[str, float]:
    raw = store.load_snapshot("records")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Corrupt records snapshot, starting fresh.")
        return {}


def _load_last_sent(store: SQLiteStateStore) -> Optional[date]:
    raw = store.load_snapshot(LAST_SENT_KEY)
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _save_progress(store: SQLiteStateStore, sent: date, records: Dict[str, float]) -> None:
    store.upsert_snapshot(LAST_SENT_KEY, sent.isoformat())
    if records:
        store.upsert_snapshot("records", json.dumps(records))
    store.commit()


async def daily_summary_loop(
    config: AppConfig,
    state: DaemonState,
    store: SQLiteStateStore,
    beam_channel: NotificationChannel,
    stop_event: asyncio.Event,
    rng: Optional[random.Random] = None,
) -> None:
    """Send one summary card per target at config.summary_time (local time), once a day."""
    try:
        records = await store.run(_load_records, store)
        last_sent_date = await store.run(_load_last_sent, store)
    except sqlite3.Error:
        # Worst case today's summary is sent again; better than no summaries.
        logger.exception("Failed to load daily summary state; starting fresh")
        records, last_sent_date = {}, None
    target_hour, target_minute = (int(x) for x in config.summary_time.split(":"))

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=SUMMARY_CHECK_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass

        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone(get_timezone())
        # ">=" rather than exact-minute equality: a slow tick (e.g. one that
        # overlaps DB I/O) could otherwise step straight over the target
        # minute and silently skip the whole day's summary.
        if not (
            (now_local.hour, now_local.minute) >= (target_hour, target_minute)
            and last_sent_date != now_local.date()
        ):
            continue

        last_sent_date = now_local.date()
        since = now_utc - SUMMARY_WINDOW
        summaries = compute_summary(state.history, since, now_utc, config.sample_interval)
        instruments_by_channel = _instruments_by_channel(state)
        todays_fact = fact_of_the_day(rng) if (config.fun_mode and rng) else ""

        for beam, target_summary in summaries.items():
            prior_record = records.get(beam, 0.0)
            streak_seconds = target_summary.longest_on_streak.total_seconds()
            is_new_record = config.fun_mode and streak_seconds > prior_record
            if is_new_record:
                records[beam] = streak_seconds

            notification = daily_summary(
                beam,
                target_summary.uptime_pct,
                target_summary.trips,
                target_summary.longest_on_streak,
                target_summary.sparkline,
                state.count_runs_completed_since(since, instruments_by_channel.get(beam, [])),
                now_utc,
                is_new_record=is_new_record,
                fact_of_the_day=todays_fact,
                coverage_pct=target_summary.coverage_pct,
            )
            await beam_channel.broadcast(notification)

        try:
            await store.run(_save_progress, store, last_sent_date, records)
        except sqlite3.Error:
            # last_sent_date is still set in memory, so it isn't resent today.
            logger.exception("Failed to save daily summary state")

    logger.warning("Daily summary loop quit")
