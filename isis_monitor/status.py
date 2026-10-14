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
from isis_monitor.messages import get_timezone, sparkline_chars
from isis_monitor.notifiers import signed_json

logger = logging.getLogger(__name__)

HISTORY = timedelta(hours=24)
HISTORY_POINTS = 144  # 10-minute bins
# The push site's limits (its status.py); anything longer would make it refuse the whole status.
SHORT, LABEL, MAX_TITLE, MAX_NEWS = 16, 32, 200, 1000
MAX_INSTRUMENTS = 64  # the push site refuses a status with more


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
        "instruments": [_instrument(name, view) for name, view in state.instruments().items()][:MAX_INSTRUMENTS],
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
        self._refused: Optional[int] = None  # the HTTP status the site last refused one with

    def poke(self) -> None:
        self._changed.set()

    async def run(self) -> None:
        while True:
            await self._send()
            await asyncio.sleep(self.DEBOUNCE)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), max(self.interval - self.DEBOUNCE, 0))
            self._changed.clear()

    def _took(self, status: int) -> None:
        """Warns once when the site starts refusing the status (e.g. 400 for one it
        can't read, 401 for a wrong secret, 404 for an older site), and when it
        takes it again; 409 (not newer than the last, e.g. after a clock step) is routine."""
        refused = None if status in (204, 409) else status
        if refused != self._refused:
            if refused:
                logger.warning(f"Push site refuses the status: HTTP {status}; its Status page won't update")
            else:
                logger.info("Push site takes the status again")
        self._refused = refused
        if status == 409:
            logger.debug("Push site didn't take the status: HTTP 409 (not newer than the last)")

    async def _send(self) -> None:
        """One status; any failure is only logged, and never stops the publisher."""
        try:
            async with self.session.post(
                self.url, timeout=aiohttp.ClientTimeout(total=self.timeout), allow_redirects=False,
                **signed_json(self.build(), self.secret),
            ) as resp:
                self._took(resp.status)
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.debug(f"Failed to send the status: {exc!r}")
        except Exception:  # a bug: logged, and the next send tries again
            logger.exception("Failed to send the status")


SPARK_BINS = 3  # 10-minute bins per sparkline character: 48 characters for 24h
TITLE_WIDTH = 40


def render_text(status: dict) -> str:
    """The status as plain text for a terminal, times in the configured timezone."""
    def local(text: Optional[str], fmt: str = "%H:%M") -> str:
        return datetime.fromisoformat(text).astimezone(get_timezone()).strftime(fmt) if text else "?"

    def number(value: Optional[float], digits: int = 1) -> str:
        return "?" if value is None else f"{value:.{digits}f}"

    health = ", ".join(f"{k} {v}" for k, v in status["health"].items())
    lines = [f"ISIS Beam Monitor v{status['monitor_version']} at {local(status['generated_at'], '%a %d %b %H:%M:%S')}"
             f" ({health})", ""]
    for beam in status["beams"]:
        values = [v for v, _ in beam["history"]["values"]]
        groups = [values[i:i + SPARK_BINS] for i in range(0, len(values), SPARK_BINS)]
        means = [None if not (known := [v for v in g if v is not None]) else sum(known) / len(known) for g in groups]
        floor = min((m for m in means if m is not None), default=0.0)  # gaps mustn't stretch the scale
        chars = sparkline_chars([floor if m is None else m for m in means], len(means))
        spark = "".join("·" if m is None else "_" if c == " " else c for m, c in zip(means, chars))  # _ is zero
        spark = spark if spark.strip("·") else ""  # nothing for 24h
        since = f"since {local(beam['since'])}" if beam["since"] else ""
        lines.append(f"{beam['name']:<6}{number(beam['current']):>7} µA  {beam['power']:<8}{since:<13}{spark}".rstrip())
    width = max([12] + [len(i["name"]) + 2 for i in status["instruments"]])
    lines += ["", f"{'Instrument':<{width}}{'Beam':<6}{'Run':<10}{'State':<11}{'µA·h':>13}  Title"]
    for inst in status["instruments"]:
        target = "" if inst["target"] is None else f"/{'≈' if inst['auto'] else ''}{inst['target']:.0f}"
        title = inst["title"] if len(inst["title"]) <= TITLE_WIDTH else inst["title"][:TITLE_WIDTH - 1] + "…"
        lines.append(f"{inst['name']:<{width}}{inst['beam']:<6}{inst['run_number'] or '-':<10}{inst['run_state'] or '?':<11}"
                     f"{number(inst['counts']) + target:>13}  {title}".rstrip())
    seen = f" (since {local(status['mcr']['seen_at'], '%a %d %b %H:%M')})" if status["mcr"]["seen_at"] else ""
    lines += ["", f"MCR news{seen}:", status["mcr"]["news"] or "(none yet)"]
    return "\n".join(lines)
