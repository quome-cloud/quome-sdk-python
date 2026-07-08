"""``sbx.files`` — read/write/list/delete for files inside a sandbox.

All operations are scoped to the owning :class:`~quome.sandbox.Sandbox` and
go through its transport, so a ``SandboxFiles`` instance is never created
standalone — access it via ``sandbox.files``.

Wire format (from ``app/api/v1/sandbox/sandboxes.py`` on the control plane):

- ``write``: ``PUT .../files?path=<path>`` as ``multipart/form-data`` with a
  file field named ``file`` (the endpoint signature is
  ``file: UploadFile = File(...)``; ``path`` is a query param).
- ``read``: ``GET .../files/download?path=<path>`` — raw bytes, not JSON.
- ``list``: ``GET .../files?path=<path>`` — a bare JSON list of
  ``{"name": str, "type": "file" | "directory", ...}`` dicts.
- ``delete``: ``DELETE .../files?path=<path>``.
"""

from __future__ import annotations

import posixpath
from typing import Any, Protocol, cast


class _SandboxLike(Protocol):
    """The slice of a Sandbox that SandboxFiles needs.

    Typed as a Protocol rather than importing ``Sandbox`` directly so this
    module has no dependency on ``sandbox.py`` — ``sandbox.py`` imports
    ``SandboxFiles`` at runtime, and a back-import here (even under
    ``TYPE_CHECKING``) is what CodeQL's ``py/unsafe-cyclic-import`` flags.
    The Protocol breaks the cycle for real: files.py → nothing, sandbox.py →
    files.py is a clean one-way edge.
    """

    _client: Any

    def _base_path(self) -> str: ...


class SandboxFiles:
    """File operations for a single sandbox's workspace."""

    def __init__(self, sandbox: _SandboxLike) -> None:
        self._sandbox = sandbox

    def read(self, path: str) -> bytes:
        """Download the file at ``path`` and return its raw bytes."""
        response = self._sandbox._client.transport.request(
            "GET",
            f"{self._sandbox._base_path()}/files/download",
            params={"path": path},
        )
        return cast(bytes, response.content)

    def write(self, path: str, content: str | bytes) -> None:
        """Write ``content`` to ``path``, creating or overwriting the file.

        Sent as ``multipart/form-data`` with the file bytes under the
        ``file`` field and ``path`` as a query param — matching the
        ``file: UploadFile = File(...)`` endpoint signature. ``str`` content
        is UTF-8 encoded; ``bytes`` content is sent as-is (no decode), so
        binary payloads round-trip byte-for-byte.
        """
        raw = content.encode("utf-8") if isinstance(content, str) else content
        filename = posixpath.basename(path) or path
        self._sandbox._client.transport.request(
            "PUT",
            f"{self._sandbox._base_path()}/files",
            params={"path": path},
            files={"file": (filename, raw)},
        )

    def list(self, path: str = "/workspace") -> list[str]:
        """List entry names under ``path`` (default: the sandbox workspace root)."""
        response = self._sandbox._client.transport.request(
            "GET",
            f"{self._sandbox._base_path()}/files",
            params={"path": path},
        )
        payload: Any = response.json()
        entries = payload if isinstance(payload, list) else []
        return [name for entry in entries if (name := _entry_name(entry))]

    def delete(self, path: str) -> None:
        """Delete the file (or directory) at ``path``."""
        self._sandbox._client.transport.request(
            "DELETE",
            f"{self._sandbox._base_path()}/files",
            params={"path": path},
        )


def _entry_name(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("name") or "")
    return ""
