import asyncio
import contextlib
import json
import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response
import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch
from isis_monitor.config import DEFAULT_BEAMS, AppConfig, InstrumentConfig
from isis_monitor.daemon_state import DaemonState
from isis_monitor.beam import (
    BeamMonitor,
    _classify_ws_error,
    _PendingChange,
)
from isis_monitor.instrument import STALL_CHECK_WINDOW, _fit_rate, auto_target
from isis_monitor.tests.test_instlist import encode
from isis_monitor.tests.helpers import FakePVWS, fake_channel, running, wait_until


PEARL_UAMPS = "IN:PEARL:DAE:TOTALUAMPS"
PEARL_TITLE = "IN:PEARL:DAE:WDTITLE"
TS1_PV, TS2_PV, MUONS_PV = (b.pv for b in DEFAULT_BEAMS)
TS1, TS2 = DEFAULT_BEAMS[0].boundaries, DEFAULT_BEAMS[1].boundaries


def tracker(m):
    """The monitor's only instrument tracker."""
    (only,) = m.instruments.values()
    return only


def _seed_collected_baseline(m, now, value):
    """Insert a counts-collected sample older than STALL_CHECK_WINDOW so
    _check_collection_progress has something to compare the current reading to."""
    tracker(m).state.collected_samples.append((now - STALL_CHECK_WINDOW - timedelta(seconds=30), value))

def saved(m, **changes):
    """The monitor's tracker's to_snapshot(), with `changes`, as a restart would read it."""
    return {**json.loads(json.dumps(tracker(m).to_snapshot())), **changes}


def restarted(old, new):
    """`new` (a fresh monitor) restored from `old`'s instrument state, through
    JSON like the daemon's snapshot."""
    new.restore_instruments(json.loads(json.dumps(old.instrument_views())))
    return new

DEBOUNCE_SECONDS = 0.05
SETTLE = DEBOUNCE_SECONDS * 3  # wait comfortably past the debounce window in tests


@pytest.fixture
def mock_config():
    return AppConfig(isis_websocket_url="wss://test", instruments=[InstrumentConfig("PEARL", 100.0, "TS1")])


@pytest.fixture
def mock_channels():
    return fake_channel("Beam"), fake_channel("Exp")


def title(name, pv=PEARL_TITLE):
    """A PVWS run-title update; the title arrives base64-encoded."""
    raw = name if isinstance(name, bytes) else name.encode()
    return {"pv": pv, "b64byt": base64.b64encode(raw).decode()}


def stall_monitor(mock_config, mock_channels, run_name="Run 1"):
    """A monitor connected to PVWS with TS1 on, run_name active, stall_minutes ~0.6s
    and a 1000 µA·h target, so the usual test counts are mid-run."""
    m = make_monitor(replace(mock_config, stall_minutes=0.01), mock_channels, counts_target=1000)
    m._current_ws = MagicMock()
    tracker(m).state.run_name = run_name
    m.beams["TS1"].power = "high"
    return m


def make_monitor(mock_config, mock_channels, counts_target=100, sink=None):
    beam_channel, exp_channel = mock_channels
    instruments = [replace(i, notify_counts=counts_target) for i in mock_config.instruments]
    return BeamMonitor(
        replace(mock_config, instruments=instruments, debounce_seconds=DEBOUNCE_SECONDS),
        beam_channel, exp_channel, sink=sink,
    )


# ---------------------------------------------------------------------------
# _parse_current
# ---------------------------------------------------------------------------

def test_parse_current(mock_config, mock_channels):
    m = make_monitor(mock_config, mock_channels)
    assert m._parse_current("123.4") == 123.4
    assert m._parse_current(0) == 0.0
    for junk in ("NaN", "nan", "inf", "bad_string", None):
        assert m._parse_current(junk) is None


@pytest.mark.parametrize("junk", ["NaN", None, "bad_string"])
async def test_unreadable_beam_current_is_ignored_not_off(mock_config, mock_channels, junk):
    """E.g. PVWS's NaN before a PV has a value: no "beam off" card or sample."""
    beam_channel, _ = mock_channels
    m = make_monitor(mock_config, mock_channels)
    await m._handle_update({"pv": TS1_PV, "value": junk})
    beam_channel.broadcast.assert_not_called()
    assert m.beams["TS1"].power == ""
    await m._handle_update({"pv": TS1_PV, "value": "60.0"})
    await m._handle_update({"pv": TS1_PV, "value": junk})
    assert (m.beams["TS1"].power, m.beams["TS1"].current) == ("medium", 60.0)


async def test_unreadable_beam_current_shows_as_unknown(mock_config, mock_channels):
    """Unknown isn't sampled into the history, so a dead PV doesn't count as uptime."""
    sink = MagicMock()
    m = make_monitor(mock_config, mock_channels, sink=sink)
    await m._handle_update({"pv": TS1_PV, "value": "60.0"})
    await m._handle_update({"pv": TS1_PV, "value": "NaN"})
    sink.update_beam_state.assert_called_with("TS1", 60.0, "unknown")


# ---------------------------------------------------------------------------
# _get_power_label
# ---------------------------------------------------------------------------

def test_get_power_label(mock_config, mock_channels):
    m = make_monitor(mock_config, mock_channels)
    assert m._get_power_label(-5, TS1) == "off"
    assert m._get_power_label(0, TS1) == "off"
    assert m._get_power_label(20, TS1) == "low"
    assert m._get_power_label(75, TS1) == "medium"
    assert m._get_power_label(150, TS1) == "high"
    assert m._get_power_label(0, TS2) == "off"
    assert m._get_power_label(5, TS2) == "low"
    assert m._get_power_label(15, TS2) == "medium"
    assert m._get_power_label(30, TS2) == "high"


# ---------------------------------------------------------------------------
# _handle_update — beam-current arm
# ---------------------------------------------------------------------------

async def test_handle_update_beam_startup_sends_immediately(mock_config, mock_channels):
    """The first reading for a target is a startup card, sent with no debounce."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    await m._handle_update({"pv": TS1_PV, "value": "10.0"})

    assert m.beams["TS1"].current == 10.0
    assert m.beams["TS1"].power == "low"
    beam_channel.broadcast.assert_called_once()
    notification = beam_channel.broadcast.call_args[0][0]
    assert "online" in notification.title and notification.title.endswith("TS1 is low")
    assert notification.beam == "TS1"


async def test_handle_update_beam_no_change_no_broadcast(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    await m._handle_update({"pv": TS1_PV, "value": "10.0"})
    beam_channel.broadcast.reset_mock()

    await m._handle_update({"pv": TS1_PV, "value": "45.0"})
    await asyncio.sleep(SETTLE)

    assert m.beams["TS1"].current == 45.0
    assert m.beams["TS1"].power == "low"
    beam_channel.broadcast.assert_not_called()


async def test_handle_update_beam_change_is_debounced(mock_config, mock_channels):
    """A confirmed state change is not sent immediately — only after the debounce window."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    await m._handle_update({"pv": TS1_PV, "value": "10.0"})  # startup
    beam_channel.broadcast.reset_mock()

    await m._handle_update({"pv": TS1_PV, "value": "60.0"})
    assert m.beams["TS1"].power == "medium"  # state updates immediately
    beam_channel.broadcast.assert_not_called()  # but no card yet — still pending

    await asyncio.sleep(SETTLE)

    beam_channel.broadcast.assert_called_once()
    notification = beam_channel.broadcast.call_args[0][0]
    assert "low → medium" in notification.title
    assert notification.beam == "TS1"


async def test_handle_update_beam_flapping_sends_nothing(mock_config, mock_channels):
    """A change that reverts before the debounce window elapses is dropped entirely."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    await m._handle_update({"pv": TS1_PV, "value": "10.0"})  # startup
    beam_channel.broadcast.reset_mock()

    await m._handle_update({"pv": TS1_PV, "value": "60.0"})  # -> medium
    await m._handle_update({"pv": TS1_PV, "value": "10.0"})  # -> low again
    await asyncio.sleep(SETTLE)

    beam_channel.broadcast.assert_not_called()


async def test_handle_update_beam_trip_notes_other_targets(mock_config, mock_channels):
    """When multiple targets go off in the same debounce window, each card names the others."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    # Startup — get all three targets to a non-zero state first.
    await m._handle_update({"pv": TS1_PV, "value": "60.0"})
    await m._handle_update({"pv": TS2_PV, "value": "20.0"})
    await m._handle_update({"pv": MUONS_PV, "value": "3.0"})
    beam_channel.broadcast.reset_mock()

    # All three trip off within the same window.
    await m._handle_update({"pv": TS1_PV, "value": "0.0"})
    await m._handle_update({"pv": TS2_PV, "value": "0.0"})
    await m._handle_update({"pv": MUONS_PV, "value": "0.0"})
    await asyncio.sleep(SETTLE)

    assert beam_channel.broadcast.call_count == 3
    for sent in beam_channel.broadcast.call_args_list:
        notification = sent.args[0]
        assert "also went off, likely a facility-wide trip" in notification.text


async def test_change_aggregator_cancel_all_stops_pending_flush(mock_config, mock_channels):
    """cancel_all() (called on shutdown) must stop pending timers from firing."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    await m._handle_update({"pv": TS1_PV, "value": "10.0"})  # startup
    beam_channel.broadcast.reset_mock()

    await m._handle_update({"pv": TS1_PV, "value": "60.0"})  # pending
    m.change_aggregator.cancel_all()
    await asyncio.sleep(SETTLE)

    beam_channel.broadcast.assert_not_called()
    assert m.change_aggregator._pending == {}


async def test_change_aggregator_logs_flush_failure_and_keeps_working(mock_config, mock_channels, caplog):
    """A failure while sending one change is logged, and later changes still flush."""
    beam_channel, _ = mock_channels
    m = make_monitor(mock_config, mock_channels)
    await m._handle_update({"pv": TS1_PV, "value": "10.0"})  # startup
    beam_channel.broadcast.reset_mock()

    with patch("isis_monitor.beam.beam_change", side_effect=RuntimeError("boom")):
        await m._handle_update({"pv": TS1_PV, "value": "60.0"})
        await asyncio.sleep(SETTLE)
    assert "TS1: failed to send the beam change" in caplog.text
    assert "boom" in caplog.text

    await m._handle_update({"pv": TS1_PV, "value": "150.0"})
    await wait_until(lambda: beam_channel.broadcast.call_count == 1)


# ---------------------------------------------------------------------------
# _handle_update — run-name (b64byt) arm
# ---------------------------------------------------------------------------

async def test_handle_update_run_name_first_set(mock_config, mock_channels):
    """The very first run seen isn't a completion: no card, nothing to count yet."""
    beam_channel, exp_channel = mock_channels
    sink = MagicMock()
    m = make_monitor(mock_config, mock_channels, sink=sink)

    run_name = "Run 12345"
    await m._handle_update(title(run_name))

    assert tracker(m).state.run_name == run_name
    exp_channel.broadcast.assert_not_called()  # No previous run → no notification
    sink.record_run_completed.assert_not_called()


