import base64
import json
from datetime import datetime, timedelta, timezone
from operator import attrgetter
from pathlib import Path

import pytest

from isis_monitor.config import InstrumentConfig
from isis_monitor.runs import (
    REPLAY_TIMEOUT,
    SETTLE,
    RunTracker,
    parse_start_time,
    pv_handlers,
)

DATA = Path(__file__).parent / "data" / "runs"
T0 = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)


class Decisions:
    """A RunListener that records what it's told."""
    def __init__(self):
        self.log = []

    def run_started(self, run, previous, silent):
        self.previous = previous
        self.log.append(("start", run.number, "silent" if silent else previous.number if previous else None))

    def run_completed(self, run):
        self.log.append(("done", run.number, round(run.last_counts, 3) if run.final_known else None))

    def run_rebegun(self, run, previous_title):
        self.log.append(("rebegin", run.number))

    def experiment_finished(self, rb, runs, uah, since, partial, now):
        self.log.append(("experiment", rb, runs, round(uah, 3), partial))


def update(suffix, value):
    """A PVWS update as the server sends it."""
    if value == "NaN":
        return {"value": "NaN"}  # whatever the PV's type
    if suffix == "WDTITLE":
        return {"b64byt": base64.b64encode(value.encode() + b"\0" * 8).decode()}
    return {"value": value} if suffix == "TOTALUAMPS" else {"text": value}


RESUBSCRIBE = "RESUBSCRIBE"  # not a PV: PVWS reconnected, and will re-send every PV


def reconnect(t=100):
    """The update a reconnect at `t` seconds is, in a Replay."""
    return (t, RESUBSCRIBE, None)


class Replay:
    """A RunTracker fed (seconds, PV suffix, value) updates on a fake clock;
    its timer is replaced by ticking before every update."""
    def __init__(self, name="PEARL", saved=None, auto=False, **instrument):
        self.decisions = Decisions()
        self.tracker = RunTracker(InstrumentConfig(name, 100.0, auto_counts=auto, **instrument), self.decisions,
                                  call_later=lambda delay, callback: None)
        if saved is not None:
            self.tracker.from_snapshot(saved)
        self.handlers = {pv.split(":DAE:")[1]: h for pv, h in pv_handlers(self.tracker).items()}
        self.now = T0
        self.tracker.resubscribed(self.now)

    def __call__(self, *updates):
        for t, suffix, value in updates:
            self.at(t)
            if suffix == RESUBSCRIBE:
                self.tracker.resubscribed(self.now)
            else:
                self.handlers[suffix](update(suffix, value), self.now)
        return self

    def at(self, t):
        self.now = T0 + timedelta(seconds=t)
        self.tracker.tick(self.now)
        return self

    def settle(self):
        """Let any timer fire."""
        return self.at((self.now - T0).total_seconds() + max(SETTLE, REPLAY_TIMEOUT).total_seconds() + 1)

    @property
    def log(self):
        return self.decisions.log


def fixture(name):
    data = json.loads((DATA / f"{name}.json").read_text())
    return data["instrument"], data["updates"]


def replay_fixture(name, **kwargs):
    instrument, updates = fixture(name)
    return Replay(instrument, **kwargs)(*updates).settle()


