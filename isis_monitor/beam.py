import asyncio
import json
import base64
import logging
import math
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

import websockets

from isis_monitor.config import AppConfig
from isis_monitor.messages import (
    beam_change,
    frames_stalled,
    frames_vetoed,
    run_milestone,
    run_started,
    run_finishing,
    startup_status,
)
from isis_monitor.notifiers import NotificationChannel
from isis_monitor.protocols import TUIProtocol, MonitorSinkProtocol

logger = logging.getLogger(__name__)

FRAME_SAMPLE_WINDOW = timedelta(minutes=15)
FRAME_CHECK_INTERVAL = 60.0
RUN_MILESTONE_INTERVAL = 25


def _fit_rate(samples: List[Tuple[datetime, float]]) -> float:
    """Least-squares slope (value per second) through (time, value) samples."""
    if len(samples) < 2:
        return 0.0
    t0 = samples[0][0]
    xs = [(t - t0).total_seconds() for t, _ in samples]
    ys = [v for _, v in samples]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    numer = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return numer / denom

@dataclass
class BeamTarget:
    """Describes one accelerator beam target and how to handle its updates."""
    state_key: str      # Key into MonitorState.beams
    channel_label: str  # Passed to the TUI and sink as the channel name
    display_name: str   # Human-readable name used in log/notification messages

BEAM_TARGETS: List[BeamTarget] = [
    BeamTarget("TS1",  "TS1",   "TS1"),
    BeamTarget("TS2",  "TS2",   "TS2"),
    BeamTarget("Muon", "Muons", "Muon"),
]

BEAM_TARGET_BY_KEY: Dict[str, BeamTarget] = {bt.state_key: bt for bt in BEAM_TARGETS}


@dataclass
class BeamState:
    """Per-beam runtime state."""
    current: float = -1.0
    power: str = ""
    since: Optional[datetime] = None  # when `power` last changed


class _PendingChange:
    """A not-yet-confirmed state change, awaiting the debounce window."""
    def __init__(
        self,
        bt: BeamTarget,
        prev_state: str,
        prev_val: float,
        prev_since: datetime,
        new_state: str,
        beam_val: float,
        high_threshold: float,
        started_at: datetime,
    ):
        self.bt = bt
        self.prev_state = prev_state
        self.prev_val = prev_val
        self.prev_since = prev_since
        self.new_state = new_state
        self.beam_val = beam_val
        self.high_threshold = high_threshold
        self.started_at = started_at
        self.task: Optional[asyncio.Task] = None


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
        try:
            await asyncio.sleep(self.debounce_seconds)
        except asyncio.CancelledError:
            return
        await self._flush(state_key)

    async def _flush(self, state_key: str) -> None:
        pending = self._pending.pop(state_key, None)
        if pending is None:
            return

        if pending.new_state == pending.prev_state:
            logger.debug(f"{pending.bt.display_name}: flapped back to {pending.prev_state}, dropping.")
            return

        trip_note = ""
        if pending.new_state == "off":
            also_off = [
                other.bt.display_name
                for key, other in self._pending.items()
                if key != state_key
                and other.new_state == "off"
                and abs((other.started_at - pending.started_at).total_seconds()) <= self.debounce_seconds
            ]
            also_off += [
                BEAM_TARGET_BY_KEY[key].display_name
                for key, off_time in self._recent_offs.items()
                if key != state_key
                and abs((off_time - pending.started_at).total_seconds()) <= self.debounce_seconds
            ]
            if also_off:
                joined = " and ".join(also_off) if len(also_off) <= 2 else ", ".join(also_off[:-1]) + f" and {also_off[-1]}"
                trip_note = f"⚠️ {joined} also went off, likely a facility-wide trip"
            self._recent_offs[state_key] = pending.started_at

        notification = beam_change(
            pending.bt.display_name,
            pending.prev_state,
            pending.new_state,
            pending.beam_val,
            pending.prev_val,
            pending.high_threshold,
            pending.started_at - pending.prev_since,
            pending.started_at,
            trip_note=trip_note,
            rng=self.rng if self.fun_mode else None,
        )
        logger.info(f"State Change: {notification.to_plain_text()}")
        await self.beam_channel.broadcast(notification)

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
        self.run_name: str = ""
        self.run_started_at: Optional[datetime] = None
        self.current_counts: float = -1.0  # whichever of good/raw counts_type tracks
        self.current_good_frames: float = -1.0
        self.current_raw_frames: float = -1.0
        self.frame_samples: Deque[Tuple[datetime, float, float]] = deque()  # (time, good, raw)
        self.end_notified: bool = False

        # Periodic frame-progress check (BeamMonitor._check_frame_progress)
        self.last_check_good: float = -1.0
        self.last_check_raw: float = -1.0
        self.frames_stalled_since: Optional[datetime] = None
        self.veto_warned: bool = False
        self.stall_warned: bool = False