async def test_handle_update_run_name_change(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    # Seed first run
    tracker(m).state.run_name = "Run 12345"
    tracker(m).state.run_started_at = datetime.now(timezone.utc) - timedelta(hours=2)
    tracker(m).state.current_counts = 1000.0

    new_run = "Run 12346"
    await m._handle_update(title(new_run))

    assert tracker(m).state.run_name == new_run
    exp_channel.broadcast.assert_called_once()
    notification = exp_channel.broadcast.call_args[0][0]
    assert "new run" in notification.title.lower()
    assert notification.text == new_run
    facts = dict(notification.facts)
    assert facts["Previous run"] == "Run 12345"
    assert facts["Duration"].startswith("2h")
    assert "1000.0" in facts["Final total collected"]
    assert tracker(m).state.current_counts == 0


async def test_handle_update_run_name_change_resets_end_notified(mock_config, mock_channels):
    """A new run must re-arm the 'about to finish' notification for itself,
    even if the previous run ended with end_notified already set."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels, counts_target=10)
    tracker(m).state.run_name = "Run 1"
    tracker(m).state.run_started_at = datetime.now(timezone.utc)
    tracker(m).state.end_notified = True

    await m._handle_update(title("Run 2"))
    assert tracker(m).state.end_notified is False

    exp_channel.broadcast.reset_mock()
    # counts_target=10 is small enough that the old "< target - 25" reset
    # path would never trip; the run-start reset must do it instead.
    await m._handle_update({"pv": PEARL_UAMPS, "value": 12.0})
    exp_channel.broadcast.assert_called_once()
    assert "about to finish" in exp_channel.broadcast.call_args[0][0].title


async def test_handle_update_run_name_nan_ignored(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    await m._handle_update({"pv": PEARL_TITLE, "b64byt": "nan"})
    assert tracker(m).state.run_name == ""
    exp_channel.broadcast.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_update — counts (IN:<NAME>:DAE:TOTALUAMPS) arm
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("final", [300.5, 10.0])  # 10: stopped early, far short of the target
async def test_counts_reset_starts_a_run_whose_title_is_unchanged(mock_config, mock_channels, final):
    """A hidden title stays "Hidden" across runs, so only the counts reset shows a new one."""
    _, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels, counts_target=400)
    t0 = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    await tracker(m).handle_run_name(title("Hidden")["b64byt"], t0)
    await tracker(m).handle_counts(final + 0.5, t0 + timedelta(hours=2))
    await tracker(m).handle_counts(final, t0 + timedelta(hours=2, minutes=1))  # tiny dips don't count
    exp_channel.broadcast.assert_not_called()

    await tracker(m).handle_counts(0.0, t0 + timedelta(hours=3))
    exp_channel.broadcast.assert_called_once()
    notification = exp_channel.broadcast.call_args[0][0]
    assert notification.title == "PEARL: New run started" and notification.text == "Hidden"
    facts = dict(notification.facts)
    assert facts["Previous run"] == "Hidden" and facts["Duration"].startswith("3h")
    assert f"{final:.1f}" in facts["Final total collected"]
    assert tracker(m).state.run_started_at == t0 + timedelta(hours=3)
    assert tracker(m).state.current_counts == 0.0


@pytest.mark.parametrize("delay, cards", [(timedelta(minutes=1), 1), (timedelta(minutes=5), 2)])
async def test_title_soon_after_a_counts_reset_names_that_run(mock_config, mock_channels, delay, cards):
    """A counts reset that lands before the new title mustn't give one run two cards."""
    _, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels, counts_target=1000)
    t0 = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    await tracker(m).handle_run_name(title("Run 1")["b64byt"], t0)
    await tracker(m).handle_counts(300.0, t0 + timedelta(hours=1))
    reset_at = t0 + timedelta(hours=2)
    await tracker(m).handle_counts(0.0, reset_at)
    await tracker(m).handle_run_name(title("Run 2")["b64byt"], reset_at + delay)

    assert exp_channel.broadcast.call_count == cards
    assert tracker(m).state.run_name == "Run 2"
    assert tracker(m).state.run_started_at == (reset_at if cards == 1 else reset_at + delay)


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("titles", [["Run 2"], ["Run 2 typo", "Run 2"]])
async def test_old_total_resent_before_begin_doesnt_start_another_run(mock_config, mock_channels, restart, titles):
    """Title first, then BEGIN resets the counts: a reconnect or restart in between
    brings the old total back, and neither it nor BEGIN's reset is a new run."""
    _, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels, counts_target=100)
    t0 = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    await tracker(m).handle_run_name(title("Run 1")["b64byt"], t0)
    await tracker(m).handle_counts(300.0, t0 + timedelta(hours=1))
    for name in titles:  # a title edited before BEGIN keeps Run 1's total as the stale one
        await tracker(m).handle_run_name(title(name)["b64byt"], t0 + timedelta(hours=2))
    exp_channel.broadcast.reset_mock()  # Run 1's finishing card and Run 2's new-run card(s)
    if restart:
        m = restarted(m, make_monitor(mock_config, mock_channels, counts_target=100))

    later = t0 + timedelta(hours=2, minutes=10)
    await tracker(m).handle_run_name(title("Run 2")["b64byt"], later)  # re-sent on (re)connect
    await tracker(m).handle_counts(300.0, later)  # Run 1's total, not yet reset
    await tracker(m).handle_counts(0.0, later + timedelta(minutes=5))  # BEGIN
    await tracker(m).handle_counts(2.0, later + timedelta(minutes=6))

    exp_channel.broadcast.assert_not_called()  # no second new-run card, nor a finishing one
    assert tracker(m).state.run_name == "Run 2" and tracker(m).state.current_counts == 2.0
    assert tracker(m).state.stale_counts == -1.0
    # A later reset (a new run that keeps the title) is still caught.
    await tracker(m).handle_counts(0.0, later + timedelta(hours=1))
    exp_channel.broadcast.assert_called_once()


async def test_handle_update_counts_triggers_notification_past_the_target(mock_config, mock_channels):
    """TOTALUAMPS publishes the total µA·h collected this run as a number."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels, counts_target=100)
    tracker(m).state.run_name = "Run 1"
    m.beams["TS1"].power = "high"  # PEARL's own beam target

    await m._handle_update({"pv": PEARL_UAMPS, "value": 90.0})
    assert tracker(m).state.current_counts == 90.0
    exp_channel.broadcast.assert_not_called()  # below the target

    await m._handle_update({"pv": PEARL_UAMPS, "value": 110.0})
    assert tracker(m).state.current_counts == 110.0
    assert tracker(m).state.end_notified is True
    exp_channel.broadcast.assert_called_once()
    notification = exp_channel.broadcast.call_args[0][0]
    assert "about to finish" in notification.title
    assert notification.text == "Run 1"
    facts = dict(notification.facts)
    assert "110.0 / 100" in facts["Collected"]
    assert facts["Instrument beam"] == "high"


async def _feed_counts(m, readings):
    """Feed (minutes after t0, µA·h) readings to the only tracker."""
    t0 = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    for minutes, value in readings:
        await tracker(m).handle_counts(value, t0 + timedelta(minutes=minutes))


async def test_finishing_card_is_sent_ahead_of_the_target_on_its_eta(mock_config, mock_channels):
    _, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels, counts_target=100)  # finish_warning_minutes = 15
    tracker(m).state.run_name = "Run 1"
    tracker(m).state.run_started_at = datetime(2026, 9, 27, 11, 0, tzinfo=timezone.utc)

    # 1 µA·h a minute: 20 minutes to go at 80, 15 at 85.
    await _feed_counts(m, [(0, 70.0), (5, 75.0), (10, 80.0)])
    exp_channel.broadcast.assert_not_called()
    await _feed_counts(m, [(15, 85.0)])
    exp_channel.broadcast.assert_called_once()
    facts = dict(exp_channel.broadcast.call_args[0][0].facts)
    assert facts["ETA"] == "15m"

    # Sent once, however the ETA wobbles or the target is then passed.
    await _feed_counts(m, [(16, 85.5), (17, 90.0), (30, 101.0)])
    exp_channel.broadcast.assert_called_once()
    # Counts reset (a new run that kept its title): a new-run card, and the
    # next finish gets a card too.
    await _feed_counts(m, [(31, 0.0)])
    assert "New run" in exp_channel.broadcast.call_args[0][0].title
    assert tracker(m).state.end_notified is False


@pytest.mark.parametrize("readings", [
    [(0, 90.0), (2, 99.0)],  # too little history to trust the rate
    [(0, 0.0), (5, 5.0)],  # rising, but ETA 95 minutes
    [(0, 90.0), (5, 90.0)],  # not rising at all
])
async def test_finishing_card_waits_for_the_target_without_a_near_eta(mock_config, mock_channels, readings):
    _, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels, counts_target=100)
    tracker(m).state.run_name = "Run 1"

    await _feed_counts(m, readings)
    exp_channel.broadcast.assert_not_called()
    await _feed_counts(m, [(6, 100.5)])  # the target itself always triggers it
    exp_channel.broadcast.assert_called_once()


async def test_eta_rate_fit_is_rate_limited(mock_config, mock_channels):
    m = make_monitor(mock_config, mock_channels, counts_target=1000)
    tracker(m).state.run_name = "Run 1"
    readings = [(i / 12, i * 0.1) for i in range(12 * 6)]  # every 5s for 6 minutes, far from the target

    with patch("isis_monitor.instrument._fit_rate", wraps=_fit_rate) as fit:
        await _feed_counts(m, readings)
    assert fit.call_count == 6  # every 30s once there are 3 minutes of history


async def test_finish_warning_minutes_zero_only_sends_at_the_target(mock_config, mock_channels):
    _, exp_channel = mock_channels
    m = make_monitor(replace(mock_config, finish_warning_minutes=0), mock_channels, counts_target=100)
    tracker(m).state.run_name = "Run 1"

    await _feed_counts(m, [(0, 90.0), (5, 99.0), (6, 100.0)])
    exp_channel.broadcast.assert_not_called()
    await _feed_counts(m, [(7, 100.1)])
    exp_channel.broadcast.assert_called_once()


async def test_handle_update_counts_malformed(mock_config, mock_channels):
    m = make_monitor(mock_config, mock_channels, counts_target=100)

    await m._handle_update({"pv": PEARL_UAMPS, "value": "bad_format"})
    await m._handle_update({"pv": PEARL_UAMPS, "value": float("inf")})
    await m._handle_update({"pv": PEARL_UAMPS, "value": "NaN"})
    await m._handle_update({"pv": PEARL_UAMPS, "text": "50/90"})  # old dashboard format: not routed
    assert tracker(m).state.current_counts == -1.0  # unchanged, no crash
    await m._handle_update({"pv": PEARL_UAMPS, "value": 42.0})
    assert tracker(m).to_snapshot()["counts"] == 42.0


async def test_handle_update_counts_zero_is_a_real_reading(mock_config, mock_channels):
    """TOTALUAMPS is 0 at the start of a run; that mustn't be taken as blank."""
    m = make_monitor(mock_config, mock_channels, counts_target=100)
    await m._handle_update({"pv": PEARL_UAMPS, "value": 12.5})
    await m._handle_update({"pv": PEARL_UAMPS, "value": 0})
    assert tracker(m).state.current_counts == 0.0


