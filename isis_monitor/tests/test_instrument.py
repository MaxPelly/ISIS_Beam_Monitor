"""InstrumentTracker: the cards and records its RunTracker's decisions make
(new run, restart, finishing, stall, experiment), and its saved state. How
runs are told apart is test_runs.py's."""
import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from isis_monitor.config import InstrumentConfig
from isis_monitor.daemon_state import DaemonState
from isis_monitor.instrument import InstrumentTracker, _fit_rate
from isis_monitor.runs import REPLAY_TIMEOUT, SETTLE, auto_target
from isis_monitor.tests.helpers import fake_channel
from isis_monitor.tests.test_runs import NEXT_RUN, RUNNING, T0, fixture, update

# Seen between runs first, so run 10's start is announced.
BEGIN = [(0, "_RBNUMBER", "123"), (0, "RUNNUMBER", "00010"), (0, "WDTITLE", "Run ten"), (0, "RUNSTATE_STR", "SETUP"),
         (5, "RUNSTATE_STR", "BEGINNING"), (5, "TOTALUAMPS", 0.0), (7, "RUNSTATE_STR", "RUNNING"),
         (7, "STARTTIME", "Tue 13-Oct-2026 10:00:07")]
END = [(0, "RUNSTATE_STR", "ENDING"), (1, "RUNNUMBER", "00011"), (1, "RUNSTATE_STR", "SETUP")]
NEW_RUN = "PEARL: New run started"


class Live:
    """An InstrumentTracker as BeamMonitor drives it (or `tracker`), fed
    (seconds, PV suffix, value) updates on a fake clock, recording its runs
    into a DaemonState. Its settle timer is replaced by ticking before every
    update. `saved` is restored as after a restart (through JSON)."""
    def __init__(self, name="PEARL", target=1000.0, *, saved=None, state=None, channel=None, at=0.0, tracker=None,
                 stall_minutes=1.0, finish_warning_minutes=15.0, **instrument):
        self.power = "high"
        if tracker is None:
            tracker = InstrumentTracker(
                InstrumentConfig(name, target, **instrument), channel or fake_channel(), lambda _beam: self.power,
                stall_minutes, finish_warning_minutes, sink=state or DaemonState(instrument_names=[name]))
        self.tracker, self.state, self.channel = tracker, tracker.sink, tracker.experiment_channel
        if saved is not None:
            tracker.from_snapshot(json.loads(json.dumps(saved)))
        tracker.runs._call_later = lambda delay, callback: None
        self.handlers = {pv.split(":DAE:")[1]: h for pv, h in tracker.pv_handlers().items()}
        self.now = T0
        self.reconnect(at)

    @classmethod
    def restarted(cls, old, at, **kwargs):
        """A new daemon carrying on from `old`'s saved view, sending to the same channel and state."""
        return cls(old.tracker.instrument.name, old.tracker.instrument.notify_counts, saved=old.tracker.view(),
                   state=old.state, channel=old.channel, at=at, **kwargs)

    async def __call__(self, *updates, offset=0.0):
        for t, suffix, value in updates:
            self.at(t + offset)
            await self.handlers[suffix](update(suffix, value), self.now)
        return self

    def at(self, t):
        assert t >= self.seconds, "the clock went back"
        self.now = T0 + timedelta(seconds=t)
        self.tracker.runs.tick(self.now)
        return self

    @property
    def seconds(self):
        return (self.now - T0).total_seconds()

    def settle(self):
        """Let any timer fire."""
        return self.at(self.seconds + max(SETTLE, REPLAY_TIMEOUT).total_seconds() + 1)

    def reconnect(self, t):
        self.tracker.resubscribed(self.at(t).now)
        return self

    async def check(self, t):
        """The stall check, at `t`."""
        await self.tracker.check_collection_progress(self.at(t).now)

    async def counts(self, *readings, base=60.0):
        """(minutes after `base` seconds, µA·h) readings."""
        return await self(*[(base + 60 * m, "TOTALUAMPS", v) for m, v in readings])

    @property
    def cards(self):
        return [c.args[0] for c in self.channel.broadcast.call_args_list]

    @property
    def titles(self):
        return [c.title for c in self.cards]

    @property
    def completions(self):
        """(instrument, run number, µA·h) of each run recorded completed."""
        return [(name, number, round(uah, 3)) for _, name, number, uah in self.state.run_completions]