def state_of(tracker, expected):
    """The tracker's attributes named by `expected`'s keys ("run.title"; "credited()" is called)."""
    def get(key):
        value = attrgetter(key.removesuffix("()"))(tracker)
        return value() if key.endswith("()") else value
    return {key: get(key) for key in expected}


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Recorded sequences (cut from 2.9h of live PVWS data, 13 Oct 2026)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, decisions, state", [
    # Adopted mid-run without a card, then completed with its final total
    # when its number moves on, and the next run started after it.
    ("gem_transition", [("start", "00102941", "silent"), ("done", "00102941", 13.094),
                        ("start", "00102942", "00102941")], {}),
    ("wish_long_ending", [("start", "00065044", "silent"), ("done", "00065044", 22.67)], {}),
    # Seen between runs first, so its start is announced; retitles start nothing.
    # The run takes its title (the last retitle in SETUP) and start time (10:29:19 BST) at begin.
    ("hifi_setup_retitles", [("start", "00211425", None)],
     {"run.title": "Y3Cu9OH19Cl8_T=0.075_F=0", "run.started_at": utc(2026, 10, 13, 9, 29, 19)}),
    # Aborts don't change the number: the same run, begun again.
    ("inter_aborts", [("start", "00093428", "silent"), ("rebegin", "00093428"), ("rebegin", "00093428")], {}),
    # A re-begin resets the run.
    ("larmor_abort", [("start", "00101373", "silent"), ("rebegin", "00101373")],
     {"run.start_text": "Tue 13-Oct-2026 11:39:19", "run.started_at": utc(2026, 10, 13, 10, 39, 19),
      "run.last_counts": 0.0, "run.end_notified": False}),
    ("mari_quick_abort", [("start", "31996", "silent"), ("done", "31996", 160.096), ("start", "31997", "31996"),
                          ("rebegin", "31997")], {}),
    ("emu_failed_begins", [], {}),
    ("enginx_short_runs", [("start", "00379616", "silent"), ("done", "00379616", 0.148)]
     + [d for n in range(17, 21) for d in (("start", f"00379{600 + n}", f"00379{599 + n}"),
                                           ("done", f"00379{600 + n}", 0.149))]
     + [("start", "00379621", "00379620")], {}),
    # Outages, SAVING and pauses neither end nor start a run, and counts are
    # credited again after an outage (the last ones), even when a NaN comes
    # after the other PV is back (as in osiris_late_nan).
    ("osiris_outages", [("start", "00158186", "silent")], {"credited()": True, "run.last_counts": 71.669}),
    ("osiris_late_nan", [("start", "00158187", "silent")], {"credited()": True, "run.last_counts": 42.761}),
    ("polaris_saving", [("start", "00167061", "silent")], {}),
    ("polref_nan", [], {}),  # its IOC comes back between runs
    # Connected while a run was ending: adopted quietly then, so its end (and total) is recorded.
    ("gem_connect_in_ending", [("start", "00102942", "silent"), ("done", "00102942", 13.094),
                               ("start", "00102943", "00102942")], {}),
])
def test_recorded_sequences(name, decisions, state):
    r = replay_fixture(name)
    assert r.log == decisions
    assert state_of(r.tracker, state) == state


# ---------------------------------------------------------------------------
# Constructed sequences
# ---------------------------------------------------------------------------

RUNNING = [(0, "_RBNUMBER", "123"), (0, "RUNNUMBER", "00010"), (0, "WDTITLE", "Run ten"),
           (0, "RUNSTATE_STR", "RUNNING"), (0, "STARTTIME", "Tue 13-Oct-2026 09:00:00"), (0, "TOTALUAMPS", 5.0)]
NEXT_RUN = [(20, "RUNSTATE_STR", "ENDING"), (21, "RUNNUMBER", "00011"), (21, "RUNSTATE_STR", "SETUP"),
            (22, "WDTITLE", "Run eleven"), (25, "RUNSTATE_STR", "BEGINNING"), (25, "TOTALUAMPS", 0.0),
            (27, "RUNSTATE_STR", "RUNNING"), (27, "STARTTIME", "Tue 13-Oct-2026 10:00:27")]
NO_START_TIME = [u for u in RUNNING if u[1] != "STARTTIME"]
OUTAGE = [(30, "RUNNUMBER", "NaN"), (30, "RUNSTATE_STR", "NaN"), (30, "TOTALUAMPS", "NaN")]


def running(**kwargs):
    """A tracker connected mid-run 10, with 8 µA·h credited."""
    r = Replay(**kwargs)(*RUNNING, (10, "TOTALUAMPS", 8.0))
    assert r.log == [("start", "00010", "silent")]
    return r


def restarted():
    """A new tracker carrying on from running()'s saved state."""
    return Replay(saved=json.loads(json.dumps(running().tracker.to_snapshot())))


def restart_in(state):
    return [u if u[1] != "RUNSTATE_STR" else (0, u[1], state) for u in RUNNING]


STARTS = {"running": running, "fresh": Replay, "restarted": restarted}


