import asyncio
import json
import os
import stat
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from isis_monitor.ipc import SERVER_LINE_LIMIT, IPCServer, ipc_request
from isis_monitor.tests.helpers import never_answers, raw_request, serving, wait_until


async def test_ipc_command(tmp_path):
    async with serving(tmp_path) as server:
        reply = await ipc_request(server.socket_path, {"method": "command", "name": "force_reconnect_all"})
    assert reply == {"ok": True, "result": {"handled": "force_reconnect_all"}}


async def test_ipc_malformed_json_and_oversized_payload(tmp_path):
    async with serving(tmp_path) as server:
        reply = await raw_request(server, b"{bad_json\n")
        assert reply["ok"] is False and reply["error"] == "invalid_json"
        assert (await raw_request(server, b"[" * 50_000 + b"\n"))["error"] == "invalid_json"  # RecursionError
        # A request line over SERVER_LINE_LIMIT closes that connection.
        reader, writer = await asyncio.open_unix_connection(str(server.socket_path))
        writer.write(b'{"padding": "' + b"A" * (SERVER_LINE_LIMIT + 10) + b'"}\n')
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), timeout=1.0) == b""
        writer.close()
        await writer.wait_closed()


async def test_invalid_and_unknown_requests(tmp_path):
    async with serving(tmp_path) as server:
        not_object = await raw_request(server, b"[1, 2]\n")
        unknown = await raw_request(server, b'{"method": "nope"}\n')
    assert not_object == {"ok": False, "error": "invalid_request"}
    assert unknown == {"ok": False, "error": "unknown_method"}


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


async def test_start_replaces_stale_socket_and_restricts_permissions(tmp_path):
    stale = tmp_path / "d.sock"
    stale.write_text("left over from a crash")
    async with serving(tmp_path) as server:
        assert stat.S_ISSOCK(server.socket_path.stat().st_mode)
        assert stat.S_IMODE(server.socket_path.stat().st_mode) == 0o600


async def test_connection_accepted_during_shutdown_is_closed(tmp_path):
    server = IPCServer(tmp_path / "d.sock", AsyncMock())
    server._closing = True
    writer = MagicMock()
    await server._handle_client(MagicMock(), writer)
    writer.close.assert_called_once()
    assert not server._clients


async def test_config_methods_are_routed_to_config_handler(tmp_path):
    config_handler = AsyncMock(return_value={"config": {"x": 1}})
    async with serving(tmp_path, config_handler=config_handler) as server:
        get = await ipc_request(server.socket_path, {"method": "get_config"})
        update = await ipc_request(server.socket_path, {"method": "update_config", "settings": {"a": "b"}})
    assert get["config"] == {"x": 1} and get["ok"] is True
    assert config_handler.await_args_list[0].args == ("get_config", {"method": "get_config"})
    assert config_handler.await_args_list[1].args[1]["settings"] == {"a": "b"}
    assert update["ok"] is True


async def test_config_methods_unknown_without_config_handler(tmp_path):
    async with serving(tmp_path) as server:
        reply = await ipc_request(server.socket_path, {"method": "get_config"})
    assert (reply["ok"], reply["error"]) == (False, "unknown_method")


async def test_socket_is_created_owner_only(tmp_path):
    modes = []
    real_start = asyncio.start_unix_server

    async def spy(*args, path, **kwargs):
        server = await real_start(*args, path=path, **kwargs)
        modes.append(os.stat(path).st_mode & 0o777)
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


async def test_ipc_request_surfaces_garbage_from_the_daemon(tmp_path):
    garbage = b"this is not json\n"

    async def garbage_server(reader, writer):
        writer.write(garbage)
        await writer.drain()
        writer.close()

    srv = await asyncio.start_unix_server(garbage_server, path=str(tmp_path / "g.sock"))
    async with srv:
        with pytest.raises(json.JSONDecodeError):
            await ipc_request(tmp_path / "g.sock", {"method": "get_status"})
        garbage = b"[]\n"
        with pytest.raises(ValueError, match="Not a reply"):
            await ipc_request(tmp_path / "g.sock", {"method": "get_status"})


async def test_ipc_request_gets_the_status_on_a_connection_of_its_own(tmp_path):
    async with serving(tmp_path) as server:
        server.status = lambda: {"kind": "status"}
        assert await ipc_request(server.socket_path, {"method": "get_status"}) == {"ok": True, "status": {"kind": "status"}}
        await wait_until(lambda: not server._clients)  # the connection's closed
    async with serving(tmp_path) as server:  # without a status builder
        assert (await ipc_request(server.socket_path, {"method": "get_status"}))["error"] == "unknown_method"


async def test_ipc_request_fails_without_an_answer(tmp_path):
    with pytest.raises(FileNotFoundError):
        await ipc_request(tmp_path / "none.sock", {"method": "get_status"})

    async def hang_up(_reader, writer):
        writer.close()

    server = await asyncio.start_unix_server(hang_up, path=str(tmp_path / "h.sock"))
    async with server:
        with pytest.raises(ConnectionError):  # reset, or closed without answering
            await ipc_request(tmp_path / "h.sock", {"method": "get_status"})
    with patch("isis_monitor.ipc.STOP_FLUSH_TIMEOUT", 0.05):
        async with serving(tmp_path, command_handler=never_answers) as server:
            with pytest.raises(TimeoutError, match="No reply to 'command' within"):
                await ipc_request(server.socket_path, {"method": "command", "name": "x"}, timeout=0.05)