async def begun(**kwargs):
    """Run 10 begun with its start seen, so announced."""
    r = (await Live(**kwargs)(*BEGIN)).settle()
    assert r.titles == [NEW_RUN] and r.tracker.runs.run.number == "00010"
    return r


async def under_way(**kwargs):
    """Connected mid-run 10, at 5 µA·h: adopted without a card."""
    r = (await Live(**kwargs)(*RUNNING)).settle()
    assert r.titles == [] and r.tracker.runs.run.number == "00010"
    return r


async def replay(name, **kwargs):
    instrument, updates = fixture(name)
    return (await Live(instrument, **kwargs)(*updates)).settle()


# ---------------------------------------------------------------------------
# Run cards and records
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name, titles, completed", [
    # Adopted mid-run without a card; the next run is announced.
    ("gem_transition", ["GEM: New run started"], [("GEM", "102941", 13.094)]),
    ("wish_long_ending", [], [("WISH", "65044", 22.67)]),
    ("hifi_setup_retitles", ["HIFI: New run started"], []),
    # Re-begun under the same number: a card only if it was retitled first.
    ("inter_aborts", [], []),
    ("larmor_abort", ["LARMOR: Run 101373 restarted"], []),
    ("mari_quick_abort", ["MARI: New run started", "MARI: Run 31997 restarted"], [("MARI", "31996", 160.096)]),
    ("emu_failed_begins", [], []),
    ("enginx_short_runs", ["ENGINX: New run started"] * 5, [("ENGINX", f"3796{n}", 0.148 if n == 16 else 0.149)
                                                            for n in range(16, 21)]),
    ("osiris_outages", [], []),
    ("osiris_late_nan", [], []),
    ("polaris_saving", [], []),
    ("polref_nan", [], []),
])
async def test_recorded_sequences(name, titles, completed):
    r = await replay(name)
    assert r.titles == titles
    assert r.completions == completed


async def test_a_retitled_rebegin_card_names_the_run_and_its_new_title():
    (card,) = (await replay("larmor_abort")).cards
    assert (card.text, dict(card.facts)) == ("{RT1_Standard}_TRANS", {"Run number": "101373"})
    assert card.timestamp == datetime(2026, 10, 13, 10, 39, 19, tzinfo=timezone.utc)  # its STARTTIME


@pytest.mark.parametrize("next_runs, facts", [
    (NEXT_RUN, {"Run number": "11", "Previous run": "Run ten", "Duration": "1h 0m",  # from its STARTTIME
                "Final total collected": "8.0 µA·h", "Target": "1000 µA·h"}),
    # Reconnected with run 11 already counting: run 10's final is unknown.
    ([(100, "RUNSTATE_STR", "RUNNING"), (100, "RUNNUMBER", "00011"), (100, "TOTALUAMPS", 0.5)],
     {"Run number": "11", "Previous run": "Run ten", "Duration": "1h 1m", "Final total collected": "unknown",
      "Target": "1000 µA·h"}),
    # Run 11 was missed altogether: nothing to say of the one before.
    ([(100, "RUNSTATE_STR", "RUNNING"), (100, "RUNNUMBER", "00012"), (100, "TOTALUAMPS", 0.5)],
     {"Run number": "12", "Target": "1000 µA·h"}),
])
async def test_a_new_run_card_reports_on_the_run_before(next_runs, facts):
    r = await under_way()
    await r((12, "TOTALUAMPS", 8.0))
    if next_runs is not NEXT_RUN:
        r.reconnect(100)
    (await r(*next_runs)).settle()
    (card,) = r.cards
    assert card.title == NEW_RUN and dict(card.facts) == facts