@pytest.mark.parametrize("start, updates, decisions, state", [
    # PVWS sometimes delivers an update twice, back to back.
    pytest.param("running", [u for u in RUNNING[1:4] + NEXT_RUN for _ in (1, 2)],
                 [("done", "00010", 8.0), ("start", "00011", "00010")], {}, id="duplicates"),
    # Connected mid-run: started silently (no card); seen between runs first, it's announced.
    pytest.param("fresh", RUNNING, [("start", "00010", "silent")], {}, id="first start seen mid-run"),
    pytest.param("fresh", [(0, "RUNNUMBER", "00011"), (0, "RUNSTATE_STR", "SETUP"), (5, "RUNSTATE_STR", "RUNNING")],
                 [("start", "00011", None)], {}, id="first start seen between runs"),
    # Connected between runs: the STARTTIME seen is the last run's, so the next
    # run's own (when it comes) isn't a re-begin.
    pytest.param("fresh", [(0, "RUNNUMBER", "00011"), (0, "RUNSTATE_STR", "SETUP"),
                           (0, "STARTTIME", "Tue 13-Oct-2026 09:00:00"), (5, "RUNSTATE_STR", "RUNNING"),
                           (8, "STARTTIME", "Tue 13-Oct-2026 10:00:05")],
                 [("start", "00011", None)], {"run.started_at": utc(2026, 10, 13, 9, 0, 5)}, id="stale start time"),
    pytest.param("fresh", [*NO_START_TIME, (10, "TOTALUAMPS", 8.0), (20, "RUNSTATE_STR", "SETUP"),
                           (25, "RUNSTATE_STR", "RUNNING"), (25, "TOTALUAMPS", 0.0)],
                 [("start", "00010", "silent"), ("rebegin", "00010")], {},
                 id="without a start time, counts reset to zero is a rebegin"),
    # Adopted (quietly: it's already over) as it ends, so its end is recorded.
    pytest.param("running", [*NEXT_RUN[:5], (27, "RUNSTATE_STR", "RUNNING"), (27.2, "TOTALUAMPS", 0.4),
                             (27.5, "RUNSTATE_STR", "ENDING"), (28, "RUNNUMBER", "00012"), (28, "RUNSTATE_STR", "SETUP")],
                 [("done", "00010", 8.0), ("start", "00011", "silent"), ("done", "00011", 0.4)], {},
                 id="a run ending while its start settles"),
    pytest.param("running", [*NEXT_RUN[:5], (26, "RUNSTATE_STR", "SETUP"), (28, "RUNNUMBER", "00012")],
                 [("done", "00010", 8.0)], {}, id="a run that never got under way"),

    # --- Reconnects: PVWS re-sends every PV in no fixed order -------------------
    # The run that changed while disconnected is completed once, and the new run's
    # counts aren't the old one's. Run 10's end wasn't seen and the DAE is already
    # counting run 11, so 10's final is unknown.
    *[pytest.param("running", [reconnect(), *replay], [("done", "00010", None), ("start", "00011", "00010")],
                   {"run.last_counts": 0.5}, id=f"next run counting after a reconnect, {order}")
      for order, replay in [
          ("state first", [(100, "RUNSTATE_STR", "RUNNING"), (100, "TOTALUAMPS", 0.5), (100, "RUNNUMBER", "00011")]),
          ("counts first", [(100, "TOTALUAMPS", 0.5), (100, "RUNNUMBER", "00011"), (100, "RUNSTATE_STR", "RUNNING")]),
      ]],
    # A run that ended unseen takes its final counts from between runs, whatever
    # order the replay brings them in: it's decided once both PVs are back.
    *[pytest.param("running", [reconnect(), *replay], [("done", "00010", 9.5)], {}, id=f"ended unseen {n}")
      for n, replay in enumerate([
          [(100, "TOTALUAMPS", 9.5), (100, "RUNSTATE_STR", "SETUP"), (100, "RUNNUMBER", "00011")],
          [(100, "RUNNUMBER", "00011"), (100, "RUNSTATE_STR", "SETUP"), (100, "TOTALUAMPS", 9.5)],
          [(100, "RUNNUMBER", "00011"), (100, "TOTALUAMPS", 9.5), (100, "RUNSTATE_STR", "SETUP")],
      ])],
    pytest.param("running", [reconnect(), (100, "RUNNUMBER", "00012"), (100, "RUNSTATE_STR", "RUNNING"),
                             (100, "TOTALUAMPS", 1.0)],
                 [("done", "00010", None), ("start", "00012", None)], {},  # no previous: 11 was missed
                 id="a reconnect that skips a run"),
    # E.g. ENDING or paused across the gap: the re-sent total is the final one.
    pytest.param("running", [reconnect(), (100, "TOTALUAMPS", 9.5), (100, "RUNNUMBER", "00010"),
                             (100, "RUNSTATE_STR", "ENDING"), (130, "RUNNUMBER", "00011"), (130, "RUNSTATE_STR", "SETUP")],
                 [("done", "00010", 9.5)], {}, id="counts re-sent while replaying, not moving again"),
    # Two runs passed while disconnected: 30.0 is run 11's, not 10's.
    pytest.param("running", [reconnect(), (100, "TOTALUAMPS", 30.0), (100, "RUNSTATE_STR", "SETUP"),
                             (100, "RUNNUMBER", "00012")],
                 [("done", "00010", None)], {}, id="ended unseen, not given a later run's counts"),
    # The run changed while disconnected; the replay sends its STARTTIME and
    # counts before RUNSTATE and RUNNUMBER: not a re-begin.
    pytest.param("running", [reconnect(), (100, "STARTTIME", "Tue 13-Oct-2026 10:01:00"), (100, "TOTALUAMPS", 0.3),
                             (100, "RUNSTATE_STR", "RUNNING"), (100, "RUNNUMBER", "00011"),
                             (110, "TOTALUAMPS", 0.0), (120, "TOTALUAMPS", 0.6)],
                 [("done", "00010", None), ("start", "00011", "00010")],
                 {"run.start_text": "Tue 13-Oct-2026 10:01:00"}, id="a new run's start time re-sent first"),
    # A run whose final is unknown makes the experiment "at least".
    pytest.param("running", [reconnect(), (100, "RUNSTATE_STR", "RUNNING"), (100, "RUNNUMBER", "00011")],
                 [("done", "00010", None), ("start", "00011", "00010")], {"exp_partial": True, "exp_runs": 1},
                 id="final unknown"),
    # Run 10's final is unknown (11 came and went unseen); 11 is adopted as it ends.
    *[pytest.param("running", [reconnect(), (100, "RUNNUMBER", "00011"), (100, "RUNSTATE_STR", "ENDING"),
                               (100, "TOTALUAMPS", 4.0), *next_number],
                   [("done", "00010", None), ("start", "00011", "silent"), ("done", "00011", 4.0)], {},
                   id=f"first seen ending after a reconnect, {order}")
      for order, next_number in [
          ("number first", [(120, "RUNNUMBER", "00012"), (120, "RUNSTATE_STR", "SETUP")]),
          ("SETUP first", [(120, "RUNSTATE_STR", "SETUP"), (121, "RUNNUMBER", "00012")]),  # as ZOOM often sends them
      ]],
    pytest.param("running", [reconnect(), (100, "RUNNUMBER", "00011"), (100, "RUNSTATE_STR", "ENDING"),
                             (100, "TOTALUAMPS", "NaN"), (120, "RUNSTATE_STR", "SETUP"), (121, "RUNNUMBER", "00012")],
                 [("done", "00010", None), ("start", "00011", "silent"), ("done", "00011", None)], {},
                 id="an adopted run without counts has no final"),
    # Run 10 was ending when the replay came, and the number moved on before its SETUP was re-sent.
    pytest.param("running", [reconnect(), (100, "RUNSTATE_STR", "ENDING"), (100, "TOTALUAMPS", 13.0),
                             (100.2, "RUNNUMBER", "00011"), (100.2, "RUNSTATE_STR", "SETUP"), *NEXT_RUN[3:]],
                 [("done", "00010", 13.0), ("start", "00011", "00010")], {},
                 id="a stale ENDING replayed doesn't adopt the next run"),
    # Run 10 ended and 11 ran unseen; the replay lands in 11's ENDING, and SETUP comes before 12.
    # Run 10's final is unknown; run 11 is adopted once its number moves on, with its own final.
    pytest.param("running", [reconnect(), (100, "RUNSTATE_STR", "ENDING"), (100, "RUNNUMBER", "00011"),
                             (100, "TOTALUAMPS", 30.0), (100, "STARTTIME", "Tue 13-Oct-2026 10:30:00"),  # not run 10's
                             (102, "RUNSTATE_STR", "SETUP"), (102, "RUNNUMBER", "00012")],
                 [("done", "00010", None), ("start", "00011", "silent"), ("done", "00011", 30.0)], {},
                 id="a later run's ENDING replayed"),
    # Run 10 had no STARTTIME, so a replay of 11's ENDING with SETUP first can't be told
    # from 10's: 10 takes the counts, and 11 is recorded without them, not counted twice.
    pytest.param("fresh", [*NO_START_TIME, (10, "TOTALUAMPS", 8.0), reconnect(), (100, "RUNSTATE_STR", "ENDING"),
                           (100, "RUNNUMBER", "00011"), (100, "TOTALUAMPS", 30.0), (102, "RUNSTATE_STR", "SETUP"),
                           (103, "RUNNUMBER", "00012")],
                 [("start", "00010", "silent"), ("done", "00010", 30.0), ("start", "00011", "silent"),
                  ("done", "00011", None)], {"exp_runs": 1, "exp_uah": 30.0},
                 id="counts a previous run took as its final"),
    # Re-begun while disconnected: found when the replay ends.
    pytest.param("running", [reconnect(), (100, "STARTTIME", "Tue 13-Oct-2026 10:01:30"), (100, "TOTALUAMPS", 0.2),
                             (100, "RUNNUMBER", "00010"), (100, "RUNSTATE_STR", "RUNNING")],
                 [("rebegin", "00010")], {}, id="a rebegin while disconnected"),
    # ...and the counts re-sent with it are the new attempt's.
    pytest.param("running", [reconnect(), (100, "STARTTIME", "Tue 13-Oct-2026 10:01:30"), (100, "TOTALUAMPS", 3.0),
                             (100, "RUNNUMBER", "00010"), (100, "RUNSTATE_STR", "PAUSED"),
                             (130, "RUNSTATE_STR", "ENDING"), (131, "RUNNUMBER", "00011"), (131, "RUNSTATE_STR", "SETUP")],
                 [("rebegin", "00010"), ("done", "00010", 3.0)], {}, id="counts re-sent after a rebegin"),

    # --- Outages -------------------------------------------------------------------
    pytest.param("running", [*OUTAGE, (31, "TOTALUAMPS", 12.0),  # before RUNNUMBER is back: not credited
                             (40, "RUNSTATE_STR", "RUNNING"), (40, "RUNNUMBER", "00010"), (41, "TOTALUAMPS", 12.5)],
                 [], {"run.last_counts": 12.5}, id="an outage neither ends nor starts a run"),
    # A run that ends during an outage:
    # the DAE is between runs and its counts came first: they're the run's final total;
    pytest.param("running", [*OUTAGE, (40, "TOTALUAMPS", 12.0), (40, "RUNSTATE_STR", "SETUP"),
                             (40, "RUNNUMBER", "00011")],
                 [("done", "00010", 12.0)], {}, id="ends in an outage, counts first"),
    # counts back after the others, still in SETUP: they're the final too, not the stale 8.0;
    pytest.param("running", [*OUTAGE, (40, "RUNSTATE_STR", "SETUP"), (40, "RUNNUMBER", "00011"),
                             (41, "TOTALUAMPS", 12.0)],
                 [("done", "00010", 12.0)], {}, id="ends in an outage, counts last"),
    # the next run began before any counts came: unknown.
    pytest.param("running", [*OUTAGE, (40, "RUNSTATE_STR", "SETUP"), (40, "RUNNUMBER", "00011"),
                             (41, "RUNSTATE_STR", "BEGINNING"), (41, "TOTALUAMPS", 0.0)],
                 [("done", "00010", None)], {}, id="ends in an outage, next run begun"),
    # Run 10 ended unseen with no counts yet, and its final is unknown once another run passes, not run 11's 30.
    pytest.param("running", [*OUTAGE, (40, "RUNSTATE_STR", "SETUP"), (40, "RUNNUMBER", "00011"),
                             *[(50, s, v) for _, s, v in OUTAGE],
                             (200, "TOTALUAMPS", 30.0), (200, "RUNSTATE_STR", "SETUP"), (200, "RUNNUMBER", "00012")],
                 [("done", "00010", None)], {}, id="a final still due when another run passes"),

    # --- Restarts --------------------------------------------------------------------
    # Carries on without repeating anything; the re-sent counts are credited once the replay's over.
    pytest.param("restarted", restart_in("RUNNING"), [],
                 {"run.number": "00010", "run.started_at": utc(2026, 10, 13, 8), "run.last_counts": 5.0},
                 id="restart while RUNNING"),
    pytest.param("restarted", restart_in("ENDING"), [],
                 {"run.number": "00010", "run.started_at": utc(2026, 10, 13, 8), "run.last_counts": 5.0},
                 id="restart while ENDING"),
    # Aborted, say: counts in SETUP aren't the run's.
    pytest.param("restarted", restart_in("SETUP"), [],
                 {"run.number": "00010", "run.started_at": utc(2026, 10, 13, 8), "run.last_counts": 8.0},
                 id="restart in SETUP"),
    pytest.param("restarted", [(0, "RUNNUMBER", "00011"), (0, "RUNSTATE_STR", "RUNNING"), (0, "TOTALUAMPS", 2.0)],
                 [("done", "00010", None), ("start", "00011", "00010")], {}, id="restart after the run ended"),
    pytest.param("restarted", [(0, "TOTALUAMPS", 9.5), (0, "RUNNUMBER", "00010"), (0, "RUNSTATE_STR", "PAUSED"),
                               (30, "RUNNUMBER", "00011"), (30, "RUNSTATE_STR", "SETUP")],
                 [("done", "00010", 9.5)], {}, id="restart in a pause credits the re-sent counts"),

    # --- Experiments -------------------------------------------------------------------
    # An RB change between runs ends the experiment; run 11 is the new experiment's.
    pytest.param("running", [*NEXT_RUN[:3], (22, "_RBNUMBER", "456"), *NEXT_RUN[3:], (40, "TOTALUAMPS", 3.0),
                             *[(t + 40, s, v.replace("11", "12") if s == "RUNNUMBER" else v) for t, s, v in NEXT_RUN]],
                 [("done", "00010", 8.0), ("experiment", "123", 1, 8.0, True), ("start", "00011", "00010"),
                  ("done", "00011", 3.0), ("start", "00012", "00011")], {"exp_runs": 1, "rb": "456"},
                 id="an RB change between runs"),
    # Mid-run: "at least", with the run in progress, whose completion isn't the new experiment's.
    pytest.param("running", [(15, "_RBNUMBER", "456"), *NEXT_RUN],
                 [("experiment", "123", 1, 8.0, True), ("done", "00010", 8.0), ("start", "00011", "00010")],
                 {"exp_runs": 0}, id="an RB change mid-run"),
    pytest.param("running", [(15, "_RBNUMBER", "456"), (16, "_RBNUMBER", "123"), *NEXT_RUN],
                 [("experiment", "123", 1, 8.0, True), ("done", "00010", 8.0), ("start", "00011", "00010")],
                 {"exp_runs": 0}, id="an RB number that comes back doesn't recount a run"),
    pytest.param("running", [(15, "_RBNUMBER", "0"), (16, "_RBNUMBER", "789")],
                 [("experiment", "123", 1, 8.0, True)], {}, id="no experiment card for RB 0"),
    # The first RB after a resubscribe marks a change unwatched.
    pytest.param("running", [*NEXT_RUN, reconnect(60), (60, "_RBNUMBER", "456")],
                 [("done", "00010", 8.0), ("start", "00011", "00010"), ("experiment", "123", 1, 8.0, True)],
                 {"exp_partial": True}, id="the first RB after a resubscribe"),
    # Without its start time a run isn't taken as just begun: it may have begun long before the daemon saw it.
    pytest.param("fresh", [*NO_START_TIME, (10, "TOTALUAMPS", 8.0), (20, "_RBNUMBER", "456")],
                 [("start", "00010", "silent"), ("experiment", "123", 1, 8.0, True)], {},
                 id="an RB change without the run's start time"),
    # Its STARTTIME is days old, so ignored: it began long ago.
    pytest.param("fresh", [*NO_START_TIME, (0, "STARTTIME", "Sat 10-Oct-2026 09:00:00"), (10, "TOTALUAMPS", 8.0),
                           (15, "_RBNUMBER", "456")],
                 [("start", "00010", "silent"), ("experiment", "123", 1, 8.0, True)], {},
                 id="an RB change with an unreadable start time"),
    # A run ending when the RB number changes stays the old experiment's: runs 10
    # and 11, though 11 began 18s before.
    pytest.param("running", [*NEXT_RUN, (30, "TOTALUAMPS", 2.0), (40, "RUNSTATE_STR", "ENDING"), (45, "_RBNUMBER", "456")],
                 [("done", "00010", 8.0), ("start", "00011", "00010"), ("experiment", "123", 2, 10.0, True)], {},
                 id="an RB change as a run ends"),
])
def test_constructed_sequences(start, updates, decisions, state):
    r = STARTS[start]()
    before = len(r.log)
    r(*updates).settle()
    assert r.log[before:] == decisions
    assert state_of(r.tracker, state) == state


