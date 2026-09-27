import asyncio
import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatusCode
import base64
import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch
from isis_monitor.config import AppConfig, InstrumentConfig
from isis_monitor.daemon_state import DaemonState
from isis_monitor.notifiers import NotificationChannel
from isis_monitor.beam import (
    BeamMonitor,
    BEAM_TARGETS,
    _classify_ws_error,
)
from isis_monitor.instrument import STALL_CHECK_WINDOW, _fit_rate


PEARL_UAMPS = "IN:PEARL:DAE:TOTALUAMPS"


def tracker(m):
    """The monitor's only instrument tracker."""
    (only,) = m.instruments.values()
    return only


def _seed_collected_baseline(m, now, value):
    """Insert a counts-collected sample older than STALL_CHECK_WINDOW so
    _check_collection_progress has something to compare the current reading to."""
    tracker(m).state.collected_samples.append((now - STALL_CHECK_WINDOW - timedelta(seconds=30), value))

DEBOUNCE_SECONDS = 0.05
SETTLE = DEBOUNCE_SECONDS * 3  # wait comfortably past the debounce window in tests


@pytest.fixture
def mock_config():
    return AppConfig(
        mcr_news_url="",
        isis_websocket_url="wss://test",
        news_teams_url="",
        beam_teams_url="",
        experiment_teams_url="",
        instruments=[InstrumentConfig(
            "PEARL", 100.0, "TS1", run_name_pv=AppConfig.run_name_pv,
        )],
    )


@pytest.fixture
def mock_channels():
    beam_channel = NotificationChannel("Beam")
    beam_channel.broadcast = AsyncMock()
    exp_channel = NotificationChannel("Exp")
    exp_channel.broadcast = AsyncMock()
    return beam_channel, exp_channel


def make_monitor(mock_config, mock_channels, counts_target=100, rng=None, sink=None):
    beam_channel, exp_channel = mock_channels
    instruments = [replace(i, notify_counts=counts_target) for i in mock_config.instruments]
    return BeamMonitor(
        replace(mock_config, instruments=instruments), beam_channel, exp_channel,
        debounce_seconds=DEBOUNCE_SECONDS, rng=rng, sink=sink,
    )


# ---------------------------------------------------------------------------
# _safe_float
# ---------------------------------------------------------------------------

def test_safe_float(mock_config, mock_channels):
    m = make_monitor(mock_config, mock_channels)
    assert m._safe_float("123.4") == 123.4
    assert m._safe_float(123.4) == 123.4
    assert m._safe_float("NaN") == 0.0
    assert m._safe_float("nan") == 0.0
    assert m._safe_float("bad_string") == 0.0
    assert m._safe_float(None) == 0.0


# ---------------------------------------------------------------------------
# _get_power_label
# ---------------------------------------------------------------------------

def test_get_power_label(mock_config, mock_channels):
    m = make_monitor(mock_config, mock_channels)
    assert m._get_power_label(-5, "TS1") == "off"
    assert m._get_power_label(0, "TS1") == "off"
    assert m._get_power_label(20, "TS1") == "low"
    assert m._get_power_label(75, "TS1") == "medium"
    assert m._get_power_label(150, "TS1") == "high"
    assert m._get_power_label(0, "TS2") == "off"
    assert m._get_power_label(5, "TS2") == "low"
    assert m._get_power_label(15, "TS2") == "medium"
    assert m._get_power_label(30, "TS2") == "high"


# ---------------------------------------------------------------------------
# _handle_update — beam-current arm
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_update_beam_startup_sends_immediately(mock_config, mock_channels):
    """The first reading for a target is a startup card, sent with no debounce."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "10.0"})

    assert m.state.beams["TS1"].current == 10.0
    assert m.state.beams["TS1"].power == "low"
    beam_channel.broadcast.assert_called_once()
    notification = beam_channel.broadcast.call_args[0][0]
    assert "online" in notification.title and notification.title.endswith("TS1 is low")
    assert notification.channel == "TS1"


@pytest.mark.asyncio
async def test_handle_update_beam_no_change_no_broadcast(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "10.0"})
    beam_channel.broadcast.reset_mock()

    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "45.0"})
    await asyncio.sleep(SETTLE)

    assert m.state.beams["TS1"].current == 45.0
    assert m.state.beams["TS1"].power == "low"
    beam_channel.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_handle_update_beam_change_is_debounced(mock_config, mock_channels):
    """A confirmed state change is not sent immediately — only after the debounce window."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "10.0"})  # startup
    beam_channel.broadcast.reset_mock()

    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "60.0"})
    assert m.state.beams["TS1"].power == "medium"  # state updates immediately
    beam_channel.broadcast.assert_not_called()  # but no card yet — still pending

    await asyncio.sleep(SETTLE)

    beam_channel.broadcast.assert_called_once()
    notification = beam_channel.broadcast.call_args[0][0]
    assert "low → medium" in notification.title
    assert notification.channel == "TS1"