@pytest.mark.parametrize("retitled", [True, False])
async def test_an_abort_and_rebegin_under_the_same_number(retitled):
    """POLARIS-style: ~40 minutes in, the run aborts, is retitled in SETUP (a
    new temperature), and begins again under its number: a restart card, not a
    new run. INTER-style, re-begun with its title: nothing. Neither is counted
    as another run, and the finishing card is due again."""
    r = await begun(target=25.0)
    await r((60, "TOTALUAMPS", 5.0), (2400, "TOTALUAMPS", 30.0))  # past the target: a finishing card
    await r((2401, "RUNSTATE_STR", "ABORTING"), (2402, "RUNSTATE_STR", "SETUP"),
            (2460, "WDTITLE", "Run ten at 100K" if retitled else "Run ten"),
            (2500, "RUNSTATE_STR", "BEGINNING"), (2500, "TOTALUAMPS", 0.0), (2505, "RUNSTATE_STR", "RUNNING"),
            (2505, "STARTTIME", "Tue 13-Oct-2026 10:41:45"))
    restarted = ["PEARL: Run 10 restarted"] if retitled else []
    assert r.titles == [NEW_RUN, "PEARL: Run about to finish", *restarted]
    if retitled:
        assert r.cards[-1].text == "Run ten at 100K"
    assert r.completions == []
    run = r.tracker.runs.run
    assert run.started_at == T0 + timedelta(seconds=2505) and run.last_counts == 0.0

    await r((3000, "TOTALUAMPS", 26.0))
    await r(*END, offset=3600)
    assert r.titles[-1] == "PEARL: Run about to finish"
    assert r.completions == [("PEARL", "10", 26.0)]


async def reconnect_replay(r):
    r.reconnect(100)
    return await r((100, "TOTALUAMPS", 9.0), (100, "RUNSTATE_STR", "RUNNING"), (100, "WDTITLE", "Run ten"),
                   (100, "RUNNUMBER", "00010"), (100, "STARTTIME", "Tue 13-Oct-2026 10:00:07"))


async def restart_replay(r):
    r = Live.restarted(r, at=100)
    return await r((100, "WDTITLE", "Run ten"), (100, "TOTALUAMPS", 9.0), (100, "RUNNUMBER", "00010"),
                   (100, "STARTTIME", "Tue 13-Oct-2026 10:00:07"), (100, "RUNSTATE_STR", "RUNNING"))


async def outage(r):
    nan = [(100, pv, "NaN") for pv in ("RUNNUMBER", "RUNSTATE_STR", "TOTALUAMPS", "STARTTIME", "WDTITLE", "_RBNUMBER")]
    return await r(*nan, (130, "TOTALUAMPS", 9.0), (131, "RUNSTATE_STR", "RUNNING"), (131, "RUNNUMBER", "00010"))


async def rebegin(r):
    return await r((100, "RUNSTATE_STR", "ABORTING"), (101, "RUNSTATE_STR", "SETUP"),
                   (120, "RUNSTATE_STR", "BEGINNING"), (121, "RUNSTATE_STR", "RUNNING"),
                   (121, "STARTTIME", "Tue 13-Oct-2026 10:02:01"), (122, "TOTALUAMPS", 9.0))


@pytest.mark.parametrize("gap", [reconnect_replay, restart_replay, outage, rebegin])
async def test_a_run_is_counted_once_whatever_happens_during_it(gap):
    """What's re-sent after a reconnect or a restart, an outage, or a re-begin
    under the same number neither completes the run nor starts another."""
    r = await begun()
    await r((60, "TOTALUAMPS", 8.0))
    r = (await gap(r)).settle()
    assert r.titles == [NEW_RUN] and r.completions == []
    assert r.tracker.runs.run.number == "00010" and r.tracker.runs.run.last_counts == 9.0

    await r(*END, offset=200)
    assert r.titles == [NEW_RUN] and r.completions == [("PEARL", "10", 9.0)]


