"""The asynchronous Quome client: an ``AsyncTransport`` plus a lazily-resolved,
memoized org id.

Mirrors ``quome.client.Quome`` method-for-method — enforced by
``tests/test_parity.py``. The only structural difference is that org id
resolution and connection teardown are coroutines here: ``await
client.org_id()`` and ``await client.close()``, instead of a plain property
and a sync method. That's an unavoidable consequence of resolving the org id
over the network — a ``@property`` can't await.
"""

from __future__ import annotations

from .._org import AsyncOrgResolver
from .._transport import AsyncTransport


class AsyncQuome:
    """Asynchronous Quome API client.

    Holds an :class:`~quome._transport.AsyncTransport` and resolves + caches
    the org id for the presented API key on first use (see
    :class:`~quome._org.AsyncOrgResolver`). Constructing an ``AsyncQuome``
    never makes a network call and never requires ``QUOME_API_KEY`` to
    already be set — both the API key and the org id are resolved lazily, on
    the first request that needs them.
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.transport = AsyncTransport(api_key=api_key, base_url=base_url)
        self._org_resolver = AsyncOrgResolver(self.transport)

    async def org_id(self) -> str:
        """The org id for the API key backing this client.

        Resolved once (``QUOME_ORG_ID`` env var, or ``GET /api/v1/api-keys/self``)
        and cached for the lifetime of this client.
        """
        return await self._org_resolver.resolve()

    async def close(self) -> None:
        await self.transport.aclose()

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.transport!r})"


_default_client: AsyncQuome | None = None


def default_async_client() -> AsyncQuome:
    """Async counterpart of :func:`quome.client.default_client`.

    Returns the process-wide default async client, constructing it from
    environment variables (``QUOME_API_KEY`` / ``QUOME_BASE_URL`` /
    ``QUOME_ORG_ID``) the first time it's needed.
    """
    global _default_client
    if _default_client is None:
        _default_client = AsyncQuome()
    return _default_client
