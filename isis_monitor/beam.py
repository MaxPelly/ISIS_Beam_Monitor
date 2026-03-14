import asyncio
import json
import base64
import logging
import math
from datetime import datetime
from typing import Any, Dict

import websockets

from isis_monitor.config import AppConfig
from isis_monitor.notifiers import NotificationChannel

logger = logging.getLogger(__name__)

# --- Constants ---
PV_TS1_BEAM_CURRENT = "AC:TS1:BEAM:CURR"
PV_TS2_BEAM_CURRENT = "AC:TS2:BEAM:CURR"
PV_MUON_BEAM_CURRENT = "AC:MUON:BEAM:CURR"
PV_COUNTS = "IN:PEARL:CS:DASHBOARD:TAB:2:1:VALUE"
PV_RUN_NAME = "IN:PEARL:DAE:WDTITLE"

# --- Beam State Cutoffs ---
BEAM_BOUNDERIES = {
    "TS1": (0, 50, 100),
    "TS2": (0, 12, 25),
    "Muon": (0, 12, 25),
}

class MonitorState:
    """Holds the runtime state of the beam monitor."""
    def __init__(self):
        self.TS1_beam_current: float = -1
        self.TS1_beam_power_state: str = ""

        self.TS2_beam_current: float = -1
        self.TS2_beam_power_state: str = ""

        self.muon_beam_current: float = -1
        self.muon_beam_power_state: str = ""    
        
        self.run_name: str = ""
        self.current_counts: float = -1.0
        self.end_notified: bool = False

class BeamMonitor:
    def __init__(self, config: AppConfig, beam_channel: NotificationChannel, 
                 experiment_channel: NotificationChannel, counts_target: float):
        
        self.data_url = config.isis_websocket_url
        self.beam_channel = beam_channel
        self.experiment_channel = experiment_channel
        self.counts_target = counts_target
        self.state = MonitorState()

    def _safe_float(self, value: Any) -> float:
        """Safely converts value to float. Returns 0.0 if value is 'NaN', None, or malformed."""
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
        bounderies = BEAM_BOUNDERIES[beam]        
        if beam_uA <= bounderies[0]: return "off"
        if beam_uA < bounderies[1]: return "low"
        if beam_uA < bounderies[2]: return "medium"
        return "high"

    async def _handle_update(self, message: Dict[str, Any]):
        """Dispatch update messages using Pattern Matching."""
        time_now = datetime.now()
        
        match message:
            case {"pv": pv, "value": raw_val}:
                if pv == PV_TS1_BEAM_CURRENT:
                    beam_val = self._safe_float(raw_val)
                    new_state = self._get_power_label(beam_val, "TS1")

                    if new_state != self.state.TS1_beam_power_state:
                        msg = f"{time_now}: TS1 Beam is now {new_state}. Current: {beam_val:.3f} uA"
                        logger.info(f"\nState Change: {msg}")
                        await self.beam_channel.broadcast(msg, "TS1")
                    
                    self.state.TS1_beam_current = beam_val
                    self.state.TS1_beam_power_state = new_state
                    
                elif pv == PV_TS2_BEAM_CURRENT:
                    beam_val = self._safe_float(raw_val)
                    new_state = self._get_power_label(beam_val, "TS2")

                    if new_state != self.state.TS2_beam_power_state:
                        msg = f"{time_now}: TS2 Beam is now {new_state}. Current: {beam_val:.3f} uA"
                        logger.info(f"\nState Change: {msg}")
                        await self.beam_channel.broadcast(msg, "TS2")
                    
                    self.state.TS2_beam_current = beam_val
                    self.state.TS2_beam_power_state = new_state
                    
                elif pv == PV_MUON_BEAM_CURRENT:
                    beam_val = self._safe_float(raw_val)
                    new_state = self._get_power_label(beam_val, "Muon")

                    if new_state != self.state.muon_beam_power_state:
                        msg = f"{time_now}: Muon Beam is now {new_state}. Current: {beam_val:.3f} uA"
                        logger.info(f"\nState Change: {msg}")
                        await self.beam_channel.broadcast(msg, "Muons")
                    
                    self.state.muon_beam_current = beam_val
                    self.state.muon_beam_power_state = new_state

            case {"pv": PV_RUN_NAME, "b64byt": b64_data}:
                if not b64_data or (isinstance(b64_data, str) and b64_data.lower() == "nan"):
                    return

                try:
                    name = base64.b64decode(b64_data).decode().strip("\x00")
                except Exception:
                    return
                    
                if self.state.run_name and self.state.run_name != name:
                    msg = f"{time_now}: Detected new run start. {name}"
                    logger.info(f"\nNew Run: {msg}")
                    await self.experiment_channel.broadcast(msg)
                    self.state.current_counts = 0
                
                self.state.run_name = name

            case {"pv": PV_COUNTS, "text": text_val}:
                if not text_val or (isinstance(text_val, str) and text_val.lower() == "nan"):
                    return

                try:
                    counts = float(text_val.split("/")[1])
                except (IndexError, ValueError):
                    return

                self.state.current_counts = counts
                
                if self.state.end_notified and (counts < (self.counts_target - 25)):
                    self.state.end_notified = False

                if (counts > self.counts_target) and not self.state.end_notified:
                    msg = f"{time_now}: {self.state.run_name} about to finish"
                    logger.info(f"\nTarget Reached: {msg}")
                    await self.experiment_channel.broadcast(msg)
                    self.state.end_notified = True

        # Formatting with fixed widths and padding to prevent trailing characters and UI jitter
        status = (
            f"{time_now:%H:%M:%S} | "
            f"TS1: {self.state.TS1_beam_current:7.3f} uA ({self.state.TS1_beam_power_state:<6}) | "
            f"TS2: {self.state.TS2_beam_current:7.3f} uA ({self.state.TS2_beam_power_state:<6}) | "
            f"Muons: {self.state.muon_beam_current:7.3f} uA ({self.state.muon_beam_power_state:<6})"
        )
        print(f"\r{status:<120}", end="", flush=True)

    async def run(self):
        subscribe_msg = json.dumps({
            "type": "subscribe",
            "pvs": [PV_TS1_BEAM_CURRENT, PV_TS2_BEAM_CURRENT, PV_MUON_BEAM_CURRENT, PV_COUNTS, PV_RUN_NAME]
        })

        if not self.data_url:
            logger.warning("No WebSocket URL provided. Beam monitor will not run.")
            return

        logger.info(f"Beam Monitor started. Connecting to {self.data_url}...")

        while True:
            try:
                async with websockets.connect(self.data_url) as ws:
                    logger.info("WebSocket connected.")
                    await ws.send(subscribe_msg)
                    
                    async for raw_msg in ws:
                        try:
                            data = json.loads(raw_msg)
                            if data.get("type") == "update":
                                await self._handle_update(data)
                        except json.JSONDecodeError:
                            pass 

            except (websockets.exceptions.ConnectionClosed, OSError):
                logger.warning("WebSocket Connection lost. Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Unexpected error in BeamMonitor: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)