@pytest.mark.parametrize("name, updates, completed, announced", [
    # Connected while it was ending: its final total is known.
    ("GEM", fixture("gem_connect_in_ending")[1], [("GEM", "102942", 13.094)], ["102943"]),
    # Seen ending without counts (e.g. its IOC's TOTALUAMPS unreadable): recorded at 0.
    ("PEARL", [(0, "RUNNUMBER", "00010"), (0, "RUNSTATE_STR", "ENDING"), (5, "RUNNUMBER", "00011"),
               (5, "RUNSTATE_STR", "SETUP")], [("PEARL", "10", 0.0)], []),
])
async def test_a_run_adopted_as_it_ends_is_recorded_without_a_card(name, updates, completed, announced):
    r = (await Live(name)(*updates)).settle()
    assert r.completions == completed
    assert [dict(c.facts)["Run number"] for c in r.cards] == announced  # only the next run's, if it began


# ---------------------------------------------------------------------------
# The finishing card
# ---------------------------------------------------------------------------

async def test_the_finishing_card_is_sent_once_at_the_target():
    r = await under_way(target=100.0)
    await r.counts((0, 90.0))
    assert r.titles == []
    await r.counts((1, 110.0), (2, 120.0))
    (card,) = r.cards
    assert (card.title, card.text) == ("PEARL: Run about to finish", "Run ten")
    facts = dict(card.facts)
    assert (facts["Run number"], facts["Collected"], facts["ETA"], facts["Instrument beam"]) == (
        "10", "110.0 / 100 µA·h", "target reached", "high")
    assert r.tracker.runs.run.end_notified


async def test_the_finishing_card_is_sent_ahead_of_the_target_on_its_eta():
    r = await under_way(target=100.0)  # finish_warning_minutes = 15
    await r.counts((0, 70.0), (5, 75.0), (10, 80.0))  # 1 µA·h a minute: 20 minutes to go at 80
    assert r.titles == []
    await r.counts((15, 85.0))
    assert dict(r.cards[0].facts)["ETA"] == "15m"
    await r.counts((16, 85.5), (17, 90.0), (30, 101.0))  # however the ETA wobbles, or the target is passed
    assert len(r.cards) == 1


@pytest.mark.parametrize("readings", [
    [(0, 90.0), (2, 99.0)],  # too little history to trust the rate
    [(0, 0.0), (5, 5.0)],  # rising, but ETA 95 minutes
    [(0, 90.0), (5, 90.0)],  # not rising at all
])
async def test_the_finishing_card_waits_for_the_target_without_a_near_eta(readings):
    r = await under_way(target=100.0)
    await r.counts(*readings)
    assert r.titles == []
    await r.counts((6, 100.5))
    assert r.titles == ["PEARL: Run about to finish"]


async def test_the_eta_rate_fit_is_rate_limited():
    r = await under_way()
    with patch("isis_monitor.instrument._fit_rate", wraps=_fit_rate) as fit:
        await r.counts(*[(i / 12, i * 0.1) for i in range(12 * 6)])  # every 5s for 6 minutes
    assert fit.call_count == 6  # every 30s once there are 3 minutes of history


async def test_finish_warning_minutes_zero_only_sends_at_the_target():
    r = await under_way(target=100.0, finish_warning_minutes=0)
    await r.counts((0, 90.0), (5, 99.0), (6, 100.0))
    assert r.titles == []
    await r.counts((7, 100.1))
    assert r.titles == ["PEARL: Run about to finish"]


def test_fit_rate():
    t = T0
    assert _fit_rate([]) == _fit_rate([(t, 10.0)]) == _fit_rate([(t, 1.0), (t, 5.0)]) == 0.0
    assert _fit_rate([(t, 0.0), (t + timedelta(seconds=10), 100.0)]) == pytest.approx(10.0)


