#!/usr/bin/env python3
# Author: Max Pelly
# Created: 02-DEC-2025
# License: GNU AGPL 3

import asyncio
import websockets
import ssl
import json
import datetime
import requests
import base64
import argparse
import configparser
import logging
import sys
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import List, Any

# --- Configuration & Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("BeamMonitor")

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

# ==========================================
# Notification Abstraction Layer
# ==========================================

class Notifier(ABC):
    """Abstract interface for any notification method."""
    @abstractmethod
    async def send(self, message: str):
        pass

class TeamsNotifier(Notifier):
    """Sends notifications to a Microsoft Teams Webhook."""
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def _create_payload(self, message: str, channel: str) -> dict:
        return {
            "type": "message",
            "summary": message,
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "summary": message,
                    "channel": channel,
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": [
                        {"type": "TextBlock", "size": "Medium", "weight": "Bolder", "text": "Beam Update"},
                        {"type": "TextBlock", "text": message, "wrap": True}
                    ]
                }
            }]
        }

    async def send(self, message: str, channel: str):
        if not self.webhook_url:
            return

        payload = self._create_payload(message, channel)
        try:
            # Run blocking I/O in a separate thread
            await asyncio.to_thread(requests.post, self.webhook_url, json=payload)
            pass
        except Exception as e:
            logger.error(f"Failed to send Teams webhook: {e}")

class NotificationChannel:
    """
    Manages a group of notifiers for a specific topic (e.g., 'Beam' or 'Experiment').
    """
    def __init__(self, name: str):
        self.name = name
        self.notifiers: List[Notifier] = []

    def add_notifier(self, notifier: Notifier):
        self.notifiers.append(notifier)

    async def broadcast(self, message: str, channel: str):
        """Sends the message to all registered notifiers in parallel."""
        if not self.notifiers:
            return
        
        await asyncio.gather(*(n.send(message, channel) for n in self.notifiers))

# ==========================================
# Monitor Logic
# ==========================================

@dataclass
class MonitorState:
    """Holds the runtime state of the beam monitor."""
    TS1_beam_current: float = -1
    TS1_beam_power_state: str = ""

    TS2_beam_current: float = -1
    TS2_beam_power_state: str = ""

    muon_beam_current: float = -1
    muon_beam_power_state: str = ""    
    
    run_name: str = ""
    current_counts: float = -1.0
    end_notified: bool = False

