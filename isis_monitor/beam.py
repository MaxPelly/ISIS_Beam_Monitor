import asyncio
import contextlib
import json
import logging
import math
import socket
import ssl
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import websockets

from isis_monitor.config import AppConfig, BeamTargetConfig
from isis_monitor.daemon_state import DaemonState
from isis_monitor.instlist import INSTLIST_PV, Instruments, parse_instlist
from isis_monitor.instrument import InstrumentTracker, PVHandler, on_field
from isis_monitor.messages import beam_change, fmt_duration, startup_status
from isis_monitor.notifiers import NotificationChannel

logger = logging.getLogger(__name__)

COLLECTION_CHECK_INTERVAL = 60.0
BEAM_MAX_BACKOFF = 300.0  # longest wait between retries of a persistent connection problem


def _classify_ws_error(exc: BaseException) -> Tuple[str, str]:
    """(kind, description) of a failed or lost beam connection. Only
    "transient" problems are likely to clear by themselves within seconds."""
    if isinstance(exc, websockets.InvalidStatus):  # the server answered the handshake with an error
        status = exc.response.status_code
        if status >= 500:
            return "transient", f"PVWS server error (HTTP {status})"
        return "rejected", f"PVWS rejected the connection (HTTP {status}); check [DATA] isis_websocket_url"
    if isinstance(exc, websockets.InvalidURI):
        return "config", f"invalid URL ({exc}); check [DATA] isis_websocket_url"
    # Both are OSErrors, so they're checked first. EAI_AGAIN is a temporary
    # lookup failure, as a short network outage gives.
    if isinstance(exc, ssl.SSLError):
        return "tls", f"TLS error: {exc}"
    if isinstance(exc, socket.gaierror) and exc.errno != socket.EAI_AGAIN:
        return "dns", f"can't resolve the PVWS host ({exc}); network down or wrong URL?"
    # InvalidMessage: the handshake was cut off, e.g. while PVWS restarts.
    if isinstance(exc, (websockets.ConnectionClosed, websockets.InvalidMessage, OSError)):  # OSError: incl. TimeoutError
        return "transient", f"connection lost: {str(exc) or repr(exc)}"
    if isinstance(exc, websockets.InvalidHandshake):  # e.g. too many redirects, a bad upgrade
        return "rejected", f"PVWS handshake failed ({exc}); check [DATA] isis_websocket_url"
    return "unexpected", f"unexpected error: {exc!r}"


@dataclass
class BeamState:
    """Per-beam runtime state."""
    current: float = -1.0
    power: str = ""
    since: Optional[datetime] = None  # when `power` last changed


@dataclass
class _PendingChange:
    """A not-yet-confirmed state change, awaiting the debounce window."""
    bt: BeamTargetConfig
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
        debounce_seconds: float,
    ):
        self.beam_channel = beam_channel
        self.debounce_seconds = debounce_seconds
        self._pending: Dict[str, _PendingChange] = {}
        self._recent_offs: Dict[str, datetime] = {}

    def queue_change(self, change: _PendingChange) -> None:
        key = change.bt.name
        existing = self._pending.get(key)
        if existing is not None:
            # Already pending — just update the latest reading; the original
            # timer (and the original prev_state/prev_since) still applies.
            existing.new_state = change.new_state
            existing.beam_val = change.beam_val
            return

        change.task = asyncio.create_task(self._flush_after_delay(key))
        self._pending[key] = change

    async def _flush_after_delay(self, state_key: str) -> None:
        await asyncio.sleep(self.debounce_seconds)
        try:
            await self._flush(state_key)
        except Exception:
            logger.exception(f"{state_key}: failed to send the beam change")

    async def _flush(self, state_key: str) -> None:
        pending = self._pending.pop(state_key, None)
        if pending is None:
            return

        if pending.new_state == pending.prev_state:
            logger.debug(f"{pending.bt.name}: flapped back to {pending.prev_state}, dropping.")
            return

        trip_note = ""
        if pending.new_state == "off":
            also_off = [
                other.bt.name
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
            pending.bt.name,
            prev_state=pending.prev_state,
            new_state=pending.new_state,
            beam_val=pending.beam_val,
            prev_val=pending.prev_val,
            high_threshold=pending.high_threshold,
            time_in_prev_state=pending.started_at - pending.prev_since,
            time_now=pending.started_at,
            trip_note=trip_note,
        )
        logger.info(f"State Change: {notification.to_summary()}")
        self.beam_channel.broadcast(notification)

    def pending(self, state_key: str) -> Optional[_PendingChange]:
        """The unconfirmed change for a target, if one is waiting to flush."""
        return self._pending.get(state_key)

    def cancel_all(self) -> None:
        """Cancel any outstanding flush timers, e.g. on shutdown."""
        for pending in self._pending.values():
            if pending.task is not None:
                pending.task.cancel()
        self._pending.clear()