def test_a_title_re_sent_while_replaying_is_not_given_to_the_old_run():
    r = running()(reconnect(), (100, "WDTITLE", "Run eleven"), (100, "RUNSTATE_STR", "RUNNING"),
                  (100, "RUNNUMBER", "00011")).settle()
    assert r.log[1:] == [("done", "00010", None), ("start", "00011", "00010")]
    assert (r.decisions.previous.title, r.tracker.run.title) == ("Run ten", "Run eleven")


def test_resubscribe_replays():
    # Counts while replaying aren't credited.
    r = running()(reconnect(50), (50, "TOTALUAMPS", 0.0))
    assert r.tracker.run.last_counts == 8.0 and r.tracker.counts == 0.0

    # A replay that never comes times out...
    r = running()(reconnect(50))
    r.at(50 + REPLAY_TIMEOUT.total_seconds())
    assert r.tracker.replaying  # without the run number: what's credited and started depends on it
    r((61, "RUNNUMBER", "00010"))
    assert not r.tracker.replaying  # without the run state

    # ...as does one without the run state.
    r = running()(reconnect(), (100, "RUNNUMBER", "00010"))
    r.at(100 + REPLAY_TIMEOUT.total_seconds())
    assert not r.tracker.replaying

    # Counts aren't credited after a timeout without the run number.
    r = running()(reconnect(), (100, "RUNSTATE_STR", "RUNNING"))
    r.at(100 + REPLAY_TIMEOUT.total_seconds())  # gave up waiting
    r((112, "TOTALUAMPS", 50.0))
    assert r.tracker.replaying and r.tracker.run.last_counts == 8.0

    # A partial (severity-only) update is skipped, not taken as the PV gone.
    r = running()
    r.handlers["RUNNUMBER"]({"severity": "MAJOR"}, r.now)
    r.handlers["RUNSTATE_STR"]({"severity": "INVALID"}, r.now)
    assert not r.tracker.replaying and r.tracker.run_number == "00010"


