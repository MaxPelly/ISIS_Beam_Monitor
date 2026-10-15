import asyncio
import base64
import contextlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from isis_monitor.beam import (
    BeamMonitor,
    _classify_ws_error,
    _PendingChange,
)
from isis_monitor.config import DEFAULT_BEAMS, AppConfig, InstrumentConfig
from isis_monitor.daemon_state import DaemonState
from isis_monitor.tests.helpers import FakePVWS, fake_channel, running, wait_until
from isis_monitor.tests.test_instlist import encode
from isis_monitor.tests.test_instrument import Live
from isis_monitor.tests.test_runs import RUNNING, T0

PEARL_UAMPS = "IN:PEARL:DAE:TOTALUAMPS"
PEARL_TITLE = "IN:PEARL:DAE:WDTITLE"
TS1_PV, TS2_PV, MUONS_PV = (b.pv for b in DEFAULT_BEAMS)
TS1, TS2 = DEFAULT_BEAMS[0].boundaries, DEFAULT_BEAMS[1].boundaries


def tracker(m):
    """The monitor's only instrument tracker."""
    (only,) = m.instruments.values()
    return only


DEBOUNCE_SECONDS = 0.05
SETTLE = DEBOUNCE_SECONDS * 3  # wait comfortably past the debounce window in tests


@pytest.fixture
def mock_config():
    return AppConfig(isis_websocket_url="wss://test", instruments=[InstrumentConfig("PEARL", 100.0, "TS1")])


@pytest.fixture
def mock_channels():
    return fake_channel("Beam"), fake_channel("Exp")


def make_monitor(mock_config, mock_channels, counts_target=100, sink=None):
    beam_channel, exp_channel = mock_channels
    instruments = [replace(i, notify_counts=counts_target) for i in mock_config.instruments]
    return BeamMonitor(
        replace(mock_config, instruments=instruments, debounce_seconds=DEBOUNCE_SECONDS),
        beam_channel, exp_channel, sink=sink,
    )


# ---------------------------------------------------------------------------
# _parse_current, _get_power_label
# ---------------------------------------------------------------------------

def test_parse_current_and_power_labels(mock_config, mock_channels):
    m = make_monitor(mock_config, mock_channels)
    assert m._parse_current("123.4") == 123.4
    assert m._parse_current(0) == 0.0
    for junk in ("NaN", "nan", "inf", "bad_string", None):
        assert m._parse_current(junk) is None

    assert m._get_power_label(-5, TS1) == "off"
    assert m._get_power_label(0, TS1) == "off"
    assert m._get_power_label(20, TS1) == "low"
    assert m._get_power_label(75, TS1) == "medium"
    assert m._get_power_label(150, TS1) == "high"
    assert m._get_power_label(0, TS2) == "off"
    assert m._get_power_label(5, TS2) == "low"
    assert m._get_power_label(15, TS2) == "medium"
    assert m._get_power_label(30, TS2) == "high"


@pytest.mark.parametrize("junk", ["NaN", None, "bad_string"])
async def test_unreadable_beam_current_is_unknown_not_off(mock_config, mock_channels, junk):
    """E.g. PVWS's NaN before a PV has a value: no "beam off" card or sample.
    Unknown isn't sampled into the history, so a dead PV doesn't count as uptime."""
    beam_channel, _ = mock_channels
    sink = MagicMock() if junk == "NaN" else None  # with a sink and without
    m = make_monitor(mock_config, mock_channels, sink=sink)
    await m._handle_update({"pv": TS1_PV, "value": junk})
    beam_channel.broadcast.assert_not_called()
    assert m.beams["TS1"].power == ""
    await m._handle_update({"pv": TS1_PV, "value": "60.0"})
    await m._handle_update({"pv": TS1_PV, "value": junk})
    assert (m.beams["TS1"].power, m.beams["TS1"].current) == ("medium", 60.0)
    if sink:
        sink.update_beam_state.assert_called_with("TS1", 60.0, "unknown")


