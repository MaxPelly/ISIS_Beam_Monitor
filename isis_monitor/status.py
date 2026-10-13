"""The status the daemon publishes to the push site's status page.

build() is a pure function of the daemon's state; see the push site's
status.py for the payload's layout (version 1). Beams and instruments with
no live reading are sent as unknown, never as zero, so a feed outage doesn't
look like the beam going off.
"""
import asyncio
import contextlib
import logging
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
MAX_TITLE = 200


def _iso(ts: Optional[datetime]) -> Optional[str]:
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds") if ts else None


def _history(samples, now: datetime) -> dict:
    """The last 24h of (time, current, power) samples, as each bin's mean
    current and its most common power; [None, "unknown"] where there are none."""
    width = HISTORY / HISTORY_POINTS
    start = now - HISTORY
    bins = [[] for _ in range(HISTORY_POINTS)]
    for ts, current, power in samples:
        if start <= ts < now + width:  # just past now is the last bin's (timer jitter)
            bins[min(int((ts - start) / width), HISTORY_POINTS - 1)].append((current, power))
    values = [
        [round(sum(c for c, _ in b) / len(b), 2), Counter(p for _, p in b).most_common(1)[0][0]] if b
        else [None, "unknown"]
        for b in bins
    ]
    return {"interval_s": width.total_seconds(), "values": values}


def _instrument(name: str, view: dict) -> dict:
    counts, target = view.get("counts", -1.0), view.get("target")
    return {
        "name": name,
        "beam": view.get("beam_target", ""),
        "run_number": view.get("run_number", ""),
        "run_state": view.get("run_state", ""),
        "title": view.get("run_name", "")[:MAX_TITLE],
        "started_at": view.get("run_started_at"),
        "counts": round(counts, 2) if counts is not None and counts >= 0 else None,
        "target": target,
        "auto": bool(view.get("auto")),
        "rb": view.get("rb_number", ""),
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
            "current": round(float(beam["current"]), 2) if known else None,
            "power": str(beam["power"]),
            "since": _iso(since.get(name)) if known else None,
            "history": _history(state.history[name], now),
        })
    news = "" if state.mcr_news == MCR_NEWS_PLACEHOLDER else state.mcr_news
    return {
        # To the millisecond: the site only keeps a status newer than the last.
        "v": 1, "kind": "status", "generated_at": now.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
        "interval_s": interval_s,
        "monitor_version": __version__,
        "health": dict(state.health),
        "beams": beams,
        "instruments": [_instrument(name, view) for name, view in state.instruments().items()],
        "mcr": {"news": news, "seen_at": _iso(state.mcr_news_at)},
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
        try:
            status = self.build()
        except Exception:  # a bug: logged, and the next send tries again
            logger.exception("Failed to build the status")
            return
        try:
            async with self.session.post(
                self.url, timeout=aiohttp.ClientTimeout(total=self.timeout), allow_redirects=False,
                **signed_json(status, self.secret),
            ) as resp:
                if resp.status not in (204, 409):  # 409: an older one, e.g. after a clock step
                    logger.debug(f"Push site didn't take the status: HTTP {resp.status}")
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.debug(f"Failed to send the status: {exc!r}")
