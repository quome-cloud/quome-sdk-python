"""Response dataclasses shared across sandbox exec paths.

Kept in their own module (rather than ``sandbox.py``) so that ``sandbox.py``
and ``_stream.py`` can both depend on :class:`ExecResult` without importing
each other — ``sandbox.py`` imports ``stream_exec`` from ``_stream.py``, so
the reverse import would be circular.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecResult:
    """The result of running a command in a sandbox, from any of the three
    exec paths (sync, async job, or streaming).

    ``exit_code`` is ``None`` only for the streaming path (``Sandbox.run(...,
    on_stdout=...)``) — the exec WebSocket protocol carries a single combined
    output stream with no exit code and no separate stderr channel, so
    ``stderr`` is always ``""`` there too. The sync and async-job paths
    always populate a real ``int`` exit code; use one of those (no
    ``on_stdout``) if you need it.
    """

    exit_code: int | None
    stdout: str
    stderr: str