# ---------------------------------------------------------------------------
# _fit_rate
# ---------------------------------------------------------------------------

def test_fit_rate_returns_zero_for_degenerate_samples():
    t = datetime.now(timezone.utc)
    assert _fit_rate([]) == 0.0
    assert _fit_rate([(t, 10.0)]) == 0.0
    assert _fit_rate([(t, 1.0), (t, 5.0)]) == 0.0  # identical timestamps


def test_fit_rate_computes_slope_per_second():
    t0 = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    samples = [(t0, 0.0), (t0 + timedelta(seconds=10), 100.0)]
    assert _fit_rate(samples) == pytest.approx(10.0)


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


# ---------------------------------------------------------------------------
# _check_collection_progress — stall detection
# ---------------------------------------------------------------------------

async def test_check_collection_progress_no_check_without_enough_history(mock_config, mock_channels):
    """Fewer than STALL_CHECK_WINDOW worth of samples means there's nothing
    to compare against yet — must not warn."""
    _, exp_channel = mock_channels
    m = stall_monitor(mock_config, mock_channels)
    now = datetime.now(timezone.utc)

    tracker(m).state.collected_samples.append((now - timedelta(minutes=1), 100.0))
    tracker(m).state.current_counts = 100.0

    await m._check_collection_progress(now)

    exp_channel.broadcast.assert_not_called()


async def test_check_collection_progress_no_false_stall_when_counts_update_in_batches(mock_config, mock_channels):
    """Regression: a source that updates the collected count in less frequent
    batches than the 60s check interval must not look stalled just because a
    batch hasn't landed in the latest minute — movement is judged over
    STALL_CHECK_WINDOW, not the last tick."""
    _, exp_channel = mock_channels
    m = stall_monitor(mock_config, mock_channels)
    now = datetime.now(timezone.utc)

    # A counts batch landed 4 minutes ago (inside the 5-minute window), so
    # the total genuinely moved over the window even though it hasn't ticked
    # in the last minute.
    tracker(m).state.collected_samples.append((now - timedelta(minutes=6), 50.0))
    tracker(m).state.collected_samples.append((now - timedelta(minutes=4), 100.0))
    tracker(m).state.current_counts = 100.0

    await m._check_collection_progress(now)
    await m._check_collection_progress(now + timedelta(seconds=1))

    exp_channel.broadcast.assert_not_called()


async def test_check_collection_progress_detects_stall_when_instrument_beam_on(mock_config, mock_channels):
    _, exp_channel = mock_channels
    m = stall_monitor(mock_config, mock_channels)  # the instrument's beam is on
    now = datetime.now(timezone.utc)
    _seed_collected_baseline(m, now, 100.0)
    tracker(m).state.current_counts = 100.0  # unmoved over the window

    await m._check_collection_progress(now)  # starts the stall clock
    exp_channel.broadcast.assert_not_called()

    await m._check_collection_progress(now + timedelta(seconds=1))  # past stall_minutes
    exp_channel.broadcast.assert_called_once()
    notification = exp_channel.broadcast.call_args[0][0]
    assert notification.title == "PEARL: Data collection stalled"


@pytest.mark.parametrize("counts, end_notified, warned", [
    (1000.0, True, False),  # reached notify_counts: the run has usually ended
    (100.0, True, True),  # only forecast to finish: a hang now is still a stall
])
async def test_stall_checks_stop_once_the_run_reaches_its_target(mock_config, mock_channels, counts, end_notified, warned):
    _, exp_channel = mock_channels
    m = stall_monitor(mock_config, mock_channels)
    now = datetime.now(timezone.utc)
    _seed_collected_baseline(m, now, counts)
    tracker(m).state.current_counts = counts
    tracker(m).state.end_notified = end_notified

    await m._check_collection_progress(now)
    await m._check_collection_progress(now + timedelta(seconds=1))
    assert exp_channel.broadcast.called is warned


@pytest.mark.parametrize("run_state, warned", [
    ("RUNNING", True), ("VETOING", True), ("", True),  # "": not known yet, as before the PV
    ("SETUP", False),  # e.g. a run ended short of its target
    ("PAUSED", False), ("WAITING", False), ("ENDING", False),
])
async def test_stall_checks_only_while_the_dae_is_counting(mock_config, mock_channels, run_state, warned):
    _, exp_channel = mock_channels
    m = stall_monitor(mock_config, mock_channels)
    await m._handle_update({"pv": "IN:PEARL:DAE:RUNSTATE_STR", "text": run_state})
    now = datetime.now(timezone.utc)
    _seed_collected_baseline(m, now, 100.0)
    tracker(m).state.current_counts = 100.0

    await m._check_collection_progress(now)
    await m._check_collection_progress(now + timedelta(seconds=1))
    assert exp_channel.broadcast.called is warned


async def test_check_collection_progress_movement_resets_stall_clock(mock_config, mock_channels):
    _, exp_channel = mock_channels
    m = stall_monitor(mock_config, mock_channels)
    now = datetime.now(timezone.utc)
    _seed_collected_baseline(m, now, 100.0)
    tracker(m).state.current_counts = 100.0
    await m._check_collection_progress(now)
    assert tracker(m).state.collection_stalled_since is not None

    # Counts move again — stall clock resets.
    tracker(m).state.current_counts = 110.0
    await m._check_collection_progress(now + timedelta(seconds=0.5))
    assert tracker(m).state.collection_stalled_since is None

    exp_channel.broadcast.assert_not_called()


@pytest.mark.parametrize("paused", [False, "paused", "disconnected"])
async def test_collection_resumed_card_follows_a_stall_warning(mock_config, mock_channels, paused):
    _, exp_channel = mock_channels
    m = stall_monitor(mock_config, mock_channels)
    now = datetime.now(timezone.utc)
    _seed_collected_baseline(m, now, 100.0)
    tracker(m).state.current_counts = 100.0
    await m._check_collection_progress(now)
    await m._check_collection_progress(now + timedelta(seconds=1))
    assert exp_channel.broadcast.call_args[0][0].title == "PEARL: Data collection stalled"

    if paused == "paused":  # paused to fix it, then resumed
        tracker(m).handle_run_state("PAUSED")
        await m._check_collection_progress(now + timedelta(minutes=10))
        tracker(m).handle_run_state("RUNNING")
    elif paused == "disconnected":  # a PVWS drop doesn't lose the stall's start
        m._current_ws = None
        await m._check_collection_progress(now + timedelta(minutes=10))
        m._current_ws = MagicMock()
    tracker(m).state.current_counts = 101.0
    await m._check_collection_progress(now + timedelta(minutes=20))
    card = exp_channel.broadcast.call_args[0][0]
    assert card.title == "PEARL: Data collection resumed" and "20m" in card.text
    await m._check_collection_progress(now + timedelta(minutes=21))
    assert exp_channel.broadcast.call_count == 2  # once


async def test_new_run_after_a_stall_warning_sends_no_resumed_card(mock_config, mock_channels):
    _, exp_channel = mock_channels
    m = stall_monitor(mock_config, mock_channels)
    tracker(m).state.run_started_at = now = datetime.now(timezone.utc)
    _seed_collected_baseline(m, now, 100.0)
    tracker(m).state.current_counts = 100.0
    await m._check_collection_progress(now)
    await m._check_collection_progress(now + timedelta(seconds=1))
    await m._handle_update(title("Run 2"))
    await m._handle_update({"pv": PEARL_UAMPS, "value": 5.0})
    _seed_collected_baseline(m, now + timedelta(minutes=10), 0.0)
    await m._check_collection_progress(now + timedelta(minutes=10))
    assert [c[0][0].title for c in exp_channel.broadcast.call_args_list] == [
        "PEARL: Data collection stalled", "PEARL: New run started",
    ]


async def test_check_collection_progress_no_active_run_never_warns(mock_config, mock_channels):
    """Between runs, the collected count is naturally static — that must not
    look like a stall just because no run is currently in progress."""
    _, exp_channel = mock_channels
    m = stall_monitor(mock_config, mock_channels, run_name="")  # no run active
    now = datetime.now(timezone.utc)
    _seed_collected_baseline(m, now, 100.0)
    tracker(m).state.current_counts = 100.0

    await m._check_collection_progress(now)
    await m._check_collection_progress(now + timedelta(seconds=1))

    exp_channel.broadcast.assert_not_called()
    assert tracker(m).state.collection_stalled_since is None