@pytest.mark.asyncio
async def test_handle_update_beam_flapping_sends_nothing(mock_config, mock_channels):
    """A change that reverts before the debounce window elapses is dropped entirely."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "10.0"})  # startup
    beam_channel.broadcast.reset_mock()

    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "60.0"})  # -> medium
    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "10.0"})  # -> low again
    await asyncio.sleep(SETTLE)

    beam_channel.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_handle_update_beam_trip_notes_other_targets(mock_config, mock_channels):
    """When multiple targets go off in the same debounce window, each card names the others."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    # Startup — get all three targets to a non-zero state first.
    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "60.0"})
    await m._handle_update({"pv": mock_config.ts2_beam_current_pv, "value": "20.0"})
    await m._handle_update({"pv": mock_config.muon_beam_current_pv, "value": "3.0"})
    beam_channel.broadcast.reset_mock()

    # All three trip off within the same window.
    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "0.0"})
    await m._handle_update({"pv": mock_config.ts2_beam_current_pv, "value": "0.0"})
    await m._handle_update({"pv": mock_config.muon_beam_current_pv, "value": "0.0"})
    await asyncio.sleep(SETTLE)

    assert beam_channel.broadcast.call_count == 3
    for sent in beam_channel.broadcast.call_args_list:
        notification = sent.args[0]
        assert "also went off, likely a facility-wide trip" in notification.text


@pytest.mark.asyncio
async def test_change_aggregator_cancel_all_stops_pending_flush(mock_config, mock_channels):
    """cancel_all() (called on shutdown) must stop pending timers from firing."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "10.0"})  # startup
    beam_channel.broadcast.reset_mock()

    await m._handle_update({"pv": mock_config.ts1_beam_current_pv, "value": "60.0"})  # pending
    m.change_aggregator.cancel_all()
    await asyncio.sleep(SETTLE)

    beam_channel.broadcast.assert_not_called()
    assert m.change_aggregator._pending == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("fun_mode", [False, True])
async def test_handle_update_beam_flavour_only_in_fun_mode(mock_config, mock_channels, fun_mode):
    """Flavour lines appear only with fun_mode on. fun_mode defaults to False,
    and then no flavour is added even with an rng available."""
    config = replace(mock_config, fun_mode=True) if fun_mode else mock_config
    beam_channel, exp_channel = mock_channels
    m = make_monitor(config, mock_channels, rng=random.Random(1))

    await m._handle_update({"pv": config.ts1_beam_current_pv, "value": "10.0"})  # startup
    assert (beam_channel.broadcast.call_args[0][0].flavour != "") is fun_mode
    beam_channel.broadcast.reset_mock()

    await m._handle_update({"pv": config.ts1_beam_current_pv, "value": "60.0"})
    await asyncio.sleep(SETTLE)
    assert (beam_channel.broadcast.call_args[0][0].flavour != "") is fun_mode


# ---------------------------------------------------------------------------
# _handle_update — run-name (b64byt) arm
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_update_run_name_first_set(mock_config, mock_channels):
    """The very first run seen isn't a completion: no card, nothing to count yet."""
    beam_channel, exp_channel = mock_channels
    sink = MagicMock()
    m = make_monitor(mock_config, mock_channels, sink=sink)

    run_name = "Run 12345"
    b64 = base64.b64encode(run_name.encode()).decode()
    await m._handle_update({"pv": mock_config.run_name_pv, "b64byt": b64})

    assert tracker(m).state.run_name == run_name
    exp_channel.broadcast.assert_not_called()  # No previous run → no notification
    sink.record_run_completed.assert_not_called()


