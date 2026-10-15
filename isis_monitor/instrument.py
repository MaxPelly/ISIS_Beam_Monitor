"""One instrument's run cards: its RunTracker (runs.py) decides when runs
start, end and begin again; this sends their cards, the finishing card as a
run nears its target, and stall warnings."""
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Deque, Dict, Optional, Sequence, Tuple

from isis_monitor.config import InstrumentConfig
from isis_monitor.daemon_state import DaemonState
from isis_monitor.messages import (
    collection_resumed,
    collection_stalled,
    experiment_finished,
    run_finishing,
    run_restarted,
    run_started,
)
from isis_monitor.notifiers import NotificationChannel
from isis_monitor.runs import AUTO_MIN_RUNS, RunInfo, RunTracker
from isis_monitor.runs import pv_handlers as run_pv_handlers

logger = logging.getLogger(__name__)

PVHandler = Callable[[dict, datetime], Awaitable[None]]


def on_field(field: str, handle: Callable[[Any, datetime], Optional[Awaitable[None]]]) -> PVHandler:
    """A PVWS update handler calling `handle(update[field], time_now)`. PVWS
    also sends partial updates (e.g. only a new severity), which are skipped."""
    async def handler(update: dict, time_now: datetime) -> None:
        if field in update and (result := handle(update[field], time_now)) is not None:
            await result
    return handler


COUNTS_SAMPLE_WINDOW = timedelta(minutes=15)
STALL_CHECK_WINDOW = timedelta(minutes=5)
# How much counts history the rate needs before an early finishing card is
# sent on its ETA; until then the card waits for the target itself.
ETA_MIN_HISTORY = timedelta(minutes=3)
ETA_CHECK_INTERVAL = timedelta(seconds=30)  # at most one rate fit per this, per instrument
# DAE run states in which counts should rise, so flat counts are a stall.
# Others (SETUP, PAUSED, WAITING on run control, and the brief ones between)
# aren't checked; an automated run plan can pass through them in seconds.
COUNTING_RUN_STATES = ("RUNNING", "VETOING")
# Bumped when to_snapshot()'s layout changes; other entries are ignored.
SNAPSHOT_FORMAT = 3


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
    numer = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    return numer / denom


def shown(number: str) -> str:
    """A run number as the cards show it: "00127920" -> "127920"."""
    return number.lstrip("0") or number