def test_auto_targets_come_from_the_experiments_completed_runs():
    r = Replay(auto=True)(*RUNNING)
    for n, total in enumerate((40.0, 50.0, 60.0, 70.0)):
        t = 100 * (n + 1)
        number = f"000{11 + n}"
        r((t, "TOTALUAMPS", total), (t + 1, "RUNSTATE_STR", "ENDING"), (t + 2, "RUNNUMBER", number),
          (t + 2, "RUNSTATE_STR", "SETUP"), (t + 3, "RUNSTATE_STR", "RUNNING"), (t + 3, "TOTALUAMPS", 0.0)).settle()
    assert list(r.tracker.run_totals) == [40.0, 50.0, 60.0, 70.0]  # the adopted run's final total counts too
    assert (r.tracker.run.target, r.tracker.run.auto_runs) == (55.0, 4)


@pytest.mark.parametrize("rb_at, new_experiments", [(29, True), (27 + 31, False)])
def test_an_rb_change_just_after_begin_makes_the_run_the_new_experiments(rb_at, new_experiments):
    """Within NEW_EXPERIMENT_WINDOW of its begin, though counts have come (with beam on they
    do within a second), the run in progress is the new experiment's first, with its target."""
    r = running(auto=True)
    r.tracker.run_totals.extend([20.0] * 3)
    r(*NEXT_RUN, (28, "TOTALUAMPS", 0.02)).settle()
    assert r.tracker.run.target == 20.0
    if not new_experiments:  # later, it's the old one's
        r((rb_at, "_RBNUMBER", "456"))
        assert ("experiment", "123", 2, 8.02, True) in r.log and r.tracker.run.target == 20.0
        return
    r((rb_at, "_RBNUMBER", "456"), (40, "TOTALUAMPS", 3.0), (50, "RUNSTATE_STR", "ENDING"),
      (51, "RUNNUMBER", "00012"), (51, "RUNSTATE_STR", "SETUP")).settle()
    assert ("experiment", "123", 1, 8.0, True) in r.log  # run 11 isn't counted in it
    assert (r.tracker.exp_runs, r.tracker.exp_uah) == (1, 3.0)  # but in the new one
    assert r.tracker.run.target == r.tracker.instrument.notify_counts  # with no runs of its own yet


