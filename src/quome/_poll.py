"""Polling helpers shared by sandbox lifecycle waits and async exec jobs.

Both helpers are deliberately dumb about *how* to fetch state — they take a
zero-arg callable and poll it — so they work equally well against
``GET /sandboxes/{id}`` (status polling) and ``GET /sandboxes/{id}/exec/{job_id}``
(job polling) without either caller needing to know about the other's shape.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Container
from typing import Any

from ._exceptions import SandboxProvisioningError

_DEFAULT_STATUS_POLL_INTERVAL = 2.0

#: Sandbox statuses that can never transition to "running" — shared with
#: :mod:`quome.sandbox`, which imports this rather than redefining it.
TERMINAL_FAIL_STATES = frozenset({"failed", "error", "deleted"})
_DEFAULT_TERMINAL_FAIL = TERMINAL_FAIL_STATES
_JOB_BACKOFF_INITIAL = 1.0
_JOB_BACKOFF_MAX = 3.0
_JOB_BACKOFF_MULTIPLIER = 1.5


def wait_for_status(
    fetch: Callable[[], dict[str, Any]],
    want: str,
    terminal_fail: Container[str] = _DEFAULT_TERMINAL_FAIL,
    timeout: float = 300,
    interval: float = _DEFAULT_STATUS_POLL_INTERVAL,
) -> dict[str, Any]:
    """Poll ``fetch()`` until its ``status`` field equals ``want``.

    Raises :class:`~quome._exceptions.SandboxProvisioningError` the moment
    ``status`` lands in ``terminal_fail`` — a sandbox that's already
    failed/errored/been deleted will never reach ``want``, so there's no
    reason to wait out the rest of ``timeout``. Raises ``TimeoutError`` if
    ``want`` isn't observed before the deadline.
    """
    deadline = time.monotonic() + timeout
    while True:
        body = fetch()
        status = body.get("status")

        if status == want:
            return body

        if status in terminal_fail:
            raise SandboxProvisioningError(
                f"sandbox {body.get('id', '?')} entered terminal state {status!r} "
                f"while waiting for status {want!r}",
                sandbox_id=body.get("id"),
                status=str(status) if status is not None else None,
            )

        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"timed out after {timeout}s waiting for status {want!r} "
                f"(last observed: {status!r})"
            )

        time.sleep(interval)


def poll_job(
    fetch_status: Callable[[], dict[str, Any]],
    timeout: float,
    initial_backoff: float = _JOB_BACKOFF_INITIAL,
    max_backoff: float = _JOB_BACKOFF_MAX,
) -> dict[str, Any]:
    """Poll an async exec job until it reports ``status == "done"``.

    Backs off from ``initial_backoff`` seconds up to ``max_backoff`` seconds
    between polls (default 1s -> 3s), so a long-running command isn't
    hammering the API every second. Raises ``TimeoutError`` if the job
    hasn't finished within ``timeout`` seconds.
    """
    deadline = time.monotonic() + timeout
    backoff = initial_backoff
    while True:
        body = fetch_status()

        if body.get("status") == "done":
            return body

        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"timed out after {timeout}s waiting for exec job to complete "
                f"(last status: {body.get('status')!r})"
            )

        time.sleep(min(backoff, max_backoff))
        backoff = min(backoff * _JOB_BACKOFF_MULTIPLIER, max_backoff)


async def wait_for_status_async(
    fetch: Callable[[], Awaitable[dict[str, Any]]],
    want: str,
    terminal_fail: Container[str] = _DEFAULT_TERMINAL_FAIL,
    timeout: float = 300,
    interval: float = _DEFAULT_STATUS_POLL_INTERVAL,
) -> dict[str, Any]:
    """Async mirror of :func:`wait_for_status` — polls a coroutine instead."""
    deadline = time.monotonic() + timeout
    while True:
        body = await fetch()
        status = body.get("status")

        if status == want:
            return body

        if status in terminal_fail:
            raise SandboxProvisioningError(
                f"sandbox {body.get('id', '?')} entered terminal state {status!r} "
                f"while waiting for status {want!r}",
                sandbox_id=body.get("id"),
                status=str(status) if status is not None else None,
            )

        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"timed out after {timeout}s waiting for status {want!r} "
                f"(last observed: {status!r})"
            )

        await asyncio.sleep(interval)


async def poll_job_async(
    fetch_status: Callable[[], Awaitable[dict[str, Any]]],
    timeout: float,
    initial_backoff: float = _JOB_BACKOFF_INITIAL,
    max_backoff: float = _JOB_BACKOFF_MAX,
) -> dict[str, Any]:
    """Async mirror of :func:`poll_job` — polls a coroutine instead."""
    deadline = time.monotonic() + timeout
    backoff = initial_backoff
    while True:
        body = await fetch_status()

        if body.get("status") == "done":
            return body

        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"timed out after {timeout}s waiting for exec job to complete "
                f"(last status: {body.get('status')!r})"
            )

        await asyncio.sleep(min(backoff, max_backoff))
        backoff = min(backoff * _JOB_BACKOFF_MULTIPLIER, max_backoff)
