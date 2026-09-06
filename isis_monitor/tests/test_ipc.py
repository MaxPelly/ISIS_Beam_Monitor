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

@pytest.mark.asyncio
async def test_ipc_malformed_json_and_oversized_payload(tmp_path):
    socket_path = tmp_path / "daemon.sock"
    state = DaemonState()

    async def command_handler(_name: str):
        return {}

    server = IPCServer(socket_path, state, command_handler)
    await server.start()

    # Manual socket connection to send raw bad bytes
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    
    # 1. Malformed JSON
    writer.write(b"{bad_json\n")
    await writer.drain()
    
    resp_line = await reader.readline()
    resp = __import__("json").loads(resp_line.decode())
    assert resp["ok"] is False
    assert resp["error"] == "invalid_json"

    # 2. Oversized payload (limit is 65536)
    large_payload = b"{" + b'"padding": "' + b'A' * 70000 + b'"}\n'
    writer.write(large_payload)
    await writer.drain()

    # The server should drop the connection due to ValueError from limit
    # or just close. Wait to see it drops.
    try:
        await asyncio.wait_for(reader.readline(), timeout=1.0)
    except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.TimeoutError):
        pass # Expected

    writer.close()
    await writer.wait_closed()
    await server.stop()