@pytest.mark.parametrize("totals, expected", [
    ([], (130.0, 0)),
    ([100.0, 102.0], (130.0, 0)),  # too few runs: the fallback
    ([100.0, 0.4, 102.0], (130.0, 0)),  # a short run doesn't count towards them
    ([100.0, 102.0, 98.0], (100.0, 3)),
    # One or two mistaken short runs are left out, not averaged in.
    ([100.0, 5.0, 102.0, 0.2, 98.0], (100.0, 3)),
    # Only the last AUTO_RUNS of the rest count; an odd long run is outvoted.
    ([50.0, 50.0, 100.0, 100.0, 300.0, 100.0, 100.0], (100.0, 5)),
    # An aborted run that still counts is outvoted by the median too.
    ([100.0, 40.0, 100.0, 104.0], (100.0, 4)),
])
def test_auto_target(totals, expected):
    assert auto_target(totals, 130.0) == expected


async def test_auto_targets_are_on_the_new_run_and_finishing_cards():
    r = await under_way(target=30.0, auto_counts=True, finish_warning_minutes=0)
    for n, total in enumerate((40.0, 50.0, 60.0)):
        t = 100 * (n + 1)
        await r((t, "TOTALUAMPS", total), (t + 1, "RUNSTATE_STR", "ENDING"), (t + 2, "RUNNUMBER", f"000{11 + n}"),
                (t + 2, "RUNSTATE_STR", "SETUP"), (t + 3, "RUNSTATE_STR", "RUNNING"), (t + 3, "TOTALUAMPS", 0.0))
        r.settle()
    targets = [dict(c.facts)["Target"] for c in r.cards if c.title == NEW_RUN]
    assert targets == ["30 µA·h (auto's fallback, until 3 runs)"] * 2 + ["50 µA·h (auto, from 3 runs)"]
    await r((400, "TOTALUAMPS", 50.5))
    assert dict(r.cards[-1].facts)["Collected"] == "50.5 / 50 µA·h (auto)"


# ---------------------------------------------------------------------------
# Stalls
# ---------------------------------------------------------------------------

async def stalled(**kwargs):
    """Under way at 100 µA·h from 60s; the stall check finds them unmoved at
    420s (5 minutes on) and, with stall_minutes = 1, warns at 480s."""
    r = await under_way(**kwargs)
    await r.counts((0, 100.0))
    await r.check(420)
    assert r.tracker.stalled_since == r.now
    return r


@pytest.mark.parametrize("power, warned", [("high", True), ("low", True), ("off", False)])
async def test_a_stall_is_warned_of_while_the_instruments_beam_is_on(power, warned):
    r = await stalled()
    r.power = power
    await r.check(479)
    assert r.titles == []
    await r.check(480)
    await r.check(540)
    assert r.titles == (["PEARL: Data collection stalled"] if warned else [])
    if warned:
        assert r.cards[0].text == "No µA·h collected for 1m while TS1 beam is on."
        assert dict(r.cards[0].facts) == {"Run number": "10"}


async def test_no_stall_without_enough_history():
    r = await under_way()
    await r.counts((0, 100.0))
    await r.check(120)
    await r.check(300)
    assert r.titles == [] and r.tracker.stalled_since is None


async def test_counts_updated_in_batches_arent_a_stall():
    """Movement is judged over STALL_CHECK_WINDOW, not since the last check."""
    r = await under_way()
    await r.counts((0, 50.0), (2, 100.0))  # at 60s and 180s
    await r.check(420)
    await r.check(479)
    assert r.titles == [] and r.tracker.stalled_since is None


@pytest.mark.parametrize("updates, checked", [
    ([(400, "RUNSTATE_STR", "VETOING")], True),
    ([(400, "RUNSTATE_STR", "PAUSED")], False),
    ([(400, "RUNSTATE_STR", "WAITING")], False),
    ([(400, "RUNSTATE_STR", "SAVING")], False),
    ([(400, "RUNSTATE_STR", "NaN")], False),  # unknown, e.g. its IOC is down
    ([(400, "RUNSTATE_STR", "ABORTING"), (401, "RUNSTATE_STR", "SETUP")], False),
    ([(400 + t, s, v) for t, s, v in END], False),  # ended: between runs
])
async def test_stalls_are_only_checked_while_a_run_is_counting(updates, checked):
    r = await under_way()
    await r.counts((0, 100.0))
    await r(*updates)
    await r.check(420)
    await r.check(480)
    assert r.titles == (["PEARL: Data collection stalled"] if checked else [])