@pytest.mark.asyncio
async def test_handle_update_run_name_change(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    # Seed first run
    tracker(m).state.run_name = "Run 12345"
    tracker(m).state.run_started_at = datetime.now(timezone.utc) - timedelta(hours=2)
    tracker(m).state.current_counts = 1000.0

    new_run = "Run 12346"
    b64 = base64.b64encode(new_run.encode()).decode()
    await m._handle_update({"pv": mock_config.run_name_pv, "b64byt": b64})

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


@pytest.mark.asyncio
async def test_handle_update_run_name_change_resets_end_notified(mock_config, mock_channels):
    """A new run must re-arm the 'about to finish' notification for itself,
    even if the previous run ended with end_notified already set."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels, counts_target=10)
    tracker(m).state.run_name = "Run 1"
    tracker(m).state.run_started_at = datetime.now(timezone.utc)
    tracker(m).state.end_notified = True

    b64 = base64.b64encode(b"Run 2").decode()
    await m._handle_update({"pv": mock_config.run_name_pv, "b64byt": b64})
    assert tracker(m).state.end_notified is False

    exp_channel.broadcast.reset_mock()
    # counts_target=10 is small enough that the old "< target - 25" reset
    # path would never trip; the run-start reset must do it instead.
    await m._handle_update({"pv": PEARL_UAMPS, "value": 12.0})
    exp_channel.broadcast.assert_called_once()
    assert "about to finish" in exp_channel.broadcast.call_args[0][0].title


@pytest.mark.asyncio
async def test_handle_update_run_name_nan_ignored(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)

    await m._handle_update({"pv": mock_config.run_name_pv, "b64byt": "nan"})
    assert tracker(m).state.run_name == ""
    exp_channel.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_handle_update_run_name_change_no_milestone_for_zero_total(mock_config, mock_channels):
    """The sink returns 0 for an instrument it doesn't know; 0 % 25 == 0
    must not be mistaken for a milestone."""
    fun_config = replace(mock_config, fun_mode=True)
    _, exp_channel = mock_channels
    sink = MagicMock()
    sink.record_run_completed.return_value = 0
    m = make_monitor(fun_config, mock_channels, sink=sink)
    tracker(m).state.run_name = "Run 1"
    tracker(m).state.run_started_at = datetime.now(timezone.utc)

    await m._handle_update({"pv": fun_config.run_name_pv, "b64byt": base64.b64encode(b"Run 2").decode()})

    assert exp_channel.broadcast.call_count == 1  # just the "new run" card


@pytest.mark.asyncio
async def test_handle_update_run_name_change_no_milestone_without_fun_mode(mock_config, mock_channels):
    beam_channel, exp_channel = mock_channels
    sink = MagicMock()
    sink.record_run_completed.return_value = 25
    m = make_monitor(mock_config, mock_channels, sink=sink)  # fun_mode defaults to False
    tracker(m).state.run_name = "Run 24"
    tracker(m).state.run_started_at = datetime.now(timezone.utc)

    b64 = base64.b64encode(b"Run 25").decode()
    await m._handle_update({"pv": mock_config.run_name_pv, "b64byt": b64})

    exp_channel.broadcast.assert_called_once()  # only the "new run" card


# ---------------------------------------------------------------------------
# _handle_update — counts (IN:<NAME>:DAE:TOTALUAMPS) arm
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_update_counts_triggers_notification_past_the_target(mock_config, mock_channels):
    """TOTALUAMPS publishes the total µA·h collected this run as a number."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels, counts_target=100)
    tracker(m).state.run_name = "Run 1"
    m.state.beams["TS1"].power = "high"  # PEARL's own beam target

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


@pytest.mark.asyncio
async def test_handle_update_counts_malformed(mock_config, mock_channels):
    sink = MagicMock()
    m = make_monitor(mock_config, mock_channels, counts_target=100, sink=sink)

    await m._handle_update({"pv": PEARL_UAMPS, "value": "bad_format"})
    await m._handle_update({"pv": PEARL_UAMPS, "value": float("inf")})
    await m._handle_update({"pv": PEARL_UAMPS, "value": "NaN"})
    await m._handle_update({"pv": PEARL_UAMPS, "text": "50/90"})  # old dashboard format: not routed
    assert tracker(m).state.current_counts == -1.0  # unchanged, no crash
    sink.update_counts.assert_not_called()
    await m._handle_update({"pv": PEARL_UAMPS, "value": 42.0})
    sink.update_counts.assert_called_once_with("PEARL", 42.0)


@pytest.mark.asyncio
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


# ---------------------------------------------------------------------------
# _check_collection_progress — stall detection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_collection_progress_no_check_without_enough_history(mock_config, mock_channels):
    """Fewer than STALL_CHECK_WINDOW worth of samples means there's nothing
    to compare against yet — must not warn."""
    beam_channel, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)
    m._current_ws = MagicMock()  # connected to PVWS
    tracker(m).state.run_name = "Run 1"
    now = datetime.now(timezone.utc)

    tracker(m).state.collected_samples.append((now - timedelta(minutes=1), 100.0))
    tracker(m).state.current_counts = 100.0

    await m._check_collection_progress(now)

    exp_channel.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_check_collection_progress_no_false_stall_when_counts_update_in_batches(mock_config, mock_channels):
    """Regression: a source that updates the collected count in less frequent
    batches than the 60s check interval must not look stalled just because a
    batch hasn't landed in the latest minute — movement is judged over
    STALL_CHECK_WINDOW, not the last tick."""
    beam_config = replace(mock_config, stall_minutes=0.01)
    beam_channel, exp_channel = mock_channels
    m = make_monitor(beam_config, mock_channels)
    m._current_ws = MagicMock()  # connected to PVWS
    tracker(m).state.run_name = "Run 1"
    m.state.beams["TS1"].power = "high"
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


@pytest.mark.asyncio
async def test_check_collection_progress_detects_stall_when_instrument_beam_on(mock_config, mock_channels):
    beam_config = replace(mock_config, stall_minutes=0.01)  # ~0.6s, fast for tests
    beam_channel, exp_channel = mock_channels
    m = make_monitor(beam_config, mock_channels)
    m._current_ws = MagicMock()  # connected to PVWS
    tracker(m).state.run_name = "Run 1"
    m.state.beams["TS1"].power = "high"  # instrument beam is on

    now = datetime.now(timezone.utc)
    _seed_collected_baseline(m, now, 100.0)
    tracker(m).state.current_counts = 100.0  # unmoved over the window

    await m._check_collection_progress(now)  # starts the stall clock
    exp_channel.broadcast.assert_not_called()

    await m._check_collection_progress(now + timedelta(seconds=1))  # past stall_minutes
    exp_channel.broadcast.assert_called_once()
    notification = exp_channel.broadcast.call_args[0][0]
    assert notification.title == "PEARL: Data collection stalled"