class BeamMonitor:
    def __init__(
        self,
        config: AppConfig,
        beam_channel: NotificationChannel,
        experiment_channel: NotificationChannel,
        sink: Optional[DaemonState] = None,
    ):
        self.config = config
        self.data_url = config.isis_websocket_url
        self.beam_channel = beam_channel
        self.sink = sink
        self.beams: Dict[str, BeamState] = {bt.name: BeamState() for bt in config.beams}
        self.change_aggregator = BeamChangeAggregator(beam_channel, config.debounce_seconds)
        self._force_reconnect = asyncio.Event()
        self._current_ws = None
        self._close_task: Optional[asyncio.Task] = None

        self.instruments: Dict[str, InstrumentTracker] = {
            inst.name: InstrumentTracker(
                inst, experiment_channel, self._beam_power, config.stall_minutes,
                finish_warning_minutes=config.finish_warning_minutes,
                sink=sink, default_target=config.instrument_target,
            )
            for inst in config.instruments
        }
        self._unlisted_warned: set = set()
        # Start with the PV prefixes and stations last seen, until CS:INSTLIST comes.
        self.apply_instlist(sink.instlist if sink else {})
        self._build_routes()

    def _build_routes(self) -> None:
        # Each PV's update handler, in the order they're subscribed to:
        # CS:INSTLIST, the beams, then each instrument's (see pv_handlers).
        self.routes: Dict[str, PVHandler] = {INSTLIST_PV: on_field("b64byt", self._handle_instlist)}
        self.routes.update({
            bt.pv: on_field("value", lambda value, now, bt=bt: self._handle_beam_current(bt, value, now))
            for bt in self.config.beams
        })
        for tracker in self.instruments.values():
            self.routes.update(tracker.pv_handlers())

    def apply_instlist(self, listed: Instruments) -> Tuple[List[str], List[str]]:
        """Set each instrument's PV prefix and target station from
        CS:INSTLIST; returns the PVs to stop and start subscribing to."""
        old, new = [], []
        for name, tracker in self.instruments.items():
            if name not in listed:
                if listed and name not in self._unlisted_warned:
                    self._unlisted_warned.add(name)
                    logger.warning(f"{name} isn't in CS:INSTLIST (a typo?); keeping {tracker.instrument.pv_prefix} "
                                   "for its PVs")
                continue
            prefix, station = listed[name]
            tracker.set_station(station)
            if prefix != tracker.instrument.pv_prefix:
                logger.info(f"{name}: its PVs start {prefix}, from CS:INSTLIST")
                old += tracker.pv_handlers()
                tracker.instrument = replace(tracker.instrument, pv_prefix=prefix)
                if self._current_ws is not None:  # otherwise the connection will resubscribe it
                    tracker.resubscribed()  # the new PVs' values come in as on a reconnect
                new += tracker.pv_handlers()
        return old, new

    async def _handle_instlist(self, b64: Any, time_now: datetime) -> None:
        listed = parse_instlist(b64)
        if not listed:
            return
        if self.sink:
            self.sink.instlist = listed
        old, new = self.apply_instlist(listed)
        if new:
            self._build_routes()
            if self._current_ws is not None:
                await self._current_ws.send(json.dumps({"type": "clear", "pvs": old}))
                await self._current_ws.send(json.dumps({"type": "subscribe", "pvs": new}))

    @staticmethod
    def _parse_current(value: Any) -> Optional[float]:
        """The beam current, or None for NaN (PVWS's "no value yet"), None or
        junk, which must not be read as the beam going off."""
        try:
            val = float(value)
        except (ValueError, TypeError):
            return None
        return val if math.isfinite(val) else None

    @staticmethod
    def _get_power_label(beam_uA: float, boundaries: tuple) -> str:
        if beam_uA <= boundaries[0]: return "off"
        if beam_uA < boundaries[1]: return "low"
        if beam_uA < boundaries[2]: return "medium"
        return "high"

    async def _handle_beam_current(
        self, bt: BeamTargetConfig, raw_val: Any, time_now: datetime
    ):
        """Handle a beam-current value update for a single target."""
        beam_val = self._parse_current(raw_val)
        if beam_val is None:
            # No notification either way, but the last reading can't be
            # trusted: "unknown" keeps it out of the history (and uptime).
            logger.debug(f"Unreadable {bt.name} beam current {raw_val!r}")
            if self.sink:
                self.sink.update_beam_state(bt.name, self.beams[bt.name].current, "unknown")
            return
        new_state = self._get_power_label(beam_val, bt.boundaries)
        beam_state = self.beams[bt.name]
        prev_state = beam_state.power
        prev_val = beam_state.current
        prev_since = beam_state.since

        if new_state != prev_state:
            if prev_state == "":
                # First reading for this target — always send immediately, never debounced.
                notification = startup_status(
                    bt.name, state=new_state, beam_val=beam_val, time_now=time_now,
                )
                logger.info(f"Startup: {notification.to_summary()}")
                self.beam_channel.broadcast(notification)
                beam_state.since = time_now
            else:
                pending = self.change_aggregator.pending(bt.name)
                self.change_aggregator.queue_change(_PendingChange(
                    bt, prev_state, prev_val, prev_since or time_now,
                    new_state, beam_val, bt.boundaries[2], time_now,
                ))
                if pending is not None and new_state == pending.prev_state:
                    # Back where it was before a flicker the debounce will drop,
                    # so the time in that state carries on rather than restarting.
                    beam_state.since = pending.prev_since
                else:
                    beam_state.since = time_now

        beam_state.current = beam_val
        beam_state.power = new_state
        if self.sink:
            self.sink.update_beam_state(bt.name, beam_val, new_state)

    async def _handle_update(self, message: Dict[str, Any]):
        """Dispatch a WebSocket update message to its PV's handler."""
        if handler := self.routes.get(message.get("pv")):
            await handler(message, datetime.now(timezone.utc))

    def restore_instruments(self, saved: Dict[str, object]) -> None:
        """Seed each tracker from its entry in the restored snapshot."""
        for name, tracker in self.instruments.items():
            if name in saved:
                tracker.from_snapshot(saved[name])

    def instrument_views(self) -> Dict[str, dict]:
        """Each instrument's live state, for the snapshot and clients."""
        return {name: tracker.view() for name, tracker in self.instruments.items()}

    def _beam_power(self, target: str) -> str:
        beam_state = self.beams.get(target)
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
            for tracker in self.instruments.values():
                tracker.runs.cancel()

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
        logger.info(f"Beam Monitor started. Connecting to {self.data_url}...")
        interval = self.config.beam_reconnect_interval

        backoff = 0.0  # grows while a persistent (not "transient") problem lasts
        # Repeats of the last logged kind of failure are only logged at debug,
        # with one summary line once the connection is back.
        last_kind = ""
        failed_attempts, down_since = 0, None
        while True:
            connected = False
            kind, detail, error = "transient", "closed", None
            try:
                async with websockets.connect(self.data_url) as ws:
                    connected = True
                    self._current_ws = ws
                    # A reconnect requested during the handshake is satisfied by
                    # this new connection; left set, it would make later
                    # requests no-ops until the connection next dropped.
                    self._force_reconnect.clear()
                    if failed_attempts:
                        down_for = fmt_duration(datetime.now(timezone.utc) - down_since)
                        logger.info(f"WebSocket connected after {failed_attempts} failed attempt(s) over {down_for}.")
                    else:
                        logger.info("WebSocket connected.")
                    backoff, last_kind, failed_attempts, down_since = 0.0, "", 0, None
                    self._set_health("connected")
                    for tracker in self.instruments.values():
                        tracker.resubscribed()
                    await ws.send(json.dumps({"type": "subscribe", "pvs": list(self.routes)}))
                    async for raw in ws:
                        await self._handle_message(raw)
            except Exception as exc:
                kind, detail = _classify_ws_error(exc)
                error = exc if kind == "unexpected" else None  # log its traceback
            finally:
                self._current_ws = None
            if not connected:
                failed_attempts += 1
            down_since = down_since or datetime.now(timezone.utc)

            # Retrying something that won't fix itself soon every few seconds
            # just spams the log, so those waits double up to BEAM_MAX_BACKOFF.
            if kind == "transient":
                delay = interval
            else:
                backoff = min(max(backoff * 2, interval), max(BEAM_MAX_BACKOFF, interval))
                delay = backoff
            if not self._force_reconnect.is_set():
                # "error" flags a problem that likely needs someone to fix it.
                self._set_health("disconnected" if kind == "transient" else "error")
                message = f"WebSocket {detail}; reconnecting in {delay:g}s"
                if kind == last_kind:
                    logger.debug(message)
                elif kind == "transient":
                    logger.warning(message)
                else:
                    logger.error(message, exc_info=error)
                last_kind = kind
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._force_reconnect.wait(), timeout=delay)
            if self._force_reconnect.is_set():
                self._force_reconnect.clear()
                # The operator may have fixed the problem: start afresh.
                backoff, last_kind = 0.0, ""
                logger.info("Beam reconnect requested by operator.")
                self._set_health("reconnecting")
