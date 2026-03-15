import asyncio
import json
import base64
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import websockets

from isis_monitor.config import AppConfig
from isis_monitor.notifiers import NotificationChannel
from isis_monitor.protocols import TUIProtocol

logger = logging.getLogger(__name__)

@dataclass
class BeamTarget:
    """Describes one accelerator beam target and how to handle its updates."""
    state_key: str      # Key into MonitorState.beams
    channel_label: str  # Passed to broadcast() and TUI as the channel name
    display_name: str   # Human-readable name used in log/notification messages

BEAM_TARGETS: List[BeamTarget] = [
    BeamTarget("TS1",  "TS1",   "TS1"),
    BeamTarget("TS2",  "TS2",   "TS2"),
    BeamTarget("Muon", "Muons", "Muon"),
]


@dataclass
class BeamState:
    """Per-beam runtime state."""
    current: float = -1.0
    power: str = ""


class MonitorState:
    """Holds the runtime state of the beam monitor."""
    def __init__(self):
        self.beams: Dict[str, BeamState] = {
            bt.state_key: BeamState() for bt in BEAM_TARGETS
        }
        self.run_name: str = ""
        self.current_counts: float = -1.0
        self.end_notified: bool = False


class BeamMonitor:
    def __init__(
        self,
        config: AppConfig,
        beam_channel: NotificationChannel,
        experiment_channel: NotificationChannel,
        counts_target: float,
        tui: Optional[TUIProtocol] = None,
    ):
        self.config = config
        self.data_url = config.isis_websocket_url
        self.counts_pv = config.counts_pv
        self.run_name_pv = config.run_name_pv
        self.beam_channel = beam_channel
        self.experiment_channel = experiment_channel
        self.counts_target = counts_target
        self.tui = tui
        self.state = MonitorState()

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
        prev_state = self.state.beams[bt.state_key].power

        if new_state != prev_state:
            msg = (
                f"{time_now}: {bt.display_name} Beam is now {new_state}. "
                f"Current: {beam_val:.3f} uA"
            )
            logger.info(f"\nState Change: {msg}")
            await self.beam_channel.broadcast(msg, bt.channel_label)

        self.state.beams[bt.state_key].current = beam_val
        self.state.beams[bt.state_key].power = new_state

    async def _handle_update(self, message: Dict[str, Any]):
        """Dispatch WebSocket update messages."""
        time_now = datetime.now()

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
                    msg = f"{time_now}: Detected new run start. {name}"
                    logger.info(f"\nNew Run: {msg}")
                    await self.experiment_channel.broadcast(msg)
                    self.state.current_counts = 0

                self.state.run_name = name

            case {"pv": pv, "text": text_val} if pv == self.counts_pv:
                if not text_val or (
                    isinstance(text_val, str) and text_val.lower() == "nan"
                ):
                    return
                try:
                    counts = float(text_val.split("/")[1])
                except (IndexError, ValueError) as e:
                    logger.warning(f"Failed to parse counts from '{text_val}': {e}")
                    return

                self.state.current_counts = counts

                if self.state.end_notified and counts < (self.counts_target - 25):
                    self.state.end_notified = False

                if counts > self.counts_target and not self.state.end_notified:
                    msg = f"{time_now}: {self.state.run_name} about to finish"
                    logger.info(f"\nTarget Reached: {msg}")
                    await self.experiment_channel.broadcast(msg)
                    self.state.end_notified = True

        if self.tui:
            for bt in BEAM_TARGETS:
                state = self.state.beams[bt.state_key]
                self.tui.update_beam_state(bt.channel_label, state.current, state.power)

    async def run(self, stop_event: Optional[asyncio.Event] = None):
        subscribe_msg = json.dumps({
            "type": "subscribe",
            "pvs": list(self.pv_to_beam.keys()) + [self.counts_pv, self.run_name_pv],
        })

        if not self.data_url:
            logger.warning("No WebSocket URL provided. Beam monitor will not run.")
            return

        logger.info(f"Beam Monitor started. Connecting to {self.data_url}...")

        while stop_event is None or not stop_event.is_set():
            try:
                async with websockets.connect(self.data_url) as ws:
                    logger.info("WebSocket connected.")
                    await ws.send(subscribe_msg)

                    async for raw_msg in ws:
                        if stop_event and stop_event.is_set():
                            return
                        try:
                            data = json.loads(raw_msg)
                            if data.get("type") == "update":
                                await self._handle_update(data)
                        except json.JSONDecodeError:
                            pass

            except asyncio.CancelledError:
                return
            except (websockets.exceptions.ConnectionClosed, OSError):
                if stop_event and stop_event.is_set():
                    return
                logger.warning(f"WebSocket Connection lost. Reconnecting in {self.config.beam_reconnect_interval}s...")
                await asyncio.sleep(self.config.beam_reconnect_interval)
            except Exception as e:
                if stop_event and stop_event.is_set():
                    return
                logger.error(f"Unexpected error in BeamMonitor: {e}. Reconnecting in {self.config.beam_reconnect_interval}s...")
                await asyncio.sleep(self.config.beam_reconnect_interval)