@pytest.mark.asyncio
async def test_check_collection_progress_movement_resets_stall_clock(mock_config, mock_channels):
    beam_config = replace(mock_config, stall_minutes=0.01)
    beam_channel, exp_channel = mock_channels
    m = make_monitor(beam_config, mock_channels)
    m._current_ws = MagicMock()  # connected to PVWS
    tracker(m).state.run_name = "Run 1"
    m.state.beams["TS1"].power = "high"

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


@pytest.mark.asyncio
async def test_check_collection_progress_no_active_run_never_warns(mock_config, mock_channels):
    """Between runs, the collected count is naturally static — that must not
    look like a stall just because no run is currently in progress."""
    beam_config = replace(mock_config, stall_minutes=0.01)
    beam_channel, exp_channel = mock_channels
    m = make_monitor(beam_config, mock_channels)
    m._current_ws = MagicMock()  # connected to PVWS
    assert tracker(m).state.run_name == ""  # no run active
    m.state.beams["TS1"].power = "high"

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

import contextlib
import json

import websockets


class FakePVWS:
    """A local PVWS stand-in: records subscriptions, pushes scripted messages."""

    def __init__(self, messages=(), close_after_send=False):
        self.messages = list(messages)
        self.close_after_send = close_after_send
        self.connections = 0
        self.subscriptions = []
        self.connected = asyncio.Event()
        self._server = None

    async def _handler(self, ws):
        self.connections += 1
        self.subscriptions.append(json.loads(await ws.recv()))
        self.connected.set()
        for msg in self.messages:
            await ws.send(msg if isinstance(msg, str) else json.dumps(msg))
        if self.close_after_send:
            return
        await ws.wait_closed()

    async def __aenter__(self):
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()


async def wait_until(predicate, timeout=2.0):
    async def _poll():
        while not predicate():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_poll(), timeout)


@contextlib.asynccontextmanager
async def running(monitor):
    task = asyncio.create_task(monitor.run())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def ws_monitor(mock_config, mock_channels, url, sink=None, reconnect_interval=60.0, **kw):
    config = replace(mock_config, isis_websocket_url=url, beam_reconnect_interval=reconnect_interval)
    return make_monitor(config, mock_channels, sink=sink, **kw)


@pytest.mark.asyncio
async def test_run_loop_subscribes_and_dispatches_updates(mock_config, mock_channels):
    sink = MagicMock()
    update = {"type": "update", "pv": mock_config.ts1_beam_current_pv, "value": 150.0}
    async with FakePVWS([update]) as server:
        m = ws_monitor(mock_config, mock_channels, server.url, sink=sink)
        async with running(m):
            await wait_until(lambda: sink.update_beam_state.called)

    assert set(server.subscriptions[0]["pvs"]) == {
        mock_config.ts1_beam_current_pv, mock_config.ts2_beam_current_pv,
        mock_config.muon_beam_current_pv, PEARL_UAMPS, mock_config.run_name_pv,
    }
    sink.update_beam_state.assert_called_with("TS1", 150.0, "high")
    sink.update_health.assert_any_call("beam", "connected")


@pytest.mark.asyncio
async def test_run_loop_survives_malformed_messages(mock_config, mock_channels, caplog):
    """Bad frames and handler errors are logged and skipped, not a reconnect."""
    sink = MagicMock()
    good = {"type": "update", "pv": mock_config.ts2_beam_current_pv, "value": 40.0}
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


@pytest.mark.asyncio
async def test_run_loop_reconnects_after_server_closes(mock_config, mock_channels):
    sink = MagicMock()
    async with FakePVWS(close_after_send=True) as server:
        m = ws_monitor(mock_config, mock_channels, server.url, sink=sink, reconnect_interval=0.02)
        async with running(m):
            await wait_until(lambda: server.connections >= 3)
    sink.update_health.assert_any_call("beam", "disconnected")


