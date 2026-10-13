from __future__ import annotations

import asyncio
import json
import logging
import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Deque, Dict, Iterable, List, Optional, Tuple

from isis_monitor import instlist
from isis_monitor.config import BEAM_NAMES

logger = logging.getLogger(__name__)

MCR_NEWS_PLACEHOLDER = "Waiting for initial MCR news..."

SUBSCRIBER_QUEUE_SIZE = 500
# How far back run completions are kept in the snapshot: the daily summary
# covers runs over the last 24h, so older ones don't survive a restart.
RUN_COMPLETIONS_WINDOW = timedelta(hours=24)


def _aware_iso(text: str) -> str:
    """Normalise a saved timestamp, treating one without a zone as UTC (the
    daemon always writes UTC); comparing naive and aware times would raise."""
    ts = datetime.fromisoformat(text)
    return (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).isoformat()


def _finite_floats(value: object) -> List[float]:
    """A saved list of numbers; raises ValueError if it's anything else."""
    if not isinstance(value, list):
        raise ValueError(f"expected a list, got {value!r}")
    numbers = [float(v) for v in value]
    if not all(math.isfinite(v) for v in numbers):
        raise ValueError("non-finite number")
    return numbers


@dataclass
class DaemonEvent:
    event: str
    payload: dict


