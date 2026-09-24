from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Awaitable, Callable, Optional

from isis_monitor.daemon_state import DaemonEvent, DaemonState

PROTOCOL_VERSION = 1


class IPCServer:
    def __init__(
        self,
        socket_path: Path,
        state: DaemonState,
        command_handler: Callable[[str], Awaitable[dict]],
    ):
        self.socket_path = Path(socket_path)
        self.state = state
        self.command_handler = command_handler
        self.server: Optional[asyncio.base_events.Server] = None

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self.server = await asyncio.start_unix_server(self._handle_client, path=str(self.socket_path), limit=1024*1024*10)
        os.chmod(self.socket_path, 0o600)

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        if self.socket_path.exists():
            self.socket_path.unlink()

    async def _send(self, writer: asyncio.StreamWriter, payload: dict) -> None:
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        queue = None
        subscription_task = None
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    req = json.loads(line.decode())
                except json.JSONDecodeError:
                    await self._send(
                        writer,
                        {"ok": False, "error": "invalid_json", "version": PROTOCOL_VERSION},
                    )
                    continue

                method = req.get("method")
                if method == "get_snapshot":
                    await self._send(
                        writer,
                        {
                            "ok": True,
                            "version": PROTOCOL_VERSION,
                            "snapshot": self.state.snapshot(),
                        },
                    )
                elif method == "get_history":
                    await self._send(
                        writer,
                        {
                            "ok": True,
                            "version": PROTOCOL_VERSION,
                            "history": self.state.get_history_snapshot(),
                        },
                    )
                elif method == "get_logs":
                    await self._send(
                        writer,
                        {
                            "ok": True,
                            "version": PROTOCOL_VERSION,
                            "logs": self.state.get_logs_snapshot(),
                        },
                    )
                elif method == "subscribe_updates":
                    if queue is None:
                        queue = self.state.subscribe()
                        subscription_task = asyncio.create_task(
                            self._forward_events(queue, writer)
                        )
                    await self._send(
                        writer,
                        {"ok": True, "version": PROTOCOL_VERSION, "subscribed": True},
                    )
                elif method == "command":
                    command = str(req.get("name", ""))
                    result = await self.command_handler(command)
                    await self._send(
                        writer,
                        {"ok": True, "version": PROTOCOL_VERSION, "result": result},
                    )
                else:
                    await self._send(
                        writer,
                        {
                            "ok": False,
                            "version": PROTOCOL_VERSION,
                            "error": "unknown_method",
                        },
                    )
        finally:
            if subscription_task:
                subscription_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await subscription_task
            if queue is not None:
                self.state.unsubscribe(queue)
            writer.close()
            await writer.wait_closed()

    async def _forward_events(self, queue: asyncio.Queue, writer: asyncio.StreamWriter) -> None:
        while True:
            ev: DaemonEvent = await queue.get()
            payload = {
                "ok": True,
                "version": PROTOCOL_VERSION,
                "event": ev.event,
                "payload": ev.payload,
            }
            try:
                await self._send(writer, payload)
            except (ConnectionError, BrokenPipeError, OSError):
                break


class IPCClient:
    """A single socket carries both request/response replies and, once
    subscribed, pushed events — both `request()` and `iter_events()` used to
    call `reader.readline()` directly, so using both concurrently (e.g. a
    command sent while the TUI's event-stream loop is running) raised
    "readline() called while another coroutine is already waiting for
    incoming data". A single background task now owns the reader and
    demultiplexes each line into either the pending-response queue or the
    event queue, so callers never touch the reader themselves.
    """
    def __init__(self, socket_path: Path):
        self.socket_path = Path(socket_path)
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._responses: "Optional[asyncio.Queue]" = None
        self._events: "Optional[asyncio.Queue]" = None
        self._read_task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_unix_connection(str(self.socket_path), limit=1024*1024*10)
        self._responses = asyncio.Queue()
        self._events = asyncio.Queue()
        self._read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        """The sole reader of `self.reader`; routes each line by content."""
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    await self._responses.put(None)
                    await self._events.put(None)
                    return
                msg = json.loads(line.decode())
                if "event" in msg:
                    await self._events.put(msg)
                else:
                    await self._responses.put(msg)
        except Exception as exc:
            await self._responses.put(exc)
            await self._events.put(exc)

    async def close(self) -> None:
        if self._read_task is not None:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task
            self._read_task = None
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
        self.reader = None
        self.writer = None
        self._responses = None
        self._events = None

    async def request(self, payload: dict) -> dict:
        if not self.writer or not self.reader or self._responses is None:
            raise RuntimeError("IPC client is not connected")
        self.writer.write((json.dumps(payload) + "\n").encode())
        await self.writer.drain()
        msg = await self._responses.get()
        if msg is None:
            raise ConnectionError("Daemon closed IPC connection")
        if isinstance(msg, Exception):
            raise msg
        return msg

    async def iter_events(self):
        if not self.reader or self._events is None:
            raise RuntimeError("IPC client is not connected")
        while True:
            msg = await self._events.get()
            if msg is None:
                raise ConnectionError("Daemon closed IPC stream")
            if isinstance(msg, Exception):
                raise msg
            yield msg
