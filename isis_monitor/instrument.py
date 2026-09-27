"""Run and counts tracking for one instrument, fed by BeamMonitor's WebSocket."""
import base64
import logging
import math
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Deque, Optional, Sequence, Tuple

from isis_monitor.config import InstrumentConfig
from isis_monitor.messages import collection_stalled, run_finishing, run_milestone, run_started
from isis_monitor.notifiers import NotificationChannel
from isis_monitor.protocols import MonitorSinkProtocol

logger = logging.getLogger(__name__)

COUNTS_SAMPLE_WINDOW = timedelta(minutes=15)
STALL_CHECK_WINDOW = timedelta(minutes=5)
# How much counts history the rate needs before an early finishing card is
# sent on its ETA; until then the card waits for notify_counts itself.
ETA_MIN_HISTORY = timedelta(minutes=3)
RUN_MILESTONE_INTERVAL = 25


def _fit_rate(samples: Sequence[Tuple[datetime, float]]) -> float:
    """Least-squares slope (value per second) through (time, value) samples."""
    if len(samples) < 2:
        return 0.0
    t0 = samples[0][0]
    xs = [(t - t0).total_seconds() for t, _ in samples]
    ys = [v for _, v in samples]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    numer = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return numer / denom


def _is_blank(value: Any) -> bool:
    """PVWS sends "nan" (or nothing) for a PV with no value yet."""
    return not value or (isinstance(value, str) and value.lower() == "nan")


@dataclass
class InstrumentState:
    """Per-instrument runtime state."""
    run_name: str = ""
    run_started_at: Optional[datetime] = None
    current_counts: float = -1.0  # total current collected so far this experiment
    collected_samples: Deque[Tuple[datetime, float]] = field(default_factory=deque)  # (time, current_counts)
    end_notified: bool = False

    # Periodic collection-progress check (InstrumentTracker.check_collection_progress)
    collection_stalled_since: Optional[datetime] = None
    stall_warned: bool = False