# ---------------------------------------------------------------------------
# _handle_update — beam-current arm
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("values, power, cancel, sent", [
    ([45.0], "low", False, None),  # no change of power: no card
    # A confirmed change is sent only after the debounce window.
    ([60.0], "medium", False, "low → medium"),
    # A change that reverts before the debounce window elapses is dropped entirely.
    ([60.0, 10.0], "low", False, None),
    # cancel_all() (called on shutdown) must stop pending timers from firing.
    ([60.0], "medium", True, None),
])
async def test_beam_changes_are_debounced(mock_config, mock_channels, values, power, cancel, sent):
    beam_channel, _ = mock_channels
    m = make_monitor(mock_config, mock_channels)

    # The first reading for a target is a startup card, sent with no debounce.
    await m._handle_update({"pv": TS1_PV, "value": "10.0"})
    assert (m.beams["TS1"].current, m.beams["TS1"].power) == (10.0, "low")
    (startup,) = [c.args[0] for c in beam_channel.broadcast.call_args_list]
    assert "online" in startup.title and startup.title.endswith("TS1 is low") and startup.beam == "TS1"
    beam_channel.broadcast.reset_mock()

    for value in values:
        await m._handle_update({"pv": TS1_PV, "value": str(value)})
    # The state updates immediately, but no card yet.
    assert (m.beams["TS1"].current, m.beams["TS1"].power) == (values[-1], power)
    beam_channel.broadcast.assert_not_called()
    if cancel:
        m.change_aggregator.cancel_all()
        await m.change_aggregator._flush("TS1")  # nothing pending: nothing to send
    await asyncio.sleep(SETTLE)
    if cancel:
        assert m.change_aggregator._pending == {}  # no timer added work back

    if sent is None:
        beam_channel.broadcast.assert_not_called()
    else:
        (card,) = [c.args[0] for c in beam_channel.broadcast.call_args_list]
        assert sent in card.title and card.beam == "TS1"


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
# _handle_update — an instrument's PVs (runs and their cards: test_runs.py,
# test_instrument.py)
# ---------------------------------------------------------------------------

def title(name, pv=PEARL_TITLE):
    """A PVWS run-title update; the title arrives base64-encoded."""
    raw = name if isinstance(name, bytes) else name.encode()
    return {"pv": pv, "b64byt": base64.b64encode(raw).decode()}


@pytest.mark.parametrize("updates, field, expected", [
    # No terminal escape sequences into `status`, the log or cards
    ([title(b"\x1b]0;pwn\x07Run\x1b[2J 1\x00\x00")], "title", "]0;pwn Run [2J 1"),
    ([title(b"\x00\x00\x00")], "title", "Run 0"),  # blank: ignored
    ([{"pv": PEARL_TITLE, "b64byt": "!!!not base64"}], "title", "Run 0"),
    ([{"pv": PEARL_TITLE, "b64byt": "nan"}], "title", "Run 0"),  # disconnected
    # Unreadable counts leave them unchanged, no crash.
    ([{"pv": PEARL_UAMPS, "value": "bad_format"}, {"pv": PEARL_UAMPS, "value": float("inf")},
      {"pv": PEARL_UAMPS, "text": "50/90"}], "counts", None),  # old dashboard format: not routed
    ([{"pv": PEARL_UAMPS, "value": 0}], "counts", 0.0),  # 0 at a run's start is a real reading
])
async def test_run_titles_and_counts_are_decoded_and_cleaned(mock_config, mock_channels, updates, field, expected):
    m = make_monitor(mock_config, mock_channels)
    await m._handle_update(title("Run 0"))
    for update in updates:
        await m._handle_update(update)
    assert getattr(tracker(m).runs, field) == expected


# ---------------------------------------------------------------------------
# _run_loop() against a real local WebSocket server
# ---------------------------------------------------------------------------

def two_instrument_monitor(mock_config, mock_channels, sink=None):
    config = replace(mock_config, instruments=[
        InstrumentConfig("PEARL", 100.0, "TS1"),
        InstrumentConfig("WISH", 50.0, "TS2"),
    ])
    return BeamMonitor(replace(config, debounce_seconds=DEBOUNCE_SECONDS), *mock_channels, sink=sink)


def ws_monitor(mock_config, mock_channels, url, sink=None, reconnect_interval=60.0, **kw):
    config = replace(mock_config, isis_websocket_url=url, beam_reconnect_interval=reconnect_interval)
    return make_monitor(config, mock_channels, sink=sink, **kw)


