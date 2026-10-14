"""Run detection by run number.

A run is identified by IN:<NAME>:DAE:RUNNUMBER, which the DAE moves on (+1)
when a run ends (ENDING -> SETUP); the next run begins under the new number
once the run state is active. An abort doesn't change the number: the run is
begun again under it, with a new DAE:STARTTIME. Live data (13 Oct 2026, 166
run changes) showed that PVWS re-sends every PV on (re)subscribe in no fixed
order, and that a disconnected IOC, or the whole gateway, sends NaN for
everything, so nothing here depends on the order of updates.

RunTracker is synchronous and driven by each update's time, so recorded
sequences replay deterministically; its one timer (the settle before a start)
calls tick().
"""
import asyncio
import base64
import logging
import math
import statistics
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Deque, Optional, Protocol, Tuple

from isis_monitor.config import InstrumentConfig
from isis_monitor.messages import UK_TZ, strip_controls

logger = logging.getLogger(__name__)

# Run states in which a run is under way, if perhaps paused or saving: one
# seen in any of them under a new number has begun.
ACTIVE_STATES = frozenset({"BEGINNING", "RUNNING", "VETOING", "PAUSING", "PAUSED", "RESUMING", "WAITING", "SAVING"})
IDLE_STATES = frozenset({"SETUP", "ENDING", "ABORTING", "PROCESSING"})
# A begin can fail (BEGINNING -> SETUP after 5s, as on EMU), so a run only
# starts once it's past BEGINNING.
STARTED_STATES = ACTIVE_STATES - {"BEGINNING"}
# Counts still belong to the run while it ends (they don't move then).
CREDITED_STATES = ACTIVE_STATES | {"ENDING"}
# A run is only started once it's been under way this long, which absorbs
# repeated and reordered updates. Real runs as short as 2.6s were seen.
SETTLE = timedelta(seconds=1)
# An RB number change this soon after a run began makes it the new experiment's
# first run (counts arrive within a second of RUNNING, so they can't tell).
NEW_EXPERIMENT_WINDOW = timedelta(seconds=30)
REPLAY_TIMEOUT = timedelta(seconds=10)  # after a resubscribe, at most this long without RUNNUMBER and RUNSTATE
START_TIME_FORMAT = "%a %d-%b-%Y %H:%M:%S"  # DAE:STARTTIME, UK local time, e.g. "Tue 13-Oct-2026 10:49:43"
# notify_counts = auto: a run's target is the median of the experiment's last
# AUTO_RUNS run totals, leaving out very short ones (e.g. a mistaken setup),
# once AUTO_MIN_RUNS are left; AUTO_HISTORY totals are kept to allow for them.
AUTO_HISTORY = 7
AUTO_RUNS = 5
AUTO_MIN_RUNS = 3
AUTO_SHORT_UAH = 1.0  # µA·h; a run below this, or AUTO_SHORT_FRACTION of
AUTO_SHORT_FRACTION = 0.25  # the median of all kept totals, is "very short"


def auto_target(totals, fallback: float) -> Tuple[float, int]:
    """The target for a run from recent run totals (oldest first), and how
    many runs it came from: 0 means there weren't enough, so it's `fallback`."""
    if not totals:
        return fallback, 0
    floor = max(AUTO_SHORT_UAH, AUTO_SHORT_FRACTION * statistics.median(totals))
    kept = [t for t in totals if t >= floor][-AUTO_RUNS:]
    if len(kept) < AUTO_MIN_RUNS:
        return fallback, 0
    return round(statistics.median(kept), 1), len(kept)


def pv_text(value: Any) -> Optional[str]:
    """A text PV's value; None if it's unknown (NaN, as a disconnected PV sends) or blank."""
    if value is None:
        return None
    text = strip_controls(str(value)).strip()
    return None if not text or text.lower() == "nan" else text


def parse_start_time(text: str, now: datetime) -> Optional[datetime]:
    """DAE:STARTTIME as a time, if it's readable and within a day of now."""
    try:
        started = datetime.strptime(text, START_TIME_FORMAT).replace(tzinfo=UK_TZ)
    except ValueError:
        return None
    return started.astimezone(timezone.utc) if abs(started - now) <= timedelta(days=1) else None