class DaemonState:
    """The daemon's in-memory state and event bus.

    Confined to the event-loop thread: it is not thread-safe, because the
    asyncio.Queue subscribers it publishes to are not either. Code running in
    other threads must hop onto the loop (see main.StateLogHandler).
    """

    def __init__(
        self,
        history_maxlen: int = 10_080,
        logs_maxlen: int = 200,
        instrument_names: Iterable[str] = (),
    ):
        self.beam_states: Dict[str, Dict[str, object]] = {
            beam: {"current": 0.0, "power": "unknown"} for beam in BEAM_NAMES
        }
        self.history: Dict[str, Deque[Tuple[datetime, float, str]]] = {
            beam: deque(maxlen=history_maxlen) for beam in BEAM_NAMES
        }
        self.mcr_news = MCR_NEWS_PLACEHOLDER
        self.mcr_news_at: Optional[datetime] = None  # when this run of the daemon last saw it change
        self.logs: Deque[str] = deque(maxlen=logs_maxlen)
        self.instrument_names = frozenset(instrument_names)
        # Each instrument tracker's live view (its snapshot plus its effective
        # beam target), set by run_daemon once BeamMonitor exists; the trackers
        # are the only record of their state.
        self.instruments: Callable[[], Dict[str, dict]] = dict
        # Their entries from the restored snapshot, for BeamMonitor.restore_instruments
        self.saved_instruments: Dict[str, object] = {}
        # (time, instrument, run number, µA·h)
        self.run_completions: Deque[Tuple[datetime, str, str, float]] = deque(maxlen=2000)
        # The last good CS:INSTLIST, so a restart starts with the right PV prefixes
        self.instlist: instlist.Instruments = {}
        self.health: Dict[str, str] = {
            "daemon": "starting",
            "beam": "unknown",
            "mcr": "unknown",
        }
        self._subscribers: List[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        """Queue of DaemonEvents; a None item means the subscriber fell too far
        behind and was dropped, so the consumer should disconnect and resync."""
        q: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def _publish(self, event: str, payload: dict) -> None:
        dropped = 0
        for q in list(self._subscribers):
            try:
                q.put_nowait(DaemonEvent(event=event, payload=payload))
            except asyncio.QueueFull:
                self._subscribers.remove(q)
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(None)
                dropped += 1
        # Logged only after removal: this log line is itself published back
        # through update_log(), and would otherwise recurse into the full queue.
        if dropped:
            logger.warning(f"Dropped {dropped} IPC subscriber(s) whose event queue was full")

    def update_log(self, message: str) -> None:
        self.logs.append(message)
        self._publish("log", {"message": message})

    def update_beam_state(self, beam: str, current: float, power: str) -> None:
        if beam not in self.beam_states:
            return
        self.beam_states[beam] = {"current": float(current), "power": str(power)}
        self._publish("beam", {"beam": beam, "current": current, "power": power})

    def append_beam_sample(
        self,
        beam: str,
        current: float,
        power: str,
        ts: Optional[datetime] = None,
        publish: bool = True,
    ) -> None:
        ts = ts or datetime.now(timezone.utc)
        if beam not in self.history:
            return
        self.history[beam].append((ts, float(current), str(power)))
        if publish:
            self._publish(
                "sample",
                {"beam": beam, "timestamp": ts.isoformat(), "current": current, "power": power},
            )

    def sample_all_currents(self, ts: Optional[datetime] = None) -> List[Tuple[datetime, str, float, str]]:
        """Append the latest value of every beam to its history; return the
        rows as (timestamp, beam, current, power) for persisting. Beams with
        no live reading ("unknown", e.g. just after reconnecting) are skipped."""
        ts = ts or datetime.now(timezone.utc)
        rows = [
            (ts, beam, float(state["current"]), str(state["power"]))
            for beam, state in self.beam_states.items()
            if state["power"] != "unknown"
        ]
        for _, beam, current, power in rows:
            self.append_beam_sample(beam, current, power, ts=ts)
        return rows

    def trim_history_before(self, cutoff: datetime) -> None:
        for samples in self.history.values():
            while samples and samples[0][0] < cutoff:
                samples.popleft()

    def update_mcr_news(self, news: str) -> None:
        self.mcr_news, self.mcr_news_at = news, datetime.now(timezone.utc)
        self._publish("mcr", {"news": news})

    def update_health(self, component: str, status: str) -> None:
        # Monitors re-report the same status on every successful poll; only
        # actual transitions are worth an event to every subscriber.
        if self.health.get(component) == status:
            return
        self.health[component] = status
        self._publish("health", {"component": component, "status": status})
        if component == "beam" and status != "connected":
            # No readings arrive without the feed, so the last ones can't be
            # trusted; PVWS re-sends every value once it reconnects.
            for beam, beam_state in self.beam_states.items():
                self.update_beam_state(beam, float(beam_state["current"]), "unknown")

    def record_run_completed(self, instrument: str, ts: datetime, run_number: str, uah: float) -> None:
        """Record a completed run and the µA·h it collected (ignored for an unknown instrument)."""
        if instrument in self.instrument_names:
            self.run_completions.append((ts, instrument, run_number, uah))

    def runs_completed_since(self, since: datetime, instruments: Iterable[str]) -> Dict[str, Tuple[int, float]]:
        """Each of `instruments`' runs completed at or after `since`, and their µA·h."""
        totals = {name: (0, 0.0) for name in instruments}
        for ts, name, _number, uah in self.run_completions:
            if ts >= since and name in totals:
                runs, total = totals[name]
                totals[name] = (runs + 1, total + uah)
        return totals

    def snapshot(self) -> dict:
        cutoff = datetime.now(timezone.utc) - RUN_COMPLETIONS_WINDOW
        return {
            "beam_states": {beam: dict(state) for beam, state in self.beam_states.items()},
            "mcr_news": self.mcr_news,
            "instruments": self.instruments(),
            "run_completions": [
                [ts.isoformat(), name, number, uah]
                for ts, name, number, uah in self.run_completions if ts >= cutoff
            ],
            "instlist": instlist.to_snapshot(self.instlist),
        }

    def get_history_snapshot(self, limit: Optional[int] = None) -> dict:
        """History per beam, optionally only the most recent `limit` samples."""
        def tail(data):
            return list(data)[-limit:] if limit else data

        return {
            beam: [
                {"timestamp": ts.isoformat(), "current": cur, "power": power}
                for ts, cur, power in tail(data)
            ]
            for beam, data in self.history.items()
        }

    def get_logs_snapshot(self) -> list:
        return list(self.logs)

    def restore_from_snapshot_json(self, raw: Optional[str]) -> None:
        if not raw:
            return
        try:
            snap = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Corrupt daemon state snapshot, starting fresh.")
            return
        beam_states = snap.get("beam_states", {})
        for beam in BEAM_NAMES:
            if beam in beam_states:
                # The power is left "unknown" until the beam feed connects:
                # the saved one may be long out of date.
                self.beam_states[beam] = {
                    "current": float(beam_states[beam].get("current", 0.0)),
                    "power": "unknown",
                }
        self.mcr_news = str(snap.get("mcr_news", self.mcr_news))
        saved = snap.get("instruments")
        saved = saved if isinstance(saved, dict) else {}
        self.saved_instruments = {k: v for k, v in saved.items() if k in self.instrument_names}
        self._restore_run_completions(snap.get("run_completions"))
        self.instlist = instlist.from_snapshot(snap.get("instlist"))

    def _restore_run_completions(self, saved: object) -> None:
        """Restore recent completions of instruments still in the config."""
        if not isinstance(saved, list):
            return
        cutoff = datetime.now(timezone.utc) - RUN_COMPLETIONS_WINDOW
        skipped = 0
        for entry in saved:
            try:
                text, name, number, uah = entry
                ts = datetime.fromisoformat(_aware_iso(text))
                (uah,) = _finite_floats([uah])
                if not isinstance(number, str):
                    raise TypeError("run number isn't text")
                wanted = name in self.instrument_names and ts >= cutoff
            except (TypeError, ValueError):  # includes an unhashable name, or an older 3-item entry
                skipped += 1
                continue
            if wanted:
                self.run_completions.append((ts, name, number, uah))
        if skipped:
            logger.warning(f"Skipped {skipped} malformed run completion(s) in snapshot")
