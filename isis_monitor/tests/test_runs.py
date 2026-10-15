import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from isis_monitor.config import InstrumentConfig
from isis_monitor.runs import REPLAY_TIMEOUT, SETTLE, RunTracker, parse_start_time, pv_handlers

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


# ---------------------------------------------------------------------------
# Recorded sequences (cut from 2.9h of live PVWS data, 13 Oct 2026)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, decisions", [
    # Adopted mid-run without a card, then completed with its final total
    # when its number moves on, and the next run started after it.
    ("gem_transition", [("start", "00102941", "silent"), ("done", "00102941", 13.094),
                        ("start", "00102942", "00102941")]),
    ("wish_long_ending", [("start", "00065044", "silent"), ("done", "00065044", 22.67)]),
    # Seen between runs first, so its start is announced; retitles start nothing.
    ("hifi_setup_retitles", [("start", "00211425", None)]),
    # Aborts don't change the number: the same run, begun again.
    ("inter_aborts", [("start", "00093428", "silent"), ("rebegin", "00093428"), ("rebegin", "00093428")]),
    ("larmor_abort", [("start", "00101373", "silent"), ("rebegin", "00101373")]),
    ("mari_quick_abort", [("start", "31996", "silent"), ("done", "31996", 160.096), ("start", "31997", "31996"),
                          ("rebegin", "31997")]),
    ("emu_failed_begins", []),
    ("enginx_short_runs", [("start", "00379616", "silent"), ("done", "00379616", 0.148)]
     + [d for n in range(17, 21) for d in (("start", f"00379{600 + n}", f"00379{599 + n}"),
                                           ("done", f"00379{600 + n}", 0.149))]
     + [("start", "00379621", "00379620")]),
    # Outages, SAVING and pauses neither end nor start a run.
    ("osiris_outages", [("start", "00158186", "silent")]),
    ("osiris_late_nan", [("start", "00158187", "silent")]),
    ("polaris_saving", [("start", "00167061", "silent")]),
    ("polref_nan", []),  # its IOC comes back between runs
    # Connected while a run was ending: adopted quietly then, so its end (and total) is recorded.
    ("gem_connect_in_ending", [("start", "00102942", "silent"), ("done", "00102942", 13.094),
                               ("start", "00102943", "00102942")]),
])
def test_recorded_sequences(name, decisions):
    assert replay_fixture(name).log == decisions


@pytest.mark.parametrize("name", ["osiris_outages", "osiris_late_nan"])
def test_counts_are_credited_again_after_an_outage(name):
    """Even when a NaN comes after the other PV is back (as in osiris_late_nan)."""
    r = replay_fixture(name)
    _, updates = fixture(name)
    last_counts = [v for _, s, v in updates if s == "TOTALUAMPS" and v != "NaN"][-1]
    assert r.tracker.credited() and r.tracker.run.last_counts == last_counts


def test_a_run_takes_its_title_and_start_time_at_begin():
    r = replay_fixture("hifi_setup_retitles")
    assert r.tracker.run.title == "Y3Cu9OH19Cl8_T=0.075_F=0"  # the last retitle in SETUP
    assert r.tracker.run.started_at == datetime(2026, 10, 13, 9, 29, 19, tzinfo=timezone.utc)  # 10:29:19 BST


def test_a_rebegin_resets_the_run():
    r = replay_fixture("larmor_abort")
    run = r.tracker.run
    assert run.start_text == "Tue 13-Oct-2026 11:39:19"
    assert run.started_at == datetime(2026, 10, 13, 10, 39, 19, tzinfo=timezone.utc)
    assert run.last_counts == 0.0 and not run.end_notified


# ---------------------------------------------------------------------------
# Constructed sequences
# ---------------------------------------------------------------------------

RUNNING = [(0, "_RBNUMBER", "123"), (0, "RUNNUMBER", "00010"), (0, "WDTITLE", "Run ten"),
           (0, "RUNSTATE_STR", "RUNNING"), (0, "STARTTIME", "Tue 13-Oct-2026 09:00:00"), (0, "TOTALUAMPS", 5.0)]
NEXT_RUN = [(20, "RUNSTATE_STR", "ENDING"), (21, "RUNNUMBER", "00011"), (21, "RUNSTATE_STR", "SETUP"),
            (22, "WDTITLE", "Run eleven"), (25, "RUNSTATE_STR", "BEGINNING"), (25, "TOTALUAMPS", 0.0),
            (27, "RUNSTATE_STR", "RUNNING"), (27, "STARTTIME", "Tue 13-Oct-2026 10:00:27")]


