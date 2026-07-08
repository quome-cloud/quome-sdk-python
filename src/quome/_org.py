"""Org resolution: derive the caller's org id from the API key.

Resolution order:

1. ``QUOME_ORG_ID`` env var, if set — no HTTP call at all. This is the escape
   hatch for CI / multi-org service accounts that want to pin a specific org
   without relying on the key's default.
2. ``GET /api/v1/api-keys/self`` — every Quome API key is scoped to exactly
   one org, and this endpoint reports it.

Nothing here holds process-global state. Callers that want the result
memoized across multiple calls (the ``Client`` object in a later task) should
hold an :class:`OrgResolver` instance — one per client — rather than caching
at module scope, so two clients configured with different API keys never
share a cached org id.
"""

from __future__ import annotations

import os

from ._transport import AsyncTransport, Transport


def _org_id_from_self_response(body: dict[str, object]) -> str:
    org_id = body["org_id"]
    assert isinstance(org_id, str)  # noqa: S101 - guards a malformed server response
    return org_id


def resolve_org(transport: Transport) -> str:
    """Return the org id for the API key backing ``transport``.

    Honors ``QUOME_ORG_ID`` first (no HTTP call); otherwise calls
    ``GET /api/v1/api-keys/self`` and returns the ``org_id`` field.
    """
    env_org_id = os.environ.get("QUOME_ORG_ID")
    if env_org_id:
        return env_org_id

    response = transport.request("GET", "/api/v1/api-keys/self")
    return _org_id_from_self_response(response.json())


async def resolve_org_async(transport: AsyncTransport) -> str:
    """Async mirror of :func:`resolve_org` over an :class:`AsyncTransport`."""
    env_org_id = os.environ.get("QUOME_ORG_ID")
    if env_org_id:
        return env_org_id

    response = await transport.request("GET", "/api/v1/api-keys/self")
    return _org_id_from_self_response(response.json())


class OrgResolver:
    """Per-instance memoizing wrapper around :func:`resolve_org`.

    Intended for a client object to hold one of these and call
    :meth:`resolve` wherever it needs the org id, instead of resolving (and
    re-hitting the network) on every call.
    """

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._org_id: str | None = None

    def resolve(self) -> str:
        if self._org_id is None:
            self._org_id = resolve_org(self._transport)
        return self._org_id


class AsyncOrgResolver:
    """Async mirror of :class:`OrgResolver` over an :class:`AsyncTransport`."""

    def __init__(self, transport: AsyncTransport) -> None:
        self._transport = transport
        self._org_id: str | None = None

    async def resolve(self) -> str:
        if self._org_id is None:
            self._org_id = await resolve_org_async(self._transport)
        return self._org_id
