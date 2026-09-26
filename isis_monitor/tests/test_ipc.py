import asyncio
import contextlib
import json
import stat
from unittest.mock import AsyncMock, MagicMock

import pytest

from isis_monitor.daemon_state import SUBSCRIBER_QUEUE_SIZE, DaemonState
from isis_monitor.ipc import SERVER_LINE_LIMIT, IPCClient, IPCServer
from isis_monitor.tests.test_beam import wait_until


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

    events = client.iter_events()
    payload = await asyncio.wait_for(events.__anext__(), timeout=1.0)
    assert payload["event"] == "beam"
    assert payload["payload"]["beam"] == "TS1"

    await client.close()
    await server.stop()


@pytest.mark.asyncio
async def test_ipc_request_and_events_do_not_race(tmp_path):
    """A command sent while the event stream is being consumed must not
    raise — request() and iter_events() no longer share a bare readline()."""
    socket_path = tmp_path / "daemon.sock"
    state = DaemonState()

    async def command_handler(name: str):
        return {"handled": name}

    server = IPCServer(socket_path, state, command_handler)
    await server.start()

    client = IPCClient(socket_path)
    await client.connect()
    await client.request({"method": "subscribe_updates"})

    received_events = []

    async def consume_events():
        async for ev in client.iter_events():
            received_events.append(ev)

    consumer_task = asyncio.create_task(consume_events())
    await asyncio.sleep(0.05)  # let the consumer start awaiting the queue

    state.update_beam_state("TS1", 1.0, "low")
    cmd = await client.request({"method": "command", "name": "force_reconnect_all"})
    assert cmd["ok"] is True
    assert cmd["result"] == {"handled": "force_reconnect_all"}

    await asyncio.sleep(0.05)
    consumer_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await consumer_task

    assert any(ev["event"] == "beam" for ev in received_events)

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
    resp = json.loads(resp_line.decode())
    assert resp["ok"] is False
    assert resp["error"] == "invalid_json"

    # 2. A request line over SERVER_LINE_LIMIT closes that connection.
    writer.write(b'{"padding": "' + b"A" * (SERVER_LINE_LIMIT + 10) + b'"}\n')
    await writer.drain()
    assert await asyncio.wait_for(reader.read(), timeout=1.0) == b""

    writer.close()
    await writer.wait_closed()
    await server.stop()


@contextlib.asynccontextmanager
async def serving(tmp_path, state=None, command_handler=None):
    async def default_handler(name):
        return {"handled": name}

    server = IPCServer(tmp_path / "d.sock", state or DaemonState(), command_handler or default_handler)
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


@pytest.mark.asyncio
async def test_stop_returns_promptly_with_subscribed_client(tmp_path):
    """Python 3.12's Server.wait_closed() waits for open connections; stop()
    must close them rather than hang while a TUI is attached."""
    server = IPCServer(tmp_path / "d.sock", DaemonState(), AsyncMock())
    await server.start()
    client = IPCClient(server.socket_path)
    await client.connect()
    await client.request({"method": "subscribe_updates"})

    await asyncio.wait_for(server.stop(), timeout=2.0)

    assert not server.socket_path.exists()
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(client.iter_events().__anext__(), timeout=1.0)
    await client.close()


@pytest.mark.asyncio
async def test_slow_subscriber_is_disconnected_so_it_can_resync(tmp_path):
    state = DaemonState()
    async with serving(tmp_path, state) as server, connected(server) as client:
        await client.request({"method": "subscribe_updates"})
        await asyncio.sleep(0.05)
        # Overflow the subscriber queue faster than the forwarder can drain it.
        for i in range(SUBSCRIBER_QUEUE_SIZE + 1):
            state.update_log(f"flood {i}")

        with pytest.raises(ConnectionError):
            async for _ in client.iter_events():
                pass
        await wait_until(lambda: not state._subscribers)


@pytest.mark.asyncio
async def test_invalid_and_unknown_requests(tmp_path):
    async with serving(tmp_path) as server:
        not_object = await raw_request(server, b"[1, 2]\n")
        unknown = await raw_request(server, b'{"method": "nope"}\n')
    assert not_object == {"ok": False, "error": "invalid_request", "version": 1}
    assert unknown == {"ok": False, "error": "unknown_method", "version": 1}