def running(**kwargs):
    """A tracker connected mid-run 10, with 8 µA·h credited."""
    r = Replay(**kwargs)(*RUNNING, (10, "TOTALUAMPS", 8.0))
    assert r.log == [("start", "00010", "silent")]
    return r


def test_duplicate_updates_change_nothing():
    """PVWS sometimes delivers an update twice, back to back."""
    r = running()(*[u for u in RUNNING[1:4] + NEXT_RUN for _ in (1, 2)]).settle()
    assert r.log[1:] == [("done", "00010", 8.0), ("start", "00011", "00010")]


@pytest.mark.parametrize("order", ["state first", "counts first"])
def test_a_resubscribe_replay_in_any_order_completes_the_run_once(order):
    """PVWS re-sends every PV in no fixed order; the run that changed while
    disconnected is completed once, and the new run's counts aren't the old one's."""
    r = running()
    r.tracker.resubscribed(r.at(100).now)
    replay = [(100, "RUNSTATE_STR", "RUNNING"), (100, "TOTALUAMPS", 0.5), (100, "RUNNUMBER", "00011")]
    if order == "counts first":
        replay = [replay[1], replay[2], replay[0]]
    r(*replay).settle()
    # Run 10's end wasn't seen and the DAE is already counting run 11, so 10's final is unknown.
    assert r.log[1:] == [("done", "00010", None), ("start", "00011", "00010")]
    assert r.tracker.run.last_counts == 0.5


@pytest.mark.parametrize("order", [
    [(100, "TOTALUAMPS", 9.5), (100, "RUNSTATE_STR", "SETUP"), (100, "RUNNUMBER", "00011")],
    [(100, "RUNNUMBER", "00011"), (100, "RUNSTATE_STR", "SETUP"), (100, "TOTALUAMPS", 9.5)],
    [(100, "RUNNUMBER", "00011"), (100, "TOTALUAMPS", 9.5), (100, "RUNSTATE_STR", "SETUP")],
])
def test_a_run_that_ended_unseen_takes_its_final_counts_from_between_runs(order):
    """Whatever order the replay brings them in: it's decided once both PVs are back."""
    r = reconnect(running())(*order).settle()
    assert r.log[1:] == [("done", "00010", 9.5)]


def test_a_reconnect_that_skips_a_run_reports_it_missed():
    r = running()
    r.tracker.resubscribed(r.at(100).now)
    r((100, "RUNNUMBER", "00012"), (100, "RUNSTATE_STR", "RUNNING"), (100, "TOTALUAMPS", 1.0)).settle()
    assert r.log[1:] == [("done", "00010", None), ("start", "00012", None)]  # no previous: 11 was missed


def test_an_outage_neither_ends_nor_starts_a_run():
    r = running()((30, "RUNNUMBER", "NaN"), (30, "RUNSTATE_STR", "NaN"), (30, "TOTALUAMPS", "NaN"),
                  (31, "TOTALUAMPS", 12.0),  # before RUNNUMBER is back: not credited
                  (40, "RUNSTATE_STR", "RUNNING"), (40, "RUNNUMBER", "00010"), (41, "TOTALUAMPS", 12.5)).settle()
    assert r.log == [("start", "00010", "silent")]
    assert r.tracker.run.last_counts == 12.5


def test_counts_while_replaying_arent_credited():
    r = running()
    r.tracker.resubscribed(r.at(50).now)
    r((50, "TOTALUAMPS", 0.0))
    assert r.tracker.run.last_counts == 8.0 and r.tracker.counts == 0.0


def test_a_resubscribe_replay_that_never_comes_times_out():
    r = running()
    r.tracker.resubscribed(r.at(50).now)
    r.at(50 + REPLAY_TIMEOUT.total_seconds())
    assert r.tracker.replaying  # without the run number: what's credited and started depends on it
    r((61, "RUNNUMBER", "00010"))
    assert not r.tracker.replaying  # without the run state


def test_a_replay_without_the_run_state_carries_on_at_the_timeout():
    r = reconnect(running())((100, "RUNNUMBER", "00010"))
    r.at(100 + REPLAY_TIMEOUT.total_seconds())
    assert not r.tracker.replaying


