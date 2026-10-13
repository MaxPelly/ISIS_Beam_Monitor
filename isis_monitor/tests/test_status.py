import json
from datetime import datetime, timedelta, timezone

from isis_monitor.config import InstrumentConfig
from isis_monitor.daemon_state import DaemonState
from isis_monitor.instrument import InstrumentTracker
from isis_monitor.status import HISTORY_POINTS, MAX_TITLE, build
from isis_monitor.tests.helpers import fake_channel

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def state_with(*instruments):
    state = DaemonState(instrument_names=[i.instrument.name for i in instruments])
    state.instruments = lambda: {i.instrument.name: i.view() for i in instruments}
    return state


def tracker(name="PEARL", **config):
    return InstrumentTracker(InstrumentConfig(name, 130.0, "TS1", **config), fake_channel(), lambda b: "high", 15)


def test_beams_report_live_values_and_unknown_ones_as_unknown():
    state = state_with()
    state.update_beam_state("TS1", 152.34, "high")
    status = build(state, {"TS1": NOW - timedelta(hours=1)}, 60, NOW)
    ts1, ts2 = status["beams"][:2]
    assert (ts1["current"], ts1["power"], ts1["since"]) == (152.34, "high", "2026-10-13T11:00:00+00:00")
    assert (ts2["current"], ts2["power"], ts2["since"]) == (None, "unknown", None)  # not 0: no reading yet
    assert [b["name"] for b in status["beams"]] == ["TS1", "TS2", "Muons"]


def test_history_is_24h_in_10_minute_bins():
    state = state_with()
    for minutes, current, power in ((5, 100.0, "high"), (8, 50.0, "high"), (9, 0.0, "off"),
                                    (24 * 60 + 5, 1.0, "low")):  # the last is too old
        state.history["TS1"].append((NOW - timedelta(minutes=minutes), current, power))
    values = build(state, {}, 60, NOW)["beams"][0]["history"]["values"]
    assert len(values) == HISTORY_POINTS
    assert values[-1] == [50.0, "high"]  # mean, and the most common power
    assert values[-2] == [None, "unknown"] and values[0] == [None, "unknown"]


def test_instruments_come_from_the_trackers():
    pearl = tracker(auto_counts=True)
    pearl.state.run_name, pearl.state.current_counts, pearl.state.run_number = "x" * 300, 65.256, "127921"
    pearl.state.run_state, pearl.state.rb_number = "RUNNING", "2610403"
    wish = tracker("WISH")
    (p, w) = build(state_with(pearl, wish), {}, 60, NOW)["instruments"]
    assert p == {"name": "PEARL", "beam": "TS1", "run_number": "127921", "run_state": "RUNNING",
                 "title": "x" * MAX_TITLE, "started_at": None, "counts": 65.26, "target": 130.0, "auto": True,
                 "rb": "2610403"}
    assert w["counts"] is None  # nothing read yet


def test_mcr_news_and_health():
    state = state_with()
    assert build(state, {}, 60, NOW)["mcr"] == {"news": "", "seen_at": None}  # not the placeholder
    state.update_mcr_news("Beam on.")
    state.update_health("beam", "connected")
    status = build(state, {}, 60, NOW)
    assert status["mcr"]["news"] == "Beam on." and status["mcr"]["seen_at"]
    assert status["health"]["beam"] == "connected"
    assert (status["v"], status["kind"], status["interval_s"]) == (1, "status", 60)


def test_a_long_status_stays_well_under_the_push_sites_limit():
    """20 instruments with the longest titles and news; the site takes up to 64 KB."""
    trackers = [tracker(f"INSTRUMENT{n:02d}") for n in range(20)]
    for t in trackers:
        t.state.run_name = "é" * 300
    state = state_with(*trackers)
    state.update_mcr_news("é" * 1000)
    for name in state.history:
        for minutes in range(0, 24 * 60, 1):
            state.history[name].append((NOW - timedelta(minutes=minutes), 123.456, "medium"))
    assert len(json.dumps(build(state, {}, 60, NOW), ensure_ascii=False).encode()) < 48 * 1024