# ---------------------------------------------------------------------------
# _run_loop() against a real local WebSocket server
# ---------------------------------------------------------------------------

def ws_monitor(mock_config, mock_channels, url, sink=None, reconnect_interval=60.0, **kw):
    config = replace(mock_config, isis_websocket_url=url, beam_reconnect_interval=reconnect_interval)
    return make_monitor(config, mock_channels, sink=sink, **kw)


async def test_run_loop_subscribes_and_dispatches_updates(mock_config, mock_channels):
    sink = MagicMock()
    update = {"type": "update", "pv": TS1_PV, "value": 150.0}
    async with FakePVWS([update]) as server:
        m = ws_monitor(mock_config, mock_channels, server.url, sink=sink)
        async with running(m):
            await wait_until(lambda: sink.update_beam_state.called)

    assert set(server.subscriptions[0]["pvs"]) == {
        "CS:INSTLIST", TS1_PV, TS2_PV,
        MUONS_PV, PEARL_UAMPS, PEARL_TITLE,
        "IN:PEARL:DAE:RUNSTATE_STR", "IN:PEARL:DAE:RUNNUMBER", "IN:PEARL:DAE:_RBNUMBER", "IN:PEARL:DAE:STARTTIME",
    }
    sink.update_beam_state.assert_called_with("TS1", 150.0, "high")
    sink.update_health.assert_any_call("beam", "connected")


async def test_run_loop_survives_malformed_messages(mock_config, mock_channels, caplog):
    """Bad frames and handler errors are logged and skipped, not a reconnect."""
    sink = MagicMock()
    good = {"type": "update", "pv": TS2_PV, "value": 40.0}
    boom = {"type": "update", "pv": PEARL_UAMPS, "value": 2.0}
    async with FakePVWS(["not json", "[1, 2]", {"type": "other"}, boom, good]) as server:
        m = ws_monitor(mock_config, mock_channels, server.url, sink=sink)
        original = m._handle_update

        async def flaky_handle_update(data):
            if data is not None and data.get("pv") == PEARL_UAMPS:
                raise RuntimeError("handler bug")
            await original(data)

        m._handle_update = flaky_handle_update
        async with running(m):
            await wait_until(lambda: sink.update_beam_state.called)

    assert server.connections == 1
    sink.update_beam_state.assert_called_once_with("TS2", 40.0, "high")
    assert "handler bug" in caplog.text


async def test_run_loop_reconnects_after_server_closes(mock_config, mock_channels):
    sink = MagicMock()
    async with FakePVWS(close_after_send=True) as server:
        m = ws_monitor(mock_config, mock_channels, server.url, sink=sink, reconnect_interval=0.02)
        async with running(m):
            await wait_until(lambda: server.connections >= 3)
    sink.update_health.assert_any_call("beam", "disconnected")


async def test_reconnect_repopulates_beam_states_marked_unknown(mock_config, mock_channels):
    """DaemonState marks beams unknown when the feed drops; PVWS re-sending an
    unchanged value on reconnect must restore it, without a beam notification."""
    beam_channel, _ = mock_channels
    state = DaemonState()
    update = {"type": "update", "pv": TS1_PV, "value": 150.0}
    powers = []
    update_beam_state = state.update_beam_state

    def record(beam, current, power):
        if beam == "TS1":
            powers.append(power)
        update_beam_state(beam, current, power)

    state.update_beam_state = record

    async with FakePVWS([update], close_after_send=True) as server:
        m = ws_monitor(mock_config, mock_channels, server.url, sink=state, reconnect_interval=0.02)
        async with running(m):
            await wait_until(lambda: powers[:3] == ["high", "unknown", "high"])

    beam_channel.broadcast.assert_called_once()  # just the startup card


async def test_request_reconnect_while_connected_reconnects_immediately(mock_config, mock_channels):
    sink = MagicMock()
    async with FakePVWS() as server:
        m = ws_monitor(mock_config, mock_channels, server.url, sink=sink, reconnect_interval=60.0)
        async with running(m):
            await asyncio.wait_for(server.connected.wait(), 2)
            await wait_until(lambda: m._current_ws is not None)
            assert m.request_reconnect() is True
            assert m.request_reconnect() is False  # already pending
            await wait_until(lambda: server.connections == 2)
            await wait_until(lambda: not m._force_reconnect.is_set())
            assert m.request_reconnect() is True  # flag consumed; can request again
    sink.update_health.assert_any_call("beam", "reconnecting")


async def test_request_reconnect_while_disconnected_skips_backoff(mock_config, mock_channels):
    """A reconnect requested during the backoff wait retries at once, and does
    not linger to tear down the next successful connection."""
    async with FakePVWS() as server:
        url, port = server.url, server.port
    # Server is now closed: the first attempt fails and backs off for 60s.
    sink = MagicMock()
    m = ws_monitor(mock_config, mock_channels, url, sink=sink, reconnect_interval=60.0)
    async with running(m):
        await wait_until(lambda: call("beam", "disconnected") in sink.update_health.call_args_list)
        async with FakePVWS(port=port) as server:
            m.request_reconnect()
            await asyncio.wait_for(server.connected.wait(), 2)
            await asyncio.sleep(0.1)
            assert server.connections == 1  # the stale request didn't force a second one


async def test_run_cancels_promptly_and_cancels_pending_debounce(mock_config, mock_channels):
    async with FakePVWS() as server:
        m = ws_monitor(mock_config, mock_channels, server.url)
        task = asyncio.create_task(m.run())
        await asyncio.wait_for(server.connected.wait(), 2)
        now = datetime.now(timezone.utc)
        m.change_aggregator.queue_change(_PendingChange(DEFAULT_BEAMS[0], "high", 150, now, "off", 0, 140, now))
        pending_task = m.change_aggregator._pending["TS1"].task
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    await asyncio.sleep(0)
    assert pending_task.cancelled()
    assert m.change_aggregator._pending == {}


async def test_run_without_url_returns_immediately(mock_config, mock_channels, caplog):
    m = ws_monitor(mock_config, mock_channels, "")
    await asyncio.wait_for(m.run(), 1)
    assert "will not run" in caplog.text


async def test_collection_check_loop_calls_progress_check(mock_config, mock_channels):
    m = make_monitor(mock_config, mock_channels)
    m._check_collection_progress = AsyncMock()
    with patch("isis_monitor.beam.COLLECTION_CHECK_INTERVAL", 0.01):
        task = asyncio.create_task(m._collection_check_loop())
        await wait_until(lambda: m._check_collection_progress.await_count >= 2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_aggregator_flush_without_pending_is_noop(mock_config, mock_channels):
    m = make_monitor(mock_config, mock_channels)
    await m.change_aggregator._flush("TS1")
    mock_channels[0].broadcast.assert_not_called()


async def test_handle_update_run_name_bad_base64_is_ignored(mock_config, mock_channels, caplog):
    m = make_monitor(mock_config, mock_channels)
    await m._handle_update({"pv": PEARL_TITLE, "b64byt": "!!!not base64"})
    assert tracker(m).state.run_name == ""
    assert "Failed to decode run name" in caplog.text


def test_prune_collected_samples_drops_samples_outside_window(mock_config, mock_channels):
    from isis_monitor.instrument import COUNTS_SAMPLE_WINDOW
    m = make_monitor(mock_config, mock_channels)
    now = datetime.now(timezone.utc)
    tracker(m).state.collected_samples.extend([
        (now - COUNTS_SAMPLE_WINDOW - timedelta(seconds=1), 1.0),
        (now, 2.0),
    ])
    tracker(m)._prune_collected_samples(now)
    assert list(tracker(m).state.collected_samples) == [(now, 2.0)]


async def test_run_loop_bad_url_marks_health_and_retries(mock_config, mock_channels, caplog):
    sink = MagicMock()
    m = ws_monitor(mock_config, mock_channels, "not-a-websocket-url", sink=sink, reconnect_interval=0.01)
    async with running(m):
        await wait_until(lambda: "WebSocket invalid URL" in caplog.text)
        await wait_until(lambda: call("beam", "error") in sink.update_health.call_args_list)
    assert call("beam", "disconnected") not in sink.update_health.call_args_list


def rejected_with(status: int) -> InvalidStatus:
    """What connect() raises when PVWS answers the handshake with `status`."""
    return InvalidStatus(Response(status, "", Headers()))


def test_classify_ws_error():
    import socket
    import ssl
    from websockets.exceptions import ConnectionClosedError, InvalidMessage, InvalidURI, SecurityError

    cases = {
        rejected_with(403): "rejected",
        rejected_with(404): "rejected",
        rejected_with(503): "transient",
        InvalidURI("nope", "not a ws URL"): "config",
        ssl.SSLCertVerificationError("certificate verify failed"): "tls",
        socket.gaierror(socket.EAI_NONAME, "Name or service not known"): "dns",
        socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"): "transient",
        InvalidMessage("did not receive a valid HTTP response"): "transient",
        SecurityError("too many redirects"): "rejected",
        ConnectionClosedError(None, None): "transient",
        ConnectionRefusedError(111, "refused"): "transient",
        TimeoutError(): "transient",
        RuntimeError("bug"): "unexpected",
    }
    for exc, kind in cases.items():
        assert _classify_ws_error(exc)[0] == kind, exc
    assert "HTTP 403" in _classify_ws_error(rejected_with(403))[1]


async def test_run_loop_backs_off_only_for_persistent_problems(mock_config, mock_channels):
    """Transient failures retry after reconnect_interval; persistent ones
    double the wait each time up to BEAM_MAX_BACKOFF, resetting on connect."""
    m = ws_monitor(mock_config, mock_channels, "ws://pvws.invalid", reconnect_interval=5.0)
    rejected = rejected_with(403)
    failures = [rejected] * 8 + [None, rejected, OSError("reset"), rejected]
    waits = []

    class Connected:  # stands in for a connection the server closes at once
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, _msg):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    def connect(_url):
        exc = failures.pop(0)
        if exc is None:
            return Connected()
        raise exc

    async def fake_wait_for(aw, timeout):
        aw.close()  # the un-awaited Event.wait() coroutine
        waits.append(timeout)
        if not failures:
            raise asyncio.CancelledError
        raise TimeoutError

    with patch("isis_monitor.beam.websockets.connect", side_effect=connect), \
            patch("isis_monitor.beam.asyncio.wait_for", side_effect=fake_wait_for):
        with pytest.raises(asyncio.CancelledError):
            await m._run_loop()

    assert waits == [5, 10, 20, 40, 80, 160, 300, 300, 5, 5, 5, 10]