def test_a_partial_update_is_skipped():
    r = running()
    r.handlers["RUNNUMBER"]({"severity": "MAJOR"}, r.now)
    r.handlers["RUNSTATE_STR"]({"severity": "INVALID"}, r.now)
    assert not r.tracker.replaying and r.tracker.run_number == "00010"


def test_a_run_ending_while_its_start_settles_is_still_recorded():
    """Adopted (quietly: it's already over) as it ends, so its end is recorded."""
    r = running()(*NEXT_RUN[:5], (27, "RUNSTATE_STR", "RUNNING"), (27.2, "TOTALUAMPS", 0.4),
                  (27.5, "RUNSTATE_STR", "ENDING"), (28, "RUNNUMBER", "00012"), (28, "RUNSTATE_STR", "SETUP")).settle()
    assert r.log[1:] == [("done", "00010", 8.0), ("start", "00011", "silent"), ("done", "00011", 0.4)]


def test_a_run_that_never_got_under_way_isnt_recorded():
    r = running()(*NEXT_RUN[:5], (26, "RUNSTATE_STR", "SETUP"), (28, "RUNNUMBER", "00012")).settle()
    assert r.log[1:] == [("done", "00010", 8.0)]


def test_a_start_time_from_before_the_run_isnt_taken_as_its():
    """Connected between runs: the STARTTIME seen is the last run's, so the next
    run's own (when it comes) isn't a re-begin."""
    r = Replay()((0, "RUNNUMBER", "00011"), (0, "RUNSTATE_STR", "SETUP"), (0, "STARTTIME", "Tue 13-Oct-2026 09:00:00"),
                 (5, "RUNSTATE_STR", "RUNNING"), (8, "STARTTIME", "Tue 13-Oct-2026 10:00:05")).settle()
    assert r.log == [("start", "00011", None)]
    assert r.tracker.run.started_at == datetime(2026, 10, 13, 9, 0, 5, tzinfo=timezone.utc)


def test_without_a_start_time_counts_reset_to_zero_is_a_rebegin():
    r = Replay()(*[u for u in RUNNING if u[1] != "STARTTIME"], (10, "TOTALUAMPS", 8.0),
                 (20, "RUNSTATE_STR", "SETUP"), (25, "RUNSTATE_STR", "RUNNING"), (25, "TOTALUAMPS", 0.0)).settle()
    assert r.log == [("start", "00010", "silent"), ("rebegin", "00010")]


# --- Experiments --------------------------------------------------------------

def test_an_rb_change_between_runs_ends_the_experiment():
    r = running()(*NEXT_RUN[:3], (22, "_RBNUMBER", "456"), *NEXT_RUN[3:]).settle()
    assert r.log[1:] == [("done", "00010", 8.0), ("experiment", "123", 1, 8.0, True), ("start", "00011", "00010")]
    assert r.tracker.exp_runs == 0 and r.tracker.rb == "456"
    r((40, "TOTALUAMPS", 3.0), *[(t + 40, s, v.replace("11", "12") if s == "RUNNUMBER" else v)
                                 for t, s, v in NEXT_RUN]).settle()
    assert ("done", "00011", 3.0) in r.log and r.tracker.exp_runs == 1  # run 11 is the new experiment's


def test_an_rb_change_mid_run_counts_that_run_in_the_old_experiment_only():
    r = running()((15, "_RBNUMBER", "456"), *NEXT_RUN).settle()
    assert r.log[1] == ("experiment", "123", 1, 8.0, True)  # "at least": the run in progress
    assert r.tracker.exp_runs == 0  # its completion isn't the new experiment's


def test_an_rb_number_that_comes_back_doesnt_recount_a_run():
    r = running()((15, "_RBNUMBER", "456"), (16, "_RBNUMBER", "123"), *NEXT_RUN).settle()
    assert [d for d in r.log if d[0] == "experiment"] == [("experiment", "123", 1, 8.0, True)]
    assert r.tracker.exp_runs == 0


def test_no_experiment_card_for_rb_0():
    r = running()((15, "_RBNUMBER", "0"), (16, "_RBNUMBER", "789")).settle()
    assert [d[1] for d in r.log if d[0] == "experiment"] == ["123"]