@pytest.mark.parametrize("gap", [None, "paused", "disconnected"])
async def test_a_resumed_card_follows_a_stall_warning(gap):
    """Pausing (e.g. to fix it) or a PVWS drop doesn't lose the stall's start."""
    r = await stalled()
    await r.check(480)
    if gap == "paused":
        await r((600, "RUNSTATE_STR", "PAUSED"))
        await r.check(700)
        await r((800, "RUNSTATE_STR", "RUNNING"))
    elif gap == "disconnected":
        r.at(700).tracker.reset_stall_clock()  # as BeamMonitor does while PVWS is down
    await r((900, "TOTALUAMPS", 101.0))
    await r.check(900)
    await r.check(960)
    assert r.titles == ["PEARL: Data collection stalled", "PEARL: Data collection resumed"]
    assert r.cards[-1].text == "µA·h are being collected again, after 8m without."


async def test_a_run_that_has_collected_nothing_isnt_warned_of():
    """E.g. a muon instrument, whose µA·h stay 0."""
    r = (await Live()(*[u for u in RUNNING if u[1] != "TOTALUAMPS"], (0, "TOTALUAMPS", 0.0))).settle()
    await r.counts((0, 0.0), (5, 0.0))
    await r.check(420)
    await r.check(600)
    assert r.titles == []


async def test_a_resumed_card_follows_even_a_stall_longer_than_the_sample_window():
    """Flat counts send no updates, so the last sample before the window is kept as the baseline."""
    r = await stalled()
    await r.check(480)
    await r((1260, "TOTALUAMPS", 101.0))
    await r.check(1260)
    assert r.titles == ["PEARL: Data collection stalled", "PEARL: Data collection resumed"]
    assert r.cards[-1].text == "µA·h are being collected again, after 14m without."


async def test_a_new_run_after_a_stall_warning_sends_no_resumed_card():
    r = await stalled()
    await r.check(480)
    await r(*END, offset=500)
    await r((505, "RUNSTATE_STR", "RUNNING"), (520, "TOTALUAMPS", 5.0))
    await r.check(600)
    assert r.titles == ["PEARL: Data collection stalled", NEW_RUN]


async def test_a_rebegin_restarts_the_stall_clock():
    r = await stalled()
    await r((430, "RUNSTATE_STR", "ABORTING"), (431, "RUNSTATE_STR", "SETUP"), (440, "RUNSTATE_STR", "RUNNING"),
            (440, "STARTTIME", "Tue 13-Oct-2026 10:07:20"))
    assert r.tracker.stalled_since is None and not r.tracker.samples
    await r.check(480)
    assert r.titles == []


# ---------------------------------------------------------------------------
# Experiments and the view
# ---------------------------------------------------------------------------

async def test_an_rb_number_change_sends_the_old_experiments_card():
    r = await under_way()
    await r((12, "TOTALUAMPS", 8.0), *NEXT_RUN[:3], (22, "_RBNUMBER", "456"), *NEXT_RUN[3:])
    card = next(c for c in r.cards if c.title == "PEARL: Experiment finished")
    assert card.text == "RB 123"
    assert dict(card.facts) == {"Runs": "at least 1", "Total collected": "at least 8.0 µA·h",
                                "Average run": "8.0 µA·h", "Duration": "at least 22s"}


