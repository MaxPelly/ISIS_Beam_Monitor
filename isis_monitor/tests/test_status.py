import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from isis_monitor.config import InstrumentConfig
from isis_monitor.daemon_state import DaemonState
from isis_monitor.instrument import InstrumentTracker
from isis_monitor.status import (
    HISTORY_POINTS,
    MAX_INSTRUMENTS,
    MAX_TITLE,
    build,
    render_text,
)
from isis_monitor.tests.helpers import fake_channel

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def state_with(*instruments):
    state = DaemonState(instrument_names=[i.instrument.name for i in instruments])
    state.instruments = lambda: {i.instrument.name: i.view() for i in instruments}
    return state


def tracker(name="PEARL", **config):
    return InstrumentTracker(InstrumentConfig(name, 130.0, "TS1", **config), fake_channel(), lambda b: "high", 15)


def test_beams_news_and_health():
    state = state_with()
    assert build(state, {}, 60, NOW)["mcr"] == {"news": "", "seen_at": None}  # not the placeholder
    state.update_beam_state("TS1", 152.34, "high")
    state.update_mcr_news("Beam on.")
    state.update_health("beam", "connected")
    status = build(state, {"TS1": NOW - timedelta(hours=1)}, 60, NOW)
    ts1, ts2 = status["beams"][:2]
    assert (ts1["current"], ts1["power"], ts1["since"]) == (152.34, "high", "2026-10-13T11:00:00+00:00")
    assert (ts2["current"], ts2["power"], ts2["since"]) == (None, "unknown", None)  # not 0: no reading yet
    assert [b["name"] for b in status["beams"]] == ["TS1", "TS2", "Muons"]
    assert status["mcr"]["news"] == "Beam on." and status["mcr"]["seen_at"]
    assert status["health"]["beam"] == "connected"
    assert (status["v"], status["kind"], status["interval_s"]) == (1, "status", 60)


def test_history_is_24h_in_10_minute_bins_on_fixed_boundaries():
    state = state_with()
    now = NOW + timedelta(minutes=3)  # 12:03, so the last bin is 12:00-12:10, still filling
    for at, current, power in ((-24 * 60, 1.0, "low"),  # too old
                               (-9, 0.0, "off"), (-8, 50.0, "high"), (-5, 100.0, "high"),  # 11:51-11:55
                               (1, 20.0, "low"), (3.01, 21.0, "low")):  # 12:01, and just after now (jitter)
        state.history["TS1"].append((NOW + timedelta(minutes=at), current, power))
    values = build(state, {}, 60, now)["beams"][0]["history"]["values"]
    assert len(values) == HISTORY_POINTS
    assert values[-1] == [20.5, "low"]
    assert values[-2] == [50.0, "high"]  # mean, and the most common power
    assert values[-3] == [None, "unknown"] and values[0] == [None, "unknown"]
    assert build(state, {}, 60, now + timedelta(minutes=5))["beams"][0]["history"]["values"] == values


def test_instruments_come_from_the_trackers():
    pearl = tracker(auto_counts=True)
    pearl.state.run_name, pearl.state.current_counts, pearl.state.rb_number = "x" * 300, 65.256, "2610403"
    pearl.runs.run_number, pearl.runs.run_state = "00127921", "RUNNING"  # as the PV pads it
    wish = tracker("WISH")
    # E.g. MERLIN's IOC or the gateway is down: not the last counts as if live.
    merlin = tracker("MERLIN")
    merlin.state.current_counts = 50.0
    merlin.runs.run_state = ""
    (p, w, m) = build(state_with(pearl, wish, merlin), {}, 60, NOW)["instruments"]
    assert p == {"name": "PEARL", "beam": "TS1", "run_number": "127921", "run_state": "RUNNING",
                 "title": "x" * MAX_TITLE, "started_at": None, "counts": 65.26, "target": 130.0, "auto": True,
                 "rb": "2610403"}
    assert w["counts"] is None  # nothing read yet
    assert m["counts"] is None  # while its run state is unknown


def test_a_long_status_stays_within_the_push_sites_limits():
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

    # Instruments past the site's limit are left out.
    trackers = [tracker(f"I{n}") for n in range(MAX_INSTRUMENTS + 1)]
    assert len(build(state_with(*trackers), {}, 60, NOW)["instruments"]) == MAX_INSTRUMENTS


# ---------------------------------------------------------------------------
# StatusPublisher
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
import contextlib  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import itertools  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

import aiohttp  # noqa: E402
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
async def publishing(url, interval=3600.0, build=lambda: {"n": 1}, debounce=0.02):
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
    """To <ingest URL>/status (the receiver serves only that), as is the next test's with a trailing slash."""
    async with receiver() as server:
        async with publishing(str(server.make_url("/ingest")), interval=0.02):
            await wait_until(lambda: len(server.received) >= 3)
    assert server.received[0] == {"n": 1}


async def test_a_change_sends_soon_but_at_most_once_per_debounce():
    async with receiver() as server:
        async with publishing(str(server.make_url("/ingest/")), debounce=0.02) as publisher:
            await wait_until(lambda: len(server.received) == 1)  # at start
            for _ in range(5):
                publisher.poke()
            await wait_until(lambda: len(server.received) == 2)
            await asyncio.sleep(0.05)
            assert len(server.received) == 2  # the pokes were one send