async def test_run_loop_subscribes_and_dispatches_updates(mock_config, mock_channels):
    sink = MagicMock()
    update = {"type": "update", "pv": TS1_PV, "value": 150.0}
    async with FakePVWS([update]) as server:
        m = two_instrument_monitor(replace(mock_config, isis_websocket_url=server.url), mock_channels, sink=sink)
        async with running(m):
            await wait_until(lambda: sink.update_beam_state.called)

    pvs = server.subscriptions[0]["pvs"]
    assert pvs[:4] == ["CS:INSTLIST", TS1_PV, TS2_PV, MUONS_PV]
    # Each instrument's RB and run numbers first, then its title before its
    # counts, so a run changed while disconnected is started by its title.
    assert pvs[4:] == [f"IN:{name}:DAE:{pv}" for name in ("PEARL", "WISH")
                       for pv in ("_RBNUMBER", "RUNNUMBER", "WDTITLE", "TOTALUAMPS", "RUNSTATE_STR", "STARTTIME")]
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
        m._force_reconnect.set()  # as if r was pressed mid-handshake
        async with running(m):
            await asyncio.wait_for(server.connected.wait(), 2)
            await wait_until(lambda: m._current_ws is not None)
            # The new connection satisfies a request made while it was being set
            # up, so this one isn't ignored as "already pending".
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


async def test_without_a_pvws_url_run_returns_and_reconnect_says_so(mock_config, mock_channels, caplog):
    m = ws_monitor(mock_config, mock_channels, "")
    assert m.request_reconnect() is None
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

    from websockets.exceptions import (
        ConnectionClosedError,
        InvalidMessage,
        InvalidURI,
        SecurityError,
    )

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
# Instruments: routing, the run tracker on the event loop, stall checks
# ---------------------------------------------------------------------------

async def under_way(tracker):
    """`tracker` mid-run 10 (as test_runs.RUNNING) from T0, at 40 µA·h from T0 + 1 minute."""
    r = (await Live(tracker=tracker)(*RUNNING)).settle()
    await r((60, "TOTALUAMPS", 40.0))
    assert tracker.runs.run.last_counts == 40.0


async def test_a_run_begun_over_pvws_gets_its_new_run_card(mock_config, mock_channels):
    """The run tracker's settle timer runs on the event loop."""
    _, exp_channel = mock_channels
    updates = [{"pv": "IN:PEARL:DAE:RUNNUMBER", "text": "00010"}, {"pv": "IN:PEARL:DAE:RUNSTATE_STR", "text": "SETUP"},
               title("Run ten"), {"pv": "IN:PEARL:DAE:RUNSTATE_STR", "text": "RUNNING"}]
    with patch("isis_monitor.runs.SETTLE", timedelta(milliseconds=10)):
        async with FakePVWS([{"type": "update", **u} for u in updates]) as server:
            m = ws_monitor(mock_config, mock_channels, server.url)
            async with running(m):
                await wait_until(lambda: exp_channel.broadcast.called)
    card = exp_channel.broadcast.call_args[0][0]
    assert (card.title, card.text) == ("PEARL: New run started", "Run ten")
    assert m.instrument_views()["PEARL"]["run_number"] == "10"


async def test_updates_are_routed_to_their_own_instrument(mock_config, mock_channels):
    _, exp_channel = mock_channels
    m = two_instrument_monitor(mock_config, mock_channels)
    pearl, wish = m.instruments["PEARL"], m.instruments["WISH"]

    await m._handle_update(title("Wish run", "IN:WISH:DAE:WDTITLE"))
    await m._handle_update({"pv": "IN:WISH:DAE:TOTALUAMPS", "value": 60.0})

    assert (wish.runs.title, wish.runs.counts) == ("Wish run", 60.0)
    assert (pearl.runs.title, pearl.runs.counts) == ("", None)
    exp_channel.broadcast.assert_not_called()


@pytest.mark.parametrize("pv", ["CS:INSTLIST", TS1_PV, PEARL_UAMPS, PEARL_TITLE, "IN:PEARL:DAE:RUNSTATE_STR",
                                "IN:PEARL:DAE:RUNNUMBER", "IN:PEARL:DAE:_RBNUMBER"])
async def test_severity_only_updates_are_ignored(mock_config, mock_channels, pv):
    """PVWS sends updates with only a new severity; they mustn't read as a
    missing value (beam "unknown", a counts warning or run state "NONE")."""
    sink = MagicMock()
    m = make_monitor(mock_config, mock_channels, sink=sink)
    sink.reset_mock()
    before = (vars(tracker(m).runs).copy(), vars(m.beams["TS1"]).copy())
    await m._handle_update({"type": "update", "pv": pv, "severity": "MAJOR"})
    assert (vars(tracker(m).runs), vars(m.beams["TS1"])) == before
    assert not sink.method_calls


