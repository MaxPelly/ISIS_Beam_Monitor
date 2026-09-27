import asyncio
import contextlib
import json
import os
import socket
import stat
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from isis_monitor.daemon_state import SUBSCRIBER_QUEUE_SIZE, DaemonState
from isis_monitor.ipc import SERVER_LINE_LIMIT, IPCClient, IPCServer
from isis_monitor.tests.helpers import connected, never_answers, raw_request, serving, wait_until


async def test_ipc_snapshot_and_command(tmp_path):
    state = DaemonState()
    state.update_mcr_news("hello")
    async with serving(tmp_path, state) as server, connected(server) as client:
        snap = await client.request({"method": "get_snapshot"})
        cmd = await client.request({"method": "command", "name": "force_reconnect_all"})
    assert snap["ok"] is True and snap["snapshot"]["mcr_news"] == "hello"
    assert cmd["ok"] is True and cmd["result"] == {"handled": "force_reconnect_all"}


async def test_ipc_request_and_events_do_not_race(tmp_path):
    """A command sent while the event stream is being consumed must not
    raise — request() and iter_events() no longer share a bare readline()."""
    state = DaemonState()
    async with serving(tmp_path, state) as server, connected(server) as client:
        await client.request({"method": "subscribe_updates"})
        received_events = []

        async def consume_events():
            async for ev in client.iter_events():
                received_events.append(ev)

        consumer_task = asyncio.create_task(consume_events())
        await asyncio.sleep(0.05)  # let the consumer start awaiting the queue
        state.update_beam_state("TS1", 1.0, "low")
        cmd = await client.request({"method": "command", "name": "force_reconnect_all"})
        assert cmd["ok"] is True and cmd["result"] == {"handled": "force_reconnect_all"}
        await wait_until(lambda: received_events)
        consumer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer_task
    assert any(ev["event"] == "beam" and ev["payload"]["beam"] == "TS1" for ev in received_events)


async def test_ipc_malformed_json_and_oversized_payload(tmp_path):
    async with serving(tmp_path) as server:
        reply = await raw_request(server, b"{bad_json\n")
        assert reply["ok"] is False and reply["error"] == "invalid_json"
        # A request line over SERVER_LINE_LIMIT closes that connection.
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        writer.write(b'{"padding": "' + b"A" * (SERVER_LINE_LIMIT + 10) + b'"}\n')
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), timeout=1.0) == b""
        writer.close()
        await writer.wait_closed()


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


async def test_invalid_and_unknown_requests(tmp_path):
    async with serving(tmp_path) as server:
        not_object = await raw_request(server, b"[1, 2]\n")
        unknown = await raw_request(server, b'{"method": "nope"}\n')
    assert not_object == {"ok": False, "error": "invalid_request", "version": 1}
    assert unknown == {"ok": False, "error": "unknown_method", "version": 1}


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


async def test_client_not_connected_raises(tmp_path):
    client = IPCClient(tmp_path / "missing.sock")
    with pytest.raises(RuntimeError):
        await client.request({"method": "get_snapshot"})
    with pytest.raises(RuntimeError):
        await client.iter_events().__anext__()
    with pytest.raises(FileNotFoundError):
        await client.connect()


async def test_start_replaces_stale_socket_and_restricts_permissions(tmp_path):
    stale = tmp_path / "d.sock"
    stale.write_text("left over from a crash")
    async with serving(tmp_path) as server:
        assert stat.S_ISSOCK(server.socket_path.stat().st_mode)
        assert stat.S_IMODE(server.socket_path.stat().st_mode) == 0o600


async def test_connection_accepted_during_shutdown_is_closed(tmp_path):
    server = IPCServer(tmp_path / "d.sock", DaemonState(), AsyncMock())
    server._closing = True
    writer = MagicMock()
    await server._handle_client(MagicMock(), writer)
    writer.close.assert_called_once()
    assert not server._clients


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


async def test_config_methods_are_routed_to_config_handler(tmp_path):
    config_handler = AsyncMock(return_value={"config": {"x": 1}})
    async with serving(tmp_path, config_handler=config_handler) as server, connected(server) as client:
        get = await client.request({"method": "get_config"})
        update = await client.request({"method": "update_config", "settings": {"a": "b"}})
    assert get["config"] == {"x": 1} and get["ok"] is True
    assert config_handler.await_args_list[0].args == ("get_config", {"method": "get_config"})
    assert config_handler.await_args_list[1].args[1]["settings"] == {"a": "b"}
    assert update["ok"] is True


