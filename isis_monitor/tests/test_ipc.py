import asyncio
import json
import os
import stat
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from isis_monitor.ipc import SERVER_LINE_LIMIT, IPCServer, ipc_request
from isis_monitor.tests.helpers import never_answers, raw_request, serving, wait_until


async def test_requests_are_routed_to_their_handlers(tmp_path):
    config_handler = AsyncMock(return_value={"config": {"x": 1}})
    async with serving(tmp_path, config_handler=config_handler, status=lambda: {"kind": "status"}) as server:
        command = await ipc_request(server.socket_path, {"method": "command", "name": "force_reconnect_all"})
        get = await ipc_request(server.socket_path, {"method": "get_config"})
        update = await ipc_request(server.socket_path, {"method": "update_config", "settings": {"a": "b"}})
        status = await ipc_request(server.socket_path, {"method": "get_status"})
        await wait_until(lambda: not server._clients)  # each on a connection of its own, now closed
    assert command == {"ok": True, "result": {"handled": "force_reconnect_all"}}
    assert get == {"ok": True, "config": {"x": 1}}
    assert config_handler.await_args_list[0].args == ("get_config", {"method": "get_config"})
    assert config_handler.await_args_list[1].args[1]["settings"] == {"a": "b"}
    assert update["ok"] is True
    assert status == {"ok": True, "status": {"kind": "status"}}


@pytest.mark.parametrize("line, error", [
    (b"{bad_json\n", "invalid_json"),
    (b"[" * 50_000 + b"\n", "invalid_json"),  # RecursionError
    (b'{"method": "\xff"}\n', "invalid_json"),  # not UTF-8
    (b"[1, 2]\n", "invalid_request"),
    (b'{"method": "nope"}\n', "unknown_method"),
    (b'{"method": "get_config"}\n', "unknown_method"),  # without a config handler
    (b'{"method": "get_status"}\n', "unknown_method"),  # without a status builder
])
async def test_bad_requests_get_an_error_reply(tmp_path, line, error):
    async with serving(tmp_path) as server:
        assert await raw_request(server, line) == {"ok": False, "error": error}


async def test_an_oversized_request_closes_the_connection(tmp_path):
    async with serving(tmp_path) as server:
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        writer.write(b'{"padding": "' + b"A" * (SERVER_LINE_LIMIT + 10) + b'"}\n')
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), timeout=1.0) == b""
        writer.close()
        await writer.wait_closed()


async def test_failing_command_returns_error_and_keeps_connection(tmp_path):
    async def handler(name):
        raise ValueError("kaboom")

    async with serving(tmp_path, handler, status=lambda: {"still": "here"}) as server:
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        writer.write(b'{"method": "command", "name": "x"}\n{"method": "get_status"}\n')
        failed, status = json.loads(await reader.readline()), json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
    assert (failed["ok"], failed["error"]) == (False, "internal_error") and "kaboom" in failed["detail"]
    assert status == {"ok": True, "status": {"still": "here"}}


async def test_start_replaces_a_stale_socket_with_an_owner_only_one(tmp_path):
    (tmp_path / "d.sock").write_text("left over from a crash")
    modes = []
    real_start = asyncio.start_unix_server

    async def spy(*args, path, **kwargs):
        server = await real_start(*args, path=path, **kwargs)
        modes.append(os.stat(path).st_mode & 0o777)
        return server

    old_umask = os.umask(0o022)
    try:
        with patch("isis_monitor.ipc.asyncio.start_unix_server", side_effect=spy):
            async with serving(tmp_path) as server:
                assert stat.S_ISSOCK(server.socket_path.stat().st_mode)
        assert os.umask(0o022) == 0o022  # restored
    finally:
        os.umask(old_umask)
    assert modes == [0o600]  # from creation, not a chmod afterwards


async def test_connection_accepted_during_shutdown_is_closed(tmp_path):
    server = IPCServer(tmp_path / "d.sock", AsyncMock())
    server._closing = True
    writer = MagicMock()
    await server._handle_client(MagicMock(), writer)
    writer.close.assert_called_once()
    assert not server._clients
    await server.stop()  # never started: nothing to close


async def test_stop_does_not_hang_on_a_client_that_stopped_reading(tmp_path):
    server = IPCServer(tmp_path / "d.sock", AsyncMock(), status=lambda: {"padding": "A" * 60_000})
    await server.start()
    # Asks for large replies and never reads them, so the server's socket buffer fills up.
    _reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
    writer.write(b'{"method": "get_status"}\n' * 20)
    await writer.drain()
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

    server = IPCServer(tmp_path / "d.sock", handler)
    await server.start()
    reply = await ipc_request(server.socket_path, {"method": "command", "name": "restart"})
    await asyncio.wait_for(stop_task, 3)
    assert reply["result"] == {"restart": "ok"}


async def test_ipc_request_fails_without_a_proper_answer(tmp_path):
    with pytest.raises(FileNotFoundError):
        await ipc_request(tmp_path / "none.sock", {"method": "get_status"})

    reply = b""

    async def scripted(reader, writer):
        await reader.readline()
        writer.write(reply)
        await writer.drain()
        writer.close()

    async with await asyncio.start_unix_server(scripted, path=str(tmp_path / "g.sock")):
        with pytest.raises(ConnectionError, match="without answering"):
            await ipc_request(tmp_path / "g.sock", {"method": "get_status"})
        reply = b"this is not json\n"
        with pytest.raises(json.JSONDecodeError):
            await ipc_request(tmp_path / "g.sock", {"method": "get_status"})
        reply = b"[]\n"
        with pytest.raises(ValueError, match="Not a reply"):
            await ipc_request(tmp_path / "g.sock", {"method": "get_status"})

    with patch("isis_monitor.ipc.STOP_FLUSH_TIMEOUT", 0.05):
        async with serving(tmp_path, command_handler=never_answers) as server:
            with pytest.raises(TimeoutError, match="No reply to 'command' within"):
                await ipc_request(server.socket_path, {"method": "command", "name": "x"}, timeout=0.05)


async def test_connections_from_other_users_are_refused(tmp_path, caplog):
    async with serving(tmp_path) as server:
        assert (await ipc_request(server.socket_path, {"method": "command", "name": "x"}))["ok"]  # our own
        with patch("isis_monitor.ipc.os.getuid", return_value=os.getuid() + 1):
            with pytest.raises(ConnectionError):
                await ipc_request(server.socket_path, {"method": "command", "name": "x"})
    assert f"Refused an IPC connection from user {os.getuid()}" in caplog.text

    with patch("isis_monitor.ipc.socket", spec=[]):  # no SO_PEERCRED: the check is skipped
        async with serving(tmp_path) as server:
            assert (await ipc_request(server.socket_path, {"method": "command", "name": "x"}))["ok"]


async def test_an_idle_connection_is_closed(tmp_path):
    with patch("isis_monitor.ipc.IDLE_TIMEOUT", 0.05):
        async with serving(tmp_path) as server:
            reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
            assert await asyncio.wait_for(reader.read(), 2) == b""  # closed, without a request
            writer.close()
            await wait_until(lambda: not server._clients)


def test_a_peer_that_cant_be_identified_is_refused():
    from isis_monitor.ipc import _peer_uid
    writer = MagicMock()
    writer.get_extra_info.return_value.getsockopt.side_effect = OSError("gone")
    assert _peer_uid(writer) == -1  # not ours, so refused