async def test_run_loop_logs_repeated_failures_once(mock_config, mock_channels, caplog):
    import logging
    async with FakePVWS() as server:
        url, port = server.url, server.port
    # Server is now closed, so every attempt is refused.
    m = ws_monitor(mock_config, mock_channels, url, reconnect_interval=0.01)
    def logged(level):
        return [r for r in caplog.records if "reconnecting in" in r.getMessage() and r.levelno == level]

    with caplog.at_level(logging.DEBUG, logger="isis_monitor.beam"):
        async with running(m):
            await wait_until(lambda: len(logged(logging.DEBUG)) >= 2)  # repeats
            assert len(logged(logging.WARNING)) == 1

            async with FakePVWS(port=port):
                await wait_until(lambda: "WebSocket connected after" in caplog.text)
    assert "failed attempt(s) over" in caplog.text


async def test_close_ws_quietly_logs_instead_of_raising(caplog):
    ws = MagicMock()
    ws.close = AsyncMock(side_effect=RuntimeError("socket gone"))
    await BeamMonitor._close_ws_quietly(ws)
    assert "Error closing WebSocket during reconnect: socket gone" in caplog.text


# ---------------------------------------------------------------------------
# Multiple instruments
# ---------------------------------------------------------------------------

def two_instrument_monitor(mock_config, mock_channels):
    config = replace(mock_config, instruments=[
        InstrumentConfig("PEARL", 100.0, "TS1"),
        InstrumentConfig("WISH", 50.0, "TS2"),
    ])
    return BeamMonitor(replace(config, debounce_seconds=DEBOUNCE_SECONDS), *mock_channels)


async def test_updates_are_routed_to_their_own_instrument(mock_config, mock_channels):
    _, exp_channel = mock_channels
    m = two_instrument_monitor(mock_config, mock_channels)
    pearl, wish = m.instruments["PEARL"], m.instruments["WISH"]

    await m._handle_update(title("Wish run", "IN:WISH:DAE:WDTITLE"))
    await m._handle_update({"pv": "IN:WISH:DAE:TOTALUAMPS", "value": 60.0})

    assert (wish.state.run_name, wish.state.current_counts) == ("Wish run", 60.0)
    assert (pearl.state.run_name, pearl.state.current_counts) == ("", -1.0)
    # 60 is past WISH's own notify count (50) though not PEARL's (100).
    exp_channel.broadcast.assert_called_once()
    assert exp_channel.broadcast.call_args[0][0].title == "WISH: Run about to finish"


async def test_run_loop_subscribes_to_every_instruments_pvs(mock_config, mock_channels):
    async with FakePVWS([]) as server:
        config = replace(mock_config, isis_websocket_url=server.url)
        m = two_instrument_monitor(config, mock_channels)
        async with running(m):
            await wait_until(lambda: server.subscriptions)

    pvs = server.subscriptions[0]["pvs"]
    assert pvs[:4] == ["CS:INSTLIST", *(b.pv for b in DEFAULT_BEAMS)]
    # Each instrument's RB and run numbers first, then its title before its
    # counts, so a run changed while disconnected is started by its title.
    for name in ("PEARL", "WISH"):
        assert [pv for pv in pvs if pv.startswith(f"IN:{name}:")] == [
            f"IN:{name}:DAE:{pv}" for pv in ("_RBNUMBER", "RUNNUMBER", "WDTITLE", "TOTALUAMPS", "RUNSTATE_STR",
                                              "STARTTIME")
        ]


@pytest.mark.parametrize("pv", ["CS:INSTLIST", TS1_PV, PEARL_UAMPS, PEARL_TITLE, "IN:PEARL:DAE:RUNSTATE_STR",
                                "IN:PEARL:DAE:RUNNUMBER", "IN:PEARL:DAE:_RBNUMBER"])
async def test_severity_only_updates_are_ignored(mock_config, mock_channels, pv):
    """PVWS sends updates with only a new severity; they mustn't read as a
    missing value (beam "unknown", a counts warning or run state "NONE")."""
    sink = MagicMock()
    m = make_monitor(mock_config, mock_channels, sink=sink)
    sink.reset_mock()
    before = (vars(tracker(m).state).copy(), vars(m.beams["TS1"]).copy())
    await m._handle_update({"type": "update", "pv": pv, "severity": "MAJOR"})
    assert (vars(tracker(m).state), vars(m.beams["TS1"])) == before
    assert not sink.method_calls


async def test_stall_check_uses_each_instruments_beam_target(mock_config, mock_channels):
    """WISH is on TS2, which is off, so only PEARL (on TS1, high) warns."""
    _, exp_channel = mock_channels
    m = two_instrument_monitor(replace(mock_config, stall_minutes=0.01), mock_channels)
    m._current_ws = MagicMock()  # connected to PVWS
    m.beams["TS1"].power = "high"
    m.beams["TS2"].power = "off"
    now = datetime.now(timezone.utc)
    for t in m.instruments.values():
        t.state.run_name = "Run 1"
        t.state.collected_samples.append((now - STALL_CHECK_WINDOW - timedelta(seconds=30), 40.0))
        t.state.current_counts = 40.0  # below either instrument's target

    await m._check_collection_progress(now)
    await m._check_collection_progress(now + timedelta(seconds=1))

    assert m.instruments["PEARL"].state.stall_warned is True
    assert m.instruments["WISH"].state.stall_warned is False
    exp_channel.broadcast.assert_called_once()


async def test_one_instruments_failed_stall_check_does_not_skip_others(mock_config, mock_channels, caplog):
    m = two_instrument_monitor(mock_config, mock_channels)
    m._current_ws = MagicMock()  # connected to PVWS
    m.instruments["PEARL"].check_collection_progress = AsyncMock(side_effect=RuntimeError("boom"))
    m.instruments["WISH"].check_collection_progress = AsyncMock()

    await m._check_collection_progress(datetime.now(timezone.utc))

    m.instruments["WISH"].check_collection_progress.assert_awaited_once()
    assert "Collection check failed for PEARL" in caplog.text


async def test_repeated_run_title_after_reconnect_keeps_the_run_start_time(mock_config, mock_channels):
    """PVWS re-sends the current title on every reconnect; the next new-run
    card must still report the whole previous run's duration."""
    _, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)
    run_1 = title("Run 1")
    await m._handle_update(run_1)
    started = tracker(m).state.run_started_at
    tracker(m).state.run_started_at = started - timedelta(hours=6)  # the run began 6h ago

    await m._handle_update(run_1)  # reconnect re-sends it
    assert tracker(m).state.run_started_at == started - timedelta(hours=6)

    await m._handle_update(title("Run 2"))
    card = exp_channel.broadcast.call_args[0][0]
    assert dict(card.facts)["Duration"].startswith("6h")


async def test_no_stall_warning_while_pvws_is_disconnected(mock_config, mock_channels):
    """Counts are frozen and beam states stale during an outage."""
    _, exp_channel = mock_channels
    m = stall_monitor(mock_config, mock_channels)  # TS1 "high" as last known before the outage
    m._current_ws = None  # PVWS disconnected
    t = tracker(m)
    now = datetime.now(timezone.utc)
    _seed_collected_baseline(m, now, 100.0)
    t.state.current_counts = 100.0
    t.state.collection_stalled_since = now - timedelta(minutes=30)

    await m._check_collection_progress(now)
    await m._check_collection_progress(now + timedelta(minutes=5))
    exp_channel.broadcast.assert_not_called()
    assert t.state.collection_stalled_since is None  # restarts once reconnected



async def test_dropped_flicker_does_not_reset_time_in_state(mock_config, mock_channels):
    """High for 10h, a 5s flicker to medium (dropped by the debounce), then
    off: the off card must say it was high for ~10h, not since the flicker."""
    beam_channel, _ = mock_channels
    m = make_monitor(mock_config, mock_channels)
    pv = TS1_PV
    await m._handle_update({"pv": pv, "value": 150.0})  # startup: high
    ten_hours_ago = datetime.now(timezone.utc) - timedelta(hours=10)
    m.beams["TS1"].since = ten_hours_ago

    await m._handle_update({"pv": pv, "value": 100.0})  # flicker to medium...
    await m._handle_update({"pv": pv, "value": 150.0})  # ...and straight back
    assert m.beams["TS1"].since == ten_hours_ago
    await asyncio.sleep(SETTLE)  # flicker dropped

    beam_channel.broadcast.reset_mock()
    await m._handle_update({"pv": pv, "value": 0.0})
    await asyncio.sleep(SETTLE)
    card = beam_channel.broadcast.call_args[0][0]
    assert dict(card.facts)["Was high for"].startswith("10h")



async def test_restored_run_is_not_reset_or_renotified_after_a_restart(mock_config, mock_channels):
    """After a restart (e.g. a config save) PVWS re-sends the same title and
    TOTALUAMPS; neither a new-run card nor a second finishing card is sent."""
    _, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels, counts_target=100)
    started = datetime.now(timezone.utc) - timedelta(hours=3)
    pearl = saved(m, run_name="Run 1", run_started_at=started.isoformat(), counts=120.0, end_notified=True)
    m.restore_instruments({"PEARL": pearl, "MERLIN": {**pearl, "run_name": "x"}})

    await m._handle_update(title("Run 1"))
    await m._handle_update({"pv": PEARL_UAMPS, "value": 121.0})
    exp_channel.broadcast.assert_not_called()
    assert tracker(m).state.run_started_at == started

    # The run then changes: the card covers the whole run, restart included.
    await m._handle_update(title("Run 2"))
    card = exp_channel.broadcast.call_args[0][0]
    assert dict(card.facts)["Duration"].startswith("3h")
    snap = tracker(m).to_snapshot()
    assert (snap["run_name"], snap["end_notified"], snap["stale_counts"]) == ("Run 2", False, 121.0)