async def test_config_methods_unknown_without_config_handler(tmp_path):
    async with serving(tmp_path) as server, connected(server) as client:
        reply = await client.request({"method": "get_config"})
    assert (reply["ok"], reply["error"]) == (False, "unknown_method")



async def test_socket_is_created_owner_only(tmp_path):
    modes = []
    real_start = asyncio.start_unix_server

    async def spy(*args, path, **kwargs):
        server = await real_start(*args, path=path, **kwargs)
        modes.append(os.stat(path).st_mode & 0o777)  # before start() chmods it
        return server

    old_umask = os.umask(0o022)
    try:
        with patch("isis_monitor.ipc.asyncio.start_unix_server", side_effect=spy):
            async with serving(tmp_path):
                pass
        assert os.umask(0o022) == 0o022  # restored
    finally:
        os.umask(old_umask)
    assert modes == [0o600]



async def _stalled_client(server, requests: int):
    """A client that sends requests with large replies and never reads them,
    so the server's socket buffer fills up."""
    reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
    for _ in range(requests):
        writer.write(b'{"method": "get_history"}\n')
    await writer.drain()
    return reader, writer


def _state_with_big_history() -> DaemonState:
    state = DaemonState()
    ts = datetime.now(timezone.utc)
    for beam in state.history:
        state.history[beam].extend((ts, float(i), "high") for i in range(5000))
    return state


async def test_stop_does_not_hang_on_a_client_that_stopped_reading(tmp_path):
    server = IPCServer(tmp_path / "d.sock", _state_with_big_history(), AsyncMock())
    await server.start()
    _reader, writer = await _stalled_client(server, 20)
    await asyncio.sleep(0.2)  # let the server fill the socket buffer

    with patch("isis_monitor.ipc.STOP_FLUSH_TIMEOUT", 0.1):
        # asyncio.wait rather than wait_for, so a regression fails instead of
        # hanging the test run (wait_for can't cancel a stuck wait_closed).
        stopping = asyncio.ensure_future(server.stop())
        done, _pending = await asyncio.wait({stopping}, timeout=3)
    writer.close()
    assert stopping in done, "IPCServer.stop() hung on a client that stopped reading"


async def test_stop_still_delivers_a_reply_already_written(tmp_path):
    """The reply to the request that triggered a restart must reach a reading client."""
    stop_task = None

    async def handler(name):
        nonlocal stop_task
        stop_task = asyncio.create_task(server.stop())
        return {"restart": "ok"}

    server = IPCServer(tmp_path / "d.sock", DaemonState(), handler)
    await server.start()
    async with connected(server) as client:
        reply = await client.request({"method": "command", "name": "restart"})
    await asyncio.wait_for(stop_task, 3)
    assert reply["result"] == {"restart": "ok"}


async def test_subscriber_that_stops_reading_is_disconnected(tmp_path):
    state = DaemonState()
    server = IPCServer(tmp_path / "d.sock", state, AsyncMock())
    await server.start()
    try:
        # A tiny receive buffer, so the server's writes back up quickly.
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.connect(str(server.socket_path))
        reader, writer = await asyncio.open_unix_connection(sock=sock)
        writer.write(b'{"method": "subscribe_updates"}\n')
        await writer.drain()
        await asyncio.sleep(0.05)

        with patch("isis_monitor.ipc.SUBSCRIBER_DRAIN_TIMEOUT", 0.2):
            for _ in range(20):
                state.update_log("x" * 60000)
            # The server drops the connection without the client reading anything.
            await wait_until(lambda: not server._clients, timeout=3)
        writer.close()
    finally:
        await asyncio.wait_for(server.stop(), 3)


async def test_request_timeout_closes_the_client_and_wakes_other_waiters(tmp_path):
    """A late reply would otherwise be read as the next request's answer."""
    async with serving(tmp_path, command_handler=never_answers) as server:
        client = IPCClient(server.socket_path)
        await client.connect()
        events = asyncio.ensure_future(client.iter_events().__anext__())
        with pytest.raises(OSError) as exc:  # the built-in TimeoutError on every Python version
            await client.request({"method": "command", "name": "x"}, timeout=0.05)
        assert type(exc.value) is TimeoutError
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(events, 1)
        with pytest.raises(RuntimeError, match="not connected"):
            await client.request({"method": "get_snapshot"})
        await client.close()  # idempotent
