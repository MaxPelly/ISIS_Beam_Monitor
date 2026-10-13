"""The status the daemon publishes to the push site's status page.

build() is a pure function of the daemon's state; see the push site's
status.py for the payload's layout (version 1). Beams and instruments with
no live reading are sent as unknown, never as zero, so a feed outage doesn't
look like the beam going off.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from isis_monitor import __version__
from isis_monitor.config import BEAM_NAMES
from isis_monitor.daemon_state import MCR_NEWS_PLACEHOLDER, DaemonState

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
        "v": 1, "kind": "status", "generated_at": _iso(now), "interval_s": interval_s,
        "monitor_version": __version__,
        "health": dict(state.health),
        "beams": beams,
        "instruments": [_instrument(name, view) for name, view in state.instruments().items()],
        "mcr": {"news": news, "seen_at": _iso(state.mcr_news_at)},
    }
