"""Tests for the streaming exec path (``Sandbox.run(..., on_stdout=...)``).

Deliberately does NOT mock the WebSocket protocol — respx only intercepts
httpx (the ws-ticket POST), so a real ``websockets.serve`` server stands in
for the CP's exec-stream endpoint. This exercises the real two-step
handshake: httpx POST for the ticket, then a real WS connect/send/recv
against the local server.

Covers both ``stream_exec`` (sync) and ``stream_exec_async``. The sync tests
run the local WS server in a background thread with its own event loop,
since sync ``stream_exec`` wraps the shared WS coroutine in ``asyncio.run()``
and would collide with a server on the same loop. The async test instead
runs the server as a task on the *same* loop the test itself runs on, since
``stream_exec_async`` is awaited directly (no ``asyncio.run()``) — see the
"async happy path" section below.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import threading
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import httpx
import pytest
import respx
import websockets

from quome._exceptions import QuomeError
from quome._models import ExecResult
from quome._stream import stream_exec, stream_exec_async
from quome._transport import AsyncTransport, Transport

ORG_ID = "11111111-1111-1111-1111-111111111111"
SANDBOX_ID = "33333333-3333-3333-3333-333333333333"

WsHandler = Callable[[websockets.ServerConnection], Awaitable[None]]


class _LocalWsServer:
    """A real ``websockets.serve`` server on 127.0.0.1, run in its own
    thread with its own event loop so the (synchronous) ``stream_exec``
    under test can run ``asyncio.run()`` on the main thread without
    colliding with it."""

    def __init__(self, handler: WsHandler) -> None:
        self._handler = handler
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: Any = None
        self.port: int = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        self._ready.set()
        await self._server.wait_closed()

    def start(self) -> int:
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("local WS test server failed to start")
        return self.port

    def stop(self) -> None:
        loop, server = self._loop, self._server
        if loop is not None and server is not None:
            loop.call_soon_threadsafe(server.close)
        self._thread.join(timeout=5)


@pytest.fixture
def ws_server() -> Iterator[Callable[[WsHandler], int]]:
    servers: list[_LocalWsServer] = []

    def _start(handler: WsHandler) -> int:
        server = _LocalWsServer(handler)
        port = server.start()
        servers.append(server)
        return port

    yield _start

    for server in servers:
        server.stop()


def _mock_ticket(base_url: str) -> None:
    respx.post(f"{base_url}/api/v1/orgs/{ORG_ID}/sandboxes/{SANDBOX_ID}/ws-ticket").mock(
        return_value=httpx.Response(200, json={"ticket": "tkt_abc123", "expires_in": 30})
    )


# --- happy path ------------------------------------------------------------


def test_stream_exec_streams_chunks_and_returns_combined_stdout(
    ws_server: Callable[[WsHandler], int],
) -> None:
    received_payload: dict[str, Any] = {}

    async def handler(ws: websockets.ServerConnection) -> None:
        received_payload.update(json.loads(await ws.recv()))
        await ws.send("chunk-1 ")
        await ws.send("chunk-2 ")
        await ws.send("chunk-3")
        await ws.close()

    port = ws_server(handler)
    base_url = f"http://127.0.0.1:{port}"
    transport = Transport(api_key="sk_test", base_url=base_url)

    chunks: list[str] = []
    with respx.mock:
        _mock_ticket(base_url)
        result = stream_exec(
            transport,
            ORG_ID,
            SANDBOX_ID,
            "echo hi",
            "/workspace",
            None,
            chunks.append,
            timeout=5,
        )

    assert result == ExecResult(exit_code=None, stdout="chunk-1 chunk-2 chunk-3", stderr="")
    assert chunks == ["chunk-1 ", "chunk-2 ", "chunk-3"]
    assert received_payload == {"command": "echo hi", "working_dir": "/workspace"}


def test_stream_exec_folds_env_into_command_as_shell_assignments(
    ws_server: Callable[[WsHandler], int],
) -> None:
    received_payload: dict[str, Any] = {}

    async def handler(ws: websockets.ServerConnection) -> None:
        received_payload.update(json.loads(await ws.recv()))
        await ws.send("ok")
        await ws.close()

    port = ws_server(handler)
    base_url = f"http://127.0.0.1:{port}"
    transport = Transport(api_key="sk_test", base_url=base_url)

    with respx.mock:
        _mock_ticket(base_url)
        stream_exec(
            transport,
            ORG_ID,
            SANDBOX_ID,
            "echo $FOO",
            "/workspace",
            {"FOO": "bar baz's"},
            lambda _c: None,
            timeout=5,
        )

    assert received_payload["command"] == "FOO='bar baz'\\''s' echo $FOO"
    assert received_payload["working_dir"] == "/workspace"


# --- env sanitization --------------------------------------------------


def test_stream_exec_invalid_env_key_raises_value_error_before_any_io() -> None:
    # No respx mock registered and no local server started — if this
    # reached the network at all, it would fail with something other than
    # ValueError (a connection error), so this also proves ordering.
    transport = Transport(api_key="sk_test", base_url="http://127.0.0.1:9")

    with pytest.raises(ValueError, match="invalid environment variable name"):
        stream_exec(
            transport,
            ORG_ID,
            SANDBOX_ID,
            "echo hi",
            "/workspace",
            {"not a valid key": "x"},
            lambda _c: None,
            timeout=5,
        )


# --- missing extra -------------------------------------------------------


def test_stream_exec_missing_websockets_raises_clear_quome_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "websockets", None)
    # No respx mock registered — proves the import check runs before any I/O.
    transport = Transport(api_key="sk_test", base_url="http://127.0.0.1:9")

    with pytest.raises(QuomeError, match=r"quome\[ws\]"):
        stream_exec(
            transport,
            ORG_ID,
            SANDBOX_ID,
            "echo hi",
            "/workspace",
            None,
            lambda _c: None,
            timeout=5,
        )


# --- timeout ---------------------------------------------------------------


def test_stream_exec_raises_timeout_error_if_server_never_closes(
    ws_server: Callable[[WsHandler], int],
) -> None:
    async def handler(ws: websockets.ServerConnection) -> None:
        await ws.recv()
        # Never send anything and never close on our own — the point of
        # this test is that the *client* gives up. Block on a second
        # recv() so the handler exits as soon as the client disconnects
        # (after its own timeout fires and it closes its side) instead of
        # hanging around: `Server.close()` during fixture teardown awaits
        # every handler task to finish, so a handler that never returns
        # would hang the whole test suite. NOTE: deliberately not
        # asyncio.sleep() here — the conftest autouse fixture monkeypatches
        # the *global* asyncio.sleep (it patches the shared `asyncio`
        # module object quome._transport imports, not a private copy), so
        # a real asyncio.sleep call in this background thread would
        # resolve instantly too, and the handler returning early would
        # close the connection before the client's own timeout ever fires.
        with contextlib.suppress(websockets.ConnectionClosed):
            await ws.recv()

    port = ws_server(handler)
    base_url = f"http://127.0.0.1:{port}"
    transport = Transport(api_key="sk_test", base_url=base_url)

    with respx.mock:
        _mock_ticket(base_url)
        with pytest.raises(TimeoutError):
            stream_exec(
                transport,
                ORG_ID,
                SANDBOX_ID,
                "sleep 100",
                "/workspace",
                None,
                lambda _c: None,
                timeout=0.3,
            )


# --- async happy path --------------------------------------------------
#
# Mirrors test_stream_exec_streams_chunks_and_returns_combined_stdout above,
# but drives stream_exec_async directly. Unlike the sync path (which wraps
# the shared WS coroutine in asyncio.run() and therefore needs the local WS
# server to run in a background thread with its own event loop, to avoid
# colliding with the thread asyncio.run() spins up), stream_exec_async never
# calls asyncio.run() itself — it's meant to be awaited directly from an
# already-running loop (that's the whole reason AsyncSandbox uses it instead
# of stream_exec). So the local server can just run as an async context
# manager on the same event loop as the test.
#
# This asserts exit_code is None (the streaming-protocol limitation
# documented on stream_exec_async) and asserts the payload the server
# actually received — both of which only pass if the *real* stream_exec_async
# coroutine ran end-to-end (ticket POST -> WS connect -> send -> recv loop),
# not a monkeypatched stand-in.


async def test_stream_exec_async_streams_chunks_and_returns_combined_stdout() -> None:
    received_payload: dict[str, Any] = {}

    async def handler(ws: websockets.ServerConnection) -> None:
        received_payload.update(json.loads(await ws.recv()))
        await ws.send("chunk-1 ")
        await ws.send("chunk-2 ")
        await ws.send("chunk-3")
        await ws.close()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        transport = AsyncTransport(api_key="sk_test", base_url=base_url)

        chunks: list[str] = []
        with respx.mock:
            _mock_ticket(base_url)
            result = await stream_exec_async(
                transport,
                ORG_ID,
                SANDBOX_ID,
                "echo hi",
                "/workspace",
                None,
                chunks.append,
                timeout=5,
            )

    assert result == ExecResult(exit_code=None, stdout="chunk-1 chunk-2 chunk-3", stderr="")
    assert chunks == ["chunk-1 ", "chunk-2 ", "chunk-3"]
    assert received_payload == {"command": "echo hi", "working_dir": "/workspace"}
