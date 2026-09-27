from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Deque, Dict, Iterable, List, Optional, Sequence, Tuple

from isis_monitor.beam import CHANNEL_LABELS
from isis_monitor.config import InstrumentConfig
from isis_monitor.protocols import MonitorSinkProtocol

logger = logging.getLogger(__name__)

SUBSCRIBER_QUEUE_SIZE = 500


def _aware_iso(text: str) -> str:
    """Normalise a saved timestamp, treating one without a zone as UTC (the
    daemon always writes UTC); comparing naive and aware times would raise."""
    ts = datetime.fromisoformat(text)
    return (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).isoformat()


@dataclass
class DaemonEvent:
    event: str
    payload: dict


class DaemonState(MonitorSinkProtocol):
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
        self.mcr_news = "Waiting for initial MCR news..."
        self.logs: Deque[str] = deque(maxlen=logs_maxlen)
        # notify_counts and beam_target come from the config and are only
        # carried here so clients can display them.
        self.instruments: Dict[str, Dict[str, object]] = {
            inst.name: {
                "run_name": "",
                "counts": -1.0,
                "total_runs": 0,
                # Tracker state restored into BeamMonitor after a restart, so a
                # run's clock and "about to finish" card aren't reset by it.
                "run_started_at": None,  # ISO timestamp
                "end_notified": False,
                "notify_counts": inst.notify_counts,
                "beam_target": inst.beam_target,
            }
            for inst in instruments
        }
        self.run_completions: Deque[Tuple[datetime, str]] = deque(maxlen=2000)  # (time, instrument)
        self.last_update = datetime.now(timezone.utc)
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

    def _touch(self, ts: Optional[datetime] = None) -> None:
        self.last_update = ts or datetime.now(timezone.utc)

    def update_log(self, message: str) -> None:
        self.logs.append(message)
        self._touch()
        self._publish("log", {"message": message})

    def update_beam_state(self, beam: str, current: float, power: str) -> None:
        if beam not in self.beam_states:
            return
        self.beam_states[beam] = {"current": float(current), "power": str(power)}
        self._touch()
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
        self._touch(ts)
        if publish:
            self._publish(
                "sample",
                {"beam": beam, "timestamp": ts.isoformat(), "current": current, "power": power},
            )

    def sample_all_currents(self, ts: Optional[datetime] = None) -> List[Tuple[datetime, str, float, str]]:
        """Append the latest value of every beam to its history; return the
        rows as (timestamp, beam, current, power) for persisting."""
        ts = ts or datetime.now(timezone.utc)
        rows = [
            (ts, beam, float(state["current"]), str(state["power"]))
            for beam, state in self.beam_states.items()
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
        self._touch()
        self._publish("mcr", {"news": news})

    def update_run_name(self, instrument: str, run_name: str) -> None:
        if instrument not in self.instruments:
            return
        self.instruments[instrument]["run_name"] = run_name
        self._touch()
        self._publish("run", {"instrument": instrument, "run_name": run_name})

    def update_run_progress(
        self, instrument: str, run_started_at: Optional[datetime], end_notified: bool
    ) -> None:
        if instrument not in self.instruments:
            return
        self.instruments[instrument]["run_started_at"] = run_started_at.isoformat() if run_started_at else None
        self.instruments[instrument]["end_notified"] = bool(end_notified)

    def update_counts(self, instrument: str, counts: float) -> None:
        if instrument not in self.instruments:
            return
        self.instruments[instrument]["counts"] = float(counts)
        self._touch()
        self._publish("counts", {"instrument": instrument, "counts": counts})

    def update_health(self, component: str, status: str) -> None:
        # Monitors re-report the same status on every successful poll; only
        # actual transitions are worth an event to every subscriber.
        if self.health.get(component) == status:
            return
        self.health[component] = status
        self._touch()
        self._publish("health", {"component": component, "status": status})

    def record_run_completed(self, instrument: str, ts: datetime) -> int:
        """Record a completed run and return the instrument's new all-time
        total, or 0 for an unknown instrument."""
        if instrument not in self.instruments:
            return 0
        self.run_completions.append((ts, instrument))
        self.instruments[instrument]["total_runs"] += 1
        self._touch(ts)
        return self.instruments[instrument]["total_runs"]

    def count_runs_completed_since(self, since: datetime, instruments: Optional[Iterable[str]] = None) -> int:
        """Runs completed at or after `since`, optionally only on `instruments`."""
        wanted = None if instruments is None else set(instruments)
        return sum(
            1 for ts, name in self.run_completions
            if ts >= since and (wanted is None or name in wanted)
        )

    def snapshot(self) -> dict:
        return {
            "last_update": self.last_update.isoformat(),
            "beam_states": {beam: dict(state) for beam, state in self.beam_states.items()},
            "mcr_news": self.mcr_news,
            "instruments": {name: dict(info) for name, info in self.instruments.items()},
            "health": dict(self.health),
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
                self.beam_states[beam] = {
                    "current": float(beam_states[beam].get("current", 0.0)),
                    "power": str(beam_states[beam].get("power", "unknown")),
                }
        self.mcr_news = str(snap.get("mcr_news", self.mcr_news))
        self._restore_instruments(snap)
        # Health isn't restored: saved values describe connections from before
        # the restart, and would show "connected" before anything has connected.

    def _restore_instruments(self, snap: dict) -> None:
        """Restore run state for instruments still in the config; instruments
        that have since been removed are dropped."""
        saved = snap.get("instruments")
        if not isinstance(saved, dict):
            # Snapshot from before multi-instrument support: its single run
            # belongs to the first instrument.
            if not self.instruments or not any(
                k in snap for k in ("run_name", "current_counts", "total_runs_completed")
            ):
                return
            saved = {next(iter(self.instruments)): {
                "run_name": snap.get("run_name", ""),
                "counts": snap.get("current_counts", -1.0),
                "total_runs": snap.get("total_runs_completed", 0),
            }}
        for name, info in saved.items():
            if name not in self.instruments or not isinstance(info, dict):
                continue
            current = self.instruments[name]
            try:
                started = info.get("run_started_at")
                restored = {
                    "run_name": str(info.get("run_name", current["run_name"])),
                    "counts": float(info.get("counts", current["counts"])),
                    "total_runs": int(info.get("total_runs", current["total_runs"])),
                    # Validated here (raises ValueError) but kept as ISO text.
                    "run_started_at": _aware_iso(started) if started else None,
                    "end_notified": bool(info.get("end_notified", False)),
                }
            except (TypeError, ValueError):
                logger.warning(f"Skipping malformed snapshot state for instrument {name}")
                continue
            current.update(restored)