# ---------------------------------------------------------------------------
# StatusPublisher
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
import contextlib  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

import aiohttp  # noqa: E402
import pytest  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from isis_monitor.status import StatusPublisher  # noqa: E402
from isis_monitor.tests.helpers import wait_until  # noqa: E402

SECRET = b"s" * 32


@contextlib.asynccontextmanager
async def receiver(status=204):
    """A push site stand-in recording each verified status it's sent."""
    received = []

    async def ingest_status(request):
        body = await request.read()
        signed = request.headers["X-Timestamp"].encode() + b"." + body
        assert request.headers["X-Signature"] == hmac.new(SECRET, signed, hashlib.sha256).hexdigest()
        received.append(json.loads(body))
        return web.Response(status=status)
    app = web.Application()
    app.router.add_post("/ingest/status", ingest_status)
    async with TestServer(app) as server:
        server.received = received
        yield server


@contextlib.asynccontextmanager
async def publishing(url, interval=3600.0, build=lambda: {"n": 1}, debounce=0.05):
    async with aiohttp.ClientSession() as session:
        publisher = StatusPublisher(url, SECRET, build, interval, session, timeout=1)
        with patch.object(StatusPublisher, "DEBOUNCE", debounce):
            task = asyncio.create_task(publisher.run())
            try:
                yield publisher
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


async def test_the_status_is_sent_signed_at_start_and_every_interval():
    async with receiver() as server:
        async with publishing(str(server.make_url("/ingest")), interval=0.1):
            await wait_until(lambda: len(server.received) >= 3)
    assert server.received[0] == {"n": 1}


async def test_a_change_sends_soon_but_at_most_once_per_debounce():
    async with receiver() as server:
        async with publishing(str(server.make_url("/ingest/")), debounce=0.2) as publisher:
            await wait_until(lambda: len(server.received) == 1)  # at start
            for _ in range(5):
                publisher.poke()
            await wait_until(lambda: len(server.received) == 2)
            await asyncio.sleep(0.3)
            assert len(server.received) == 2  # the pokes were one send


async def test_a_receiver_that_is_down_or_refuses_doesnt_stop_publishing(caplog):
    caplog.set_level("DEBUG")
    async with publishing("http://127.0.0.1:9/ingest", interval=0.05):
        await wait_until(lambda: caplog.text.count("Failed to send the status") >= 2)
    async with receiver(status=404) as server:  # an older push site, without /ingest/status
        async with publishing(str(server.make_url("/ingest")), interval=0.05):
            await wait_until(lambda: len(server.received) >= 2)
    assert "didn't take the status: HTTP 404" in caplog.text


async def test_a_failing_build_is_logged_and_retried(caplog):
    build = MagicMock(side_effect=[RuntimeError("bug"), {"n": 2}])
    async with receiver() as server:
        async with publishing(str(server.make_url("/ingest")), interval=0.05, build=build):
            await wait_until(lambda: server.received)
    assert "Failed to build the status" in caplog.text and server.received[0] == {"n": 2}


def test_significant_changes_poke_the_publisher():
    state = DaemonState(instrument_names=["PEARL"])
    state.on_change = poke = MagicMock()
    state.update_beam_state("TS1", 150.0, "high")
    state.update_beam_state("TS1", 151.0, "high")  # the same power: no
    state.update_mcr_news("x")
    state.update_health("beam", "connected")
    state.update_health("beam", "connected")  # no change: no
    state.record_run_completed("PEARL", NOW, "1", 5.0)
    state.record_run_completed("MERLIN", NOW, "1", 5.0)  # not configured: no
    assert poke.call_count == 4


@pytest.mark.parametrize("url", ["http://127.0.0.1:8765/ingest", "http://127.0.0.1:8765/ingest/"])
def test_the_status_goes_to_the_ingest_url_plus_status(url):
    assert StatusPublisher(url, SECRET, dict, 60, MagicMock()).url == "http://127.0.0.1:8765/ingest/status"