def test_runs_that_both_end_on_zero_counts_keep_their_finals():
    """With the beam off, an adopted run's 0 µA·h matching the previous run's is no clash."""
    r = running()((15, "TOTALUAMPS", 0.0))
    r.tracker.run.start_text = None  # as if no STARTTIME was read
    r(*NEXT_RUN[:3], reconnect(), (100, "RUNSTATE_STR", "ENDING"), (100, "RUNNUMBER", "00012"),
      (100, "TOTALUAMPS", 0.0), (102, "RUNSTATE_STR", "SETUP"), (103, "RUNNUMBER", "00013")).settle()
    assert ("done", "00012", 0.0) in r.log


def test_a_run_number_going_back_is_ignored(caplog):
    r = running()(*NEXT_RUN).settle()
    r((40, "RUNNUMBER", "00010"), (41, "RUNNUMBER", "00011")).settle()
    assert [d for d in r.log if d[0] == "done"] == [("done", "00010", 8.0)]  # not recounted
    assert "ignoring run number 00010, already completed" in caplog.text
    r((50, "RUNNUMBER", "00003"))  # renumbered
    assert r.tracker.run_number == "00003"


def test_an_unknown_run_state_is_taken_as_between_runs_with_one_warning(caplog):
    r = running()((30, "RUNSTATE_STR", "DREAMING"), (31, "RUNSTATE_STR", "DREAMING")).settle()
    assert caplog.text.count("unknown run state DREAMING") == 1
    assert not r.tracker.credited()


