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
        self.server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.socket_path), limit=SERVER_LINE_LIMIT
        )
        os.chmod(self.socket_path, 0o600)

    async def stop(self) -> None:
        self._closing = True
        if self.server is not None:
            self.server.close()
            # Since Python 3.12.1, wait_closed() also waits for every open
            # connection, so a still-attached TUI would block shutdown forever.
            for writer in list(self._clients):
                writer.close()
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
                await writer.drain()
        except ConnectionError:
            pass
        # Dropped for falling behind, or the peer is gone: closing the
        # connection makes the client reconnect and resync from a snapshot.
        writer.close()


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

    async def request(self, payload: dict) -> dict:
        if self.writer is None:
            raise RuntimeError("IPC client is not connected")
        self.writer.write(_encode(payload))
        await self.writer.drain()
        return await self._take(self._responses)

    async def iter_events(self):
        if self.writer is None:
            raise RuntimeError("IPC client is not connected")
        while True:
            yield await self._take(self._events)