class InstrumentTracker:
    """Tracks one instrument's runs and counts, and sends its run notifications.

    `beam_power` looks up a beam target's current power label, so run cards
    and the stall check can report on the instrument's beam.
    """
    def __init__(
        self,
        instrument: InstrumentConfig,
        experiment_channel: NotificationChannel,
        beam_power: Callable[[str], str],
        stall_minutes: float,
        finish_warning_minutes: float = 0.0,
        fun_mode: bool = False,
        rng: Optional[random.Random] = None,
        sink: Optional[MonitorSinkProtocol] = None,
    ):
        self.instrument = instrument
        self.experiment_channel = experiment_channel
        self.beam_power = beam_power
        self.stall_minutes = stall_minutes
        self.finish_warning_minutes = finish_warning_minutes
        self.fun_mode = fun_mode
        self._rng = rng or random.Random()
        self.sink = sink
        self.state = InstrumentState()

    def _flavour_rng(self) -> Optional[random.Random]:
        return self._rng if self.fun_mode else None

    def _card_channel(self) -> str:
        """Teams channel for this instrument's cards; blank means the
        experiment channel's own name ("Experiment Updates")."""
        return self.instrument.name if self.instrument.channel == "instrument" else ""

    def _beam_state(self) -> str:
        return self.beam_power(self.instrument.beam_target)

    async def handle_run_name(self, b64_data: Any, time_now: datetime) -> None:
        if _is_blank(b64_data):
            return
        try:
            name = base64.b64decode(b64_data).decode().strip("\x00")
        except Exception as e:
            logger.warning(f"Failed to decode run name b64: {e}")
            return
        if not name.strip():
            return  # e.g. all NULs; treating it as a run would hide the next real one

        if self.state.run_name and self.state.run_name != name:
            notification = run_started(
                self.instrument.name,
                name,
                self.state.run_name,
                time_now - self.state.run_started_at,
                self.state.current_counts,
                time_now,
                rng=self._flavour_rng(),
                channel=self._card_channel(),
            )
            logger.info(f"New Run: {notification.to_plain_text()}")
            await self.experiment_channel.broadcast(notification)
            self.state.current_counts = 0
            self.state.end_notified = False  # saved below with the new run's start
            self.state.collected_samples.clear()
            self.state.collection_stalled_since = None
            self.state.stall_warned = False

            total_runs = self.sink.record_run_completed(self.instrument.name, time_now) if self.sink else 0
        else:
            # PVWS re-sends the current title on every (re)subscribe, so a repeat
            # of the same name mustn't restart the run's clock. (A new run that
            # reuses the old title can't be told apart, so it isn't reported.)
            if name == self.state.run_name and self.state.run_started_at is not None:
                return
            total_runs = 0

        # Saved before any further await, so a snapshot can't pair the new
        # completion count with the old run (which a restart would re-report).
        self.state.run_name = name
        self.state.run_started_at = time_now
        if self.sink:
            self.sink.update_run_name(self.instrument.name, name)
        self._save_progress()

        if self.fun_mode and total_runs and total_runs % RUN_MILESTONE_INTERVAL == 0:
            milestone = run_milestone(
                self.instrument.name, total_runs, time_now, rng=self._rng, channel=self._card_channel()
            )
            logger.info(f"Milestone: {milestone.to_plain_text()}")
            await self.experiment_channel.broadcast(milestone)

    async def handle_counts(self, raw_value: Any, time_now: datetime) -> None:
        """`raw_value` is IN:<NAME>:DAE:TOTALUAMPS: total µA·h collected this run."""
        # Not _is_blank(): 0 is a real reading at the start of a run.
        try:
            total_collected = float(raw_value)
        except (TypeError, ValueError):
            logger.warning(f"Failed to parse {self.instrument.counts_pv} value {raw_value!r}")
            return
        if not math.isfinite(total_collected):
            return  # PVWS sends NaN for a PV with no value yet

        previous = self.state.current_counts
        self.state.current_counts = total_collected
        samples = self.state.collected_samples
        samples.append((time_now, total_collected))
        self._prune_collected_samples(time_now)

        if self.sink:
            self.sink.update_counts(self.instrument.name, total_collected)

        counts_target = self.instrument.notify_counts
        if self.state.end_notified:
            # Counts only fall when the DAE resets them, e.g. a new run that
            # kept the old title; then the next finish needs a card too.
            if total_collected < previous and total_collected < counts_target - 25:
                self.state.end_notified = False
                self._save_progress()
            return

        remaining = counts_target - total_collected
        if remaining >= 0 and (
            self.finish_warning_minutes <= 0 or samples[-1][0] - samples[0][0] < ETA_MIN_HISTORY
        ):
            return  # can't be due yet; skips the rate fit on most updates
        rate = _fit_rate(samples)
        if remaining < 0 or (rate > 0 and remaining / rate <= self.finish_warning_minutes * 60):
            notification = run_finishing(
                self.instrument.name,
                self.state.run_name,
                total_collected,
                counts_target,
                rate,
                self._beam_state(),
                time_now,
                rng=self._flavour_rng(),
                channel=self._card_channel(),
            )
            logger.info(f"Run finishing: {notification.to_plain_text()}")
            await self.experiment_channel.broadcast(notification)
            self.state.end_notified = True
            self._save_progress()

    def _save_progress(self) -> None:
        if self.sink:
            self.sink.update_run_progress(
                self.instrument.name, self.state.run_started_at, self.state.end_notified
            )

    def restore(self, saved: dict) -> None:
        """Seed state from a DaemonState snapshot entry after a restart. PVWS
        re-sends the same run title on connect, which then changes nothing."""
        if not saved.get("run_name") or not saved.get("run_started_at"):
            return
        self.state.run_name = str(saved["run_name"])
        self.state.run_started_at = datetime.fromisoformat(saved["run_started_at"])
        self.state.current_counts = float(saved.get("counts", -1.0))
        # DaemonState has already cleared this if notify_counts has changed.
        self.state.end_notified = bool(saved.get("end_notified", False))

    def reset_stall_clock(self) -> None:
        self.state.collection_stalled_since = None

    def _prune_collected_samples(self, now: datetime) -> None:
        cutoff = now - COUNTS_SAMPLE_WINDOW
        samples = self.state.collected_samples
        while samples and samples[0][0] < cutoff:
            samples.popleft()

    def _collected_baseline_before(self, cutoff: datetime) -> Optional[float]:
        """Most recent counts-collected sample at or before `cutoff`, or None
        if there isn't `STALL_CHECK_WINDOW` worth of history yet."""
        baseline = None
        for t, value in self.state.collected_samples:
            if t > cutoff:
                break
            baseline = value
        return baseline

    async def check_collection_progress(self, time_now: datetime) -> None:
        """Detect stalled data collection (called roughly every 60s).

        Only meaningful while a run is active — between runs, the collected
        count is naturally static, which would otherwise look identical to a
        stall.

        Movement is judged over STALL_CHECK_WINDOW rather than since the last
        tick: the counts PV can plausibly update in batches, so comparing
        only the last ~60s could make a perfectly healthy, just-batchy source
        look stalled every time a batch hadn't landed yet in that particular
        minute.
        """
        if not self.state.run_name:
            return

        baseline = self._collected_baseline_before(time_now - STALL_CHECK_WINDOW)
        if baseline is None:
            return  # not enough history yet to judge movement over the window

        moved = self.state.current_counts > baseline

        if not moved:
            if self.state.collection_stalled_since is None:
                self.state.collection_stalled_since = time_now
            stalled_for = time_now - self.state.collection_stalled_since
            if (
                stalled_for >= timedelta(minutes=self.stall_minutes)
                and self._beam_state() != "off"
                and not self.state.stall_warned
            ):
                self.state.stall_warned = True
                notification = collection_stalled(
                    self.instrument.name, self.instrument.beam_target, stalled_for, time_now,
                    channel=self._card_channel(),
                )
                logger.info(f"Stall Warning: {notification.to_plain_text()}")
                await self.experiment_channel.broadcast(notification)
        else:
            self.state.collection_stalled_since = None
            self.state.stall_warned = False
