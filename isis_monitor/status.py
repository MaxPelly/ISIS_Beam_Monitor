"""The status the daemon publishes to the push site's status page.

build() is a pure function of the daemon's state; see the push site's
status.py for the payload's layout (version 1). Beams and instruments with
no live reading are sent as unknown, never as zero, so a feed outage doesn't
look like the beam going off.
"""
import asyncio
import contextlib
import logging
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Optional

import aiohttp

from isis_monitor import __version__
from isis_monitor.config import BEAM_NAMES
from isis_monitor.daemon_state import MCR_NEWS_PLACEHOLDER, DaemonState
from isis_monitor.notifiers import signed_json

logger = logging.getLogger(__name__)

HISTORY = timedelta(hours=24)
HISTORY_POINTS = 144  # 10-minute bins
# The push site's limits (its status.py); anything longer would make it refuse the whole status.
SHORT, LABEL, MAX_TITLE, MAX_NEWS = 16, 32, 200, 1000


def _text(value: object, limit: int) -> str:
    """Text the site will take: at most `limit` characters, and valid UTF-8
    (PVWS can deliver lone surrogates, which become "?")."""
    return str(value).encode("utf-8", "replace").decode("utf-8")[:limit]


def _number(value: object) -> Optional[float]:
    return round(value, 2) if isinstance(value, (int, float)) and math.isfinite(value) else None


def _iso(ts: Optional[datetime]) -> Optional[str]:
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds") if ts else None


def _history(samples, now: datetime) -> dict:
    """The last 24h of (time, current, power) samples (oldest first), as each
    10-minute bin's mean current and its most common power; [None, "unknown"]
    where there are none. Bins are on fixed 10-minute boundaries, the last one
    still filling, so they don't shift from one status to the next."""
    width = HISTORY / HISTORY_POINTS
    end = datetime.fromtimestamp((now.timestamp() // width.total_seconds() + 1) * width.total_seconds(), timezone.utc)
    start = end - HISTORY
    bins = [[] for _ in range(HISTORY_POINTS)]
    for ts, current, power in reversed(samples):  # newest first, so it can stop at 24h
        if ts < start:
            break
        bins[min(int((ts - start) / width), HISTORY_POINTS - 1)].append((current, _text(power, SHORT)))
    values = [
        [_number(sum(c for c, _ in b) / len(b)), Counter(p for _, p in b).most_common(1)[0][0]] if b
        else [None, "unknown"]
        for b in bins
    ]
    return {"interval_s": width.total_seconds(), "values": values}


def _instrument(name: str, view: dict) -> dict:
    run_state = view.get("run_state", "")
    counts = view.get("counts")
    started = view.get("run_started_at")
    return {
        "name": _text(name, LABEL),
        "beam": _text(view.get("beam_target", ""), SHORT),
        "run_number": _text(view.get("run_number", ""), LABEL),
        "run_state": _text(run_state, LABEL),
        "title": _text(view.get("run_name", ""), MAX_TITLE),
        "started_at": _iso(datetime.fromisoformat(started)) if started else None,
        # Unknown while the DAE's state is (e.g. its IOC or the gateway is down), not the last value.
        "counts": _number(counts) if run_state and counts is not None and counts >= 0 else None,
        "target": _number(view.get("target")),
        "auto": bool(view.get("auto")),
        "rb": _text(view.get("rb_number", ""), LABEL),
    }


def build(state: DaemonState, since: Dict[str, Optional[datetime]], interval_s: float,
          now: Optional[datetime] = None) -> dict:
    """The status now. `since` is when each beam's power last changed."""
    now = now or datetime.now(timezone.utc)
    beams = []
    for name in BEAM_NAMES:
        beam = state.beam_states[name]
        known = beam["power"] != "unknown"
        beams.append({
            "name": name,
            "current": _number(float(beam["current"])) if known else None,
            "power": _text(beam["power"], SHORT),
            "since": _iso(since.get(name)) if known else None,
            "history": _history(state.history[name], now),
        })
    news = "" if state.mcr_news == MCR_NEWS_PLACEHOLDER else state.mcr_news
    return {
        # To the millisecond: the site only keeps a status newer than the last.
        "v": 1, "kind": "status", "generated_at": now.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
        "interval_s": interval_s,
        "monitor_version": _text(__version__, LABEL),
        "health": {_text(k, SHORT): _text(v, SHORT) for k, v in state.health.items()},
        "beams": beams,
        "instruments": [_instrument(name, view) for name, view in state.instruments().items()],
        "mcr": {"news": _text(news, MAX_NEWS), "seen_at": _iso(state.mcr_news_at)},
    }


class StatusPublisher:
    """POSTs the status to the push site (`url` + "/status", e.g.
    .../ingest/status) every `interval` seconds, and soon after a
    significant change (poke()), but at most once per DEBOUNCE. Nothing
    queues: each send builds the latest status, and a failed one is only
    logged, since the next replaces it."""
    DEBOUNCE = 5.0

    def __init__(self, url: str, secret: bytes, build: Callable[[], dict], interval: float,
                 session: aiohttp.ClientSession, timeout: float = 2.0):
        self.url = url.rstrip("/") + "/status"
        self.secret, self.build, self.interval = secret, build, interval
        self.session, self.timeout = session, timeout
        self._changed = asyncio.Event()

    def poke(self) -> None:
        self._changed.set()

    async def run(self) -> None:
        while True:
            await self._send()
            await asyncio.sleep(self.DEBOUNCE)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), max(self.interval - self.DEBOUNCE, 0))
            self._changed.clear()

    async def _send(self) -> None:
        """One status; any failure is only logged, and never stops the publisher."""
        try:
            async with self.session.post(
                self.url, timeout=aiohttp.ClientTimeout(total=self.timeout), allow_redirects=False,
                **signed_json(self.build(), self.secret),
            ) as resp:
                if resp.status != 204:  # e.g. 409: not newer than the last, after a clock step
                    logger.debug(f"Push site didn't take the status: HTTP {resp.status}")
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.debug(f"Failed to send the status: {exc!r}")
        except Exception:  # a bug: logged, and the next send tries again
            logger.exception("Failed to send the status")
