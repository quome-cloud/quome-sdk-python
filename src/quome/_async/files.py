"""``sbx.files`` — async read/write/list/delete for files inside a sandbox.

Mirrors :class:`quome.files.SandboxFiles` method-for-method — enforced by
``tests/test_parity.py``. Wire format, URL shapes, and payload parsing are
identical to the sync surface (see that module's docstring for the full wire
format); ``_entry_name`` is imported directly from it rather than
duplicated. The only difference is that every method here is a coroutine.
"""

from __future__ import annotations

import posixpath
from collections.abc import Awaitable
from typing import Any, Protocol, cast

from ..files import _entry_name


class _AsyncSandboxLike(Protocol):
    """The slice of an AsyncSandbox that AsyncSandboxFiles needs.

    A Protocol rather than a back-import of ``AsyncSandbox`` — see the same
    note in ``quome.files._SandboxLike``. ``_base_path`` is async here.
    """

    _client: Any

    def _base_path(self) -> Awaitable[str]: ...


class AsyncSandboxFiles:
    """File operations for a single async sandbox's workspace."""

    def __init__(self, sandbox: _AsyncSandboxLike) -> None:
        self._sandbox = sandbox

    async def read(self, path: str) -> bytes:
        """Download the file at ``path`` and return its raw bytes."""
        base_path = await self._sandbox._base_path()
        response = await self._sandbox._client.transport.request(
            "GET",
            f"{base_path}/files/download",
            params={"path": path},
        )
        return cast(bytes, response.content)

    async def write(self, path: str, content: str | bytes) -> None:
        """Write ``content`` to ``path``, creating or overwriting the file.

        See :meth:`quome.files.SandboxFiles.write` for the wire format.
        """
        raw = content.encode("utf-8") if isinstance(content, str) else content
        filename = posixpath.basename(path) or path
        base_path = await self._sandbox._base_path()
        await self._sandbox._client.transport.request(
            "PUT",
            f"{base_path}/files",
            params={"path": path},
            files={"file": (filename, raw)},
        )

    async def list(self, path: str = "/workspace") -> list[str]:
        """List entry names under ``path`` (default: the sandbox workspace root)."""
        base_path = await self._sandbox._base_path()
        response = await self._sandbox._client.transport.request(
            "GET",
            f"{base_path}/files",
            params={"path": path},
        )
        payload: Any = response.json()
        entries = payload if isinstance(payload, list) else []
        return [name for entry in entries if (name := _entry_name(entry))]

    async def delete(self, path: str) -> None:
        """Delete the file (or directory) at ``path``."""
        base_path = await self._sandbox._base_path()
        await self._sandbox._client.transport.request(
            "DELETE",
            f"{base_path}/files",
            params={"path": path},
        )