async def test_stall_check_uses_each_instruments_beam_target(mock_config, mock_channels):
    """WISH is on TS2, which is off, so only PEARL (on TS1, high) warns."""
    _, exp_channel = mock_channels
    m = two_instrument_monitor(replace(mock_config, stall_minutes=1), mock_channels)
    m._current_ws = MagicMock()  # connected to PVWS
    m.beams["TS1"].power = "high"
    m.beams["TS2"].power = "off"
    for t in m.instruments.values():
        await under_way(t)

    await m._check_collection_progress(T0 + timedelta(minutes=7))
    await m._check_collection_progress(T0 + timedelta(minutes=8))

    assert m.instruments["PEARL"].stall_warned is True
    assert m.instruments["WISH"].stall_warned is False
    assert exp_channel.broadcast.call_args[0][0].title == "PEARL: Data collection stalled"


async def test_one_instruments_failed_stall_check_does_not_skip_others(mock_config, mock_channels, caplog):
    m = two_instrument_monitor(mock_config, mock_channels)
    m._current_ws = MagicMock()  # connected to PVWS
    m.instruments["PEARL"].check_collection_progress = AsyncMock(side_effect=RuntimeError("boom"))
    m.instruments["WISH"].check_collection_progress = AsyncMock()

    await m._check_collection_progress(datetime.now(timezone.utc))

    m.instruments["WISH"].check_collection_progress.assert_awaited_once()
    assert "Collection check failed for PEARL" in caplog.text


async def test_no_stall_warning_while_pvws_is_disconnected(mock_config, mock_channels):
    """Counts are frozen and beam states stale during an outage."""
    _, exp_channel = mock_channels
    m = make_monitor(replace(mock_config, stall_minutes=1), mock_channels)
    m.beams["TS1"].power = "high"  # as last known before the outage
    await under_way(tracker(m))
    m._current_ws = MagicMock()
    await m._check_collection_progress(T0 + timedelta(minutes=7))
    assert tracker(m).stalled_since is not None

    m._current_ws = None  # PVWS disconnected
    await m._check_collection_progress(T0 + timedelta(minutes=8))
    await m._check_collection_progress(T0 + timedelta(minutes=9))
    exp_channel.broadcast.assert_not_called()
    assert tracker(m).stalled_since is None  # restarts once reconnected


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
    tracker(m).runs.rb_resync = False

    await m._handle_update(instlist(("PEARL", "IN:PEARL_5B:", "MUON")))
    assert tracker(m).runs.rb_resync  # an RB number from the new PVs wasn't watched changing

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
    assert tracker(m).runs.counts == 12.0

    m._current_ws.send.reset_mock()
    await m._handle_update(instlist(("PEARL", "IN:PEARL_5B:", "MUON")))  # re-sent on reconnect
    m._current_ws.send.assert_not_awaited()


@pytest.mark.parametrize("instrument, lists, target, warned, not_logged", [
    # Not on a target station: [PVS] instrument_target, with one warning.
    pytest.param({}, [[("PEARL", "IN:PEARL:", "MISC")]] * 2, "TS2",
                 "PEARL: CS:INSTLIST doesn't put it on TS1, TS2 or MUON", None, id="not-on-a-station"),
    pytest.param({"beam_target": "TS1"}, [[("PEARL", "IN:PEARL:", "MUON")]], "TS1", None, "CS:INSTLIST",
                 id="configured-wins"),
    # Unlisted: keeps its PVs, with one warning, and not Muons from the older list.
    pytest.param({}, [[("PEARL", "IN:PEARL:", "MUON")]] + [[("WISH", "IN:WISH:", "TS2")]] * 2, "TS2",
                 "PEARL isn't in CS:INSTLIST", "MUON", id="unlisted"),
])
async def test_instlist_beam_target_fallbacks(mock_config, mock_channels, caplog, instrument, lists, target, warned,
                                              not_logged):
    m = listed_monitor(mock_config, mock_channels, **instrument)
    for entries in lists:
        await m._handle_update(instlist(*entries))
    assert tracker(m).beam_target == target and PEARL_UAMPS in m.routes
    if warned:
        assert caplog.text.count(warned) == 1
    if not_logged:
        assert not_logged not in caplog.text


async def test_a_new_instlist_is_saved_and_an_unreadable_one_changes_nothing(mock_config, mock_channels):
    state = DaemonState(instrument_names=["PEARL"])
    m = listed_monitor(mock_config, mock_channels, sink=state)
    await m._handle_update(instlist(("PEARL", "IN:PEARL:", "TS1")))
    restored = DaemonState()
    restored.restore_from_snapshot_json(json.dumps(state.snapshot()))
    assert restored.instlist == {"PEARL": ("IN:PEARL:", "TS1")}  # for the next start

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