@pytest.mark.asyncio
async def test_reconnect_repopulates_beam_states_marked_unknown(mock_config, mock_channels):
    """DaemonState marks beams unknown when the feed drops; PVWS re-sending an
    unchanged value on reconnect must restore it, without a beam notification."""
    beam_channel, _ = mock_channels
    state = DaemonState()
    events = state.subscribe()
    update = {"type": "update", "pv": mock_config.ts1_beam_current_pv, "value": 150.0}
    powers = []

    def ts1_powers():
        while not events.empty():
            event = events.get_nowait()
            if event.event == "beam" and event.payload["beam"] == "TS1":
                powers.append(event.payload["power"])
        return powers

    async with FakePVWS([update], close_after_send=True) as server:
        m = ws_monitor(mock_config, mock_channels, server.url, sink=state, reconnect_interval=0.02)
        async with running(m):
            await wait_until(lambda: ts1_powers()[:3] == ["high", "unknown", "high"])

    beam_channel.broadcast.assert_called_once()  # just the startup card


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_request_reconnect_while_disconnected_skips_backoff(mock_config, mock_channels):
    """A reconnect requested during the backoff wait retries at once, and does
    not linger to tear down the next successful connection."""
    async with FakePVWS() as server:
        url = server.url
    # Server is now closed: the first attempt fails and backs off for 60s.
    sink = MagicMock()
    m = ws_monitor(mock_config, mock_channels, url, sink=sink, reconnect_interval=60.0)
    async with running(m):
        await wait_until(lambda: call("beam", "disconnected") in sink.update_health.call_args_list)
        port = int(url.rsplit(":", 1)[1])
        server = FakePVWS()
        server._server = await websockets.serve(server._handler, "127.0.0.1", port)
        try:
            m.request_reconnect()
            await asyncio.wait_for(server.connected.wait(), 2)
            await asyncio.sleep(0.1)
            assert server.connections == 1  # the stale request didn't force a second one
        finally:
            server._server.close()
            await server._server.wait_closed()


@pytest.mark.asyncio
async def test_run_cancels_promptly_and_cancels_pending_debounce(mock_config, mock_channels):
    async with FakePVWS() as server:
        m = ws_monitor(mock_config, mock_channels, server.url)
        task = asyncio.create_task(m.run())
        await asyncio.wait_for(server.connected.wait(), 2)
        now = datetime.now(timezone.utc)
        m.change_aggregator.queue_change(BEAM_TARGETS[0], "high", 150, now, "off", 0, 140, now)
        pending_task = m.change_aggregator._pending["TS1"].task
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    await asyncio.sleep(0)
    assert pending_task.cancelled()
    assert m.change_aggregator._pending == {}


@pytest.mark.asyncio
async def test_run_without_url_returns_immediately(mock_config, mock_channels, caplog):
    m = ws_monitor(mock_config, mock_channels, "")
    await asyncio.wait_for(m.run(), 1)
    assert "will not run" in caplog.text


