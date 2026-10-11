"""Run and counts tracking for one instrument, fed by BeamMonitor's WebSocket."""
import base64
import logging
import math
import random
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Deque, Optional, Sequence, Tuple

from isis_monitor.config import InstrumentConfig
from isis_monitor.daemon_state import DaemonState
from isis_monitor.messages import (
    collection_resumed, collection_stalled, experiment_finished, run_finishing, run_milestone, run_started,
    strip_controls,
)
from isis_monitor.notifiers import NotificationChannel

logger = logging.getLogger(__name__)

COUNTS_SAMPLE_WINDOW = timedelta(minutes=15)
STALL_CHECK_WINDOW = timedelta(minutes=5)
# How much counts history the rate needs before an early finishing card is
# sent on its ETA; until then the card waits for notify_counts itself.
ETA_MIN_HISTORY = timedelta(minutes=3)
ETA_CHECK_INTERVAL = timedelta(seconds=30)  # at most one rate fit per this, per instrument
RUN_MILESTONE_INTERVAL = 25
# DAE run states in which counts should rise, so flat counts are a stall.
# Others (SETUP, PAUSED, WAITING on run control, and the brief ones between)
# aren't checked; an automated run plan can pass through them in seconds.
COUNTING_RUN_STATES = ("RUNNING", "VETOING")
# A title change this soon after a counts reset started a run (e.g. one titled
# after its BEGIN) names that run rather than starting another; the card has
# already gone out under the old title.
RESET_RETITLE_WINDOW = timedelta(minutes=2)
# notify_counts = auto: a run's target is the median of the experiment's last
# AUTO_RUNS run totals, leaving out very short ones (e.g. a mistaken setup),
# once AUTO_MIN_RUNS are left; AUTO_HISTORY totals are kept to allow for them.
AUTO_HISTORY = 7
AUTO_RUNS = 5
AUTO_MIN_RUNS = 3
AUTO_SHORT_UAH = 1.0  # µA·h; a run below this, or AUTO_SHORT_FRACTION of
AUTO_SHORT_FRACTION = 0.25  # the median of all kept totals, is "very short"


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


