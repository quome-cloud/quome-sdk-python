"""HTTP transport layer: auth headers, base URL resolution, retries, and
mapping HTTP error responses onto the typed exception hierarchy.

The API key is resolved lazily on the first request (not at construction) so
that constructing a client never raises just because ``QUOME_API_KEY`` isn't
set yet — useful for import-time client instances. Nothing here ever puts the
key into a log line, an exception message, or a ``repr()``.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Any

import httpx

from ._exceptions import (
    AuthenticationError,
    NotFoundError,
    QuomeAPIError,
    QuotaExceededError,
    SandboxNotRunningError,
)
from ._version import __version__

DEFAULT_BASE_URL = "https://api.quome.studio"

_MAX_GET_ATTEMPTS = 3
_RETRYABLE_STATUS = frozenset({502, 503, 504})
_BACKOFF_MIN_SECONDS = 0.2
_BACKOFF_MAX_SECONDS = 1.0
_SANDBOX_NOT_RUNNING_SIGNALS = ("not_running", "not running", "stopped")


def _jitter_backoff() -> float:
    return random.uniform(_BACKOFF_MIN_SECONDS, _BACKOFF_MAX_SECONDS)  # noqa: S311


def _resolve_base_url(base_url: str | None) -> str:
    resolved = base_url or os.environ.get("QUOME_BASE_URL") or DEFAULT_BASE_URL
    return resolved.rstrip("/")


def _resolve_api_key(api_key: str | None) -> str:
    key = api_key or os.environ.get("QUOME_API_KEY")
    if not key:
        raise AuthenticationError("QUOME_API_KEY not set")
    return key


def _build_headers(api_key: str | None, extra: dict[str, str]) -> dict[str, str]:
    headers = dict(extra)
    headers["X-API-Key"] = _resolve_api_key(api_key)
    headers["User-Agent"] = f"quome-python/{__version__}"
    return headers


def _extract_detail(response: httpx.Response) -> str:
    try:
        data: Any = response.json()
    except ValueError:
        return response.text
    if isinstance(data, dict):
        detail = data.get("detail", data)
        return detail if isinstance(detail, str) else str(detail)
    return str(data)


def _parse_retry_after(response: httpx.Response) -> float | None:
    header = response.headers.get("retry-after")
    if header is None:
        return None
    try:
        return float(header)
    except ValueError:
        return None


def _is_sandbox_not_running(detail: str) -> bool:
    lowered = detail.lower()
    return any(signal in lowered for signal in _SANDBOX_NOT_RUNNING_SIGNALS)


def raise_for_api_error(response: httpx.Response) -> None:
    """Raise the appropriate typed exception for a non-2xx response. No-op on 2xx."""
    if response.is_success:
        return

    status = response.status_code
    detail = _extract_detail(response)

    if status in (401, 403):
        raise AuthenticationError(detail, status_code=status)
    if status == 404:
        raise NotFoundError(status, detail)
    if status == 429:
        raise QuotaExceededError(status, detail, retry_after=_parse_retry_after(response))
    if status == 409 and _is_sandbox_not_running(detail):
        raise SandboxNotRunningError(status, detail)
    raise QuomeAPIError(status, detail)


class Transport:
    """Synchronous HTTP transport for the Quome API."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self._api_key = api_key
        self.base_url = _resolve_base_url(base_url)
        self._client = httpx.Client(base_url=self.base_url)

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        extra_headers: dict[str, str] = dict(kwargs.pop("headers", None) or {})
        headers = _build_headers(self._api_key, extra_headers)
        max_attempts = _MAX_GET_ATTEMPTS if method.upper() == "GET" else 1

        for attempt in range(1, max_attempts + 1):
            try:
                response = self._client.request(method, path, headers=headers, **kwargs)
            except httpx.TransportError:
                if attempt >= max_attempts:
                    raise
                time.sleep(_jitter_backoff())
                continue

            if response.status_code in _RETRYABLE_STATUS and attempt < max_attempts:
                time.sleep(_jitter_backoff())
                continue

            raise_for_api_error(response)
            return response

        raise AssertionError("unreachable")  # pragma: no cover

    def close(self) -> None:
        self._client.close()

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(base_url={self.base_url!r})"


class AsyncTransport:
    """Asynchronous HTTP transport for the Quome API."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self._api_key = api_key
        self.base_url = _resolve_base_url(base_url)
        self._client = httpx.AsyncClient(base_url=self.base_url)

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        extra_headers: dict[str, str] = dict(kwargs.pop("headers", None) or {})
        headers = _build_headers(self._api_key, extra_headers)
        max_attempts = _MAX_GET_ATTEMPTS if method.upper() == "GET" else 1

        for attempt in range(1, max_attempts + 1):
            try:
                response = await self._client.request(method, path, headers=headers, **kwargs)
            except httpx.TransportError:
                if attempt >= max_attempts:
                    raise
                await asyncio.sleep(_jitter_backoff())
                continue

            if response.status_code in _RETRYABLE_STATUS and attempt < max_attempts:
                await asyncio.sleep(_jitter_backoff())
                continue

            raise_for_api_error(response)
            return response

        raise AssertionError("unreachable")  # pragma: no cover

    async def aclose(self) -> None:
        await self._client.aclose()

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(base_url={self.base_url!r})"