def test_the_first_rb_after_a_resubscribe_marks_a_change_unwatched():
    r = running()(*NEXT_RUN).settle()
    r.tracker.resubscribed(r.at(60).now)
    r((60, "_RBNUMBER", "456")).settle()
    assert r.log[-1] == ("experiment", "123", 1, 8.0, True)
    assert r.tracker.exp_partial


def test_auto_targets_come_from_the_experiments_completed_runs():
    r = Replay(auto=True)(*RUNNING)
    for n, total in enumerate((40.0, 50.0, 60.0, 70.0)):
        t = 100 * (n + 1)
        number = f"000{11 + n}"
        r((t, "TOTALUAMPS", total), (t + 1, "RUNSTATE_STR", "ENDING"), (t + 2, "RUNNUMBER", number),
          (t + 2, "RUNSTATE_STR", "SETUP"), (t + 3, "RUNSTATE_STR", "RUNNING"), (t + 3, "TOTALUAMPS", 0.0)).settle()
    assert list(r.tracker.run_totals) == [40.0, 50.0, 60.0, 70.0]  # the adopted run's final total counts too
    assert (r.tracker.run.target, r.tracker.run.auto_runs) == (55.0, 4)


# --- Restarts -----------------------------------------------------------------

@pytest.mark.parametrize("state, credited", [
    ("RUNNING", 5.0),  # the re-sent counts are credited once the replay's over
    ("ENDING", 5.0),
    ("SETUP", 8.0),  # aborted, say: counts in SETUP aren't the run's
])
def test_a_restart_carries_on_without_repeating_anything(state, credited):
    before = running()
    saved = json.loads(json.dumps(before.tracker.to_snapshot()))
    r = Replay(saved=saved)(*[u if u[1] != "RUNSTATE_STR" else (0, u[1], state) for u in RUNNING]).settle()
    assert r.log == []
    assert r.tracker.run.number == "00010" and r.tracker.run.started_at == before.tracker.run.started_at
    assert r.tracker.run.last_counts == credited


def test_a_restart_after_the_run_ended_completes_it_and_announces_the_next():
    saved = json.loads(json.dumps(running().tracker.to_snapshot()))
    r = Replay(saved=saved)((0, "RUNNUMBER", "00011"), (0, "RUNSTATE_STR", "RUNNING"), (0, "TOTALUAMPS", 2.0)).settle()
    assert r.log == [("done", "00010", None), ("start", "00011", "00010")]


def test_a_first_start_mid_run_is_adopted_silently_but_one_seen_between_runs_is_announced():
    assert running().log == [("start", "00010", "silent")]
    r = Replay()((0, "RUNNUMBER", "00011"), (0, "RUNSTATE_STR", "SETUP"), (5, "RUNSTATE_STR", "RUNNING")).settle()
    assert r.log == [("start", "00011", None)]


def test_snapshot_round_trip():
    r = running(auto=True)(*NEXT_RUN).settle()
    saved = json.loads(json.dumps(r.tracker.to_snapshot()))
    restored = Replay(saved=saved, auto=True).tracker
    assert restored.to_snapshot() == r.tracker.to_snapshot()
    assert restored.run == r.tracker.run


@pytest.mark.parametrize("change", [
    {"run": "junk"}, {"run_number": 10}, {"exp_gen": True}, {"exp_uah": float("nan")}, {"exp_since": "yesterday"},
    {"run_totals": [1, "x"]}, {"run": {"number": "1"}},
])
def test_a_malformed_snapshot_is_ignored(change, caplog):
    saved = {**running().tracker.to_snapshot(), **change}
    tracker = Replay(saved=json.loads(json.dumps(saved))).tracker
    assert tracker.run is None and not tracker.restored
    assert "ignoring malformed saved run state" in caplog.text


def test_a_changed_notify_counts_resets_the_restored_runs_target():
    r = running()
    r.tracker.run.end_notified = True
    tracker = RunTracker(InstrumentConfig("PEARL", 200.0), call_later=lambda *a: None)
    tracker.from_snapshot(json.loads(json.dumps(r.tracker.to_snapshot())), same_target=False)
    assert (tracker.run.target, tracker.run.end_notified) == (200.0, False)


def test_start_time_parsing():
    now = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
    assert parse_start_time("Tue 13-Oct-2026 10:49:43", now) == datetime(2026, 10, 13, 9, 49, 43, tzinfo=timezone.utc)
    assert parse_start_time("Tue 13-Oct-2025 10:49:43", now) is None  # more than a day away
    assert parse_start_time("soon", now) is None