async def test_restore_ignores_entries_without_a_run(mock_config, mock_channels):
    m = make_monitor(mock_config, mock_channels)
    m.restore_instruments({"PEARL": saved(m, run_name="Run 1", run_started_at=None, rb_number="A")})
    assert tracker(m).state.run_name == "" and tracker(m).state.run_started_at is None
    assert tracker(m).state.rb_number == "A"  # the rest is restored


async def test_finishing_card_state_is_in_the_snapshot(mock_config, mock_channels):
    m = make_monitor(mock_config, mock_channels, counts_target=100)
    tracker(m).state.run_name = "Run 1"
    tracker(m).state.run_started_at = started = datetime.now(timezone.utc)

    def progress():
        snap = tracker(m).to_snapshot()
        return snap["run_started_at"], snap["end_notified"], snap["stale_counts"]

    await m._handle_update({"pv": PEARL_UAMPS, "value": 110.0})
    assert progress() == (started.isoformat(), True, -1.0)
    await m._handle_update({"pv": PEARL_UAMPS, "value": 10.0})  # reset by a new run
    assert tracker(m).state.end_notified is False
    assert progress() == (tracker(m).state.run_started_at.isoformat(), False, -1.0)



@pytest.mark.parametrize("saved_target, renotified", [(100.0, False), (120.0, True), (80.0, True)])
async def test_finishing_card_after_restart_depends_on_the_threshold_it_was_sent_for(
    mock_config, mock_channels, saved_target, renotified,
):
    """The card was sent (maybe early, at 101) for saved_target; the daemon
    restarted with a target of 100. A plain restart mustn't resend it, but a
    card for a changed target is still due."""
    _, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels, counts_target=100)
    m.restore_instruments({"PEARL": saved(
        m, run_name="Run 1", run_started_at=datetime.now(timezone.utc).isoformat(),
        counts=101.0, end_notified=True, notify_counts=saved_target,
    )})
    assert tracker(m).state.end_notified is not renotified
    await m._handle_update({"pv": PEARL_UAMPS, "value": 102.0})
    assert exp_channel.broadcast.called is renotified


def auto_monitor(mock_config, mock_channels, sink=None, fallback=100):
    """A monitor whose PEARL has notify_counts = auto, <fallback>, mid-run."""
    instruments = [replace(i, auto_counts=True) for i in mock_config.instruments]
    m = make_monitor(replace(mock_config, instruments=instruments), mock_channels, counts_target=fallback, sink=sink)
    tracker(m).state.run_name = "Run 0"
    tracker(m).state.run_started_at = datetime.now(timezone.utc)
    return m


def _age_run(m):
    """Make the run in progress older than NEW_EXPERIMENT_RUN_WINDOW, so an RB
    number change finds it well under way."""
    tracker(m).state.run_started_at -= timedelta(hours=1)


async def _finish_runs(m, *totals):
    """Collect each total in turn, each new run started by the DAE's counts reset."""
    for total in totals:
        await m._handle_update({"pv": PEARL_UAMPS, "value": total})
        await m._handle_update({"pv": PEARL_UAMPS, "value": 0.0})


async def test_auto_target_comes_from_recent_runs_once_there_are_enough(mock_config, mock_channels):
    _, exp_channel = mock_channels
    m = auto_monitor(replace(mock_config, finish_warning_minutes=0), mock_channels)
    await _finish_runs(m, 60.0, 5.0, 62.0)  # the short run (a mistaken setup) doesn't count
    assert tracker(m).state.run_target == 100.0  # the fallback, so far

    await _finish_runs(m, 58.0)
    assert tracker(m).state.run_target == 60.0
    assert dict(exp_channel.broadcast.call_args[0][0].facts)["Target"] == "60 µA·h (auto, from 3 runs)"
    exp_channel.broadcast.reset_mock()  # the new-run cards
    snap = tracker(m).to_snapshot()
    assert (snap["target"], snap["auto_runs"], snap["run_totals"]) == (60.0, 3, [60.0, 5.0, 62.0, 58.0])
    await m._handle_update({"pv": PEARL_UAMPS, "value": 59.0})
    exp_channel.broadcast.assert_not_called()
    await m._handle_update({"pv": PEARL_UAMPS, "value": 60.5})
    card = exp_channel.broadcast.call_args[0][0]
    assert dict(card.facts)["Collected"] == "60.5 / 60 µA·h (auto)"


async def test_finishing_and_stall_cards_name_the_run_number(mock_config, mock_channels):
    _, exp_channel = mock_channels
    m = stall_monitor(replace(mock_config, finish_warning_minutes=0), mock_channels)
    await m._handle_update({"pv": "IN:PEARL:DAE:RUNNUMBER", "text": "00127920"})
    now = datetime.now(timezone.utc)
    _seed_collected_baseline(m, now, 100.0)
    tracker(m).state.current_counts = 100.0
    await m._check_collection_progress(now)
    await m._check_collection_progress(now + timedelta(seconds=1))
    await m._handle_update({"pv": PEARL_UAMPS, "value": 1001.0})
    stalled, finishing = (c[0][0] for c in exp_channel.broadcast.call_args_list)
    assert dict(stalled.facts)["Run number"] == dict(finishing.facts)["Run number"] == "127920"


async def test_fixed_target_ignores_run_totals(mock_config, mock_channels):
    m = make_monitor(mock_config, mock_channels, counts_target=100)
    m.restore_instruments({"PEARL": saved(m, run_totals=[50.0, 52.0, 48.0])})
    tracker(m).state.run_name = "Run 0"
    tracker(m).state.run_started_at = datetime.now(timezone.utc)
    await _finish_runs(m, 60.0, 61.0, 62.0)
    assert tracker(m).state.run_target == 100.0 and not tracker(m).state.run_totals


async def test_restored_run_totals_set_the_next_auto_target(mock_config, mock_channels):
    m = auto_monitor(mock_config, mock_channels)
    m.restore_instruments({"PEARL": saved(m, run_totals=[50.0, 52.0, 48.0], rb_number="1", run_rb="1")})
    assert tracker(m).state.run_target == 100.0  # the run in progress keeps the target it started with
    await _finish_runs(m, 49.0)
    assert (tracker(m).state.run_target, tracker(m).state.auto_runs) == (49.5, 4)


PEARL_RB = "IN:PEARL:DAE:_RBNUMBER"


def rb(number):
    return {"pv": PEARL_RB, "text": number}


async def test_rb_change_mid_run_keeps_that_runs_target_through_a_restart(mock_config, mock_channels):
    _, exp_channel = mock_channels
    m = auto_monitor(mock_config, mock_channels, fallback=30)
    await m._handle_update(rb("A"))
    await _finish_runs(m, 100.0, 100.0, 100.0)
    await m._handle_update({"pv": PEARL_UAMPS, "value": 40.0})
    _age_run(m)
    await m._handle_update(rb("B"))  # the next experiment's, while A's last run counts
    exp_channel.broadcast.reset_mock()
    await m._handle_update({"pv": PEARL_UAMPS, "value": 40.5})
    assert tracker(m).state.run_target == 100.0
    exp_channel.broadcast.assert_not_called()

    m2 = restarted(m, auto_monitor(mock_config, mock_channels, fallback=30))
    assert (tracker(m2).state.run_target, tracker(m2).state.auto_runs) == (100.0, 3)
    await _finish_runs(m2, 101.0)  # A's last run ends: B's first run starts on the fallback
    assert tracker(m2).state.run_target == 30.0 and not tracker(m2).state.run_totals
    await m2._handle_update({"pv": PEARL_UAMPS, "value": 30.5})
    assert dict(exp_channel.broadcast.call_args[0][0].facts)["Collected"] == "30.5 / 30 µA·h"  # not "(auto)"


async def test_first_rb_number_mid_run_keeps_that_runs_target_through_a_restart(mock_config, mock_channels):
    m = auto_monitor(mock_config, mock_channels, fallback=30)
    await _finish_runs(m, 100.0, 100.0, 100.0)
    await m._handle_update({"pv": PEARL_UAMPS, "value": 40.0})
    await m._handle_update(rb("A"))
    m2 = restarted(m, auto_monitor(mock_config, mock_channels, fallback=30))
    assert (tracker(m2).state.run_target, tracker(m2).state.auto_runs) == (100.0, 3)


def _experiment_cards(exp_channel):
    return [c[0][0] for c in exp_channel.broadcast.call_args_list if c[0][0].title.endswith("Experiment finished")]


async def test_rb_number_change_sends_the_old_experiments_summary(mock_config, mock_channels):
    _, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)  # a fixed target tracks experiments too
    tracker(m).state.run_name = "Run 0"
    tracker(m).state.run_started_at = now = datetime.now(timezone.utc)
    await tracker(m).handle_rb_number("A", now - timedelta(hours=30))  # first seen: started earlier
    await _finish_runs(m, 40.0, 60.0)
    await m._handle_update({"pv": PEARL_UAMPS, "value": 50.0})  # the last run ends, not yet completed
    tracker(m).state.run_started_at = now - timedelta(hours=1)
    await tracker(m).handle_rb_number("B", now)
    (card,) = _experiment_cards(exp_channel)
    assert (card.title, card.text) == ("PEARL: Experiment finished", "RB A")
    assert dict(card.facts) == {
        "Runs": "at least 3", "Total collected": "at least 150.0 µA·h", "Average run": "50.0 µA·h",
        "Duration": "at least 30h 0m",
    }

    # B's: its first run is the one that starts now, as the 50 was A's.
    await _finish_runs(m, 0.0, 20.0, 30.0)
    await tracker(m).handle_rb_number("0", now + timedelta(hours=5))  # B ends; the instrument is idle
    card = _experiment_cards(exp_channel)[-1]
    assert (card.text, dict(card.facts)) == ("RB B", {
        "Runs": "2", "Total collected": "50.0 µA·h", "Average run": "25.0 µA·h", "Duration": "5h 0m",
    })
    # Runs with no experiment are no one's.
    await _finish_runs(m, 10.0, 10.0)
    await tracker(m).handle_rb_number("C", now + timedelta(hours=6))
    assert len(_experiment_cards(exp_channel)) == 2
    assert tracker(m).state.exp_partial is False  # C's start was seen

    # An RB number with no runs (e.g. one corrected straight away) gets no card.
    await tracker(m).handle_rb_number("D", now + timedelta(hours=5))
    assert len(_experiment_cards(exp_channel)) == 2


