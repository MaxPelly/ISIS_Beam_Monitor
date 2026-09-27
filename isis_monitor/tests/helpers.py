"""Shared test helpers: polling, a fake PVWS server, IPC server/client contexts."""
import asyncio
import contextlib
import json
from unittest.mock import AsyncMock

import websockets

from isis_monitor.daemon_state import DaemonState
from isis_monitor.ipc import IPCClient, IPCServer
from isis_monitor.notifiers import NotificationChannel


async def wait_until(predicate, timeout=2.0):
    async def _poll():
        while not predicate():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_poll(), timeout)


def fake_channel(name="Test"):
    """A NotificationChannel whose broadcast is an AsyncMock."""
    channel = NotificationChannel(name)
    channel.broadcast = AsyncMock()
    return channel


class FakePVWS:
    """A local PVWS stand-in: records subscriptions, pushes scripted messages.

    Pass ``port`` to (re)start a server on a known port."""

    def __init__(self, messages=(), close_after_send=False, port=0):
        self.messages = list(messages)
        self.close_after_send = close_after_send
        self.port = port
        self.connections = 0
        self.subscriptions = []
        self.connected = asyncio.Event()
        self._server = None

    async def _handler(self, ws):
        self.connections += 1
        self.subscriptions.append(json.loads(await ws.recv()))
        self.connected.set()
        for msg in self.messages:
            await ws.send(msg if isinstance(msg, str) else json.dumps(msg))
        if self.close_after_send:
            return
        await ws.wait_closed()

    async def __aenter__(self):
        self._server = await websockets.serve(self._handler, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}"
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()


@contextlib.asynccontextmanager
async def running(monitor):
    task = asyncio.create_task(monitor.run())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@contextlib.asynccontextmanager
async def serving(tmp_path, state=None, command_handler=None, config_handler=None):
    async def default_handler(name):
        return {"handled": name}

    server = IPCServer(tmp_path / "d.sock", state or DaemonState(), command_handler or default_handler, config_handler)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@contextlib.asynccontextmanager
async def connected(server):
    client = IPCClient(server.socket_path)
    await client.connect()
    try:
        yield client
    finally:
        await client.close()


async def raw_request(server, line: bytes) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
    writer.write(line)
    await writer.drain()
    reply = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    return reply


async def never_answers(_name):
    await asyncio.sleep(3600)
