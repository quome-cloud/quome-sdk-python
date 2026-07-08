"""Async mirror of ``quome.sandbox.Sandbox`` — lifecycle, exec dispatch,
files, preview_url, over :class:`~quome._transport.AsyncTransport`.

Mirrors :class:`~quome.sandbox.Sandbox` method-for-method — enforced by
``tests/test_parity.py``. All routing rules (the three ``run()`` exec modes,
the ``preview_url`` public-only rule), URL shapes, and payload parsing are
identical to the sync surface — ``_as_dict`` and ``_exec_result_from_payload``
are imported directly from :mod:`quome.sandbox` rather than duplicated. The
only difference is that every method that does I/O (or reads ``org_id``,
which itself does I/O on first use) is a coroutine.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .._models import ExecResult
from .._poll import TERMINAL_FAIL_STATES, poll_job_async, wait_for_status_async
from .._stream import stream_exec_async
from ..sandbox import _SYNC_EXEC_TIMEOUT_CEILING, _as_dict, _exec_result_from_payload
from ..templates import resolve_template_async
from .client import AsyncQuome, default_async_client
from .files import AsyncSandboxFiles


class AsyncSandbox:
    """An async handle to a Quome sandbox: an isolated, ephemeral compute
    environment.

    Construct via :meth:`create` or :meth:`get` rather than the constructor
    directly.
    """

    def __init__(self, client: AsyncQuome, data: dict[str, Any]) -> None:
        self._client = client
        self._data = data
        self.files = AsyncSandboxFiles(self)

    # -- fields -----------------------------------------------------------

    @property
    def id(self) -> str:
        return str(self._data["id"])

    @property
    def status(self) -> str:
        return str(self._data["status"])

    @property
    def proxy_subdomain(self) -> str | None:
        value = self._data.get("proxy_subdomain")
        return str(value) if value is not None else None

    @property
    def exposed_ports(self) -> list[int]:
        return list(self._data.get("exposed_ports") or [])

    @property
    def expires_at(self) -> str | None:
        value = self._data.get("expires_at")
        return str(value) if value is not None else None

    @property
    def created_at(self) -> str | None:
        value = self._data.get("created_at")
        return str(value) if value is not None else None

    @property
    def started_at(self) -> str | None:
        value = self._data.get("started_at")
        return str(value) if value is not None else None

    async def org_id(self) -> str:
        return await self._client.org_id()

    async def _base_path(self) -> str:
        return f"/api/v1/orgs/{await self.org_id()}/sandboxes/{self.id}"

    async def _get(self, suffix: str = "") -> dict[str, Any]:
        base_path = await self._base_path()
        response = await self._client.transport.request("GET", f"{base_path}{suffix}")
        return _as_dict(response.json())

    async def _post(self, suffix: str = "", json: dict[str, Any] | None = None) -> dict[str, Any]:
        base_path = await self._base_path()
        response = await self._client.transport.request("POST", f"{base_path}{suffix}", json=json)
        return _as_dict(response.json())

    # -- create / get / list ------------------------------------------------

    @classmethod
    async def create(
        cls,
        template: str,
        name: str | None = None,
        resources: dict[str, Any] | None = None,
        idle_timeout: int | None = None,
        wait: bool = True,
        timeout: float = 300,
        client: AsyncQuome | None = None,
    ) -> AsyncSandbox:
        """Create a sandbox from ``template`` (a template name or id).

        See :meth:`quome.sandbox.Sandbox.create` for the full contract
        (template resolution, ``wait`` semantics, failure handling) — it's
        identical here, just awaited.
        """
        client = client or default_async_client()
        org_id = await client.org_id()
        template_id = await resolve_template_async(client.transport, org_id, template)

        body: dict[str, Any] = {"template_id": template_id}
        if name is not None:
            body["name"] = name
        if resources is not None:
            body["resources"] = resources
        if idle_timeout is not None:
            body["idle_timeout"] = idle_timeout

        response = await client.transport.request(
            "POST", f"/api/v1/orgs/{org_id}/sandboxes", json=body
        )
        sandbox = cls(client, _as_dict(response.json()))

        if wait:
            await sandbox._wait_until_running(timeout)

        return sandbox

    async def _wait_until_running(self, timeout: float) -> None:
        self._data = await wait_for_status_async(
            self._get, want="running", terminal_fail=TERMINAL_FAIL_STATES, timeout=timeout
        )

    @classmethod
    async def get(cls, sandbox_id: str, client: AsyncQuome | None = None) -> AsyncSandbox:
        """Fetch a single sandbox by id."""
        client = client or default_async_client()
        org_id = await client.org_id()
        response = await client.transport.request(
            "GET", f"/api/v1/orgs/{org_id}/sandboxes/{sandbox_id}"
        )
        return cls(client, _as_dict(response.json()))

    @classmethod
    async def list(cls, client: AsyncQuome | None = None) -> list[AsyncSandbox]:
        """List all sandboxes in the org."""
        client = client or default_async_client()
        org_id = await client.org_id()
        response = await client.transport.request("GET", f"/api/v1/orgs/{org_id}/sandboxes")
        payload: Any = response.json()
        items = payload.get("data", []) if isinstance(payload, dict) else payload
        return [cls(client, _as_dict(item)) for item in items]

    # -- lifecycle ----------------------------------------------------------

    async def refresh(self) -> AsyncSandbox:
        """Re-fetch this sandbox's state from the API and update in place."""
        self._data = await self._get()
        return self

    async def stop(self) -> AsyncSandbox:
        self._data = await self._post("/stop")
        return self

    async def resume(self) -> AsyncSandbox:
        self._data = await self._post("/resume")
        return self

    async def delete(self) -> None:
        base_path = await self._base_path()
        await self._client.transport.request("DELETE", base_path)

    async def extend(self) -> AsyncSandbox:
        self._data = await self._post("/extend")
        return self

    # -- exec -----------------------------------------------------------

    async def run(
        self,
        cmd: str,
        timeout: float = 300,
        working_dir: str = "/workspace",
        env: dict[str, str] | None = None,
        on_stdout: Callable[[str], None] | None = None,
    ) -> ExecResult:
        """Run ``cmd`` in the sandbox and return its result.

        Same three-mode dispatch as :meth:`quome.sandbox.Sandbox.run` (see
        its docstring for the full rationale): ``on_stdout`` given routes to
        the streaming WS path; otherwise ``timeout <= 240`` is a single
        synchronous ``POST /exec``, and ``timeout > 240`` submits an async
        exec job and polls it to completion.
        """
        if on_stdout is not None:
            return await stream_exec_async(
                self._client.transport,
                await self.org_id(),
                self.id,
                cmd,
                working_dir,
                env,
                on_stdout,
                timeout,
            )

        if timeout <= _SYNC_EXEC_TIMEOUT_CEILING:
            return await self._run_sync(cmd, timeout, working_dir, env)

        return await self._run_async_job(cmd, timeout, working_dir, env)

    async def _run_sync(
        self, command: str, timeout: float, working_dir: str, env: dict[str, str] | None
    ) -> ExecResult:
        payload = await self._post(
            "/exec",
            json={
                "command": command,
                "timeout": timeout,
                "working_dir": working_dir,
                "env": env or {},
                "mode": "sync",
            },
        )
        return _exec_result_from_payload(payload)

    async def _run_async_job(
        self, command: str, timeout: float, working_dir: str, env: dict[str, str] | None
    ) -> ExecResult:
        submitted = await self._post(
            "/exec",
            json={
                "command": command,
                "timeout": timeout,
                "working_dir": working_dir,
                "env": env or {},
                "mode": "async",
            },
        )
        job_id = submitted["job_id"]

        payload = await poll_job_async(lambda: self._get(f"/exec/{job_id}"), timeout=timeout)
        return _exec_result_from_payload(payload)

    # -- preview ----------------------------------------------------------

    async def preview_url(self, port: int, *, public: bool) -> str:
        """Expose ``port`` and return its public preview URL.

        ``public`` is required and keyword-only — calling without it raises
        ``TypeError`` (Python's own argument-binding, before this coroutine
        is even awaited). The returned URL is **publicly internet-reachable**
        — see :meth:`quome.sandbox.Sandbox.preview_url` for the full
        rationale. Passing ``public=False`` raises ``ValueError``.
        """
        if not public:
            raise ValueError("preview_url requires public=True; the URL is internet-reachable")

        payload = await self._post("/ports", json={"port": port})
        return str(payload["url"])

    def __repr__(self) -> str:
        return f"AsyncSandbox(id={self.id!r}, status={self.status!r})"
