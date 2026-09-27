"""Newline-delimited JSON over a UNIX socket between the daemon and its clients.

Every reply carries "ok" and "version". Once a client sends
`subscribe_updates`, pushed events (which carry an "event" key) are
interleaved with replies on the same connection.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable, Optional, Set

from isis_monitor.daemon_state import DaemonState

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
# Requests are tiny; replies (the history snapshot) can be large.
SERVER_LINE_LIMIT = 64 * 1024
# How long stop() lets clients take already-buffered replies before cutting
# off any that aren't reading (e.g. a suspended TUI), so shutdown can't hang.
STOP_FLUSH_TIMEOUT = 1.0
# A subscriber that takes no events for this long (e.g. a suspended TUI) is
# disconnected; it reconnects and resyncs from a snapshot when it resumes.
SUBSCRIBER_DRAIN_TIMEOUT = 30.0
CLIENT_LINE_LIMIT = 16 * 1024 * 1024


def _encode(payload: dict) -> bytes:
    return (json.dumps(payload) + "\n").encode()


class IPCServer:
    def __init__(
        self,
        socket_path: Path,
        state: DaemonState,
        command_handler: Callable[[str], Awaitable[dict]],
        config_handler: Optional[Callable[[str, dict], Awaitable[dict]]] = None,
    ):
        """`config_handler(method, request)` serves get_config and update_config."""
        self.socket_path = Path(socket_path)
        self.state = state
        self.command_handler = command_handler
        self.config_handler = config_handler
        self.server: Optional[asyncio.base_events.Server] = None
        self._clients: Set[asyncio.StreamWriter] = set()
        self._closing = False

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        # Created owner-only from the start (it can rewrite the config), not
        # left open to others until the chmod below.
        old_umask = os.umask(0o177)
        try:
            self.server = await asyncio.start_unix_server(
                self._handle_client, path=str(self.socket_path), limit=SERVER_LINE_LIMIT
            )
        finally:
            os.umask(old_umask)
        os.chmod(self.socket_path, 0o600)

    async def stop(self) -> None:
        self._closing = True
        if self.server is not None:
            self.server.close()
            # Since Python 3.12.1, wait_closed() also waits for every open
            # connection, so a still-attached TUI would block shutdown forever.
            # close() still flushes buffered output first (e.g. the reply to
            # the update_config that triggered a restart), which never ends
            # if the client has stopped reading, so those get aborted.
            writers = list(self._clients)
            for writer in writers:
                writer.close()
            try:
                await asyncio.wait_for(asyncio.shield(self.server.wait_closed()), STOP_FLUSH_TIMEOUT)
            except asyncio.TimeoutError:
                for writer in writers:
                    writer.transport.abort()
                await self.server.wait_closed()
        self.socket_path.unlink(missing_ok=True)

    async def _reply(self, req: dict) -> dict:
        method = req.get("method")
        if method == "get_snapshot":
            return {"snapshot": self.state.snapshot()}
        if method == "get_history":
            limit = req.get("limit")
            return {"history": self.state.get_history_snapshot(limit if isinstance(limit, int) else None)}
        if method == "get_logs":
            return {"logs": self.state.get_logs_snapshot()}
        if method == "command":
            return {"result": await self.command_handler(str(req.get("name", "")))}
        if method in ("get_config", "update_config") and self.config_handler is not None:
            return await self.config_handler(method, req)
        return {"ok": False, "error": "unknown_method"}

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closing:  # accepted just before stop(), but not yet tracked by it
            writer.close()
            return
        self._clients.add(writer)
        queue: Optional[asyncio.Queue] = None
        forwarder: Optional[asyncio.Task] = None
        try:
            while line := await reader.readline():
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    reply = {"ok": False, "error": "invalid_json"}
                else:
                    if not isinstance(req, dict):
                        reply = {"ok": False, "error": "invalid_request"}
                    elif req.get("method") == "subscribe_updates":
                        if queue is None:
                            queue = self.state.subscribe()
                            forwarder = asyncio.create_task(self._forward_events(queue, writer))
                        reply = {"subscribed": True}
                    else:
                        try:
                            reply = await self._reply(req)
                        except Exception as exc:
                            logger.exception(f"IPC request {req.get('method')!r} failed")
                            reply = {"ok": False, "error": "internal_error", "detail": str(exc)}
                writer.write(_encode({"ok": True, **reply, "version": PROTOCOL_VERSION}))
                await writer.drain()
        except (ConnectionError, ValueError):
            pass  # client went away, or sent a line over SERVER_LINE_LIMIT
        finally:
            self._clients.discard(writer)
            if forwarder is not None:
                forwarder.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await forwarder
            if queue is not None:
                self.state.unsubscribe(queue)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _forward_events(self, queue: asyncio.Queue, writer: asyncio.StreamWriter) -> None:
        try:
            while (ev := await queue.get()) is not None:
                writer.write(_encode({
                    "ok": True, "version": PROTOCOL_VERSION, "event": ev.event, "payload": ev.payload,
                }))
                # Bounded: a client that stops reading would otherwise leave
                # this blocked here, never seeing the drop sentinel above.
                await asyncio.wait_for(writer.drain(), SUBSCRIBER_DRAIN_TIMEOUT)
        except (ConnectionError, asyncio.TimeoutError):
            pass
        # Dropped for falling behind, not reading, or the peer is gone: dropping the
        # connection makes the client reconnect and resync from a snapshot.
        # Aborted rather than closed, since close() would first wait to flush
        # stale events to a client that isn't reading them.
        writer.transport.abort()


class IPCClient:
    """One background task owns the reader and routes each line to either
    the reply queue or the event queue, so request() and iter_events() can
    be used concurrently without two coroutines calling readline() at once.
    """

    def __init__(self, socket_path: Path):
        self.socket_path = Path(socket_path)
        self.writer: Optional[asyncio.StreamWriter] = None
        self._responses: asyncio.Queue = asyncio.Queue()
        self._events: asyncio.Queue = asyncio.Queue()
        self._read_task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        reader, self.writer = await asyncio.open_unix_connection(
            str(self.socket_path), limit=CLIENT_LINE_LIMIT
        )
        self._read_task = asyncio.create_task(self._read_loop(reader))

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        end: object = ConnectionError("Daemon closed IPC connection")
        try:
            while line := await reader.readline():
                msg = json.loads(line)
                (self._events if "event" in msg else self._responses).put_nowait(msg)
        except Exception as exc:
            end = exc
        self._responses.put_nowait(end)
        self._events.put_nowait(end)

    async def close(self) -> None:
        if self._read_task is not None:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task
            self._read_task = None
            # Cancelled, the read loop didn't post its end marker: wake anyone
            # else still waiting on this client rather than leave them hanging.
            end = ConnectionError("IPC client closed")
            self._responses.put_nowait(end)
            self._events.put_nowait(end)
        if self.writer is not None:
            self.writer.close()
            with contextlib.suppress(ConnectionError):
                await self.writer.wait_closed()
            self.writer = None

    @staticmethod
    async def _take(queue: asyncio.Queue):
        msg = await queue.get()
        if isinstance(msg, BaseException):
            queue.put_nowait(msg)  # so later callers fail too, instead of hanging
            raise msg
        return msg

    async def request(self, payload: dict, timeout: Optional[float] = None) -> dict:
        """Send `payload` and return its reply. On timeout the client is
        closed, since a late reply would otherwise be taken as the answer to
        the next request, and the built-in TimeoutError (an OSError, so it
        counts as a lost connection) is raised."""
        if self.writer is None:
            raise RuntimeError("IPC client is not connected")
        self.writer.write(_encode(payload))
        try:
            return await asyncio.wait_for(self._send_and_take(), timeout)
        except asyncio.TimeoutError:  # not the built-in TimeoutError before Python 3.11
            await self.close()
            raise TimeoutError(f"No reply to {payload.get('method')!r} within {timeout}s") from None

    async def _send_and_take(self) -> dict:
        await self.writer.drain()
        return await self._take(self._responses)

    async def iter_events(self):
        if self.writer is None:
            raise RuntimeError("IPC client is not connected")
        while True:
            yield await self._take(self._events)