async def test_a_receiver_that_is_down_or_refuses_doesnt_stop_publishing(caplog):
    caplog.set_level("DEBUG")
    async with publishing("http://127.0.0.1:9/ingest", interval=0.02):
        await wait_until(lambda: caplog.text.count("Failed to send the status") >= 2)
    async with receiver(status=404) as server:  # an older push site, without /ingest/status
        async with publishing(str(server.make_url("/ingest")), interval=0.02):
            await wait_until(lambda: len(server.received) >= 2)
    assert caplog.text.count("Push site refuses the status: HTTP 404") == 1  # once, not every send
    assert caplog.text.count("Can't reach the push site at http://127.0.0.1:9/ingest/status") == 1


def test_refusals_are_warned_of_once_and_recovery_noted(caplog):
    caplog.set_level("INFO")
    publisher = StatusPublisher("http://x/ingest", SECRET, dict, 60, MagicMock())
    assert publisher.url == StatusPublisher("http://x/ingest/", SECRET, dict, 60, MagicMock()).url == "http://x/ingest/status"
    for status in (204, 400, 400, 409, 204, 409):
        publisher._took(status)
    assert caplog.text.count("refuses the status: HTTP 400") == 1
    assert caplog.text.count("takes the status again") == 1


async def test_a_failing_build_or_unencodable_status_is_logged_and_retried(caplog):
    # (A lone surrogate, e.g. from a bug, can't be encoded.)
    build = MagicMock(side_effect=itertools.chain([RuntimeError("bug"), {"t": "\ud800"}], itertools.repeat({"n": 2})))
    async with receiver() as server:
        async with publishing(str(server.make_url("/ingest")), interval=0.02, build=build):
            await wait_until(lambda: server.received)
    assert caplog.text.count("Failed to send the status") == 2 and server.received[0] == {"n": 2}


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

    # An instrument's run state and number, on a change.
    sink = MagicMock(spec=DaemonState())
    pearl = InstrumentTracker(InstrumentConfig("PEARL", 130.0, "TS1"), fake_channel(), lambda b: "high", 15, sink=sink)
    for text in ("RUNNING", "RUNNING", "ENDING"):
        pearl.handle_run_state(text)
    for text in ("00010", "10", "00011"):
        pearl.handle_run_number(text)
    assert sink.on_change.call_count == 4


def test_every_string_fits_the_push_sites_limits():
    """A longer one would make the site refuse the whole status."""
    long = "x" * 2000
    pearl = tracker("P" * 40)
    pearl.state.run_name, pearl.state.rb_number, pearl.beam_target = long + "\ud800", long, long
    pearl.runs.run_number, pearl.runs.run_state = long, long
    state = state_with(pearl)
    state.mcr_news = long
    status = build(state, {}, 60, NOW)
    (inst,) = status["instruments"]
    assert (len(inst["name"]), len(inst["beam"]), len(inst["run_number"]), len(inst["run_state"]),
            len(inst["rb"]), len(inst["title"]), len(status["mcr"]["news"])) == (32, 16, 32, 32, 32, 200, 1000)
    json.dumps(status, ensure_ascii=False).encode()  # no lone surrogates


def test_render_text_shows_beams_instruments_and_news_in_local_time():
    state = state_with(tracker(auto_counts=True), tracker("WISH", channel="instrument"))
    pearl = state.instruments()["PEARL"]
    state.instruments = lambda: {"PEARL": {**pearl, "run_name": "y" * 50, "run_number": "127921",
                                           "run_state": "RUNNING", "counts": 65.2},
                                 "WISH": {"beam_target": "TS1"}}
    state.update_beam_state("TS1", 152.34, "high")
    state.update_mcr_news("Beam on")
    for minutes, current in ((-60, 100.0), (-50, 200.0)):  # two of the last 48 half-hours; the rest are gaps
        state.history["TS1"].append((NOW + timedelta(minutes=minutes), current, "high"))
    text = render_text(build(state, {"TS1": NOW - timedelta(hours=1)}, 60, NOW))
    lines = text.splitlines()
    assert lines[0].startswith("ISIS Beam Monitor v") and "at Tue 13 Oct 13:00:00 (daemon starting" in lines[0]
    assert lines[2].startswith("TS1     152.3 µA  high    since 12:00  ··") and lines[2].endswith("·▁█·")  # the last half-hour has none yet
    assert lines[3] == "TS2         ? µA  unknown"  # no data for 24h
    state.history["Muons"].append((NOW - timedelta(minutes=5), 0.0, "off"))
    assert render_text(build(state, {}, 60, NOW)).splitlines()[4].endswith("··_")  # off isn't a gap
    assert lines[7].split()[:5] == ["PEARL", "TS1", "127921", "RUNNING", "65.2/≈130"] and lines[7].endswith("y" * 39 + "…")
    assert lines[8].split() == ["WISH", "TS1", "-", "?", "?"]
    assert lines[-2:] == [f"MCR news (since {state.mcr_news_at.astimezone(ZoneInfo('Europe/London')):%a %d %b %H:%M}):",
                          "Beam on"]


async def test_a_slow_site_isnt_reported_unreachable(caplog):
    """A timeout may pass; only a site that can't be connected to is warned of."""
    caplog.set_level("DEBUG")
    publisher = StatusPublisher("http://x/ingest", SECRET, dict, 60, MagicMock())
    publisher.session.post.side_effect = TimeoutError
    await publisher._send()
    assert "Failed to send the status" in caplog.text and "Can't reach" not in caplog.text
