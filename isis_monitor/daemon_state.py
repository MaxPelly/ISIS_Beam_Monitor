from __future__ import annotations

import asyncio
import json
import logging
import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, Iterable, List, Optional, Sequence, Tuple

from isis_monitor.config import CHANNEL_LABELS, InstrumentConfig

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
        instruments: Sequence[InstrumentConfig] = (),
    ):
        self.beam_states: Dict[str, Dict[str, object]] = {
            beam: {"current": 0.0, "power": "unknown"} for beam in CHANNEL_LABELS
        }
        self.history: Dict[str, Deque[Tuple[datetime, float, str]]] = {
            beam: deque(maxlen=history_maxlen) for beam in CHANNEL_LABELS
        }
        self.mcr_news = MCR_NEWS_PLACEHOLDER
        self.logs: Deque[str] = deque(maxlen=logs_maxlen)
        # notify_counts, auto and beam_target come from the config, for
        # clients to display; the saved notify_counts and auto are also
        # checked on restore. target is the current run's (see InstrumentTracker).
        self.instruments: Dict[str, Dict[str, object]] = {
            inst.name: {
                "run_name": "",
                "counts": -1.0,
                "total_runs": 0,
                # Tracker state restored into BeamMonitor after a restart, so a
                # run's clock and "about to finish" card aren't reset by it.
                "run_started_at": None,  # ISO timestamp
                "end_notified": False,
                "stale_counts": -1.0,
                "run_totals": [],  # completed runs' µA·h, for auto targets
                # The current experiment, for InstrumentTracker's summary card
                "rb_number": "",
                "run_rb": "",
                "exp_since": None,  # ISO timestamp
                "exp_partial": True,
                "exp_runs": 0,
                "exp_uah": 0.0,
                "target": inst.notify_counts,
                "auto_runs": 0,  # how many runs an auto target came from; 0 = the fallback
                "notify_counts": inst.notify_counts,
                "auto": inst.auto_counts,
                "beam_target": inst.beam_target,
            }
            for inst in instruments
        }
        self.run_completions: Deque[Tuple[datetime, str, float]] = deque(maxlen=2000)  # (time, instrument, µA·h)
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
        self.mcr_news = news
        self._publish("mcr", {"news": news})

    def update_run_name(self, instrument: str, run_name: str) -> None:
        if instrument not in self.instruments:
            return
        self.instruments[instrument]["run_name"] = run_name
        self._publish("run", {"instrument": instrument, "run_name": run_name})

    def update_run_progress(
        self, instrument: str, run_started_at: Optional[datetime], end_notified: bool,
        stale_counts: float,
    ) -> None:
        if instrument not in self.instruments:
            return
        self.instruments[instrument]["run_started_at"] = run_started_at.isoformat() if run_started_at else None
        self.instruments[instrument]["end_notified"] = bool(end_notified)
        self.instruments[instrument]["stale_counts"] = float(stale_counts)

    def update_counts(self, instrument: str, counts: float) -> None:
        if instrument not in self.instruments:
            return
        self.instruments[instrument]["counts"] = float(counts)
        self._publish("counts", {"instrument": instrument, "counts": counts})

    def update_target(self, instrument: str, target: float, auto_runs: int, run_totals: List[float]) -> None:
        """The current run's target, and the run history it came from."""
        if instrument not in self.instruments:
            return
        self.instruments[instrument].update(target=float(target), auto_runs=int(auto_runs), run_totals=run_totals)
        self._publish("target", {"instrument": instrument, "target": target, "auto_runs": auto_runs})

    def update_experiment(
        self, instrument: str, rb_number: str, run_rb: str, since: Optional[datetime],
        partial: bool, runs: int, uah: float,
    ) -> None:
        """Saved, not published: only restored into the tracker."""
        if instrument in self.instruments:
            self.instruments[instrument].update(
                rb_number=rb_number, run_rb=run_rb, exp_since=since.isoformat() if since else None,
                exp_partial=bool(partial), exp_runs=int(runs), exp_uah=float(uah),
            )

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

    def record_run_completed(self, instrument: str, ts: datetime, uah: float) -> int:
        """Record a completed run and the µA·h it collected, and return the
        instrument's new all-time total, or 0 for an unknown instrument."""
        if instrument not in self.instruments:
            return 0
        self.run_completions.append((ts, instrument, uah))
        self.instruments[instrument]["total_runs"] += 1
        return self.instruments[instrument]["total_runs"]

    def runs_completed_since(self, since: datetime, instruments: Iterable[str]) -> Dict[str, Tuple[int, float]]:
        """Each of `instruments`' runs completed at or after `since`, and their µA·h."""
        totals = {name: (0, 0.0) for name in instruments}
        for ts, name, uah in self.run_completions:
            if ts >= since and name in totals:
                runs, total = totals[name]
                totals[name] = (runs + 1, total + uah)
        return totals

    def snapshot(self) -> dict:
        cutoff = datetime.now(timezone.utc) - RUN_COMPLETIONS_WINDOW
        return {
            "beam_states": {beam: dict(state) for beam, state in self.beam_states.items()},
            "mcr_news": self.mcr_news,
            "instruments": {name: dict(info) for name, info in self.instruments.items()},
            "run_completions": [
                [ts.isoformat(), name, uah] for ts, name, uah in self.run_completions if ts >= cutoff
            ],
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
        for beam in CHANNEL_LABELS:
            if beam in beam_states:
                # The power is left "unknown" until the beam feed connects:
                # the saved one may be long out of date.
                self.beam_states[beam] = {
                    "current": float(beam_states[beam].get("current", 0.0)),
                    "power": "unknown",
                }
        self.mcr_news = str(snap.get("mcr_news", self.mcr_news))
        self._restore_instruments(snap)
        self._restore_run_completions(snap.get("run_completions"))

    def _restore_instruments(self, snap: dict) -> None:
        """Restore run state for instruments still in the config; instruments
        that have since been removed are dropped."""
        saved = snap.get("instruments")
        if not isinstance(saved, dict):
            return
        for name, info in saved.items():
            if name not in self.instruments or not isinstance(info, dict):
                continue
            current = self.instruments[name]
            try:
                started, exp_since = info.get("run_started_at"), info.get("exp_since")
                # A finishing card sent for, or a target from, another
                # notify_counts (changed by the config save behind this
                # restart) doesn't cover the new one, which may still be ahead.
                same_target = (
                    float(info.get("notify_counts", current["notify_counts"])) == current["notify_counts"]
                    and info.get("auto", False) == current["auto"]
                )
                restored = {
                    "run_name": str(info.get("run_name", current["run_name"])),
                    "counts": float(info.get("counts", current["counts"])),
                    "total_runs": int(info.get("total_runs", current["total_runs"])),
                    # Validated here (raises ValueError) but kept as ISO text.
                    "run_started_at": _aware_iso(started) if started else None,
                    "end_notified": bool(info.get("end_notified", False)) and same_target,
                    "stale_counts": float(info.get("stale_counts", -1.0)),
                    "run_totals": _finite_floats(info.get("run_totals", [])),
                    "rb_number": str(info.get("rb_number", "")),
                    "run_rb": str(info.get("run_rb", "")),
                    "exp_since": _aware_iso(exp_since) if exp_since else None,
                    "exp_partial": bool(info.get("exp_partial", True)),
                    "exp_runs": int(info.get("exp_runs", 0)),
                    "exp_uah": float(info.get("exp_uah", 0.0)),
                }
                # None has the tracker work the target out again (see InstrumentTracker.restore).
                restored.update(
                    target=float(info["target"]) if same_target and "target" in info else None,
                    auto_runs=int(info.get("auto_runs", 0)),
                )
            except (TypeError, ValueError):
                logger.warning(f"Skipping malformed snapshot state for instrument {name}")
                continue
            current.update(restored)

    def _restore_run_completions(self, saved: object) -> None:
        """Restore recent completions of instruments still in the config."""
        if not isinstance(saved, list):
            return
        cutoff = datetime.now(timezone.utc) - RUN_COMPLETIONS_WINDOW
        skipped = 0
        for entry in saved:
            try:
                text, name, uah = entry
                ts = datetime.fromisoformat(_aware_iso(text))
                (uah,) = _finite_floats([uah])
                wanted = name in self.instruments and ts >= cutoff
            except (TypeError, ValueError):  # includes an unhashable name
                skipped += 1
                continue
            if wanted:
                self.run_completions.append((ts, name, uah))
        if skipped:
            logger.warning(f"Skipped {skipped} malformed run completion(s) in snapshot")