class BeamMonitor:
    def __init__(
        self,
        config: AppConfig,
        beam_channel: NotificationChannel,
        experiment_channel: NotificationChannel,
        counts_target: float,
        tui: Optional[TUIProtocol] = None,
        sink: Optional[MonitorSinkProtocol] = None,
        debounce_seconds: float = 20.0,
        rng: Optional[random.Random] = None,
    ):
        self.config = config
        self.data_url = config.isis_websocket_url
        self.counts_pv = config.counts_pv
        self.run_name_pv = config.run_name_pv
        self.beam_channel = beam_channel
        self.experiment_channel = experiment_channel
        self.counts_target = counts_target
        self.tui = tui
        self.sink = sink
        self.state = MonitorState()
        self._rng = rng or random.Random()
        self.change_aggregator = BeamChangeAggregator(
            beam_channel, debounce_seconds, fun_mode=config.fun_mode, rng=self._rng,
        )
        self._force_reconnect = asyncio.Event()
        self._current_ws = None

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

    def _safe_float(self, value: Any) -> float:
        """Safely converts value to float. Returns 0.0 on NaN, None, or error."""
        if value is None:
            return 0.0
        try:
            if isinstance(value, str) and value.strip().lower() == "nan":
                return 0.0
            val = float(value)
            if math.isnan(val):
                return 0.0
            return val
        except (ValueError, TypeError):
            return 0.0

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
                notification = startup_status(bt.display_name, new_state, beam_val, time_now, rng=rng)
                logger.info(f"Startup: {notification.to_plain_text()}")
                await self.beam_channel.broadcast(notification)
            else:
                high_threshold = self.beam_boundaries[bt.state_key][2]
                self.change_aggregator.queue_change(
                    bt, prev_state, prev_val, prev_since or time_now,
                    new_state, beam_val, high_threshold, time_now,
                )
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

            case {"pv": pv, "b64byt": b64_data} if pv == self.run_name_pv:
                if not b64_data or (
                    isinstance(b64_data, str) and b64_data.lower() == "nan"
                ):
                    return
                try:
                    name = base64.b64decode(b64_data).decode().strip("\x00")
                except Exception as e:
                    logger.warning(f"Failed to decode run name b64: {e}")
                    return

                if self.state.run_name and self.state.run_name != name:
                    notification = run_started(
                        name,
                        self.state.run_name,
                        time_now - self.state.run_started_at,
                        self.state.current_good_frames,
                        self.state.current_raw_frames,
                        time_now,
                        rng=self._rng if self.config.fun_mode else None,
                    )
                    logger.info(f"New Run: {notification.to_plain_text()}")
                    await self.experiment_channel.broadcast(notification)
                    self.state.current_counts = 0
                    self.state.current_good_frames = 0.0
                    self.state.current_raw_frames = 0.0
                    self.state.end_notified = False
                    self.state.frame_samples.clear()
                    self.state.last_check_good = -1.0
                    self.state.last_check_raw = -1.0
                    self.state.frames_stalled_since = None
                    self.state.veto_warned = False
                    self.state.stall_warned = False

                    if self.sink:
                        total_runs = self.sink.record_run_completed(time_now)
                        if self.config.fun_mode and total_runs % RUN_MILESTONE_INTERVAL == 0:
                            milestone = run_milestone(total_runs, time_now, rng=self._rng)
                            logger.info(f"Milestone: {milestone.to_plain_text()}")
                            await self.experiment_channel.broadcast(milestone)

                self.state.run_name = name
                self.state.run_started_at = time_now
                if self.sink:
                    self.sink.update_run_name(name)

            case {"pv": pv, "text": text_val} if pv == self.counts_pv:
                if not text_val or (
                    isinstance(text_val, str) and text_val.lower() == "nan"
                ):
                    return
                try:
                    parts = text_val.split("/")
                    good_frames = float(parts[0])
                    raw_frames = float(parts[1])
                except (IndexError, ValueError) as e:
                    logger.warning(f"Failed to parse counts from '{text_val}': {e}")
                    return

                tracked = good_frames if self.config.counts_type == "good" else raw_frames

                self.state.current_good_frames = good_frames
                self.state.current_raw_frames = raw_frames
                self.state.current_counts = tracked
                self.state.frame_samples.append((time_now, good_frames, raw_frames))
                self._prune_frame_samples(time_now)

                if self.sink:
                    self.sink.update_counts(tracked)

                if self.state.end_notified and tracked < (self.counts_target - 25):
                    self.state.end_notified = False

                if tracked > self.counts_target and not self.state.end_notified:
                    rate = _fit_rate(self._tracked_frame_samples())
                    notification = run_finishing(
                        self.state.run_name,
                        tracked,
                        self.counts_target,
                        good_frames,
                        raw_frames,
                        rate,
                        self._instrument_beam_state(),
                        time_now,
                        rng=self._rng if self.config.fun_mode else None,
                    )
                    logger.info(f"Target Reached: {notification.to_plain_text()}")
                    await self.experiment_channel.broadcast(notification)
                    self.state.end_notified = True

        if self.tui:
            for bt in BEAM_TARGETS:
                state = self.state.beams[bt.state_key]
                self.tui.update_beam_state(bt.channel_label, state.current, state.power)

    def _prune_frame_samples(self, now: datetime) -> None:
        cutoff = now - FRAME_SAMPLE_WINDOW
        samples = self.state.frame_samples
        while samples and samples[0][0] < cutoff:
            samples.popleft()

    def _tracked_frame_samples(self) -> List[Tuple[datetime, float]]:
        """The (time, value) series counts_target is measured against."""
        if self.config.counts_type == "good":
            return [(t, good) for t, good, _ in self.state.frame_samples]
        return [(t, raw) for t, _, raw in self.state.frame_samples]

    def _instrument_beam_state(self) -> str:
        beam_state = self.state.beams.get(self.config.instrument_target)
        return beam_state.power if beam_state else "unknown"

    async def _check_frame_progress(self, time_now: datetime) -> None:
        """Detect vetoed or stalled frame collection (called roughly every 60s).

        Only meaningful while a run is active — between runs, frame counts are
        naturally static, which would otherwise look identical to a stall.
        """
        if not self.state.run_name:
            return

        good = self.state.current_good_frames
        raw = self.state.current_raw_frames
        good_moved = self.state.last_check_good >= 0 and good > self.state.last_check_good
        raw_moved = self.state.last_check_raw >= 0 and raw > self.state.last_check_raw
        self.state.last_check_good = good
        self.state.last_check_raw = raw

        if raw_moved and not good_moved:
            if not self.state.veto_warned:
                self.state.veto_warned = True
                notification = frames_vetoed(time_now)
                logger.info(f"Veto Warning: {notification.to_plain_text()}")
                await self.experiment_channel.broadcast(notification)
        elif good_moved:
            self.state.veto_warned = False

        if not good_moved and not raw_moved:
            if self.state.frames_stalled_since is None:
                self.state.frames_stalled_since = time_now
            stalled_for = time_now - self.state.frames_stalled_since
            if (
                stalled_for >= timedelta(minutes=self.config.stall_minutes)
                and self._instrument_beam_state() != "off"
                and not self.state.stall_warned
            ):
                self.state.stall_warned = True
                notification = frames_stalled(self.config.instrument_target, stalled_for, time_now)
                logger.info(f"Stall Warning: {notification.to_plain_text()}")
                await self.experiment_channel.broadcast(notification)
        else:
            self.state.frames_stalled_since = None
            self.state.stall_warned = False

    async def _frame_check_loop(self, stop_event: Optional[asyncio.Event] = None) -> None:
        while stop_event is None or not stop_event.is_set():
            if stop_event is not None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=FRAME_CHECK_INTERVAL)
                except asyncio.TimeoutError:
                    pass
                if stop_event.is_set():
                    break
            else:
                await asyncio.sleep(FRAME_CHECK_INTERVAL)

            await self._check_frame_progress(datetime.now(timezone.utc))
        logger.warning("Frame check loop quit")

    def request_reconnect(self) -> bool:
        if self._force_reconnect.is_set():
            return False
        self._force_reconnect.set()
        ws = self._current_ws
        if ws is not None:
            asyncio.create_task(ws.close())
        return True

    async def run(self, stop_event: Optional[asyncio.Event] = None):
        if not self.data_url:
            logger.warning("No WebSocket URL provided. Beam monitor will not run.")
            return
        try:
            await asyncio.gather(
                self._run_loop(stop_event),
                self._frame_check_loop(stop_event),
            )
        finally:
            self.change_aggregator.cancel_all()

    async def _run_loop(self, stop_event: Optional[asyncio.Event] = None):
        subscribe_msg = json.dumps({
            "type": "subscribe",
            "pvs": list(self.pv_to_beam.keys()) + [self.counts_pv, self.run_name_pv],
        })

        logger.info(f"Beam Monitor started. Connecting to {self.data_url}...")

        while stop_event is None or not stop_event.is_set():
            try:
                async with websockets.connect(self.data_url) as ws:
                    logger.info("WebSocket connected.")
                    if self.sink:
                        self.sink.update_health("beam", "connected")
                    await ws.send(subscribe_msg)
                    self._current_ws = ws

                    while True:
                        recv_task = asyncio.create_task(ws.recv())
                        reconnect_task = asyncio.create_task(self._force_reconnect.wait())

                        tasks = {recv_task, reconnect_task}
                        stop_task = None

                        if stop_event is not None:
                            stop_task = asyncio.create_task(stop_event.wait())
                            tasks.add(stop_task)

                        try:
                            done, pending = await asyncio.wait(
                                tasks,
                                return_when=asyncio.FIRST_COMPLETED,
                            )

                            # Prioritize shutdown/reconnection if multiple tasks finish together.
                            if stop_task is not None and stop_task in done:
                                logger.warning("Deep Beam Loop Quit")
                                return

                            if reconnect_task in done:
                                self._force_reconnect.clear()
                                logger.info("Beam reconnect requested by operator.")

                                if self.sink:
                                    self.sink.update_health("beam", "reconnecting")

                                break

                            # recv_task completed.
                            raw_msg = recv_task.result()

                        except websockets.ConnectionClosedOK:
                            logger.warning("Websocket Closed OK")
                            break

                        finally:
                            # Never leave recv/event tasks running into the next iteration.
                            for task in tasks:
                                if not task.done():
                                    task.cancel()

                            await asyncio.gather(*tasks, return_exceptions=True)

                        try:
                            data = json.loads(raw_msg)
                            if data.get("type") == "update":
                                await self._handle_update(data)
                        except json.JSONDecodeError as exc:
                            logger.debug("Failed to decode WS message: %s", exc)

            except asyncio.CancelledError:
                logger.warning(f"Beam Loop Cancelled")
                return
            except (websockets.exceptions.ConnectionClosed, OSError):
                if stop_event and stop_event.is_set():
                    logger.warning(f"Beam Loop Quit")
                    return
                if self.sink:
                    self.sink.update_health("beam", "disconnected")
                logger.warning(f"WebSocket Connection lost. Reconnecting in {self.config.beam_reconnect_interval}s...")
                await asyncio.sleep(self.config.beam_reconnect_interval)
            except Exception as e:
                if stop_event and stop_event.is_set():
                    logger.warning(f"Error Beam Loop Quit")
                    return
                if self.sink:
                    self.sink.update_health("beam", "error")
                logger.error(f"Unexpected error in BeamMonitor: {e}. Reconnecting in {self.config.beam_reconnect_interval}s...")
                await asyncio.sleep(self.config.beam_reconnect_interval)
            finally:
                self._current_ws = None
        logger.warning(f"Fallthrough Beam Loop Quit")
        return