class BeamMonitor:
    def __init__(self, 
                 data_url: str, 
                 beam_channel: NotificationChannel, 
                 experiment_channel: NotificationChannel, 
                 counts_target: float):
        
        self.data_url = data_url
        self.beam_channel = beam_channel
        self.experiment_channel = experiment_channel
        self.counts_target = counts_target
        self.state = MonitorState()

    def _safe_float(self, value: Any) -> float:
        """
        Safely converts value to float. 
        Returns 0.0 if value is 'NaN', None, or malformed.
        """
        if value is None:
            return 0.0
            
        try:
            # Handle string "NaN" explicitly or standard float conversion
            if isinstance(value, str) and value.strip().lower() == "nan":
                return 0.0
            
            val = float(value)
            
            # Handle actual float('nan')
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

    async def _handle_update(self, message: dict):
        """Dispatch update messages using Pattern Matching."""
        time_now = datetime.datetime.now()
        match message:
            # Case 1: Beam Current Update
            # Matches any update for beam current, captures value in raw_val
            case {"pv": pv, "value": raw_val}:
                if pv == PV_TS1_BEAM_CURRENT:
                    # Convert string/NaN to safe float
                    beam_val = self._safe_float(raw_val)
                    new_state = self._get_power_label(beam_val, "TS1")
                    

                    if new_state != self.state.TS1_beam_power_state:
                        msg = f"{time_now}: TS1 Beam is now {new_state}. Current: {beam_val:.3f} uA"
                        logger.info(f"\nState Change: {msg}")
                        await self.beam_channel.broadcast(msg, "TS1")
                    
                    self.state.TS1_beam_current = beam_val
                    self.state.TS1_beam_power_state = new_state
                    
                elif pv == PV_TS2_BEAM_CURRENT:
                    # Convert string/NaN to safe float
                    beam_val = self._safe_float(raw_val)
                    new_state = self._get_power_label(beam_val, "TS2")
                    

                    if new_state != self.state.TS2_beam_power_state:
                        msg = f"{time_now}: TS2 Beam is now {new_state}. Current: {beam_val:.3f} uA"
                        logger.info(f"\nState Change: {msg}")
                        await self.beam_channel.broadcast(msg, "TS2")
                    
                    self.state.TS2_beam_current = beam_val
                    self.state.TS2_beam_power_state = new_state
                    
                elif pv == PV_MUON_BEAM_CURRENT:
                    # Convert string/NaN to safe float
                    beam_val = self._safe_float(raw_val)
                    new_state = self._get_power_label(beam_val, "Muon")
                    

                    if new_state != self.state.muon_beam_power_state:
                        msg = f"{time_now}: Muon Beam is now {new_state}. Current: {beam_val:.3f} uA"
                        logger.info(f"\nState Change: {msg}")
                        await self.beam_channel.broadcast(msg, "Muons")
                    
                    self.state.muon_beam_current = beam_val
                    self.state.muon_beam_power_state = new_state

            # Case 2: Experiment Name Update
            case {"pv": PV_RUN_NAME, "b64byt": b64_data}:
                # If "NaN" or empty is sent for b64byt, we skip
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

            # Case 3: Counts Update
            case {"pv": PV_COUNTS, "text": text_val}:
                # If "NaN" is in the text field, we skip
                if not text_val or (isinstance(text_val, str) and text_val.lower() == "nan"):
                    return

                try:
                    # Expected format: "current/total"
                    counts = float(text_val.split("/")[1])
                except (IndexError, ValueError):
                    return

                self.state.current_counts = counts
                
                # Reset notification if we are at the start of a run
                if self.state.end_notified and (counts < (self.counts_target - 25)):
                    self.state.end_notified = False

                # Trigger notification if target reached
                if (counts > self.counts_target) and not self.state.end_notified:
                    msg = f"{time_now}: {self.state.run_name} about to finish"
                    logger.info(f"\nTarget Reached: {msg}")
                    await self.experiment_channel.broadcast(msg)
                    self.state.end_notified = True

        print(f"{time_now:%H:%M:%S}    TS1: {self.state.TS1_beam_current:.3f} uA | State: {self.state.TS1_beam_power_state}    TS2: {self.state.TS2_beam_current:.3f} uA | State: {self.state.TS2_beam_power_state}    Muons: {self.state.muon_beam_current:.3f} uA | State: {self.state.muon_beam_power_state}", end="\r", flush=True)

    async def run(self):
        """Main robust connection loop."""
        subscribe_msg = json.dumps({
            "type": "subscribe",
            "pvs": [PV_TS1_BEAM_CURRENT,  PV_TS2_BEAM_CURRENT, PV_MUON_BEAM_CURRENT]
        })

        logger.info(f"Monitor started. Connecting to {self.data_url}...")

        while True:
            try:
                # Using standard secure TLS context
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
                logger.warning("Connection lost. Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Unexpected error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

# ==========================================
# Application Entry
# ==========================================

def main():
    parser = argparse.ArgumentParser(description="ISIS Beam and Experiment Monitor")
    parser.add_argument("config", type=Path, help="Path to .ini configuration file")
    parser.add_argument("-nc", "--notify_counts", type=float, default=130, help="Counts threshold for notification")
    args = parser.parse_args()

    if not args.config.exists():
        logger.critical(f"Config file not found: {args.config}")
        sys.exit(1)

    config = configparser.ConfigParser(interpolation=None)
    config.read(args.config)

    # 1. Setup Data URL
    data_url = config.get("DATA", "isis_websocket_url", fallback="")

    # 2. Setup Notification Channels
    beam_channel = NotificationChannel("Beam Updates")
    exp_channel = NotificationChannel("Experiment Updates")

    # 3. Configure Teams Notifiers
    teams_beam_url = config.get("WEBHOOKS", "beam_teams_url", fallback="")
    teams_exp_url = config.get("WEBHOOKS", "experiment_teams_url", fallback="")

    if teams_beam_url:
        beam_channel.add_notifier(TeamsNotifier(teams_beam_url))
    if teams_exp_url:
        exp_channel.add_notifier(TeamsNotifier(teams_exp_url))

    # 4. Initialize and Run Monitor
    monitor = BeamMonitor(data_url, beam_channel, exp_channel, args.notify_counts)
    
    try:
        asyncio.run(monitor.run())
    except KeyboardInterrupt:
        print("\nStopping monitor...")

if __name__ == "__main__":
    main()