class InstrumentTracker:
    """One instrument's runs (by run number) and their notifications.

    `beam_power` looks up a beam target's current power label, so run cards
    and the stall check can report on the instrument's beam: `beam_target`,
    the config's if it sets one, else its target station from CS:INSTLIST
    (see set_station), else `default_target` ([PVS] instrument_target).
    """
    def __init__(
        self,
        instrument: InstrumentConfig,
        experiment_channel: NotificationChannel,
        beam_power: Callable[[str], str],
        stall_minutes: float,
        finish_warning_minutes: float = 15.0,
        sink: Optional[DaemonState] = None,
        default_target: str = "TS1",
    ):
        self.default_target = default_target
        self.beam_target = instrument.beam_target or default_target
        self._station_warned = False
        self.experiment_channel = experiment_channel
        self.beam_power = beam_power
        self.stall_minutes = stall_minutes
        self.finish_warning_minutes = finish_warning_minutes
        self.sink = sink
        self.runs = RunTracker(instrument, listener=self)
        # The run's progress (live only): (time, µA·h) credited to it, for its rate
        self.samples: Deque[Tuple[datetime, float]] = deque()
        self.eta_checked_at: Optional[datetime] = None
        self.stalled_since: Optional[datetime] = None
        self.stall_warned = False

    @property
    def instrument(self) -> InstrumentConfig:
        return self.runs.instrument

    @instrument.setter
    def instrument(self, instrument: InstrumentConfig) -> None:
        """E.g. with a new PV prefix from CS:INSTLIST."""
        self.runs.instrument = instrument

    def set_station(self, station: Optional[str]) -> None:
        """The instrument's target station from CS:INSTLIST, or None if it
        isn't on TS1, TS2 or MUON."""
        if self.instrument.beam_target:
            return
        if station is None and not self._station_warned:
            self._station_warned = True
            logger.warning(f"{self.instrument.name}: CS:INSTLIST doesn't put it on TS1, TS2 or MUON; using "
                           f"{self.default_target} ([PVS] instrument_target) unless its beam_target is set")
        self.beam_target = station or self.default_target

    def _beam_state(self) -> str:
        return self.beam_power(self.beam_target)

    def pv_handlers(self) -> Dict[str, PVHandler]:
        """This instrument's PVs, each with its update handler."""
        handlers = run_pv_handlers(self.runs)
        counts_pv = self.instrument.counts_pv

        def wrap(pv: str, handle: Callable[[dict, datetime], None]) -> PVHandler:
            async def handler(update: dict, time_now: datetime) -> None:
                handle(update, time_now)
                if pv == counts_pv and "value" in update and self.runs.counts is not None and self.runs.credited():
                    self._counted(time_now)
            return handler
        return {pv: wrap(pv, handle) for pv, handle in handlers.items()}

    # --- RunTracker's decisions (its RunListener) -------------------------

    def run_started(self, run: RunInfo, previous: Optional[RunInfo], silent: bool) -> None:
        self._reset_progress()
        if silent:  # e.g. under way when the daemon first started: already begun, unannounced
            return
        if not self.instrument.auto_counts:
            target_note = ""
        elif run.auto_runs:
            target_note = f"auto, from {run.auto_runs} runs"
        else:
            target_note = f"auto's fallback, until {AUTO_MIN_RUNS} runs"
        notification = run_started(
            self.instrument.name, run_number=shown(run.number), new_run_name=run.title, time_now=run.started_at,
            target=run.target, target_note=target_note,
            prev_run_name=previous.title if previous else None,
            prev_duration=previous.ended_at - previous.started_at if previous and previous.ended_at else None,
            prev_counts=previous.last_counts if previous and previous.final_known else -1.0,
        )
        logger.info(f"New Run: {notification.to_summary()}")
        self.experiment_channel.broadcast(notification)

    def run_completed(self, run: RunInfo) -> None:
        """Recorded for the daily summary, including a run adopted as it ended
        (never announced, and perhaps without its counts)."""
        if self.sink:
            self.sink.record_run_completed(self.instrument.name, run.ended_at, shown(run.number),
                                           max(run.last_counts, 0.0))

    def run_rebegun(self, run: RunInfo, previous_title: str) -> None:
        """An abort and a re-begin under the same number: a card only if it was
        retitled (e.g. a new temperature), as for a new run in all but number."""
        self._reset_progress()
        if run.title == previous_title:
            return
        notification = run_restarted(self.instrument.name, run_number=shown(run.number), run_name=run.title,
                                     time_now=run.started_at)
        logger.info(f"Run restarted: {notification.to_summary()}")
        self.experiment_channel.broadcast(notification)

    def experiment_finished(self, rb: str, runs: int, uah: float, since: datetime, partial: bool,
                            now: datetime) -> None:
        notification = experiment_finished(self.instrument.name, rb_number=rb, runs=runs, uah=uah,
                                           duration=now - since, partial=partial, time_now=now)
        logger.info(f"Experiment finished: {notification.to_summary()}")
        self.experiment_channel.broadcast(notification)

    # --- Progress: the finishing card and stalls -------------------------

    def _reset_progress(self) -> None:
        self.samples.clear()
        self.eta_checked_at, self.stalled_since, self.stall_warned = None, None, False

    def _counted(self, time_now: datetime) -> None:
        """Counts credited to the run: send its finishing card when it reaches
        its target, or is due to within finish_warning_minutes."""
        run = self.runs.run
        self.samples.append((time_now, run.last_counts))
        cutoff = time_now - COUNTS_SAMPLE_WINDOW
        # Keeping the last one from before the window: after a long stall (flat
        # counts don't update) it's the baseline the resumed card needs.
        while len(self.samples) > 1 and self.samples[1][0] < cutoff:
            self.samples.popleft()
        if run.end_notified:
            return
        remaining = run.target - run.last_counts
        if remaining >= 0:
            # Only an ETA can make the card due before the target; the fit is
            # rate-limited, as counts can update every few seconds.
            checked = self.eta_checked_at
            if (self.finish_warning_minutes <= 0 or self.samples[-1][0] - self.samples[0][0] < ETA_MIN_HISTORY
                    or (checked is not None and time_now - checked < ETA_CHECK_INTERVAL)):
                return
            self.eta_checked_at = time_now
        rate = _fit_rate(self.samples)
        if remaining < 0 or (rate > 0 and remaining / rate <= self.finish_warning_minutes * 60):
            notification = run_finishing(
                self.instrument.name, run_name=run.title, counts_collected=run.last_counts, counts_target=run.target,
                rate_per_second=rate, instrument_state=self._beam_state(), time_now=time_now,
                auto=run.auto_runs > 0, run_number=shown(run.number),
            )
            logger.info(f"Run finishing: {notification.to_summary()}")
            self.experiment_channel.broadcast(notification)
            run.end_notified = True

    def reset_stall_clock(self) -> None:
        """While counts can't be judged (e.g. PVWS is down, or the run paused).
        A stall already warned of keeps its start, for the resumed card."""
        if not self.stall_warned:
            self.stalled_since = None

    async def check_collection_progress(self, time_now: datetime) -> None:
        """Detect stalled data collection (called roughly every 60s): only while
        a run is under way in COUNTING_RUN_STATES (an unknown state, e.g. its IOC
        is down, can't be judged). Movement is judged over STALL_CHECK_WINDOW, as
        the counts PV can update in batches."""
        runs, run = self.runs, self.runs.run
        if not run or run.number != runs.run_number or runs.last_completed == run.number:
            return
        if runs.run_state not in COUNTING_RUN_STATES or run.last_counts <= 0:
            # (Nothing ever collected, as on muon instruments, whose µA·h stay 0, isn't a stall.)
            self.reset_stall_clock()
            return
        baseline = None  # the latest sample at or before the window's start
        for t, value in self.samples:
            if t > time_now - STALL_CHECK_WINDOW:
                break
            baseline = value
        if baseline is None:
            return  # not enough history yet
        if run.last_counts <= baseline:
            if self.stalled_since is None:
                self.stalled_since = time_now
            stalled_for = time_now - self.stalled_since
            if (stalled_for >= timedelta(minutes=self.stall_minutes) and self._beam_state() != "off"
                    and not self.stall_warned):
                self.stall_warned = True
                notification = collection_stalled(self.instrument.name, beam_target=self.beam_target,
                                                  stalled_for=stalled_for, time_now=time_now,
                                                  run_number=shown(run.number))
                logger.info(f"Stall Warning: {notification.to_summary()}")
                self.experiment_channel.broadcast(notification)
        else:
            if self.stall_warned:
                notification = collection_resumed(self.instrument.name, stalled_for=time_now - self.stalled_since,
                                                  time_now=time_now)
                logger.info(f"Collection resumed: {notification.to_summary()}")
                self.experiment_channel.broadcast(notification)
            self.stalled_since, self.stall_warned = None, False

    # --- State ---------------------------------------------------------------

    def to_snapshot(self) -> dict:
        """What a restart needs to carry on: the run tracker's state."""
        return {"format": SNAPSHOT_FORMAT, "notify_counts": self.instrument.notify_counts,
                "auto": self.instrument.auto_counts, "runs": self.runs.to_snapshot()}

    def view(self) -> dict:
        """The snapshot plus what clients show: the run (the last, between
        runs), its progress, the effective beam target, and the DAE's run
        number and state ("" while unknown, e.g. its IOC is down)."""
        run = self.runs.run
        return {
            **self.to_snapshot(), "beam_target": self.beam_target,
            "run_number": shown(self.runs.run_number), "run_state": self.runs.run_state,
            "run_name": run.title if run else "", "run_started_at": run.started_at.isoformat() if run else None,
            "counts": run.last_counts if run else -1.0, "target": run.target if run else self.instrument.notify_counts,
            "auto_runs": run.auto_runs if run else 0, "rb_number": self.runs.rb,
        }

    def from_snapshot(self, saved: object) -> None:
        """Restore state saved by to_snapshot() after a restart; a malformed or
        older-format entry is logged and ignored."""
        name = self.instrument.name
        if isinstance(saved, dict) and saved.get("format") == 2 and "shadow" in saved:
            # 1.3.0rc's: the shadow's state is now the live one, with whether the
            # run in progress has had its finishing card, which the shadow never sent.
            runs = saved["shadow"]
            if isinstance(runs, dict) and isinstance(runs.get("run"), dict) and not runs["run"].get("ended_at"):
                runs = {**runs, "run": {**runs["run"], "end_notified": saved.get("end_notified") is True}}
            saved = {**saved, "runs": runs}
        elif not isinstance(saved, dict) or saved.get("format") != SNAPSHOT_FORMAT:
            logger.warning(f"{name}: ignoring saved state from an older version")
            return
        # A finishing card sent for, or a target from, another notify_counts
        # (changed by the config save behind this restart) doesn't cover the
        # new one, which may still be ahead.
        same_target = (saved.get("notify_counts") == self.instrument.notify_counts
                       and saved.get("auto") == self.instrument.auto_counts)
        self.runs.from_snapshot(saved.get("runs"), same_target)

    def resubscribed(self, time_now: datetime) -> None:
        """PVWS is about to re-send every value, which may have changed unseen."""
        self.runs.resubscribed(time_now)
