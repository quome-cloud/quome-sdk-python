"""The synchronous Quome client: a transport plus a lazily-resolved org id.

See ``quome._async.client.AsyncQuome`` for the async mirror of this surface
over ``AsyncTransport`` — enforced identical (method names) by
``tests/test_parity.py``.
"""

from __future__ import annotations

from ._org import OrgResolver
from ._transport import Transport


class Quome:
    """Synchronous Quome API client.

    Holds a :class:`~quome._transport.Transport` and resolves + caches the
    org id for the presented API key on first use (see
    :class:`~quome._org.OrgResolver`). Constructing a ``Quome`` never makes a
    network call and never requires ``QUOME_API_KEY`` to already be set —
    both the API key and the org id are resolved lazily, on the first
    request that needs them.
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.transport = Transport(api_key=api_key, base_url=base_url)
        self._org_resolver = OrgResolver(self.transport)

    @property
    def org_id(self) -> str:
        """The org id for the API key backing this client.

        Resolved once (``QUOME_ORG_ID`` env var, or ``GET /api/v1/api-keys/self``)
        and cached for the lifetime of this client.
        """
        return self._org_resolver.resolve()

    def close(self) -> None:
        self.transport.close()

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.transport!r})"


_default_client: Quome | None = None


def default_client() -> Quome:
    """Return the process-wide default client, constructing it from
    environment variables (``QUOME_API_KEY`` / ``QUOME_BASE_URL`` /
    ``QUOME_ORG_ID``) the first time it's needed.

    Backs the module-level convenience surface, e.g.
    ``quome.Sandbox.create(...)`` called without an explicit ``client=``.
    """
    global _default_client
    if _default_client is None:
        _default_client = Quome()
    return _default_client
