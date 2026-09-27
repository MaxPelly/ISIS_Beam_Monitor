import asyncio
import contextlib
import json
import logging
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import websockets

from isis_monitor.config import AppConfig
from isis_monitor.instrument import InstrumentTracker
from isis_monitor.messages import beam_change, startup_status
from isis_monitor.notifiers import NotificationChannel
from isis_monitor.protocols import MonitorSinkProtocol

logger = logging.getLogger(__name__)

COLLECTION_CHECK_INTERVAL = 60.0


@dataclass
class BeamTarget:
    """Describes one accelerator beam target."""
    state_key: str      # Key into MonitorState.beams; also the name used in messages
    channel_label: str  # Passed to the sink and notifications as the channel name


BEAM_TARGETS: List[BeamTarget] = [
    BeamTarget("TS1", "TS1"),
    BeamTarget("TS2", "TS2"),
    BeamTarget("Muon", "Muons"),
]

# The channel labels ("TS1", "TS2", "Muons") that beam.py passes to the TUI
# and sink — the single source of truth for the three target names used
# elsewhere (daemon_state.py, tui.py, main.py) instead of re-spelling them.
CHANNEL_LABELS: Tuple[str, ...] = tuple(bt.channel_label for bt in BEAM_TARGETS)


@dataclass
class BeamState:
    """Per-beam runtime state."""
    current: float = -1.0
    power: str = ""
    since: Optional[datetime] = None  # when `power` last changed


@dataclass
class _PendingChange:
    """A not-yet-confirmed state change, awaiting the debounce window."""
    bt: BeamTarget
    prev_state: str
    prev_val: float
    prev_since: datetime
    new_state: str
    beam_val: float
    high_threshold: float
    started_at: datetime
    task: Optional[asyncio.Task] = None


class BeamChangeAggregator:
    """Debounces raw beam-state transitions before they become notifications.

    PVWS only pushes a value when it changes, so a transition can't be
    confirmed by waiting for a later reading — instead each change starts a
    flush timer, and if the state has flapped back to where it started by
    the time the timer fires, no notification is sent.
    """
    def __init__(
        self,
        beam_channel: NotificationChannel,
        debounce_seconds: float = 20.0,
        fun_mode: bool = False,
        rng: Optional[random.Random] = None,
    ):
        self.beam_channel = beam_channel
        self.debounce_seconds = debounce_seconds
        self.fun_mode = fun_mode
        self.rng = rng or random.Random()
        self._pending: Dict[str, _PendingChange] = {}
        self._recent_offs: Dict[str, datetime] = {}

    def queue_change(
        self,
        bt: BeamTarget,
        prev_state: str,
        prev_val: float,
        prev_since: datetime,
        new_state: str,
        beam_val: float,
        high_threshold: float,
        time_now: datetime,
    ) -> None:
        existing = self._pending.get(bt.state_key)
        if existing is not None:
            # Already pending — just update the latest reading; the original
            # timer (and the original prev_state/prev_since) still applies.
            existing.new_state = new_state
            existing.beam_val = beam_val
            return

        pending = _PendingChange(
            bt, prev_state, prev_val, prev_since, new_state, beam_val, high_threshold, time_now,
        )
        pending.task = asyncio.create_task(self._flush_after_delay(bt.state_key))
        self._pending[bt.state_key] = pending

    async def _flush_after_delay(self, state_key: str) -> None:
        await asyncio.sleep(self.debounce_seconds)
        await self._flush(state_key)

    async def _flush(self, state_key: str) -> None:
        pending = self._pending.pop(state_key, None)
        if pending is None:
            return

        if pending.new_state == pending.prev_state:
            logger.debug(f"{pending.bt.state_key}: flapped back to {pending.prev_state}, dropping.")
            return

        trip_note = ""
        if pending.new_state == "off":
            also_off = [
                other.bt.state_key
                for key, other in self._pending.items()
                if key != state_key
                and other.new_state == "off"
                and abs((other.started_at - pending.started_at).total_seconds()) <= self.debounce_seconds
            ]
            also_off += [
                key
                for key, off_time in self._recent_offs.items()
                if key != state_key
                and abs((off_time - pending.started_at).total_seconds()) <= self.debounce_seconds
            ]
            if also_off:
                joined = " and ".join(also_off) if len(also_off) <= 2 else ", ".join(also_off[:-1]) + f" and {also_off[-1]}"
                trip_note = f"⚠️ {joined} also went off, likely a facility-wide trip"
            self._recent_offs[state_key] = pending.started_at

        notification = beam_change(
            pending.bt.state_key,
            pending.prev_state,
            pending.new_state,
            pending.beam_val,
            pending.prev_val,
            pending.high_threshold,
            pending.started_at - pending.prev_since,
            pending.started_at,
            trip_note=trip_note,
            rng=self.rng if self.fun_mode else None,
            channel=pending.bt.channel_label,
        )
        logger.info(f"State Change: {notification.to_plain_text()}")
        await self.beam_channel.broadcast(notification)

    def pending(self, state_key: str) -> Optional[_PendingChange]:
        """The unconfirmed change for a target, if one is waiting to flush."""
        return self._pending.get(state_key)

    def cancel_all(self) -> None:
        """Cancel any outstanding flush timers, e.g. on shutdown."""
        for pending in self._pending.values():
            if pending.task is not None:
                pending.task.cancel()
        self._pending.clear()