@pytest.mark.asyncio
async def test_collection_check_loop_calls_progress_check(mock_config, mock_channels):
    m = make_monitor(mock_config, mock_channels)
    m._check_collection_progress = AsyncMock()
    with patch("isis_monitor.beam.COLLECTION_CHECK_INTERVAL", 0.01):
        task = asyncio.create_task(m._collection_check_loop())
        await wait_until(lambda: m._check_collection_progress.await_count >= 2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_aggregator_flush_without_pending_is_noop(mock_config, mock_channels):
    m = make_monitor(mock_config, mock_channels)
    await m.change_aggregator._flush("TS1")
    mock_channels[0].broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_handle_update_run_name_bad_base64_is_ignored(mock_config, mock_channels, caplog):
    m = make_monitor(mock_config, mock_channels)
    await m._handle_update({"pv": mock_config.run_name_pv, "b64byt": "!!!not base64"})
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


@pytest.mark.asyncio
async def test_run_loop_bad_url_marks_health_and_retries(mock_config, mock_channels, caplog):
    sink = MagicMock()
    m = ws_monitor(mock_config, mock_channels, "not-a-websocket-url", sink=sink, reconnect_interval=0.01)
    async with running(m):
        await wait_until(lambda: "WebSocket invalid URL" in caplog.text)
        await wait_until(lambda: call("beam", "disconnected") in sink.update_health.call_args_list)


def test_classify_ws_error():
    import socket
    import ssl
    from websockets.exceptions import ConnectionClosedError, InvalidStatus, InvalidURI
    from websockets.http11 import Response

    cases = {
        InvalidStatusCode(403, Headers()): "rejected",
        InvalidStatusCode(404, Headers()): "rejected",
        InvalidStatusCode(503, Headers()): "transient",
        InvalidStatus(Response(401, "Unauthorized", Headers())): "rejected",
        InvalidURI("nope", "not a ws URL"): "config",
        ssl.SSLCertVerificationError("certificate verify failed"): "tls",
        socket.gaierror(-2, "Name or service not known"): "dns",
        ConnectionClosedError(None, None): "transient",
        ConnectionRefusedError(111, "refused"): "transient",
        asyncio.TimeoutError(): "transient",
        RuntimeError("bug"): "unexpected",
    }
    for exc, kind in cases.items():
        assert _classify_ws_error(exc)[0] == kind, exc
    assert "HTTP 403" in _classify_ws_error(InvalidStatusCode(403, Headers()))[1]


@pytest.mark.asyncio
async def test_run_loop_backs_off_only_for_persistent_problems(mock_config, mock_channels):
    """Transient failures retry after reconnect_interval; persistent ones
    double the wait each time up to BEAM_MAX_BACKOFF, resetting on connect."""
    m = ws_monitor(mock_config, mock_channels, "ws://pvws.invalid", reconnect_interval=5.0)
    rejected = InvalidStatusCode(403, Headers())
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
        raise asyncio.TimeoutError

    with patch("isis_monitor.beam.websockets.connect", side_effect=connect), \
            patch("isis_monitor.beam.asyncio.wait_for", side_effect=fake_wait_for):
        with pytest.raises(asyncio.CancelledError):
            await m._run_loop()

    assert waits == [5, 10, 20, 40, 80, 160, 300, 300, 5, 5, 5, 10]


@pytest.mark.asyncio
async def test_run_loop_logs_repeated_failures_once(mock_config, mock_channels, caplog):
    import logging
    async with FakePVWS() as server:
        url = server.url
    # Server is now closed, so every attempt is refused.
    m = ws_monitor(mock_config, mock_channels, url, reconnect_interval=0.01)
    with caplog.at_level(logging.INFO, logger="isis_monitor.beam"):
        async with running(m):
            await wait_until(lambda: "reconnecting in" in caplog.text)
            await asyncio.sleep(0.1)  # several more refused attempts
            assert caplog.text.count("reconnecting in") == 1

            port = int(url.rsplit(":", 1)[1])
            server = FakePVWS()
            server._server = await websockets.serve(server._handler, "127.0.0.1", port)
            try:
                await wait_until(lambda: "WebSocket connected after" in caplog.text)
            finally:
                server._server.close()
                await server._server.wait_closed()
    assert "failed attempt(s) over" in caplog.text


@pytest.mark.asyncio
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
    return BeamMonitor(config, *mock_channels, debounce_seconds=DEBOUNCE_SECONDS)


@pytest.mark.asyncio
async def test_updates_are_routed_to_their_own_instrument(mock_config, mock_channels):
    _, exp_channel = mock_channels
    m = two_instrument_monitor(mock_config, mock_channels)
    pearl, wish = m.instruments["PEARL"], m.instruments["WISH"]

    await m._handle_update({"pv": "IN:WISH:DAE:WDTITLE", "b64byt": base64.b64encode(b"Wish run").decode()})
    await m._handle_update({"pv": "IN:WISH:DAE:TOTALUAMPS", "value": 60.0})

    assert (wish.state.run_name, wish.state.current_counts) == ("Wish run", 60.0)
    assert (pearl.state.run_name, pearl.state.current_counts) == ("", -1.0)
    # 60 is past WISH's own notify count (50) though not PEARL's (100).
    exp_channel.broadcast.assert_called_once()
    assert exp_channel.broadcast.call_args[0][0].title == "WISH: Run about to finish"


@pytest.mark.asyncio
async def test_run_loop_subscribes_to_every_instruments_pvs(mock_config, mock_channels):
    async with FakePVWS([]) as server:
        config = replace(mock_config, isis_websocket_url=server.url)
        m = two_instrument_monitor(config, mock_channels)
        async with running(m):
            await wait_until(lambda: server.subscriptions)

    assert {"IN:PEARL:DAE:TOTALUAMPS", "IN:PEARL:DAE:WDTITLE", "IN:WISH:DAE:TOTALUAMPS", "IN:WISH:DAE:WDTITLE"} <= set(
        server.subscriptions[0]["pvs"]
    )


@pytest.mark.asyncio
async def test_stall_check_uses_each_instruments_beam_target(mock_config, mock_channels):
    """WISH is on TS2, which is off, so only PEARL (on TS1, high) warns."""
    _, exp_channel = mock_channels
    m = two_instrument_monitor(replace(mock_config, stall_minutes=0.01), mock_channels)
    m._current_ws = MagicMock()  # connected to PVWS
    m.state.beams["TS1"].power = "high"
    m.state.beams["TS2"].power = "off"
    now = datetime.now(timezone.utc)
    for t in m.instruments.values():
        t.state.run_name = "Run 1"
        t.state.collected_samples.append((now - STALL_CHECK_WINDOW - timedelta(seconds=30), 100.0))
        t.state.current_counts = 100.0

    await m._check_collection_progress(now)
    await m._check_collection_progress(now + timedelta(seconds=1))

    assert m.instruments["PEARL"].state.stall_warned is True
    assert m.instruments["WISH"].state.stall_warned is False
    exp_channel.broadcast.assert_called_once()


@pytest.mark.asyncio
async def test_one_instruments_failed_stall_check_does_not_skip_others(mock_config, mock_channels, caplog):
    m = two_instrument_monitor(mock_config, mock_channels)
    m._current_ws = MagicMock()  # connected to PVWS
    m.instruments["PEARL"].check_collection_progress = AsyncMock(side_effect=RuntimeError("boom"))
    m.instruments["WISH"].check_collection_progress = AsyncMock()

    await m._check_collection_progress(datetime.now(timezone.utc))

    m.instruments["WISH"].check_collection_progress.assert_awaited_once()
    assert "Collection check failed for PEARL" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, expected", [("experiment", ""), ("instrument", "PEARL")])
async def test_run_cards_use_the_instruments_channel_setting(mock_config, mock_channels, mode, expected):
    """Blank is filled with "Experiment Updates" by the experiment channel on broadcast."""
    _, exp_channel = mock_channels
    config = replace(mock_config, fun_mode=True, stall_minutes=0.01,
                     instruments=[replace(mock_config.instruments[0], channel=mode)])
    sink = MagicMock()
    sink.record_run_completed.return_value = 25
    m = make_monitor(config, mock_channels, sink=sink)
    m._current_ws = MagicMock()  # connected to PVWS
    t = tracker(m)
    m.state.beams["TS1"].power = "high"
    t.state.run_name = "Run 24"
    t.state.run_started_at = datetime.now(timezone.utc)

    await m._handle_update({"pv": config.run_name_pv, "b64byt": base64.b64encode(b"Run 25").decode()})
    now = datetime.now(timezone.utc)
    _seed_collected_baseline(m, now, 150.0)  # samples are oldest first
    await m._handle_update({"pv": PEARL_UAMPS, "value": 150.0})
    await m._check_collection_progress(now)
    await m._check_collection_progress(now + timedelta(seconds=1))

    titles_channels = [(c.args[0].title, c.args[0].channel) for c in exp_channel.broadcast.call_args_list]
    assert [title for title, _ in titles_channels] == [
        "PEARL: New run started", "PEARL: 25 runs completed", "PEARL: Run about to finish",
        "PEARL: Data collection stalled",
    ]
    assert {channel for _, channel in titles_channels} == {expected}



@pytest.mark.asyncio
async def test_repeated_run_title_after_reconnect_keeps_the_run_start_time(mock_config, mock_channels):
    """PVWS re-sends the current title on every reconnect; the next new-run
    card must still report the whole previous run's duration."""
    _, exp_channel = mock_channels
    sink = MagicMock()
    m = make_monitor(mock_config, mock_channels, sink=sink)
    title = {"pv": mock_config.run_name_pv, "b64byt": base64.b64encode(b"Run 1").decode()}
    await m._handle_update(title)
    started = tracker(m).state.run_started_at
    tracker(m).state.run_started_at = started - timedelta(hours=6)  # the run began 6h ago

    await m._handle_update(title)  # reconnect re-sends it
    assert tracker(m).state.run_started_at == started - timedelta(hours=6)
    sink.update_run_name.assert_called_once()

    await m._handle_update({"pv": mock_config.run_name_pv, "b64byt": base64.b64encode(b"Run 2").decode()})
    card = exp_channel.broadcast.call_args[0][0]
    assert dict(card.facts)["Duration"].startswith("6h")


@pytest.mark.asyncio
async def test_no_stall_warning_while_pvws_is_disconnected(mock_config, mock_channels):
    """Counts are frozen and beam states stale during an outage."""
    _, exp_channel = mock_channels
    m = make_monitor(replace(mock_config, stall_minutes=0.01), mock_channels)
    t = tracker(m)
    t.state.run_name = "Run 1"
    m.state.beams["TS1"].power = "high"  # last known before the outage
    now = datetime.now(timezone.utc)
    _seed_collected_baseline(m, now, 100.0)
    t.state.current_counts = 100.0
    t.state.collection_stalled_since = now - timedelta(minutes=30)

    await m._check_collection_progress(now)
    await m._check_collection_progress(now + timedelta(minutes=5))
    exp_channel.broadcast.assert_not_called()
    assert t.state.collection_stalled_since is None  # restarts once reconnected



@pytest.mark.asyncio
async def test_dropped_flicker_does_not_reset_time_in_state(mock_config, mock_channels):
    """High for 10h, a 5s flicker to medium (dropped by the debounce), then
    off: the off card must say it was high for ~10h, not since the flicker."""
    beam_channel, _ = mock_channels
    m = make_monitor(mock_config, mock_channels)
    pv = mock_config.ts1_beam_current_pv
    await m._handle_update({"pv": pv, "value": 150.0})  # startup: high
    ten_hours_ago = datetime.now(timezone.utc) - timedelta(hours=10)
    m.state.beams["TS1"].since = ten_hours_ago

    await m._handle_update({"pv": pv, "value": 100.0})  # flicker to medium...
    await m._handle_update({"pv": pv, "value": 150.0})  # ...and straight back
    assert m.state.beams["TS1"].since == ten_hours_ago
    await asyncio.sleep(SETTLE)  # flicker dropped

    beam_channel.broadcast.reset_mock()
    await m._handle_update({"pv": pv, "value": 0.0})
    await asyncio.sleep(SETTLE)
    card = beam_channel.broadcast.call_args[0][0]
    assert dict(card.facts)["Was high for"].startswith("10h")



@pytest.mark.asyncio
async def test_restored_run_is_not_reset_or_renotified_after_a_restart(mock_config, mock_channels):
    """After a restart (e.g. a TUI config save) PVWS re-sends the same title and
    TOTALUAMPS; neither a new-run card nor a second finishing card is sent."""
    _, exp_channel = mock_channels
    sink = MagicMock()
    m = make_monitor(mock_config, mock_channels, counts_target=100, sink=sink)
    started = datetime.now(timezone.utc) - timedelta(hours=3)
    m.restore_instruments({"PEARL": {
        "run_name": "Run 1", "run_started_at": started.isoformat(), "counts": 120.0, "end_notified": True,
    }, "MERLIN": {"run_name": "x", "run_started_at": started.isoformat()}})

    await m._handle_update({"pv": mock_config.run_name_pv, "b64byt": base64.b64encode(b"Run 1").decode()})
    await m._handle_update({"pv": PEARL_UAMPS, "value": 121.0})
    exp_channel.broadcast.assert_not_called()
    assert tracker(m).state.run_started_at == started

    # The run then changes: the card covers the whole run, restart included.
    await m._handle_update({"pv": mock_config.run_name_pv, "b64byt": base64.b64encode(b"Run 2").decode()})
    card = exp_channel.broadcast.call_args[0][0]
    assert dict(card.facts)["Duration"].startswith("3h")
    sink.update_run_progress.assert_called_with("PEARL", ANY, False)


@pytest.mark.asyncio
async def test_restore_ignores_entries_without_a_run(mock_config, mock_channels):
    m = make_monitor(mock_config, mock_channels)
    m.restore_instruments({"PEARL": {"run_name": "", "run_started_at": None, "counts": 5.0}})
    assert tracker(m).state.run_name == "" and tracker(m).state.current_counts == -1.0


@pytest.mark.asyncio
async def test_finishing_card_state_is_saved_to_the_sink(mock_config, mock_channels):
    sink = MagicMock()
    m = make_monitor(mock_config, mock_channels, counts_target=100, sink=sink)
    tracker(m).state.run_name = "Run 1"
    await m._handle_update({"pv": PEARL_UAMPS, "value": 110.0})
    sink.update_run_progress.assert_called_with("PEARL", None, True)
    await m._handle_update({"pv": PEARL_UAMPS, "value": 10.0})  # back below target - 25
    assert tracker(m).state.end_notified is False
    sink.update_run_progress.assert_called_with("PEARL", None, False)



@pytest.mark.asyncio
async def test_raised_threshold_after_restart_still_gets_its_finishing_card(mock_config, mock_channels):
    """The card was sent at 101 with a target of 100; the config save that
    raised the target to 120 restarted the daemon. Passing 120 must notify."""
    _, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels, counts_target=120)
    m.restore_instruments({"PEARL": {
        "run_name": "Run 1", "run_started_at": datetime.now(timezone.utc).isoformat(),
        "counts": 101.0, "end_notified": True,
    }})
    assert tracker(m).state.end_notified is False
    await m._handle_update({"pv": PEARL_UAMPS, "value": 121.0})
    assert exp_channel.broadcast.call_args[0][0].title == "PEARL: Run about to finish"