def test_a_rebegin_while_disconnected_is_found_when_the_replay_ends():
    r = running()
    r.tracker.resubscribed(r.at(100).now)
    r((100, "STARTTIME", "Tue 13-Oct-2026 10:01:30"), (100, "TOTALUAMPS", 0.2),
      (100, "RUNNUMBER", "00010"), (100, "RUNSTATE_STR", "RUNNING")).settle()
    assert r.log[1:] == [("rebegin", "00010")]


def test_counts_re_sent_after_a_rebegin_while_disconnected_are_the_new_attempts():
    r = reconnect(running())((100, "STARTTIME", "Tue 13-Oct-2026 10:01:30"), (100, "TOTALUAMPS", 3.0),
                             (100, "RUNNUMBER", "00010"), (100, "RUNSTATE_STR", "PAUSED"),
                             (130, "RUNSTATE_STR", "ENDING"), (131, "RUNNUMBER", "00011"),
                             (131, "RUNSTATE_STR", "SETUP")).settle()
    assert r.log[1:] == [("rebegin", "00010"), ("done", "00010", 3.0)]


def test_an_unknown_run_state_is_taken_as_between_runs_with_one_warning(caplog):
    r = running()((30, "RUNSTATE_STR", "DREAMING"), (31, "RUNSTATE_STR", "DREAMING")).settle()
    assert caplog.text.count("unknown run state DREAMING") == 1
    assert not r.tracker.credited()


# --- Gaps: a reconnect, restart or outage with things changing unseen -------

def reconnect(r, t=100):
    r.tracker.resubscribed(r.at(t).now)
    return r


def test_counts_re_sent_while_replaying_are_credited_when_they_dont_move_again():
    """E.g. ENDING or paused across the gap: the re-sent total is the final one."""
    r = reconnect(running())((100, "TOTALUAMPS", 9.5), (100, "RUNNUMBER", "00010"), (100, "RUNSTATE_STR", "ENDING"),
                             (130, "RUNNUMBER", "00011"), (130, "RUNSTATE_STR", "SETUP")).settle()
    assert r.log[1:] == [("done", "00010", 9.5)]


def test_a_restart_in_pause_credits_the_re_sent_counts():
    saved = json.loads(json.dumps(running().tracker.to_snapshot()))
    r = Replay(saved=saved)((0, "TOTALUAMPS", 9.5), (0, "RUNNUMBER", "00010"), (0, "RUNSTATE_STR", "PAUSED"),
                            (30, "RUNNUMBER", "00011"), (30, "RUNSTATE_STR", "SETUP")).settle()
    assert r.log == [("done", "00010", 9.5)]


@pytest.mark.parametrize("updates, final", [
    # The DAE is between runs and its counts came first: they're the run's final total.
    ([(40, "TOTALUAMPS", 12.0), (40, "RUNSTATE_STR", "SETUP"), (40, "RUNNUMBER", "00011")], 12.0),
    # Counts back after the others, still in SETUP: they're the final too, not the stale 8.0.
    ([(40, "RUNSTATE_STR", "SETUP"), (40, "RUNNUMBER", "00011"), (41, "TOTALUAMPS", 12.0)], 12.0),
    # The next run began before any counts came: unknown.
    ([(40, "RUNSTATE_STR", "SETUP"), (40, "RUNNUMBER", "00011"), (41, "RUNSTATE_STR", "BEGINNING"),
      (41, "TOTALUAMPS", 0.0)], None),
])
def test_a_run_that_ends_during_an_outage(updates, final):
    r = running()((30, "RUNNUMBER", "NaN"), (30, "RUNSTATE_STR", "NaN"), (30, "TOTALUAMPS", "NaN"), *updates).settle()
    assert r.log[1:] == [("done", "00010", final)]


def test_a_run_that_ended_unseen_isnt_given_a_later_runs_counts():
    """Two runs passed while disconnected: 30.0 is run 11's, not 10's."""
    r = reconnect(running())((100, "TOTALUAMPS", 30.0), (100, "RUNSTATE_STR", "SETUP"),
                             (100, "RUNNUMBER", "00012")).settle()
    assert r.log[1:] == [("done", "00010", None)]