@pytest.mark.parametrize("first", ["title", "reset", "counts"])
async def test_new_experiments_first_run_counts_whatever_order_its_pvs_come_in(mock_config, mock_channels, first):
    """An automated run plan: A's last run ends at 50, then B's first run
    starts, its title, BEGIN's reset (or even a few counts) coming before B."""
    _, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)
    await m._handle_update(rb("A"))
    await m._handle_update(title("A1"))
    await m._handle_update({"pv": PEARL_UAMPS, "value": 50.0})
    if first == "title":
        await m._handle_update(title("B1"))
    await m._handle_update({"pv": PEARL_UAMPS, "value": 0.0})
    if first == "counts":
        await m._handle_update({"pv": PEARL_UAMPS, "value": 0.5})
    await m._handle_update(rb("B"))
    await m._handle_update({"pv": PEARL_UAMPS, "value": 40.0})
    await m._handle_update({"pv": PEARL_UAMPS, "value": 0.0})  # B2 begins
    await m._handle_update(rb("C"))
    a, b = (dict(c.facts) for c in _experiment_cards(exp_channel))
    assert (a["Runs"], a["Total collected"]) == ("at least 1", "at least 50.0 µA·h")
    assert (b["Runs"], b["Total collected"]) == ("1", "40.0 µA·h")


async def test_new_experiments_first_run_gets_the_fallback_target(mock_config, mock_channels):
    m = auto_monitor(mock_config, mock_channels, fallback=30)
    await m._handle_update(rb("A"))
    await _finish_runs(m, 100.0, 100.0, 100.0)  # the last starts B's first run, targeted at 100
    assert tracker(m).state.run_target == 100.0
    await m._handle_update(rb("B"))
    assert (tracker(m).state.run_target, tracker(m).state.auto_runs) == (30.0, 0)


@pytest.mark.parametrize("before, resent, partial", [
    ("A", "0", True),  # A ended while unwatched
    ("A", "B", True),
    ("0", "B", False),  # A's end was seen, and the instrument was idle until B
])
async def test_rb_number_resent_on_connect(mock_config, mock_channels, before, resent, partial):
    """PVWS re-sends the RB number on connect: a change then happened unwatched."""
    _, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)
    await m._handle_update(rb("Z"))
    await m._handle_update(rb("A"))  # A's start is seen: its figures are exact
    await m._handle_update(title("A1"))
    await m._handle_update({"pv": PEARL_UAMPS, "value": 50.0})
    _age_run(m)
    await m._handle_update(rb(before))
    tracker(m).resubscribed()
    await m._handle_update(rb(resent))
    assert dict(_experiment_cards(exp_channel)[-1].facts)["Runs"] == ("at least 1" if partial else "1")
    assert tracker(m).state.exp_partial is True  # the new one began unwatched too


async def test_corrected_rb_number_doesnt_count_the_last_run_again(mock_config, mock_channels):
    _, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)
    await m._handle_update(rb("Z"))
    await m._handle_update(rb("A"))  # A's start is seen: its figures are exact
    await m._handle_update(title("A1"))
    await m._handle_update({"pv": PEARL_UAMPS, "value": 50.0})
    _age_run(m)
    await m._handle_update(rb("B"))  # a typo, corrected to C
    await m._handle_update(rb("C"))
    await _finish_runs(m, 50.0, 40.0)  # A1 ends; C's first run collects 40
    await m._handle_update(rb("D"))
    a, c = (dict(card.facts) for card in _experiment_cards(exp_channel))
    assert (a["Runs"], c["Runs"], c["Total collected"]) == ("1", "1", "40.0 µA·h")


async def test_rb_number_back_from_zero_doesnt_count_the_last_run_again(mock_config, mock_channels):
    """A -> "0" -> A (e.g. cleared and re-entered) ends A, then starts it again."""
    _, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)
    await m._handle_update(rb("Z"))
    await m._handle_update(rb("A"))
    await m._handle_update(title("A1"))
    await _finish_runs(m, 40.0)
    await m._handle_update({"pv": PEARL_UAMPS, "value": 50.0})  # in progress
    _age_run(m)
    await m._handle_update(rb("0"))
    await m._handle_update(rb("A"))
    await _finish_runs(m, 50.0, 10.0)  # the 50 completes
    await m._handle_update(rb("B"))
    first, second = (dict(card.facts) for card in _experiment_cards(exp_channel))
    assert (first["Runs"], first["Total collected"]) == ("2", "90.0 µA·h")
    assert (second["Runs"], second["Total collected"]) == ("1", "10.0 µA·h")


async def test_experiment_progress_survives_a_restart(mock_config, mock_channels):
    _, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)
    tracker(m).state.run_name = "Run 0"
    tracker(m).state.run_started_at = now = datetime.now(timezone.utc)
    await tracker(m).handle_rb_number("A", now)
    await m._handle_update({"pv": PEARL_UAMPS, "value": 10.0})
    tracker(m).state.run_started_at = now - timedelta(hours=1)
    await tracker(m).handle_rb_number("B", now)
    await _finish_runs(m, 40.0, 50.0)  # the 40 run began before B
    for resent in ("B", "C"):  # PVWS re-sends B on connect; or, it changed while the daemon was down
        m2 = restarted(m, make_monitor(mock_config, mock_channels))
        await tracker(m2).handle_rb_number(resent, now + timedelta(hours=1))
        await tracker(m2).handle_rb_number("C", now + timedelta(hours=1))
        facts = dict(_experiment_cards(exp_channel)[-1].facts)
        assert facts["Runs"] == ("1" if resent == "B" else "at least 1")
    # The new experiment began unwatched too.
    assert tracker(m2).state.exp_partial is True


async def test_runs_before_any_rb_number_are_not_in_its_history(mock_config, mock_channels):
    m = auto_monitor(mock_config, mock_channels)
    await m._handle_update(rb("0"))  # e.g. commissioning between experiments
    await _finish_runs(m, 60.0, 61.0, 62.0)
    await m._handle_update(rb("2610028"))
    assert not tracker(m).state.run_totals


async def test_new_rb_number_starts_a_new_run_history(mock_config, mock_channels):
    m = auto_monitor(mock_config, mock_channels)
    await m._handle_update({"pv": PEARL_RB})  # PVWS's first message can have no value
    await m._handle_update(rb("2610028"))
    await _finish_runs(m, 60.0, 61.0, 62.0)
    assert tracker(m).state.run_target == 61.0

    # The first seen and a repeat don't clear it.
    for update in (rb("2610028"), rb("")):
        await m._handle_update(update)
    assert tracker(m).state.run_target == 61.0

    # The last run of the old experiment ended; the new one's RB comes first.
    await m._handle_update({"pv": PEARL_UAMPS, "value": 63.0})
    _age_run(m)
    await m._handle_update(rb("2620381"))
    assert tracker(m).state.run_target == 61.0 and not tracker(m).state.run_totals  # still the old run's
    await m._handle_update({"pv": PEARL_UAMPS, "value": 0.0})  # its first run begins
    assert not tracker(m).state.run_totals  # the 63 was the old experiment's
    assert tracker(m).state.run_target == 100.0
    await _finish_runs(m, 20.0, 21.0, 22.0)
    assert tracker(m).state.run_target == 21.0


async def test_new_run_is_saved_before_its_cards(mock_config, mock_channels):
    """The new-run card, which shows the new run's target, is sent once it's saved."""
    _, exp_channel = mock_channels
    sink = MagicMock()
    order = []
    sink.record_run_completed.side_effect = lambda *a: order.append("completed")
    m = make_monitor(mock_config, mock_channels, sink=sink)
    exp_channel.broadcast.side_effect = lambda n: order.append((n.title, tracker(m).to_snapshot()["run_name"]))
    tracker(m).state.run_name = "Run 24"
    tracker(m).state.run_started_at = datetime.now(timezone.utc)
    tracker(m).state.current_counts = 42.0
    await m._handle_update(title("Run 25"))
    assert order == ["completed", ("PEARL: New run started", "Run 25")]
    sink.record_run_completed.assert_called_once_with("PEARL", ANY, "", 42.0)  # with Run 24's µA·h



async def test_blank_decoded_run_title_is_ignored(mock_config, mock_channels):
    _, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)
    await m._handle_update(title("Run 1"))
    await m._handle_update(title(b"\x00\x00\x00"))
    assert tracker(m).state.run_name == "Run 1"
    exp_channel.broadcast.assert_not_called()
    await m._handle_update(title("Run 2"))
    assert exp_channel.broadcast.call_args[0][0].text == "Run 2"


async def test_reconnect_requested_during_handshake_does_not_block_later_ones(mock_config, mock_channels):
    """The new connection satisfies a request made while it was being set up."""
    async with FakePVWS() as server:
        m = ws_monitor(mock_config, mock_channels, server.url)
        m._force_reconnect.set()  # as if r was pressed mid-handshake
        async with running(m):
            await asyncio.wait_for(server.connected.wait(), 2)
            await wait_until(lambda: m._current_ws is not None)
            assert m.request_reconnect() is True  # not ignored as "already pending"
            await wait_until(lambda: server.connections == 2)


async def test_run_title_control_characters_are_stripped(mock_config, mock_channels):
    """A title can't carry terminal escape sequences into `status`, the log or cards."""
    m = make_monitor(mock_config, mock_channels)
    await m._handle_update(title(b"\x1b]0;pwn\x07Run\x1b[2J 1\x00\x00"))
    assert tracker(m).state.run_name == "]0;pwn Run [2J 1"


def test_restored_run_name_is_cleaned_like_a_live_one(mock_config, mock_channels):
    m = make_monitor(mock_config, mock_channels)
    m.restore_instruments({"PEARL": saved(m, run_name="Run 1\x00", run_started_at="2026-01-01T00:00:00+00:00")})
    assert tracker(m).state.run_name == "Run 1"


