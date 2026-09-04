import asyncio
from pathlib import Path

import pytest

from isis_monitor.daemon_state import DaemonState
from isis_monitor.ipc import IPCClient, IPCServer


@pytest.mark.asyncio
async def test_ipc_snapshot_and_command(tmp_path):
    socket_path = tmp_path / "daemon.sock"
    state = DaemonState()
    state.update_mcr_news("hello")

    async def command_handler(name: str):
        if name == "force_reconnect_all":
            return {"beam": True, "mcr": True}
        return {"error": "unknown"}

    server = IPCServer(socket_path, state, command_handler)
    await server.start()

    client = IPCClient(socket_path)
    await client.connect()

    snap = await client.request({"method": "get_snapshot"})
    assert snap["ok"] is True
    assert snap["snapshot"]["mcr_news"] == "hello"

    cmd = await client.request({"method": "command", "name": "force_reconnect_all"})
    assert cmd["ok"] is True
    assert cmd["result"] == {"beam": True, "mcr": True}

    await client.close()
    await server.stop()


@pytest.mark.asyncio
async def test_ipc_subscribe_updates(tmp_path):
    socket_path = tmp_path / "daemon.sock"
    state = DaemonState()

    async def command_handler(_name: str):
        return {"ok": True}

    server = IPCServer(socket_path, state, command_handler)
    await server.start()

    client = IPCClient(socket_path)
    await client.connect()

    sub = await client.request({"method": "subscribe_updates"})
    assert sub["ok"] is True

    state.update_beam_state("TS1", 12.3, "low")

    msg = await asyncio.wait_for(client.reader.readline(), timeout=1.0)
    payload = __import__("json").loads(msg.decode())
    assert payload["event"] == "beam"
    assert payload["payload"]["beam"] == "TS1"

    await client.close()
    await server.stop()