def test_a_new_runs_start_time_re_sent_first_after_a_reconnect_isnt_a_rebegin():
    """The run changed while disconnected; the replay sends its STARTTIME and
    counts before RUNSTATE and RUNNUMBER."""
    r = reconnect(running())((100, "STARTTIME", "Tue 13-Oct-2026 10:01:00"), (100, "TOTALUAMPS", 0.3),
                             (100, "RUNSTATE_STR", "RUNNING"), (100, "RUNNUMBER", "00011"),
                             (110, "TOTALUAMPS", 0.0), (120, "TOTALUAMPS", 0.6)).settle()
    assert r.log[1:] == [("done", "00010", None), ("start", "00011", "00010")]
    assert r.tracker.run.start_text == "Tue 13-Oct-2026 10:01:00"


def test_a_title_re_sent_while_replaying_isnt_given_to_the_old_run():
    r = reconnect(running())((100, "WDTITLE", "Run eleven"), (100, "RUNSTATE_STR", "RUNNING"),
                             (100, "RUNNUMBER", "00011")).settle()
    assert r.decisions.previous.title == "Run ten" and r.tracker.run.title == "Run eleven"


def test_a_run_whose_final_is_unknown_makes_the_experiment_at_least():
    r = reconnect(running(), 100)((100, "RUNSTATE_STR", "RUNNING"), (100, "RUNNUMBER", "00011")).settle()
    assert r.tracker.exp_partial and r.tracker.exp_runs == 1


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



@pytest.mark.parametrize("next_number", [
    [(120, "RUNNUMBER", "00012"), (120, "RUNSTATE_STR", "SETUP")],
    [(120, "RUNSTATE_STR", "SETUP"), (121, "RUNNUMBER", "00012")],  # SETUP first, as ZOOM often sends them
])
def test_a_run_first_seen_ending_after_a_reconnect_is_recorded(next_number):
    r = reconnect(running())((100, "RUNNUMBER", "00011"), (100, "RUNSTATE_STR", "ENDING"), (100, "TOTALUAMPS", 4.0),
                             *next_number).settle()
    # Run 10's final is unknown (11 came and went unseen); 11 is adopted as it ends.
    assert r.log[1:] == [("done", "00010", None), ("start", "00011", "silent"), ("done", "00011", 4.0)]


def test_a_stale_ending_replayed_doesnt_adopt_the_next_run():
    """Run 10 was ending when the replay came, and the number moved on before its SETUP was re-sent."""
    r = reconnect(running())((100, "RUNSTATE_STR", "ENDING"), (100, "TOTALUAMPS", 13.0), (100.2, "RUNNUMBER", "00011"),
                             (100.2, "RUNSTATE_STR", "SETUP"), *NEXT_RUN[3:]).settle()
    assert r.log[1:] == [("done", "00010", 13.0), ("start", "00011", "00010")]


def test_a_final_still_due_when_another_run_passes_is_unknown():
    r = running()((30, "RUNNUMBER", "NaN"), (30, "RUNSTATE_STR", "NaN"), (30, "TOTALUAMPS", "NaN"),
                  (40, "RUNSTATE_STR", "SETUP"), (40, "RUNNUMBER", "00011"))  # run 10 ended unseen; no counts yet
    r((50, "RUNNUMBER", "NaN"), (50, "RUNSTATE_STR", "NaN"), (50, "TOTALUAMPS", "NaN"),
      (200, "TOTALUAMPS", 30.0), (200, "RUNSTATE_STR", "SETUP"), (200, "RUNNUMBER", "00012")).settle()
    assert r.log[1:] == [("done", "00010", None)]  # not run 11's 30 µA·h


def test_a_replayed_ending_of_a_later_run_isnt_taken_as_the_runs_final():
    """Run 10 ended and 11 ran unseen; the replay lands in 11's ENDING, and SETUP comes before 12.
    Run 10's final is unknown; run 11 is adopted once its number moves on, with its own final."""
    r = reconnect(running())((100, "RUNSTATE_STR", "ENDING"), (100, "RUNNUMBER", "00011"), (100, "TOTALUAMPS", 30.0),
                             (100, "STARTTIME", "Tue 13-Oct-2026 10:30:00"),  # not run 10's
                             (102, "RUNSTATE_STR", "SETUP"), (102, "RUNNUMBER", "00012")).settle()
    assert r.log[1:] == [("done", "00010", None), ("start", "00011", "silent"), ("done", "00011", 30.0)]


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