async def test_instrument_state_round_trips_through_its_snapshot(mock_config, mock_channels):
    m = auto_monitor(mock_config, mock_channels)
    await m._handle_update(rb("2610028"))
    await _finish_runs(m, 60.0, 61.0, 62.0)
    await m._handle_update({"pv": PEARL_UAMPS, "value": 70.0})
    m2 = restarted(m, auto_monitor(mock_config, mock_channels))
    assert tracker(m2).to_snapshot() == tracker(m).to_snapshot()
    assert tracker(m2).state.run_totals == tracker(m).state.run_totals  # [60, 61, 62]
    assert m2.instrument_views()["PEARL"]["beam_target"] == "TS1"


@pytest.mark.parametrize("changes", [
    {"counts": None}, {"run_totals": "12"}, {"run_totals": [1.0, "x"]}, {"run_totals": [float("nan")]},
    {"run_started_at": "yesterday"}, {"end_notified": "yes"}, {"exp_runs": True}, {"exp_uah": float("inf")},
])
def test_a_malformed_saved_entry_is_ignored_whole(mock_config, mock_channels, caplog, changes):
    m = make_monitor(mock_config, mock_channels)
    fresh = tracker(m).to_snapshot()
    good = {"run_name": "Run 4", "run_started_at": "2026-01-01T00:00:00+00:00", "rb_number": "A"}
    m.restore_instruments({"PEARL": saved(m, **{**good, **changes})})
    assert tracker(m).to_snapshot() == fresh  # nothing partially applied
    assert "PEARL: ignoring malformed saved state" in caplog.text


@pytest.mark.parametrize("entry", [
    "junk", {}, {"format": 1, "run_name": "Run 4", "counts": 3.0},
    # DaemonState's layout before trackers saved their own state
    {"run_name": "Run 4", "counts": 3.0, "notify_counts": 100.0, "run_started_at": "2026-01-01T00:00:00+00:00",
     "end_notified": False, "stale_counts": -1.0, "run_totals": [], "target": 100.0, "auto_runs": 0, "auto": False},
])
def test_an_older_saved_entry_is_ignored(mock_config, mock_channels, caplog, entry):
    m = make_monitor(mock_config, mock_channels)
    fresh = tracker(m).to_snapshot()
    m.restore_instruments({"PEARL": entry})
    assert tracker(m).to_snapshot() == fresh
    assert "PEARL: ignoring saved state from an older version" in caplog.text


def test_saved_times_without_a_timezone_are_utc(mock_config, mock_channels):
    m = make_monitor(mock_config, mock_channels)
    m.restore_instruments({"PEARL": saved(m, run_name="Run 1", run_started_at="2026-09-27T08:00:00",
                                          exp_since="2026-09-26T08:00:00")})
    assert tracker(m).state.run_started_at == datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)
    assert tracker(m).state.exp_since == datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)


def test_finishing_card_is_due_again_after_switching_to_auto(mock_config, mock_channels):
    fixed = make_monitor(mock_config, mock_channels, counts_target=50)
    m = auto_monitor(mock_config, mock_channels, fallback=50)
    m.restore_instruments({"PEARL": saved(fixed, run_name="Run 1", run_started_at="2026-09-27T08:00:00+00:00",
                                          end_notified=True)})
    assert tracker(m).state.run_name == "Run 1" and tracker(m).state.end_notified is False


# ---------------------------------------------------------------------------
# CS:INSTLIST: PV prefixes and default beam targets
# ---------------------------------------------------------------------------

def instlist(*entries):
    """A CS:INSTLIST update listing (name, prefix, targetStation) entries."""
    return {"pv": "CS:INSTLIST", "b64byt": encode(
        [{"name": name, "pvPrefix": prefix, "targetStation": station} for name, prefix, station in entries]
    )}


def listed_monitor(mock_config, mock_channels, sink=None, **instrument):
    """A monitor whose PEARL has no beam_target set, so it comes from CS:INSTLIST."""
    config = replace(mock_config, instrument_target="TS2",
                     instruments=[InstrumentConfig("PEARL", 100.0, **instrument)])
    return make_monitor(config, mock_channels, sink=sink)


async def test_instlist_sets_the_beam_target_and_resubscribes_a_new_prefix(mock_config, mock_channels):
    m = listed_monitor(mock_config, mock_channels)
    assert tracker(m).beam_target == "TS2"  # [PVS] instrument_target until the list comes
    m._current_ws = AsyncMock()
    tracker(m).state.rb_resync = False

    await m._handle_update(instlist(("PEARL", "IN:PEARL_5B:", "MUON")))
    assert tracker(m).state.rb_resync  # an RB number from the new PVs wasn't watched changing

    assert tracker(m).beam_target == "Muons"
    assert m.instrument_views()["PEARL"]["beam_target"] == "Muons"
    old = [f"IN:PEARL:DAE:{pv}"
           for pv in ("_RBNUMBER", "RUNNUMBER", "WDTITLE", "TOTALUAMPS", "RUNSTATE_STR", "STARTTIME")]
    new = [pv.replace("IN:PEARL:", "IN:PEARL_5B:") for pv in old]
    assert [json.loads(c.args[0]) for c in m._current_ws.send.await_args_list] == [
        {"type": "clear", "pvs": old}, {"type": "subscribe", "pvs": new},
    ]
    assert set(new) <= set(m.routes) and not set(old) & set(m.routes)
    await m._handle_update({"pv": "IN:PEARL_5B:DAE:TOTALUAMPS", "value": 12.0})
    assert tracker(m).state.current_counts == 12.0

    m._current_ws.send.reset_mock()
    await m._handle_update(instlist(("PEARL", "IN:PEARL_5B:", "MUON")))  # re-sent on reconnect
    m._current_ws.send.assert_not_awaited()


async def test_instlist_misc_falls_back_to_instrument_target_with_one_warning(mock_config, mock_channels, caplog):
    m = listed_monitor(mock_config, mock_channels)
    for _ in range(2):
        await m._handle_update(instlist(("PEARL", "IN:PEARL:", "MISC")))
    assert tracker(m).beam_target == "TS2"
    assert caplog.text.count("PEARL: CS:INSTLIST doesn't put it on TS1, TS2 or MUON") == 1


async def test_a_configured_beam_target_wins_over_instlist(mock_config, mock_channels):
    m = listed_monitor(mock_config, mock_channels, beam_target="TS1")
    await m._handle_update(instlist(("PEARL", "IN:PEARL:", "MUON")))
    assert tracker(m).beam_target == "TS1"


async def test_an_unlisted_instrument_keeps_its_pvs_with_one_warning(mock_config, mock_channels, caplog):
    m = listed_monitor(mock_config, mock_channels)
    await m._handle_update(instlist(("PEARL", "IN:PEARL:", "MUON")))
    for _ in range(2):
        await m._handle_update(instlist(("WISH", "IN:WISH:", "TS2")))
    assert caplog.text.count("PEARL isn't in CS:INSTLIST") == 1 and "MUON" not in caplog.text
    assert PEARL_UAMPS in m.routes and tracker(m).beam_target == "TS2"  # not Muons from the older list


async def test_an_unreadable_instlist_changes_nothing(mock_config, mock_channels):
    state = DaemonState(instrument_names=["PEARL"])
    state.instlist = {"PEARL": ("IN:PEARL:", "TS1")}
    m = listed_monitor(mock_config, mock_channels, sink=state)
    await m._handle_update({"pv": "CS:INSTLIST", "b64byt": "!!!"})
    assert tracker(m).beam_target == "TS1" and PEARL_UAMPS in m.routes
    assert state.instlist == {"PEARL": ("IN:PEARL:", "TS1")}  # the last good one is kept


async def test_the_cached_instlist_is_used_from_the_first_connection(mock_config, mock_channels):
    state = DaemonState(instrument_names=["PEARL"])
    state.instlist = {"PEARL": ("IN:PEARL_5B:", "Muons")}
    async with FakePVWS([]) as server:
        m = listed_monitor(replace(mock_config, isis_websocket_url=server.url), mock_channels, sink=state)
        async with running(m):
            await wait_until(lambda: server.subscriptions)
    assert "IN:PEARL_5B:DAE:TOTALUAMPS" in server.subscriptions[0]["pvs"]
    assert m.instrument_views()["PEARL"]["beam_target"] == "Muons"


async def test_a_new_instlist_is_saved_for_the_next_start(mock_config, mock_channels):
    state = DaemonState(instrument_names=["PEARL"])
    m = listed_monitor(mock_config, mock_channels, sink=state)
    await m._handle_update(instlist(("PEARL", "IN:PEARL:", "TS1")))
    restored = DaemonState()
    restored.restore_from_snapshot_json(json.dumps(state.snapshot()))
    assert restored.instlist == {"PEARL": ("IN:PEARL:", "TS1")}


# ---------------------------------------------------------------------------
# The shadow RunTracker (1.3.0rc1)
# ---------------------------------------------------------------------------

async def test_a_failing_shadow_never_reaches_the_live_path(mock_config, mock_channels, caplog):
    with patch("isis_monitor.runs.RunTracker.on_counts", side_effect=RuntimeError("shadow bug")):
        m = make_monitor(mock_config, mock_channels)  # its handlers are bound now
    tracker(m).runs.to_snapshot = MagicMock(side_effect=RuntimeError("shadow bug"))
    tracker(m).runs.resubscribed = MagicMock(side_effect=RuntimeError("shadow bug"))
    await m._handle_update({"pv": PEARL_UAMPS, "value": 12.0})
    tracker(m).resubscribed()
    assert tracker(m).state.current_counts == 12.0  # the live tracker still had it
    assert tracker(m).to_snapshot()["shadow"] is None
    assert caplog.text.count("[shadow] PEARL: failed") == 3


async def test_the_shadow_state_is_saved_with_the_tracker(mock_config, mock_channels):
    m = make_monitor(mock_config, mock_channels)
    for update in ({"pv": "IN:PEARL:DAE:RUNNUMBER", "text": "00010"}, {"pv": "IN:PEARL:DAE:RUNSTATE_STR", "text": "SETUP"},
                   {"pv": "IN:PEARL:DAE:_RBNUMBER", "text": "123"}):
        await m._handle_update(update)
    restored = make_monitor(mock_config, mock_channels)
    restored.restore_instruments(json.loads(json.dumps(m.instrument_views())))
    assert tracker(restored).runs.to_snapshot() == tracker(m).runs.to_snapshot()
    assert tracker(restored).runs.restored and tracker(restored).runs.rb == "123"