class MonitorState:
    """Holds the runtime state of the beam monitor."""
    def __init__(self):
        self.beams: Dict[str, BeamState] = {
            bt.state_key: BeamState() for bt in BEAM_TARGETS
        }


class BeamMonitor:
    def __init__(
        self,
        config: AppConfig,
        beam_channel: NotificationChannel,
        experiment_channel: NotificationChannel,
        sink: Optional[MonitorSinkProtocol] = None,
        debounce_seconds: float = 20.0,
        rng: Optional[random.Random] = None,
    ):
        self.config = config
        self.data_url = config.isis_websocket_url
        self.beam_channel = beam_channel
        self.experiment_channel = experiment_channel
        self.sink = sink
        self.state = MonitorState()
        self._rng = rng or random.Random()
        self.change_aggregator = BeamChangeAggregator(
            beam_channel, debounce_seconds, fun_mode=config.fun_mode, rng=self._rng,
        )
        self._force_reconnect = asyncio.Event()
        self._current_ws = None
        self._close_task: Optional[asyncio.Task] = None

        # Build dynamic lookups from Config
        self.pv_to_beam: Dict[str, BeamTarget] = {
            config.ts1_beam_current_pv: BEAM_TARGETS[0],
            config.ts2_beam_current_pv: BEAM_TARGETS[1],
            config.muon_beam_current_pv: BEAM_TARGETS[2],
        }
        
        self.beam_boundaries = {
            "TS1": config.ts1_boundaries,
            "TS2": config.ts2_boundaries,
            "Muon": config.muon_boundaries,
        }

        self.instruments: Dict[str, InstrumentTracker] = {
            inst.name: InstrumentTracker(
                inst, experiment_channel, self._beam_power, config.stall_minutes,
                fun_mode=config.fun_mode, rng=self._rng, sink=sink,
            )
            for inst in config.instruments
        }
        self.pv_to_run_name: Dict[str, InstrumentTracker] = {
            t.instrument.run_name_pv: t for t in self.instruments.values()
        }
        self.pv_to_counts: Dict[str, InstrumentTracker] = {
            t.instrument.counts_pv: t for t in self.instruments.values()
        }

    @staticmethod
    def _safe_float(value: Any) -> float:
        """Converts value to float, mapping NaN, None and junk to 0.0."""
        try:
            val = float(value)
        except (ValueError, TypeError):
            return 0.0
        return 0.0 if math.isnan(val) else val

    def _get_power_label(self, beam_uA: float, beam: str) -> str:
        boundaries = self.beam_boundaries[beam]
        if beam_uA <= boundaries[0]: return "off"
        if beam_uA < boundaries[1]: return "low"
        if beam_uA < boundaries[2]: return "medium"
        return "high"

    async def _handle_beam_current(
        self, bt: BeamTarget, raw_val: Any, time_now: datetime
    ):
        """Handle a beam-current value update for a single target."""
        beam_val = self._safe_float(raw_val)
        new_state = self._get_power_label(beam_val, bt.state_key)
        beam_state = self.state.beams[bt.state_key]
        prev_state = beam_state.power
        prev_val = beam_state.current
        prev_since = beam_state.since

        if new_state != prev_state:
            if prev_state == "":
                # First reading for this target — always send immediately, never debounced.
                rng = self._rng if self.config.fun_mode else None
                notification = startup_status(
                    bt.state_key, new_state, beam_val, time_now,
                    rng=rng, channel=bt.channel_label,
                )
                logger.info(f"Startup: {notification.to_plain_text()}")
                await self.beam_channel.broadcast(notification)
                beam_state.since = time_now
            else:
                pending = self.change_aggregator.pending(bt.state_key)
                high_threshold = self.beam_boundaries[bt.state_key][2]
                self.change_aggregator.queue_change(
                    bt, prev_state, prev_val, prev_since or time_now,
                    new_state, beam_val, high_threshold, time_now,
                )
                if pending is not None and new_state == pending.prev_state:
                    # Back where it was before a flicker the debounce will drop,
                    # so the time in that state carries on rather than restarting.
                    beam_state.since = pending.prev_since
                else:
                    beam_state.since = time_now

        beam_state.current = beam_val
        beam_state.power = new_state
        if self.sink:
            self.sink.update_beam_state(bt.channel_label, beam_val, new_state)

    async def _handle_update(self, message: Dict[str, Any]):
        """Dispatch WebSocket update messages."""
        time_now = datetime.now(timezone.utc)

        # NOTE: Bare module-level names are NOT constant patterns in Python's
        # structural pattern matching — they are capture variables. Guard clauses
        # are therefore used for the PV-specific arms.
        match message:
            case {"pv": pv, "value": raw_val} if pv in self.pv_to_beam:
                await self._handle_beam_current(self.pv_to_beam[pv], raw_val, time_now)

            case {"pv": pv, "b64byt": b64_data} if pv in self.pv_to_run_name:
                await self.pv_to_run_name[pv].handle_run_name(b64_data, time_now)

            case {"pv": pv, "value": raw_val} if pv in self.pv_to_counts:
                await self.pv_to_counts[pv].handle_counts(raw_val, time_now)

    def restore_instruments(self, saved: Dict[str, dict]) -> None:
        """Seed each tracker from DaemonState's restored per-instrument state."""
        for name, tracker in self.instruments.items():
            if name in saved:
                tracker.restore(saved[name])

    def _beam_power(self, target: str) -> str:
        beam_state = self.state.beams.get(target)
        return beam_state.power if beam_state else "unknown"

    async def _check_collection_progress(self, time_now: datetime) -> None:
        if self._current_ws is None:
            # Counts can't move while PVWS is disconnected, and beam states are
            # stale; don't mistake that for a stall. The clock restarts on reconnect.
            for tracker in self.instruments.values():
                tracker.reset_stall_clock()
            return
        for tracker in self.instruments.values():
            # One instrument's failure mustn't stop the others being checked.
            try:
                await tracker.check_collection_progress(time_now)
            except Exception:
                logger.exception(f"Collection check failed for {tracker.instrument.name}")

    async def _collection_check_loop(self) -> None:
        while True:
            await asyncio.sleep(COLLECTION_CHECK_INTERVAL)
            await self._check_collection_progress(datetime.now(timezone.utc))

    def request_reconnect(self) -> bool:
        if self._force_reconnect.is_set():
            return False
        self._force_reconnect.set()
        if self._current_ws is not None:
            # Closing ends the `async for` in _run_loop, which then reconnects.
            self._close_task = asyncio.create_task(self._close_ws_quietly(self._current_ws))
        return True

    @staticmethod
    async def _close_ws_quietly(ws) -> None:
        try:
            await ws.close()
        except Exception as e:
            logger.warning(f"Error closing WebSocket during reconnect: {e}")

    async def run(self) -> None:
        """Monitor until cancelled."""
        if not self.data_url:
            logger.warning("No WebSocket URL provided. Beam monitor will not run.")
            return
        try:
            await asyncio.gather(self._run_loop(), self._collection_check_loop())
        finally:
            self.change_aggregator.cancel_all()

    def _set_health(self, status: str) -> None:
        if self.sink:
            self.sink.update_health("beam", status)

    async def _handle_message(self, raw: Any) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.debug("Failed to decode WS message: %s", exc)
            return
        if not (isinstance(data, dict) and data.get("type") == "update"):
            return
        # One malformed PV value must not tear down the connection.
        try:
            await self._handle_update(data)
        except Exception:
            logger.exception(f"Failed to handle PV update: {data!r:.200}")

    async def _run_loop(self) -> None:
        subscribe_msg = json.dumps({
            "type": "subscribe",
            "pvs": [*self.pv_to_beam, *self.pv_to_counts, *self.pv_to_run_name],
        })
        logger.info(f"Beam Monitor started. Connecting to {self.data_url}...")
        interval = self.config.beam_reconnect_interval

        while True:
            try:
                async with websockets.connect(self.data_url) as ws:
                    self._current_ws = ws
                    # A reconnect requested during the handshake is satisfied by
                    # this new connection; left set, it would make later
                    # requests no-ops until the connection next dropped.
                    self._force_reconnect.clear()
                    logger.info("WebSocket connected.")
                    self._set_health("connected")
                    await ws.send(subscribe_msg)
                    async for raw in ws:
                        await self._handle_message(raw)
                logger.warning("WebSocket closed.")
            except (websockets.ConnectionClosed, OSError) as exc:
                logger.warning(f"WebSocket connection lost: {exc}")
            except Exception as exc:
                logger.error(f"Unexpected error in BeamMonitor: {exc!r}")
            finally:
                self._current_ws = None

            if not self._force_reconnect.is_set():
                self._set_health("disconnected")
                logger.warning(f"Reconnecting in {interval}s...")
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._force_reconnect.wait(), timeout=interval)
            if self._force_reconnect.is_set():
                self._force_reconnect.clear()
                logger.info("Beam reconnect requested by operator.")
                self._set_health("reconnecting")