async def test_the_view_shows_the_run():
    r = Live(target=100.0)
    assert {k: v for k, v in r.tracker.view().items() if k != "runs"} == {
        "format": 3, "notify_counts": 100.0, "auto": False, "beam_target": "TS1", "run_number": "",
        "run_state": "", "run_name": "", "run_started_at": None, "counts": -1.0, "target": 100.0, "auto_runs": 0,
        "rb_number": ""}
    await r(*RUNNING)
    r.settle()
    view = r.tracker.view()
    assert (view["run_number"], view["run_state"], view["run_name"], view["run_started_at"], view["counts"],
            view["rb_number"]) == ("10", "RUNNING", "Run ten", "2026-10-13T08:00:00+00:00", 5.0, "123")


# ---------------------------------------------------------------------------
# Saved state
# ---------------------------------------------------------------------------

async def test_the_snapshot_round_trips():
    r = await under_way(auto_counts=True)
    await r((12, "TOTALUAMPS", 40.0), *NEXT_RUN)
    r.settle()
    restored = Live.restarted(r, at=100, auto_counts=True)
    assert restored.tracker.to_snapshot() == r.tracker.to_snapshot()
    assert restored.tracker.runs.restored and list(restored.tracker.runs.run_totals) == [40.0]


async def test_the_1_3_0rc_saved_state_carries_on_without_a_reset():
    """1.3.0rc's format 2 had the run tracker's state as "shadow", its runs
    without begun_title."""
    r = await begun()
    await r((60, "TOTALUAMPS", 8.0))
    shadow = json.loads(json.dumps(r.tracker.runs.to_snapshot()))
    del shadow["run"]["begun_title"]
    rc = {"format": 2, "notify_counts": 1000.0, "auto": False, "run_name": "Run ten", "counts": 8.0,
          "end_notified": False, "shadow": shadow}
    upgraded = Live(saved=rc, state=r.state, channel=r.channel, at=100)
    assert upgraded.tracker.runs.run.number == "00010" and upgraded.tracker.runs.rb == "123"
    await upgraded(*[(100, s, v) for _, s, v in BEGIN[1:3]], (100, "RUNSTATE_STR", "RUNNING"),
                   (100, "STARTTIME", "Tue 13-Oct-2026 10:00:07"))
    await upgraded((110, "TOTALUAMPS", 9.0))
    await upgraded(*END, offset=200)
    assert r.titles == [NEW_RUN] and r.completions == [("PEARL", "10", 9.0)]


async def test_a_finishing_card_sent_under_1_3_0rc_isnt_sent_again():
    """1.3.0rc kept that in the old logic's state; its shadow never sent one."""
    r = await begun()
    shadow = json.loads(json.dumps(r.tracker.runs.to_snapshot()))
    upgraded = Live(saved={"format": 2, "notify_counts": 1000.0, "auto": False, "end_notified": True,
                           "shadow": shadow}, state=r.state, channel=r.channel, at=100)
    await upgraded(*[(100, s, v) for _, s, v in BEGIN[1:3]], (100, "RUNSTATE_STR", "RUNNING"),
                   (100, "STARTTIME", "Tue 13-Oct-2026 10:00:07"), (110, "TOTALUAMPS", 1500.0))
    assert r.titles == [NEW_RUN]  # past its target, but no second finishing card


async def test_a_retitled_rebegin_during_a_reconnect_gets_its_restart_card():
    r = await begun()
    r.reconnect(100)
    # Aborted and begun again under a new title, unseen; the replay brings it all before it ends.
    await r((100, "WDTITLE", "Run ten at 100K"), (100, "STARTTIME", "Tue 13-Oct-2026 10:01:30"),
            (100, "RUNNUMBER", "00010"), (100, "RUNSTATE_STR", "RUNNING"))
    assert r.titles == [NEW_RUN, "PEARL: Run 10 restarted"] and r.cards[-1].text == "Run ten at 100K"


