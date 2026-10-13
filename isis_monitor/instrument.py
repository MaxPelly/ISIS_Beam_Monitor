"""Run and counts tracking for one instrument, fed by BeamMonitor's WebSocket."""
import base64
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Deque, Dict, Optional, Sequence, Tuple

from isis_monitor.config import InstrumentConfig
from isis_monitor.daemon_state import DaemonState
from isis_monitor.messages import (
    collection_resumed, collection_stalled, experiment_finished, run_finishing, run_started,
    strip_controls,
)
from isis_monitor.notifiers import NotificationChannel
from isis_monitor.runs import AUTO_HISTORY, AUTO_MIN_RUNS, RunTracker, auto_target, pv_handlers as run_pv_handlers

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
# sent on its ETA; until then the card waits for notify_counts itself.
ETA_MIN_HISTORY = timedelta(minutes=3)
ETA_CHECK_INTERVAL = timedelta(seconds=30)  # at most one rate fit per this, per instrument
# DAE run states in which counts should rise, so flat counts are a stall.
# Others (SETUP, PAUSED, WAITING on run control, and the brief ones between)
# aren't checked; an automated run plan can pass through them in seconds.
COUNTING_RUN_STATES = ("RUNNING", "VETOING")
# A title change this soon after a counts reset started a run (e.g. one titled
# after its BEGIN) names that run rather than starting another; the card has
# already gone out under the old title.
RESET_RETITLE_WINDOW = timedelta(minutes=2)
# A run started this soon before an RB number change is taken to be the new
# experiment's: PVWS sends the PVs of an automated run's start in no fixed order.
NEW_EXPERIMENT_RUN_WINDOW = timedelta(seconds=30)


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


# Bumped when to_snapshot()'s layout changes; other entries are ignored.
SNAPSHOT_FORMAT = 2


def _typed(saved: dict, key: str, kind: type):
    """saved[key] if it's a `kind` (ints count as floats); raises otherwise."""
    value = saved[key]
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise TypeError(f"{key} isn't a {kind.__name__}")
    if kind is float and not math.isfinite(value):
        raise ValueError(f"{key} isn't finite")
    return value


def _finite(values: list) -> list:
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values):
        raise ValueError("run_totals must be finite numbers")
    return [float(v) for v in values]