@pytest.mark.asyncio
async def test_new_run_is_saved_before_the_milestone_card(mock_config, mock_channels):
    """Every 25th completed run (with fun_mode on) gets a milestone card, sent
    after the new run has been saved."""
    fun_config = replace(mock_config, fun_mode=True)
    _, exp_channel = mock_channels
    sink = MagicMock()
    sink.record_run_completed.return_value = 25
    order = []
    sink.update_run_name.side_effect = lambda *a: order.append("saved")
    exp_channel.broadcast.side_effect = lambda n: order.append(n.title)
    m = make_monitor(fun_config, mock_channels, sink=sink)
    tracker(m).state.run_name = "Run 24"
    tracker(m).state.run_started_at = datetime.now(timezone.utc)
    await m._handle_update({"pv": fun_config.run_name_pv, "b64byt": base64.b64encode(b"Run 25").decode()})
    assert order == ["PEARL: New run started", "saved", "PEARL: 25 runs completed"]
    sink.record_run_completed.assert_called_once_with("PEARL", ANY)



@pytest.mark.asyncio
async def test_blank_decoded_run_title_is_ignored(mock_config, mock_channels):
    _, exp_channel = mock_channels
    m = make_monitor(mock_config, mock_channels)
    pv = mock_config.run_name_pv
    await m._handle_update({"pv": pv, "b64byt": base64.b64encode(b"Run 1").decode()})
    await m._handle_update({"pv": pv, "b64byt": base64.b64encode(b"\x00\x00\x00").decode()})
    assert tracker(m).state.run_name == "Run 1"
    exp_channel.broadcast.assert_not_called()
    await m._handle_update({"pv": pv, "b64byt": base64.b64encode(b"Run 2").decode()})
    assert exp_channel.broadcast.call_args[0][0].text == "Run 2"


@pytest.mark.asyncio
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