async def test_a_run_from_1_3_0rc_retitled_and_rebegun_gets_a_restart_card():
    r = await begun()
    shadow = json.loads(json.dumps(r.tracker.runs.to_snapshot()))
    del shadow["run"]["begun_title"]
    upgraded = Live(saved={"format": 2, "notify_counts": 1000.0, "auto": False, "shadow": shadow},
                    state=r.state, channel=r.channel, at=100)
    await upgraded((100, "RUNNUMBER", "00010"), (100, "RUNSTATE_STR", "RUNNING"), (100, "WDTITLE", "Run ten"),
                   (100, "STARTTIME", "Tue 13-Oct-2026 10:00:07"), (120, "RUNSTATE_STR", "ABORTING"),
                   (121, "RUNSTATE_STR", "SETUP"), (125, "WDTITLE", "Run ten at 100K"),
                   (130, "RUNSTATE_STR", "RUNNING"), (130, "STARTTIME", "Tue 13-Oct-2026 10:02:10"))
    assert r.titles == [NEW_RUN, "PEARL: Run 10 restarted"]


SAVED_RUN = {"number": "00010", "title": "Run ten", "started_at": "2026-10-13T08:00:00+00:00", "start_text": None,
             "exp_gen": 1, "target": 1000.0, "auto_runs": 0, "end_notified": False, "last_counts": 5.0,
             "ended_at": None, "final_known": False, "begun_title": "Run ten"}


def saved_runs(**changes):
    return {"run_number": "00010", "run": SAVED_RUN, "last_completed": "", "rb": "123", "exp_gen": 1,
            "exp_since": "2026-10-13T09:00:00", "exp_partial": True, "exp_runs": 0, "exp_uah": 0.0,
            "run_totals": [], **changes}


def entry(runs):
    return {"format": 3, "notify_counts": 1000.0, "auto": False, "runs": runs}


@pytest.mark.parametrize("saved, logged", [
    ("junk", "ignoring saved state from an older version"),
    ({}, "ignoring saved state from an older version"),
    ({"format": 2, "run_name": "Run 4", "counts": 3.0}, "ignoring saved state from an older version"),
    ({"format": 2, "shadow": None}, "ignoring malformed saved run state (not an object)"),
    (entry("junk"), "ignoring malformed saved run state"),
    (entry(saved_runs(run={**SAVED_RUN, "started_at": "yesterday"})), "ignoring malformed saved run state"),
    (entry(saved_runs(run={**SAVED_RUN, "end_notified": "yes"})), "ignoring malformed saved run state"),
    (entry(saved_runs(exp_runs=True)), "ignoring malformed saved run state"),
    (entry(saved_runs(run_totals=[1.0, float("nan")])), "ignoring malformed saved run state"),
])
def test_an_older_or_malformed_saved_entry_is_ignored_whole(caplog, saved, logged):
    fresh = Live().tracker.to_snapshot()
    with caplog.at_level(logging.WARNING):
        r = Live(saved=saved)
    assert r.tracker.to_snapshot() == fresh and not r.tracker.runs.restored
    assert f"PEARL: {logged}" in caplog.text


def test_saved_times_without_a_timezone_are_utc():
    r = Live(saved=entry(saved_runs(run={**SAVED_RUN, "started_at": "2026-10-13T08:00:00"})))
    assert r.tracker.runs.run.started_at == datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)
    assert r.tracker.runs.exp_since == T0


@pytest.mark.parametrize("notify_counts, auto, renotified", [
    (1000.0, False, False),  # a plain restart: not sent again
    (800.0, False, True),  # notify_counts changed in the config save behind the restart
    (1000.0, True, True),  # switched to auto
])
async def test_a_finishing_card_sent_before_a_restart_is_due_again_only_for_a_new_target(notify_counts, auto,
                                                                                     renotified):
    run = {**SAVED_RUN, "end_notified": True, "last_counts": 1001.0}
    saved = {**entry(saved_runs(run=run)), "notify_counts": notify_counts, "auto": auto}
    r = Live(saved=saved, at=100, finish_warning_minutes=0)
    assert r.tracker.runs.run.end_notified is not renotified
    await r((100, "RUNNUMBER", "00010"), (100, "RUNSTATE_STR", "RUNNING"), (110, "TOTALUAMPS", 1002.0))
    assert r.titles == (["PEARL: Run about to finish"] if renotified else [])