def auto_target(totals: Sequence[float], fallback: float) -> Tuple[float, int]:
    """The target for a run from recent run totals (oldest first), and how
    many runs it came from: 0 means there weren't enough, so it's `fallback`."""
    if not totals:
        return fallback, 0
    floor = max(AUTO_SHORT_UAH, AUTO_SHORT_FRACTION * statistics.median(totals))
    kept = [t for t in totals if t >= floor][-AUTO_RUNS:]
    if len(kept) < AUTO_MIN_RUNS:
        return fallback, 0
    return round(statistics.median(kept), 1), len(kept)


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
    eta_checked_at: Optional[datetime] = None
    counts_reset_at: Optional[datetime] = None  # when a counts reset last started a run
    # The previous run's total, after a title change started a run, until
    # BEGIN resets the DAE's counts; -1 when not waiting for that reset.
    stale_counts: float = -1.0
    run_target: float = 0.0  # µA·h this run's finishing card is due at
    auto_runs: int = 0  # how many runs an auto run_target came from; 0 = the fallback
    # Completed runs' totals (oldest first), for auto targets
    run_totals: Deque[float] = field(default_factory=lambda: deque(maxlen=AUTO_HISTORY))
    rb_number: str = ""  # the experiment's; "" until known
    run_rb: str = ""  # rb_number when this run started; its total only counts for that experiment
    # The experiment's completed runs so far, for its summary card
    exp_since: Optional[datetime] = None  # when its RB number was first seen
    exp_partial: bool = True  # whether it had started before then (the daemon saw no change to it)
    exp_runs: int = 0
    exp_uah: float = 0.0

    run_state: str = ""  # IN:<NAME>:DAE:RUNSTATE_STR; "" until known
    # IN:<NAME>:DAE:RUNNUMBER, for cards sent mid-run (it changes at BEGIN,
    # in no fixed order with the title and counts); "" until known
    run_number: str = ""

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
        finish_warning_minutes: float = 15.0,
        flavour_rng: Optional[random.Random] = None,  # set only with fun_mode
        sink: Optional[DaemonState] = None,
    ):
        self.instrument = instrument
        self.experiment_channel = experiment_channel
        self.beam_power = beam_power
        self.stall_minutes = stall_minutes
        self.finish_warning_minutes = finish_warning_minutes
        self.flavour_rng = flavour_rng
        self.sink = sink
        self.state = InstrumentState(run_target=instrument.notify_counts)

    def _card_channel(self) -> str:
        """Teams channel for this instrument's cards; blank means the
        experiment channel's own name ("Experiment Updates")."""
        return self.instrument.name if self.instrument.channel == "instrument" else ""

    def _beam_state(self) -> str:
        return self.beam_power(self.instrument.beam_target)

    def _set_target(self) -> None:
        """Fix the current run's target: notify_counts, or with auto_counts,
        one from the recent run totals."""
        inst = self.instrument
        target, runs = inst.notify_counts, 0
        if inst.auto_counts:
            target, runs = auto_target(self.state.run_totals, inst.notify_counts)
            if target != self.state.run_target:
                source = f"the last {runs} runs" if runs else f"the fallback, until {AUTO_MIN_RUNS} runs are seen"
                logger.info(f"{inst.name} auto target {target:g} µA·h, from {source}")
        self.state.run_target, self.state.auto_runs = target, runs
        self._save_target()

    def _save_target(self) -> None:
        if self.sink:
            self.sink.update_target(
                self.instrument.name, self.state.run_target, self.state.auto_runs, list(self.state.run_totals)
            )

    def _save_experiment(self) -> None:
        if self.sink:
            s = self.state
            self.sink.update_experiment(
                self.instrument.name, s.rb_number, s.run_rb, s.exp_since, s.exp_partial, s.exp_runs, s.exp_uah
            )

    def handle_run_state(self, text: Any) -> None:
        self.state.run_state = strip_controls(str(text)).strip().upper()

    def handle_run_number(self, text: Any) -> None:
        number = strip_controls(str(text)).strip()
        self.state.run_number = number.lstrip("0") or number  # "00127920" -> "127920"

    async def handle_rb_number(self, text: Any, time_now: datetime) -> None:
        """`text` is IN:<NAME>:DAE:_RBNUMBER; a new experiment gets a summary
        card for the last one, and starts a new run history. "0" (an
        instrument with no experiment) and blanks are ignored, so a gap
        between experiments doesn't end one."""
        rb = strip_controls(str(text)).strip()
        state = self.state
        if rb in ("", "0") or rb == state.rb_number:
            return
        old_rb, notification = state.rb_number, None
        if old_rb:
            logger.info(f"{self.instrument.name} RB number {old_rb} -> {rb}: run history cleared")
            # The run in progress is usually the old experiment's last, ended
            # but only completed when the next one starts.
            last = state.current_counts if state.run_rb == old_rb and state.current_counts > 0 else 0.0
            runs = state.exp_runs + (last > 0)
            if runs and state.exp_since is not None:
                notification = experiment_finished(
                    self.instrument.name, old_rb, runs, state.exp_uah + last,
                    time_now - state.exp_since, state.exp_partial, time_now, channel=self._card_channel(),
                )
            state.exp_partial = False
        else:
            state.run_rb = rb  # the first seen; the run in progress is taken to be its
        state.run_totals.clear()  # also runs from before any RB number, e.g. while it was "0"
        state.rb_number = rb
        state.exp_since, state.exp_runs, state.exp_uah = time_now, 0, 0.0
        self._save_target()  # the run in progress keeps its target; the next is the new experiment's
        self._save_experiment()
        if notification:
            logger.info(f"Experiment finished: {notification.to_plain_text()}")
            await self.experiment_channel.broadcast(notification)

    async def handle_run_name(self, b64_data: Any, time_now: datetime) -> None:
        if _is_blank(b64_data):
            return
        try:
            name = strip_controls(base64.b64decode(b64_data).decode())  # e.g. NUL padding
        except Exception as e:
            logger.warning(f"Failed to decode run name b64: {e}")
            return
        if not name:
            return  # e.g. all NULs; treating it as a run would hide the next real one

        # PVWS re-sends the current title on every (re)subscribe, so a repeat
        # of the same name mustn't restart the run's clock; a new run that keeps
        # its title (e.g. "Hidden") is caught by handle_counts instead.
        if name == self.state.run_name and self.state.run_started_at is not None:
            return
        reset_at = self.state.counts_reset_at
        retitled = reset_at is not None and time_now - reset_at <= RESET_RETITLE_WINDOW
        if self.state.run_name and not retitled:
            await self._start_run(name, time_now, from_title=True)
            return
        # The first title seen, or the title of a run a counts reset just started.
        self.state.run_name = name
        self.state.counts_reset_at = None
        if not retitled:
            self.state.run_started_at = time_now
        if self.sink:
            self.sink.update_run_name(self.instrument.name, name)
        self._save_progress()

    async def _start_run(self, name: str, time_now: datetime, from_title: bool) -> None:
        """Send the new-run card for `name`, reporting on the run just ended."""
        prev_name, prev_duration, prev_counts = (
            self.state.run_name, time_now - self.state.run_started_at, self.state.current_counts
        )
        if not from_title:
            self.state.stale_counts = -1.0
        elif self.state.stale_counts < 0:  # kept through title edits before BEGIN
            self.state.stale_counts = self.state.current_counts
        self.state.counts_reset_at = None if from_title else time_now
        if self.state.current_counts > 0 and self.state.run_rb == self.state.rb_number:  # not unread or retitled
            if self.instrument.auto_counts:
                self.state.run_totals.append(self.state.current_counts)
            if self.state.rb_number:
                self.state.exp_runs += 1
                self.state.exp_uah += self.state.current_counts
        self.state.run_rb = self.state.rb_number
        self.state.current_counts = 0
        self.state.end_notified = False  # saved below with the new run's start
        self.state.collected_samples.clear()
        self.state.collection_stalled_since = None
        self.state.stall_warned = False

        total_runs = self.sink.record_run_completed(self.instrument.name, time_now) if self.sink else 0
        # Saved before any await, so a snapshot can't pair the new
        # completion count with the old run (which a restart would re-report).
        self.state.run_name = name
        self.state.run_started_at = time_now
        self._set_target()
        self._save_experiment()
        if self.sink:
            self.sink.update_run_name(self.instrument.name, name)
            self.sink.update_counts(self.instrument.name, 0)
        self._save_progress()

        if not self.instrument.auto_counts:
            target_note = ""
        elif self.state.auto_runs:
            target_note = f"auto, from {self.state.auto_runs} runs"
        else:
            target_note = f"auto's fallback, until {AUTO_MIN_RUNS} runs"
        notification = run_started(
            self.instrument.name, name, prev_name, prev_duration, prev_counts, time_now,
            self.state.run_target, target_note, rng=self.flavour_rng, channel=self._card_channel(),
        )
        logger.info(f"New Run: {notification.to_plain_text()}")
        await self.experiment_channel.broadcast(notification)

        if self.flavour_rng and total_runs and total_runs % RUN_MILESTONE_INTERVAL == 0:
            milestone = run_milestone(
                self.instrument.name, total_runs, time_now, rng=self.flavour_rng, channel=self._card_channel()
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

        stale = self.state.stale_counts
        if stale >= 0:
            # A new title came before BEGIN: PVWS may re-send the old total (on
            # reconnect) until BEGIN's reset, which mustn't start another run.
            # (So if that total was under ~1, the new run's first ~1 is ignored.)
            if abs(total_collected - stale) <= 1:
                return
            self.state.stale_counts = -1.0
            self._save_progress()
        elif (
            # Counts only fall when the DAE resets them for a new run, which is
            # the only sign of one whose title is unchanged (e.g. "Hidden").
            self.state.run_name
            and self.state.run_started_at is not None
            and total_collected < self.state.current_counts - 1
        ):
            await self._start_run(self.state.run_name, time_now, from_title=False)

        self.state.current_counts = total_collected
        samples = self.state.collected_samples
        samples.append((time_now, total_collected))
        self._prune_collected_samples(time_now)

        if self.sink:
            self.sink.update_counts(self.instrument.name, total_collected)

        counts_target = self.state.run_target
        if self.state.end_notified:
            return

        remaining = counts_target - total_collected
        if remaining >= 0:
            # Only an ETA can make the card due before the target; the fit is
            # rate-limited, as counts can update every few seconds.
            checked = self.state.eta_checked_at
            if (
                self.finish_warning_minutes <= 0
                or samples[-1][0] - samples[0][0] < ETA_MIN_HISTORY
                or (checked is not None and time_now - checked < ETA_CHECK_INTERVAL)
            ):
                return
            self.state.eta_checked_at = time_now
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
                rng=self.flavour_rng,
                channel=self._card_channel(),
                auto=self.state.auto_runs > 0,
                run_number=self.state.run_number,
            )
            logger.info(f"Run finishing: {notification.to_plain_text()}")
            await self.experiment_channel.broadcast(notification)
            self.state.end_notified = True
            self._save_progress()

    def _save_progress(self) -> None:
        if self.sink:
            self.sink.update_run_progress(
                self.instrument.name, self.state.run_started_at, self.state.end_notified,
                self.state.stale_counts,
            )

    def restore(self, saved: dict) -> None:
        """Seed state from a DaemonState snapshot entry after a restart. PVWS
        re-sends the same run title on connect, which then changes nothing."""
        if self.instrument.auto_counts:  # a fixed target's old totals would be stale by the time auto is on
            self.state.run_totals.extend(saved.get("run_totals", ()))
        self.state.rb_number = str(saved.get("rb_number", ""))
        self.state.run_rb = str(saved.get("run_rb", ""))
        since = saved.get("exp_since")
        self.state.exp_since = datetime.fromisoformat(since) if since else None
        self.state.exp_partial = bool(saved.get("exp_partial", True))
        self.state.exp_runs = int(saved.get("exp_runs", 0))
        self.state.exp_uah = float(saved.get("exp_uah", 0.0))
        if saved.get("target") is None:  # DaemonState drops it if notify_counts has changed
            self._set_target()
        else:  # the run keeps the target it started with, even if its history has since been cleared
            self.state.run_target = float(saved["target"])
            self.state.auto_runs = int(saved.get("auto_runs", 0))
            self._save_target()
        if not saved.get("run_name") or not saved.get("run_started_at"):
            return
        self.state.run_name = strip_controls(str(saved["run_name"]))  # as handle_run_name stores it
        self.state.run_started_at = datetime.fromisoformat(saved["run_started_at"])
        self.state.current_counts = float(saved.get("counts", -1.0))
        # DaemonState has already cleared this if notify_counts has changed.
        self.state.end_notified = bool(saved.get("end_notified", False))
        self.state.stale_counts = float(saved.get("stale_counts", -1.0))

    def reset_stall_clock(self) -> None:
        """While counts can't be judged (e.g. PVWS is down, or the run paused).
        A stall already warned of keeps its start, for the resumed card."""
        if not self.state.stall_warned:
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
        stall. A run that has reached its target has usually ended (the
        title, and so run_name, stays until the next run), so it isn't checked;
        one merely forecast to finish soon still is. Once the DAE's run state
        is known, only COUNTING_RUN_STATES are checked, which also covers runs
        ended short of their target.

        Movement is judged over STALL_CHECK_WINDOW rather than since the last
        tick: the counts PV can plausibly update in batches, so comparing
        only the last ~60s could make a perfectly healthy, just-batchy source
        look stalled every time a batch hadn't landed yet in that particular
        minute.
        """
        if not self.state.run_name or self.state.current_counts >= self.state.run_target:
            return
        if self.state.run_state and self.state.run_state not in COUNTING_RUN_STATES:
            # e.g. a run ended short of its target, or paused: flat counts are expected.
            self.reset_stall_clock()
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
                    channel=self._card_channel(), run_number=self.state.run_number,
                )
                logger.info(f"Stall Warning: {notification.to_plain_text()}")
                await self.experiment_channel.broadcast(notification)
        else:
            if self.state.stall_warned:
                notification = collection_resumed(
                    self.instrument.name, time_now - self.state.collection_stalled_since, time_now,
                    channel=self._card_channel(),
                )
                logger.info(f"Collection resumed: {notification.to_plain_text()}")
                await self.experiment_channel.broadcast(notification)
            self.state.collection_stalled_since = None
            self.state.stall_warned = False