@dataclass
class RunInfo:
    """The run in progress, or the last one, until the next begins."""
    number: str
    title: str
    started_at: datetime
    start_text: Optional[str]  # the DAE:STARTTIME it began with; None until seen
    exp_gen: int  # the experiment it belongs to (RunTracker.exp_gen when it began)
    target: float  # µA·h its finishing card is due at
    auto_runs: int  # how many runs an auto target came from; 0 = the fallback
    end_notified: bool = False
    last_counts: float = -1.0  # µA·h credited to it; -1 = none read
    ended_at: Optional[datetime] = None  # when its number moved on (or a later start found it gone)
    final_known: bool = False  # whether last_counts is its final total


class RunListener(Protocol):
    """What RunTracker's decisions are acted on by (None in shadow mode)."""
    def run_started(self, run: RunInfo, previous: Optional[RunInfo], silent: bool) -> None: ...
    def run_completed(self, run: RunInfo) -> None: ...
    def run_rebegun(self, run: RunInfo) -> None: ...
    def experiment_finished(self, rb: str, runs: int, uah: float, since: datetime, partial: bool,
                            now: datetime) -> None: ...


class RunTracker:
    """One instrument's runs and experiments, from its DAE's PVs.

    `call_later(seconds, callback)` schedules the settle timer (by default on
    the running loop); `log_prefix` marks its decisions in the log.
    """

    def __init__(self, instrument: InstrumentConfig, listener: Optional[RunListener] = None,
                 call_later: Optional[Callable] = None, log_prefix: str = ""):
        self.instrument = instrument
        self.listener = listener
        self._call_later = call_later
        self._log = f"{log_prefix}{instrument.name}"
        # Saved across restarts
        self.run_number = ""  # as last seen: the current run's, or the next one's
        self.run: Optional[RunInfo] = None
        self.last_completed = ""  # number of the last run recorded as completed
        self.rb = ""  # the experiment's RB number, "0" for none; "" until seen
        self.exp_gen = 0  # bumped at every RB number change
        self.exp_since: Optional[datetime] = None
        self.exp_partial = True  # it began before the daemon saw it, so its totals are "at least"
        self.exp_runs = 0
        self.exp_uah = 0.0
        self.run_totals: Deque[float] = deque(maxlen=AUTO_HISTORY)  # this experiment's, for auto targets
        # Live only
        self.run_state = ""  # "" while unknown
        self.title = ""
        self.counts: Optional[float] = None
        self.start_text: Optional[str] = None  # DAE:STARTTIME as last seen
        # Whether it came since the DAE was last between runs, so is the current run's
        self._start_text_fresh = False
        self.restored = False  # whether saved state was restored (so the first start isn't adopted silently)
        self._seen_idle = False  # whether it's been seen between runs since the daemon started
        self.replaying = True  # see resubscribed()
        self._replay_deadline: Optional[datetime] = None
        self._seen_number = self._seen_state = False
        self.rb_resync = True  # the first RB number after (re)subscribing may have changed unwatched
        self._pending: Optional[Tuple[str, datetime]] = None  # (number, since) of a start that's settling
        self._final_due = False  # a run ended unseen, and its final counts are still to come
        self._timer = None
        self._unknown_states: set = set()

    # --- PV updates ------------------------------------------------------

    def resubscribed(self, now: datetime) -> None:
        """PVWS is about to re-send every value, in no fixed order. Until both
        RUNNUMBER and RUNSTATE have come (or RUNNUMBER, after REPLAY_TIMEOUT),
        nothing starts and no counts are credited to a run."""
        self._suspend()
        self._seen_number = self._seen_state = False
        # What was seen before may have changed unseen.
        self.run_state, self.counts, self.start_text, self._start_text_fresh = "", None, None, False
        self._replay_deadline = now + REPLAY_TIMEOUT
        self.rb_resync = True
        self._schedule(REPLAY_TIMEOUT)

    def _suspend(self) -> None:
        """Until RUNNUMBER and RUNSTATE have each come since their last NaN
        (an IOC or gateway outage), waiting however long that takes."""
        self.replaying, self._replay_deadline, self._pending = True, None, None

    def _seen(self, now: datetime) -> None:
        timed_out = self._replay_deadline is not None and now >= self._replay_deadline
        if self.replaying and self._seen_number and (self._seen_state or timed_out):
            self._replayed(now)

    def _replayed(self, now: datetime) -> None:
        """The replay is over (or timed out): settle what it delivered, in
        whatever order it came."""
        self.replaying, self._replay_deadline = False, None
        run = self.run
        if run and run.number != self.run_number and self.last_completed != run.number:
            self._end_unseen(now)
        elif run and run.number == self.run_number:  # what was re-sent meanwhile is this run's
            # A STARTTIME that changed under the same number is a re-begin;
            # checked first, as the counts re-sent are then the new attempt's.
            if run.start_text and self.start_text and self.start_text != run.start_text:
                self._rebegin(now)
            run.title = self.title or run.title
            if self.counts is not None and self.credited():
                run.last_counts = self.counts
        self._maybe_start(now)

    def _end_unseen(self, now: datetime, waited: bool = False) -> None:
        """Complete a run whose number moved on unseen. While the DAE is in
        SETUP for the next number its counts are still this run's final (they
        only reset at BEGIN), unless more than one run passed; if none have
        come yet, the next ones are. ENDING may be this run's (its SETUP not
        yet re-sent) or the next run's, so that waits for SETUP too. Once
        another run begins or ends first (`waited`), or a STARTTIME other
        than its own shows one did, its final is unknown."""
        run = self.run
        waited = waited or bool(run.start_text and self.start_text and self.start_text != run.start_text)
        follows = _follows(run.number, self.run_number)
        if follows and not waited and (self.run_state == "ENDING" or (self.run_state == "SETUP" and self.counts is None)):
            self._final_due = True  # see on_counts, on_run_state and on_run_number
            return
        self._final_due = False
        final = (not waited and follows and self.run_state == "SETUP" and self.counts is not None
                 and self.counts >= run.last_counts)
        if final:
            run.last_counts = self.counts
        self._complete(now, final_known=final)

    def on_run_number(self, value: Any, now: datetime) -> None:
        number = pv_text(value)
        if number is None:  # the IOC is down; the number stays known
            self._seen_number = False
            self._suspend()
            return
        was_seen, self._seen_number = self._seen_number, True
        if number == self.last_completed and number != self.run_number:
            # E.g. an IOC that went back by one: recounting a run would duplicate it. (Further
            # back is taken as the DAE renumbered, so is followed.)
            logger.warning(f"{self._log}: ignoring run number {number}, already completed")
        elif number != self.run_number:
            old = self.run_number
            if self._final_due:  # another run passed before its final counts came
                self._end_unseen(now, waited=True)
            run = self.run
            if (was_seen and not self.replaying and self.run_state == "ENDING" and old != self.last_completed
                    and not (run and run.number == old) and _follows(old, number)):
                self._start(now)  # it began unseen (e.g. before a reconnect) and has just ended
            self.run_number = number
            run = self.run
            if run and run.number == old and self.last_completed != old and not self.replaying:
                self._complete(now, final_known=True)  # (while replaying: see _replayed)
        self._seen(now)
        self._maybe_start(now)

    def on_run_state(self, value: Any, now: datetime) -> None:
        state = pv_text(value)
        if state is None:
            self.run_state, self._seen_state = "", False
            self._suspend()
            return
        self.run_state = state.upper()
        self._seen_state = True
        if self._final_due and not self.replaying and self.run_state != "ENDING":  # begun: the counts are the next run's
            self._end_unseen(now, waited=self.run_state != "SETUP")
        if self.run_state not in ACTIVE_STATES:
            self._seen_idle, self._start_text_fresh = True, False
            if self.run_state not in IDLE_STATES and self.run_state not in self._unknown_states:
                self._unknown_states.add(self.run_state)
                logger.warning(f"{self._log}: unknown run state {self.run_state}, taken as between runs")
        self._seen(now)
        self._maybe_start(now)

    def on_title(self, title: str, now: datetime) -> None:
        """The decoded WDTITLE. It names the run under its number, and may
        change several times before BEGIN; it never starts a run."""
        self.title = title
        if not self.replaying and self.run and self.run.number == self.run_number:
            self.run.title = title  # while replaying, it may be the next run's: see _seen

    def credited(self) -> bool:
        """Whether counts now belong to the run (see on_counts)."""
        run = self.run
        return (not self.replaying and self._seen_number and run is not None
                and run.number == self.run_number and self.run_state in CREDITED_STATES)

    def on_counts(self, value: Any, now: datetime) -> bool:
        """TOTALUAMPS: kept for display, and credited to the run when it's
        surely its (not while replaying, when they may be the next run's).
        Returns whether they were credited."""
        try:
            counts = float(value)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(counts):
            self.counts = None  # the IOC is down; they may move on unseen
            return False
        self.counts = counts
        if self._final_due and not self.replaying:
            self._end_unseen(now)
        if not self.credited():
            return False
        run = self.run
        # With no readable STARTTIME, a reset to about 0 is the only sign of a re-begin.
        if run.start_text is None and counts < 0.1 and run.last_counts > 1:
            self._rebegin(now)
        run.last_counts = counts
        return True

    def on_start_time(self, value: Any, now: datetime) -> None:
        text = pv_text(value)
        if text is None:
            return
        self.start_text = text
        self._start_text_fresh = self.run_state not in IDLE_STATES  # e.g. a run's, before its state comes
        run = self.run
        if self.replaying or not run or run.number != self.run_number:
            return
        if run.start_text is None:  # its first: when it really began
            run.start_text = text
            run.started_at = parse_start_time(text, now) or run.started_at
        elif text != run.start_text:
            self._rebegin(now)

    def on_rb(self, value: Any, now: datetime) -> None:
        """_RBNUMBER, "0" while there's no experiment. A change ends the
        experiment (none for "0"), counting the run in progress; that run's
        own completion then counts for neither, as its exp_gen is old."""
        rb = pv_text(value)
        if rb is None:
            return
        unwatched, self.rb_resync = self.rb_resync, False
        if rb == self.rb:
            return
        old, partial = self.rb, True  # the first seen had begun before
        run = self.run
        in_progress = run and self.last_completed != run.number and run.exp_gen == self.exp_gen
        # Only just begun, it's the new experiment's first run (and gets its target).
        just_begun = in_progress and run.number == self.run_number and now - run.started_at <= NEW_EXPERIMENT_WINDOW
        if old:
            extra = run.last_counts if in_progress and not just_begun and run.last_counts > 0 else 0.0
            runs = self.exp_runs + (extra > 0)
            if old != "0" and runs and self.exp_since:
                logger.info(f"{self._log}: experiment {old} finished: {runs} runs, {self.exp_uah + extra:.1f} µA·h")
                if self.listener:
                    self.listener.experiment_finished(old, runs, self.exp_uah + extra, self.exp_since,
                                                      self.exp_partial or unwatched or extra > 0, now)
            partial = unwatched
        logger.info(f"{self._log}: RB number {old or '(none)'} -> {rb}")
        self.rb, self.exp_gen, self.exp_partial = rb, self.exp_gen + 1, partial
        self.exp_since, self.exp_runs, self.exp_uah = now, 0, 0.0
        self.run_totals.clear()
        if just_begun:
            run.exp_gen = self.exp_gen
            run.target, run.auto_runs = self._target()

    def tick(self, now: datetime) -> None:
        """Run when a timer fires: give up waiting for a replay, or start a settled run."""
        if self.replaying and self._replay_deadline and now >= self._replay_deadline:
            if self._seen_number:
                logger.warning(f"{self._log}: no RUNSTATE within {REPLAY_TIMEOUT.seconds}s of resubscribing; "
                               "carrying on without it")
                self._replayed(now)
            else:  # acting on a number from before it would credit and start the wrong runs
                logger.warning(f"{self._log}: no RUNNUMBER within {REPLAY_TIMEOUT.seconds}s of resubscribing; "
                               "waiting for it")
        self._maybe_start(now)

    def cancel(self) -> None:
        """Stop the timer, e.g. on shutdown."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    # --- Decisions -------------------------------------------------------

    def _schedule(self, delay: timedelta) -> None:
        call_later = self._call_later
        if call_later is None:
            try:
                call_later = asyncio.get_running_loop().call_later
            except RuntimeError:  # no loop (e.g. set up outside one): the next update ticks instead
                return
        self.cancel()
        self._timer = call_later(delay.total_seconds() + 0.05, lambda: self.tick(datetime.now(timezone.utc)))

    def _maybe_start(self, now: datetime) -> None:
        """Start a run once it's been under way under a new number for SETTLE
        (one first seen ending: see on_run_number)."""
        run = self.run
        if (self.replaying or not self.run_number or (run is not None and run.number == self.run_number)
                or self.run_state not in STARTED_STATES):
            self._pending = None
            return
        if not self._pending or self._pending[0] != self.run_number:
            self._pending = (self.run_number, now)
            self._schedule(SETTLE)
        elif now - self._pending[1] >= SETTLE:
            self._pending = None
            self._start(now)
        else:  # e.g. a timer that fired early: wait out the rest
            self._schedule(SETTLE - (now - self._pending[1]))

    def _start(self, now: datetime) -> None:
        previous = self.run
        if previous and self.last_completed != previous.number:  # its end wasn't seen, e.g. the daemon was down
            self._complete(now, final_known=False)
        target, auto_runs = self._target()
        # One from before the DAE was last between runs is an earlier run's.
        start_text = self.start_text if self._start_text_fresh else None
        started_at = (parse_start_time(start_text, now) if start_text else None) or now
        self.run = RunInfo(
            self.run_number, self.title, started_at, start_text, self.exp_gen, target, auto_runs,
            last_counts=self.counts if self.counts is not None and self.run_state in CREDITED_STATES else -1.0,
        )
        silent = (previous is None and not self.restored and not self._seen_idle) or self.run_state == "ENDING"
        if previous and not _follows(previous.number, self.run_number):
            previous = None  # runs were missed (its final_known says whether its total is)
        logger.info(f"{self._log}: run {self.run_number} began{' (adopted)' if silent else ''}: {self.title!r}")
        if self.listener:
            self.listener.run_started(self.run, previous, silent)

    def _target(self) -> Tuple[float, int]:
        inst = self.instrument
        return auto_target(self.run_totals, inst.notify_counts) if inst.auto_counts else (inst.notify_counts, 0)

    def _complete(self, now: datetime, final_known: bool) -> None:
        run = self.run
        run.ended_at, run.final_known, self.last_completed = now, final_known, run.number
        if run.exp_gen == self.exp_gen:
            self.exp_partial = self.exp_partial or not final_known  # so its totals are "at least"
        if run.exp_gen == self.exp_gen and run.last_counts > 0:
            self.exp_runs += 1
            self.exp_uah += run.last_counts
            if self.instrument.auto_counts and final_known:
                self.run_totals.append(run.last_counts)
        counts = f"{run.last_counts:.1f} µA·h" if run.last_counts >= 0 else "no counts read"
        unknown = "" if final_known else " (final total unknown)"
        logger.info(f"{self._log}: run {run.number} completed, {counts}{unknown}")
        if self.listener:
            self.listener.run_completed(run)

    def _rebegin(self, now: datetime) -> None:
        run = self.run
        run.start_text = self.start_text
        run.started_at = (parse_start_time(self.start_text, now) if self.start_text else None) or now
        run.last_counts, run.end_notified = 0.0, False
        logger.info(f"{self._log}: run {run.number} begun again (an abort)")
        if self.listener:
            self.listener.run_rebegun(run)

    # --- Persistence -----------------------------------------------------

    def to_snapshot(self) -> dict:
        run = None
        if self.run:
            run = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in asdict(self.run).items()}
        return {
            "run_number": self.run_number, "run": run, "last_completed": self.last_completed,
            "rb": self.rb, "exp_gen": self.exp_gen,
            "exp_since": self.exp_since.isoformat() if self.exp_since else None,
            "exp_partial": self.exp_partial, "exp_runs": self.exp_runs, "exp_uah": self.exp_uah,
            "run_totals": list(self.run_totals),
        }

    def from_snapshot(self, saved: object, same_target: bool = True) -> None:
        """Restore what to_snapshot() saved; anything malformed is logged and
        ignored. Without `same_target` (notify_counts or auto changed in the
        config behind this restart), the run's target is worked out again and
        a finishing card sent for the old one doesn't cover it."""
        try:
            if not isinstance(saved, dict):
                raise TypeError("not an object")
            run = saved["run"]
            if run is not None:
                if not isinstance(run, dict):
                    raise TypeError("run isn't an object")
                run = RunInfo(**{**run, "started_at": _time(run["started_at"]),
                                 "ended_at": _time(run["ended_at"]) if run["ended_at"] else None})
                _check_run(run)
            values = (
                _of(saved, "run_number", str), _of(saved, "last_completed", str), _of(saved, "rb", str),
                _of(saved, "exp_gen", int), _time(saved["exp_since"]) if saved["exp_since"] else None,
                _of(saved, "exp_partial", bool), _of(saved, "exp_runs", int), _of(saved, "exp_uah", float),
                [float(_finite(v)) for v in _of(saved, "run_totals", list)],
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(f"{self._log}: ignoring malformed saved run state ({exc})")
            return
        (self.run_number, self.last_completed, self.rb, self.exp_gen, self.exp_since, self.exp_partial,
         self.exp_runs, self.exp_uah, totals) = values
        self.run_totals = deque(totals if self.instrument.auto_counts else (), maxlen=AUTO_HISTORY)
        self.run, self.restored = run, True
        if run and not same_target:
            run.target, run.auto_runs = self._target()
            run.end_notified = False


def _follows(previous: str, number: str) -> bool:
    try:
        return int(number) == int(previous) + 1
    except ValueError:
        return False


def _of(saved: dict, key: str, kind: type):
    value = saved[key]
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if not isinstance(value, kind) or (kind is not bool and isinstance(value, bool)):
        raise TypeError(f"{key} isn't a {kind.__name__}")
    return _finite(value) if kind is float else value


def _finite(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{value!r} isn't a finite number")
    return value


def _time(text: Any) -> datetime:
    if not isinstance(text, str):
        raise TypeError("a time isn't text")
    ts = datetime.fromisoformat(text)
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _check_run(run: RunInfo) -> None:
    for name, kind in (("number", str), ("title", str), ("exp_gen", int), ("auto_runs", int),
                       ("end_notified", bool), ("final_known", bool)):
        _of(asdict(run), name, kind)
    for name in ("target", "last_counts"):
        setattr(run, name, float(_finite(getattr(run, name))))
    if run.start_text is not None and not isinstance(run.start_text, str):
        raise TypeError("start_text isn't text")


def _field(update: dict, key: str) -> Any:
    """update[key]; "NaN" if the PV is disconnected (sent as the value whatever
    its type); None for a partial update without it (e.g. only a new
    severity), which is skipped."""
    if key in update:
        return update[key]
    return "NaN" if str(update.get("value")).lower() == "nan" else None


def pv_handlers(tracker: "RunTracker") -> dict:
    """The tracker's PVs, each with a (sync) handler taking a PVWS update and its time."""
    inst = tracker.instrument

    def on(key: str, handle: Callable[[Any, datetime], Any]):
        def handler(update: dict, now: datetime) -> None:
            value = _field(update, key)
            if value is not None:
                handle(value, now)
        return handler

    def title(b64: Any, now: datetime) -> None:
        if pv_text(b64) is None:  # NaN: the IOC is down
            return
        try:
            text = strip_controls(base64.b64decode(str(b64)).decode(errors="replace"))  # e.g. NUL padding
        except ValueError:
            return
        if text:
            tracker.on_title(text, now)

    return {
        inst.rb_number_pv: on("text", tracker.on_rb),
        inst.run_number_pv: on("text", tracker.on_run_number),
        inst.run_name_pv: on("b64byt", title),
        inst.counts_pv: on("value", tracker.on_counts),
        inst.run_state_pv: on("text", tracker.on_run_state),
        inst.start_time_pv: on("text", tracker.on_start_time),
    }
