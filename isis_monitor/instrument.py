"""Run and counts tracking for one instrument, fed by BeamMonitor's WebSocket."""
import base64
import logging
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Deque, List, Optional, Tuple

from isis_monitor.config import InstrumentConfig
from isis_monitor.messages import collection_stalled, run_finishing, run_milestone, run_started
from isis_monitor.notifiers import NotificationChannel
from isis_monitor.protocols import MonitorSinkProtocol

logger = logging.getLogger(__name__)

COUNTS_SAMPLE_WINDOW = timedelta(minutes=15)
STALL_CHECK_WINDOW = timedelta(minutes=5)
RUN_MILESTONE_INTERVAL = 25


def _fit_rate(samples: List[Tuple[datetime, float]]) -> float:
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
        fun_mode: bool = False,
        rng: Optional[random.Random] = None,
        sink: Optional[MonitorSinkProtocol] = None,
    ):
        self.instrument = instrument
        self.experiment_channel = experiment_channel
        self.beam_power = beam_power
        self.stall_minutes = stall_minutes
        self.fun_mode = fun_mode
        self._rng = rng or random.Random()
        self.sink = sink
        self.state = InstrumentState()

    def _flavour_rng(self) -> Optional[random.Random]:
        return self._rng if self.fun_mode else None

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

        if self.state.run_name and self.state.run_name != name:
            notification = run_started(
                self.instrument.name,
                name,
                self.state.run_name,
                time_now - self.state.run_started_at,
                self.state.current_counts,
                time_now,
                rng=self._flavour_rng(),
            )
            logger.info(f"New Run: {notification.to_plain_text()}")
            await self.experiment_channel.broadcast(notification)
            self.state.current_counts = 0
            self.state.end_notified = False
            self.state.collected_samples.clear()
            self.state.collection_stalled_since = None
            self.state.stall_warned = False

            if self.sink:
                total_runs = self.sink.record_run_completed(time_now)
                if self.fun_mode and total_runs % RUN_MILESTONE_INTERVAL == 0:
                    milestone = run_milestone(self.instrument.name, total_runs, time_now, rng=self._rng)
                    logger.info(f"Milestone: {milestone.to_plain_text()}")
                    await self.experiment_channel.broadcast(milestone)

        self.state.run_name = name
        self.state.run_started_at = time_now
        if self.sink:
            self.sink.update_run_name(name)

    async def handle_counts(self, text_val: Any, time_now: datetime) -> None:
        if _is_blank(text_val):
            return
        try:
            # "live_current/total_collected"; live current is already
            # tracked via the beam-current PVs, so only the total is used.
            parts = str(text_val).split("/")
            float(parts[0])  # validates the format; value unused
            total_collected = float(parts[1])
        except (IndexError, ValueError) as e:
            logger.warning(f"Failed to parse counts from '{text_val}': {e}")
            return

        self.state.current_counts = total_collected
        self.state.collected_samples.append((time_now, total_collected))
        self._prune_collected_samples(time_now)

        if self.sink:
            self.sink.update_counts(total_collected)

        counts_target = self.instrument.notify_counts
        if self.state.end_notified and total_collected < (counts_target - 25):
            self.state.end_notified = False

        if total_collected > counts_target and not self.state.end_notified:
            rate = _fit_rate(list(self.state.collected_samples))
            notification = run_finishing(
                self.instrument.name,
                self.state.run_name,
                total_collected,
                counts_target,
                rate,
                self._beam_state(),
                time_now,
                rng=self._flavour_rng(),
            )
            logger.info(f"Target Reached: {notification.to_plain_text()}")
            await self.experiment_channel.broadcast(notification)
            self.state.end_notified = True

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
                    self.instrument.name, self.instrument.beam_target, stalled_for, time_now
                )
                logger.info(f"Stall Warning: {notification.to_plain_text()}")
                await self.experiment_channel.broadcast(notification)
        else:
            self.state.collection_stalled_since = None
            self.state.stall_warned = False
