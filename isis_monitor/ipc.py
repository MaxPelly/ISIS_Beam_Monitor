"""Newline-delimited JSON over a UNIX socket between the daemon and the CLI.

Each request is one line and gets one reply line, which carries "ok".
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable, Optional, Set

logger = logging.getLogger(__name__)

# Requests are small; replies (the status, with 24h of beam history) larger.
SERVER_LINE_LIMIT = 64 * 1024
CLIENT_LINE_LIMIT = 1024 * 1024
# How long stop() lets clients take already-buffered replies before cutting
# off any that aren't reading (e.g. a suspended terminal), so shutdown can't hang.
STOP_FLUSH_TIMEOUT = 1.0
REQUEST_TIMEOUT = 10.0  # a stuck daemon mustn't hang a command


def _encode(payload: dict) -> bytes:
    return (json.dumps(payload) + "\n").encode()


class IPCServer:
    def __init__(
        self,
        socket_path: Path,
        command_handler: Callable[[str], Awaitable[dict]],
        config_handler: Optional[Callable[[str, dict], Awaitable[dict]]] = None,
        status: Optional[Callable[[], dict]] = None,
    ):
        """`config_handler(method, request)` serves get_config and update_config;
        `status()` serves get_status."""
        self.socket_path = Path(socket_path)
        self.command_handler = command_handler
        self.config_handler = config_handler
        self.status = status
        self.server: Optional[asyncio.base_events.Server] = None
        self._clients: Set[asyncio.StreamWriter] = set()
        self._closing = False

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        # Created owner-only (it can rewrite the config): set by the umask at
        # creation, rather than a chmod afterwards that would leave a window.
        old_umask = os.umask(0o177)
        try:
            self.server = await asyncio.start_unix_server(
                self._handle_client, path=str(self.socket_path), limit=SERVER_LINE_LIMIT
            )
        finally:
            os.umask(old_umask)

    async def stop(self) -> None:
        self._closing = True
        if self.server is not None:
            self.server.close()
            # Since Python 3.12.1, wait_closed() also waits for every open
            # connection, so a client still connected would block shutdown forever.
            # close() still flushes buffered output first (e.g. the reply to
            # the update_config that triggered a restart), which never ends
            # if the client has stopped reading, so those get aborted.
            writers = list(self._clients)
            for writer in writers:
                writer.close()
            try:
                await asyncio.wait_for(asyncio.shield(self.server.wait_closed()), STOP_FLUSH_TIMEOUT)
            except TimeoutError:
                for writer in writers:
                    writer.transport.abort()
                await self.server.wait_closed()
        self.socket_path.unlink(missing_ok=True)

    async def _reply(self, req: dict) -> dict:
        method = req.get("method")
        if method == "get_status" and self.status is not None:
            return {"status": self.status()}
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
        try:
            while line := await reader.readline():
                try:
                    req = json.loads(line)
                except (ValueError, RecursionError):  # e.g. JSONDecodeError, or "[[[[…"
                    reply = {"ok": False, "error": "invalid_json"}
                else:
                    if not isinstance(req, dict):
                        reply = {"ok": False, "error": "invalid_request"}
                    else:
                        try:
                            reply = await self._reply(req)
                        except Exception as exc:
                            logger.exception(f"IPC request {req.get('method')!r} failed")
                            reply = {"ok": False, "error": "internal_error", "detail": str(exc)}
                writer.write(_encode({"ok": True, **reply}))
                await writer.drain()
        except (ConnectionError, ValueError):
            pass  # client went away, or sent a line over SERVER_LINE_LIMIT
        finally:
            self._clients.discard(writer)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()


async def ipc_request(socket_path: Path, payload: dict, timeout: Optional[float] = None) -> dict:
    """Send one request on a connection of its own and return the reply.
    Raises OSError (including TimeoutError) if the daemon can't be reached or
    doesn't answer in time, and ValueError for a reply that isn't JSON."""
    async def exchange() -> dict:
        reader, writer = await asyncio.open_unix_connection(str(socket_path), limit=CLIENT_LINE_LIMIT)
        try:
            writer.write(_encode(payload))
            await writer.drain()
            line = await reader.readline()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
        if not line:
            raise ConnectionError("The daemon closed the connection without answering")
        reply = json.loads(line)
        if not isinstance(reply, dict):
            raise ValueError(f"Not a reply: {line[:100]!r}")
        return reply

    timeout = REQUEST_TIMEOUT if timeout is None else timeout
    try:
        return await asyncio.wait_for(exchange(), timeout)
    except TimeoutError:
        raise TimeoutError(f"No reply to {payload.get('method')!r} within {timeout:.0f}s") from None
