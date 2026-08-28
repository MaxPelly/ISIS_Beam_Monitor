from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Deque, Dict, List, Optional, Tuple

from isis_monitor.protocols import MonitorSinkProtocol


@dataclass
class DaemonEvent:
    event: str
    payload: dict


class DaemonState(MonitorSinkProtocol):
    def __init__(self, history_maxlen: int = 10_080, logs_maxlen: int = 200):
        self._lock = RLock()
        self.history_maxlen = history_maxlen
        self.beam_states: Dict[str, Dict[str, object]] = {
            "TS1": {"current": 0.0, "power": "unknown"},
            "TS2": {"current": 0.0, "power": "unknown"},
            "Muons": {"current": 0.0, "power": "unknown"},
        }
        self.history: Dict[str, Deque[Tuple[datetime, float, str]]] = {
            beam: deque(maxlen=history_maxlen) for beam in self.beam_states
        }
        self.mcr_news = "Waiting for initial MCR news..."
        self.logs: Deque[str] = deque(maxlen=logs_maxlen)
        self.run_name = ""
        self.current_counts = -1.0
        self.last_update = datetime.now(timezone.utc)
        self.health: Dict[str, str] = {
            "daemon": "starting",
            "beam": "unknown",
            "mcr": "unknown",
        }
        self._subscribers: List[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _publish(self, event: str, payload: dict) -> None:
        dead = []
        for q in list(self._subscribers):
            try:
                q.put_nowait(DaemonEvent(event=event, payload=payload))
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def update_log(self, message: str) -> None:
        with self._lock:
            self.logs.append(message)
            self.last_update = datetime.now(timezone.utc)
        self._publish("log", {"message": message})

    def update_beam_state(self, beam: str, current: float, power: str) -> None:
        with self._lock:
            if beam not in self.beam_states:
                return
            self.beam_states[beam] = {"current": float(current), "power": str(power)}
            self.last_update = datetime.now(timezone.utc)
        self._publish("beam", {"beam": beam, "current": current, "power": power})

    def append_beam_sample(self, beam: str, current: float, power: str, ts: Optional[datetime] = None) -> None:
        ts = ts or datetime.now(timezone.utc)
        with self._lock:
            if beam not in self.history:
                return
            self.history[beam].append((ts, float(current), str(power)))
            self.last_update = ts
        self._publish(
            "sample",
            {
                "beam": beam,
                "timestamp": ts.isoformat(),
                "current": current,
                "power": power,
            },
        )

    def trim_history_before(self, cutoff: datetime) -> None:
        with self._lock:
            for beam in self.history:
                trimmed = deque(
                    (entry for entry in self.history[beam] if entry[0] >= cutoff),
                    maxlen=self.history_maxlen,
                )
                self.history[beam] = trimmed

    def update_mcr_news(self, news: str) -> None:
        with self._lock:
            self.mcr_news = news
            self.last_update = datetime.now(timezone.utc)
        self._publish("mcr", {"news": news})

    def update_run_name(self, run_name: str) -> None:
        with self._lock:
            self.run_name = run_name
            self.last_update = datetime.now(timezone.utc)
        self._publish("run", {"run_name": run_name})

    def update_counts(self, counts: float) -> None:
        with self._lock:
            self.current_counts = float(counts)
            self.last_update = datetime.now(timezone.utc)
        self._publish("counts", {"counts": counts})

    def update_health(self, component: str, status: str) -> None:
        with self._lock:
            self.health[component] = status
            self.last_update = datetime.now(timezone.utc)
        self._publish("health", {"component": component, "status": status})

    def snapshot(self) -> dict:
        with self._lock:
            history_json = {
                beam: [
                    {
                        "timestamp": ts.isoformat(),
                        "current": cur,
                        "power": power,
                    }
                    for ts, cur, power in data
                ]
                for beam, data in self.history.items()
            }
            return {
                "last_update": self.last_update.isoformat(),
                "beam_states": dict(self.beam_states),
                "history": history_json,
                "mcr_news": self.mcr_news,
                "logs": list(self.logs),
                "run_name": self.run_name,
                "current_counts": self.current_counts,
                "health": dict(self.health),
            }

    def sample_all_currents(self, ts: Optional[datetime] = None) -> None:
        ts = ts or datetime.now(timezone.utc)
        with self._lock:
            items = list(self.beam_states.items())
        for beam, state in items:
            self.append_beam_sample(
                beam,
                float(state["current"]),
                str(state["power"]),
                ts=ts,
            )

    def cutoff_for_days(self, retention_days: int) -> datetime:
        return datetime.now(timezone.utc) - timedelta(days=retention_days)