def test_a_timer_that_fires_early_waits_out_the_rest():
    scheduled = []
    tracker = RunTracker(InstrumentConfig("PEARL", 100.0), call_later=lambda d, cb: scheduled.append(d))
    tracker.resubscribed(T0)
    handlers = {pv.split(":DAE:")[1]: h for pv, h in pv_handlers(tracker).items()}
    for suffix, value in (("RUNNUMBER", "00011"), ("RUNSTATE_STR", "SETUP")):
        handlers[suffix](update(suffix, value), T0)
    handlers["RUNSTATE_STR"](update("RUNSTATE_STR", "RUNNING"), T0)
    tracker.tick(T0 + SETTLE / 2)
    assert tracker.run is None and scheduled[-1] == pytest.approx((SETTLE / 2).total_seconds() + 0.05)


def test_start_time_parsing():
    now = utc(2026, 10, 13, 12)
    assert parse_start_time("Tue 13-Oct-2026 10:49:43", now) == utc(2026, 10, 13, 9, 49, 43)
    assert parse_start_time("Tue 13-Oct-2025 10:49:43", now) is None  # more than a day away
    assert parse_start_time("soon", now) is None


# --- Saved state ------------------------------------------------------------------

def test_snapshot_round_trip_and_a_new_target():
    r = running(auto=True)(*NEXT_RUN).settle()
    saved = json.loads(json.dumps(r.tracker.to_snapshot()))
    restored = Replay(saved=saved, auto=True).tracker
    assert restored.to_snapshot() == r.tracker.to_snapshot()
    assert restored.run == r.tracker.run

    # A changed notify_counts resets the restored run's target.
    r = running()
    r.tracker.run.end_notified = True
    tracker = RunTracker(InstrumentConfig("PEARL", 200.0), call_later=lambda *a: None)
    tracker.from_snapshot(json.loads(json.dumps(r.tracker.to_snapshot())), same_target=False)
    assert (tracker.run.target, tracker.run.end_notified) == (200.0, False)


@pytest.mark.parametrize("change", [
    {"run": "junk"}, {"run_number": 10}, {"exp_gen": True}, {"exp_uah": float("nan")}, {"exp_since": "yesterday"},
    {"run_totals": [1, "x"]}, {"run": {"number": "1"}}, {"exp_uah": 10**400}, {"run_totals": [10**400]},
])
def test_a_malformed_snapshot_is_ignored(change, caplog):
    saved = {**running().tracker.to_snapshot(), **change}
    tracker = Replay(saved=json.loads(json.dumps(saved))).tracker
    assert tracker.run is None and not tracker.restored
    assert "ignoring malformed saved run state" in caplog.text