def _aware(text: object) -> Optional[datetime]:
    """A saved ISO timestamp (UTC if it has no zone), or None."""
    if text is None:
        return None
    ts = datetime.fromisoformat(text)  # raises TypeError or ValueError if it isn't one
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


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
    rb_number: str = ""  # the experiment's, or "0" for none; "" until known
    run_rb: str = ""  # rb_number when this run started; its total only counts for that experiment
    # The experiment's completed runs so far, for its summary card
    exp_since: Optional[datetime] = None  # when its RB number was first seen
    exp_partial: bool = True  # whether it had started before then (the daemon saw no change to it)
    exp_runs: int = 0
    exp_uah: float = 0.0
    # Until the first RB number after (re)subscribing: a change then happened
    # unwatched, so both experiments' figures are only "at least"
    rb_resync: bool = True

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
        self._instrument = instrument
        self.default_target = default_target
        self.beam_target = instrument.beam_target or default_target
        self._station_warned = False
        self.experiment_channel = experiment_channel
        self.beam_power = beam_power
        self.stall_minutes = stall_minutes
        self.finish_warning_minutes = finish_warning_minutes
        self.sink = sink
        self.state = InstrumentState(run_target=instrument.notify_counts)
        # Run detection by run number, in shadow: it only logs its decisions
        # (with "[shadow]") for comparing with this tracker's cards, until it
        # replaces the title and counts heuristics.
        self.runs = RunTracker(instrument, log_prefix="[shadow] ")

    @property
    def instrument(self) -> InstrumentConfig:
        return self._instrument

    @instrument.setter
    def instrument(self, instrument: InstrumentConfig) -> None:
        """E.g. with a new PV prefix from CS:INSTLIST, for the shadow too."""
        self._instrument = self.runs.instrument = instrument

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

    def pv_handlers(self) -> Dict[str, PVHandler]:
        """This instrument's PVs, each with its update handler. RB numbers come
        first (see handle_rb_number), then titles before counts, so a run that
        changed while disconnected is reported under its new title rather than
        started by its counts reset; the server may still reorder them."""
        inst = self.instrument
        handlers = {
            inst.rb_number_pv: on_field("text", self.handle_rb_number),
            inst.run_number_pv: on_field("text", lambda text, _now: self.handle_run_number(text)),
            inst.run_name_pv: on_field("b64byt", self.handle_run_name),
            inst.counts_pv: on_field("value", self.handle_counts),
            inst.run_state_pv: on_field("text", lambda text, _now: self.handle_run_state(text)),
        }
        shadow = run_pv_handlers(self.runs)
        return {pv: self._with_shadow(handlers.get(pv), shadow[pv]) for pv in shadow}

    def _with_shadow(self, handler: Optional[PVHandler], shadow: Callable[[dict, datetime], None]) -> PVHandler:
        """`handler`, after the shadow's own; a failure in the shadow is only logged."""
        async def both(update: dict, time_now: datetime) -> None:
            try:
                shadow(update, time_now)
            except Exception:
                logger.exception(f"[shadow] {self.instrument.name}: failed to handle {update.get('pv')}")
            if handler:
                await handler(update, time_now)
        return both

    def handle_run_state(self, text: Any) -> None:
        state = strip_controls(str(text)).strip().upper()
        if self.sink and state != self.state.run_state:
            self.sink.on_change()
        self.state.run_state = state

    def handle_run_number(self, text: Any) -> None:
        number = strip_controls(str(text)).strip()
        number = number.lstrip("0") or number  # "00127920" -> "127920"
        if self.sink and number != self.state.run_number:
            self.sink.on_change()
        self.state.run_number = number

    async def handle_rb_number(self, text: Any, time_now: datetime) -> None:
        """`text` is IN:<NAME>:DAE:_RBNUMBER, "0" while the instrument has no
        experiment. A change ends the experiment, if any, with a summary card,
        and starts a new run history."""
        rb = strip_controls(str(text)).strip()
        state = self.state
        if not rb:
            return
        unwatched, state.rb_resync = state.rb_resync, False
        if rb == state.rb_number:
            return
        old_rb, notification, new_first_run = state.rb_number, None, False
        if old_rb:
            logger.info(f"{self.instrument.name} RB number {old_rb} -> {rb}: run history cleared")
            # The run in progress is usually the old experiment's last, ended
            # but only completed when the next one starts; unless it's just
            # begun (or has nothing collected), when it's the new one's first.
            started = state.run_started_at
            fresh = started is not None and time_now - started <= NEW_EXPERIMENT_RUN_WINDOW
            last = state.current_counts if state.run_rb == old_rb and state.current_counts > 0 and not fresh else 0.0
            if not last and state.run_rb == old_rb:
                state.run_rb, new_first_run = rb, True
            else:  # counted for an older experiment (or no one's), even if its RB number comes back
                state.run_rb = ""
            runs = state.exp_runs + (last > 0)
            if old_rb != "0" and runs and state.exp_since is not None:  # "0" was no experiment
                notification = experiment_finished(
                    self.instrument.name, rb_number=old_rb, runs=runs, uah=state.exp_uah + last,
                    duration=time_now - state.exp_since, partial=state.exp_partial or unwatched, time_now=time_now,
                )
            state.exp_partial = unwatched
        else:
            state.run_rb = rb  # the first seen; the run in progress is taken to be its
        state.run_totals.clear()  # also runs from before any RB number
        state.rb_number = rb
        state.exp_since, state.exp_runs, state.exp_uah = time_now, 0, 0.0
        if new_first_run:  # its target is the new experiment's too: (auto's) fallback
            self._set_target()
        # Otherwise the run in progress keeps its target; the next is the new experiment's.
        if notification:
            logger.info(f"Experiment finished: {notification.to_summary()}")
            self.experiment_channel.broadcast(notification)

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

        if self.sink:
            self.sink.record_run_completed(self.instrument.name, time_now, "", max(prev_counts, 0.0))
        self.state.run_name = name
        self.state.run_started_at = time_now
        self._set_target()

        if not self.instrument.auto_counts:
            target_note = ""
        elif self.state.auto_runs:
            target_note = f"auto, from {self.state.auto_runs} runs"
        else:
            target_note = f"auto's fallback, until {AUTO_MIN_RUNS} runs"
        notification = run_started(
            self.instrument.name, new_run_name=name, prev_run_name=prev_name, prev_duration=prev_duration,
            prev_counts=prev_counts, time_now=time_now, target=self.state.run_target, target_note=target_note,
        )
        logger.info(f"New Run: {notification.to_summary()}")
        self.experiment_channel.broadcast(notification)

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
                run_name=self.state.run_name,
                counts_collected=total_collected,
                counts_target=counts_target,
                rate_per_second=rate,
                instrument_state=self._beam_state(),
                time_now=time_now,
                auto=self.state.auto_runs > 0,
                run_number=self.state.run_number,
            )
            logger.info(f"Run finishing: {notification.to_summary()}")
            self.experiment_channel.broadcast(notification)
            self.state.end_notified = True

    def to_snapshot(self) -> dict:
        """What a restart needs to carry on: the run in progress (so its clock
        and finishing card aren't reset), the experiment and the run history."""
        s = self.state
        return {
            "format": SNAPSHOT_FORMAT,
            "notify_counts": self.instrument.notify_counts,
            "auto": self.instrument.auto_counts,
            "run_name": s.run_name,
            "run_started_at": s.run_started_at.isoformat() if s.run_started_at else None,
            "counts": s.current_counts,
            "end_notified": s.end_notified,
            "stale_counts": s.stale_counts,
            "target": s.run_target,
            "auto_runs": s.auto_runs,
            "run_totals": list(s.run_totals),
            "rb_number": s.rb_number,
            "run_rb": s.run_rb,
            "exp_since": s.exp_since.isoformat() if s.exp_since else None,
            "exp_partial": s.exp_partial,
            "exp_runs": s.exp_runs,
            "exp_uah": s.exp_uah,
            "shadow": self._shadow_snapshot(),
        }

    def _shadow_snapshot(self) -> Optional[dict]:
        try:
            return self.runs.to_snapshot()
        except Exception:  # never let the shadow stop the state being saved
            logger.exception(f"[shadow] {self.instrument.name}: failed to save its state")
            return None

    def view(self) -> dict:
        """The snapshot plus what clients show: the effective beam target, and
        the DAE's run number and state."""
        # The run number and state as the shadow sees them: "" while unknown, e.g. its IOC is down.
        return {**self.to_snapshot(), "beam_target": self.beam_target,
                "run_number": self.runs.run_number, "run_state": self.runs.run_state}

    def from_snapshot(self, saved: object) -> None:
        """Restore state saved by to_snapshot() after a restart; a malformed or
        older-format entry is logged and ignored. PVWS re-sends the same run
        title on connect, which then changes nothing."""
        name = self.instrument.name
        if not isinstance(saved, dict) or saved.get("format") != SNAPSHOT_FORMAT:
            logger.warning(f"{name}: ignoring saved state from an older version")
            return
        try:
            state = InstrumentState(
                run_name=strip_controls(_typed(saved, "run_name", str)),
                run_started_at=_aware(saved.get("run_started_at")),
                current_counts=_typed(saved, "counts", float),
                end_notified=_typed(saved, "end_notified", bool),
                stale_counts=_typed(saved, "stale_counts", float),
                run_target=_typed(saved, "target", float),
                auto_runs=_typed(saved, "auto_runs", int),
                run_totals=deque(_finite(_typed(saved, "run_totals", list)), maxlen=AUTO_HISTORY),
                rb_number=_typed(saved, "rb_number", str),
                run_rb=_typed(saved, "run_rb", str),
                exp_since=_aware(saved.get("exp_since")),
                exp_partial=_typed(saved, "exp_partial", bool),
                exp_runs=_typed(saved, "exp_runs", int),
                exp_uah=_typed(saved, "exp_uah", float),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(f"{name}: ignoring malformed saved state ({exc})")
            return
        if not state.run_started_at:
            state.run_name = ""
        # A finishing card sent for, or a target from, another notify_counts
        # (changed by the config save behind this restart) doesn't cover the
        # new one, which may still be ahead.
        same_target = (saved.get("notify_counts") == self.instrument.notify_counts
                       and saved.get("auto") == self.instrument.auto_counts)
        if not self.instrument.auto_counts:  # a fixed target's history would be stale by the time auto is on
            state.run_totals.clear()
        state.end_notified = state.end_notified and same_target
        self.state = state
        if not same_target:  # otherwise the run keeps the target it started with
            self._set_target()
        if "shadow" in saved:
            self.runs.from_snapshot(saved["shadow"], same_target)

    def resubscribed(self) -> None:
        """PVWS is about to re-send every value, which may have changed unseen."""
        self.state.rb_resync = True
        try:
            self.runs.resubscribed(datetime.now(timezone.utc))
        except Exception:
            logger.exception(f"[shadow] {self.instrument.name}: failed to resubscribe")

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
                    self.instrument.name, beam_target=self.beam_target, stalled_for=stalled_for,
                    time_now=time_now, run_number=self.state.run_number,
                )
                logger.info(f"Stall Warning: {notification.to_summary()}")
                self.experiment_channel.broadcast(notification)
        else:
            if self.state.stall_warned:
                notification = collection_resumed(
                    self.instrument.name, stalled_for=time_now - self.state.collection_stalled_since, time_now=time_now,
                )
                logger.info(f"Collection resumed: {notification.to_summary()}")
                self.experiment_channel.broadcast(notification)
            self.state.collection_stalled_since = None
            self.state.stall_warned = False
