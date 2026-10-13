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
        self.log.append(("start", run.number, "silent" if silent else previous.number if previous else None))

    def run_completed(self, run):
        self.log.append(("done", run.number, round(run.last_counts, 3) if run.final_known else None))

    def run_rebegun(self, run):
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
    r = running()(*RUNNING[1:4], *RUNNING[1:4], *NEXT_RUN, *NEXT_RUN).settle()
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
    assert r.log[1:] == [("done", "00010", None), ("start", "00011", None)]
    assert r.tracker.run.last_counts == 0.5


def test_a_run_that_ended_unseen_takes_its_final_counts_from_between_runs():
    r = running()
    r.tracker.resubscribed(r.at(100).now)
    r((100, "TOTALUAMPS", 9.5), (100, "RUNSTATE_STR", "SETUP"), (100, "RUNNUMBER", "00011")).settle()
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
    assert not r.tracker.replaying


def test_a_partial_update_is_skipped():
    r = running()
    r.handlers["RUNNUMBER"]({"severity": "MAJOR"}, r.now)
    r.handlers["RUNSTATE_STR"]({"severity": "INVALID"}, r.now)
    assert not r.tracker.replaying and r.tracker.run_number == "00010"


def test_a_run_ended_within_the_settle_is_logged_not_started(caplog):
    caplog.set_level("INFO")
    r = running()(*NEXT_RUN[:5], (27, "RUNSTATE_STR", "RUNNING"), (27.5, "RUNSTATE_STR", "ENDING"),
                  (28, "RUNNUMBER", "00012"), (28, "RUNSTATE_STR", "SETUP")).settle()
    assert r.log[1:] == [("done", "00010", 8.0)]
    assert "run 00011 ended before it was announced" in caplog.text


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

@pytest.mark.parametrize("state, expected", [
    ("RUNNING", []),  # the same run carries on
    ("ENDING", []),
    ("SETUP", []),
])
def test_a_restart_carries_on_without_repeating_anything(state, expected):
    before = running()
    saved = json.loads(json.dumps(before.tracker.to_snapshot()))
    r = Replay(saved=saved)(*[u if u[1] != "RUNSTATE_STR" else (0, u[1], state) for u in RUNNING]).settle()
    assert r.log == expected
    assert r.tracker.run.number == "00010" and r.tracker.run.started_at == before.tracker.run.started_at


def test_a_restart_after_the_run_ended_completes_it_and_announces_the_next():
    saved = json.loads(json.dumps(running().tracker.to_snapshot()))
    r = Replay(saved=saved)((0, "RUNNUMBER", "00011"), (0, "RUNSTATE_STR", "RUNNING"), (0, "TOTALUAMPS", 2.0)).settle()
    assert r.log == [("done", "00010", None), ("start", "00011", None)]


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
    tracker = Replay(saved=json.loads(json.dumps(r.tracker.to_snapshot()))).tracker
    tracker.from_snapshot(json.loads(json.dumps(r.tracker.to_snapshot())), same_target=False)
    assert not tracker.run.end_notified


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


def test_an_unknown_run_state_is_taken_as_between_runs_with_one_warning(caplog):
    r = running()((30, "RUNSTATE_STR", "DREAMING"), (31, "RUNSTATE_STR", "DREAMING")).settle()
    assert caplog.text.count("unknown run state DREAMING") == 1
    assert not r.tracker.credited()