def test_a_run_number_going_back_is_ignored(caplog):
    r = running()(*NEXT_RUN).settle()
    r((40, "RUNNUMBER", "00010"), (41, "RUNNUMBER", "00011")).settle()
    assert [d for d in r.log if d[0] == "done"] == [("done", "00010", 8.0)]  # not recounted
    assert "ignoring run number 00010, already completed" in caplog.text
    r((50, "RUNNUMBER", "00003"))  # renumbered
    assert r.tracker.run_number == "00003"


def test_counts_arent_credited_after_a_replay_timeout_without_the_run_number():
    r = reconnect(running())
    r((100, "RUNSTATE_STR", "RUNNING"))
    r.at(100 + REPLAY_TIMEOUT.total_seconds())  # gave up waiting
    r((112, "TOTALUAMPS", 50.0))
    assert r.tracker.replaying and r.tracker.run.last_counts == 8.0


NO_START_TIME = [u for u in RUNNING if u[1] != "STARTTIME"]


def test_without_its_start_time_a_run_isnt_taken_as_just_begun():
    """It may have begun long before the daemon saw it."""
    r = Replay()(*NO_START_TIME, (10, "TOTALUAMPS", 8.0)).settle()
    r((20, "_RBNUMBER", "456"))
    assert ("experiment", "123", 1, 8.0, True) in r.log


def test_a_run_ending_when_the_rb_number_changes_stays_the_old_experiments():
    r = running()(*NEXT_RUN, (30, "TOTALUAMPS", 2.0), (40, "RUNSTATE_STR", "ENDING")).settle()
    r((45, "_RBNUMBER", "456"))
    assert ("experiment", "123", 2, 10.0, True) in r.log  # runs 10 and 11, though 11 began 18s before


def test_counts_a_previous_run_took_as_its_final_arent_an_adopted_runs_too():
    """Run 10 had no STARTTIME, so a replay of 11's ENDING with SETUP first can't be told
    from 10's: 10 takes the counts, and 11 is recorded without them, not counted twice."""
    r = Replay()(*NO_START_TIME, (10, "TOTALUAMPS", 8.0)).settle()
    reconnect(r)((100, "RUNSTATE_STR", "ENDING"), (100, "RUNNUMBER", "00011"), (100, "TOTALUAMPS", 30.0),
                 (102, "RUNSTATE_STR", "SETUP"), (103, "RUNNUMBER", "00012")).settle()
    assert r.log[1:] == [("done", "00010", 30.0), ("start", "00011", "silent"), ("done", "00011", None)]
    assert (r.tracker.exp_runs, r.tracker.exp_uah) == (1, 30.0)


def test_an_adopted_run_without_counts_has_no_final():
    r = reconnect(running())((100, "RUNNUMBER", "00011"), (100, "RUNSTATE_STR", "ENDING"), (100, "TOTALUAMPS", "NaN"),
                             (120, "RUNSTATE_STR", "SETUP"), (121, "RUNNUMBER", "00012")).settle()
    assert r.log[-2:] == [("start", "00011", "silent"), ("done", "00011", None)]


def test_a_start_time_that_cant_be_read_doesnt_make_a_run_just_begun():
    r = Replay()(*NO_START_TIME, (0, "STARTTIME", "Sat 10-Oct-2026 09:00:00"), (10, "TOTALUAMPS", 8.0)).settle()
    r((15, "_RBNUMBER", "456"))  # its STARTTIME is days old, so ignored: it began long ago
    assert ("experiment", "123", 1, 8.0, True) in r.log


def test_runs_that_both_end_on_zero_counts_keep_their_finals():
    """With the beam off, an adopted run's 0 µA·h matching the previous run's is no clash."""
    r = running()((15, "TOTALUAMPS", 0.0))
    r.tracker.run.start_text = None  # as if no STARTTIME was read
    r(*NEXT_RUN[:3]).settle()
    reconnect(r)((100, "RUNSTATE_STR", "ENDING"), (100, "RUNNUMBER", "00012"), (100, "TOTALUAMPS", 0.0),
                 (102, "RUNSTATE_STR", "SETUP"), (103, "RUNNUMBER", "00013")).settle()
    assert ("done", "00012", 0.0) in r.log