@pytest.mark.asyncio
async def test_failing_command_returns_error_and_keeps_connection(tmp_path):
    async def handler(name):
        raise ValueError("kaboom")

    state = DaemonState()
    state.update_mcr_news("still here")
    async with serving(tmp_path, state, handler) as server, connected(server) as client:
        reply = await client.request({"method": "command", "name": "x"})
        assert reply["ok"] is False
        assert reply["error"] == "internal_error"
        assert "kaboom" in reply["detail"]
        snap = await client.request({"method": "get_snapshot"})
        assert snap["snapshot"]["mcr_news"] == "still here"


@pytest.mark.asyncio
async def test_get_history_limit_and_get_logs(tmp_path):
    state = DaemonState()
    for i in range(5):
        state.append_beam_sample("TS1", float(i), "low")
    state.update_log("hello")
    async with serving(tmp_path, state) as server, connected(server) as client:
        limited = await client.request({"method": "get_history", "limit": 2})
        full = await client.request({"method": "get_history"})
        bogus = await client.request({"method": "get_history", "limit": "2"})
        logs = await client.request({"method": "get_logs"})
    assert [r["current"] for r in limited["history"]["TS1"]] == [3.0, 4.0]
    assert len(full["history"]["TS1"]) == 5
    assert len(bogus["history"]["TS1"]) == 5
    assert logs["logs"] == ["hello"]


@pytest.mark.asyncio
async def test_client_requests_fail_fast_after_daemon_disconnects(tmp_path):
    server = IPCServer(tmp_path / "d.sock", DaemonState(), AsyncMock())
    await server.start()
    client = IPCClient(server.socket_path)
    await client.connect()
    await client.request({"method": "get_snapshot"})  # ensure the server has accepted us
    await server.stop()

    for _ in range(2):  # every later call fails too, rather than hanging
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(client.request({"method": "get_snapshot"}), timeout=1.0)
    await client.close()


@pytest.mark.asyncio
async def test_client_not_connected_raises(tmp_path):
    client = IPCClient(tmp_path / "missing.sock")
    with pytest.raises(RuntimeError):
        await client.request({"method": "get_snapshot"})
    with pytest.raises(RuntimeError):
        await client.iter_events().__anext__()
    with pytest.raises(FileNotFoundError):
        await client.connect()


@pytest.mark.asyncio
async def test_start_replaces_stale_socket_and_restricts_permissions(tmp_path):
    stale = tmp_path / "d.sock"
    stale.write_text("left over from a crash")
    async with serving(tmp_path) as server:
        assert stat.S_ISSOCK(server.socket_path.stat().st_mode)
        assert stat.S_IMODE(server.socket_path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_connection_accepted_during_shutdown_is_closed(tmp_path):
    server = IPCServer(tmp_path / "d.sock", DaemonState(), AsyncMock())
    server._closing = True
    writer = MagicMock()
    await server._handle_client(MagicMock(), writer)
    writer.close.assert_called_once()
    assert not server._clients


@pytest.mark.asyncio
async def test_client_surfaces_garbage_from_server(tmp_path):
    async def garbage_server(reader, writer):
        writer.write(b"this is not json\n")
        await writer.drain()
        writer.close()

    srv = await asyncio.start_unix_server(garbage_server, path=str(tmp_path / "g.sock"))
    client = IPCClient(tmp_path / "g.sock")
    await client.connect()
    with pytest.raises(json.JSONDecodeError):
        await asyncio.wait_for(client.request({"method": "get_snapshot"}), 1)
    await client.close()
    srv.close()
    await srv.wait_closed()


@pytest.mark.asyncio
async def test_config_methods_are_routed_to_config_handler(tmp_path):
    config_handler = AsyncMock(return_value={"config": {"x": 1}})
    server = IPCServer(tmp_path / "d.sock", DaemonState(), AsyncMock(), config_handler)
    await server.start()
    try:
        async with connected(server) as client:
            get = await client.request({"method": "get_config"})
            update = await client.request({"method": "update_config", "settings": {"a": "b"}})
    finally:
        await server.stop()
    assert get["config"] == {"x": 1} and get["ok"] is True
    assert config_handler.await_args_list[0].args == ("get_config", {"method": "get_config"})
    assert config_handler.await_args_list[1].args[1]["settings"] == {"a": "b"}
    assert update["ok"] is True


@pytest.mark.asyncio
async def test_config_methods_unknown_without_config_handler(tmp_path):
    async with serving(tmp_path) as server, connected(server) as client:
        reply = await client.request({"method": "get_config"})
    assert (reply["ok"], reply["error"]) == (False, "unknown_method")
